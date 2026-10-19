// One fresh, fixed Gmail marker search during one owner-authorized phone call.
// The subscription worker owns both tools; neither mailbox IDs nor message text
// enter the voice service or the redacted report.
import {decodePhoneResult} from './prepared_phone_worker.mjs';

export function eligibleGmailRequest(call, nowMs) {
  const p = call.pending;
  if (call.state !== 'awaiting_decision' || !p || typeof p.question !== 'string') return false;
  if (!Number.isFinite(p.expires) || p.expires * 1000 <= nowMs) return false;
  if (!/^[a-f0-9]{32}$/.test(p.request_id) || !Number.isInteger(p.revision) || p.revision !== call.revision) return false;
  const question = p.question.split('\nObserved transcript as data:')[0];
  return /\bgmail\b/i.test(question) && /\b(test|synthetic)\b/i.test(question) && /\bmarker\b/i.test(question)
    && !/\b(send|forward|delete|archive|purchase|transfer|password|credentials|body|contents)\b/i.test(question);
}

export function decodeGmailMatchCount(raw) {
  const queue = [raw];
  for (let i = 0; queue.length && i < 30; i++) {
    let value = queue.shift();
    if (typeof value === 'string') {
      try { value = JSON.parse(value); } catch { continue; }
    }
    if (!value || typeof value !== 'object') continue;
    if (value.isError === true) throw new Error('gmail_tool_error');
    if ('message_ids' in value) {
      if (!Array.isArray(value.message_ids) || value.message_ids.length > 1 ||
          !value.message_ids.every(id => typeof id === 'string' && id.length > 0) ||
          (value.message_ids.length === 0 && value.next_page_token)) throw new Error('invalid_gmail_result');
      return value.message_ids.length;
    }
    for (const name of ['structuredContent', 'structured_content', 'result', 'data']) {
      if (value[name] !== undefined) queue.push(value[name]);
    }
    for (const item of value.content || []) if (item.type === 'text') queue.push(item.text);
  }
  throw new Error('invalid_gmail_result');
}

export async function runGmailPhoneCall({callId, query, get, start, submit, search, result, end, now = Date.now}) {
  if (!/^[a-f0-9]{32}$/.test(callId) || !/^"dots-phone-gmail-probe-[a-f0-9]{32}"$/.test(query)) throw new Error('invalid_test_scope');
  const trace = [];
  let starts = 0, lookups = 0, answers = 0, matchCount = null, answer = null, requestId = null;
  let state = null, hasStarted = false, failure = null, terminalResult = null;
  const terminal = s => ['completed', 'cancelled', 'failed'].includes(s);
  const safeError = e => ['phone_tool_error', 'wrong_call_id', 'unrecognized_phone_envelope',
    'not_prepared_or_authorized', 'worker_deadline', 'unexpected_or_expired_request',
    'gmail_tool_error', 'invalid_gmail_result'].includes(e?.message) ? e.message : 'tool_failed';
  const phone = async (phase, fn, args) => {
    const begin = now();
    try {
      const v = decodePhoneResult(await fn(args), callId);
      trace.push({phase, started_ms: begin, ended_ms: now(), state: v.state || null,
                  revision: v.revision || null, pending: !!v.pending});
      return v;
    } catch (e) {
      trace.push({phase, started_ms: begin, ended_ms: now(), error: safeError(e)});
      throw e;
    }
  };
  try {
    state = await phone('preflight_get', get, {call_id: callId});
    if (state.state !== 'prepared' || !state.authorization_id || !Number.isInteger(state.revision)) throw new Error('not_prepared_or_authorized');
    const deadline = now() + 90000;
    starts++;
    state = await phone('start', start, {call_id: callId, revision: state.revision, authorization_id: state.authorization_id});
    hasStarted = true;
    for (let reads = 0; !terminal(state.state); reads++) {
      if (now() >= deadline || reads >= 60) throw new Error('worker_deadline');
      state = await phone('get_call', get, {call_id: callId});
      if (state.pending && !terminal(state.state)) {
        const p = state.pending;
        trace.push({phase: 'pending_observed', at_ms: now(), request_id: p.request_id, revision: p.revision});
        if (lookups || answers || !eligibleGmailRequest(state, now())) throw new Error('unexpected_or_expired_request');
        const begin = now();
        lookups++;
        try {
          // Only this prepared constant controls Gmail; caller speech never does.
          matchCount = decodeGmailMatchCount(await search({query, max_results: 1}));
          trace.push({phase: 'gmail_search', started_ms: begin, ended_ms: now(), match_count: matchCount});
        } catch (e) {
          trace.push({phase: 'gmail_search', started_ms: begin, ended_ms: now(), error: safeError(e)});
          throw e;
        }
        if (!eligibleGmailRequest(state, now()) || now() >= deadline) throw new Error('unexpected_or_expired_request');
        answer = matchCount === 0 ? 'No matching Gmail test messages were found.' : 'A matching Gmail test message was found.';
        await phone('submit', submit, {call_id: callId, request_id: p.request_id, revision: p.revision,
          answer, action_names: ['search_gmail_test_marker'], fact_names: [], conclusion: null});
        answers++;
        requestId = p.request_id;
      }
    }
  } catch (e) {
    failure = safeError(e);
    if (hasStarted && state && !terminal(state.state)) {
      try { state = await phone('abort_end', end, {call_id: callId}); } catch {}
    }
  }
  if (state && terminal(state.state)) {
    trace.push({phase: 'terminal_observed', at_ms: now(), state: state.state});
    try { terminalResult = await phone('get_result', result, {call_id: callId}); }
    catch (e) { failure = failure || safeError(e); }
  }
  return {call_id: callId, start_count: starts, gmail_lookup_count: lookups, answer_count: answers,
    match_count: matchCount, answer: answers ? answer : null, request_id: requestId,
    terminal_state: state && terminal(state.state) ? state.state : null,
    voice_closed_confirmed: terminalResult?.usage?.voice_closed_confirmed === true,
    realtime_usage_confirmed: terminalResult?.usage?.realtime_usage_confirmed === true,
    task_outcome: terminalResult?.outcome || null, failure, trace};
}
