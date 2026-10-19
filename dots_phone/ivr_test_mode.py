"""Explicit IVR-only tests: model-classified human boundary, controller-owned stop."""

from __future__ import annotations

import asyncio
import time


def instructions(call):
    if not call["brief"].get("stop_on_human", False):
        return ""
    return (
        "\nIVR-ONLY TEST MODE. Continue the approved task through automated menus. "
        "As soon as you believe an actual live person has answered, immediately call "
        "end_test_on_human silently, before speaking or consulting Codex. This takes "
        "priority over introductions, answering a person and mandatory consultation. "
        "Do not ask the person any question, perform a live-person check, apologize or "
        "say goodbye. A natural, context-specific live-agent greeting or introduction "
        "is enough to stop; do not wait for certainty. A human-sounding recording or "
        "conversational automated agent is still automation: an IVR routing question, "
        "recording notice, hold music, advertisement, queue update or a promise to "
        "connect you with a representative is not evidence that a person has answered. "
        "Do not stop merely because the word representative or a recorded name occurs. "
        "That distinction applies to end_test_on_human only; it does not override the "
        "prepared call's stop conditions. An unanswered question, failed lookup, or request "
        "for optional information is not by itself a stop condition. Consult Codex when "
        "a missing fact or interpretation needs help; if it remains unknown, give a short "
        "truthful reply or decline the optional step and continue within scope. Do not "
        "assume that asking for information makes its disclosure mandatory. Separately, "
        "if the recipient establishes an actual condition "
        "that the preparation says must end the test (such as rejected verification or an "
        "announced transfer), immediately call end_test_at_stop_condition silently, before "
        "any further speech, menu selection or Codex consultation. Select the applicable prepared "
        "stop condition and briefly explain why the conversation satisfies it in your own words. "
        "You do not need to reproduce transcript wording. An offered transfer option "
        "is different from an announcement that the transfer is happening. Never navigate "
        "past a required stop to keep pursuing the goal. Use what you hear and the conversation "
        "to distinguish a live person from automation. Human detection is an unverified "
        "model judgment, not task completion."
    )


def end_test(llm, reason, *, evidence="", explanation="", stop_condition=""):
    call = llm.supervisor.store.get("call", llm.call_id)
    if not call or not call["brief"].get("stop_on_human", False):
        return False
    if reason not in {"test_human_detected", "test_response_limit", "test_stop_condition"}:
        return False
    if reason == "test_stop_condition" and stop_condition not in call["brief"].get(
        "stop_conditions", []
    ):
        return False
    if llm.stop_requested:
        return True
    call["usage"]["ivr_test_stop"] = {
        "reason": reason,
        "human_detection_verified": False,
        "model_judgment_verified": False,
        "model_evidence": evidence[:400],
        "model_explanation": explanation[:400],
        # Keep what the controller observed separate from the model's account.
        # This is an audit snapshot, never a lexical test of the judgment.
        "conversation_evidence": [
            {"role": entry["role"], "text": entry["text"][-1000:]}
            for entry in (getattr(llm, "dialog", None) or [])[-12:]
            if entry.get("role") in {"user", "assistant"} and entry.get("text")
        ],
        "at": time.time(),
    }
    if stop_condition:
        call["usage"]["ivr_test_stop"]["stop_condition"] = stop_condition
    llm.supervisor.save(call)
    llm.stop_requested = True

    async def stop():
        # Close model input immediately rather than waiting for carrier REST.
        # This also prevents further VAD generations after the response cap.
        socket = getattr(llm, "_websocket", None)
        close = asyncio.create_task(socket.close()) if socket is not None else None
        try:
            await llm.supervisor.end(call["owner"], llm.call_id, reason)
        finally:
            if close is not None:
                await asyncio.gather(close, return_exceptions=True)

    # Independent controller teardown must outlive cancellation of the voice task.
    llm.stop_task = asyncio.create_task(stop())
    return True


def observe_response(llm, event):
    if event.get("type") != "response.created":
        return
    call = llm.supervisor.store.get("call", llm.call_id)
    if not call or not call["brief"].get("stop_on_human", False):
        return
    response_id = event.get("response", {}).get("id")
    if not response_id:
        return
    seen = set(call["usage"].get("ivr_test_response_ids", []))
    if response_id in seen:
        return
    seen.add(response_id)
    call["usage"]["ivr_test_response_ids"] = sorted(seen)
    llm.supervisor.save(call)
    limit = call["brief"].get("test_response_limit", 0)
    if limit and len(seen) >= limit:
        end_test(llm, "test_response_limit")


def tools(llm, call=None):
    if call is None:
        store = getattr(getattr(llm, "supervisor", None), "store", None)
        call = store.get("call", llm.call_id) if store is not None else None
    if not call or not call.get("brief", {}).get("stop_on_human", False):
        return []
    from pipecat.adapters.schemas.function_schema import FunctionSchema

    async def stop(params):
        args = params.arguments
        values = [args.get("evidence"), args.get("reason")]
        valid = all(isinstance(value, str) and 1 <= len(value.strip()) <= 240 for value in values)
        accepted = valid and end_test(
            llm, "test_human_detected", evidence=args["evidence"], explanation=args["reason"]
        )
        await llm.return_local_tool_result(
            params, {"ending": bool(accepted), "task_completed": False}, run_llm=False
        )

    async def stop_at_condition(params):
        args = params.arguments
        condition, evidence = args.get("stop_condition"), args.get("evidence")
        # The model judges meaning. end_test checks the owner's current mode
        # and approved scope; ASR spelling and paraphrases cannot veto teardown.
        valid = (
            isinstance(condition, str)
            and isinstance(evidence, str)
            and 1 <= len(evidence.strip()) <= 400
        )
        accepted = valid and end_test(
            llm,
            "test_stop_condition",
            evidence=evidence,
            explanation=evidence,
            stop_condition=condition,
        )
        await llm.return_local_tool_result(
            params, {"ending": bool(accepted), "task_completed": False}, run_llm=not accepted
        )

    result = [
        FunctionSchema(
            name="end_test_on_human",
            description=(
                "IVR-only test: end immediately and silently when you believe a live person "
                "has answered. Do not speak, ask a check question or consult Codex first. "
                "Exclude automated menus, recordings, holds, advertisements and transfer announcements. "
                "The controller records your unverified judgment and ends this test unresolved."
            ),
            properties={
                "evidence": {"type": "string", "minLength": 1, "maxLength": 240},
                "reason": {"type": "string", "minLength": 1, "maxLength": 240},
            },
            required=["evidence", "reason"],
            handler=stop,
        )
    ]
    conditions = call["brief"].get("stop_conditions", [])
    if conditions:
        result.append(
            FunctionSchema(
                name="end_test_at_stop_condition",
                description=(
                    "IVR-only test: stop silently when current recipient speech establishes "
                    "a prepared stop condition, including rejected verification or an announced "
                    "transfer when the brief requires stopping there. Do this before speaking, "
                    "navigating or consulting Codex. An unanswered or optional question alone "
                    "does not establish a required stop; clarify uncertainty or use an honest "
                    "fallback within scope. Use your judgment about the conversation, "
                    "select the applicable prepared condition and briefly explain why it applies. "
                    "Exact transcript quotes are unnecessary. This ends the test unresolved; it does not record a "
                    "successful balance or other business result."
                ),
                properties={
                    "stop_condition": {"type": "string", "enum": conditions},
                    "evidence": {
                        "type": "string",
                        "description": (
                            "Your brief explanation of why the conversation meets this prepared "
                            "condition. Use your own words; an exact transcript quote is unnecessary."
                        ),
                        "minLength": 1,
                        "maxLength": 400,
                    },
                },
                required=["stop_condition", "evidence"],
                handler=stop_at_condition,
            )
        )
    return result
