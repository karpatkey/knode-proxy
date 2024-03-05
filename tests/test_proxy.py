import logging
import queue
import socket
import time
import threading
from unittest.mock import patch

import uvicorn
import httpx
import web3
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

import proxy

logger = logging.getLogger()

PROXY_PORT = 8000
PROXY_URL = f"http://127.0.0.1:{PROXY_PORT}/"
FAKE_NODE_PORT = 8888
FAKE_NODE_URL = f"http://127.0.0.1:{FAKE_NODE_PORT}/"

_fake_data = queue.Queue()

fake_upstream_node = proxy.UpstreamNode(FAKE_NODE_URL)


def get_next_fake_data():
    return _fake_data.get(block=False)


async def fake_node_root(request):
    data = await request.json()
    upstream_data, status_code = get_next_fake_data()
    logger.debug(f"Fake node: {upstream_data}, {status_code}")
    return JSONResponse(content=upstream_data, status_code=status_code)


routes = [
    Route("/", endpoint=fake_node_root, methods=["POST"]),
]

fake_node = Starlette(routes=routes)


def run_proxy():
    uvicorn.run(proxy.app, port=PROXY_PORT)


def run_fake_node():
    uvicorn.run(fake_node, port=FAKE_NODE_PORT)


def wait_for_port(port, host='localhost', timeout=5.0):
    start_time = time.time()
    while True:
        try:
            s = socket.create_connection((host, port), timeout=timeout)
            s.close()
            return
        except socket.error:
            time.sleep(0.05)
            if time.time() - start_time >= timeout:
                raise socket.error("Timeout waiting for port")


# TODO: move to fixture
proxy_thread = threading.Thread(target=run_proxy)
proxy_thread.daemon = True
proxy_thread.start()

fake_node_thread = threading.Thread(target=run_fake_node)
fake_node_thread.daemon = True
fake_node_thread.start()
wait_for_port(PROXY_PORT)
wait_for_port(FAKE_NODE_PORT)

proxy.cache_enable(False)

def test_get_balance():
    w3 = web3.Web3(web3.HTTPProvider(PROXY_URL + "ethereum"))

    # {'jsonrpc': '2.0', 'method': 'eth_getBalance', 'params': ['0x6CF63938f2CD5DFEBbDE0010bb640ed7Fa679693', '0x1272617'], 'id': 1}
    balance = w3.eth.get_balance("0x6CF63938f2CD5DFEBbDE0010bb640ed7Fa679693", block_identifier=19342871)
    assert balance == 10000000000000000


def test_chain_id():
    w3 = web3.Web3(web3.HTTPProvider(PROXY_URL + "ethereum"))
    assert w3.eth.chain_id == 1


def test_with_fake_node():
    with patch.object(proxy, "get_upstream_node_for_blockchain", lambda b: fake_upstream_node):
        w3 = web3.Web3(web3.HTTPProvider(PROXY_URL + "ethereum"))

        _fake_data.put(({'jsonrpc': '2.0', 'id': 1, 'result': '0x1'}, 200))
        _fake_data.put(({'jsonrpc': '2.0', 'id': 1, 'result': '0x2386f26fc10000'}, 200))

        assert w3.eth.chain_id == 1

        balance = w3.eth.get_balance("0x6CF63938f2CD5DFEBbDE0010bb640ed7Fa679693", block_identifier=19342871)
        assert balance == 10000000000000000


def test_with_fake_node_500_error():
    assert _fake_data.empty()
    with patch.object(proxy, "get_upstream_node_for_blockchain", lambda b: fake_upstream_node):
        _fake_data.put(({'jsonrpc': '2.0', 'id': 1, 'error': {"code": -32003, "message": "Transaction rejected"}}, 500))

        response = httpx.post(PROXY_URL + "ethereum",
                              json={'jsonrpc': '2.0', 'method': 'eth_getBalance',
                                    'params': ['0x6CF63938f2CD5DFEBbDE0010bb640ed7Fa679693', '0x1272619'], 'id': 1})

        assert response.status_code == 503


def test_upstream_node_selector():
    node_a = proxy.UpstreamNode("a")
    node_b = proxy.UpstreamNode("b")
    selector = proxy.UpstreamNodeSelector(nodes=[node_a, node_b])
    assert selector.get_node() == node_a
    assert selector.get_node() == node_b
    assert selector.get_node() == node_a
    assert selector.get_node() == node_b
    node_a.status = proxy.NodeStatus.UNHEALTHY
    # one node unhealthy, continue with the next healthy one
    assert selector.get_node() == node_b
    assert selector.get_node() == node_b
    node_b.status = proxy.NodeStatus.UNHEALTHY

    # both nodes are unhealthy, just continue retrying with the unhealthy
    assert selector.get_node() == node_a
    assert selector.get_node() == node_b

    # b is now healthy again
    node_b.status = proxy.NodeStatus.HEALTHY
    assert selector.get_node() == node_b
    assert selector.get_node() == node_b

    # a is now healthy again
    node_a.status = proxy.NodeStatus.HEALTHY
    assert selector.get_node() == node_a
    assert selector.get_node() == node_b