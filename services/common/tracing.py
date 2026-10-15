"""Cloud Trace（OpenTelemetry）。推論 → ツール → Gateway → デバイスの応答を1本のトレースにする。

- Agent: ADK が推論とツール実行のスパンを出す。ツールはそのスパンから traceparent を作って exec に載せる
- Gateway: exec の traceparent を親にして「デバイスとの往復」のスパンを作り、デバイスにはそのスパンを親として渡す
- デバイス: traceparent をそのまま監査ログと result に残す（スパンは出さない。Pi からは送らない）
"""
from __future__ import annotations

import contextlib
import os


def setup_tracing() -> None:
    from opentelemetry import trace
    from opentelemetry.exporter.cloud_trace import CloudTraceSpanExporter
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    provider = TracerProvider()
    provider.add_span_processor(BatchSpanProcessor(CloudTraceSpanExporter(project_id=os.environ.get("GOOGLE_CLOUD_PROJECT"))))
    trace.set_tracer_provider(provider)


def traceparent_of_current() -> str | None:
    from opentelemetry import trace

    ctx = trace.get_current_span().get_span_context()
    if not ctx.is_valid:
        return None
    return f"00-{ctx.trace_id:032x}-{ctx.span_id:016x}-{int(ctx.trace_flags):02x}"


@contextlib.contextmanager
def child_span(name: str, traceparent: str | None, attributes: dict):
    """traceparent を親にしたスパンを開き、(span, そのスパンの traceparent) を返す。OTel が未設定なら何もしない。"""
    from opentelemetry import trace
    from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

    ctx = TraceContextTextMapPropagator().extract({"traceparent": traceparent}) if traceparent else None
    tracer = trace.get_tracer("mugemuge")
    with tracer.start_as_current_span(name, context=ctx, kind=trace.SpanKind.CLIENT,
                                      attributes={k: v for k, v in attributes.items() if v is not None}) as span:
        yield span, traceparent_of_current() or traceparent
