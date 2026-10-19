"""Small model responses expanded into the existing, validated advice contract."""

from typing import Literal

from pydantic import Field

from dots_phone.models import StrictModel


class Prepared(StrictModel):
    kind: Literal["prepared"]


class Wait(StrictModel):
    kind: Literal["wait"]


class Say(StrictModel):
    kind: Literal["say"]
    text: str = Field(min_length=1, max_length=240)
    facts: list[str] = Field(max_length=8)
    quote: str = Field(max_length=400)


class Guide(StrictModel):
    kind: Literal["guide"]
    status: Literal["ready", "unknown", "blocked"]
    text: str = Field(min_length=1, max_length=600)


class Review(StrictModel):
    kind: Literal["review"]
    ok: bool
    text: str = Field(max_length=600)


class MenuKey(StrictModel):
    kind: Literal["key"]
    digit: str = Field(pattern=r"^[0-9*#]$")
    quote: str = Field(max_length=400)


class EnterFact(StrictModel):
    kind: Literal["fact"]
    name: str = Field(min_length=1, max_length=80)
    prompt: str = Field(max_length=400)
    end: str = Field(pattern=r"^[#*]?$")
    offset: int = Field(ge=0)
    count: int | None = Field(ge=1)
    value: str = Field(max_length=240)


class EnterSource(StrictModel):
    kind: Literal["source"]
    digits: str = Field(pattern=r"^[0-9]+$", max_length=240)
    prompt: str = Field(max_length=400)
    quote: str = Field(min_length=1, max_length=400)
    end: str = Field(pattern=r"^[#*]?$")


class CompactDecision(StrictModel):
    # A nested union keeps the root an object for Structured Outputs. Keep this
    # SAME schema for preparation, speech, controls, waits, and report reviews.
    decision: Prepared | Wait | Say | Guide | Review | MenuKey | EnterFact | EnterSource

    def advice(self):
        from dots_phone.call_reasoner import ReasoningAdvice

        decision = self.decision
        result = {
            "status": "ready",
            "next_step": "Wait silently for the recipient.",
            "answer": "",
            "supporting_quote": "",
            "supporting_fact_names": [],
            "report_supported": False,
            "action": None,
        }
        match decision:
            case Prepared():
                raise ValueError("preparation is not a live decision")
            case Say():
                result.update(
                    next_step="Speak the supported answer once.",
                    answer=decision.text,
                    supporting_fact_names=decision.facts,
                    supporting_quote=decision.quote,
                )
            case Guide():
                result.update(status=decision.status, next_step=decision.text)
            case Review():
                result.update(
                    next_step=decision.text
                    or (
                        "Accept the supported report."
                        if decision.ok
                        else "Correct the unsupported report."
                    ),
                    report_supported=decision.ok,
                )
            case MenuKey():
                result.update(
                    next_step="Execute the validated menu key silently.",
                    action={
                        "kind": "press_phone_key",
                        "digit": decision.digit,
                        "menu_quote": decision.quote,
                    },
                )
            case EnterFact():
                result.update(
                    next_step="Execute the validated verification entry silently.",
                    action={
                        "kind": "enter_verification_fact",
                        "fact_name": decision.name,
                        "prompt_quote": decision.prompt,
                        "terminator": decision.end,
                        "digit_offset": decision.offset,
                        "digit_count": decision.count,
                        "fact_value": decision.value,
                    },
                )
            case EnterSource():
                result.update(
                    next_step="Execute the validated verification entry silently.",
                    supporting_quote=decision.quote,
                    action={
                        "kind": "enter_verification_fact",
                        "source_value": decision.digits,
                        "prompt_quote": decision.prompt,
                        "terminator": decision.end,
                    },
                )
        return ReasoningAdvice.model_validate(result)
