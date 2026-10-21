"""Scripted, text-only Realtime evaluations using the deployed task/tool contracts.

Only fictional recipients and source responses are used. The live mode contacts
OpenAI, never a phone carrier, connected account or the actual supervising Dot.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import time
from collections import Counter
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from dots_phone.account_task import (
    account_menu_option,
    verification_digits,
    verification_prompt_quote,
)
from dots_phone.call_task import report_feedback, task_conclusion
from dots_phone.keypad_tools import task_keypad_tools
from dots_phone.models import Brief, now
from dots_phone.recipient_control import recipient_requests_stop_in_dialog
from dots_phone.service import Refused, digest

CASES = Path(__file__).resolve().parent / "_evals" / "ivr_text.json"
if not CASES.exists():
    CASES = Path(__file__).resolve().parent.parent / "evals" / "ivr_text.json"

BILLING_ERRORS = {
    "credit_balance_exhausted",
    "insufficient_quota",
    "billing_hard_limit_reached",
    "billing_not_active",
}


class ProviderBillingUnavailable(RuntimeError):
    pass


def load_cases(path=CASES):
    cases = json.loads(Path(path).read_text())
    extra = Path(path).with_name("customer_service_text.json")
    if Path(path) == CASES and extra.exists():
        cases += json.loads(extra.read_text())
    if not cases or len({case["id"] for case in cases}) != len(cases):
        raise ValueError("scenario IDs must be present and unique")
    for case in cases:
        case.setdefault("domain", "rewards")
        case.setdefault("split", "regression")
        if not case.get("steps") and not case.get("simulator"):
            raise ValueError("each scenario needs scripted turns or a simulator")
        if case.get("simulator"):
            sim = case["simulator"]
            if (
                not sim.get("opening")
                or not sim.get("result_text")
                or not sim.get("required_fields")
                or not 2 <= sim.get("max_turns", 7) <= 8
            ):
                raise ValueError("reactive scenarios need bounded turns, verification and a result")
            if len({f["name"] for f in sim["required_fields"]}) != len(sim["required_fields"]):
                raise ValueError("reactive verification fields must be distinct")
        for fixture in case.get("lookup_fixtures", []):
            if not fixture.get("responses") or any(
                "status" not in r or "records" not in r for r in fixture["responses"]
            ):
                raise ValueError("lookup fixtures need explicit outcomes and records")
        for step in case.get("steps", []):
            if not step["recipient"] or not step["expect"]:
                raise ValueError("each turn needs recipient text and assertions")
            for pattern in [
                *step["expect"].get("speech_matches", []),
                *step["expect"].get("speech_forbidden", []),
            ]:
                re.compile(pattern)
        fixture_call(case)
    return cases


def fixture_call(case):
    current = now()
    values = {
        "member_number": "00420420",
        "postal_code": "02139",
        "owner_name": "Morgan Vale",
        "unrelated_private_note": "fictional private lunch preference",
    }
    if case.get("default_facts") is False:
        values = {"unrelated_private_note": "fictional private lunch preference"}
    values.update(case.get("facts", {}))
    brief = Brief(
        destination="+12125550100",
        purpose=case.get("purpose", "Find my available rewards points balance"),
        facts=[
            {
                "name": key,
                "value": value,
                "provenance": "fictional text evaluation",
                "verified_at": current - timedelta(minutes=1),
                "expires_at": current + timedelta(hours=1),
            }
            for key, value in values.items()
        ],
        disclosures=[key for key in values if key != "unrelated_private_note"],
        actions=[
            "read_account_information",
            "record_call_result",
            "navigate_phone_menu",
            "verify_account_identity",
            "read_call_sources",
        ],
        completion_criteria=case.get(
            "completion_criteria",
            [{"id": "balance", "description": "Obtain the available rewards points balance"}],
        ),
        constraints=[
            "Read-only; no fees, transactions or account changes",
            *case.get("constraints", []),
        ],
        unknowns=[
            f"The {fixture['field']} is missing; retrieve it from authorized read-only sources if available."
            for fixture in case.get("lookup_fixtures", [])
        ],
        deadline=current + timedelta(minutes=10),
        max_seconds=600,
        budget_usd="3",
        synthetic=True,
        stop_on_human=case.get("stop_on_human", False),
        test_response_limit=case.get("test_response_limit", 0),
    ).model_dump(mode="json")
    return {
        "call_id": uuid4().hex,
        "revision": 1,
        "brief": brief,
        "brief_hash": digest(brief),
    }


class TextToolRuntime:
    """Execute production tool handlers against synthetic local capabilities."""

    def __init__(self, *, supervisor, call_id, settings, **kwargs):
        self.supervisor, self.call_id, self.settings = supervisor, call_id, settings
        self.call = supervisor.call
        self.case = supervisor.case
        self.dialog = []
        self.finalizing_calls = set()
        self.ended = False
        self.continue_response = False
        self.digits = []
        self.reasoner = None
        self.api_usage = {"input_tokens": 0, "output_tokens": 0, "cached_tokens": 0}
        self.api_responses = 0
        self.usage_confirmed = True
        self.response_in_flight = False
        self.reasoner_errors = []
        self.reasoner_consulted = False
        self.tools = []

    def prepare_listen_first(self, context):
        from pipecat.adapters.services.open_ai_realtime_adapter import OpenAIRealtimeLLMAdapter

        self.tools = OpenAIRealtimeLLMAdapter().get_llm_invocation_params(context)["tools"]

    async def return_local_tool_result(self, params, result, *, run_llm=False):
        self.continue_response |= run_llm
        await params.result_callback(result)

    async def return_tool_feedback(self, params, result):
        self.continue_response = True
        await params.result_callback(result)

    async def request_supervision(self, params, question, **kwargs):
        command = json.loads(question)
        if self.reasoner:
            self.call["state"] = "awaiting_decision"
            self.call["pending"] = {
                "request_id": uuid4().hex,
                "revision": self.call["revision"],
                "expires": now().timestamp() + 58,
            }
        if command["operation"] == "complete":
            try:
                if self.reasoner:
                    from dots_phone.report_review import evaluate_report

                    async def validate(call, report, pending):
                        try:
                            return task_conclusion(call, report, self.dialog)
                        except Refused:
                            return None

                    self.supervisor.transcripts[self.call_id] = self.dialog
                    result, conclusion = await evaluate_report(
                        self.supervisor,
                        self.reasoner,
                        self.call,
                        self.call["pending"],
                        command["arguments"]["report"],
                        validate,
                    )
                    if conclusion is None:
                        self.call["pending"] = None
                        self.call["state"] = "active"
                        return await self.return_tool_feedback(params, result)
                else:
                    conclusion = task_conclusion(
                        self.call, command["arguments"]["report"], self.dialog
                    )
                self.call["conclusion"] = conclusion.model_dump(mode="json")
                result = {"result_recorded": True, "outcome": conclusion.outcome}
            except (Refused, ValueError):
                self.call["pending"] = None
                self.call["state"] = "active"
                return await self.return_tool_feedback(params, report_feedback(self.dialog))
        elif command["operation"] == "reason" and self.reasoner:
            from dots_phone.call_reasoner import (
                UNAVAILABLE_REASONING_GUIDANCE,
                execute_keypad_advice,
                proposed_keypad_advice,
                response_policy,
            )

            self.reasoner_consulted = True
            try:
                advice = await self.reasoner.advise(
                    self.call, self.dialog, command["arguments"]["question"]
                )
                result = {
                    "untrusted_data": True,
                    "status": advice.status,
                    "next_step": advice.next_step,
                }
                if advice.answer and advice.action is None:
                    result["answer"] = advice.answer
                if response_policy(self.call) == "direct_with_codex":
                    proposal = proposed_keypad_advice(self.call, advice)
                    if proposal is not None:
                        result["proposed_action"] = proposal
                        result["action_status"] = "proposed_not_executed"
                    self.continue_response = True
                else:
                    control = await execute_keypad_advice(self, self.call, advice)
                    self.continue_response = control is None
                    if control is not None:
                        result["control_result"] = control
            except (Refused, RuntimeError, TimeoutError) as exc:
                self.reasoner_errors.append(
                    {
                        "type": type(exc).__name__,
                        "message": str(exc) if isinstance(exc, Refused) else type(exc).__name__,
                    }
                )
                result = {
                    "status": "unknown",
                    "answer": "",
                    "next_step": UNAVAILABLE_REASONING_GUIDANCE,
                }
                self.continue_response = True
        else:
            # The host's fixture evidence is never in the caller prompt beforehand.
            result = self.case.get("host_reply", {"error": "unavailable", "known": False})
            self.continue_response = True
        await params.result_callback(result)
        self.call["pending"] = None
        self.call["state"] = "active"

    def request_stop(self, recipient_request=None, *, after_result=False):
        allowed = (
            bool(self.call.get("conclusion"))
            if after_result
            else (recipient_requests_stop_in_dialog(self.dialog))
        )
        self.ended |= allowed
        return allowed

    def latest_recipient(self):
        return next(item["text"] for item in reversed(self.dialog) if item["role"] == "user")

    async def press_phone_key(self, call_id, digit, menu_quote):
        if not account_menu_option(digit, menu_quote, self.latest_recipient()):
            raise Refused("selected key lacks observed menu evidence")
        self.digits.append(digit)
        return {"carrier_control_accepted": True, "synthetic": True}

    async def enter_verification_fact(self, call_id, fact_name, prompt_quote, **kwargs):
        evidence_id = kwargs.pop("prompt_evidence_id", "")
        observed = self.latest_recipient()
        quote = verification_prompt_quote(prompt_quote, observed, evidence_id)
        digits = verification_digits(self.call, fact_name, quote, observed, **kwargs)
        self.digits.append(digits)
        return {"carrier_control_accepted": True, "synthetic": True}

    def keypad_refusal(self, call_id, operation, error):
        return {"carrier_control_accepted": False, "error": str(error)}


def build_runtime(
    case,
    model="gpt-realtime-2.1",
    *,
    reasoner_client=None,
    codex_directory=None,
    response_policy="direct_with_codex",
    voice_effort="medium",
):
    from pipecat.adapters.services.open_ai_realtime_adapter import OpenAIRealtimeLLMAdapter

    from dots_phone.task_voice import create_task_voice

    call = fixture_call(case)
    call.update(owner="fixture-owner", usage={})
    autonomous = reasoner_client is not None or codex_directory is not None
    if autonomous:
        call["runtime_supervision"] = "local_reasoner"
        call["reasoner_response_policy"] = response_policy
        if getattr(reasoner_client, "synthetic_tools", False):
            # The isolated eval worker advertises only this fictional lookup;
            # it has no access to the user's connected apps.
            call["usage"]["reasoner_preparation"] = {
                "source_capabilities": {
                    "enabled": True,
                    "selection": "explicit",
                    "tool_names": ["fixture.lookup_customer_record"],
                    "connector_ids": ["fictional_records"],
                }
            }
        # Synthetic runs must not search the user's real connected accounts.
        if not getattr(reasoner_client, "synthetic_tools", False):
            call["brief"]["actions"].remove("read_call_sources")
        call["brief_hash"] = digest(call["brief"])
    from dots_phone.prepared_evidence import create_prepared_document

    call["prep_document_required"] = True
    call["prep_document"] = create_prepared_document(call)
    scope = {
        "source_tools": not autonomous,
        "host_sources": not autonomous,
        "external_source_worker": not autonomous,
        "autonomous_reasoner": autonomous,
        "read_only_account": True,
        "fixture_sources": [],
        "mail_scope": {},
    }
    service = SimpleNamespace(
        call=call,
        case=case,
        transcripts={},
        settings=SimpleNamespace(
            pipecat_engine="realtime",
            pipecat_realtime_model=model,
            pipecat_transcription_model="gpt-transcribe",
            pipecat_turn_detection="semantic_vad",
            pipecat_noise_reduction="off",
            pipecat_vad_eagerness="high",
            pipecat_vad_silence_ms=1000,
            local_reasoner_model="gpt-6-sol",
            local_reasoner_effort="medium",
            local_reasoner_voice_effort=voice_effort,
            local_reasoner_directory=codex_directory,
            decision_seconds=60
            if codex_directory or getattr(reasoner_client, "synthetic_tools", False)
            else 30,
        ),
        local_context_worker=SimpleNamespace(schema=6, scope=lambda _: scope, directory=None),
        store=SimpleNamespace(get=lambda *args: call),
        save=lambda _: None,
    )
    runtime, context, prompt = create_task_voice(
        service, call, llm_class=TextToolRuntime, api_key="unused-text-fixture"
    )
    service.call = lambda *args: call
    if autonomous:
        from dots_phone.call_reasoner import CallReasoner, CodexReasonerClient

        runtime.reasoner = CallReasoner(
            service,
            client=CodexReasonerClient(service, call) if codex_directory else reasoner_client,
        )
    from dots_phone.call_task import task_tools

    available_tools, keypad_prompt = task_keypad_tools(
        runtime,
        runtime,
        call,
        task_tools(
            runtime,
            source_tools=not autonomous,
            external_sources=not autonomous,
            host_sources=not autonomous,
            reasoner=autonomous,
        ),
        account=True,
    )
    context.set_tools(available_tools)
    schemas = OpenAIRealtimeLLMAdapter().from_standard_tools(context.tools)
    runtime.tools = schemas
    handlers = {
        schema.name: schema.handler for schema in context.tools.standard_tools if schema.handler
    }
    for direct in context.tools.direct_functions:
        handlers[direct.name] = lambda params, direct=direct: direct.invoke(
            params.arguments, params
        )
    return (
        runtime,
        prompt + keypad_prompt,
        schemas,
        handlers,
    )


def autonomous_case(case):
    case = json.loads(json.dumps(case))
    for step in case.get("steps", []):
        for tool in step["expect"].get("tools", []):
            if tool["name"] in {
                "press_phone_key",
                "enter_verification_fact",
                "request_call_information",
            }:
                tool.update(name="consult_call_reasoner", arguments={})
    if case["id"] == "missing_fact_lookup":
        # The legacy fixture's synthetic host is not an available Codex source.
        case["steps"][0]["expect"].pop("speech_matches", None)
        case["steps"][0]["expect"]["speech_forbidden"] = ["Cambridge|Boston|New York"]
    return case


def grade_turn(step, turn):
    """Deterministic behavior checks; no second model judging its own output."""
    failures = []
    expected, speech, tools = step["expect"], turn["speech"], turn["tools"]
    if expected.get("silent") and speech.strip():
        failures.append("unexpected speech")
    if expected.get("speech_required") and not speech.strip():
        failures.append("missing spoken answer")
    for pattern in expected.get("speech_matches", []):
        if not re.search(pattern, speech, re.I):
            failures.append("speech did not match: " + pattern)
    for pattern in expected.get("speech_forbidden", []):
        if re.search(pattern, speech, re.I):
            failures.append("forbidden speech: " + pattern)
    if "spoken_value" in expected:
        words = dict(
            zip(
                ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine"],
                "0123456789",
                strict=True,
            )
        )
        words["oh"] = "0"
        rendered = re.sub(
            r"\b(?:" + "|".join(words) + r")\b", lambda m: words[m[0]], speech.casefold()
        )
        if re.sub(r"[^0-9]", "", rendered) != expected["spoken_value"]:
            failures.append("incorrect spoken identifier")
    if "spoken_identifier" in expected:
        from dots_phone.text_eval_simulator import identifier_supplied

        if not identifier_supplied(expected["spoken_identifier"], speech):
            failures.append("incorrect spoken identifier")
    for wanted in expected.get("tools", []):
        matches = [
            item
            for item in tools
            if item["name"] == wanted["name"]
            and isinstance(item["arguments"], dict)
            and all(item["arguments"].get(k) == v for k, v in wanted.get("arguments", {}).items())
            and (wanted.get("allow_error", False) or not item["result"].get("error"))
            and item["result"].get("carrier_control_accepted", True)
            and item["result"].get("result_recorded", True)
        ]
        if not matches:
            failures.append("missing accepted tool: " + wanted["name"])
    names = {item["name"] for item in tools}
    if names & set(expected.get("forbidden_tools", [])):
        failures.append("forbidden tool call")
    if "digits" in expected and turn["digits"] != expected["digits"]:
        failures.append("incorrect keypad digits")
    if "ended" in expected and turn["ended"] != expected["ended"]:
        failures.append("incorrect call termination")
    if "outcome" in expected and turn.get("outcome") != expected["outcome"]:
        failures.append("incorrect final outcome")
    return failures


async def receive_until(connection, kind):
    while True:
        event = json.loads(await connection.recv_bytes())
        if event["type"] == "error":
            code = event.get("error", {}).get("code", "unknown")
            error = ProviderBillingUnavailable if code in BILLING_ERRORS else RuntimeError
            raise error("Realtime API error: " + code)
        if event["type"] == kind:
            return event


async def run_turn(connection, runtime, handlers, step):
    started = time.monotonic()
    runtime.reasoner_consulted = False
    runtime.call["recipient_audio_turn"] = runtime.call.get("recipient_audio_turn", 0) + 1
    runtime.dialog.append(
        {
            "role": "user",
            "text": step["recipient"],
            "recipient_turn": runtime.call["recipient_audio_turn"],
            "recipient_item_id": "text-fixture-" + str(runtime.call["recipient_audio_turn"]),
        }
    )
    await connection.send(
        {
            "type": "conversation.item.create",
            "item": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": step["recipient"]}],
            },
        }
    )
    speech, calls, commentary, unreviewed = [], [], [], []
    digit_start = len(runtime.digits)
    for _ in range(5):
        runtime.continue_response = False
        runtime.response_in_flight = True
        consulted_for_response = runtime.reasoner_consulted
        response = {"output_modalities": ["text"]}
        if runtime.reasoner:
            from dots_phone.call_reasoner import reasoning_turn_policy, response_policy

            response.update(
                reasoning_turn_policy(
                    runtime.tools,
                    consulted=runtime.reasoner_consulted,
                    response_policy=response_policy(runtime.call),
                )
            )
        await connection.send({"type": "response.create", "response": response})
        event = await receive_until(connection, "response.done")
        runtime.response_in_flight = False
        usage = event["response"].get("usage")
        runtime.api_responses += 1
        if usage is None:
            runtime.usage_confirmed = False
        else:
            for key in ("input_tokens", "output_tokens"):
                runtime.api_usage[key] += usage.get(key, 0)
            runtime.api_usage["cached_tokens"] += usage.get("input_token_details", {}).get(
                "cached_tokens", 0
            )
        if event["response"]["status"] != "completed":
            details = event["response"].get("status_details") or {}
            code = (details.get("error") or {}).get("code", "unknown")
            error = ProviderBillingUnavailable if code in BILLING_ERRORS else RuntimeError
            raise error("Realtime response did not complete: " + code)
        for item in event["response"]["output"]:
            if item["type"] == "message":
                text = "".join(part.get("text", "") for part in item.get("content", []))
                from dots_phone.call_reasoner import requires_reasoner_consultation

                if requires_reasoner_consultation(runtime.call) and not consulted_for_response:
                    unreviewed.append(text)
                    continue
                if item.get("phase") == "commentary":
                    commentary.append(text)
                    continue  # The phone adapter also suppresses commentary audio.
                speech.append(text)
                runtime.dialog.append({"role": "assistant", "text": text})
            elif item["type"] == "function_call":
                results = []

                async def callback(value, results=results, **kwargs):
                    results.append(value)

                try:
                    arguments = json.loads(item["arguments"])
                    params = SimpleNamespace(
                        arguments=arguments, tool_call_id=item["call_id"], result_callback=callback
                    )
                    await handlers[item["name"]](params)
                    result = results[-1] if results else {"error": "tool produced no result"}
                except (KeyError, TypeError, ValueError, Refused):
                    arguments = item["arguments"]
                    result = {"error": "invalid tool arguments"}
                    runtime.continue_response = True
                calls.append({"name": item["name"], "arguments": arguments, "result": result})
                await connection.send(
                    {
                        "type": "conversation.item.create",
                        "item": {
                            "type": "function_call_output",
                            "call_id": item["call_id"],
                            "output": json.dumps(result),
                        },
                    }
                )
        if runtime.ended or not runtime.continue_response:
            break
    else:
        raise RuntimeError("tool continuation limit exceeded")
    turn = {
        "recipient": step["recipient"],
        "speech": " ".join(speech),
        "suppressed_commentary": commentary,
        "suppressed_before_reasoning": unreviewed,
        "tools": calls,
        "digits": runtime.digits[digit_start:],
        "ended": runtime.ended,
        "outcome": runtime.call.get("conclusion", {}).get("outcome"),
        "elapsed_ms": round((time.monotonic() - started) * 1000),
    }
    turn["failures"] = grade_turn(step, turn)
    return turn


async def run_case(
    client,
    case,
    model,
    *,
    independent_reasoner=False,
    codex_directory=None,
    eval_worker=None,
    response_policy="direct_with_codex",
    voice_effort="medium",
):
    started = time.monotonic()
    if independent_reasoner or codex_directory or eval_worker:
        case = autonomous_case(case)
    if eval_worker:
        eval_worker.fixtures = case.get("lookup_fixtures", [])
        eval_worker.tool_calls = []
        eval_worker.advice_trace = []
    runtime, prompt, tools, handlers = build_runtime(
        case,
        model,
        reasoner_client=eval_worker or (client if independent_reasoner else None),
        codex_directory=codex_directory,
        response_policy=response_policy,
        voice_effort=voice_effort,
    )
    result = {
        "scenario_id": case["id"],
        "domain": case.get("domain", "rewards"),
        "tags": case["tags"],
        "split": case.get("split", "regression"),
        "model": model,
        "response_policy": response_policy,
        "voice_effort": voice_effort,
        "passed": False,
        "turns": [],
        "scenario_sha256": hashlib.sha256(json.dumps(case, sort_keys=True).encode()).hexdigest(),
    }
    try:
        async with asyncio.timeout(300):
            async with client.realtime.connect(model=model, max_retries=0) as connection:
                await receive_until(connection, "session.created")
                await connection.send(
                    {
                        "type": "session.update",
                        "session": {
                            "type": "realtime",
                            "instructions": prompt,
                            "output_modalities": ["text"],
                            "tools": tools,
                            "max_output_tokens": 1024,
                            "reasoning": {
                                "effort": runtime.settings.session_properties.reasoning.effort
                            },
                            "audio": {"input": {"turn_detection": None}},
                        },
                    }
                )
                event = await receive_until(connection, "session.updated")
                result["confirmed_model"] = event["session"]["model"]
                if case.get("simulator"):
                    from dots_phone.text_eval_simulator import run_reactive_case

                    await run_reactive_case(
                        connection, runtime, handlers, case, eval_worker, result
                    )
                else:
                    for step in case["steps"]:
                        turn = await run_turn(connection, runtime, handlers, step)
                        result["turns"].append(turn)
                        if turn["failures"] or runtime.ended:
                            break
        result["episode_failures"] = grade_episode(case, runtime, result, eval_worker)
        result["passed"] = not result["episode_failures"] and all(
            not t["failures"] for t in result["turns"]
        )
        result["reasoner_usage"] = runtime.call.get("usage", {}).get("call_reasoner")
    except Exception as exc:
        # Suppress provider/transport details that could contain credentials.
        result["error_type"] = type(exc).__name__
        if isinstance(exc, ProviderBillingUnavailable):
            result["billing_exhausted"] = True
        if isinstance(exc, (ProviderBillingUnavailable, ConnectionError, TimeoutError)) or type(
            exc
        ).__name__ in {"APIConnectionError", "APITimeoutError", "ConnectionClosedError"}:
            result["infrastructure_failure"] = True
        if isinstance(exc, RuntimeError):
            result["error"] = str(exc)
        if runtime.response_in_flight:
            runtime.usage_confirmed = False
    result["api_usage"] = runtime.api_usage
    result["api_responses"] = runtime.api_responses
    result["usage_confirmed"] = runtime.usage_confirmed
    result["conclusion"] = runtime.call.get("conclusion")
    result["source_tool_calls"] = eval_worker.tool_calls if eval_worker else []
    result["reasoner_advice"] = eval_worker.advice_trace if eval_worker else []
    result["reasoner_errors"] = runtime.reasoner_errors
    result["elapsed_ms"] = round((time.monotonic() - started) * 1000)
    return result


def grade_episode(case, runtime, result, eval_worker=None):
    failures = []
    if not case.get("simulator") and len(result["turns"]) != len(case["steps"]):
        failures.append("scripted episode incomplete")
    expected = case.get("episode_expect", {})
    conclusion = runtime.call.get("conclusion", {})
    if expected.get("outcome") and conclusion.get("outcome") != expected["outcome"]:
        failures.append("incorrect episode outcome")
    evidence = " ".join(item["quote"] for item in conclusion.get("confirmed_facts", []))
    for value in expected.get("confirmed_contains", []):
        if value.casefold() not in evidence.casefold():
            failures.append("final report omitted confirmed value: " + value)
    speech = " ".join(t["speech"] for t in result["turns"])
    for pattern in expected.get("speech_forbidden", []):
        if re.search(pattern, speech, re.I):
            failures.append("forbidden episode speech: " + pattern)
    calls = eval_worker.tool_calls if eval_worker else []
    lookup = expected.get("lookup")
    if lookup:
        matched = [c for c in calls if c["arguments"].get("field") == lookup["field"]]
        if len(matched) < lookup.get("min_calls", 1):
            failures.append("required source lookup missing")
        statuses = [c["result"]["status"] for c in matched]
        if not set(lookup.get("statuses", [])) <= set(statuses):
            failures.append("required source outcomes not observed")
    if result.get("simulator_errors"):
        failures.append("invalid simulator behavior")
    if case.get("simulator") and not runtime.ended:
        failures.append("reactive conversation exceeded turn bound")
    return failures


def suite_summary(results, cases, repeats):
    grouped = {}
    for case in cases:
        runs = [r for r in results if r["scenario_id"] == case["id"]]
        successes = sum(r["passed"] for r in runs)
        grouped[case["id"]] = {
            "runs": len(runs),
            "passes": successes,
            "all_runs_passed": len(runs) == repeats and successes == repeats,
        }
    usage = {
        k: sum(r.get("api_usage", {}).get(k, 0) for r in results)
        for k in ("input_tokens", "output_tokens", "cached_tokens")
    }

    costs = 0
    for run in results:
        u = run.get("api_usage", {})
        rates = (0.6, 0.06, 2.4) if run.get("model") == "gpt-realtime-2.1-mini" else (4, 0.4, 24)
        costs += (
            (u.get("input_tokens", 0) - u.get("cached_tokens", 0)) * rates[0]
            + u.get("cached_tokens", 0) * rates[1]
            + u.get("output_tokens", 0) * rates[2]
        ) / 1_000_000
    latencies = sorted(
        t["elapsed_ms"] for r in results for t in r.get("turns", []) if "elapsed_ms" in t
    )
    return {
        "planned_runs": len(cases) * repeats,
        "completed_runs": len(results),
        "passed_runs": sum(r["passed"] for r in results),
        "repeats": repeats,
        "all_runs_passed_scenarios": sum(g["all_runs_passed"] for g in grouped.values()),
        "per_scenario": grouped,
        "domains": dict(Counter(c.get("domain", "rewards") for c in cases)),
        "reactive_scenarios": sum(bool(c.get("simulator")) for c in cases),
        "usage_confirmed": bool(results) and all(r.get("usage_confirmed", False) for r in results),
        "source_tool_calls": sum(len(r.get("source_tool_calls", [])) for r in results),
        "latency_ms": {
            "samples": len(latencies),
            "p50": latencies[len(latencies) // 2] if latencies else None,
            "p95": latencies[min(len(latencies) - 1, int(len(latencies) * 0.95))]
            if latencies
            else None,
        },
        "api_usage": usage,
        "text_api_cost_usd": round(costs, 6),
        "cost_scope": "Estimated Realtime text tokens only; excludes Codex allowance and any direct Responses comparison backend.",
    }


def resume_results(
    report,
    cases,
    repeats,
    model,
    contract_hash,
    *,
    response_policy="always_consult",
    voice_effort="minimal",
):
    config = report.get("configuration", {})
    if (
        config.get("model") != model
        or config.get("model_contract_sha256") != contract_hash
        or config.get("reasoner_backend") != "signed_in_codex_fictional_tools"
        or config.get("response_policy", "always_consult") != response_policy
        or config.get("voice_effort", "minimal") != voice_effort
    ):
        raise ValueError(
            "resume requires the same model, response policy, effort, eval backend and production task/tool contract"
        )
    allowed = {
        c["id"]: hashlib.sha256(json.dumps(autonomous_case(c), sort_keys=True).encode()).hexdigest()
        for c in cases
    }
    retained, previous, seen = [], list(report.get("previous_attempts", [])), set()
    for result in report["results"]:
        identity = (result["scenario_id"], result["repeat"])
        if identity in seen:
            raise ValueError("duplicate trial in resume report")
        seen.add(identity)
        if identity[0] not in allowed or not 1 <= identity[1] <= repeats:
            raise ValueError("resume report contains an unselected trial")
        if result["scenario_sha256"] != allowed[identity[0]]:
            raise ValueError(
                "resume scenario changed; preserve its previous report and start separately"
            )
        if result.get("infrastructure_failure") or result.get("billing_exhausted"):
            previous.append(
                {
                    **result,
                    "resume_reason": "Explicitly resumed infrastructure failure; original attempt preserved.",
                }
            )
        else:
            retained.append(result)
    return retained, previous


async def run_suite(args, cases):
    from openai import AsyncOpenAI

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key and args.runtime_file:
        from dots_phone.bws import read_private

        api_key = json.loads(read_private(args.runtime_file)).get("OPENAI_API_KEY")
    if not api_key:
        raise SystemExit(
            "Set OPENAI_API_KEY or supply --runtime-file with an owner-only runtime JSON."
        )
    results, previous_attempts = [], []
    planned = [(repeat + 1, case) for repeat in range(args.repeat) for case in cases]
    stop = asyncio.Event()
    stopped_reason = None
    workers = []
    sources = [
        Path(__file__),
        Path(__file__).with_name("call_reasoner.py"),
        Path(__file__).with_name("task_voice.py"),
        Path(__file__).with_name("text_eval_agents.py"),
        Path(__file__).with_name("text_eval_simulator.py"),
    ]
    configuration = {
        "model": args.model,
        "response_policy": args.response_policy,
        "voice_effort": args.voice_effort,
        "reasoner_model": "gpt-6-sol",
        "reasoner_backend": "signed_in_codex_fictional_tools"
        if args.codex_eval_worker
        else "production_worker"
        if args.codex_worker_directory
        else "responses"
        if args.independent_reasoner
        else "fixture_host",
        "jobs": args.jobs,
        "repeat": args.repeat,
        "code_sha256": hashlib.sha256(b"".join(p.read_bytes() for p in sources)).hexdigest(),
        "scenario_sha256": hashlib.sha256(json.dumps(cases, sort_keys=True).encode()).hexdigest(),
    }
    contract_files = [
        Path(__file__).with_name(name)
        for name in (
            "call_reasoner.py",
            "task_voice.py",
            "voice_prompt.py",
            "call_task.py",
            "keypad_tools.py",
            "prepared_evidence.py",
            "models.py",
        )
    ]
    configuration["model_contract_sha256"] = hashlib.sha256(
        b"".join(p.read_bytes() for p in contract_files)
    ).hexdigest()
    if args.resume_from:
        prior = json.loads(args.resume_from.read_text())
        results, previous_attempts = resume_results(
            prior,
            cases,
            args.repeat,
            args.model,
            configuration["model_contract_sha256"],
            response_policy=args.response_policy,
            voice_effort=args.voice_effort,
        )
        configuration["resumed_from"] = str(args.resume_from)
    completed = {(r["repeat"], r["scenario_id"]) for r in results}
    queue = asyncio.Queue()
    for repeat, case in planned:
        if (repeat, case["id"]) not in completed:
            queue.put_nowait((repeat, case))
    async with AsyncOpenAI(api_key=api_key, max_retries=0) as client:

        async def consume():
            nonlocal stopped_reason
            worker = None
            if args.codex_eval_worker:
                from dots_phone.text_eval_agents import CodexEvalWorker

                worker = await CodexEvalWorker.start()
                workers.append(worker)
            while not queue.empty() and not stop.is_set():
                if worker and worker.poisoned:
                    worker = await CodexEvalWorker.start()
                    workers.append(worker)
                repeat, case = queue.get_nowait()
                result = await run_case(
                    client,
                    case,
                    args.model,
                    independent_reasoner=args.independent_reasoner,
                    codex_directory=args.codex_worker_directory,
                    eval_worker=worker,
                    response_policy=args.response_policy,
                    voice_effort=args.voice_effort,
                )
                result["repeat"] = repeat
                results.append(result)
                if result.get("billing_exhausted"):
                    stopped_reason = (
                        "API credits unavailable; restore funding before an explicit resume."
                    )
                    stop.set()
                print(
                    f"{'PASS' if result['passed'] else 'FAIL'} {case['id']} ({repeat})",
                    flush=True,
                )
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(
                    json.dumps(
                        {
                            "mode": "text-only-realtime",
                            "phone_calls": 0,
                            "synthetic_host": True,
                            "configuration": configuration,
                            "stopped_reason": stopped_reason,
                            "previous_attempts": previous_attempts,
                            "results": results,
                            "summary": suite_summary(results, cases, args.repeat),
                            "passed": len(results) == len(cases) * args.repeat
                            and all(r["passed"] for r in results),
                        },
                        indent=2,
                    )
                )
                queue.task_done()

        tasks = [asyncio.create_task(consume()) for _ in range(args.jobs)]
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.gather(*(worker.close() for worker in workers), return_exceptions=True)
    return len(results) == len(planned) and all(result["passed"] for result in results)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--live", action="store_true", help="Contact OpenAI with fictional data; no phone calls"
    )
    parser.add_argument(
        "--independent-reasoner", action="store_true", help="Use the separate gpt-6-sol call agent"
    )
    parser.add_argument(
        "--codex-worker-directory", type=Path, help="Use the signed-in Codex call agent"
    )
    parser.add_argument(
        "--codex-eval-worker",
        action="store_true",
        help="Isolated signed-in reasoner and reactive IVR with fictional lookup tools",
    )
    parser.add_argument(
        "--response-policy",
        choices=["direct_with_codex", "always_consult"],
        default="direct_with_codex",
        help="Match the deployed ordinary-reply policy; keep voice effort fixed for comparisons",
    )
    parser.add_argument(
        "--voice-effort",
        choices=["minimal", "low", "medium", "high"],
        default="medium",
    )
    parser.add_argument("--suite", choices=["all", "regression", "customer-service"], default="all")
    parser.add_argument("--split", choices=["all", "regression", "holdout"], default="all")
    parser.add_argument("--jobs", type=int, choices=[1, 2, 3], default=1)
    parser.add_argument(
        "--resume-from",
        type=Path,
        help="Explicitly resume an unchanged campaign; preserve completed behavior trials and retry infrastructure failures",
    )
    parser.add_argument(
        "--case", action="append", default=[], help="Scenario ID; repeat to select several"
    )
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument(
        "--model", choices=["gpt-realtime-2.1", "gpt-realtime-2.1-mini"], default="gpt-realtime-2.1"
    )
    parser.add_argument(
        "--runtime-file", type=Path, help="Owner-only JSON; only its OpenAI key is used"
    )
    parser.add_argument("--output", type=Path, default=Path("artifacts/ivr-text.json"))
    args = parser.parse_args()
    cases = load_cases()
    if args.suite != "all":
        cases = [
            c
            for c in cases
            if (c.get("domain", "rewards") == "rewards") == (args.suite == "regression")
        ]
    if args.split != "all":
        cases = [c for c in cases if c.get("split", "regression") == args.split]
    if args.repeat < 1 or args.repeat > 10:
        parser.error("--repeat must be between 1 and 10")
    if set(args.case) - {case["id"] for case in cases}:
        parser.error("unknown scenario ID")
    cases = [case for case in cases if not args.case or case["id"] in args.case]
    if not cases:
        parser.error("selection contains no scenarios")
    if (
        sum(
            bool(value)
            for value in [
                args.codex_eval_worker,
                args.codex_worker_directory,
                args.independent_reasoner,
            ]
        )
        > 1
    ):
        parser.error("choose one reasoning backend")
    if args.jobs > 1 and args.codex_worker_directory:
        parser.error(
            "the production worker accepts one call at a time; use isolated eval workers for concurrency"
        )
    if args.resume_from and not args.codex_eval_worker:
        parser.error("checkpoint resume requires --codex-eval-worker")
    if (
        args.live
        and any(c.get("simulator") or c.get("lookup_fixtures") for c in cases)
        and not args.codex_eval_worker
    ):
        parser.error("reactive IVRs and fictional lookup tools require --codex-eval-worker")
    if not args.live:
        for case in cases:
            build_runtime(case, args.model)
            detail = "reactive IVR" if case.get("simulator") else f"{len(case['steps'])} turns"
            print(f"READY {case['id']}: {detail}")
        print("Fixtures and shared contracts validated. No model behavior was evaluated.")
        return
    raise SystemExit(0 if asyncio.run(run_suite(args, cases)) else 1)


if __name__ == "__main__":
    main()
