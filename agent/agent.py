
import os
import sys
import threading
import time

from agent.metrics import (
    metrics_port,
    record_agent_execution,
    record_model_usage,
    record_request,
    record_tool_failure,
    start_metrics_server,
)
from agent.observability import get_tracer, record_exception

tracer = get_tracer()


def execute_tool(tool_name: str, question: str) -> dict:
    """Run a deterministic local tool and emit tool-related span attributes."""
    with tracer.start_as_current_span("agent.execute_tool") as span:
        span.set_attribute("agent.tool.name", tool_name)
        status = "success"
        details = {"tool_name": tool_name, "status": "success"}

        if tool_name == "retrieval":
            if not question or not question.strip():
                status = "error"
                details["status"] = "error"
                details["error"] = "Question cannot be empty"
                span.set_attribute("agent.tool.status", "error")
                raise ValueError("Question cannot be empty")
            lowered = question.lower()
            if any(keyword in lowered for keyword in ("bypass", "exploit", "drop table", "secret")):
                status = "guardrail_blocked"
                details["status"] = "guardrail_blocked"
                details["error"] = "Request rejected by guardrail policy"
                span.set_attribute("agent.tool.status", "guardrail_blocked")
                raise PermissionError("Request rejected by guardrail policy")
            if "fail" in lowered:
                status = "error"
                details["status"] = "error"
                details["error"] = "Tool failed to complete request"
                span.set_attribute("agent.tool.status", "error")
                record_tool_failure(tool_name, status)
                raise RuntimeError("Tool failed to complete request")
            context = "Local mock retrieval context for observability and incident-response workflows."
            details["content_length"] = len(context)
            span.set_attribute("agent.tool.output_size", len(context))
            return {"ok": True, "status": status, "context": context}

        span.set_attribute("agent.tool.status", status)
        return {"ok": True, "status": status}


def _mock_model_call(question: str) -> str:
    """Use a deterministic local model response that does not require paid APIs."""
    prompt = question.strip()
    normalized = prompt.lower()
    if "observability" in normalized:
        return "Observability is the practice of collecting telemetry, correlating signals, and acting on health data to reduce incident response time."
    if "incident" in normalized:
        return "An incident is a service disruption or degradation that requires coordinated detection, mitigation, and postmortem review."
    answer = f"Agent response to: {prompt}"
    return answer


def get_health_status() -> dict:
    """Return basic runtime health information for local checks and tests."""
    return {
        "status": "ok",
        "service": "agent-observability-system",
        "metrics_port": metrics_port(),
        "tracing": "enabled",
    }


def run_agent(question: str) -> dict:
    """Execute a deterministic agent workflow with tracing and Prometheus instrumentation."""
    status = "success"
    start_time = time.perf_counter()

    with tracer.start_as_current_span("agent.request") as span:
        span.set_attribute("agent.input_length", len(question or ""))
        span.set_attribute("agent.service", "agent-observability-system")

        try:
            with tracer.start_as_current_span("agent.validate_input") as validation_span:
                if not question or not str(question).strip():
                    raise ValueError("Question cannot be empty")
                sanitized_question = str(question).strip()
                validation_span.set_attribute("agent.input_clean", sanitized_question)
                validation_span.set_attribute("agent.input_length", len(sanitized_question))
            with tracer.start_as_current_span("agent.execute_tool") as tool_span:
                tool_result = execute_tool("retrieval", str(question).strip())
                tool_span.set_attribute("agent.tool.ok", tool_result.get("ok", False))
                tool_span.set_attribute("agent.tool.status", tool_result.get("status", "unknown"))

            with tracer.start_as_current_span("agent.model_call") as model_span:
                answer = _mock_model_call(str(question).strip())
                model_span.set_attribute("agent.model", "local-deterministic")
                model_span.set_attribute("agent.output_length", len(answer))
                model_span.set_attribute("agent.token_input_estimate", max(1, len(str(question).strip().split())))
                model_span.set_attribute("agent.token_output_estimate", max(1, len(answer.split())))
                record_model_usage(
                    model="local-deterministic",
                    input_tokens=max(1, len(str(question).strip().split())),
                    output_tokens=max(1, len(answer.split())),
                    estimated_cost_usd=0.0,
                )

            span.set_attribute("agent.status", "success")
            latency_seconds = time.perf_counter() - start_time
            response = {
                "status": "success",
                "answer": answer,
                "error": None,
                "latency_seconds": round(latency_seconds, 4),
            }
            return response
        except Exception as exc:
            status = "error"
            latency_seconds = time.perf_counter() - start_time
            span.set_attribute("agent.status", "error")
            span.set_attribute("error.type", type(exc).__name__)
            span.set_attribute("error.message", str(exc)[:200])
            record_exception(span, exc)
            if isinstance(exc, PermissionError):
                error_message = "Request rejected by guardrail policy"
            else:
                error_message = str(exc) if str(exc) else "Agent execution failed"
            response = {
                "status": "error",
                "answer": None,
                "error": error_message,
                "latency_seconds": round(latency_seconds, 4),
            }
            return response
        finally:
            final_latency = time.perf_counter() - start_time
            record_request(status, final_latency, estimated_cost_usd=0.0)
            record_agent_execution(final_latency, status)


if __name__ == "__main__":
    start_metrics_server(port=int(os.getenv("METRICS_PORT", "8000")))
    result = run_agent("fail this request")
    print("\nAgent result:")
    print(result)
    print("\nMetrics available at http://localhost:%s/metrics" % os.getenv("METRICS_PORT", "8000"))
    print("\nHealth: %s" % get_health_status())
    if sys.stdin.isatty():
        try:
            input("Press Enter to stop the metrics server...")
        except EOFError:
            print("\nInteractive input is unavailable; keeping the server alive.")
            threading.Event().wait()
    else:
        print("\nAgent is running without an interactive terminal. Use stop_project.ps1 to stop it.")
        threading.Event().wait()
