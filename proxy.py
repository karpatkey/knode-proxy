import asyncio
import enum
import itertools
import logging
import os
import json
import random
import time

import anyio
import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
import uvicorn

import metrics
from cache import is_cache_enabled, is_cacheable, cache, generate_cache_key

logger = logging.getLogger("proxy")

MAX_UPSTREAM_TRIES_FOR_REQUEST = 5
MAX_HTTP_CONNECTIONS = 10
MAX_KEEPALIVE_CONNECTIONS = 10

cfg_data = os.environ.get("KPROXY_NODE_CFG", "")
if not cfg_data:
    if os.path.exists(os.environ.get("KPROXY_NODE_CFG_FILE", "")):
        cfg_data = open(os.environ.get("KPROXY_NODE_CFG_FILE")).read()
    else:
        raise RuntimeError("KPROXY_NODE_CFG or KPROXY_NODE_CFG_FILE must be defined")

config = json.loads(cfg_data)

AUTHORIZED_KEYS = os.environ.get("KPROXY_AUTHORIZED_KEYS", "").strip()
if AUTHORIZED_KEYS:
    AUTHORIZED_KEYS = AUTHORIZED_KEYS.split(",")


class NodeNotHealthy(Exception):
    pass


class NodeStatus(enum.Enum):
    HEALTHY = 1
    UNHEALTHY = 2


ENDPOINTS: dict[str, "UpstreamNodeSelector"] = {}

httpx_limits = httpx.Limits(max_keepalive_connections=MAX_HTTP_CONNECTIONS, max_connections=MAX_KEEPALIVE_CONNECTIONS)


class UpstreamNode:
    HEALTH_CHECK_INTERVAL_S = 10.

    def __init__(self, endpoint: str):
        self.endpoint = endpoint
        self.client = httpx.AsyncClient(limits=httpx_limits)
        self.status: NodeStatus = NodeStatus.HEALTHY

        async def check_loop():
            while True:
                await anyio.sleep(self.HEALTH_CHECK_INTERVAL_S + random.random() * self.HEALTH_CHECK_INTERVAL_S / 2)
                await self.health_check()

        try:
            asyncio.get_running_loop()
            asyncio.create_task(check_loop())
        except RuntimeError:
            pass

    def __str__(self) -> str:
        return f"UpstreamNode({self.endpoint})"

    async def health_check(self):
        # ask something that won't be already cached by the upstream
        block = random.randint(1, 100000)
        data = {'jsonrpc': '2.0',
                'method': 'eth_getBalance',
                'params': ['0x6CF63938f2CD5DFEBbDE0010bb640ed7Fa679693', hex(block)],
                'id': 0x43a174}
        start = time.monotonic()
        try:
            await self.make_request(data)
        except Exception:
            pass

    async def make_request(self, data: dict) -> httpx.Response:
        try:
            response = await self.client.post(self.endpoint, json=data)
        except (httpx.HTTPError, anyio.EndOfStream) as exc:
            self.status = NodeStatus.UNHEALTHY
            logger.warning(f"{repr(exc)} for {self}")
            raise NodeNotHealthy(f"{self} {exc}")

        if response.status_code != 200:
            self.status = NodeStatus.UNHEALTHY
            logger.warning(f"status code {response.status_code} != 200 for {self}")
            raise NodeNotHealthy(f"{self} returned status code == {response.status_code}")
        else:
            self.status = NodeStatus.HEALTHY
        return response


class UpstreamNodeSelector:
    def __init__(self, nodes: list[UpstreamNode]):
        self.nodes = nodes
        self._cyclic_iterator = itertools.cycle(self.nodes)

    def get_node(self) -> UpstreamNode:
        node = next(self._cyclic_iterator)
        cycle_counter = itertools.count()
        # cycle at least one round but move to the next one in the cycle if all are unhealthy
        while node.status != NodeStatus.HEALTHY and next(cycle_counter) <= len(self.nodes) + 1:
            node = next(self._cyclic_iterator)
        return node


def get_upstream_node_for_blockchain(blockchain: str) -> UpstreamNode:
    try:
        node = ENDPOINTS[blockchain].get_node()
    except KeyError:
        raise NotImplementedError(f"Not supported blockchain {blockchain}")
    return node


async def make_request(node: UpstreamNode, blockchain: str, data: dict):
    method = data['method']
    params = data.get('params', [])

    # Generating the cache key before knowing if it is cacheable is not performant,
    # but it simplifies the functions as geting the response is done only in one place
    params_hash = generate_cache_key(params)
    cache_key = f"{blockchain}.{method}.{params_hash}"
    if cache_key not in cache or not is_cache_enabled():
        upstream_response = await node.make_request(data)
        logger.debug(f"upstream status code {upstream_response.status_code}")
        resp_data = upstream_response.json()

        if is_cacheable(method, params) and is_cache_enabled():
            if "error" not in resp_data and "result" in resp_data and resp_data["result"] is not None:
                cache[cache_key] = ("result", resp_data["result"])
            elif "error" in resp_data:
                # These errors are "good ones", we want to cache the error because they should be invariant
                # (don't depend on the node nor the time)
                ERRORS_TO_CACHE = {-32000, -32015}
                if resp_data["error"]["code"] in ERRORS_TO_CACHE:
                    cache[cache_key] = ("error", resp_data["error"])
        else:
            logger.debug(f"Not caching '{method}' with params: '{params}'")

        return resp_data
    else:
        key, data = cache[cache_key]
        return {"jsonrpc": "2.0", "id": 11, key: data, "cached": True}


def error_response(request_data, code, message, data=None):
    resp = {"jsonrpc": "2.0", "id": request_data["id"], "error": {"code": code, "message": message}}
    if data is not None:
        resp["error"]["data"] = data
    return JSONResponse(content=resp)


def set_metric_ctx(request, key, value):
    request.scope["metrics_ctx"][key] = value


async def root(request: Request):
    request_data = await request.json()
    if AUTHORIZED_KEYS:
        key = request.query_params.get("key", "")
        if key not in AUTHORIZED_KEYS:
            return error_response(request_data, code=401, message="Unauthorized")

    blockchain = request.path_params['blockchain']

    if blockchain not in ENDPOINTS:
        return error_response(request_data, code=404, message=f"No RPC nodes for blockchain {blockchain}")

    set_metric_ctx(request, key="rpc", value=True)
    set_metric_ctx(request, key="blockchain", value=blockchain)
    set_metric_ctx(request, key="method", value=request_data.get("method", "unknown"))

    for upstream_try in range(MAX_UPSTREAM_TRIES_FOR_REQUEST):
        node = get_upstream_node_for_blockchain(blockchain)
        logger.info(f"Get request for '{blockchain}' to {node.endpoint}, try {upstream_try}, with data: {request_data!s:.100}")
        error = False
        start_time = time.monotonic()
        try:
            metrics.upstream_requests_total.labels(upstream_node=node.endpoint,
                                                   rpc_method=request_data.get("method", "unknown")).inc()
            upstream_data = await make_request(node, blockchain, request_data)

            upstream_data["id"] = request_data["id"]
        except NodeNotHealthy:
            error = True
        finally:
            metrics.upstream_latency_s.labels(upstream_node=node.endpoint).observe(time.monotonic() - start_time)
            if error:
                continue

        logger.info(f"Response for '{blockchain}' with data: {upstream_data!s:.100}")
        set_metric_ctx(request, key="upstream_tries", value=upstream_try + 1)
        set_metric_ctx(request, key="cached", value=upstream_data.pop("cached", False))
        return JSONResponse(content=upstream_data)

    set_metric_ctx(request, key="error", value=502)
    return error_response(request_data, code=502, message="Can't get a good response from upstream nodes")


async def status(request: Request):
    return JSONResponse(content={"status": "ok"})


# Load nodes from the config
for network, endpoints in config['nodes'].items():
    ENDPOINTS[network] = UpstreamNodeSelector([UpstreamNode(endpoint) for endpoint in endpoints])

routes = [
    Route("/status", endpoint=status, methods=["GET"]),
    Route("/{blockchain}", endpoint=root, methods=["POST"]),
]


class MetricsMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        start_time = time.monotonic()

        response = await call_next(request)

        print(request.scope["metrics_ctx"])
        duration = time.monotonic() - start_time
        metrics.http_request_duration_s.observe(duration)
        metrics.http_requests_total.labels(status_code=str(response.status_code)).inc()
        if response.status_code != 200:
            metrics.http_errors_total.inc()
        return response


class MonitoringMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        scope["metrics_ctx"] = {}
        start_time = time.monotonic()
        try:
            await self.app(scope, receive, send)
        finally:
            duration = time.monotonic() - start_time
            metrics.http_request_duration_s.observe(duration)
            if "rpc" in scope["metrics_ctx"]:
                status = str(scope["metrics_ctx"].get("error", 200))
                blockchain = scope["metrics_ctx"].get("blockchain")
                cached = scope["metrics_ctx"].get("cached")
                method = scope["metrics_ctx"].get("method")
                metrics.rpc_requests_total.labels(status_code=status,
                                                  rpc_method=method,
                                                  blockchain=blockchain,
                                                  cached=cached).inc()

                if status != 200:
                    metrics.http_errors_total.inc()


middleware = [
    Middleware(MonitoringMiddleware)
]

app = Starlette(routes=routes, middleware=middleware)

if __name__ == "__main__":
    if not AUTHORIZED_KEYS:
        logging.warning("No AUTHORIZED_KEYS configured, everyone with access can use the service!")

    logger.info("Prometheus metrics HTTP running on http://127.0.0.1:9999")
    metrics.start_http_server(addr="127.0.0.1", port=9999)

    # TODO: in depth review of the logging config
    log_cfg = {
        "version": 1,
        "disable_existing_loggers": True,
        "formatters": {
            "standard": {"format": "%(asctime)s [%(levelname)s] %(name)s: %(message)s"},
        },
        "handlers": {
            "default": {
                "level": "INFO",
                "formatter": "standard",
                "class": "logging.StreamHandler",
                "stream": "ext://sys.stdout",
            },
        },
        "loggers": {
            "": {
                "level": "INFO",
                "handlers": [],
                "propagate": True,
            },
            "proxy": {
                "level": "INFO",
                "handlers": ["default"],
                "propagate": False,
            },
            "uvicorn.error": {
                "level": "DEBUG",
                "handlers": ["default"],
                "propagate": False,
            },
            "uvicorn.access": {
                "level": "DEBUG",
                "handlers": ["default"],
            },
        },
    }
    print(logger)
    uvicorn.run(app, port=8888, log_level="info", log_config=log_cfg)
