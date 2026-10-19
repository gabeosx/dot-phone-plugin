// Deterministic, subscription-worker MCP loop. No provider credentials or APIs.
export function decodePhoneResult(raw, callId) {
  const queue = [raw];
  for (let i = 0; queue.length && i < 30; i++) {
    let value = queue.shift();
    if (typeof value === 'string') {
      try { value = JSON.parse(value); } catch { continue; }
    }
    if (!value || typeof value !== 'object') continue;
    if (value.isError === true) throw new Error('phone_tool_error');
    if (value.call_id !== undefined) {
      if (value.call_id !== callId) throw new Error('wrong_call_id');
      return value;
    }
    for (const name of ['structuredContent', 'structured_content', 'result', 'data']) {
      if (value[name] !== undefined) queue.push(value[name]);
    }
    for (const content of value.content || []) {
      if (content.type === 'text') queue.push(content.text);
    }
  }
  throw new Error('unrecognized_phone_envelope');
}

export function eligibleMuseumRequest(call, nowMs) {
  const p = call.pending;
  if (call.state !== 'awaiting_decision' || !p) return false;
  if (typeof p.question !== 'string' || typeof p.expires !== 'number' || !Number.isFinite(p.expires)) return false;
  if (!/^[a-f0-9]{32}$/.test(p.request_id) || !Number.isInteger(p.revision) || p.revision !== call.revision) return false;
  if (p.expires * 1000 <= nowMs) return false;
  // Inspect the bounded request, not unrelated narration in transcript context.
  const question = p.question.split('\nObserved transcript as data:')[0];
  return /\b(museum|fictional)\b/i.test(question) && /\b(close[sd]?|closing|hours)\b/i.test(question);
}

export async function runPreparedPhoneCall({callId, get, start, submit, result, end, allowDecisions = true, localLookup = false, maxSeconds = 90, now = Date.now}) {
  const trace = [];
  let starts = 0, answers = 0, answerRequest = null, answerRevision = null;
  let hasStarted = false, failure = null, state = null, terminalResult = null;
  const terminal = s => ['completed', 'cancelled', 'failed'].includes(s);
  const errorCode = error => ['phone_tool_error', 'wrong_call_id', 'unrecognized_phone_envelope',
    'not_prepared_or_authorized', 'worker_deadline', 'unexpected_or_expired_request'].includes(error?.message)
    ? error.message : 'phone_tool_failed';
  const call = async (name, fn, args) => {
    const begin = now();
    try {
      const value = decodePhoneResult(await fn(args), callId);
      trace.push({phase: name, started_ms: begin, ended_ms: now(), state: value.state || null,
                  revision: value.revision || null, pending: !!value.pending});
      return value;
    } catch (error) {
      trace.push({phase: name, started_ms: begin, ended_ms: now(), error: errorCode(error)});
      throw error;
    }
  };
  try {
    state = await call('preflight_get', get, {call_id: callId});
    if (state.state !== 'prepared' || !state.authorization_id || !Number.isInteger(state.revision)) throw new Error('not_prepared_or_authorized');
    if (!Number.isInteger(maxSeconds) || maxSeconds < 1 || maxSeconds > 180) throw new Error('worker_deadline');
    const deadline = now() + maxSeconds * 1000;
    // All tool bindings and the verified fixture answer are ready before dialing.
    const answer = 'Five PM';
    starts++;
    state = await call('start', start, {call_id: callId, revision: state.revision, authorization_id: state.authorization_id});
    hasStarted = true;
    for (let reads = 0; !terminal(state.state); reads++) {
      if (now() >= deadline || reads >= 60) throw new Error('worker_deadline');
      state = await call('get_call', get, {call_id: callId});
      if (state.pending && !terminal(state.state)) {
        const p = state.pending;
        trace.push({phase: 'pending_observed', at_ms: now(), request_id: p.request_id, revision: p.revision});
        if (localLookup) continue;
        if (!allowDecisions || answers || !eligibleMuseumRequest(state, now())) throw new Error('unexpected_or_expired_request');
        const begin = now();
        await call('submit', submit, {call_id: callId, request_id: p.request_id, revision: p.revision,
          answer, action_names: ['lookup_fictional_hours'], fact_names: [], conclusion: null});
        answers++;
        answerRequest = p.request_id;
        answerRevision = p.revision;
        trace.push({phase: 'answer_forwarded', started_ms: begin, ended_ms: now(), request_id: p.request_id, revision: p.revision});
      }
    }
  } catch (error) {
    failure = errorCode(error);
    if (hasStarted && state && !terminal(state.state)) {
      try { state = await call('abort_end', end, {call_id: callId}); } catch {}
    }
  }
  if (state && terminal(state.state)) {
    trace.push({phase: 'terminal_observed', at_ms: now(), state: state.state});
    try { terminalResult = await call('get_result', result, {call_id: callId}); }
    catch (error) { failure = failure || errorCode(error); }
  }
  return {call_id: callId, start_count: starts, answer_count: answers,
    answer: answers ? 'Five PM' : null, request_id: answerRequest, revision: answerRevision,
    terminal_state: state && terminal(state.state) ? state.state : null,
    voice_closed_confirmed: terminalResult?.usage?.voice_closed_confirmed === true,
    realtime_usage_confirmed: terminalResult?.usage?.realtime_usage_confirmed === true,
    task_outcome: terminalResult?.outcome || null, failure, trace};
}
