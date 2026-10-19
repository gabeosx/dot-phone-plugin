"""Coordinate pending retrieval with fresh turns and one spoken acknowledgement."""

from __future__ import annotations

import asyncio
import json
import secrets
import time

from pipecat.services.openai.realtime import events

from dots_phone.speech_style import choose_holding_phrase


class LookupWait:
    def __init__(self, llm):
        self.llm = llm
        self.pending = {}
        self.pending_turns = {}
        self.response_turns = {}
        self.blocked_responses = set()
        self.task = None
        self.tokens = {}
        self.holding_responses = {}
        self.last_holding_phrase = None

    def model_led(self):
        call = self.llm.supervisor.store.get("call", self.llm.call_id)
        return bool(
            call
            and call.get("runtime_supervision") == "local_reasoner"
            and call.get("reasoner_response_policy") == "direct_with_codex"
        )

    def policy(self, tools):
        if self.model_led():
            # A dependency belongs to the question that triggered it, not to
            # every future turn. Keep ordinary tools usable for new questions;
            # finish_call remains controller-refused while anything is pending.
            return {
                "tools": [t for t in tools if t.get("name") != "finish_call"],
                "tool_choice": "auto",
            }
        return {
            "tools": [
                t
                for t in tools
                if t.get("name")
                in {
                    "wait_for_recipient",
                    "end_test_on_human",
                    "end_test_at_stop_condition",
                    "hang_up",
                }
            ],
            "tool_choice": "required",
        }

    async def refresh(self):
        llm = self.llm
        if llm.closing or llm.stop_requested or not llm._websocket or not llm._context:
            return
        await llm.send_client_event(
            events.SessionUpdateEvent(
                session=events.SessionProperties(
                    instructions=llm._settings.system_instruction,
                )
            )
        )

    async def begin(self, tool_id):
        first = not self.pending
        if tool_id not in self.pending:
            self.pending[tool_id] = False
            self.pending_turns[tool_id] = self.llm.reasoner_turn
            # Responses already underway cannot become a continuation of a
            # result they never received, even after the result is acknowledged.
            self.blocked_responses.update(self.response_turns)
        await self.refresh()
        if first and (self.task is None or self.task.done()):
            self.task = asyncio.create_task(self.hold(self.llm.reasoner_turn))

    async def finish(self, tool_id):
        # A successful send remains gated until the provider acknowledges it.
        if tool_id in self.pending and not self.pending[tool_id]:
            self.pending.pop(tool_id)
            self.pending_turns.pop(tool_id, None)
            await self.refresh()

    def sent(self, tool_id):
        if tool_id in self.pending:
            self.pending[tool_id] = True

    async def acknowledged(self, tool_id):
        if tool_id in self.pending:
            self.pending.pop(tool_id)
            self.pending_turns.pop(tool_id, None)
            await self.refresh()

    def created(self, response):
        self.response_turns[response["id"]] = self.llm.reasoner_turn
        token = (response.get("metadata") or {}).get("lookup_hold")
        turn = self.tokens.pop(token, None) if isinstance(token, str) else None
        if turn is not None:
            if not self.pending or turn != self.llm.reasoner_turn or self.llm.stop_requested:
                return False
            self.holding_responses[response["id"]] = turn
        return self.allowed(response["id"])

    def allowed(self, response_id):
        if response_id in self.holding_responses:
            return self.holding_responses[response_id] == self.llm.reasoner_turn
        if response_id in self.blocked_responses:
            return False
        if not self.pending:
            return True
        turn = self.response_turns.get(response_id)
        newest_dependency = max(
            self.pending_turns.get(tool_id, self.llm.reasoner_turn) for tool_id in self.pending
        )
        if self.model_led() and turn == self.llm.reasoner_turn and turn > newest_dependency:
            # The model can address a fresh question from known evidence while
            # retrieval for an earlier question continues independently.
            return True
        self.blocked_responses.add(response_id)
        return False

    async def hold(self, turn):
        llm = self.llm
        try:
            await asyncio.sleep(0.8)
            while self.pending and not llm.closing and not llm.stop_requested:
                # Never start a holding phrase after a new recipient turn. It
                # could be a person in human-stop mode, or a changed question.
                if turn != llm.reasoner_turn:
                    return
                if (
                    llm.caller_speaking
                    or llm.active_responses
                    or llm.pending_response_requests
                    or llm._current_audio_response is not None
                    or time.monotonic() - llm.caller_stopped_at < 0.1
                ):
                    await asyncio.sleep(0.02)
                    continue
                token = secrets.token_hex(16)
                self.tokens[token] = turn
                call = llm.supervisor.store.get("call", llm.call_id)
                if not call or call["state"] not in {"active", "awaiting_decision"}:
                    self.tokens.pop(token, None)
                    return
                phrase = choose_holding_phrase(ivr_only=call["brief"].get("stop_on_human", False))
                self.last_holding_phrase = phrase
                # Out-of-band generation receives no call context or facts.
                # This acknowledgement belongs only to the triggering turn.
                payload = {
                    "type": "response.create",
                    "response": {
                        "conversation": "none",
                        "metadata": {"lookup_hold": token},
                        "input": [],
                        "output_modalities": ["audio"],
                        "instructions": (
                            "Say exactly this short phone acknowledgement in a relaxed, casual voice: "
                            + json.dumps(phrase)
                            + " Say nothing else. Use the final_answer phase."
                        ),
                        "tools": [],
                        "tool_choice": "none",
                        "max_output_tokens": 256,
                    },
                }
                llm.pending_response_requests += 1
                try:
                    await llm._websocket.send(json.dumps(payload))
                except Exception:
                    llm.pending_response_requests -= 1
                    self.tokens.pop(token, None)
                    raise
                llm.stage("lookup_holding_requested", None)
                return
        except asyncio.CancelledError:
            raise
        except Exception:
            # A holding-phrase failure does not authorize a substantive answer.
            llm.voice_failure("lookup_holding_send")

    async def close(self):
        if self.task and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        self.pending.clear()
        self.pending_turns.clear()
        self.response_turns.clear()
        self.blocked_responses.clear()
        self.tokens.clear()
