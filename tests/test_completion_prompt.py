import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from dots_phone.call_reasoner import (
    VOICE_COMPLETION_PROMPT,
    VOICE_SOURCE_ANSWER_PROMPT,
    CallReasoner,
    ReasoningAdvice,
)


@pytest.mark.parametrize("policy", ["direct_with_codex", "always_consult"])
def test_both_voice_policies_receive_silent_completion_instruction_and_finish_tool(
    service, brief, tmp_path, policy
):
    pytest.importorskip("pipecat")
    from dots_phone.call_reasoner import reasoning_turn_policy
    from dots_phone.task_voice import create_task_voice
    from tests.test_call_reasoner import prepare

    service.settings.local_reasoner_response_policy = policy
    prepared = prepare(service, brief, tmp_path)
    call = service.call("owner", prepared["call_id"])
    llm, context, _ = create_task_voice(service, call, api_key="offline-fixture")
    assert VOICE_COMPLETION_PROMPT in llm._settings.system_instruction
    assert VOICE_SOURCE_ANSWER_PROMPT in llm._settings.system_instruction
    assert "call finish_call silently" in VOICE_COMPLETION_PROMPT
    assert "It closes the phone line immediately" in VOICE_COMPLETION_PROMPT
    assert "semantic review run AFTER hangup" in VOICE_COMPLETION_PROMPT
    assert "Never change a factual claim to fit" in VOICE_COMPLETION_PROMPT
    assert "BEFORE finishing" in VOICE_COMPLETION_PROMPT
    tools = llm.get_llm_adapter().get_llm_invocation_params(context)["tools"]
    allowed = reasoning_turn_policy(tools, response_policy=policy, consulted=True)
    assert allowed["tool_choice"] == "auto"
    assert "finish_call" in {tool["name"] for tool in allowed["tools"]}
    finish = next(tool for tool in tools if tool["name"] == "finish_call")
    assert "review run after hangup" in finish["description"]
    assert "unsupported claims remain unverified" in finish["description"]
    assembled = " ".join(llm._settings.system_instruction.split())
    assert "repair the references silently" not in assembled
    assert "If finish_call is rejected for citation or format" not in assembled
    assert "finish_call records the verified final result" not in assembled
    assert "thank the recipient briefly" not in assembled


async def test_summary_question_does_not_replace_reasoners_goal_and_finalization_contract(
    service, brief
):
    prepared = service.prepare("owner", brief)
    call = service.call("owner", prepared["call_id"])
    recipient = "The investigation remains open. The next update is due October 21."
    question = "Confirm the concise summary to speak and whether any further action is needed."
    advice = ReasoningAdvice(
        status="ready",
        next_step="Call finish_call silently with the status evidence.",
        answer="",
        supporting_quote="",
        report_supported=False,
    )
    client = SimpleNamespace(
        responses=SimpleNamespace(
            parse=AsyncMock(
                return_value=SimpleNamespace(output_parsed=advice, usage=None),
            )
        )
    )
    result = await CallReasoner(service, client).advise(
        call,
        [{"role": "user", "text": recipient}],
        question,
    )
    request = client.responses.parse.await_args.kwargs
    prompt = " ".join(request["instructions"].split())
    context = json.loads(request["input"])
    assert context["question"] == question
    assert context["recent_transcript"][0]["text"] == recipient
    assert "Before recommending a spoken summary or yielding, check the approved goal" in prompt
    assert "direct the voice model to call finish_call silently" in prompt
    assert "do not recommend a recap, waiting or another consultation instead" in prompt
    assert "Advice to finish is not report approval" in prompt
    assert result.report_supported is False and result.answer == "" and result.action is None
    assert service.call("owner", call["call_id"]).get("conclusion") is None


def test_routing_is_model_judgment_under_owner_limits_without_a_forced_escalation_script():
    from dots_phone.call_reasoner import DIRECT_COMMUNICATION_PROMPT, REASONING_PROMPT
    from dots_phone.call_task import TASK_CONVERSATION_PROMPT
    from dots_phone.voice_prompt import IVR_PREFERENCE

    for prompt in (DIRECT_COMMUNICATION_PROMPT, REASONING_PROMPT, TASK_CONVERSATION_PROMPT):
        words = " ".join(prompt.split())
        assert "stop conditions" in words
        assert "Do not try a third" not in words
        assert "both receive the wrong topic" not in words
        assert "one meaningfully different" not in words
    assert "There is no fixed" in DIRECT_COMMUNICATION_PROMPT
    assert "another perspective" in DIRECT_COMMUNICATION_PROMPT
    assert "You and the voice model can both reason" in REASONING_PROMPT
    assert "only when consistent with the owner's instructions" in IVR_PREFERENCE


@pytest.mark.parametrize("policy", ["direct_with_codex", "always_consult"])
async def test_runtime_verified_source_answer_is_speakable_without_starting_disclosure(
    service, brief, tmp_path, policy
):
    pytest.importorskip("pipecat")
    from dots_phone.task_voice import create_task_voice
    from tests.test_call_reasoner import prepare

    service.settings.local_reasoner_response_policy = policy
    prepared = prepare(service, brief, tmp_path, source_verification=True)
    call = service.call("owner", prepared["call_id"])
    assert "account_number" not in call["brief"]["disclosures"]
    call["usage"]["reasoner_preparation"] = {
        "source_capabilities": {
            "enabled": True,
            "selection": "explicit",
            "connector_ids": ["fictional-records"],
            "tool_names": ["fictional-records.read"],
        }
    }
    call["retrieved_sources"] = {
        "record": {
            "text": "Account number: 0009918",
            "revision": call["revision"],
            "brief_hash": call["brief_hash"],
        }
    }
    service.save(call)
    advice = ReasoningAdvice(
        status="ready",
        next_step="Say the account number to the recipient.",
        answer="Zero zero zero nine nine one eight.",
        supporting_quote="0009918",
        report_supported=False,
    )
    client = SimpleNamespace(
        responses=SimpleNamespace(
            parse=AsyncMock(return_value=SimpleNamespace(output_parsed=advice, usage=None))
        )
    )
    result = await CallReasoner(service, client).advise(
        call,
        [{"role": "user", "text": "What is your account number? Please say it."}],
        "Find the account number for spoken verification.",
    )
    assert result.status == "ready" and result.answer == advice.answer and result.action is None
    _, _, prompt = create_task_voice(service, call, api_key="offline-fixture")
    instructions = " ".join(prompt.split())
    assert "status ready with a nonempty answer for the current question" in instructions
    assert (
        "retrieved account or reference number absent from the starting disclosures" in instructions
    )
    assert "Do not refuse it because the briefing lists it as missing" in instructions
    assert "Unknown status or an empty answer does not establish the missing fact" in instructions
    assert "Never provide passwords, PINs, security answers or one-time codes" in instructions
    assert not service.provider.dials


def test_report_feedback_preserves_evidence_and_distinguishes_format_from_fact_conflicts():
    from dots_phone.call_task import report_feedback

    words = "Your available rewards balance is 12,340 points, not pending points."
    feedback = report_feedback([{"role": "user", "text": words}])
    assert feedback["result_recorded"] is False
    assert feedback["recipient_evidence"][0]["text"] == words
    assert "preserving factual claims" in feedback["guidance"]
    assert "Transcription and native interpretation can both be wrong" in feedback["guidance"]
    assert "ask the recipient to clarify" in feedback["guidance"]
    assert "An unrelated later turn is not clarification" in feedback["guidance"]
    assert len(json.dumps(feedback, ensure_ascii=False)) <= 1450


async def test_material_report_review_requests_clarification_without_a_replacement_fact(
    service, brief
):
    prepared = service.prepare("owner", brief)
    call = service.call("owner", prepared["call_id"])
    report = {
        "outcome": "completed",
        "evidence": [
            {
                "criterion_id": "balance",
                "text": "The available balance is 12,340 points.",
                "quote": "The available balance is 12,300 points.",
            }
        ],
        "unresolved_questions": [],
    }
    advice = ReasoningAdvice(
        status="unknown",
        next_step="Ask the recipient to confirm the exact available points balance.",
        answer="",
        supporting_quote="",
        report_supported=False,
    )
    client = SimpleNamespace(
        responses=SimpleNamespace(
            parse=AsyncMock(
                return_value=SimpleNamespace(output_parsed=advice, usage=None),
            )
        )
    )
    result = await CallReasoner(service, client).advise(
        call,
        [{"role": "user", "text": report["evidence"][0]["quote"]}],
        "Review the proposed final outcome.",
        report=report,
    )
    request = client.responses.parse.await_args.kwargs
    assert json.loads(request["input"])["review_report"] == report
    prompt = " ".join(request["instructions"].split())
    assert "For formatting-only rejection, return status ready, report_supported false" in prompt
    assert "For material disagreement, return status unknown, report_supported false" in prompt
    assert "compare the current report with its original rejected report and conflict" in prompt
    assert "Approval requires status ready." in prompt
    assert "When conflict is true or factual content has changed" in prompt
    assert "equivalent citation wording can be repaired using existing evidence" in prompt
    assert "a genuinely later recipient turn that addresses the disputed fact" in prompt
    assert (
        "An unrelated later turn, repeated transcript entry or the assistant's own statement is not resolution"
        in prompt
    )
    assert result.status == "unknown" and result.answer == "" and result.action is None
    assert not result.report_supported and not service.call("owner", call["call_id"]).get(
        "conclusion"
    )
