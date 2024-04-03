import json
import logging
import os
import time

import uvicorn
from starlette.applications import Starlette
from starlette.authentication import AuthCredentials, AuthenticationBackend, AuthenticationError, SimpleUser
from starlette.authentication import requires
from starlette.middleware import Middleware
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

import cache
import metrics
from rpc import (
    ENDPOINTS,
    MAX_UPSTREAM_TRIES_FOR_REQUEST,
    get_upstream_node_for_blockchain,
    make_request,
    NodeNotHealthy,
    UpstreamNodeSelector,
    UpstreamNode,
)

logger = logging.getLogger("proxy")

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


class QueryAuthBackend(AuthenticationBackend):
    async def authenticate(self, conn):
        key = conn.query_params.get("key", None)
        if AUTHORIZED_KEYS and key is None:
            return
        if AUTHORIZED_KEYS and key not in AUTHORIZED_KEYS:
            raise AuthenticationError("Invalid credentials")
        else:
            return AuthCredentials(["authenticated"]), SimpleUser(key)


def build_error_response(rpc_id, code, message, data=None):
    resp = {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": code, "message": message}}
    if data is not None:
        resp["error"]["data"] = data
    return JSONResponse(content=resp)


def set_metric_ctx(request, key, value):
    request.scope["metrics_ctx"][key] = value


@requires("authenticated")
async def node_rpc(request: Request):
    try:
        request_data = await request.json()
    except json.JSONDecodeError:
        return build_error_response(rpc_id=None, code=-32700, message="Parse error")

    blockchain = request.path_params["blockchain"]

    if blockchain not in ENDPOINTS:
        return build_error_response(request_data["id"], code=404, message=f"No RPC nodes for blockchain {blockchain}")

    set_metric_ctx(request, key="rpc", value=True)
    set_metric_ctx(request, key="blockchain", value=blockchain)
    set_metric_ctx(request, key="method", value=request_data.get("method", "unknown"))

    for upstream_try in range(MAX_UPSTREAM_TRIES_FOR_REQUEST):
        node = get_upstream_node_for_blockchain(blockchain)
        logger.info(
            f"Get request for '{blockchain}' to {node.endpoint}, try {upstream_try}, with data: {request_data!s:.100}"
        )
        error = False
        start_time = time.monotonic()
        try:
            metrics.upstream_requests_total.labels(
                upstream_node=node.endpoint, rpc_method=request_data.get("method", "unknown")
            ).inc()
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
    return build_error_response(request_data["id"], code=502, message="Can't get a good response from upstream nodes")


async def status(request: Request):
    return JSONResponse(content={"status": "ok"})


@requires("authenticated")
async def cache_clear(request: Request):
    cache.clear()
    return JSONResponse(content={"status": "ok"})


# Load nodes from the config
for network, endpoints in config["nodes"].items():
    ENDPOINTS[network] = UpstreamNodeSelector([UpstreamNode(endpoint) for endpoint in endpoints])

routes = [
    Route("/status", endpoint=status, methods=["GET"]),
    Route("/chain/{blockchain}", endpoint=node_rpc, methods=["POST"]),
    Route("/cache/clear", endpoint=cache_clear, methods=["POST"]),
]

middleware = [
    Middleware(AuthenticationMiddleware, backend=QueryAuthBackend()),
    Middleware(metrics.MonitoringMiddleware),
]

app = Starlette(routes=routes, middleware=middleware)

if __name__ == "__main__":
    if not AUTHORIZED_KEYS:
        logging.warning("No AUTHORIZED_KEYS configured, everyone with access can use the service!")

    HOST = os.environ.get("KPROXY_HOST", "127.0.0.1")

    logger.info(f"Prometheus metrics HTTP running on http://{HOST}:9999")
    metrics.start_http_server(addr=HOST, port=9999)

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
    uvicorn.run(app, host=HOST, port=8888, log_level="info", log_config=log_cfg)
