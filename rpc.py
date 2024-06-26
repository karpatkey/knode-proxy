import asyncio
import enum
import itertools
import logging
import os
import random
import ssl
import time

import anyio
import httpx

import metrics

logger = logging.getLogger("rpc")

MAX_UPSTREAM_TRIES_FOR_REQUEST = 5
MAX_HTTP_CONNECTIONS = int(os.environ.get("KPROXY_NODE_MAX_CONNECTIONS", 20))

ENDPOINTS: dict[str, "UpstreamNodeSelector"] = {}
HTTPX_LIMITS = httpx.Limits(max_keepalive_connections=MAX_HTTP_CONNECTIONS, max_connections=MAX_HTTP_CONNECTIONS)

on_startup = []

class NodeNotHealthy(Exception):
    pass


class NodeStatus(enum.Enum):
    HEALTHY = 1
    UNHEALTHY = 2


class UpstreamNode:
    HEALTH_CHECK_INTERVAL_S = 10.0

    def __init__(self, url: str):
        self.url = url
        self.client = httpx.AsyncClient(limits=HTTPX_LIMITS)
        self.status: NodeStatus = NodeStatus.HEALTHY
        on_startup.append(self.start)

    async def check_loop(self):
        while True:
            await anyio.sleep(self.HEALTH_CHECK_INTERVAL_S + random.random() * self.HEALTH_CHECK_INTERVAL_S / 2)
            logger.info("Checking health of %s", self)
            await self.health_check()

    def start(self):
        asyncio.create_task(self.check_loop())

    def set_status(self, status: NodeStatus):
        self.status = status
        metrics.upstream_state.labels(upstream_node=self.url).set(status.value)

    def __str__(self) -> str:
        return f"UpstreamNode({self.url})"

    async def health_check(self):
        # ask something that won't be already cached by the upstream
        block = random.randint(1, 100000)
        data = {
            "jsonrpc": "2.0",
            "method": "eth_getBalance",
            "params": ["0x6CF63938f2CD5DFEBbDE0010bb640ed7Fa679693", hex(block)],
            "id": 0x43A174,
        }
        try:
            await self.make_request(data)
        except Exception:
            logger.exception("health checking error")

    async def make_request(self, data: dict) -> httpx.Response:
        metrics.upstream_requests_total.labels(upstream_node=self.url, rpc_method=data.get("method", "unknown")).inc()
        metrics.upstream_requests_concurrent.labels(upstream_node=self.url).inc()
        error = None
        try:
            start_time = time.monotonic()
            response = await self.client.post(self.url, json=data)
            metrics.upstream_latency_s.labels(upstream_node=self.url).observe(time.monotonic() - start_time)
            if response.status_code != 200:
                error = f"{self} returned status code == {response.status_code}"
        except (httpx.HTTPError, anyio.EndOfStream, ssl.SSLError, Exception) as exc:
            error = f"Exception found on {self}.make_request: {repr(exc)}"

        metrics.upstream_requests_concurrent.labels(upstream_node=self.url).dec()

        if error:
            self.set_status(NodeStatus.UNHEALTHY)
            logger.warning(error)
            raise NodeNotHealthy(error)
        else:
            self.set_status(NodeStatus.HEALTHY)

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


async def make_request(node: UpstreamNode, data: dict):
    upstream_response = await node.make_request(data)
    logger.debug(f"upstream status code {upstream_response.status_code}")
    resp_data = upstream_response.json()
    return resp_data
