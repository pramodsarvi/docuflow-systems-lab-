"""Agent traceability: OpenTelemetry spans in the OpenInference shape that LiteLLM Lens (and Phoenix, Langfuse, any
OTLP backend) understands.

One query = one trace:
  docuflow.agent            openinference.span.kind=AGENT   gen_ai.agent.name, input.value (question), output.value (answer),
  │                                                           session.id (query id), docuflow.citations, docuflow.during_ingest
  ├─ llm.step.N             openinference.span.kind=LLM     llm.model_name, input.value (last message), output.value (raw action),
  │                                                           llm.token_count.completion, docuflow.action
  └─ tool.<name>            openinference.span.kind=TOOL    tool.name, input.value (query / doc id), output.value (observation)

Enabled when OTEL_EXPORTER_OTLP_TRACES_ENDPOINT is set (Lens: http://lens.<ns>:4318/v1/traces). LENS_TRACING_KEY (or
OTEL_EXPORTER_OTLP_TRACES_HEADERS) authenticates the exporter. Spans are exported in the background (BatchSpanProcessor),
so tracing adds no latency to queries; with no endpoint every call is a cheap no-op.
"""
import contextlib
import json
import os

_tracer = None
_enabled = None
MAX_ATTR = 8000          # keep span attributes bounded; observations can be long


def enabled():
    global _enabled
    if _enabled is None:
        _enabled = bool(os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"))
    return _enabled


def tracer():
    global _tracer
    if _tracer is not None or not enabled():
        return _tracer
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    headers = {}
    if os.environ.get("LENS_TRACING_KEY"):
        headers["Authorization"] = f"Bearer {os.environ['LENS_TRACING_KEY']}"
    provider = TracerProvider(resource=Resource.create({
        "service.name": os.environ.get("OTEL_SERVICE_NAME", "docuflow"),
        "service.version": os.environ.get("DOCUFLOW_VERSION", "dev"),
        "deployment.environment": os.environ.get("DOCUFLOW_ENV", "local")}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(
        endpoint=os.environ["OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"], headers=headers or None, timeout=10)))
    trace.set_tracer_provider(provider)
    _tracer = trace.get_tracer("docuflow.agent")
    return _tracer


def _val(v):
    if isinstance(v, (dict, list)):
        v = json.dumps(v, ensure_ascii=False, default=str)
    v = "" if v is None else str(v)
    return v[:MAX_ATTR]


class _NoSpan:
    def set(self, **_):
        return self

    def trace_id(self):
        return None


class _Span:
    def __init__(self, span):
        self.span = span

    def set(self, **attrs):
        for k, v in attrs.items():
            if v is None:
                continue
            key = k.replace("__", ".")
            self.span.set_attribute(key, v if isinstance(v, (bool, int, float)) else _val(v))
        return self

    def trace_id(self):
        return format(self.span.get_span_context().trace_id, "032x")


@contextlib.contextmanager
def span(name, kind, **attrs):
    """Context manager yielding an object with .set(**attrs); attribute names use __ for dots (input__value)."""
    t = tracer()
    if t is None:
        yield _NoSpan()
        return
    from opentelemetry.trace import Status, StatusCode
    with t.start_as_current_span(name) as s:
        w = _Span(s).set(openinference__span__kind=kind, **attrs)
        try:
            yield w
        except Exception as e:
            s.record_exception(e)
            s.set_status(Status(StatusCode.ERROR, str(e)[:200]))
            raise
