"""Pinned Pipecat Realtime adapter for bounded synthetic carrier comparisons."""

import asyncio
import json
import time

from dots_phone.pipecat_transport import LiveInputBuffer
from pipecat.audio.utils import create_stream_resampler
from pipecat.frames.frames import InputAudioRawFrame
from pipecat.services.openai.realtime import events
from pipecat.services.openai.realtime.llm import OpenAIRealtimeLLMService


class ObservedSocket:
    def __init__(self, socket, observe, sent):
        self.socket, self.observe, self.sent = socket, observe, sent

    def __getattr__(self, name):
        return getattr(self.socket, name)

    async def __aiter__(self):
        async for message in self.socket:
            self.observe(json.loads(message))
            yield message

    async def send(self, message):
        await self.socket.send(message)
        self.sent(json.loads(message))


class MeasuredRealtime(OpenAIRealtimeLLMService):
    def __init__(self, *, evidence, **kwargs):
        self.evidence = evidence
        self.ready = asyncio.Event()
        self.input_queue = LiveInputBuffer(self.ready)
        self.input_task = None
        self.input_resampler = create_stream_resampler()
        self.active_responses = set()
        self.response_usage = {}
        self.responses_created = set()
        self.pending_requests = 0
        self.tool_names = {}
        self.transmitted_tools = set()
        self.on_lookup_result_ready = None
        super().__init__(**kwargs)

    def observe(self, event):
        kind = event.get("type")
        if kind in {"response.created", "response.done"}:
            response = event["response"]
            sid = response["id"]
            self.evidence.mark("realtime_response", event_type=kind, response_id=sid, status=response.get("status"))
            if kind == "response.created":
                self.responses_created.add(sid)
                self.active_responses.add(sid)
                self.pending_requests = max(0, self.pending_requests - 1)
            else:
                self.active_responses.discard(sid)
                self.response_usage[sid] = response.get("usage")
                if response.get("status") in {"failed", "incomplete"}:
                    self.evidence.errors.append({"code": "realtime_response_" + response["status"]})
        elif kind == "error":
            code = (event.get("error") or {}).get("code")
            if code in {"response_cancel_not_active", "conversation_already_has_active_response"}:
                self.evidence.mark("realtime_control_race", code=code)
                if code == "conversation_already_has_active_response":
                    self.pending_requests = max(0, self.pending_requests - 1)
            else:
                self.evidence.errors.append({"code": code or "realtime_error"})
        elif kind in {"conversation.item.added", "response.output_item.added"}:
            item = event.get("item") or {}
            if item.get("type") == "function_call" and item.get("call_id"):
                self.tool_names[item["call_id"]] = item.get("name")

    def sent(self, event):
        if event.get("type") == "response.create":
            self.evidence.mark("backend_command_sent", event_type="response.create")
        elif event.get("type") == "conversation.item.create" and event.get("item", {}).get("type") == "function_call_output":
            call_id = event["item"]["call_id"]
            self.transmitted_tools.add(call_id)
            self.evidence.mark("backend_command_sent", event_type="response.item.create")
            self.evidence.mark("tool_output_transmitted", name=self.tool_names.get(call_id), call_id=call_id)

    async def _receive_task_handler(self):
        self._websocket = ObservedSocket(self._websocket, self.observe, self.sent)
        await super()._receive_task_handler()

    async def _handle_evt_session_updated(self, event):
        await super()._handle_evt_session_updated(event)
        self.evidence.session_id = event.session.id
        self.evidence.mark("session_started", engine="realtime")
        self.ready.set()

    async def send_client_event(self, event):
        if self.evidence.close_sent or self._websocket is None:
            self.evidence.mark("command_suppressed_at_close", event_type=event.type)
            return
        if isinstance(event, events.ResponseCreateEvent):
            self.pending_requests += 1
        await super().send_client_event(event)

    async def deliver_tool_result(self, params, result):
        # Keep the normal context record, but do not wait for its path through
        # paced output before returning the result to the provider. Mark it as
        # delivered so the later context frame cannot send it twice.
        if getattr(params, "function_name", None):
            self.tool_names[params.tool_call_id] = params.function_name
        deadline = time.monotonic() + 3
        while self.active_responses:
            if time.monotonic() >= deadline:
                self.evidence.errors.append({"code": "tool_delivery_active_response_timeout"})
                return
            await asyncio.sleep(0.01)
        if self.evidence.close_sent:
            return
        if self.tool_names.get(params.tool_call_id) == "lookup_hours" and self.on_lookup_result_ready:
            await self._truncate_current_audio_response()
            await self.on_lookup_result_ready()
        # Reserve before enqueueing the normal context callback. If context
        # travels quickly, it must not race us to send a second tool output.
        self._completed_tool_calls.add(params.tool_call_id)
        await params.result_callback(result)
        await self._send_tool_result(params.tool_call_id, json.dumps(result))
        await self._create_response()

    async def _send_user_audio(self, frame):
        if self.evidence.close_sent:
            return
        self.input_queue.put_nowait(frame)
        if self.input_task is None:
            self.input_task = self.create_task(self.dispatch_input(), "realtime-carrier-input")

    async def dispatch_input(self):
        await asyncio.wait_for(self.ready.wait(), timeout=10)
        while not self.evidence.close_sent:
            frame = await self.input_queue.get()
            audio = await self.input_resampler.resample(frame.audio, frame.sample_rate, 24000)
            if audio:
                await super()._send_user_audio(InputAudioRawFrame(audio, 24000, 1))
            self.input_queue.dispatched(frame)

    async def drain_delegations(self):
        # Let the queued final tool result reach the context/continuation first.
        deadline = time.monotonic() + 8
        await asyncio.sleep(0.25)
        while self.active_responses or self.pending_requests or self._function_call_tasks:
            if time.monotonic() >= deadline:
                self.evidence.errors.append({"code": "realtime_drain_timeout"})
                return
            await asyncio.sleep(0.02)
        self.evidence.mark("delegations_drained")

    def usage_report(self):
        confirmed = bool(self.responses_created) and self.responses_created == set(self.response_usage) and all(self.response_usage.values())
        return {
            "engine": "realtime",
            "usage_confirmed": confirmed,
            "response_count": len(self.responses_created),
            "response_usage": self.response_usage,
            "tool_outputs_transmitted": bool(self.tool_names) and set(self.tool_names) <= self.transmitted_tools,
            "transmitted_tool_names": sorted(self.tool_names[sid] for sid in self.transmitted_tools if self.tool_names.get(sid)),
            "closure_basis": "WebSocket close acknowledged after responses drained; Realtime has no GPT-Live session.closed duration event",
        }

    async def stop(self, frame):
        self.evidence.close_sent = True
        if self.input_task:
            await self.cancel_task(self.input_task)
            self.input_task = None
        await super().stop(frame)

    async def _disconnect(self):
        socket = self._websocket
        await super()._disconnect()
        if socket is not None:
            self.evidence.closed = socket.close_code in {1000, 1001}
            self.evidence.mark("closed", engine="realtime", websocket_close_code=socket.close_code)
