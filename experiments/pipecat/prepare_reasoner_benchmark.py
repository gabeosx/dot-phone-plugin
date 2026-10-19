"""Freeze production reasoning inputs and grade a Codex-only latency comparison."""

import argparse
import json
import re
from pathlib import Path
from statistics import median

from dots_phone.call_reasoner import REASONING_PROMPT, ReasoningAdvice, reasoning_context
from dots_phone.reasoner_protocol import CompactDecision
from dots_phone.service import digest
from dots_phone.text_eval import fixture_call
from openai.lib._pydantic import to_strict_json_schema


def payload():
    call = fixture_call(
        {
            "default_facts": False,
            "purpose": "Find my current library loan due date and whether I can renew it.",
            "facts": {
                "owner_name": "Morgan Vale",
                "library_card": "00420420",
                "postal_code": "02139",
            },
            "completion_criteria": [
                {"id": "due", "description": "Obtain the loan due date and renewal availability"}
            ],
        }
    )
    call["brief"]["actions"].remove("read_call_sources")
    call["brief_hash"] = digest(call["brief"])
    history = []
    cases = []

    def add(name, recipient, question, assistant="", report=None):
        if recipient:
            history.append({"role": "user", "text": recipient})
        cases.append(
            {"id": name, "context": reasoning_context(call, list(history), question, report=report)}
        )
        if assistant:
            history.append({"role": "assistant", "text": assistant})

    add(
        "menu",
        "For loan information press 2. For a representative press 0.",
        "Which option should we select?",
    )
    add(
        "authorized_identifier",
        "What is your library card? Please say it.",
        "What should I answer?",
        "Zero zero four two zero four two zero.",
    )
    add(
        "incorrect_readback",
        "I heard zero zero four two zero four two one. Is that correct?",
        "How should I handle this readback?",
        "No. Zero zero four two zero four two zero.",
    )
    add(
        "missing_information",
        "What is the account holder's date of birth?",
        "This fact is absent and no source tools are available. What should I do?",
        "Can you verify using the library card and postal code instead?",
    )
    history.extend(
        [
            {"role": "user", "text": "What can I help you with?"},
            {"role": "assistant", "text": "I need my loan due date and renewal availability."},
            {
                "role": "user",
                "text": "Here is information about getting a new library card. What can I help you with?",
            },
            {
                "role": "assistant",
                "text": "Please check when my borrowed item is due and whether I can renew it.",
            },
        ]
    )
    add(
        "routing_recovery",
        "Here is information about getting a new library card again. What can I help you with?",
        "Both different topic requests reached the wrong service. What next?",
        "Please connect me to a representative.",
    )
    evidence = "Your loan is due October 30, 2026. Renewal is unavailable because another reader reserved the item."
    bad = {
        "outcome": "completed",
        "confirmed_facts": [
            {"text": "Renewal was completed until November 30, 2026.", "quote": evidence}
        ],
        "commitments": [],
        "unresolved_questions": [],
        "termination_reason": "task_finished",
    }
    good = {**bad, "confirmed_facts": [{"text": evidence, "quote": evidence}]}
    add("reject_false_completion", evidence, "Review this proposed final report.", report=bad)
    add("accept_supported_completion", "", "Review the corrected final report.", report=good)
    return {
        "instructions": REASONING_PROMPT,
        "schema": to_strict_json_schema(CompactDecision),
        "prewarm": True,
        "brief_hash": call["brief_hash"],
        "cases": cases,
        "repeats": 2,
        "configurations": [
            {"name": "sol_medium", "model": "gpt-6-sol", "effort": "medium"},
        ],
    }


def grade(row):
    advice = (
        CompactDecision.model_validate(row["advice"]).advice()
        if "decision" in row["advice"]
        else ReasoningAdvice.model_validate(row["advice"])
    )
    text = f"{advice.answer} {advice.next_step}".lower()
    words = dict(
        zip("zero one two three four five six seven eight nine".split(), "0123456789", strict=True)
    )
    digits = re.sub(r"\b(" + "|".join(words) + r")\b", lambda m: words[m[0]], text)
    identifier = "00420420" in re.sub(r"\D", "", digits)
    return {
        "menu": lambda: (
            advice.action is not None
            and advice.action.kind == "press_phone_key"
            and advice.action.digit == "2"
            and bool(advice.action.menu_quote)
            and advice.action.menu_quote
            in "For loan information press 2. For a representative press 0."
        ),
        "authorized_identifier": lambda: (
            advice.status == "ready"
            and identifier
            and "library_card" in advice.supporting_fact_names
        ),
        "incorrect_readback": lambda: (
            (
                identifier
                or bool(re.search(r"(?:last|final).*\b(?:zero|0)\b|ends? in (?:zero|0)", text))
            )
            and bool(
                re.search(
                    r"\b(no|incorrect|wrong|corrected)\b|\bcorrect (?:the|this|that|it)\b", text
                )
            )
            and "library_card" in advice.supporting_fact_names
        ),
        "missing_information": lambda: (
            not advice.answer
            and bool(re.search(r"alternative|another|other|postal|library card", text))
            and not re.search(r"\b(?:19|20)\d{2}\b", text)
        ),
        "routing_recovery": lambda: (
            bool(re.search(r"representative|human|agent", text)) and advice.action is None
        ),
        "reject_false_completion": lambda: not advice.report_supported,
        "accept_supported_completion": lambda: advice.report_supported,
    }[row["case"]]()


def summarize(report):
    for row in report["rows"]:
        try:
            if "decision" in row["advice"]:
                row["normalized_advice"] = (
                    CompactDecision.model_validate(row["advice"]).advice().model_dump()
                )
            row["passed"] = bool(grade(row))
        except ValueError:
            row["passed"] = False
    summaries = {}
    for config in report["configurations"]:
        rows = [r for r in report["rows"] if r["configuration"] == config["name"]]
        times = sorted(r["elapsed_ms"] for r in rows)
        warm = sorted(
            r["elapsed_ms"]
            for r in rows
            if not r.get("first_interaction_turn", r["first_model_turn"])
        )
        summaries[config["name"]] = {
            "completed": len(rows),
            "passed": sum(r["passed"] for r in rows),
            "median_ms": median(times) if times else None,
            "max_ms": max(times) if times else None,
            "warm_median_ms": median(warm) if warm else None,
            "failures": [r["case"] for r in rows if not r["passed"]],
        }
        token_rows = [
            (r, r["token_usage"]["last"]) for r in rows if (r.get("token_usage") or {}).get("last")
        ]
        if token_rows:
            summaries[config["name"]]["diagnostics"] = {
                "input_tokens": sum(u["inputTokens"] for _, u in token_rows),
                "cached_input_tokens": sum(u["cachedInputTokens"] for _, u in token_rows),
                "output_tokens": sum(u["outputTokens"] for _, u in token_rows),
                "reasoning_tokens": sum(u["reasoningOutputTokens"] for _, u in token_rows),
                "first_live_turn_cache": [
                    {
                        "repeat": r["repeat"],
                        "input": u["inputTokens"],
                        "cached": u["cachedInputTokens"],
                    }
                    for r, u in token_rows
                    if r.get("first_interaction_turn", r["first_model_turn"])
                ],
            }
            timed = [r for r, _ in token_rows if r.get("first_token_ms") is not None]
            summaries[config["name"]]["diagnostics"].update(
                median_first_token_ms=median(r["first_token_ms"] for r in timed) if timed else None,
                median_after_first_token_ms=median(
                    r["elapsed_ms"] - r["first_token_ms"] for r in timed
                )
                if timed
                else None,
            )
    report["summary"] = summaries
    report["grading_note"] = (
        "Incorrect readbacks accept either the full correct identifier or an explicit correction of the mismatched final digit to zero. Affirming the wrong readback fails."
    )
    report["limitations"] = (
        "Repeated fictional episodes; no live tools or audio. Preparation and first live turn are separate. Small comparison, not a production reliability estimate; text timing does not measure VAD."
    )
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    parser.add_argument("--grade", action="store_true")
    args = parser.parse_args()
    data = summarize(json.loads(args.path.read_text())) if args.grade else payload()
    args.path.write_text(json.dumps(data, indent=2) + "\n")
    args.path.chmod(0o600)
    if args.grade:
        print(json.dumps(data["summary"], indent=2))
