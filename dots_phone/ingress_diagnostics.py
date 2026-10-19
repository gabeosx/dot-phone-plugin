"""Bounded, content-free observations of the media gateway's two socket edges."""

from __future__ import annotations

import json
import logging
import time

# Use the gateway server's existing configured handler/level, without changing
# global logging configuration or requiring deployment configuration changes.
logger = logging.getLogger("uvicorn.error.dots_phone.ingress")
STANDARD_CLOSE_CODES = frozenset(
    {1000, 1001, 1002, 1003, 1005, 1006, 1007, 1008, 1009, 1010, 1011, 1012, 1013, 1014, 1015}
)


def close_code(value):
    # Application-defined codes and arbitrary exception attributes are not
    # allowed to become an unbounded or content-bearing logging field.
    return (
        int(value)
        if isinstance(value, int) and not isinstance(value, bool) and value in STANDARD_CLOSE_CODES
        else None
    )


class IngressDiagnostics:
    def __init__(self, *, clock=time.monotonic):
        self.clock = clock
        self.started = clock()
        self.ends = {}
        self.forwarded = {"incoming": [0, 0], "outgoing": [0, 0]}
        self.first_observed_boundary = "unknown"
        self.completion_observation = "not_observed"
        self.cleanup_outcome = "not_attempted"
        self.cleanup_code = None

    def elapsed_ms(self):
        return min(86400000, max(0, round((self.clock() - self.started) * 1000, 3)))

    def forwarded_message(self, direction, size):
        counts = self.forwarded[direction]
        counts[0] = min(1000000000, counts[0] + 1)
        counts[1] = min(1000000000, counts[1] + size)

    def end(self, direction, boundary, outcome, code=None):
        if direction not in self.ends:
            self.ends[direction] = {
                "boundary": boundary,
                "outcome": outcome,
                "close_code": close_code(code),
                "at_ms": self.elapsed_ms(),
            }

    def completed(self, directions):
        if len(directions) > 1:
            self.first_observed_boundary = "ambiguous"
            self.completion_observation = "both_directions_ready"
        elif directions:
            self.first_observed_boundary = self.ends[directions[0]]["boundary"]
            self.completion_observation = "one_direction_ready"

    def setup_failed(self):
        if self.completion_observation == "not_observed":
            self.first_observed_boundary = "upstream"
            self.completion_observation = "upstream_setup_failed"

    def cleanup(self, outcome, code=None):
        self.cleanup_outcome, self.cleanup_code = outcome, close_code(code)

    def report(self):
        return {
            "event": "media_gateway_teardown",
            "schema_version": 1,
            "first_observed_boundary": self.first_observed_boundary,
            "completion_observation": self.completion_observation,
            "inferred_initiator": False,
            "elapsed_ms": self.elapsed_ms(),
            "incoming": self.ends.get("incoming"),
            "outgoing": self.ends.get("outgoing"),
            "forwarded_to_upstream_messages": self.forwarded["incoming"][0],
            "forwarded_to_upstream_bytes": self.forwarded["incoming"][1],
            "forwarded_to_carrier_messages": self.forwarded["outgoing"][0],
            "forwarded_to_carrier_bytes": self.forwarded["outgoing"][1],
            "cleanup_close_outcome": self.cleanup_outcome,
            "cleanup_close_code": self.cleanup_code,
        }

    def log(self):
        # No exception text, close reasons, headers, identifiers, URLs or raw
        # message values enter the record. It is emitted once by route finally.
        logger.info("%s", json.dumps(self.report(), separators=(",", ":")))
