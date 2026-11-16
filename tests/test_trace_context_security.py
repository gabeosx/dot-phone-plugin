"""Bound trace-context work on untrusted metadata before regex matching."""

from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.trace.propagation import tracecontext


def test_oversized_traceparent_is_rejected_before_regex_matching(monkeypatch):
    parent = "01-" + "1" * 32 + "-" + "2" * 16 + "-01"
    value = parent + "-" + " " * 4096 + "\n"
    context = Context()

    def refuse_match(*args, **kwargs):
        raise AssertionError("Oversized untrusted traceparent reached regex matching")

    monkeypatch.setattr(tracecontext.re, "search", refuse_match)
    result = tracecontext.TraceContextTextMapPropagator().extract(
        {"traceparent": value}, context=context
    )
    assert result is context


def test_valid_traceparent_still_extracts_a_remote_context():
    value = "00-" + "1" * 32 + "-" + "2" * 16 + "-01"
    context = tracecontext.TraceContextTextMapPropagator().extract({"traceparent": value})
    span = trace.get_current_span(context).get_span_context()
    assert span.is_valid and span.is_remote
    assert span.trace_id == int("1" * 32, 16)
    assert span.span_id == int("2" * 16, 16)
