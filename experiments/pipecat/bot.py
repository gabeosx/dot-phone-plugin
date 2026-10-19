"""Small GPT-Live agent; no carrier, owner configuration or production state."""

import asyncio
import base64
import re
import time
from dataclasses import asdict, dataclass, field

from pipecat.evals.serializer import EvalSerializer
from pipecat.evals.transport import EvalTransport, EvalTransportParams
from pipecat.frames.frames import EndFrame, LLMRunFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker, ProcessorUnusablePolicy
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair
from pipecat.services.llm_service import FunctionCallParams
from pipecat.services.openai.live import events
from pipecat.services.openai.live.llm import OpenAILiveLLMService, _correlation_key
from pipecat.services.openai.responses.llm import OpenAIResponsesLLMService
from pipecat.workers.runner import WorkerRunner

from audio_metrics import pcm_fields

FRONTEND = """You are a concise voice assistant in a synthetic museum test.
When asked for a long story, tell a detailed story for ten seconds ONLY if uninterrupted.
As soon as the caller starts speaking, immediately yield, even mid-sentence.
Interruption always overrides story length. Never finish the old sentence after an interruption.
Follow the latest request. Do not narrate the lookup or add a holding sentence.
Delegate any request to look up opening hours. Never guess those hours.
When the backend supplies a closing time, say that time FIRST, without any preamble.
Ending the call requires the backend's end_call tool. You cannot hang up yourself.
When the caller asks to hang up, delegate to the backend with the explicit request
to invoke end_call. Saying goodbye or promising to wrap up does not end the call.
Delegate first, then say a short goodbye. This rule is mandatory.
Speak English. No personal data or real actions exist here."""
BACKEND = """Use lookup_hours for the museum's closing time; its result is authoritative.
Return ONLY the tool's closes value, in its original words. Use place='museum'.
Use end_call when the user asks to end the call. Do not call any other tool."""


def error_details(error, key):
    message = error.message or ""
    if key:
        message = message.replace(key, "[REDACTED]")
    message = re.sub(r"sk-[A-Za-z0-9_-]{20,}", "[REDACTED]", message)
    return {
        "code": error.code,
        "type": error.type,
        "param": error.param,
        "message": message[:512],
    }


@dataclass
class Evidence:
    session_id: str | None = None
    closed: bool = False
    final_seconds: float | None = None
    close_reason: str | None = None
    close_sent: bool = False
    calls: list = field(default_factory=list)
    timeline: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    backend_responses: int = 0
    backend_usage: list = field(default_factory=list)
    origin: float = field(default_factory=time.monotonic)

    def mark(self, kind, **fields):
        event = {"kind": kind, "at_ms": round((time.monotonic() - self.origin) * 1000, 2), **fields}
        self.timeline.append(event)
        return event

    def report(self):
        return {k: v for k, v in asdict(self).items() if k != "origin"}


class MeasuredLiveService(OpenAILiveLLMService):
    """Pinned adapter hook: retain final closure/usage rather than infer it from disconnect.

    Pipecat 1.12 has no on_session_closed event and only logs Live seconds.
    Keep this hook small and cover it with protocol tests before upgrading.
    """

    def __init__(self, *, evidence, **kwargs):
        self.evidence = evidence
        self.ready = asyncio.Event()
        self.active_responses = set()
        self.pending_continuations = 0
        self.backend_changed = asyncio.Event()
        self.next_input_at = None
        self.transcript_observer = None
        super().__init__(**kwargs)

    async def send_client_event(self, event):
        if isinstance(event, events.InputAudioAppendEvent):
            if self.evidence.close_sent:
                return
            pcm = base64.b64decode(event.audio)
            now = time.monotonic()
            self.next_input_at = max(now, self.next_input_at or now)
            if self.next_input_at > now:
                await asyncio.sleep(self.next_input_at - now)
            if self.evidence.close_sent:
                self.evidence.mark("input_suppressed_at_close")
                return
            self.next_input_at += len(pcm) / 48000
        self.evidence.mark(
            "client_event", event_type=type(event).__name__, event_id=event.event_id
        )
        if isinstance(event, events.ResponseCreateEvent):
            self.pending_continuations += 1
            self.backend_changed.set()
        if isinstance(event, events.SessionCloseEvent):
            self.evidence.close_sent = True
            self.evidence.mark("close_requested")
        if isinstance(event, events.InputAudioAppendEvent):
            pcm = base64.b64decode(event.audio)
            capture = getattr(self.evidence, "capture", None)
            fields = capture.append("input", pcm) if capture else {}
            self.evidence.mark("input_audio", **pcm_fields(pcm), **fields)
        await super().send_client_event(event)
        if isinstance(event, (events.ResponseItemCreateEvent, events.ResponseCreateEvent)):
            self.evidence.mark("backend_command_sent", event_type=event.type, event_id=event.event_id)

    async def _send_user_audio(self, frame):
        # RTVI can announce readiness before the model session is started.
        # Buffer the first audio frame instead of dropping the caller's words;
        # provider events arrive on an independent receive task.
        await asyncio.wait_for(self.ready.wait(), timeout=10)
        await super()._send_user_audio(frame)

    async def drain_delegations(self):
        async def wait_until_settled():
            while self.active_responses or self.pending_continuations or self._open_function_calls:
                self.backend_changed.clear()
                await self.backend_changed.wait()

        try:
            await asyncio.wait_for(wait_until_settled(), timeout=5)
            self.evidence.mark("delegations_drained")
        except TimeoutError:
            self.evidence.errors.append({"code": "delegation_drain_timeout"})

    async def _handle_server_event(self, event):
        if isinstance(event, events.SessionStartedEvent):
            self.evidence.session_id = event.session.id
            self.evidence.mark("session_started")
            self.ready.set()
        elif isinstance(event, events.ContextAppendedEvent):
            self.evidence.mark(
                "context_acknowledged",
                event_type=event.type,
                client_event_id=event.client_event_id,
                start_ms=event.start_ms,
                end_ms=event.end_ms,
            )
        elif isinstance(event, events.SessionClosedEvent):
            self.evidence.closed = True
            self.evidence.close_reason = event.reason
            self.evidence.final_seconds = event.usage.seconds if event.usage else None
            self.evidence.mark("closed", seconds=self.evidence.final_seconds)
        elif isinstance(event, events.TranscriptDeltaEvent):
            self.evidence.mark(
                "caption",
                role=event.role,
                text=event.delta,
                start_ms=event.start_ms,
                end_ms=event.end_ms,
            )
            if event.role == "user" and self.transcript_observer:
                self.transcript_observer(event.delta)
        elif isinstance(event, events.OutputAudioDeltaEvent):
            pcm = base64.b64decode(event.delta)
            capture = getattr(self.evidence, "capture", None)
            fields = capture.append("output", pcm) if capture else {}
            self.evidence.mark("output_audio", **pcm_fields(pcm), **fields)
        elif isinstance(event, events.ErrorEvent):
            details = error_details(event.error, self.api_key)
            self.evidence.errors.append(details)
            self.evidence.mark("provider_error", **details)
        elif isinstance(event, events.ResponseEventEnvelope):
            self.evidence.mark(
                "backend_event",
                event_type=event.inner_type,
                delegation_id=event.delegation_id,
                response_id=event.event.get("response_id")
                or (event.event.get("response") or {}).get("id"),
                service_tier=(event.event.get("response") or {}).get("service_tier"),
            )
            if event.inner_type == "response.created":
                self.active_responses.add(_correlation_key(event))
                self.pending_continuations = max(0, self.pending_continuations - 1)
                self.evidence.backend_responses += 1
                if self.evidence.backend_responses > 4:
                    self.evidence.errors.append({"code": "backend_response_limit"})
                    await self.send_client_event(events.SessionCloseEvent())
            elif event.inner_type == "response.completed":
                usage = (event.event.get("response") or {}).get("usage")
                if usage:
                    self.evidence.backend_usage.append(usage)
            if event.inner_type in {"response.completed", "response.failed", "response.incomplete"}:
                self.active_responses.discard(_correlation_key(event))
        await super()._handle_server_event(event)
        self.backend_changed.set()


def build_bot(*, key, base_url, evidence, port, backend_model="gpt-5.6-terra"):
    transport = EvalTransport(
        host="127.0.0.1",
        port=port,
        params=EvalTransportParams(
            audio_in_enabled=True, audio_out_enabled=True, serializer=EvalSerializer()
        ),
    )
    llm = MeasuredLiveService(
        evidence=evidence,
        api_key=key,
        base_url=base_url,
        settings=OpenAILiveLLMService.Settings(model="gpt-live-1", system_instruction=FRONTEND),
        delegation=OpenAILiveLLMService.ResponsesDelegation(
            settings=OpenAIResponsesLLMService.Settings(
                model=backend_model,
                system_instruction=BACKEND,
                max_completion_tokens=256,
                reasoning=OpenAIResponsesLLMService.ReasoningConfig(effort="low"),
            ),
        ),
    )

    async def lookup_hours(params: FunctionCallParams, place: str):
        """Look up fictional closing hours.

        Args:
            place: The fictional place, museum.
        """
        evidence.calls.append({"name": "lookup_hours", "place": place})
        evidence.mark("tool_call", name="lookup_hours", place=place)
        await params.result_callback({"place": place, "closes": "five PM", "synthetic": True})

    async def end_call(params: FunctionCallParams):
        """End this synthetic conversation after the caller asks to hang up."""
        evidence.calls.append({"name": "end_call"})
        evidence.mark("tool_call", name="end_call")
        await params.result_callback({"ending": True})
        # The orchestrator drains the worker with EndFrame after the eval
        # sees the tool event. Never use the eval's stop_bot cancellation.

    context = LLMContext([], [lookup_hours, end_call])
    user, assistant = LLMContextAggregatorPair(context)
    worker = PipelineWorker(
        Pipeline([transport.input(), user, llm, transport.output(), assistant]),
        params=PipelineParams(
            audio_in_sample_rate=24000,
            audio_out_sample_rate=24000,
            enable_metrics=True,
            enable_usage_metrics=True,
        ),
        idle_timeout_secs=60,
        processor_unusable_policy=ProcessorUnusablePolicy.END,
    )
    runner = WorkerRunner(handle_sigint=False)

    @transport.event_handler("on_client_connected")
    async def connected(transport, client):
        await worker.queue_frames([LLMRunFrame()])

    async def run():
        await runner.add_workers(worker)
        await runner.run()

    async def stop():
        # A function_call assertion sees the start of a tool, before its
        # result and continuation finish. Drain that work before session.close;
        # the API rejects all client events once closing begins.
        await llm.drain_delegations()
        await worker.queue_frames([EndFrame()])

    return run, stop, worker
