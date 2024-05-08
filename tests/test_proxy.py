import logging
from unittest.mock import patch

import httpx

import cache
import proxy
from rpc import NodeStatus
from tests.utils import PROXY_URL, get_proxy_eth_node

logger = logging.getLogger()

cache.cache_enable(False)


def assert_needs_authentication(url):
    response = httpx.get(url)
    assert response.status_code == 403


def test_get_balance_real_upstream(proxy_server):
    w3 = get_proxy_eth_node()

    # {'jsonrpc': '2.0', 'method': 'eth_getBalance', 'params': ['0x6CF63938f2CD5DFEBbDE0010bb640ed7Fa679693', '0x1272617'], 'id': 1}
    balance = w3.eth.get_balance("0x6CF63938f2CD5DFEBbDE0010bb640ed7Fa679693", block_identifier=19342871)
    assert balance == 10000000000000000


def test_chain_id_real_upstream(proxy_server):
    w3 = get_proxy_eth_node()
    assert w3.eth.chain_id == 1


def test_get_balance(proxy_server, fake_upstream):
    w3 = get_proxy_eth_node()
    fake_upstream.add_responses(
        [
            ({"jsonrpc": "2.0", "id": 1, "result": "0x1"}, 200),
            ({"jsonrpc": "2.0", "id": 1, "result": "0x2386f26fc10000"}, 200),
        ]
    )

    assert w3.eth.chain_id == 1

    balance = w3.eth.get_balance("0x6CF63938f2CD5DFEBbDE0010bb640ed7Fa679693", block_identifier=19342871)
    assert balance == 10000000000000000


def test_with_fake_node_500_error(proxy_server, fake_upstream):
    req_id = 22
    fake_upstream.set_default_response(
        {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32003, "message": "Transaction rejected"}}, 500
    )

    response = httpx.post(
        PROXY_URL + "chain/ethereum?key=test-user",
        json={
            "jsonrpc": "2.0",
            "method": "eth_getBalance",
            "params": ["0x6CF63938f2CD5DFEBbDE0010bb640ed7Fa679693", "0x1272619"],
            "id": req_id,
        },
    )

    assert response.status_code == 200
    assert response.json()["error"]["code"] == 502
    assert response.json()["id"] == req_id


def test_unauthenticated(proxy_server):
    response = httpx.post(
        PROXY_URL + "chain/ethereum",
        json={
            "jsonrpc": "2.0",
            "method": "eth_getBalance",
            "params": ["0x6CF63938f2CD5DFEBbDE0010bb640ed7Fa679693", "0x1272619"],
            "id": 1,
        },
    )
    assert response.status_code == 403


def test_bad_authentication(proxy_server):
    response = httpx.post(
        PROXY_URL + "chain/ethereum?key=bad-actor",
        json={
            "jsonrpc": "2.0",
            "method": "eth_getBalance",
            "params": ["0x6CF63938f2CD5DFEBbDE0010bb640ed7Fa679693", "0x1272619"],
            "id": 1,
        },
    )
    assert response.status_code == 400


def test_cache_chain_id(proxy_server, fake_upstream, cache_enabled):
    w3 = get_proxy_eth_node()

    fake_upstream.add_responses(
        [
            ({"jsonrpc": "2.0", "id": 1, "result": "0x1"}, 200),
        ]
    )

    assert w3.eth.chain_id == 1

    # If cache does not work then the assert will fail
    # as there is only one fake response
    assert w3.eth.chain_id == 1


def test_get_status(proxy_server):
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
    node_a.status = NodeStatus.UNHEALTHY
    # one node unhealthy, continue with the next healthy one
    assert selector.get_node() == node_b
    assert selector.get_node() == node_b
    node_b.status = NodeStatus.UNHEALTHY

    # both nodes are unhealthy, just continue retrying with the unhealthy
    assert selector.get_node() == node_a
    assert selector.get_node() == node_b

    # b is now healthy again
    node_b.status = NodeStatus.HEALTHY
    assert selector.get_node() == node_b
    assert selector.get_node() == node_b

    # a is now healthy again
    node_a.status = NodeStatus.HEALTHY
    assert selector.get_node() == node_a
    assert selector.get_node() == node_b


def test_debug_rpc_calls(proxy_server, fake_upstream):
    w3 = get_proxy_eth_node()
    fake_upstream.add_responses(
        [
            ({"jsonrpc": "2.0", "id": 1, "result": "0x123"}, 200),
        ]
    )

    proxy.debug_last_rpc_calls.clear()

    balance = w3.eth.get_balance("0x6CF63938f2CD5DFEBbDE0010bb640ed7Fa679693", block_identifier=19342871)
    response = httpx.get(PROXY_URL + "debug/rpc_calls?key=test-user")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data == [
        {
            "req": {
                "jsonrpc": "2.0",
                "method": "eth_getBalance",
                "params": ["0x6CF63938f2CD5DFEBbDE0010bb640ed7Fa679693", "0x1272617"],
                "id": 0,
            },
            "resp": {"jsonrpc": "2.0", "id": 1, "result": "0x123"},
            "cached": False,
        }
    ]

    assert_needs_authentication(PROXY_URL + "debug/rpc_calls")


def test_debug_nodes(proxy_server):
    with patch.object(proxy, "ENDPOINTS", {}):
        proxy.setup_nodes({"ethereum": ["https://my.node/eth"], "gnosis": {"http://gnosis/xxx?key=1"}})

        response = httpx.get(PROXY_URL + "debug/nodes?key=test-user")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data == {
        "ethereum": [{"hostname": "my.node", "status": "HEALTHY"}],
        "gnosis": [{"hostname": "gnosis", "status": "HEALTHY"}],
    }

    assert_needs_authentication(PROXY_URL + "debug/nodes")


def test_bad_upstream_response(proxy_server, fake_upstream):
    # check https://github.com/karpatkey/knode-proxy/issues/24
    w3 = get_proxy_eth_node()

    responses = [({"jsonrpc": "2.0", "id": 1, "result": "0x"}, 200)] * (proxy.MAX_UPSTREAM_TRIES_FOR_REQUEST - 1)
    responses.append(({"jsonrpc": "2.0", "id": 1, "result": "good"}, 200))
    fake_upstream.add_responses(responses)

    response = httpx.post(
        PROXY_URL + "chain/ethereum?key=test-user",
        json={
            "jsonrpc": "2.0",
            "method": "eth_getBalance",
            "params": ["0x6CF63938f2CD5DFEBbDE0010bb640ed7Fa679693", "0x1272619"],
            "id": "1",
        },
    )

    assert response.status_code == 200
    assert response.json()["result"] == "good"


def test_bad_upstream_response_give_up(proxy_server, fake_upstream):
    responses = [({"jsonrpc": "2.0", "id": 1, "result": "0x"}, 200)] * proxy.MAX_UPSTREAM_TRIES_FOR_REQUEST
    fake_upstream.add_responses(responses)

    response = httpx.post(
        PROXY_URL + "chain/ethereum?key=test-user",
        json={
            "jsonrpc": "2.0",
            "method": "eth_getBalance",
            "params": ["0x6CF63938f2CD5DFEBbDE0010bb640ed7Fa679693", "0x1272619"],
            "id": "1",
        },
    )

    assert response.status_code == 200
    assert response.json()["result"] == "0x"


def test_no_cache(proxy_server, fake_upstream, cache_enabled):
    responses = [({"jsonrpc": "2.0", "id": 1, "result": "0x1"}, 200)] * 2
    fake_upstream.add_responses(responses)

    def get_chain_id():
        return httpx.post(
            PROXY_URL + "chain/ethereum?key=test-user&no-cache",
            json={
                "jsonrpc": "2.0",
                "method": "eth_chainId",
                "params": [],
                "id": "1",
            },
        )

    response = get_chain_id()
    assert response.status_code == 200
    assert response.json()["result"] == "0x1"
    assert fake_upstream.fake_data_q.qsize() == 1

    response = get_chain_id()
    assert response.status_code == 200
    assert response.json()["result"] == "0x1"
    assert fake_upstream.fake_data_q.qsize() == 0
