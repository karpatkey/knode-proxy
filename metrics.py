from prometheus_client import Counter, Histogram, start_http_server

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
