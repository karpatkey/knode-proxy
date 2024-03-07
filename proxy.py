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
from starlette.responses import PlainTextResponse, JSONResponse
from starlette.routing import Route
from web3.middleware.cache import generate_cache_key

from cache import is_cache_enabled, is_cacheable, cache

logger = logging.getLogger()

MAX_UPSTREAM_TRIES_FOR_REQUEST = 5
MAX_HTTP_CONNECTIONS = 10
MAX_KEEPALIVE_CONNECTIONS = 10


class NodeNotHealthy(Exception):
    pass


class NodeStatus(enum.Enum):
    HEALTHY = 1
    UNHEALTHY = 2


httpx_limits = httpx.Limits(max_keepalive_connections=MAX_HTTP_CONNECTIONS, max_connections=MAX_KEEPALIVE_CONNECTIONS)


class UpstreamNode:
    HEALTH_CHECK_INTERVAL_S = 10.

    def __init__(self, endpoint: str):
        self.endpoint = endpoint
        self.client = httpx.AsyncClient(limits=httpx_limits)
        self.status: NodeStatus = NodeStatus.HEALTHY
        self._last_latency = None

        async def check_loop():
            while True:
                await anyio.sleep(self.HEALTH_CHECK_INTERVAL_S + random.random() * self.HEALTH_CHECK_INTERVAL_S / 2)
                await self.health_check()

        asyncio.create_task(check_loop())

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
        self._last_latency = time.monotonic() - start

    async def make_request(self, data: dict) -> httpx.Response:
        try:
            response = await self.client.post(self.endpoint, json=data)
        except httpx.HTTPError as exc:
            self.status = NodeStatus.UNHEALTHY
            logger.warning(f"httpx.HTTPError {repr(exc)} for {self}")
            raise NodeNotHealthy(f"{self} {exc}")
        except anyio.EndOfStream as exc:
            self.status = NodeStatus.UNHEALTHY
            logger.warning(f"anyio.EndOfStream {exc} for {self}")
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


# TODO: An independent Health checker that polls every N seconds must be added so that
# nodes can be bring back to a HEALTHY status after they go into UNHEALTHY

ENDPOINTS = {
    "ethereum": UpstreamNodeSelector([
        UpstreamNode("https://foo"),
    ]),
    "gnosis": UpstreamNodeSelector([
        UpstreamNode("https://rpc.ankr.com/gnosis"),
    ])
}


def get_upstream_node_for_blockchain(blockchain: str) -> UpstreamNode:
    try:
        node = ENDPOINTS[blockchain].get_node()
    except KeyError:
        raise NotImplementedError(f"Not supported blockchain {blockchain}")
    return node


async def make_request(node: UpstreamNode, blockchain: str, data: dict):
    logging.info(f"data: {data}")
    method = data['method']
    params = data.get('params', [])

    # Generating the cache key before knowing if it is cacheable is not performant,
    # but it simplifies the functions as geting the response is done only in one place
    params_hash = generate_cache_key(params)
    cache_key = f"{blockchain}.{method}.{params_hash}"
    if cache_key not in cache or not is_cache_enabled():
        upstream_response = await node.make_request(data)
        logger.info(f"upstream status code {upstream_response.status_code}")
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
        return {"jsonrpc": "2.0", "id": 11, key: data}


async def root(request: Request):
    blockchain = request.path_params['blockchain']
    if blockchain not in ENDPOINTS:
        return PlainTextResponse(f'No RPC nodes for blockchain: {blockchain}', status_code=404)

    data = await request.json()
    for upstream_try in range(MAX_UPSTREAM_TRIES_FOR_REQUEST):
        node = get_upstream_node_for_blockchain(blockchain)
        logger.info(f"Get request for '{blockchain}' to {node.endpoint}, try {upstream_try}, with data: {data}")
        try:
            upstream_data = await make_request(node, blockchain, data)
        except NodeNotHealthy:
            continue
        logger.info(f"Response for '{blockchain}' with data: {upstream_data}")
        return JSONResponse(content=upstream_data)
    return JSONResponse(content={}, status_code=503)


routes = [
    Route("/{blockchain}", endpoint=root, methods=["POST"]),
]

app = Starlette(routes=routes)

logger.setLevel(logging.DEBUG)
