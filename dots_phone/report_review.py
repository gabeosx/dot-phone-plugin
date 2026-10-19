"""Bound report repairs to the original claim and recipient observations."""

from __future__ import annotations

import copy
import json
import time
from collections import Counter

from dots_phone.call_task import observed_quote, recipient_evidence, report_feedback
from dots_phone.models import Conclusion, TaskReport
from dots_phone.service import Refused

STATE_KEY = "report_review_state"


def terminal_review_uncertainty(call):
    """Retain scoped unknowns at shutdown, never rejected claims as facts."""
    state = call.get(STATE_KEY)
    if (
        not isinstance(state, dict)
        or state.get("revision") != call.get("revision")
        or state.get("brief_hash") != call.get("brief_hash")
    ):
        return []
    try:
        report = TaskReport.model_validate(state["original_report"])
    except (KeyError, TypeError, ValueError):
        return []
    criteria = {c["id"]: c for c in call["brief"].get("completion_criteria", [])}
    if any(claim.criterion_id not in criteria for claim in report.evidence):
        return []
    questions = list(report.unresolved_questions)
    for criterion_id in dict.fromkeys(claim.criterion_id for claim in report.evidence):
        description = criteria[criterion_id].get("description", criterion_id)
        reason = (
            "Conflicting recipient interpretations"
            if state.get("conflict")
            else "Unverified recipient evidence"
        )
        questions.append(f"{reason}; this remains unconfirmed: {description}"[:240])
    return list(dict.fromkeys(questions))[:7]


def claim_signature(report):
    """Citation fields and ordering may change; factual claim text may not."""
    return Counter((claim["criterion_id"], claim["text"]) for claim in report["evidence"])


def fresh_recipient_entries(entries, state):
    watermark = state["recipient_turn"]
    return [
        entry
        for entry in entries
        if entry.get("role") == "user"
        and type(entry.get("recipient_turn")) is int
        and entry["recipient_turn"] > watermark
        and isinstance(entry.get("recipient_item_id"), str)
        and 0 < len(entry["recipient_item_id"].strip()) <= 256
    ]


def new_review_state(call, report):
    return {
        "revision": call["revision"],
        "brief_hash": call["brief_hash"],
        "original_report": copy.deepcopy(report),
        "recipient_turn": call.get("recipient_audio_turn", 0),
        "conflict": False,
        "disputed_criteria": sorted({claim["criterion_id"] for claim in report["evidence"]}),
    }


def _current(service, call, pending, expected_state):
    fresh = service.call(call["owner"], call["call_id"])
    active = fresh.get("pending") or {}
    if (
        fresh.get("state") != "awaiting_decision"
        or fresh["revision"] != call["revision"]
        or fresh["brief_hash"] != call["brief_hash"]
        or active.get("request_id") != pending["request_id"]
        or active.get("revision") != pending["revision"]
        or active.get("expires", 0) <= time.time()
        or fresh.get(STATE_KEY) != expected_state
    ):
        raise Refused("report review became stale")
    state = fresh.get(STATE_KEY)
    if state and (
        state["revision"] != fresh["revision"] or state["brief_hash"] != fresh["brief_hash"]
    ):
        raise Refused("report repair belongs to another scope")
    return fresh


def _entries(service, call):
    return service.transcripts.get(call["call_id"], call.get("transcript", []))


def recipient_review_snapshot(call, entries):
    """Bind a review to both audio progress and the transcribed observations."""
    return {
        "recipient_audio_turn": call.get("recipient_audio_turn", 0),
        "entries": [
            {key: entry.get(key) for key in ("text", "recipient_turn", "recipient_item_id")}
            for entry in entries
            if entry.get("role") == "user"
        ],
    }


def _recipient_audio_pending(call, entries):
    current_turn = call.get("recipient_audio_turn", 0)
    if not current_turn:
        return False  # Legacy/text callers without audio turn metadata.
    transcribed = [
        entry["recipient_turn"]
        for entry in entries
        if entry.get("role") == "user"
        and type(entry.get("recipient_turn")) is int
        and isinstance(entry.get("recipient_item_id"), str)
        and 0 < len(entry["recipient_item_id"].strip()) <= 256
    ]
    return max(transcribed, default=0) < current_turn


def _listen_for_pending_recipient(entries):
    return _feedback(
        entries,
        "The recipient has started a newer audio turn whose transcription is not yet available. "
        "No result was stored. Listen to that turn, then reassess and submit the final result using "
        "the current recipient evidence. Do not complete the call from the earlier turn alone.",
    )


def _feedback(entries, guidance):
    output = report_feedback(entries)
    output["guidance"] = guidance[:900]
    while len(json.dumps(output, ensure_ascii=False)) > 1450 and output["recipient_evidence"]:
        output["recipient_evidence"].pop(0)
    return output


def _save_state(service, call, pending, expected_state, state):
    fresh = _current(service, call, pending, expected_state)
    fresh[STATE_KEY] = copy.deepcopy(state)
    service.save(fresh)
    return fresh


def _clarification(entries, next_step=None):
    if isinstance(next_step, str) and next_step.strip():
        return _feedback(
            entries,
            "No result was stored. Clarify this specific detail with the recipient: "
            + next_step.strip()[:600]
            + " Do not invent a different conflict or change the factual claim to pass review. "
            "If clarification is unavailable, finish unresolved and retain the uncertainty.",
        )
    return _feedback(
        entries,
        "The proposed result conflicts with an earlier interpretation of this recipient turn. "
        "Do not change the factual claim to match a transcript or retry completion with another "
        "citation. Ask the recipient to clarify the uncertain fact naturally. Neither interpretation "
        "is established by this rejection. If clarification is unavailable, finish unresolved and "
        "retain the uncertainty; do not describe the disputed fact as confirmed.",
    )


def _cites_later_recipient(report, state, entries):
    later = fresh_recipient_entries(entries, state)
    catalog = {item["id"] for item in recipient_evidence(later)}
    for claim in report["evidence"]:
        if claim["criterion_id"] not in state["disputed_criteria"]:
            continue
        if claim.get("evidence_id"):
            if claim["evidence_id"] not in catalog:
                return False
        else:
            try:
                observed_quote(later, claim.get("quote", ""))
            except Refused:
                return False
    return bool(later)


async def _unresolved(service, call, pending, report, validate, state):
    """A downgrade must not promote the same disputed quotation as a fact."""
    safe = copy.deepcopy(report)
    safe["evidence"] = [
        claim
        for claim in report["evidence"]
        if claim["criterion_id"] not in state["disputed_criteria"]
    ]
    note = (
        "Conflicting recipient interpretations remain unconfirmed for: "
        + ", ".join(state["disputed_criteria"])
    )[:240]
    safe["unresolved_questions"] = [
        *[q for q in report["unresolved_questions"] if q != note][:7],
        note,
    ]
    if safe["evidence"]:
        reviewed_snapshot = recipient_review_snapshot(call, _entries(service, call))
        conclusion = await validate(call, safe, pending)
        fresh = _current(service, call, pending, state)
        if recipient_review_snapshot(fresh, _entries(service, fresh)) != reviewed_snapshot:
            return (
                _feedback(
                    _entries(service, fresh),
                    "New recipient audio or transcription arrived during validation. No result "
                    "was stored. Reassess current evidence and preserve the unresolved uncertainty.",
                ),
                None,
            )
        if conclusion is None:
            return (
                _feedback(
                    _entries(service, fresh),
                    "Disputed facts remain unconfirmed. Repair citations for the other facts "
                    "without changing their meaning, and finish unresolved with the uncertainty.",
                ),
                None,
            )
    else:
        conclusion = Conclusion(
            outcome="unresolved",
            confirmed_facts=[],
            unresolved_questions=safe["unresolved_questions"],
        )
    _current(service, call, pending, state)
    return {"result_recorded": True, "outcome": "unresolved"}, conclusion


async def evaluate_report(service, reasoner, call, pending, report, validate):
    """Review a report, preserving rejected claims across citation repairs.

    ``validate(call, report, pending)`` performs existing structural citation
    validation, including its transcription grace period. The caller owns the
    final service.decision commit and its last scope check.
    """
    report = TaskReport.model_validate(report).model_dump(mode="json", exclude_none=True)
    state = copy.deepcopy(call.get(STATE_KEY))
    current = _current(service, call, pending, state)
    entries = _entries(service, current)
    later = fresh_recipient_entries(entries, state) if state else []

    if report["outcome"] == "completed" and _recipient_audio_pending(current, entries):
        return _listen_for_pending_recipient(entries), None

    if state and claim_signature(report) != claim_signature(state["original_report"]):
        changed = {**state, "conflict": True}
        changed.pop("clarification", None)
        current = _save_state(service, call, pending, state, changed)
        state = changed
    if state and state["conflict"]:
        if report["outcome"] == "unresolved":
            return await _unresolved(service, current, pending, report, validate, state)
        if report["outcome"] != "completed" or not later:
            return _clarification(entries, state.get("clarification")), None

    validated_snapshot = recipient_review_snapshot(current, entries)
    conclusion = await validate(current, report, pending)
    current = _current(service, call, pending, state)
    entries = _entries(service, current)
    if report["outcome"] == "completed" and _recipient_audio_pending(current, entries):
        return _listen_for_pending_recipient(entries), None
    if recipient_review_snapshot(current, entries) != validated_snapshot:
        return (
            _feedback(
                entries,
                "New recipient audio or transcription arrived during validation. No result was "
                "stored. Reassess current evidence before submitting the final result.",
            ),
            None,
        )
    if conclusion is None and state is None:
        new_state = new_review_state(current, report)
        current = _save_state(service, call, pending, state, new_state)
        state = new_state

    if conclusion and report["outcome"] != "completed" and state is None:
        return {"result_recorded": True, "outcome": conclusion.outcome}, conclusion

    # Even a structurally rejected quote must reach review: disagreement between
    # native audio interpretation and separate ASR must not disappear in repair.
    question = (
        "Review the final report and any preserved report_review_state. Distinguish citation-only "
        "formatting from a material factual disagreement. A material conflict requires status unknown "
        "with report_supported false and recipient clarification. Approval must resolve any preserved "
        "conflict using relevant later recipient evidence, not merely agree with the transcript."
    )
    reviewed_call = copy.deepcopy(current)
    reviewed_snapshot = recipient_review_snapshot(current, entries)
    try:
        advice = await reasoner.advise(current, entries, question, report=report)
    except Exception:
        current = _current(service, call, pending, state)
        uncertain = copy.deepcopy(state) if state else new_review_state(reviewed_call, report)
        uncertain["conflict"] = True
        _save_state(service, call, pending, state, uncertain)
        return (
            _feedback(
                _entries(service, current),
                "Independent review is unavailable. Do not claim completion or choose between "
                "conflicting interpretations. Ask for clarification if possible; otherwise finish "
                "unresolved with the unconfirmed facts omitted and the uncertainty retained.",
            ),
            None,
        )
    current = _current(service, call, pending, state)
    entries = _entries(service, current)
    if recipient_review_snapshot(current, entries) != reviewed_snapshot:
        if state is None:
            state = new_review_state(reviewed_call, report)
            _save_state(service, call, pending, None, state)
        return (
            _feedback(
                entries,
                "New recipient audio or transcription arrived during review. No result was stored. "
                "Reassess the latest recipient turn and submit current evidence; preserve the "
                "original factual interpretation unless new recipient evidence resolves a difference.",
            ),
            None,
        )
    valid = (
        advice.action is None
        and not advice.answer
        and type(advice.report_supported) is bool
        and advice.status in {"ready", "unknown", "blocked"}
        and (not advice.report_supported or advice.status == "ready")
    )
    if not valid or advice.status in {"unknown", "blocked"}:
        uncertain = copy.deepcopy(state) if state else new_review_state(current, report)
        uncertain["conflict"] = True
        # Preserve the reviewer's actual disputed detail. Generic feedback can
        # make the voice model invent an unrelated conflict from earlier offers.
        uncertain.pop("clarification", None)
        if valid and isinstance(advice.next_step, str) and advice.next_step.strip():
            uncertain["clarification"] = advice.next_step.strip()[:600]
        _save_state(service, call, pending, state, uncertain)
        return _clarification(entries, uncertain.get("clarification")), None

    if advice.report_supported and conclusion:
        if report["outcome"] == "completed" and _recipient_audio_pending(current, entries):
            return _listen_for_pending_recipient(entries), None
        if state and state["conflict"] and not _cites_later_recipient(report, state, entries):
            return _clarification(entries, state.get("clarification")), None
        return {"result_recorded": True, "outcome": conclusion.outcome}, conclusion

    if state is None:
        state = new_review_state(current, report)
        _save_state(service, call, pending, None, state)
    guidance = (
        "Repair only the citation or report format, preserving the original factual claim exactly. "
        "Use the recipient evidence IDs for equivalent wording. Do not change values, dates, units, "
        "status or polarity merely to match the transcript. If the meaning differs, ask the recipient "
        "to clarify before completion. " + (advice.next_step if not advice.report_supported else "")
    )
    return _feedback(entries, guidance), None
