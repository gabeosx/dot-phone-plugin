import json

import pytest

pytest.importorskip("pipecat")

from dots_phone.text_eval import build_runtime, grade_turn, load_cases, run_turn, suite_summary


def test_fixture_matrix_covers_all_question_orders_and_keeps_grading_data_out_of_prompt():
    cases = load_cases()
    assert len([case for case in cases if "question_order" in case["tags"]]) == 6
    assert {"keypad", "conversational", "unknown", "source_failure", "human_fallback", "hold"} <= {
        tag for case in cases for tag in case["tags"]
    }
    case = next(case for case in cases if case["id"] == "missing_fact_lookup")
    runtime, prompt, tools, _ = build_runtime(case)
    assert "Cambridge" not in prompt
    assert "fictional private lunch preference" not in prompt
    assert "expect" not in prompt
    assert {tool["name"] for tool in tools} >= {
        "press_phone_key",
        "enter_verification_fact",
        "request_call_information",
        "finish_call",
    }
    assert runtime.digits == []


def test_always_consult_caller_has_no_key_selection_tools_or_real_account_read_scope(tmp_path):
    runtime, prompt, tools, _ = build_runtime(
        load_cases()[0], codex_directory=tmp_path, response_policy="always_consult"
    )
    names = {tool["name"] for tool in tools}
    assert "consult_call_reasoner" in names
    assert not names & {"press_phone_key", "enter_verification_fact", "request_call_information"}
    assert "read_call_sources" not in runtime.call["brief"]["actions"]
    assert "Dots only prepared" in prompt


def test_autonomous_source_capability_is_reported_even_without_direct_source_tools():
    from types import SimpleNamespace

    worker = SimpleNamespace(synthetic_tools=True)
    case = next(c for c in load_cases() if c["id"] == "utility_last_payment")
    _, prompt, tools, _ = build_runtime(case, reasoner_client=worker)
    assert '"source_tools_enabled": true' in prompt
    assert '"direct_source_tools_enabled": false' in prompt
    assert "before saying a requested fact is unavailable" in " ".join(prompt.casefold().split())
    assert "request_call_information" not in {t["name"] for t in tools}


def test_grader_rejects_wrong_digits_premature_completion_and_fabricated_identifier():
    step = {"expect": {"silent": True, "digits": ["00420420#"], "ended": False}}
    turn = {"speech": "I will press it", "tools": [], "digits": ["420420"], "ended": True}
    assert len(grade_turn(step, turn)) == 3
    assert (
        grade_turn(
            {"expect": {"spoken_value": "00420420"}},
            {"speech": "zero zero four two zero four two zero", "tools": [], "ended": False},
        )
        == []
    )
    assert grade_turn(
        {"expect": {"spoken_value": "00420420"}}, {"speech": "420420", "tools": [], "ended": False}
    )


def test_source_error_is_an_observed_lookup_but_not_a_successful_keypress():
    turn = {
        "speech": "",
        "tools": [
            {
                "name": "request_call_information",
                "arguments": {},
                "result": {"error": "unavailable"},
            }
        ],
        "ended": False,
    }
    assert (
        grade_turn(
            {"expect": {"tools": [{"name": "request_call_information", "allow_error": True}]}}, turn
        )
        == []
    )
    assert grade_turn({"expect": {"tools": [{"name": "request_call_information"}]}}, turn)


def test_unknown_birth_date_grader_does_not_confuse_auxiliary_may_with_a_date():
    from dots_phone.text_eval import autonomous_case

    step = autonomous_case(next(c for c in load_cases() if c["id"] == "missing_fact_unavailable"))[
        "steps"
    ][0]
    turn = {
        "speech": "The birth date is unavailable. An alternative method may work.",
        "tools": [
            {"name": "consult_call_reasoner", "arguments": {}, "result": {"status": "unknown"}}
        ],
        "ended": False,
    }
    assert grade_turn(step, turn) == []
    turn["speech"] = "May 2, 1990."
    assert grade_turn(step, turn)


class Connection:
    def __init__(self, *outputs):
        self.outputs = list(outputs)
        self.sent = []

    async def send(self, event):
        self.sent.append(event)

    async def recv_bytes(self):
        return json.dumps(
            {
                "type": "response.done",
                "response": {"status": "completed", "output": self.outputs.pop(0)},
            }
        )


def function(name, **arguments):
    return {
        "type": "function_call",
        "name": name,
        "call_id": "synthetic-tool",
        "arguments": json.dumps(arguments),
    }


async def test_known_identifier_uses_one_response_without_consulting_codex():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    parse = AsyncMock(side_effect=AssertionError("ordinary reply must not need Codex"))
    runtime, prompt, tools, handlers = build_runtime(
        load_cases()[0], reasoner_client=SimpleNamespace(responses=SimpleNamespace(parse=parse))
    )
    step = {
        "recipient": "Please say your eight digit member number.",
        "expect": {"spoken_value": "00420420", "ended": False},
    }
    connection = Connection([{"type": "message", "content": [{"text": "00420420"}]}])
    turn = await run_turn(connection, runtime, handlers, step)
    assert turn["speech"] == "00420420"
    assert turn["failures"] == []
    assert turn["suppressed_before_reasoning"] == []
    assert runtime.api_responses == 1
    parse.assert_not_awaited()
    response = next(x["response"] for x in connection.sent if x["type"] == "response.create")
    assert response["tool_choice"] == "auto"
    assert {t["name"] for t in response["tools"]} >= {
        "consult_call_reasoner",
        "press_phone_key",
        "enter_verification_fact",
        "finish_call",
    }
    assert "Dots call preparation" in prompt
    assert "fictional private lunch preference" not in prompt


async def test_direct_reply_policy_still_dispatches_a_needed_consultation():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from dots_phone.call_reasoner import ReasoningAdvice

    parse = AsyncMock(
        return_value=SimpleNamespace(
            output_parsed=ReasoningAdvice(
                status="unknown",
                next_step="Ask for another verification method.",
                answer="",
                supporting_quote="",
                report_supported=False,
            ),
            usage=None,
        )
    )
    runtime, _, _, handlers = build_runtime(
        load_cases()[0], reasoner_client=SimpleNamespace(responses=SimpleNamespace(parse=parse))
    )
    step = {"recipient": "What is the owner's birth date?", "expect": {"ended": False}}
    connection = Connection(
        [
            function(
                "consult_call_reasoner", question="Can an authorized source supply the birth date?"
            )
        ],
        [{"type": "message", "content": [{"text": "Is there another verification method?"}]}],
    )
    turn = await run_turn(connection, runtime, handlers, step)
    assert turn["failures"] == []
    parse.assert_awaited_once()
    assert [t["name"] for t in turn["tools"]] == ["consult_call_reasoner"]


@pytest.mark.parametrize("execute_local_tool", [False, True])
async def test_direct_consult_returns_keypad_proposal_until_model_chooses_a_local_action(
    execute_local_tool,
):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from dots_phone.call_reasoner import KeypadAction, ReasoningAdvice

    advice = ReasoningAdvice(
        status="ready",
        next_step="The balance option is two; assess the current request.",
        answer="",
        supporting_quote="",
        report_supported=False,
        action=KeypadAction(kind="press_phone_key", digit="2"),
    )
    parse = AsyncMock(return_value=SimpleNamespace(output_parsed=advice, usage=None))
    case = load_cases()[0]
    runtime, _, _, handlers = build_runtime(
        case,
        reasoner_client=SimpleNamespace(responses=SimpleNamespace(parse=parse)),
        response_policy="direct_with_codex",
    )

    class ProposalConnection(Connection):
        async def recv_bytes(self):
            if len(self.outputs) == 1:
                # The first response consulted Codex. Before Realtime chooses
                # its own continuation, that advice has sent no keypad input.
                assert runtime.digits == []
                output = next(
                    item["item"]["output"]
                    for item in self.sent
                    if item["type"] == "conversation.item.create"
                    and item["item"]["type"] == "function_call_output"
                )
                result = json.loads(output)
                assert result["action_status"] == "proposed_not_executed"
                assert result["proposed_action"] == {
                    "tool_name": "press_phone_key",
                    "arguments": {"digit": "2"},
                }
                assert "control_result" not in result
            return await super().recv_bytes()

    continuation = (
        function("press_phone_key", digit="2")
        if execute_local_tool
        else function("wait_for_recipient")
    )
    step = {
        "recipient": case["steps"][0]["recipient"],
        "expect": {"silent": True, "digits": ["2"] if execute_local_tool else [], "ended": False},
    }
    turn = await run_turn(
        ProposalConnection(
            [function("consult_call_reasoner", question="Which menu option serves the goal?")],
            [continuation],
        ),
        runtime,
        handlers,
        step,
    )
    assert turn["failures"] == []
    assert [tool["name"] for tool in turn["tools"]] == [
        "consult_call_reasoner",
        "press_phone_key" if execute_local_tool else "wait_for_recipient",
    ]
    parse.assert_awaited_once()


async def test_mandatory_consult_text_runtime_preserves_legacy_keypad_execution():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from dots_phone.call_reasoner import KeypadAction, ReasoningAdvice
    from dots_phone.text_eval import autonomous_case

    advice = ReasoningAdvice(
        status="ready",
        next_step="Select the balance option.",
        answer="",
        supporting_quote="",
        report_supported=False,
        action=KeypadAction(kind="press_phone_key", digit="2"),
    )
    case = load_cases()[0]
    runtime, _, _, handlers = build_runtime(
        case,
        reasoner_client=SimpleNamespace(
            responses=SimpleNamespace(
                parse=AsyncMock(return_value=SimpleNamespace(output_parsed=advice, usage=None))
            )
        ),
        response_policy="always_consult",
    )
    turn = await run_turn(
        Connection([function("consult_call_reasoner", question="Which option serves the goal?")]),
        runtime,
        handlers,
        autonomous_case(case)["steps"][0],
    )
    assert turn["failures"] == []
    assert turn["digits"] == ["2"]
    assert turn["tools"][0]["result"]["control_result"]["carrier_control_accepted"] is True
    assert "proposed_action" not in turn["tools"][0]["result"]


async def test_text_consultation_failure_returns_uncertainty_without_forcing_a_recovery_script():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from dots_phone.call_reasoner import UNAVAILABLE_REASONING_GUIDANCE

    runtime, _, _, handlers = build_runtime(
        load_cases()[0],
        reasoner_client=SimpleNamespace(
            responses=SimpleNamespace(parse=AsyncMock(side_effect=TimeoutError))
        ),
    )
    turn = await run_turn(
        Connection(
            [function("consult_call_reasoner", question="Can you help with this uncertainty?")],
            [function("wait_for_recipient")],
        ),
        runtime,
        handlers,
        {"recipient": "Please hold.", "expect": {"silent": True, "digits": [], "ended": False}},
    )
    assert turn["failures"] == []
    result = turn["tools"][0]["result"]
    assert result["status"] == "unknown" and result["answer"] == ""
    assert result["next_step"] == UNAVAILABLE_REASONING_GUIDANCE


async def test_actual_shared_keypad_handler_derives_digits_and_preserves_silence():
    case = next(case for case in load_cases() if case["id"] == "nested_keypad_verification")
    step = case["steps"][2]
    runtime, _, _, handlers = build_runtime(case)
    connection = Connection(
        [
            {"type": "message", "phase": "commentary", "content": [{"text": "Internal planning"}]},
            function(
                "enter_verification_fact",
                fact_name="member_number",
                prompt_quote=step["recipient"],
                terminator="#",
            ),
        ]
    )
    turn = await run_turn(connection, runtime, handlers, step)
    assert turn["failures"] == []
    assert turn["digits"] == ["00420420#"]
    assert turn["suppressed_commentary"] == ["Internal planning"]
    assert connection.sent[0]["item"]["content"][0]["type"] == "input_text"
    assert connection.sent[1]["response"]["output_modalities"] == ["text"]
    assert not any(event["type"].startswith("input_audio") for event in connection.sent)


async def test_text_communication_is_gated_on_a_reasoner_result_before_owner_verification():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from dots_phone.call_reasoner import ReasoningAdvice

    case = next(c for c in load_cases() if c["id"] == "library_loan_due_date")
    fact_name, value = next((k, v) for k, v in case["facts"].items() if k != "owner_name")
    client = SimpleNamespace(
        responses=SimpleNamespace(
            parse=AsyncMock(
                return_value=SimpleNamespace(
                    output_parsed=ReasoningAdvice(
                        status="ready",
                        next_step="Give the owner's approved identifier.",
                        answer=value,
                        supporting_quote=value,
                        report_supported=False,
                    ),
                    usage=None,
                )
            )
        )
    )
    runtime, _, _, handlers = build_runtime(
        case, reasoner_client=client, response_policy="always_consult"
    )
    connection = Connection(
        [
            {"type": "message", "content": [{"text": "I cannot disclose that."}]},
            function(
                "consult_call_reasoner", question="How do I answer this verification request?"
            ),
        ],
        [{"type": "message", "content": [{"text": value}]}],
    )
    turn = await run_turn(connection, runtime, handlers, case["steps"][0])
    assert turn["speech"] == value and turn["failures"] == []
    assert turn["suppressed_before_reasoning"] == ["I cannot disclose that."]
    requests = [x["response"] for x in connection.sent if x["type"] == "response.create"]
    assert requests[0]["tool_choice"] == "required"
    assert [t["name"] for t in requests[0]["tools"]] == ["consult_call_reasoner"]
    assert requests[1]["tool_choice"] == "auto"
    client.responses.parse.assert_awaited_once()


async def test_malformed_menu_key_is_refused_and_can_be_corrected_without_dialing():
    case = load_cases()[0]
    step = case["steps"][0]
    runtime, _, _, handlers = build_runtime(case)
    connection = Connection(
        [function("press_phone_key", digit="99", menu_quote="For balances press 9")],
        [
            function(
                "press_phone_key",
                digit="2",
                menu_quote="For your available points balance, press 2.",
            )
        ],
    )
    turn = await run_turn(connection, runtime, handlers, step)
    assert turn["failures"] == []
    assert turn["tools"][0]["result"]["carrier_control_accepted"] is False
    assert turn["digits"] == ["2"]


async def test_model_menu_mistake_is_scored_as_failure_instead_of_hidden_by_a_phrase_gate():
    case = load_cases()[0]
    runtime, _, _, handlers = build_runtime(case)
    turn = await run_turn(
        Connection([function("press_phone_key", digit="9", menu_quote="I think nine is right")]),
        runtime,
        handlers,
        case["steps"][0],
    )
    # Execution enforces action/data boundaries. The scenario independently
    # assesses whether the model actually chose a useful menu option.
    assert turn["tools"][0]["result"]["carrier_control_accepted"] is True
    assert turn["digits"] == ["9"]
    assert turn["failures"]


def test_customer_service_matrix_has_distinct_goals_sources_and_private_reactive_records():
    cases = load_cases()
    assert len(cases) >= 60
    expanded = [c for c in cases if "customer_service" in c["tags"]]
    assert len({c["domain"] for c in expanded}) >= 15
    assert len({c["purpose"] for c in expanded}) == len(expanded)
    assert len([c for c in expanded if c.get("simulator")]) >= 12
    assert {c["split"] for c in expanded} == {"regression", "holdout"}
    statuses = {
        r["status"] for c in expanded for f in c.get("lookup_fixtures", []) for r in f["responses"]
    }
    assert {"ok", "timeout", "unavailable", "ambiguous", "permission_denied", "stale"} <= statuses
    for case in expanded:
        _, prompt, _, _ = build_runtime(case)
        if case.get("simulator"):
            assert case["simulator"]["result_text"] not in prompt
        for fixture in case.get("lookup_fixtures", []):
            for response in fixture["responses"]:
                for record in response.get("records", []):
                    assert record["value"] not in prompt


def test_repeated_runs_require_every_planned_trial_and_preserve_failures():
    cases = [{"id": "a", "domain": "retail"}, {"id": "b", "domain": "billing"}]
    runs = [
        {"scenario_id": "a", "passed": True},
        {"scenario_id": "a", "passed": False},
        {"scenario_id": "b", "passed": True},
    ]
    summary = suite_summary(runs, cases, 2)
    assert summary["planned_runs"] == 4
    assert summary["completed_runs"] == 3
    assert summary["all_runs_passed_scenarios"] == 0
    assert summary["per_scenario"]["a"]["passes"] == 1


def test_alphanumeric_grader_accepts_phonetics_but_rejects_wrong_zero_or_letter():
    expected = {"expect": {"spoken_identifier": "ZQ6M2B"}}
    turn = {"speech": "Zulu Quebec six Mike two Bravo", "tools": [], "ended": False}
    assert grade_turn(expected, turn) == []
    turn["speech"] = "Zulu Quebec six November two Bravo"
    assert grade_turn(expected, turn)
    turn["speech"] = "Z, Q, then six Mike two Bravo"
    assert grade_turn(expected, turn) == []


async def test_reasoner_accepts_a_verbatim_json_fact_quote_but_not_private_context():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from dots_phone.call_reasoner import ReasoningAdvice
    from dots_phone.service import Refused

    advice = ReasoningAdvice(
        status="ready",
        next_step="Give the verified answer.",
        answer="00420420",
        supporting_quote='"name":"member_number","value":"00420420"',
        report_supported=False,
    )
    client = SimpleNamespace(
        responses=SimpleNamespace(
            parse=AsyncMock(return_value=SimpleNamespace(output_parsed=advice, usage=None))
        )
    )
    runtime, _, _, _ = build_runtime(load_cases()[0], reasoner_client=client)
    assert (
        await runtime.reasoner.advise(runtime.call, [], "What is the member number?")
    ).answer == "00420420"
    advice.supporting_quote = "fictional private lunch preference"
    with pytest.raises(Refused, match="supporting evidence"):
        await runtime.reasoner.advise(runtime.call, [], "What is the member number?")


async def test_compound_fact_quote_is_valid_only_for_the_disclosed_array():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from dots_phone.call_reasoner import ReasoningAdvice
    from dots_phone.service import Refused

    case = next(c for c in load_cases() if c["id"] == "retail_order_tracking")
    parse = AsyncMock()
    runtime, _, _, _ = build_runtime(
        case, reasoner_client=SimpleNamespace(responses=SimpleNamespace(parse=parse))
    )
    brief = runtime.call["brief"]
    disclosed = [f for f in brief["facts"] if f["name"] in brief["disclosures"]]
    quote = json.dumps(disclosed, ensure_ascii=False, separators=(",", ":"))[1:-1]
    advice = ReasoningAdvice(
        status="ready",
        next_step="Say the approved name and order number.",
        answer="Samira El-Amin, A17B00",
        supporting_quote=quote,
        report_supported=False,
    )
    parse.return_value = SimpleNamespace(output_parsed=advice, usage=None)
    assert await runtime.reasoner.advise(runtime.call, [], "Name and order number?") == advice
    advice.supporting_quote = '"facts":' + json.dumps(
        disclosed, ensure_ascii=False, separators=(",", ":")
    )
    assert await runtime.reasoner.advise(runtime.call, [], "Name and order number?") == advice
    private = next(f for f in brief["facts"] if f["name"] not in brief["disclosures"])
    advice.supporting_quote = json.dumps(private, separators=(",", ":"))
    with pytest.raises(Refused, match="supporting evidence"):
        await runtime.reasoner.advise(runtime.call, [], "Name and order number?")


@pytest.mark.parametrize(
    ("quote", "allowed"),
    [
        ("“The refund is unpaid.” “A decision is due October 22.”", True),
        ('"The refund is unpaid." "A decision is due October 22."', True),
        ("“The refund is unpaid.” “The refund was paid.”", False),
        ("“The refund is unpaid.” and invent the rest", False),
        ("“”", False),
    ],
)
def test_multiple_citations_require_every_exact_excerpt_without_invented_text(quote, allowed):
    from dots_phone.call_reasoner import quote_supported

    assert (
        quote_supported(quote, ["The refund is unpaid.", "A decision is due October 22."])
        is allowed
    )


@pytest.mark.parametrize("kind", ["error", "response.done"])
async def test_credit_errors_are_distinct_and_stop_the_campaign(kind):
    from dots_phone.text_eval import ProviderBillingUnavailable, receive_until

    class BillingConnection(Connection):
        async def recv_bytes(self):
            if kind == "error":
                return json.dumps({"type": kind, "error": {"code": "credit_balance_exhausted"}})
            return json.dumps(
                {
                    "type": kind,
                    "response": {
                        "status": "failed",
                        "output": [],
                        "status_details": {"error": {"code": "insufficient_quota"}},
                    },
                }
            )

    connection = BillingConnection()
    with pytest.raises(ProviderBillingUnavailable):
        if kind == "error":
            await receive_until(connection, "response.done")
        else:
            case = load_cases()[0]
            runtime, _, _, handlers = build_runtime(case)
            await run_turn(connection, runtime, handlers, case["steps"][0])


def checkpoint_fixture():
    import hashlib

    from dots_phone.text_eval import autonomous_case

    case = load_cases()[0]
    sha = hashlib.sha256(json.dumps(autonomous_case(case), sort_keys=True).encode()).hexdigest()
    report = {
        "configuration": {
            "model": "gpt-realtime-2.1",
            "model_contract_sha256": "frozen",
            "reasoner_backend": "signed_in_codex_fictional_tools",
        },
        "results": [
            {
                "scenario_id": case["id"],
                "repeat": n,
                "scenario_sha256": sha,
                "passed": n == 1,
                **(
                    {"error_type": "RuntimeError", "infrastructure_failure": True} if n == 3 else {}
                ),
            }
            for n in (1, 2, 3)
        ],
    }
    return case, report


def test_resume_retains_successes_and_behavior_failures_and_preserves_infrastructure_attempts():
    from dots_phone.text_eval import resume_results

    case, report = checkpoint_fixture()
    retained, previous = resume_results(report, [case], 3, "gpt-realtime-2.1", "frozen")
    assert [r["repeat"] for r in retained] == [1, 2]
    assert retained[1]["passed"] is False
    assert previous[0]["repeat"] == 3
    assert previous[0]["error_type"] == "RuntimeError"
    assert "preserved" in previous[0]["resume_reason"]


def test_resume_does_not_reroll_invalid_model_output_as_infrastructure():
    from dots_phone.text_eval import resume_results

    case, report = checkpoint_fixture()
    invalid = {
        **report["results"][0],
        "repeat": 4,
        "passed": False,
        "error_type": "ValidationError",
    }
    report["results"].append(invalid)
    retained, previous = resume_results(report, [case], 4, "gpt-realtime-2.1", "frozen")
    assert invalid in retained
    assert len(previous) == 1


@pytest.mark.parametrize(
    "change", ["model", "contract", "backend", "scenario", "duplicate", "policy", "effort"]
)
def test_resume_rejects_changed_contract_or_scenario_and_duplicate_trials(change):
    from dots_phone.text_eval import resume_results

    case, report = checkpoint_fixture()
    if change == "model":
        report["configuration"]["model"] = "different"
    elif change == "contract":
        report["configuration"]["model_contract_sha256"] = "different"
    elif change == "backend":
        report["configuration"]["reasoner_backend"] = "fixture_host"
    elif change == "scenario":
        report["results"][0]["scenario_sha256"] = "different"
    elif change == "policy":
        report["configuration"]["response_policy"] = "direct_with_codex"
    elif change == "effort":
        report["configuration"]["voice_effort"] = "medium"
    else:
        report["results"].append(report["results"][0])
    with pytest.raises(ValueError):
        resume_results(report, [case], 3, "gpt-realtime-2.1", "frozen")


async def test_suite_stops_launching_trials_after_billing_failure(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    import openai

    from dots_phone import text_eval
    from dots_phone.text_eval_agents import CodexEvalWorker

    client = SimpleNamespace(__aenter__=AsyncMock(), __aexit__=AsyncMock())

    # Special methods are resolved on the type for an async context manager.
    class Client:
        async def __aenter__(self):
            return client

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr(openai, "AsyncOpenAI", lambda **kwargs: Client())
    worker = SimpleNamespace(poisoned=False, close=AsyncMock())
    monkeypatch.setattr(CodexEvalWorker, "start", AsyncMock(return_value=worker))
    monkeypatch.setenv("OPENAI_API_KEY", "fictional")
    cases = load_cases()[:2]
    run_case = AsyncMock(
        return_value={
            "scenario_id": cases[0]["id"],
            "passed": False,
            "billing_exhausted": True,
            "error_type": "ProviderBillingUnavailable",
        }
    )
    monkeypatch.setattr(text_eval, "run_case", run_case)
    args = SimpleNamespace(
        runtime_file=None,
        repeat=3,
        jobs=1,
        model="gpt-realtime-2.1",
        codex_eval_worker=True,
        codex_worker_directory=None,
        independent_reasoner=False,
        response_policy="direct_with_codex",
        voice_effort="medium",
        resume_from=None,
        output=tmp_path / "report.json",
    )
    assert await text_eval.run_suite(args, cases) is False
    assert run_case.await_count == 1
    report = json.loads(args.output.read_text())
    assert report["summary"]["completed_runs"] == 1
    assert report["stopped_reason"]
    worker.close.assert_awaited_once()


async def test_compound_answers_can_cite_disclosed_fact_names_without_serializing_json():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from dots_phone.call_reasoner import ReasoningAdvice
    from dots_phone.service import Refused

    advice = ReasoningAdvice(
        status="ready",
        next_step="Give the approved identifiers.",
        answer="Samira El-Amin, A17B00",
        supporting_quote="",
        supporting_fact_names=["owner_name", "order_number"],
        report_supported=False,
    )
    parse = AsyncMock(return_value=SimpleNamespace(output_parsed=advice, usage=None))
    runtime, _, _, _ = build_runtime(
        next(c for c in load_cases() if c["id"] == "retail_order_tracking"),
        reasoner_client=SimpleNamespace(responses=SimpleNamespace(parse=parse)),
    )
    assert await runtime.reasoner.advise(runtime.call, [], "Name and order number?") == advice
    advice.supporting_fact_names.append("private_lunch_preference")
    with pytest.raises(Refused, match="undisclosed briefing fact"):
        await runtime.reasoner.advise(runtime.call, [], "Name and order number?")
