"""Background-audio policy uses the existing silent tool, not a VAD claim."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from dots_phone.call_task import TASK_PROMPT, task_instructions, task_tools
from dots_phone.voice_prompt import BACKGROUND_AUDIO_POLICY, call_instructions


def test_shared_audio_policy_distinguishes_background_from_unclear_recipient(brief):
    call = {"brief": brief.model_dump(mode="json")}
    for delegated in (True, False):
        prompt = call_instructions(call, delegated_decisions=delegated)
        assert BACKGROUND_AUDIO_POLICY in prompt
        assert "not addressed to you" in prompt
        assert "completed request is clearly addressed to you but unintelligible" in prompt
        assert "Repeat an earlier answer when the recipient asks for it" in prompt
        assert "without reintroducing yourself or restarting the whole request" in prompt
        # General calls need not expose the task factory's wait tool.
        assert "wait_for_recipient" not in prompt
    assert "wait_for_recipient" in TASK_PROMPT
    assert "completed, clearly addressed but unintelligible request" in TASK_PROMPT


def test_real_phone_roleplay_uses_one_acknowledgment_then_ordinary_questions(brief):
    call = {"brief": brief.model_dump(mode="json")}
    call["brief"].update(synthetic=False, purpose="Role-play an airline booking conversation")
    prompt = task_instructions(call, live_sources=False)
    assert '"synthetic": false' in prompt
    assert "a role-play or simulation, including one over a real phone connection" in prompt
    assert "acknowledge that context once in the opening" in prompt
    assert "restrictions internal unless clarification is necessary" in prompt
    assert "ordinary questions, names, dates or prices fictional" in prompt


@pytest.mark.parametrize("reasoner", [False, True])
async def test_background_wait_contract_is_silent_and_needs_no_reasoner(reasoner):
    pytest.importorskip("pipecat")
    llm = SimpleNamespace(
        return_local_tool_result=AsyncMock(),
        request_supervision=AsyncMock(),
    )
    tools = task_tools(llm, source_tools=False, reasoner=reasoner)
    wait = next(tool for tool in tools if getattr(tool, "__name__", "") == "wait_for_recipient")
    assert all(
        term in wait.__doc__ for term in ("background noise", "music", "TV", "side conversations")
    )
    params = SimpleNamespace(tool_call_id="background_wait", result_callback=AsyncMock())
    await wait(params)
    llm.return_local_tool_result.assert_awaited_once_with(
        params, {"listening": True}, run_llm=False
    )
    llm.request_supervision.assert_not_awaited()
    params.result_callback.assert_not_awaited()
