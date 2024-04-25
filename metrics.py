import time

from prometheus_client import Counter, Histogram, Gauge, start_http_server

rpc_requests_total = Counter(
    "rpc_requests_total",
    documentation="Total rpc requests processed",
    labelnames=["status_code", "rpc_method", "blockchain", "cached"],
)

http_request_duration_s = Histogram(
    "http_request_duration_s",
    documentation="HTTP request processing duration in seconds",
)
http_errors_total = Counter(
    "http_errors_total",
    documentation="Total HTTP requests resulting in errors",
)
upstream_requests_total = Counter(
    name="http_upstream_requests_total",
    documentation="Total requests to upstream servers",
    labelnames=["upstream_node", "rpc_method"],
)
upstream_latency_s = Histogram(
    name="http_upstream_latency_s",
    documentation="Latency of upstream server in seconds",
    labelnames=["upstream_node"],
)

upstream_errors_total = Counter(
    name="upstream_errors_total",
    documentation="Total upstream requests resulting in errors",
    labelnames=["upstream_node"]
)

upstream_state = Gauge(
    name="upstream_status",
    documentation="The status HEALTHY=1, UNHEALTHY=2",
    labelnames=["upstream_node"]
)

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
            http_request_duration_s.observe(duration)
            if "rpc" in scope["metrics_ctx"]:
                status = str(scope["metrics_ctx"].get("error", 200))
                blockchain = scope["metrics_ctx"].get("blockchain")
                cached = scope["metrics_ctx"].get("cached")
                method = scope["metrics_ctx"].get("method")
                rpc_requests_total.labels(status_code=status,
                                          rpc_method=method,
                                          blockchain=blockchain,
                                          cached=cached).inc()

                if status != 200:
                    http_errors_total.inc()
