import pytest

pytest.importorskip("pipecat")

from dots_phone.realtime_audio_smoke import (
    acoustic_turn_latencies,
    balance_clarification_requested,
    check_budget,
    fixture_result_verified,
    response_cost,
)


@pytest.mark.parametrize(
    "text",
    [
        "Could you please repeat the available balance?",
        "Please confirm the points amount.",
        "Please read the current available balance again.",
        "Was that twelve thousand three hundred forty points?",
        "Did you say 12,340 points or 12,300 points?",
        "Could you clarify the amount?",
    ],
)
def test_balance_clarification_fixture_only_answers_actual_balance_requests(text):
    assert balance_clarification_requested(text)


@pytest.mark.parametrize(
    "text",
    [
        "Thanks, I have your balance.",
        "I will confirm the available balance.",
        "Let me repeat the points amount.",
        "Could you repeat your email address?",
        "Is there anything else I can do?",
        "Please don't repeat the balance.",
        "Your available balance is 12,340 points.",
        "Was that your name?",
    ],
)
def test_balance_clarification_fixture_does_not_respond_to_narration(text):
    assert not balance_clarification_requested(text)


def test_fixture_transcript_fault_preserves_raw_provider_text_and_recipient_identity(tmp_path):
    from dots_phone.realtime_audio_smoke import (
        CLARIFICATION_LINE,
        INCORRECT_BALANCE_TRANSCRIPT,
        LINES,
        Trace,
    )

    trace = Trace(tmp_path, inject_balance_transcript_mismatch=True)
    trace.current_fixture_turn = 4  # Initial transcription may arrive late.
    trace.input_turn_by_item = {"initial-balance": 3, "clarification": 4}
    original = {
        "type": "conversation.item.input_audio_transcription.completed",
        "item_id": "initial-balance",
        "transcript": LINES[3],
        "usage": {"seconds": 10},
    }
    injected = trace.observed_input_transcription(original)
    assert injected["transcript"] == INCORRECT_BALANCE_TRANSCRIPT
    assert injected["item_id"] == original["item_id"]
    assert original["transcript"] == LINES[3]
    assert trace.events[0]["kind"] == "provider_input_transcript"
    assert trace.events[0]["text"] == LINES[3]
    assert trace.events[0]["raw_event"] == original
    correction = {**original, "item_id": "clarification", "transcript": "provider text retained"}
    assert trace.observed_input_transcription(correction)["transcript"] == CLARIFICATION_LINE
    assert correction["transcript"] == "provider text retained"
    assert trace.transcription_fault_items == {3: "initial-balance", 4: "clarification"}
    assert not trace.abort.is_set()


@pytest.mark.parametrize("problem", ["missing_binding", "second_item"])
def test_fixture_fault_fails_closed_on_missing_or_split_recipient_identity(tmp_path, problem):
    from dots_phone.realtime_audio_smoke import Trace

    trace = Trace(tmp_path, inject_balance_transcript_mismatch=True)
    trace.current_fixture_turn = 3
    if problem == "second_item":
        trace.input_turn_by_item = {"new-item": 3}
        trace.transcription_fault_items = {3: "first-item"}
    event = {"item_id": "new-item", "transcript": "unchanged"}
    assert trace.observed_input_transcription(event) is event
    assert trace.abort.is_set()
    assert trace.failure.startswith("transcription_fault_")


def test_normal_fixture_transcription_is_never_overridden(tmp_path):
    from dots_phone.realtime_audio_smoke import Trace

    trace = Trace(tmp_path)
    trace.current_fixture_turn = 3
    event = {"item_id": "balance", "transcript": "unchanged"}
    assert trace.observed_input_transcription(event) is event
    assert not trace.abort.is_set()
    assert not trace.events[-1]["synthetic_override"]


def test_prepare_records_opt_in_modes_without_relaxing_bounds(tmp_path):
    import json
    import wave

    from dots_phone.realtime_audio_smoke import prepare

    for index in range(5):
        with wave.open(str(tmp_path / f"ivr-{index}.wav"), "wb") as stream:
            stream.setnchannels(1)
            stream.setsampwidth(2)
            stream.setframerate(24000)
            stream.writeframes(b"\0" * 960)
    assert len(prepare(tmp_path)) == 4
    assert (
        len(
            prepare(
                tmp_path,
                allow_balance_clarification=True,
                inject_balance_transcript_mismatch=True,
            )
        )
        == 5
    )
    scope = json.loads((tmp_path / "scope.json").read_text())
    assert scope["max_recipient_clarifications"] == 1
    assert scope["inject_balance_transcript_mismatch"]
    assert (scope["max_seconds"], scope["max_responses"], scope["budget_usd"]) == (90, 10, 1.0)
    assert scope["phone_calls"] == scope["real_account_queries"] == 0
    with pytest.raises(ValueError, match="requires"):
        prepare(tmp_path, inject_balance_transcript_mismatch=True)
    (tmp_path / "attempt.json").write_text("{}")
    original_scope = (tmp_path / "scope.json").read_bytes()
    with pytest.raises(FileExistsError, match="existing audio attempt"):
        prepare(tmp_path)
    assert (tmp_path / "scope.json").read_bytes() == original_scope


@pytest.mark.parametrize("value", ["12,340", "12340", "twelve thousand three hundred forty"])
def test_audio_fixture_result_checks_actual_balance(value):
    text = f"Your available rewards balance is {value} points."
    assert fixture_result_verified(
        {"outcome": "completed", "confirmed_facts": [{"text": text, "quote": text}]}
    )


@pytest.mark.parametrize(
    ("reported", "quoted"),
    [
        ("12,300", "twelve thousand three hundred"),
        ("12,300", "12,340"),
        ("123400", "123400"),
        ("", "12,340"),
    ],
)
def test_consistent_but_mistranscribed_result_cannot_pass_audio_fixture(reported, quoted):
    assert not fixture_result_verified(
        {
            "outcome": "completed",
            "confirmed_facts": [
                {"text": f"Balance: {reported} points.", "quote": f"Balance: {quoted} points."}
            ],
        }
    )


@pytest.mark.parametrize(
    "text",
    [
        "The available balance is not 12,340 points.",
        "Your pending rewards balance is 12,340 points. Available balance is 12,300 points.",
        "Your lifetime balance is 12,340 points.",
        "Your available rewards balance is 12,340 dollars.",
        "Your available rewards balance was 12,340 points.",
        "Your available rewards balance might be 12,340 points.",
        "Your available rewards balance is about 12,340 points.",
        "Your available rewards balance is 12,340 points. Your pending balance is 100 points.",
        "Your available rewards balance is 12,340 points. Your pending balance is one hundred points.",
        "Your available rewards balance is 12,340 points, which is incorrect.",
        "Your available rewards balance is 12,340.50 points.",
    ],
)
def test_balance_grader_rejects_wrong_subject_units_polarity_time_and_extra_claims(text):
    correct = "Your available rewards balance is 12,340 points."
    for reported, quoted in [(text, text), (text, correct), (correct, text)]:
        assert not fixture_result_verified(
            {"outcome": "completed", "confirmed_facts": [{"text": reported, "quote": quoted}]}
        )


def test_balance_grader_accepts_fixture_qualifier_but_rejects_separate_contradiction():
    from dots_phone.realtime_audio_smoke import LINES

    correct = {"text": "Available rewards balance: 12,340 points.", "quote": LINES[-1]}
    assert fixture_result_verified({"outcome": "completed", "confirmed_facts": [correct]})
    assert not fixture_result_verified(
        {
            "outcome": "completed",
            "confirmed_facts": [
                correct,
                {"text": "Available balance: 12,300 points.", "quote": LINES[-1]},
            ],
        }
    )


def test_played_pcm_latency_uses_voiced_end_and_keeps_turns_separate():
    from array import array

    # One hundred 20 ms frames; spoken input ends at 200 ms even though its
    # clip has trailing silence. Reply starts at 600 ms, then a silent turn.
    ivr = array("h", [0] * 48000)
    assistant = array("h", [0] * 48000)
    ivr[0:4800] = array("h", [1000] * 4800)
    assistant[14400:19200] = array("h", [1000] * 4800)
    ivr[24000:28800] = array("h", [1000] * 4800)
    result = acoustic_turn_latencies(
        ivr.tobytes(),
        assistant.tobytes(),
        [{"turn": 1, "audio_start_sample": 0}, {"turn": 2, "audio_start_sample": 24000}],
    )
    assert result["turns"][0]["recipient_voiced_end_ms"] == 200
    assert result["turns"][0]["first_audible_reply_gap_ms"] == 400
    assert result["turns"][1]["first_audible_reply_gap_ms"] is None
    assert not result["useful_answer_verified_from_audio"]


def test_played_pcm_latency_reports_overlap_instead_of_hiding_early_speech():
    from array import array

    ivr = array("h", [1000] * 4800 + [0] * 4800)
    assistant = array("h", [0] * 2400 + [1000] * 7200)
    result = acoustic_turn_latencies(
        ivr.tobytes(), assistant.tobytes(), [{"turn": 1, "audio_start_sample": 0}]
    )
    assert result["turns"][0]["first_audible_reply_gap_ms"] == -100


async def test_audio_provider_cannot_dial(service, tmp_path):
    from dots_phone.realtime_audio_smoke import Trace, audio_provider

    provider = audio_provider(service.settings, service, Trace(tmp_path))
    with pytest.raises(RuntimeError, match="no phone carrier"):
        await provider.dial({})


def test_audio_cost_accounts_for_modalities_and_cache():
    usage = {
        "input_tokens": 1000,
        "output_tokens": 100,
        "input_token_details": {
            "text_tokens": 600,
            "audio_tokens": 400,
            "cached_tokens_details": {"text_tokens": 500, "audio_tokens": 100},
        },
        "output_token_details": {"text_tokens": 20, "audio_tokens": 80},
    }
    assert response_cost(usage) == pytest.approx(0.01584)


def test_audio_budget_refuses_unknown_usage_and_reserves_headroom():
    with pytest.raises(ValueError, match="usage unavailable"):
        check_budget({"unreported": None}, 1)
    with pytest.raises(RuntimeError, match="budget"):
        check_budget({"expensive": {"input_tokens": 25000, "output_tokens": 0}}, 1)
    with pytest.raises(RuntimeError, match="response limit"):
        check_budget({}, 10)


def test_unknown_modalities_use_expensive_rate():
    assert response_cost({"input_tokens": 100, "output_tokens": 10}) == pytest.approx(0.00384)


async def test_persistent_eval_client_binds_preparation_and_turns_to_one_call(
    monkeypatch, tmp_path
):
    import json
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock

    from dots_phone.call_reasoner import REASONING_PROMPT, ReasoningAdvice
    from dots_phone.text_eval import fixture_call
    from dots_phone.text_eval_agents import CodexCallEvalWorker

    sent = []
    lifecycle = {"thread_id": "one-call-thread", "thread_creations": 1, "connected_tools": 0}

    async def read():
        if not sent:
            return b'{"ready":true,"connected_tools":0,"persistent_call_agent":true}\n'
        message = sent[-1]
        result = {"id": message["id"], "lifecycle": lifecycle}
        if message["operation"] == "prepare_call":
            result["prepared"] = True
        else:
            result.update(
                output={"decision": {"kind": "wait"}},
                tool_calls=[],
                tool_quote_verified=False,
            )
        return (json.dumps(result) + "\n").encode()

    process = SimpleNamespace(
        returncode=None,
        stdin=SimpleNamespace(write=lambda raw: sent.append(json.loads(raw)), drain=AsyncMock()),
        stdout=SimpleNamespace(readline=read),
        terminate=Mock(),
        wait=AsyncMock(return_value=0),
    )
    monkeypatch.setattr("asyncio.create_subprocess_exec", AsyncMock(return_value=process))
    call = fixture_call({})
    call["brief"]["actions"].remove("read_call_sources")
    worker = await CodexCallEvalWorker.start(call, tmp_path)
    for _ in range(2):
        await worker.parse(
            model="gpt-6-sol",
            instructions=REASONING_PROMPT,
            input=json.dumps({"brief": call["brief"]}),
            text_format=ReasoningAdvice,
        )
    assert [message["operation"] for message in sent] == ["prepare_call", "reason", "reason"]
    assert all(message["call_scope"]["call_id"] == call["call_id"] for message in sent)
    assert len({entry["thread_id"] for entry in worker.lifecycle_trace}) == 1
    assert all(message["schema"] == sent[0]["schema"] for message in sent)
    assert all(entry["thread_creations"] == 1 for entry in worker.lifecycle_trace)
    await worker.close()
    process.terminate.assert_called_once()


@pytest.mark.parametrize("policy", ["direct_with_codex", "always_consult"])
async def test_audio_harness_keeps_reporting_and_consultation_with_matching_local_controls(
    service, brief, tmp_path, policy
):
    from pipecat.adapters.services.open_ai_realtime_adapter import OpenAIRealtimeLLMAdapter

    from dots_phone.realtime_audio_smoke import Trace, build_voice
    from tests.test_call_reasoner import prepare

    service.settings.local_reasoner_response_policy = policy
    prepared = prepare(service, brief, tmp_path)
    call = service.call("owner", prepared["call_id"])
    llm, context, prompt, _ = build_voice(service, call, "unused-offline", Trace(tmp_path))
    names = {t["name"] for t in OpenAIRealtimeLLMAdapter().from_standard_tools(context.tools)}
    assert names >= {"consult_call_reasoner", "wait_for_recipient", "finish_call"}
    assert ("press_phone_key" in names) == (policy == "direct_with_codex")
    assert "Dots call preparation" in prompt
    assert llm._settings.system_instruction == prompt
    assert service.provider.dials == []
    # The no-dial voice must retain the production factory's task profile.
    assert llm._settings.session_properties.max_output_tokens == 1024


@pytest.mark.parametrize(
    ("completed", "speaking", "status", "usage", "expected_abort"),
    [
        (True, True, "cancelled", {"input_tokens": 1, "output_tokens": 1}, False),
        (False, True, "cancelled", {"input_tokens": 1, "output_tokens": 1}, True),
        (True, False, "cancelled", {"input_tokens": 1, "output_tokens": 1}, True),
        (True, True, "cancelled", {"input_tokens": 2, "output_tokens": 1}, True),
        (True, True, "cancelled", None, True),
        (True, True, "failed", {"input_tokens": 1, "output_tokens": 1}, True),
        (True, True, "incomplete", {"input_tokens": 1, "output_tokens": 1}, True),
    ],
)
async def test_audio_harness_accepts_only_metered_completed_then_interrupted_duplicate(
    service, brief, tmp_path, completed, speaking, status, usage, expected_abort
):
    from dots_phone.realtime_audio_smoke import Trace, build_voice
    from tests.test_call_reasoner import prepare

    prepared = prepare(service, brief, tmp_path)
    call = service.call("owner", prepared["call_id"])
    trace = Trace(tmp_path)
    llm, _, _, _ = build_voice(service, call, "unused-offline", trace)
    # The regression follows the real wire ordering: created, completed, new
    # recipient speech, then cancellation of that same completed response.
    llm.responses_created.add("fixture-response")
    llm.active_responses.add("fixture-response")
    if completed:
        await llm.observe(
            {
                "type": "response.done",
                "response": {
                    "id": "fixture-response",
                    "status": "completed",
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                },
            }
        )
        assert not trace.abort.is_set()
    if speaking:
        await llm.observe({"type": "input_audio_buffer.speech_started"})
    await llm.observe(
        {
            "type": "response.done",
            "response": {
                "id": "fixture-response",
                "status": status,
                "status_details": {"reason": "max_output_tokens"}
                if status == "incomplete"
                else None,
                "usage": usage,
            },
        }
    )
    assert trace.abort.is_set() == expected_abort
    if status == "incomplete":
        assert trace.events[-1]["status_details"] == {"reason": "max_output_tokens"}
    if not expected_abort:
        assert trace.events[-1]["status"] == "cancelled"
        assert trace.events[-1]["repeated_completed_cancellation"]
        assert len(llm.response_usage) == 1
        assert service.call("owner", call["call_id"])["usage"]["realtime_usage_confirmed"]


async def test_audio_harness_does_not_score_suppressed_commentary_as_spoken_answer(
    service, brief, tmp_path
):
    from dots_phone.realtime_audio_smoke import Trace, build_voice
    from tests.test_call_reasoner import prepare

    prepared = prepare(service, brief, tmp_path)
    call = service.call("owner", prepared["call_id"])
    trace = Trace(tmp_path)
    llm, _, _, _ = build_voice(service, call, "unused-offline", trace)
    llm.speech_phases["silent-commentary"] = "commentary"
    await llm.observe(
        {
            "type": "response.output_audio_transcript.done",
            "response_id": "fixture-response",
            "item_id": "silent-commentary",
            "transcript": "Yes.",
        }
    )
    assert trace.captions == []
    assert trace.events[-1]["kind"] == "suppressed_output_transcript"
    llm.speech_phases["spoken-answer"] = "final_answer"
    await llm.observe(
        {
            "type": "response.output_audio_transcript.done",
            "response_id": "fixture-response",
            "item_id": "spoken-answer",
            "transcript": "Yes.",
        }
    )
    assert trace.captions == ["Yes."]


async def test_audio_harness_fault_reaches_supervisor_without_changing_raw_event(
    service, brief, tmp_path
):
    from dots_phone.realtime_audio_smoke import (
        INCORRECT_BALANCE_TRANSCRIPT,
        LINES,
        Trace,
        build_voice,
    )
    from tests.test_call_reasoner import prepare

    prepared = prepare(service, brief, tmp_path)
    call = service.call("owner", prepared["call_id"])
    trace = Trace(tmp_path, inject_balance_transcript_mismatch=True)
    llm, _, _, _ = build_voice(service, call, "unused-offline", trace)
    trace.current_fixture_turn = 3
    await llm.observe(
        {"type": "input_audio_buffer.speech_started", "item_id": "real-provider-item"}
    )
    original = {
        "type": "conversation.item.input_audio_transcription.completed",
        "item_id": "real-provider-item",
        "content_index": 0,
        "transcript": LINES[3],
    }
    await llm.observe(original)
    assert llm.dialog[-1]["text"] == INCORRECT_BALANCE_TRANSCRIPT
    assert original["transcript"] == LINES[3]
    raw = next(event for event in trace.events if event["kind"] == "provider_input_transcript")
    assert raw["raw_event"] == original
    assert trace.transcription_fault_items == {3: "real-provider-item"}
