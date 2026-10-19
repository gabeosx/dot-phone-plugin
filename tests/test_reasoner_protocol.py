import json

import pytest
from openai.lib._pydantic import to_strict_json_schema
from pydantic import ValidationError

from dots_phone.call_reasoner import ReasoningAdvice
from dots_phone.reasoner_protocol import CompactDecision


@pytest.mark.parametrize(
    ("decision", "expected"),
    [
        ({"kind": "wait"}, {"answer": "", "action": None, "report_supported": False}),
        (
            {"kind": "say", "text": "zero zero four two", "facts": ["card"], "quote": ""},
            {"answer": "zero zero four two", "supporting_fact_names": ["card"]},
        ),
        (
            {"kind": "say", "text": "02139", "facts": [], "quote": "Postal code: 02139"},
            {"answer": "02139", "supporting_quote": "Postal code: 02139"},
        ),
        (
            {"kind": "guide", "status": "unknown", "text": "Ask for alternative verification."},
            {"answer": "", "status": "unknown"},
        ),
        ({"kind": "review", "ok": True, "text": ""}, {"report_supported": True}),
        (
            {"kind": "review", "ok": False, "text": "The quoted renewal was unavailable."},
            {"report_supported": False, "next_step": "The quoted renewal was unavailable."},
        ),
        (
            {"kind": "key", "digit": "2", "quote": "For loans press 2."},
            {"answer": "", "action": {"digit": "2", "menu_quote": "For loans press 2."}},
        ),
        (
            {
                "kind": "fact",
                "name": "card",
                "prompt": "Enter the last four digits followed by pound.",
                "end": "#",
                "offset": 4,
                "count": 4,
                "value": "",
            },
            {
                "action": {
                    "fact_name": "card",
                    "digit_offset": 4,
                    "digit_count": 4,
                    "terminator": "#",
                }
            },
        ),
        (
            {
                "kind": "source",
                "digits": "0042",
                "prompt": "Enter your reference followed by pound.",
                "quote": "Your reference is 0042.",
                "end": "#",
            },
            {"supporting_quote": "Your reference is 0042.", "action": {"source_value": "0042"}},
        ),
    ],
)
def test_compact_decisions_preserve_evidence_digits_and_control_arguments(decision, expected):
    advice = CompactDecision.model_validate({"decision": decision}).advice().model_dump()
    for key, value in expected.items():
        if isinstance(value, dict):
            assert all(advice[key][k] == v for k, v in value.items())
        else:
            assert advice[key] == value


@pytest.mark.parametrize(
    "decision",
    [
        {"kind": "prepared"},
        {"kind": "say", "text": "0042", "quote": ""},
        {"kind": "guide", "status": "ready", "text": "Wait.", "ok": True},
        {"kind": "key", "digit": "22", "quote": "Press 2."},
        {
            "kind": "source",
            "digits": "00O2",
            "quote": "0042",
            "prompt": "Enter reference",
            "end": "#",
        },
        {"kind": "other"},
    ],
)
def test_incomplete_or_wrong_phase_decisions_cannot_be_used_as_live_advice(decision):
    with pytest.raises((ValueError, ValidationError)):
        CompactDecision.model_validate({"decision": decision}).advice()


def test_compact_menu_and_wait_responses_omit_irrelevant_fields():
    decision = {"decision": {"kind": "key", "digit": "2", "quote": "For loans press 2."}}
    advice = CompactDecision.model_validate(decision).advice()
    assert len(json.dumps(decision)) < len(advice.model_dump_json()) / 2
    schema = to_strict_json_schema(CompactDecision)
    assert schema["type"] == "object" and "anyOf" in schema["properties"]["decision"]
    assert ReasoningAdvice.model_validate(advice) == advice


@pytest.mark.parametrize("digit", ["*", "#"])
def test_compact_menu_allows_phone_keys_without_a_model_copied_quote(digit):
    from dots_phone.account_task import account_menu_option

    advice = CompactDecision.model_validate(
        {"decision": {"kind": "key", "digit": digit, "quote": ""}}
    ).advice()
    assert advice.action.digit == digit
    assert advice.action.menu_quote == ""
    assert account_menu_option(digit, "", "Use the keypad for this menu.")


def test_compact_fact_prompt_is_an_optional_annotation_not_a_required_quote():
    advice = CompactDecision.model_validate(
        {
            "decision": {
                "kind": "fact",
                "name": "member",
                "prompt": "",
                "end": "#",
                "offset": 0,
                "count": None,
                "value": "",
            }
        }
    ).advice()
    assert advice.action.fact_name == "member" and advice.action.prompt_quote == ""
    assert advice.action.terminator == "#"
