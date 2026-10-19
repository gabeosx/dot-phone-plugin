"""Read-only information-call source relay, separate from reservation changes."""

import re
from decimal import Decimal

from dots_phone.local_trip_mail_worker import LocalTripMailWorker
from dots_phone.models import Conclusion, Evidence
from dots_phone.service import Refused

INFORMATION_CONSTRAINT = "Owned information-only mock with read-only scoped mail"
INFORMATION_ACTIONS = {"read_call_mail", "record_information_confirmation"}


class LocalCallMailWorker(LocalTripMailWorker):
    schema = 5
    read_action = "read_call_mail"
    completion_action = "record_information_confirmation"
    completion_guidance = "Ask the recipient to confirm the requested information, then quote their exact acknowledgement."

    def __init__(self, service, directory):
        super().__init__(service, directory)
        if self.automatic_handoff:
            raise Refused("information fixture cannot use an automatic phone campaign")

    def register_prepared(self, owner, call):
        # No automatic grant or registry expansion. Each fixture is registered
        # against its approved brief explicitly before a voice session starts.
        return None

    def scope(self, call):
        entry, brief = self.cases.get(call["call_id"]), call["brief"]
        if (
            not entry
            or call["owner"] != self.service.settings.owner
            or call["brief_hash"] != entry["brief_hash"]
            or call["revision"] != entry["revision"]
            or not brief["synthetic"]
            or brief["destination"] not in self.service.settings.allowed_numbers
            or brief["max_seconds"] > 180
            or Decimal(brief["budget_usd"]) > 3
            or set(brief["actions"]) != INFORMATION_ACTIONS
            or INFORMATION_CONSTRAINT not in brief["constraints"]
        ):
            raise Refused("call outside information fixture scope")
        return entry

    def conclusion(self, call, quote):
        def normalized(text):
            return re.sub(r"[^a-z0-9]", "", text.casefold())

        facts = {f["name"]: f["value"] for f in call["brief"]["facts"]}
        phrase = facts.get("information_confirmation_phrase", "")
        entries = self.service.transcripts.get(call["call_id"], [])
        actual = None
        wanted = normalized(quote)
        if phrase and wanted and normalized(phrase) in wanted:
            for entry in reversed(entries):
                at = normalized(entry["text"]).find(wanted)
                if entry["role"] != "user" or at < 0:
                    continue
                # A quoted acknowledgement inside a denial is not completion.
                # Use the whole observed turn, not only the model's substring.
                plain = re.sub(r"[^a-z0-9 ]", "", entry["text"].casefold())
                if re.search(r"\b(no|not|never|havent|cant|cannot|dont|didnt)\b", plain):
                    raise Refused("information completion lacks observed acknowledgement")
                positions = [
                    i for i, char in enumerate(entry["text"]) if char.isascii() and char.isalnum()
                ]
                actual = entry["text"][positions[at] : positions[at + len(wanted) - 1] + 1]
                break
        if not actual or len(actual) > 240:
            raise Refused("information completion lacks observed acknowledgement")
        return Conclusion(
            outcome="completed",
            confirmed_facts=[
                Evidence(text="Recipient acknowledged the requested information.", quote=actual)
            ],
            commitments=[],
            unresolved_questions=[],
        )


def call_mail_instructions(call):
    from dots_phone.voice_prompt import call_instructions

    return call_instructions(call) + (
        "\nThis is an owned information-only mock call, not a hotel reservation change. "
        "Explain the approved purpose once; answer the recipient's questions naturally. "
        "For an unknown email-dependent fact, choose search_call_mail's provider and search words yourself. "
        "Gmail combines words with AND; Yahoo uses a literal keyword or phrase. Select a relevant "
        "candidate and use read_call_mail with a focus phrase or returned next_offset. Interpret "
        "the raw source yourself. The tool already restricts sender, subject and dates. "
        "If a search is empty, remove speculative descriptive words and retry with a SINGLE "
        "distinctive event, company or topic keyword alone before declaring information unavailable. "
        "Do not stop after merely removing one word from a still over-specific query. "
        "Keep the AI/mock introduction within 25 words; explain the purpose without reciting "
        "internal approval rules or constraints. "
        "Start retrieval immediately and silently. Answer new questions from "
        "prepared evidence while retrieval continues. Never guess a pending result. "
        "After an interruption, return to ALL unresolved parts of the original question. "
        "For a comparison, pair EACH option's name with its date or value, explicitly name "
        "the winning option, and state the requested difference. Listing only dates is incomplete. "
        "When answering a different question during a lookup, answer only that question; "
        "do not append a pending-lookup status unless the recipient asks about its status. "
        "Make subsequent searches, reads and retries silently. "
        "After the recipient acknowledges the requested information, use record_information_confirmation "
        "with an exact quote of their acknowledgement. If completion_recorded is false, ask for "
        "a clear acknowledgement; do not mention internal validation or recording acknowledgements. "
        "Simply thank the recipient after confirmation. Do not interpret information "
        "confirmation as permission for a purchase, booking change, message or other real-world action. "
        "Speak concise English, answer every requested part, and yield. Never disclose unrelated "
        "mail, account details, payment or loyalty information. Use hang_up only for an explicit "
        "request to end this call, quoting the recipient's exact words."
    )


def call_mail_tools(llm, hang_up):
    import json

    from pipecat.adapters.schemas.direct_function import tool_options
    from pipecat.services.llm_service import FunctionCallParams

    @tool_options(cancel_on_interruption=False)
    async def search_call_mail(params: FunctionCallParams, provider: str, query: str):
        """Search approved mail using gmail or yahoo; choose search words from the question yourself."""
        await llm.request_supervision(
            params,
            json.dumps(
                {"operation": "search", "arguments": {"provider": provider, "query": query}}
            ),
        )

    @tool_options(cancel_on_interruption=False)
    async def read_call_mail(
        params: FunctionCallParams, message_id: str, focus: str = "", offset: int = 0
    ):
        """Read a previously found source. Interpret its text; use next_offset if more detail is needed."""
        await llm.request_supervision(
            params,
            json.dumps(
                {
                    "operation": "read",
                    "arguments": {"message_id": message_id, "focus": focus, "offset": offset},
                }
            ),
        )

    @tool_options(cancel_on_interruption=False)
    async def record_information_confirmation(params: FunctionCallParams, confirmation_quote: str):
        """Record information task completion only from the recipient's exact acknowledgement."""
        await llm.request_supervision(
            params,
            json.dumps(
                {"operation": "complete", "arguments": {"confirmation_quote": confirmation_quote}}
            ),
        )

    return [search_call_mail, read_call_mail, record_information_confirmation, hang_up]
