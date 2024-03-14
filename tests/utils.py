import contextlib
import logging
import queue
import threading
import time

import pytest
import web3
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
import uvicorn

import proxy

logger = logging.getLogger()

PROXY_PORT = 8000
PROXY_URL = f"http://127.0.0.1:{PROXY_PORT}/"
FAKE_UPSTREAM_NODE_PORT = 8888
FAKE_UPSTREAM_NODE_URL = f"http://127.0.0.1:{FAKE_UPSTREAM_NODE_PORT}/"


class UvicornThreadedServer(uvicorn.Server):
    def install_signal_handlers(self):
        pass

    @contextlib.contextmanager
    def run_in_thread(self):
        thread = threading.Thread(target=self.run)
        thread.start()
        try:
            while not self.started:
                time.sleep(0.01)
            yield
        finally:
            self.should_exit = True
            thread.join()


class FakeUpstreamNode:
    def __init__(self, port, log_level='info'):
        self.fake_data_q = queue.Queue()
        routes = [
            Route("/", endpoint=self.fake_node_root, methods=["POST"]),
        ]
        self.app = Starlette(routes=routes)
        config = uvicorn.Config(app=self.app, host="127.0.0.1", port=port, log_level=log_level)
        self.server = UvicornThreadedServer(config=config)
        self.node = proxy.UpstreamNode(FAKE_UPSTREAM_NODE_URL)

    async def fake_node_root(self, request):
        data = await request.json()
        upstream_data, status_code = self.fake_data_q.get(block=False)
        logger.debug(f"Fake node: {upstream_data}, {status_code}")
        return JSONResponse(content=upstream_data, status_code=status_code)

    def add_responses(self, responses: list):
        for response in responses:
            self.fake_data_q.put(response)


@pytest.fixture(scope="session")
def proxy_server():
    config = uvicorn.Config(app=proxy.app, host="127.0.0.1", port=PROXY_PORT)
    server = UvicornThreadedServer(config=config)
    with server.run_in_thread():
        yield


@pytest.fixture(scope="session")
def fake_upstream():
    fake_upstream_node = FakeUpstreamNode(FAKE_UPSTREAM_NODE_PORT)
    with fake_upstream_node.server.run_in_thread():
        fake_upstream_node.fake_data_q.queue.clear()
        yield fake_upstream_node


def get_node(url):
    class HTTPProviderNoRetry(web3.HTTPProvider):
        # disable the retry middleware
        _middlewares = tuple()

    provider = HTTPProviderNoRetry(url)
    w3 = web3.Web3(provider)
    return w3
