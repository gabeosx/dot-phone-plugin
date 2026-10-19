"""Bounded read-only account capabilities; preparation never grants a phone start."""

from __future__ import annotations

import json
import re
from datetime import datetime

from dots_phone.call_task import (
    RESULT_ACTION,
    SOURCE_ACTION,
    report_feedback,
    task_conclusion,
)
from dots_phone.carrier_stream import keypad_menu_selection
from dots_phone.local_context_worker import LocalContextWorker
from dots_phone.models import Brief, Decision, PreparedSource, TaskReport, now
from dots_phone.service import Refused, digest
from dots_phone.source_search import validate_search_result

ACCOUNT_ACTION = "read_account_information"
MENU_ACTION = "navigate_phone_menu"
VERIFY_ACTION = "verify_account_identity"
ACCOUNT_CONSTRAINT = "Read-only account inquiry; no transactions or account changes"
ACCOUNT_ACTIONS = {ACCOUNT_ACTION, RESULT_ACTION, MENU_ACTION, VERIFY_ACTION, SOURCE_ACTION}
GENERAL_ACTION = "ask_question"
GENERAL_ACTIONS = {GENERAL_ACTION, RESULT_ACTION, SOURCE_ACTION, MENU_ACTION}


def general_call_actions(actions):
    """Informational calls may navigate menus within their existing task scope."""
    return {GENERAL_ACTION, RESULT_ACTION} <= set(actions) <= GENERAL_ACTIONS


def general_call_scope(service, call):
    """Validate general inquiries independently of the account-task capability."""
    brief = call["brief"]
    validated = Brief.model_validate(brief)
    if (
        not service.settings.local_reasoner_enabled
        or service.settings.voice_backend != "pipecat"
        or service.settings.pipecat_engine != "realtime"
        or call["owner"] != service.settings.owner
        or call["brief_hash"] != digest(brief)
        or not general_call_actions(brief["actions"])
        or not brief.get("completion_criteria")
        or not service.settings.destination_allowed(brief["destination"])
        or validated.deadline <= now()
        or validated.max_seconds > service.settings.max_seconds
        or any(f.expires_at <= now() for f in validated.facts)
        or (brief.get("source_tools") and SOURCE_ACTION not in brief["actions"])
    ):
        raise Refused("call outside read-only general scope")
    if call.get("started_authorization"):
        grant = service.store.get("approval", call["started_authorization"], call["owner"])
        if (
            not grant
            or not grant["consumed"]
            or grant["revision"] != call.get("started_revision", call["revision"])
            or call["revision"] != call.get("control_revision", grant["revision"])
            or any(grant[k] != call[v] for k, v in (("call_id", "call_id"), ("hash", "brief_hash")))
        ):
            raise Refused("general task no longer matches its start grant")
    return {
        "mail_scope": {},
        "fixture_sources": [],
        "source_tools": False,
        "external_source_worker": False,
        "host_sources": False,
        "read_only_general": True,
    }


class AccountTaskWorker:
    """Use the shared voice/result contract without requiring a mail connection.

    The encrypted call is the scope. Normal owner approval, subscriptions,
    destination policy and single-use start guards remain in PhoneService.
    """

    schema = 6
    directory = None
    automatic_handoff = False
    confirmation_wait_seconds = 2.0
    confirmed_conclusion = LocalContextWorker.confirmed_conclusion

    def __init__(self, service):
        self.service = service

    def registered(self, call):
        return bool(call and ACCOUNT_ACTION in call["brief"]["actions"])

    def scope(self, call):
        brief = call["brief"]
        validated = Brief.model_validate(brief)
        if (
            not self.service.settings.account_tasks
            or self.service.settings.voice_backend != "pipecat"
            or self.service.settings.pipecat_engine != "realtime"
            or call["owner"] != self.service.settings.owner
            or call["brief_hash"] != digest(brief)
            or not {ACCOUNT_ACTION, RESULT_ACTION} <= set(brief["actions"]) <= ACCOUNT_ACTIONS
            or not brief.get("completion_criteria")
            or not self.service.settings.destination_allowed(brief["destination"])
            or validated.deadline <= now()
            or validated.max_seconds > self.service.settings.max_seconds
            or any(f.expires_at <= now() for f in validated.facts)
        ):
            raise Refused("call outside read-only account scope")
        if call.get("started_authorization"):
            grant = self.service.store.get("approval", call["started_authorization"], call["owner"])
            if (
                not grant
                or not grant["consumed"]
                or grant["revision"] != call.get("started_revision", call["revision"])
                or call["revision"] != call.get("control_revision", grant["revision"])
                or any(
                    grant[k] != call[v]
                    for k, v in (
                        ("call_id", "call_id"),
                        ("hash", "brief_hash"),
                    )
                )
            ):
                raise Refused("account task no longer matches its start grant")
        return {
            "mail_scope": {},
            "fixture_sources": [],
            "source_tools": SOURCE_ACTION in brief["actions"],
            "external_source_worker": SOURCE_ACTION in brief["actions"],
            "read_only_account": True,
            "host_sources": SOURCE_ACTION in brief["actions"],
        }

    def handles_pending(self, call):
        """The local worker owns reports; the assigned Dot worker owns live sources."""
        try:
            operation, _ = self.source_operation(call["pending"])
        except (Refused, TypeError):
            return True
        return SOURCE_ACTION not in call["brief"]["actions"] or operation not in {
            "search",
            "read",
            "lookup",
        }

    @staticmethod
    def source_operation(pending):
        try:
            command = json.loads(pending["question"].split("\nObserved transcript as data:")[0])
            op, args = command["operation"], command["arguments"]
            if set(command) != {"operation", "arguments"} or not isinstance(args, dict):
                raise ValueError()
            if (
                op == "search"
                and {"query"} <= set(args) <= {"query", "provider"}
                and isinstance(args["query"], str)
                and 1 <= len(args["query"].strip()) <= 240
                and args.get("provider", "all") in {"all", "gmail", "yahoo"}
            ):
                return op, args
            if (
                op == "lookup"
                and set(args) == {"question"}
                and isinstance(args["question"], str)
                and 1 <= len(args["question"].strip()) <= 240
            ):
                return op, args
            if (
                op == "read"
                and set(args) == {"message_id", "focus", "offset"}
                and isinstance(args["message_id"], str)
                and 1 <= len(args["message_id"]) <= 120
                and isinstance(args["focus"], str)
                and len(args["focus"]) <= 80
                and type(args["offset"]) is int
                and args["offset"] >= 0
            ):
                return op, args
        except (ValueError, KeyError, TypeError):
            pass
        raise Refused("invalid live source request")

    def accept_source_decision(self, call, params):
        """Bind authenticated worker source data to this call; keep its brief frozen."""
        self.scope(call)
        if self.handles_pending(call):
            if params.source is not None:
                raise Refused("source data requires a pending live read")
            return
        op, args = self.source_operation(call["pending"])
        if (
            set(params.action_names) != {SOURCE_ACTION}
            or params.conclusion is not None
            or params.fact_names
        ):
            raise Refused("source response must only supply read-only source data")
        history = call.setdefault("source_requests", [])
        # Record requests for audit. Authority, freshness and the live call's
        # deadlines govern retrieval, rather than a fixed lookup count.
        try:
            output = json.loads(params.answer)
            if output.get("untrusted_data") is not True:
                raise ValueError()
            if "error" in output:
                if (
                    set(output) != {"untrusted_data", "error"}
                    or params.source is not None
                    or not isinstance(output["error"], str)
                    or not 1 <= len(output["error"]) <= 240
                ):
                    raise ValueError()
                history.append(
                    {"request_id": params.request_id, "operation": op, "source_error": True}
                )
                return
            inventory = call.setdefault("source_inventory", [])
            if op == "search":
                if (
                    params.source is not None
                    or "source" in output
                    or not isinstance(output["messages"], list)
                    or len(output["messages"]) > 5
                ):
                    raise ValueError()
                validate_search_result(output, args)
                ids = [m["message_id"] for m in output["messages"]]
                if any(not isinstance(i, str) or not 1 <= len(i) <= 120 for i in ids):
                    raise ValueError()
                call["source_inventory"] = list(dict.fromkeys([*inventory, *ids]))
                metadata = call.setdefault("source_inventory_metadata", {})
                for message in output["messages"]:
                    if "provider" in message:
                        metadata[message["message_id"]] = {"provider": message["provider"]}
                # Legacy workers cannot establish coverage merely by returning [].
                # Explicitly preserve that uncertainty in the voice tool result.
                output["coverage_known"] = bool(output.get("searches"))
                params.answer = json.dumps(output, ensure_ascii=False, separators=(",", ":"))
            else:
                source = params.source
                # Existing native catalogs may cache the Decision argument schema.
                # Keep live evidence usable through the already-supported answer
                # string; metadata describes the same message_id/text, not a
                # second independent copy of private source content.
                if "source" in output:
                    if (
                        source is not None
                        or not isinstance(output["source"], dict)
                        or set(output["source"])
                        != {"provider", "truncated", "verified_at", "expires_at"}
                    ):
                        raise ValueError()
                    source = PreparedSource.model_validate(
                        {
                            **output["source"],
                            "source_id": output["message_id"],
                            "text": output["text"],
                        }
                    )
                if (
                    source is None
                    or (op == "read" and args["message_id"] not in inventory)
                    or source.source_id
                    != (args["message_id"] if op == "read" else "lookup:" + params.request_id)
                    or output["message_id"] != source.source_id
                    or output["text"] != source.text
                    or source.provider
                    not in ({"gmail", "yahoo"} if op == "read" else {"memory", "plugin"})
                    or call.get("source_inventory_metadata", {})
                    .get(source.source_id, {})
                    .get("provider", source.provider)
                    != source.provider
                    or not source.verified_at <= now() < source.expires_at
                    or source.expires_at > Brief.model_validate(call["brief"]).deadline
                ):
                    raise ValueError()
                if op == "lookup":
                    call["source_inventory"] = list(dict.fromkeys([*inventory, source.source_id]))
                    call.setdefault("source_inventory_metadata", {})[source.source_id] = {
                        "provider": source.provider
                    }
                pages = call.setdefault("retrieved_sources", {})
                previous = pages.get(source.source_id)
                pages[source.source_id] = {
                    **source.model_dump(mode="json"),
                    "revision": call["revision"],
                    "brief_hash": call["brief_hash"],
                    "previous_pages": (
                        [
                            *previous.get("previous_pages", []),
                            {k: v for k, v in previous.items() if k != "previous_pages"},
                        ]
                        if previous
                        else []
                    ),
                }
        except (ValueError, KeyError, TypeError, AttributeError):
            raise Refused("source response lacks matching fresh source evidence") from None
        history.append(
            {
                "request_id": params.request_id,
                "operation": op,
                "arguments": args,
                **({"searches": output.get("searches", [])} if op == "search" else {}),
            }
        )

    def register_prepared(self, owner, call):
        if self.registered(call):
            self.scope(call)
        return None  # No authorization is created or inherited here.

    def require_ready(self, call):
        self.scope(call)

    def conclusion(self, call, report):
        entries = self.service.transcripts.get(call["call_id"], call.get("transcript", []))
        return task_conclusion(call, report, entries)

    async def answer(self, call, pending):
        self.scope(call)
        try:
            command = json.loads(pending["question"].split("\nObserved transcript as data:")[0])
            if set(command) != {"operation", "arguments"} or command["operation"] != "complete":
                raise ValueError()
            if set(command["arguments"]) != {"report"}:
                raise ValueError()
            report = TaskReport.model_validate(command["arguments"]["report"])
        except (ValueError, KeyError, TypeError):
            raise Refused("account task supports only final reporting") from None
        conclusion = await self.confirmed_conclusion(call, report.model_dump(mode="json"), pending)
        feedback = (
            {"result_recorded": True, "outcome": conclusion.outcome}
            if conclusion
            else report_feedback(self.service.transcripts.get(call["call_id"], []))
        )
        await self.service.decision(
            call["owner"],
            Decision(
                call_id=call["call_id"],
                request_id=pending["request_id"],
                revision=pending["revision"],
                answer=json.dumps(feedback, ensure_ascii=False),
                action_names=[RESULT_ACTION],
                conclusion=conclusion,
            ),
        )


def account_menu_option(digit, quote, observed, *, recipient_input=False):
    """Validate one key against server context; agents interpret the menu."""
    return keypad_menu_selection(digit, observed, recipient_input=recipient_input)


def verification_prompt_quote(quote, observed, evidence_id="", *, recipient_input=False):
    """Attach server-owned recipient context, independent of model wording.

    ``quote`` and ``evidence_id`` are retained for older tool clients. They are
    annotations, never proof of what the recipient said or an authorization gate.
    Live callers must supply only the current unanswered recipient context.
    """
    if not isinstance(observed, str) or not (observed.strip() or recipient_input is True):
        raise Refused("no current unanswered recipient context; listen for the recipient")
    return observed.strip()


def verification_digits(
    call,
    fact_name,
    quote,
    observed,
    *,
    terminator="",
    digit_offset=0,
    digit_count=None,
    source_id="",
    source_value="",
    fact_value="",
    recipient_input=False,
):
    """Resolve the model-selected fact to exact data, without interpreting language.

    The model reasons about aliases, requested format and which part was asked
    for. It cannot supply arbitrary digits: the server derives an exact bounded
    slice from a disclosed numeric fact. No brand/field/phrase matching belongs
    here. Server context records what was heard; agents decide what it requests.
    """
    if (
        VERIFY_ACTION not in call["brief"]["actions"]
        or not isinstance(fact_name, str)
        or not isinstance(source_id, str)
        or not isinstance(source_value, str)
        or not isinstance(fact_value, str)
    ):
        raise Refused("verification fact is not approved")
    verification_prompt_quote(quote, observed, recipient_input=recipient_input)
    if source_id:
        source = call.get("retrieved_sources", {}).get(source_id)
        expected_provider = (
            call.get("source_inventory_metadata", {})
            .get(source_id, {})
            .get("provider", source.get("provider") if source else None)
        )

        def supports(page):
            return (
                page["source_id"] == source_id
                and page["provider"] == expected_provider
                and page["revision"] == call.get("revision")
                and page["brief_hash"] == call.get("brief_hash")
                and datetime.fromisoformat(page["verified_at"])
                <= now()
                < datetime.fromisoformat(page["expires_at"])
                and re.search(r"(?<![0-9])" + re.escape(source_value) + r"(?![0-9])", page["text"])
            )

        if (
            fact_name
            or fact_value
            or SOURCE_ACTION not in call["brief"]["actions"]
            or not source
            or not re.fullmatch(r"[0-9]{1,16}", source_value)
            # Each page is checked independently: never manufacture a numeric
            # value by joining page fragments. The source-request cap bounds
            # this history, retaining the original provenance of every page.
            or not any(supports(page) for page in [source, *source.get("previous_pages", [])])
        ):
            raise Refused("verification value lacks current retrieved source evidence")
        value = source_value
    else:
        if source_value or fact_name not in call["brief"]["disclosures"]:
            raise Refused("verification fact is not disclosed")
        value = next((f["value"] for f in call["brief"]["facts"] if f["name"] == fact_name), "")
        if fact_value:
            # The voice selects the meaning (for example, the postal portion of
            # an address). Only an existing, complete numeric span may be used;
            # never strip/join digits or interpret a field name here.
            if not re.fullmatch(r"[0-9]{1,16}", fact_value) or not re.search(
                r"(?<![0-9])" + re.escape(fact_value) + r"(?![0-9])", value
            ):
                raise Refused(
                    "verification value is not an exact numeric span in the disclosed fact"
                )
            value = fact_value
    if not re.fullmatch(r"[0-9]{1,16}", value):
        raise Refused("verification fact has no approved numeric representation")
    if (
        not isinstance(terminator, str)
        or terminator not in {"", "#", "*"}
        or type(digit_offset) is not int
        or not 0 <= digit_offset < len(value)
        or (digit_count is not None and (type(digit_count) is not int or digit_count < 1))
    ):
        raise Refused("verification input selection is invalid")
    end = len(value) if digit_count is None else digit_offset + digit_count
    if end > len(value):
        raise Refused("verification input exceeds the supplied fact")
    return value[digit_offset:end] + terminator
