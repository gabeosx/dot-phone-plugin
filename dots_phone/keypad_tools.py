"""Shared keypad tool contracts for phone sessions and text-only evaluations."""

from pipecat.adapters.schemas.direct_function import tool_options
from pipecat.services.llm_service import FunctionCallParams

from dots_phone.service import Refused

REASONER_KEYPAD_INSTRUCTIONS = (
    "\nFor each keypad menu or numeric verification request, use consult_call_reasoner. "
    "The reasoning agent selects and executes the control through the scoped runtime. "
    "Do not speak a key or identifier to a keypad-only system. "
    "After accepted control, stay silent until the recipient's next turn."
)


def task_keypad_tools(telephony, llm, call, tools, *, account=False):
    """Expose validated local controls unless the frozen policy delegates every turn."""
    from dots_phone.call_reasoner import requires_reasoner_consultation

    if requires_reasoner_consultation(call):
        return tools, REASONER_KEYPAD_INSTRUCTIONS
    return [*tools, *keypad_tools(telephony, llm, call, account=account)], keypad_instructions(
        account=account
    )


def keypad_tools(telephony, llm, call, *, account=False):
    def origin_arguments(params):
        origin = getattr(llm, "keypad_control_origin", None)
        return {"control_origin": origin(params.tool_call_id)} if callable(origin) else {}

    @tool_options(cancel_on_interruption=False)
    async def press_phone_key(params: FunctionCallParams, digit: str, menu_quote: str = ""):
        """Choose one heard menu key that advances the approved task. Interpret the options using the conversation. The controller attaches recipient evidence; menu_quote is an optional audit annotation. Never authorize transactions, changes or fees."""
        try:
            result = await telephony.press_phone_key(
                call["call_id"], digit, menu_quote, **origin_arguments(params)
            )
        except Refused as exc:
            result = telephony.keypad_refusal(call["call_id"], "menu", exc)
        await llm.return_local_tool_result(
            params, result, run_llm=not result["carrier_control_accepted"]
        )

    tools = [press_phone_key]
    if account:

        @tool_options(cancel_on_interruption=False)
        async def enter_verification_fact(
            params: FunctionCallParams,
            fact_name: str,
            prompt_quote: str = "",
            terminator: str = "",
            digit_offset: int = 0,
            digit_count: int | None = None,
            source_id: str = "",
            source_value: str = "",
            fact_value: str = "",
            prompt_evidence_id: str = "",
        ):
            """Select a numeric fact by meaning for a heard keypad verification request. Spoken labels need not match field names. Use fact_name for a disclosed briefing fact. If it contains text or multiple numbers, supply fact_value with the exact complete numeric span requested (for example, the postal code within an address); never combine separate spans or convert formats. For a live read source use fact_name='', source_id and source_value containing exact digits from its text, with fact_value=''. The server verifies the selected data and supplies the digits. The controller attaches current recipient evidence, including its audio-turn reference when transcription lags. prompt_quote and prompt_evidence_id are optional audit annotations; you do not need to reproduce transcript wording. Correct malformed arguments silently. Use terminator '#' or '*' only if requested, otherwise ''. For a requested portion use zero-based digit_offset and digit_count, otherwise 0 and null. Never select a PIN, password, security answer or one-time code; clarify ambiguous facts, methods or formats. The controller does not interpret language or convert formats."""
            try:
                result = await telephony.enter_verification_fact(
                    call["call_id"],
                    fact_name,
                    prompt_quote,
                    terminator=terminator,
                    digit_offset=digit_offset,
                    digit_count=digit_count,
                    source_id=source_id,
                    source_value=source_value,
                    fact_value=fact_value,
                    prompt_evidence_id=prompt_evidence_id,
                    **origin_arguments(params),
                )
            except Refused as exc:
                result = telephony.keypad_refusal(call["call_id"], "verification", exc)
            await llm.return_local_tool_result(
                params, result, run_llm=not result["carrier_control_accepted"]
            )

        tools.append(enter_verification_fact)
    return tools


def keypad_instructions(*, account=False):
    keypad_prompt = (
        "\nWhen an automated menu requires a keypad, listen to the whole menu and use "
        "press_phone_key to select a heard option that advances the approved goal. "
        "Interpret its meaning; the controller attaches recipient evidence without a wording match. "
        "Do not speak a department name to a keypad-only menu. "
        "After the tool returns, stay silent until the clerk or next menu speaks. "
        "Carrier control preserves this voice session and all booking context. "
        "Introduce yourself as AI to the clerk, after menu navigation."
    )
    if account:
        keypad_prompt += (
            "\nInterpret verification requests using the facts and conversation, including aliases and brand-specific names. "
            "Use enter_verification_fact when the recipient requests keypad input for a disclosed numeric fact; "
            "select the fact by meaning, not matching words. For a request to say, spell or confirm a value, answer aloud directly; "
            "use keypad input only when explicitly requested. "
            "Perform keypad entry and tool-argument corrections silently. Do not announce selections, digits, retries or internal validation. "
            "Choose any requested portion, terminator and existing data format yourself; the controller validates data and scope, not language. "
            "Never enter a password, PIN, security answer or one-time code. Use available source tools for missing facts. "
            "If required information remains unavailable after lookup or verification is rejected, ask for an "
            "appropriate alternative within the owner's instructions and stop conditions; if no useful route remains, stop unresolved. "
            "Carrier acceptance is not recipient verification. Wait for the actual recipient response."
        )
    return keypad_prompt
