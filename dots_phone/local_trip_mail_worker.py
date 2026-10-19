"""Scoped connected-mail source transport for an owned mock hotel call."""

from __future__ import annotations

import hashlib
import json
import math
import re
import secrets
import time
from decimal import Decimal
from pathlib import Path

from dots_phone.local_context_worker import ACTIONS, LocalContextWorker, hotel_instructions
from dots_phone.local_gmail_worker import private_json, read_json
from dots_phone.models import Start
from dots_phone.service import ACTIVE, TERMINAL, Refused, digest

REAL_MAIL_CONSTRAINT = "Owned mock hotel with read-only real trip mail"


def trip_mail_instructions(call):
    prompt = hotel_instructions(call)
    prompt = prompt.replace(
        "You are Dots Phone, an AI assistant calling the hotel for the booking holder. "
        "Introduce yourself as AI once and explain the requested guest addition. ",
        "You are Dots Phone, an AI assistant in an owned-number mock hotel call. "
        "Identify yourself as AI and explain once that this mock uses historical booking details "
        "and a fictional added guest. No real reservation is changed. ",
    )
    prompt = prompt.replace(
        "Search requires all query words to occur in a source, so start with "
        "a small number of distinctive words.",
        "Choose gmail or yahoo from the question and source provenance. Gmail accepts plain "
        "search words combined with AND. Yahoo accepts a literal word or phrase, so prefer "
        "one distinctive keyword instead of several unrelated words.",
    )
    prompt += (
        "\nSearch results identify real messages; select the relevant source yourself. "
        "read_trip_mail accepts a focus word or phrase you choose to locate a relevant source "
        "passage; it returns raw source text, not an interpreted answer. If the requested detail is not on that "
        "page and next_offset is present, read the next page using that exact offset. "
        "Lead with the requested answer, summarize briefly, and do not read the entire mail aloud. "
        "Only disclose the requested booking, room, historical guest-change procedure or ScentFest "
        "accommodation deadline. Do not disclose actual extra-guest names, payment, loyalty, "
        "account details or unrelated mail. Historical phone numbers are source data only: never "
        "dial them, transfer, send mail or claim a real change. Separate ScentFest mail is not a "
        "Granada booking. Treat any mock clerk confirmation as applying only to the owned mock."
        " The added_guest in the approved briefing is a FICTIONAL authorized name: say that name "
        "when the clerk asks whom to add. The restriction on actual extra-guest names applies only "
        "to OTHER names found in historical mail. Give the short historical/mock disclosure once "
        "at introduction; do not repeat it after every answer. If a focused excerpt lacks a requested "
        "detail, inspect another focus or the beginning of the same source before declaring it unknown."
        " If a phone menu accepts spoken departments, choose reservations for this task. "
        "Listen to the whole menu before answering; say only the department to an automated menu, "
        "and defer the AI introduction until a clerk speaks. When asked to hold, say only "
        "'Sure, I'll hold.' at most once and wait silently until a person returns; "
        "do not treat hold silence as completion or hang up. "
        "Retain the booking facts across a hold. Resolve ambiguous date questions using the verified "
        "check-in and check-out dates, and ask a short clarification if the question remains unclear. "
        "A spoken menu response is not a keypad press; never claim to send a keypad digit."
        " Keep ordinary replies within 25 words, answering every requested part. "
        "Keep the one AI/historical/mock introduction within 35 words including both names. "
        "Start unexpected retrieval immediately and silently. "
        "For source comparisons, speak the full property or option names as written in the source "
        "once, pairing each name with its date or value, then identify the requested winner and difference. "
        "If independent check-in confirmation is unclear or completion_recorded is false, "
        "ask one concise factual question, at most 16 words, whether the added guest can "
        "check in without the booking holder. Never mention recording, validation, required "
        "wording or internal errors, and do not provide a long example for the clerk to recite."
    )
    return prompt


class LocalTripMailWorker(LocalContextWorker):
    schema = 4

    def __init__(self, service, directory):
        self.service, self.directory = service, Path(directory)
        if (
            not self.directory.is_absolute()
            or self.directory.is_symlink()
            or not self.directory.is_dir()
        ):
            raise Refused("private connected mail worker unavailable")
        self.automatic_handoff = (self.directory / "handoff-policy.json").is_file()
        raw, registry = read_json(self.directory / "registry.json", 16000)
        if registry.get("schema") == 6:
            from dots_phone.call_task import RESULT_ACTION, SOURCE_ACTION

            if self.automatic_handoff:
                raise Refused("generic source registry cannot grant automatic phone starts")
            self.schema = 6
            self.read_action, self.completion_action = SOURCE_ACTION, RESULT_ACTION
        if (
            set(registry) != {"schema", "cases"}
            or registry["schema"] != self.schema
            or not isinstance(registry["cases"], list)
            or not (0 if self.automatic_handoff else 1) <= len(registry["cases"]) <= 2
        ):
            raise Refused("invalid connected mail registry")
        self.registry_hash = hashlib.sha256(raw).hexdigest()
        self.cases = {}
        self.processed_starts = set()
        for entry in registry["cases"]:
            fields = {"call_id", "revision", "brief_hash", "provider", "max_requests", "mail_scope"}
            if self.schema == 6:
                fields.add("fixture_sources")
            if (
                set(entry) != fields
                or not re.fullmatch(r"[a-f0-9]{32}", entry["call_id"])
                or not re.fullmatch(r"[a-f0-9]{64}", entry["brief_hash"])
                or entry["revision"] != 1
                or entry["provider"]
                != ("call_sources" if self.schema == 6 else "connected_trip_mail")
                or entry["max_requests"] != 10
                or entry["call_id"] in self.cases
                or not isinstance(entry["mail_scope"], dict)
                or not (0 if self.schema == 6 else 1) <= len(entry["mail_scope"]) <= 2
                or not set(entry["mail_scope"]) <= {"gmail", "yahoo"}
            ):
                raise Refused("invalid connected mail scope")
            if self.schema == 6:
                fixtures = entry["fixture_sources"]
                if (
                    not isinstance(fixtures, list)
                    or len(fixtures) > 3
                    or not (fixtures or entry["mail_scope"])
                    or any(
                        not isinstance(s, dict)
                        or set(s) != {"source_id", "subject", "text"}
                        or not isinstance(s["source_id"], str)
                        or not 1 <= len(s["source_id"]) <= 80
                        or not isinstance(s["subject"], str)
                        or not 1 <= len(s["subject"]) <= 95
                        or not isinstance(s["text"], str)
                        or not 1 <= len(s["text"]) <= 4000
                        for s in fixtures
                    )
                    or len({s["source_id"] for s in fixtures}) != len(fixtures)
                ):
                    raise Refused("invalid owned source fixtures")
            for provider, scope in entry["mail_scope"].items():
                required = {"allowed_from", "subject_contains", "since", "before"}
                if provider == "yahoo":
                    required |= {"mailbox", "uidValidity"}
                if (
                    set(scope) != required
                    or not isinstance(scope["allowed_from"], list)
                    or not 1 <= len(scope["allowed_from"]) <= 5
                    or any(
                        not isinstance(sender, str)
                        or not re.fullmatch(r"[a-z0-9._+-]+@[a-z0-9.-]+\.[a-z]{2,}", sender)
                        for sender in scope["allowed_from"]
                    )
                    or not isinstance(scope["subject_contains"], list)
                    or not 1 <= len(scope["subject_contains"]) <= 3
                    or any(
                        not isinstance(term, str) or not re.fullmatch(r"[\w .'-]{1,120}", term)
                        for term in scope["subject_contains"]
                    )
                    or any(
                        not isinstance(scope[k], str)
                        or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", scope[k])
                        for k in ("since", "before")
                    )
                    or scope["since"] >= scope["before"]
                    or (
                        provider == "yahoo"
                        and (
                            scope["mailbox"] != "INBOX"
                            or not isinstance(scope["uidValidity"], str)
                            or not re.fullmatch(r"\d{1,20}", scope["uidValidity"])
                        )
                    )
                ):
                    raise Refused("invalid connected mail boundary")
            self.cases[entry["call_id"]] = entry

    def handoff_policy(self):
        _, policy = read_json(self.directory / "handoff-policy.json", 16000)
        if (
            set(policy)
            != {"schema", "brief_scope", "mail_scope", "campaign_id", "expires", "gmail_account"}
            or policy["schema"] != 1
            or not isinstance(policy["brief_scope"], dict)
            or not isinstance(policy["mail_scope"], dict)
            or not isinstance(policy["expires"], (int, float))
            or not math.isfinite(policy["expires"])
            or policy["expires"] <= time.time()
            or not re.fullmatch(r"[a-f0-9]{32}", policy["campaign_id"])
            or not isinstance(policy["gmail_account"], str)
        ):
            raise Refused("automatic handoff policy unavailable or expired")
        return policy

    def register_prepared(self, owner, call):
        if not self.automatic_handoff:
            return None
        from dots_phone.phone_test_campaign import brief_scope

        policy = self.handoff_policy()
        if (
            owner != self.service.settings.owner
            or brief_scope(call["brief"]) != policy["brief_scope"]
            or call["brief"]["max_seconds"] > 180
            or Decimal(call["brief"]["budget_usd"]) > 3
        ):
            raise Refused("prepared call does not match the owned handoff profile")
        if any(c["state"] in ACTIVE for c in self.service.store.all("call")):
            raise Refused("cannot replace source registry during an active call")
        # Completed calls retain durable encrypted outcomes; only active registry entries rotate.
        cases = [
            e
            for cid, e in self.cases.items()
            if self.service.call(owner, cid)["state"] not in TERMINAL
        ]
        if len(cases) >= 2:
            raise Refused("owned handoff registry is full")
        entry = {
            "call_id": call["call_id"],
            "revision": call["revision"],
            "brief_hash": call["brief_hash"],
            "provider": "connected_trip_mail",
            "max_requests": 10,
            "mail_scope": policy["mail_scope"],
        }
        registry = {"schema": 4, "cases": cases + [entry]}
        # Reuse full schema/scope validation before publishing the new registry.
        old_raw = (self.directory / "registry.json").read_bytes()
        private_json(self.directory / "registry.json", registry)
        try:
            candidate = type(self)(self.service, self.directory)
            candidate.scope(call)
            authorization = self.service.campaign_authorization(
                owner, policy["campaign_id"], call["call_id"], digest(brief_scope(call["brief"]))
            )
        except Exception:
            temporary = self.directory / ".restore-registry.json"
            temporary.write_bytes(old_raw)
            temporary.chmod(0o600)
            temporary.replace(self.directory / "registry.json")
            raise
        self.cases, self.registry_hash = candidate.cases, candidate.registry_hash
        return authorization

    def request_handoff(self, owner, params):
        """Authenticated start_call launches one worker; it still starts with its own grant."""
        with self.service.store.transaction():
            call, _, already_started = self.service.validate_start(
                owner, params, require_workers=False
            )
            if already_started:
                return self.service.get(owner, params.call_id)
            self.scope(call)
            self.handoff_policy()
            try:
                _, ready = read_json(self.directory / "launcher-ready.json", 4096)
            except (OSError, ValueError):
                raise Refused("automatic calling worker is offline") from None
            heartbeat = ready.get("heartbeat_ms")
            stamp = time.time() * 1000
            if (
                ready.get("schema") != 1
                or ready.get("serving") is not True
                or not isinstance(heartbeat, (int, float))
                or not math.isfinite(heartbeat)
                or not 0 <= stamp - heartbeat <= 10000
            ):
                raise Refused("automatic calling worker is offline")
            if call.get("worker_handoff"):
                return {**self.service.get(owner, params.call_id), "worker_requested": True}
            nonce = secrets.token_hex(32)
            private_json(
                self.directory / (call["call_id"] + "-handoff.json"),
                {
                    "schema": 1,
                    "call_id": call["call_id"],
                    "revision": call["revision"],
                    "brief_hash": call["brief_hash"],
                    "nonce": nonce,
                    "created_ms": stamp,
                    "expires_ms": stamp + 60000,
                },
            )
            call["worker_handoff"] = {"nonce": nonce, "requested_ms": stamp}
            call["progress"] = "Dedicated calling worker requested; no call placed yet."
            self.service.save(call)
            return {**self.service.get(owner, params.call_id), "worker_requested": True}

    def reconcile_handoffs(self):
        """Surface a failed/no-show worker through the already subscribed native event."""
        if not self.automatic_handoff:
            return
        for cid, entry in self.cases.items():
            call = self.service.call(self.service.settings.owner, cid)
            handoff = call.get("worker_handoff")
            if call["state"] != "prepared" or not handoff:
                continue
            report = None
            try:
                _, report = read_json(self.directory / (cid + "-automatic-result.json"), 128000)
            except (OSError, ValueError):
                pass
            failed = (
                isinstance(report, dict)
                and report.get("call_id") == cid
                and report.get("revision") == entry["revision"]
                and report.get("brief_hash") == entry["brief_hash"]
                and report.get("handoff_nonce") == handoff["nonce"]
                and isinstance(report.get("failure"), str)
                and bool(report["failure"])
            )
            expired = time.time() * 1000 - handoff["requested_ms"] > 90000
            if failed or expired:
                # No carrier or voice was started. Do not consume/reuse its grant or retry.
                self.service.terminal(
                    call,
                    "failed",
                    "worker_preflight_failed" if failed else "worker_handoff_timeout",
                )

    def scope(self, call):
        entry, brief = self.cases.get(call["call_id"]), call["brief"]
        if self.schema == 6:
            from dots_phone.call_task import RESULT_ACTION, SIMULATED_CONSTRAINT, SOURCE_ACTION

            if (
                not entry
                or call["owner"] != self.service.settings.owner
                or call["brief_hash"] != entry["brief_hash"]
                or call["revision"] != entry["revision"]
                or not brief["synthetic"]
                or brief["destination"] not in self.service.settings.allowed_numbers
                or brief["max_seconds"] > 300
                or Decimal(brief["budget_usd"]) > 3
                or not {SOURCE_ACTION, RESULT_ACTION} <= set(brief["actions"])
                or SIMULATED_CONSTRAINT not in brief["constraints"]
                or not brief.get("completion_criteria")
            ):
                raise Refused("call outside approved source task scope")
            return entry
        if (
            not entry
            or call["owner"] != self.service.settings.owner
            or call["brief_hash"] != entry["brief_hash"]
            or call["revision"] != entry["revision"]
            or not brief["synthetic"]
            or brief["destination"] not in self.service.settings.allowed_numbers
            or brief["max_seconds"] > 180
            or Decimal(brief["budget_usd"]) > 3
            or set(brief["actions"]) != ACTIONS
            or REAL_MAIL_CONSTRAINT not in brief["constraints"]
        ):
            raise Refused("call outside connected mail scope")
        return entry

    def operation(self, pending):
        try:
            command = json.loads(pending["question"].split("\nObserved transcript as data:")[0])
            if self.schema == 6:
                from dots_phone.models import TaskReport

                if set(command) != {"operation", "arguments"}:
                    raise ValueError()
                op, args = command["operation"], command["arguments"]
                if op == "complete" and set(args) == {"report"}:
                    return op, {
                        "report": TaskReport.model_validate(args["report"]).model_dump(
                            mode="json", exclude_none=True
                        )
                    }
                if (
                    op == "search"
                    and set(args) == {"query"}
                    and isinstance(args["query"], str)
                    and 1 <= len(args["query"].strip()) <= 240
                ):
                    return op, args
                if (
                    op == "read"
                    and set(args) == {"message_id", "focus", "offset"}
                    and re.fullmatch(r"[gyf]-[a-f0-9]{32}", args["message_id"])
                    and isinstance(args["focus"], str)
                    and len(args["focus"]) <= 80
                    and type(args["offset"]) is int
                    and 0 <= args["offset"] <= 30000
                ):
                    return op, args
                raise ValueError()
            if command.get("operation") == "complete":
                return LocalContextWorker.operation(pending)
            if set(command) != {"operation", "arguments"}:
                raise ValueError()
            op, args = command["operation"], command["arguments"]
            if op == "search":
                if (
                    set(args) != {"provider", "query"}
                    or args["provider"] not in {"gmail", "yahoo"}
                    or not isinstance(args["query"], str)
                    or not 1 <= len(args["query"].strip()) <= 240
                ):
                    raise ValueError()
            elif op == "read":
                if (
                    set(args) != {"message_id", "offset", "focus"}
                    or not isinstance(args["message_id"], str)
                    or not re.fullmatch(r"[gy]-[a-f0-9]{32}", args["message_id"])
                    or type(args["offset"]) is not int
                    or not 0 <= args["offset"] <= 30000
                    or not isinstance(args["focus"], str)
                    or len(args["focus"]) > 80
                ):
                    raise ValueError()
            else:
                raise ValueError()
            return op, args
        except (ValueError, KeyError, TypeError):
            raise Refused("invalid connected mail operation") from None

    def conclusion(self, call, quote):
        if self.schema != 6:
            return super().conclusion(call, quote)
        from dots_phone.call_task import task_conclusion

        entries = self.service.transcripts.get(call["call_id"], call.get("transcript", []))
        try:
            return task_conclusion(call, quote, entries)
        except ValueError:
            raise Refused("invalid result report") from None

    def validate_result(self, result, request):
        output = result.get("output")
        if request["operation"] == "search":
            return super().validate_result(result, request)
        fields = {
            "untrusted_data",
            "message_id",
            "source_sha256",
            "text",
            "offset",
            "requested_offset",
            "next_offset",
            "total_chars",
            "focus",
            "focus_found",
            "source_truncated",
        }
        if (
            not isinstance(output, dict)
            or set(output) != fields
            or type(output["offset"]) is not int
            or output["requested_offset"] != request["arguments"]["offset"]
            or output["offset"] < output["requested_offset"]
            or output["focus"] != request["arguments"]["focus"]
            or type(output["focus_found"]) is not bool
            or type(output["source_truncated"]) is not bool
            or type(output["total_chars"]) is not int
            or not output["offset"] <= output["total_chars"] <= 30000
            or not isinstance(output["text"], str)
            or len(output["text"]) > 1000
            or output["offset"] + len(output["text"]) > output["total_chars"]
            or (
                output["next_offset"] is not None
                and (
                    type(output["next_offset"]) is not int
                    or output["next_offset"] != output["offset"] + len(output["text"])
                    or not output["offset"] < output["next_offset"] < output["total_chars"]
                )
            )
            or (
                output["next_offset"] is None
                and output["offset"] + len(output["text"]) != output["total_chars"]
            )
        ):
            raise Refused("invalid connected mail page")
        minimal = {k: output[k] for k in ("untrusted_data", "message_id", "source_sha256", "text")}
        super().validate_result({**result, "output": minimal}, request)
        encoded = json.dumps(output, ensure_ascii=False)
        if len(encoded) > 1400:
            raise Refused("connected mail result too large")
        return encoded

    async def dispatch_start_requests(self):
        """Private owned-test transport; existing single-use authorization checks apply."""
        for cid, entry in self.cases.items():
            path = self.directory / (cid + "-start-request.json")
            if not path.exists():
                continue
            started = time.time() * 1000
            request = None
            try:
                _, request = read_json(path, 8192)
                if (
                    isinstance(request, dict)
                    and (cid, request.get("nonce")) in self.processed_starts
                ):
                    continue
                if (
                    set(request)
                    != {
                        "schema",
                        "call_id",
                        "revision",
                        "brief_hash",
                        "authorization_id",
                        "nonce",
                        "created_ms",
                        "expires_ms",
                    }
                    or request["schema"] != self.schema
                    or request["call_id"] != cid
                    or request["revision"] != entry["revision"]
                    or request["brief_hash"] != entry["brief_hash"]
                    or not isinstance(request["nonce"], str)
                    or not re.fullmatch(r"[a-f0-9]{64}", request["nonce"])
                    or any(
                        type(request[k]) not in {int, float} or not math.isfinite(request[k])
                        for k in ("created_ms", "expires_ms")
                    )
                    or not request["created_ms"]
                    <= started
                    < request["expires_ms"]
                    <= request["created_ms"] + 30000
                ):
                    raise Refused("invalid private start request")
                call = self.service.call(self.service.settings.owner, cid)
                self.scope(call)
                self.require_ready(call)
                result = self.service.start(
                    self.service.settings.owner,
                    Start.model_validate(
                        {k: request[k] for k in ("call_id", "revision", "authorization_id")}
                    ),
                )
                fresh = self.service.call(self.service.settings.owner, cid)
                fresh["usage"].setdefault("startup_timings", {}).update(
                    start_transport="private_local_owner_grant",
                    local_request_created_ms=request["created_ms"],
                    local_request_received_ms=started,
                    local_start_handler_finished_ms=time.time() * 1000,
                )
                self.service.save(fresh)
                error = None
            except (ValueError, Refused, OSError, TypeError):
                result, error = None, "private_start_refused"
            if (
                isinstance(request, dict)
                and isinstance(request.get("nonce"), str)
                and re.fullmatch(r"[a-f0-9]{64}", request["nonce"])
            ):
                private_json(
                    self.directory / (cid + "-start-result.json"),
                    {
                        "schema": self.schema,
                        "call_id": cid,
                        "nonce": request["nonce"],
                        "result": result,
                        "error": error,
                        "received_ms": started,
                        "finished_ms": time.time() * 1000,
                    },
                )
                self.processed_starts.add((cid, request["nonce"]))
