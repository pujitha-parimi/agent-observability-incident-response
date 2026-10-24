
import json
import threading
from urllib.error import HTTPError
from urllib.request import urlopen

import pytest
from opentelemetry import trace

from agent import metrics
from agent.agent import execute_tool, get_health_status, run_agent
from agent.metrics import create_metrics_server
from agent.observability import configure_tracing, record_exception

@pytest.fixture
def live_metrics_server():
    server = create_metrics_server(port=0, addr="127.0.0.1")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize(
    "question, expected_status, expected_error",
    [
        ("What is observability?", "success", None),
        ("   ", "error", "Question cannot be empty"),
        ("fail this request", "error", "Tool failed to complete request"),
        ("drop table users", "error", "Request rejected by guardrail policy"),
    ],
)
def test_agent_execution_variants(question, expected_status, expected_error):
    result = run_agent(question)
    assert result["status"] == expected_status
    assert result["error"] == expected_error
    assert set(result.keys()) == {"status", "answer", "error", "latency_seconds"}


def test_health_status_and_response_shape():
    health = get_health_status()
    assert health["status"] == "ok"
    assert health["service"] == "agent-observability-system"

    result = run_agent("What is incident response?")
    assert result["status"] == "success"
    assert isinstance(result["answer"], str)
    assert result["error"] is None
    assert result["latency_seconds"] >= 0


def test_live_health_endpoint_returns_json(live_metrics_server):
    with urlopen(f"{live_metrics_server}/health") as response:
        body = json.loads(response.read())

    assert response.status == 200
    assert response.headers["Content-Type"].startswith("application/json")
    assert body == {
        "status": "ok",
        "service": "agent-observability-system",
        "metrics_port": metrics.metrics_port(),
        "tracing": "enabled",
    }


def test_live_metrics_endpoint_exposes_prometheus_metrics(live_metrics_server):
    run_agent("What is observability?")

    with urlopen(f"{live_metrics_server}/metrics") as response:
        body = response.read().decode("utf-8")

    assert response.status == 200
    assert "agent_requests_total" in body
    assert "agent_request_latency_seconds" in body


def test_live_server_returns_404_for_unknown_routes(live_metrics_server):
    with pytest.raises(HTTPError) as error:
        urlopen(f"{live_metrics_server}/unknown")

    assert error.value.code == 404
    assert json.loads(error.value.read()) == {"error": "Not found"}


def test_tool_failure_is_recorded():
    with pytest.raises(RuntimeError, match="Tool failed to complete request"):
        execute_tool("retrieval", "fail this request")

    tool_failures = metrics.TOOL_FAILURES.labels(tool_name="retrieval", status="error")._value.get()
    assert tool_failures >= 1


def test_exception_is_recorded_without_sensitive_data():
    tracer = configure_tracing()
    with tracer.start_as_current_span("test.exception") as span:
        exc = ValueError("Authorization: Bearer super-secret-token")
        record_exception(span, exc)
        assert span.is_recording() is True
        assert span.status.status_code.name == "ERROR"
        assert "super-secret-token" not in span.attributes.get("error.message", "")


def test_metrics_are_updated_for_successful_and_failed_requests():
    before_success = metrics.REQUEST_COUNT.labels(status="success")._value.get()
    before_error = metrics.REQUEST_COUNT.labels(status="error")._value.get()

    run_agent("What is observability?")
    run_agent("   ")

    after_success = metrics.REQUEST_COUNT.labels(status="success")._value.get()
    after_error = metrics.REQUEST_COUNT.labels(status="error")._value.get()

    assert after_success >= before_success + 1
    assert after_error >= before_error + 1

    histogram_samples = metrics.REQUEST_LATENCY.collect()[0].samples
    assert any(sample.name.endswith("_count") for sample in histogram_samples)


def test_latency_measurement_is_non_negative():
    results = [run_agent(f"Question {i}") for i in range(3)]
    for result in results:
        assert result["latency_seconds"] >= 0
        assert result["latency_seconds"] < 10


def test_tracer_provider_configuration_is_initialized_once():
    provider = trace.get_tracer_provider()
    tracer_a = configure_tracing()
    tracer_b = configure_tracing()
    assert tracer_a is tracer_b
    assert provider is not None
