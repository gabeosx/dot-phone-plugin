from dots_phone.text_eval_agents import SimulatorReply
from dots_phone.text_eval_simulator import identifier_supplied, simulator_errors, supplied_fields

SCENARIO = {
    "required_fields": [
        {"name": "reference", "value": "OR00962"},
        {"name": "owner_name", "value": "Talia Green"},
    ],
    "result_text": "Your order has not shipped.",
}


def test_ivr_cannot_verify_itself_from_its_own_readback_or_release_secret_result():
    history = [
        {
            "recipient": "I heard OR00962 for Talia Green, correct?",
            "speech": "No, that is wrong.",
            "digits": [],
        }
    ]
    reply = SimulatorReply(
        text=SCENARIO["result_text"],
        accepted_fields=["reference"],
        result_released=True,
        terminal=True,
    )
    assert supplied_fields(SCENARIO, history) == set()
    assert len(simulator_errors(SCENARIO, history, reply)) == 2


def test_ivr_accepts_actual_compound_answer_and_requires_exact_result():
    history = [
        {
            "recipient": "Name and reference?",
            "speech": "Talia Green. Oscar Romeo zero zero nine six two.",
            "digits": [],
        }
    ]
    assert supplied_fields(SCENARIO, history) == {"reference", "owner_name"}
    reply = SimulatorReply(
        text=SCENARIO["result_text"],
        accepted_fields=["reference", "owner_name"],
        result_released=True,
        terminal=True,
    )
    assert simulator_errors(SCENARIO, history, reply) == []
    reply.text = "Your order shipped."
    assert simulator_errors(SCENARIO, history, reply) == [
        "simulator changed the expected recipient result"
    ]


def test_numeric_and_spelled_identifiers_cannot_match_a_longer_wrong_number():
    assert identifier_supplied("0048", "zero zero four eight")
    assert not identifier_supplied("0048", "zero zero four eight zero")
    assert not identifier_supplied("OR00962", "OR009620")
    assert identifier_supplied(
        "ZQ6M2B", "Z as in Zulu, Q as in Quebec, six, M as in Mike, two, B as in Bravo"
    )
    assert identifier_supplied("INV7012", "INV followed by seven zero one two")


def test_failure_branch_requires_actual_caller_inability_not_private_simulator_knowledge():
    scenario = {**SCENARIO, "verification_unavailable": True}
    history = [{"recipient": "What can I help with?", "speech": "Check my order status."}]
    reply = SimulatorReply(
        text=scenario["result_text"], accepted_fields=[], result_released=True, terminal=True
    )
    assert simulator_errors(scenario, history, reply) == [
        "simulator declared verification unavailable before caller inability"
    ]
    history.append(
        {
            "recipient": "What is your reference?",
            "speech": "I can\u2019t retrieve that reference. Is there another way to verify?",
        }
    )
    assert simulator_errors(scenario, history, reply) == []


async def test_late_worker_response_cannot_be_accepted_for_a_different_case():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock

    import pytest

    from dots_phone.text_eval_agents import CodexEvalWorker

    process = SimpleNamespace(
        returncode=None,
        stdin=SimpleNamespace(write=Mock(), drain=AsyncMock()),
        stdout=SimpleNamespace(
            readline=AsyncMock(return_value=b'{"id":"previous-request","output":{}}\n')
        ),
        terminate=Mock(),
        wait=AsyncMock(return_value=0),
    )
    worker = CodexEvalWorker(process)
    with pytest.raises(RuntimeError, match="does not match"):
        await worker.request(
            operation="simulate",
            model="gpt-6-sol",
            instructions="Private scenario",
            context={},
            schema={},
        )
    assert worker.poisoned
    process.terminate.assert_called_once()
