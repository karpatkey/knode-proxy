from unittest.mock import patch

import pytest
import uvicorn

import cache
import proxy
from utils import FakeUpstreamNode, FAKE_UPSTREAM_NODE_PORT, PROXY_PORT, UvicornThreadedServer


@pytest.fixture(scope="session")
def proxy_server():
    proxy.AUTHORIZED_KEYS = ["test-user"]
    config = uvicorn.Config(app=proxy.app, host="127.0.0.1", port=PROXY_PORT)
    server = UvicornThreadedServer(config=config)
    with server.run_in_thread():
        yield


@pytest.fixture(scope="session")
def fake_upstream_node():
    node = FakeUpstreamNode(FAKE_UPSTREAM_NODE_PORT)
    with node.server.run_in_thread():
        node.fake_data_q.queue.clear()
        yield node


@pytest.fixture(scope="function")
def fake_upstream(fake_upstream_node):
    with patch.object(proxy, "get_upstream_node_for_blockchain", lambda b: fake_upstream_node.node):
        yield fake_upstream_node


@pytest.fixture(scope="function")
def cache_enabled():
    cache.clear()
    cache.cache_enable(True)
    yield
    cache.clear()
    cache.cache_enable(False)
