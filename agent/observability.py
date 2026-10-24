
import os
import re

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter, SimpleSpanProcessor
from opentelemetry.trace import ProxyTracerProvider, Status, StatusCode

_TRACER = None


def _sanitize_error_message(message: str) -> str:
    """Strip potentially sensitive details from exceptions while preserving context."""
    if not message:
        return "unknown_error"
    cleaned = message.strip()
    cleaned = re.sub(r"(?i)Bearer\s+[A-Za-z0-9._-]+", "Bearer <redacted>", cleaned)
    cleaned = re.sub(r"(?i)(authorization|api[_-]?key|token|password|secret)\s*[:=]\s*[^\s,;]+", r"\1=<redacted>", cleaned)
    cleaned = re.sub(r"(?i)(authorization|api[_-]?key|token|password|secret)\s*[:=]\s*Bearer\s+[A-Za-z0-9._-]+", r"\1=<redacted>", cleaned)
    cleaned = re.sub(r"(?i)\b(?:super|secret|access|refresh)[-_]?[A-Za-z0-9]{4,}\b", "<redacted>", cleaned)
    return cleaned[:200]


def configure_tracing() -> trace.Tracer:
    """Create a local OpenTelemetry tracer once and avoid duplicate initialization."""
    global _TRACER
    if _TRACER is not None:
        return _TRACER

    provider = trace.get_tracer_provider()
    if isinstance(provider, ProxyTracerProvider):
        service_name = os.getenv("OTEL_SERVICE_NAME", "agent-observability-system")
        resource = Resource.create({
            "service.name": service_name,
            "service.version": os.getenv("APP_VERSION", "0.1.0"),
        })
        provider = TracerProvider(resource=resource)

        if os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"):
            exporter = OTLPSpanExporter(endpoint=os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"))
            provider.add_span_processor(BatchSpanProcessor(exporter))
        else:
            provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))

        trace.set_tracer_provider(provider)

    _TRACER = provider.get_tracer("agent-observability")
    return _TRACER


def get_tracer():
    return configure_tracing()


def record_exception(span, exc: Exception) -> None:
    """Record exceptions with sanitized details, without exposing secrets."""
    if span is None:
        return
    safe_message = _sanitize_error_message(str(exc))
    span.record_exception(exc)
    span.set_attribute("error.type", type(exc).__name__)
    span.set_attribute("error.message", safe_message)
    span.set_status(Status(StatusCode.ERROR, safe_message))
