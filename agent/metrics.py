
import os
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, REGISTRY, generate_latest

_METRICS_SERVER_STARTED = False
_METRICS_SERVER = None

REQUEST_COUNT = Counter(
    "agent_requests_total",
    "Total number of agent requests processed by the application.",
    ["status"],
)

REQUEST_LATENCY = Histogram(
    "agent_request_latency_seconds",
    "Latency for agent request processing in seconds.",
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0),
)

AGENT_EXECUTION_DURATION = Histogram(
    "agent_execution_duration_seconds",
    "Agent execution duration in seconds.",
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0),
)

TOOL_FAILURES = Counter(
    "agent_tool_failures_total",
    "Total tool execution failures, keyed by tool name and failure status.",
    ["tool_name", "status"],
)

TOKEN_USAGE = Counter(
    "agent_tokens_total",
    "Model token usage for agent requests. Values are only recorded when the runtime has real or mock usage data.",
    ["model"],
)

TOKEN_COST = Counter(
    "agent_token_cost_usd_total",
    "Estimated token cost in USD when usage and pricing data are available.",
    ["model"],
)

class _AgentMetricsHandler(BaseHTTPRequestHandler):
    """Serve the agent health response and Prometheus metrics."""

    def do_GET(self) -> None:
        route = urlsplit(self.path).path
        if route == "/health":
            from agent.agent import get_health_status

            body = json.dumps(get_health_status()).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if route == "/metrics":
            body = generate_latest(REGISTRY)
            self.send_response(200)
            self.send_header("Content-Type", CONTENT_TYPE_LATEST)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        body = b'{"error":"Not found"}'
        self.send_response(404)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:
        return


def create_metrics_server(port: int = 8000, addr: str = "0.0.0.0") -> ThreadingHTTPServer:
    """Create an HTTP server for health checks and Prometheus scraping."""
    server = ThreadingHTTPServer((addr, port), _AgentMetricsHandler)
    server.daemon_threads = True
    return server


def start_metrics_server(port: int = 8000, addr: str = "0.0.0.0") -> ThreadingHTTPServer:
    """Serve /health and /metrics at http://localhost:<port>."""
    global _METRICS_SERVER_STARTED, _METRICS_SERVER
    if _METRICS_SERVER_STARTED:
        return _METRICS_SERVER
    _METRICS_SERVER = create_metrics_server(port=port, addr=addr)
    _METRICS_SERVER_STARTED = True
    thread = threading.Thread(
        target=_METRICS_SERVER.serve_forever,
        kwargs={"poll_interval": 0.1},
        name="agent-http-server",
        daemon=True,
    )
    thread.start()
    return _METRICS_SERVER


def record_request(status: str, latency_seconds: float, estimated_cost_usd: float = 0.0) -> None:
    """Record success or failure metrics for a single request."""
    REQUEST_COUNT.labels(status=status).inc()
    REQUEST_LATENCY.observe(max(0.0, latency_seconds))
    if estimated_cost_usd > 0:
        TOKEN_COST.labels(model="agent").inc(float(estimated_cost_usd))


def record_agent_execution(duration_seconds: float, status: str) -> None:
    """Record the length of the agent processing loop."""
    AGENT_EXECUTION_DURATION.observe(max(0.0, duration_seconds))
    if status not in {"success", "error"}:
        return


def record_tool_failure(tool_name: str, status: str = "error") -> None:
    """Record a tool execution failure without exposing a raw request or prompt."""
    TOOL_FAILURES.labels(tool_name=tool_name, status=status).inc()


def record_model_usage(model: str, input_tokens: int | None = None, output_tokens: int | None = None, estimated_cost_usd: float | None = None) -> None:
    """Record usage and cost only when the model layer supplies them."""
    if input_tokens is not None and output_tokens is not None:
        TOKEN_USAGE.labels(model=model).inc(int(input_tokens) + int(output_tokens))
    elif input_tokens is not None:
        TOKEN_USAGE.labels(model=model).inc(int(input_tokens))
    if estimated_cost_usd is not None:
        TOKEN_COST.labels(model=model).inc(float(estimated_cost_usd))


def metrics_port() -> int:
    """Return the configured metrics port, defaulting to 8000."""
    try:
        return int(os.getenv("METRICS_PORT", "8000"))
    except ValueError:
        return 8000
