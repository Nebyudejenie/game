"""OpenTelemetry tracing (spec section 10.4: "deposit and payout paths
end to end").

Opt-in, like every other optional integration in this codebase
(PUSHGATEWAY_URL, ADMIN_IP_ALLOWLIST, ...): configure_tracing() with an
empty endpoint leaves OpenTelemetry's own no-op default tracer in place,
so every get_tracer()/start_as_current_span() call in
services/payments/deposits.py and withdrawals.py is always safe to make
regardless of whether a real collector is configured -- there is no "is
tracing on" branch scattered through business logic, the same reasoning
packages/core/metrics.py's Counters/Gauges are always safe to touch
whether or not anything ever scrapes them.
"""

from __future__ import annotations

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Tracer


def configure_tracing(service_name: str, endpoint: str) -> None:
    if not endpoint:
        return
    provider = TracerProvider(resource=Resource.create({SERVICE_NAME: service_name}))
    # endpoint is a base URL (e.g. "http://localhost:4318", matching
    # OTEL_EXPORTER_OTLP_ENDPOINT convention) -- the http exporter only
    # auto-appends "/v1/traces" when it derives the endpoint from that env
    # var itself, not when a caller passes `endpoint=` explicitly, so the
    # full traces path is built here.
    exporter = OTLPSpanExporter(endpoint=f"{endpoint.rstrip('/')}/v1/traces")
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)


def get_tracer(name: str) -> Tracer:
    return trace.get_tracer(name)
