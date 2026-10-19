# Experimental external turn control

External turn control is disabled by default. It provides a single owner for
explicit local turn boundaries; it is not a speech detector, semantic
interruption classifier, silence-recovery timer or proven fix for a real call.
The ordinary native Realtime route remains the default.

## Activation contract

`Settings.pipecat_external_turns` defaults to `False`. Enabling it requires an
`external_turn_source_factory` supplied to `PipecatTelephony`. The factory takes
the constructed language-model service and returns a Pipecat `FrameProcessor`.
It is supported only for the ordinary general-inquiry Realtime route. Do not
enable it for registered task/factory routes, alternate engines or keypad flows.
Unsupported or incomplete configuration must fail rather than silently switch
turn ownership.

The experimental route is listen-first. It does not create an opening response
merely because a session connected. The model session must acknowledge disabled
native turn detection before external control can proceed. Model connection
loss fails closed; the experimental mode does not reconnect and manufacture a
new turn. The native route retains its existing reconnect behavior.

`dots_phone.pipecat_turn_source.LocalTurnSource` is an experimental, explicitly
injected source using the pinned package's bundled Silero and SmartTurn ONNX
models. It requires no model downloads or extra model dependencies. It is not a
validated foreground-speaker or contextual-interruption classifier. A settings
change alone does not select it or make this mode ready for use. Enabling it in
a live service requires validation against the intended acoustic conditions.

The source forwards input without awaiting inference. Separate bounded workers
classify speech and completion, with original epoch/turn identities. Completion
predictions rejoin the audio FIFO so preceding input is classified before they
can apply. A completion must survive subsequent real quiet frames and classification
of all input already captured upstream, including frames waiting to enter the
source processor. The service checks that sample frontier again when it admits
the stop; a superseded decision leaves the caller's turn open. An admitted stop
is ordered before later input and cannot commit until its aggregator projection
is acknowledged. With 20 ms carrier packets and 32 ms VAD frames, this strict
coverage check can add up to 160 ms while their boundaries align. Neither wall time nor EOF supplies endpoint
evidence. Its bounded report records source coverage, inference, availability and
handling clocks without speech or audio. Closing the source also revokes the
manual turn owner, including shielded pending boundary work.

Recognized onset immediately revokes the preceding generation's eligibility to
create a response or deliver audio, even if a preceding stop is still projecting.
This revocation does not advance the turn ID. Projection acknowledgements contain
no endpoint evidence and are handled outside the inference FIFO, allowing a
pending onset to reach the existing interruption and carrier-clear barrier while
the next VAD job is blocked. No second clear path is introduced. If the preceding
stop was never admitted, the speech remains part of the same open caller turn;
deferring that stop does not permanently revoke the eventual reply to that turn.

The source has local real-model and in-memory pipeline controls for synthetic
speech, quiet stops, noise, stale decisions, pre-ready input and teardown. These
do not establish recognition of background voices or actual model answer quality.
Native inference cannot be forcibly killed by an asyncio timeout. Two shared
workers for VAD/model initialization and two for completion bound outstanding
inference across calls. A timed-out job retains its slot until it actually exits;
exhausted capacity fails the voice path explicitly. This prevents repeated stuck
jobs from spawning unbounded workers, but does not recover a permanently stuck
native worker without restarting the process.

## Turn-source interface

The pipeline captures ordered raw input before the source processor. A source
proposes a boundary with:

```python
epoch, turn_id = llm.turn_epoch, llm.manual.turn_id
# Capture both before launching asynchronous detector work, then retain them:
if llm.recognize_onset(epoch=epoch, turn_id=turn_id):
    await llm.propose_turn(started=True, epoch=epoch, turn_id=turn_id)
```

`recognize_onset` is synchronous. Call it when the detector actually recognizes
speech, before awaiting its start projection. It revokes obsolete response
eligibility without manufacturing a new turn, endpoint or input commit. The
bundled source uses it; older custom sources that only call `propose_turn` retain
their original projection-time interruption behavior.

Use `started=False` for an explicit endpoint proposal. `True` means the proposal
was applied through the aggregator lifecycle; it does not mean a commit or
response has completed. `False` covers stale, duplicate, closed or failed
proposals. Do not emit raw speaking, proposed-speaking or interruption frames:
their priority can overtake queued input. The service owns proposal handling,
ordered input dispatch, commits, response creation and interruption cleanup.

The bundled local source also passes `classified_samples` on stop proposals.
This is the absolute 8 kHz sample frontier classified by that decision, compared
with `llm.captured_input_samples` at admission. An older custom source may omit
the argument, retaining its original boundary contract; it does not gain this
additional input-coverage check.

Each source must preserve both identities captured before asynchronous work. It
must not replace an old job's tokens with the latest values to make a stale
result valid. The epoch identifies the service lifecycle; the turn ID also
rejects an old endpoint job after a newer onset within that lifecycle.
Source-audio coverage, inference availability and local handling time are
different clocks. Input EOF, elapsed wall time and absent words are not
substitutes for endpoint evidence. An uncertain endpoint must remain uncertain.

The source must forward input promptly and run expensive inference outside the
input/output critical paths with bounded work. Slow or reordered work must not
delay input dispatch or onset handling. New speech, session replacement and
teardown invalidate obsolete decisions. Use one turn authority; do not combine
external commits with native automatic turn creation.

## Mechanical guarantees and their limits

The manual service is responsible for serializing accepted boundaries with raw
input, accounting for resampler filter tails, canceling superseded work and
rejecting revoked response output. Generation completion does not mean queued
playback has drained. No-dial tests use the installed SDK, in-memory model and
carrier connections, explicit boundary proposals and fabricated model outputs.
They validate these ownership and delivery mechanics, not detector accuracy,
provider answer quality or recipient audibility.

Directed tests keep the next VAD inference blocked while checking immediate
response revocation, late old audio rejection and clearing of already queued
playback through exactly one interruption. They also require the next distinct
response to survive. Their measured onset-to-clear times describe the in-memory
pipeline; they do not establish a production network latency bound or acoustic
recognition performance.

This mode does not implement the separate reversible-playback-hold or lexical
cue prototypes. A destructive interruption does not promise literal resumption
of the old tail. It also does not justify forcing a reply while a caller turn
remains open. Unknown input and absent endpoints can still leave the service
waiting; passing a negative no-endpoint test is characterization, not successful
recovery.

## External evaluation inputs

Use only authorized inputs stored outside the checkout. The public tests use
authored events and synthetic audio; they do not include private packet traces,
recipient speech, recorded timing windows or call-specific hashes.

Preserve input ordering and measurement boundaries in any local evaluation.
Do not append silence, manufacture an endpoint or compress playback and then
claim that a detector recovered a real conversation. A scripted endpoint is a
mechanical control, not detector accuracy evidence.

## Acceptance before operational use

First obtain reviewed foreground-speaker and turn-end labels, retaining
unresolved cases. Then evaluate the proposed actual detector on held-out quiet
speech, one-word stops, continued clauses and contextual backchannels, using
captured epoch/turn tokens and distinct source-audio, inference-availability and
handling clocks to reject stale decisions. Labels alone do not make a detector
deployable. A missing endpoint must not be rescored as recovery. A useful positive result
must preserve the input order and cutoff, add no EOF padding, and
demonstrate actual media output containing the full requested content. Measure
useful-reply latency including detector inference and provider response time;
a fabricated instantaneous response cannot establish it. These requirements
define future validation, not authorization for another experiment or deployment.

Evaluate input preservation, onset yielding, obsolete-output rejection and the
eventual reply to the complete recipient request separately. A coherent clause
followed by a pause can be a legitimate early reply opportunity; the end of the
entire WAV is not automatically the only valid endpoint. The source must still
recognize and yield to substantive continuation, including quiet speech.

Preserve existing directed-interruption timing targets and their onset
definitions. Test a one-word stop, quiet continued clauses, generation-complete
but queued playback, and ambiguous acknowledgments in different conversational
contexts. Do not infer intent from duration or a lexical whitelist alone.
Record false resumption, unknown/expiry outcomes and useful-reply latency;
first-packet time is not necessarily audible speech onset.

The existing local cue scaffold has no validated confidence operating point.
Authored high-confidence hypotheses can verify control flow, but cannot stand
in for actual recognized speech. Keep real recordings private, retain uncertain
labels, and obtain held-out evidence before choosing a semantic
policy. Offline tests do not authorize provider requests, calls or deployment.
