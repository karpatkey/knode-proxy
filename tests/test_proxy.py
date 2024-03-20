import logging
from unittest.mock import patch

import httpx

import cache
import proxy
from tests.utils import PROXY_URL, fake_upstream, proxy_server, get_node

logger = logging.getLogger()

cache.cache_enable(False)


def test_get_balance_real_upstream(proxy_server):
    w3 = get_node(PROXY_URL + "ethereum")

    # {'jsonrpc': '2.0', 'method': 'eth_getBalance', 'params': ['0x6CF63938f2CD5DFEBbDE0010bb640ed7Fa679693', '0x1272617'], 'id': 1}
    balance = w3.eth.get_balance("0x6CF63938f2CD5DFEBbDE0010bb640ed7Fa679693", block_identifier=19342871)
    assert balance == 10000000000000000


def test_chain_id_real_upstream(proxy_server):
    w3 = get_node(PROXY_URL + "ethereum")
    assert w3.eth.chain_id == 1


def test_get_balance(proxy_server, fake_upstream):
    with patch.object(proxy, "get_upstream_node_for_blockchain", lambda b: fake_upstream.node):
        w3 = get_node(PROXY_URL + "ethereum")

        fake_upstream.add_responses([
            ({'jsonrpc': '2.0', 'id': 1, 'result': '0x1'}, 200),
            ({'jsonrpc': '2.0', 'id': 1, 'result': '0x2386f26fc10000'}, 200),
        ])

        assert w3.eth.chain_id == 1

        balance = w3.eth.get_balance("0x6CF63938f2CD5DFEBbDE0010bb640ed7Fa679693", block_identifier=19342871)
        assert balance == 10000000000000000


def test_with_fake_node_500_error(proxy_server, fake_upstream):
    with patch.object(proxy, "get_upstream_node_for_blockchain", lambda b: fake_upstream.node):
        req_id = 22
        fake_upstream.set_default_response(
            {'jsonrpc': '2.0', 'id': req_id, 'error': {"code": -32003, "message": "Transaction rejected"}},
            500)

        response = httpx.post(PROXY_URL + "ethereum",
                              json={'jsonrpc': '2.0', 'method': 'eth_getBalance',
                                    'params': ['0x6CF63938f2CD5DFEBbDE0010bb640ed7Fa679693', '0x1272619'], 'id': req_id})

        assert response.status_code == 200
        assert response.json()["error"]["code"] == 502
        assert response.json()["id"] == req_id

def test_get_balance(proxy_server):
    response = httpx.get(PROXY_URL + "status")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"

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
