from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WorkflowRef(StrictModel):
    """Read static bundled instructions; accepts no call or filesystem input."""


class Fact(StrictModel):
    name: str = Field(min_length=1, max_length=60)
    value: str = Field(min_length=1, max_length=240)
    provenance: str = Field(min_length=1, max_length=160)
    verified_at: datetime
    expires_at: datetime

    @field_validator("verified_at", "expires_at")
    @classmethod
    def aware(cls, value):
        if value.tzinfo is None:
            raise ValueError("timezone required")
        return value


class CompletionCriterion(StrictModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,39}$")
    description: str = Field(min_length=1, max_length=240)
    required: bool = True


class Brief(StrictModel):
    destination: str = Field(pattern=r"^\+1[2-9][0-9]{9}$")
    purpose: str = Field(min_length=1, max_length=300)
    facts: list[Fact] = Field(default_factory=list, max_length=12)
    disclosures: list[str] = Field(default_factory=list, max_length=12)
    actions: list[str] = Field(default_factory=list, max_length=12)
    source_tools: list[str] | None = Field(
        default=None,
        max_length=64,
        description="Optional exact connected read-tool IDs (namespace.action) selected by the user. Omit to inherit established connected read-tool permissions; [] disables live tools. Requires read_call_sources for retrieval.",
    )
    constraints: list[str] = Field(default_factory=list, max_length=12)
    unknowns: list[str] = Field(default_factory=list, max_length=8)
    stop_conditions: list[str] = Field(default_factory=list, max_length=8)
    completion_criteria: list[CompletionCriterion] = Field(default_factory=list, max_length=4)
    deadline: datetime
    max_seconds: int | None = Field(
        default=None,
        ge=1,
        le=3600,
        description="Optional explicit user duration cap, including IVR, hold and conversation time. Omit or use null when the user gave no time limit: preparation derives the longest duration that fits the authorized budget and installation limit. Do not invent a shorter test cutoff.",
    )
    budget_usd: Decimal = Field(gt=0, le=20, max_digits=6, decimal_places=4)
    synthetic: bool = True
    stop_on_human: bool = Field(
        default=False,
        description="IVR-only test: silently end on a likely live person; detection is probabilistic.",
    )
    test_response_limit: int = Field(default=0, ge=0, le=100)

    @field_validator("completion_criteria")
    @classmethod
    def distinct_criteria(cls, values):
        if len({criterion.id for criterion in values}) != len(values):
            raise ValueError("completion criterion IDs must be unique")
        return values

    @field_validator("source_tools")
    @classmethod
    def selected_read_tools(cls, values):
        if values is not None and (
            len(set(values)) != len(values)
            or any(
                not 1 <= len(value) <= 240
                or "." not in value
                or any(character.isspace() for character in value)
                for value in values
            )
        ):
            raise ValueError("source tools must be unique qualified tool IDs without whitespace")
        return values

    @field_validator("disclosures", "actions", "constraints", "unknowns", "stop_conditions")
    @classmethod
    def bounded_strings(cls, values):
        if any(not x.strip() or len(x) > 180 for x in values):
            raise ValueError("brief entries must be 1–180 characters")
        return values

    @field_validator("deadline")
    @classmethod
    def aware_deadline(cls, value):
        if value.tzinfo is None:
            raise ValueError("timezone required")
        return value


class PreparedSource(StrictModel):
    """A Dot-selected excerpt, not instructions or permission to retrieve more."""

    provider: Literal["owned_fixture", "gmail", "yahoo", "memory", "plugin"]
    source_id: str = Field(min_length=1, max_length=120)
    text: str = Field(min_length=1, max_length=6000)
    truncated: bool
    verified_at: datetime
    expires_at: datetime

    @field_validator("verified_at", "expires_at")
    @classmethod
    def aware(cls, value):
        if value.tzinfo is None:
            raise ValueError("timezone required")
        return value


class PrepareCall(Brief):
    prepared_sources: list[PreparedSource] = Field(default_factory=list, max_length=4)


class Start(StrictModel):
    call_id: str
    revision: int = Field(ge=1)
    authorization_id: str


class CallRef(StrictModel):
    call_id: str


class CallHandlers(CallRef):
    decision_task_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,160}$")
    terminal_task_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,160}$")

    @model_validator(mode="after")
    def distinct_tasks(self):
        if self.decision_task_id == self.terminal_task_id:
            raise ValueError("decision and terminal tasks must be distinct")
        return self


class CallCleanup(CallHandlers):
    decision_task_enabled: Literal[False]
    terminal_task_enabled: Literal[False]


class Evidence(StrictModel):
    text: str = Field(min_length=1, max_length=240)
    quote: str = Field(min_length=1, max_length=240)
    action_name: str | None = Field(default=None, max_length=180)


class Conclusion(StrictModel):
    outcome: Literal["completed", "declined", "unresolved"]
    confirmed_facts: list[Evidence] = Field(default_factory=list, max_length=8)
    commitments: list[Evidence] = Field(default_factory=list, max_length=8)
    unresolved_questions: list[str] = Field(default_factory=list, max_length=8)

    @field_validator("unresolved_questions")
    @classmethod
    def bounded_questions(cls, values):
        if any(not x.strip() or len(x) > 240 for x in values):
            raise ValueError("unresolved questions must be 1–240 characters")
        return values


class ResultClaim(StrictModel):
    criterion_id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,39}$")
    text: str = Field(min_length=1, max_length=240)
    quote: str | None = Field(
        default=None,
        min_length=1,
        max_length=240,
        description="Exact observed recipient words; omit when using evidence_id.",
    )
    evidence_id: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{16}$",
        description="An ID from recipient_evidence in reporting feedback; use instead of quote.",
    )

    @model_validator(mode="after")
    def observed_source(self):
        if bool(self.quote) == bool(self.evidence_id):
            raise ValueError("use exactly one observed quote or evidence_id")
        return self


class TaskReport(StrictModel):
    outcome: Literal["completed", "declined", "unresolved"]
    evidence: list[ResultClaim] = Field(
        max_length=8,
        description="Cited confirmed facts only. Use [] for unresolved when no fact can be confirmed.",
    )
    unresolved_questions: list[str] = Field(default_factory=list, max_length=8)

    @model_validator(mode="before")
    @classmethod
    def omit_uncited_unresolved_claims(cls, value):
        # An uncertainty sometimes arrives in the evidence slot with quote:null.
        # It is never a confirmed fact. A separate explicit uncertainty is required
        # before dropping it, and completed/declined reports retain strict citations.
        if (
            isinstance(value, dict)
            and value.get("outcome") == "unresolved"
            and isinstance(value.get("unresolved_questions"), list)
            and 1 <= len(value["unresolved_questions"]) <= 8
            and all(
                isinstance(q, str) and q.strip() and len(q) <= 240
                for q in value["unresolved_questions"]
            )
            and isinstance(value.get("evidence"), list)
            and len(value["evidence"]) <= 8
        ):
            value = {
                **value,
                "evidence": [
                    claim
                    for claim in value["evidence"]
                    if not (
                        isinstance(claim, dict)
                        and set(claim) <= {"criterion_id", "text", "quote", "evidence_id"}
                        and claim.get("quote") is None
                        and claim.get("evidence_id") is None
                    )
                ],
            }
        return value

    @field_validator("unresolved_questions")
    @classmethod
    def bounded_questions(cls, values):
        if any(not x.strip() or len(x) > 240 for x in values):
            raise ValueError("unresolved questions must be 1–240 characters")
        return values

    @model_validator(mode="after")
    def require_report_support(self):
        if not self.evidence and (self.outcome != "unresolved" or not self.unresolved_questions):
            raise ValueError("empty evidence requires an unresolved outcome and uncertainty")
        return self


class ProposedClaim(StrictModel):
    """A claim to review after closure, never already-confirmed evidence."""

    criterion_id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,39}$")
    text: str = Field(min_length=1, max_length=240)
    quote: str | None = Field(
        default=None,
        max_length=240,
        description="Optional untrusted citation hint; closed review selects actual evidence.",
    )
    evidence_id: str | None = Field(
        default=None,
        max_length=64,
        description="Optional untrusted citation hint; never proof that a claim is supported.",
    )


class ProposedReport(StrictModel):
    """Bound autonomous finish intent without requiring live citation repair."""

    outcome: Literal["completed", "declined", "unresolved"]
    evidence: list[ProposedClaim] = Field(max_length=8)
    unresolved_questions: list[str] = Field(default_factory=list, max_length=8)

    @field_validator("unresolved_questions")
    @classmethod
    def bounded_questions(cls, values):
        if any(not x.strip() or len(x) > 240 for x in values):
            raise ValueError("unresolved questions must be 1–240 characters")
        return values

    @model_validator(mode="after")
    def require_proposal(self):
        if not self.evidence and (self.outcome != "unresolved" or not self.unresolved_questions):
            raise ValueError("empty evidence requires an unresolved outcome and uncertainty")
        return self


class Decision(StrictModel):
    call_id: str
    request_id: str
    revision: int
    answer: str = Field(min_length=1, max_length=1500)
    fact_names: list[str] = Field(default_factory=list, max_length=12)
    action_names: list[str] = Field(default_factory=list, max_length=12)
    conclusion: Conclusion | None = None
    source: PreparedSource | None = None


class Steer(StrictModel):
    call_id: str
    revision: int
    instruction: str = Field(min_length=1, max_length=1000)


def now() -> datetime:
    return datetime.now(UTC)
