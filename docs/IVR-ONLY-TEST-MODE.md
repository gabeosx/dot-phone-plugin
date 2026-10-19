# IVR-only test mode

See [validation and limits](VALIDATION.md) for the testing scope.
The repository contains no private-call evidence or reports.

This opt-in mode ends a call as soon as Realtime believes a live person has
answered. It disconnects silently, without an introduction, a check question,
a goodbye or a Codex round trip. Automated agents, recorded greetings,
hold announcements and promises to connect a representative should continue
through the usual automated workflow.

Human detection is a model judgment, not a guarantee. A convincing automated
agent can be mistaken for a person, and a real person can be missed. This mode
reduces human interaction during tests; it cannot guarantee nobody answers or
hears any agent speech. Detection evidence is logged as **unverified**.

Set `stop_on_human=true` in a call brief. The mode is part of its approved hash
and prep document. It is supported only on Pipecat Realtime. Normal calls default
to false and do not expose the `end_test_on_human` tool.

For temporary installation-wide testing, set:

```text
DOTS_PHONE_TEST_STOP_ON_HUMAN=true
DOTS_PHONE_TEST_RESPONSE_LIMIT=5
```

The installation flag snapshots these test settings into each **new** prepared
brief. Disabling it does not silently change already prepared or active calls.
If the call brief also supplies a nonzero response limit, preparation retains
the stricter nonzero limit. An installation value of zero cannot remove a
per-call bound.
The optional `compose.ivr-test.yaml` overlay supplies these settings. It preserves
the selected input filter; IVR testing does not enable or disable DeepFilterNet.
A limit of
five stops at the start of the fifth model response, before that response can
speak. This is an independent spending/loop bound and can stop before a human
is reached; `0` disables that response bound. The ordinary duration and monetary
caps still apply. Count the connection and all created responses in the test's
API budget, including responses cancelled by the cap.

The controller closes model input immediately and uses the normal carrier-stop
and voice-close lifecycle, retaining
`test_human_detected` or `test_response_limit` as the termination reason. Without
a previously verified business conclusion, the outcome is unresolved. Neither
a model's human hypothesis nor a successful hangup establishes task success.
Automatic model transport reconnection is disabled for these bounded tests.

Rollback: set the installation flag to false and response limit to 0, or omit
the Docker overlay, then recreate the service between calls. Cancel any unused
prepared test briefs. Explicit per-call opt-in remains available.

## Pending lookups and natural speech

The phone skill's [conversation guide](../plugins/dots-phone/skills/dots-phone/references/conversation.md)
is loaded into both the voice prompt and Codex guidance. It prefers contractions,
brief everyday speech and simple IVR replies. Permission and source-policy terms
stay internal. A real pending lookup can produce one short neutral acknowledgement
from the shared phrase library, such as “Just a sec” or “One moment,” rather than
repeated filler or a premature claim that the fact is unavailable. The controller
does not volunteer that a lookup is running. Known names and addresses are answered
directly; a pause does not need a fabricated explanation.

While a source lookup is pending, the controller restricts ordinary responses to
silent waiting and stop controls. It blocks ordinary answer audio and transcripts,
including late audio from an already blocked response. The gate releases only
when the provider acknowledges the tool result, or the request explicitly fails
or is cancelled. A repeated IVR question does not terminate the lookup. The
holding phrase uses a separate response with no call facts or tools; it is
bounded to one attempt and cannot begin after a new recipient turn or after the
lookup has resolved. Human-stop and explicit recipient stop remain available.

The holding response consumes a normal generated-response count, so it is subject
to the same per-call response cap, duration and spending bounds. That response
counter is separate from the user's allowance of additional telephone calls.
Filler does not imply the IVR will extend its timeout or understand a request to
wait. Evidence for this change is in `artifacts/lookup-wait-20261018`; both audio
fixtures repeat the postal-code question during a deliberately delayed lookup.

The shared library has 40 neutral holding phrases and 36 examples for other
conversation moments. IVR-only calls use the 20 short holding options; ordinary
calls can use all 40. Selection avoids the last twelve holding lines used by the
running process across calls. It still emits at most one line per pending lookup,
and the existing recipient-turn and stop controls remain authoritative. The other
examples are available to Realtime and Codex as optional wording for their actual
conversation context, not extra lines to insert into an IVR reply.
