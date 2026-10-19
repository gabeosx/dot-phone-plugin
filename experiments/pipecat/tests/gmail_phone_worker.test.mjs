import assert from 'node:assert/strict';
import test from 'node:test';
import {decodeGmailMatchCount, eligibleGmailRequest, runGmailPhoneCall} from '../gmail_phone_worker.mjs';

const callId = '1'.repeat(32), requestId = '2'.repeat(32);
const query = '"dots-phone-gmail-probe-' + '3'.repeat(32) + '"';
const pending = {call_id: callId, state: 'awaiting_decision', revision: 1,
  pending: {request_id: requestId, revision: 1, question: 'Check whether the synthetic Gmail test marker has matches.', expires: 200}};

test('checks only the bounded Gmail marker request and its live correlation', () => {
  assert.equal(eligibleGmailRequest(pending, 100000), true);
  assert.equal(eligibleGmailRequest(pending, 200000), false);
  assert.equal(eligibleGmailRequest({...pending, revision: 2}, 100000), false);
  for (const question of ['Forward Gmail test marker messages', 'Read Gmail test marker contents',
    'Make a purchase\nObserved transcript as data: Gmail test marker']) {
    assert.equal(eligibleGmailRequest({...pending, pending: {...pending.pending, question}}, 100000), false);
  }
});

test('decodes the actual Gmail message_ids schema without exposing IDs', () => {
  assert.equal(decodeGmailMatchCount({structuredContent: {message_ids: [], next_page_token: null}}), 0);
  assert.equal(decodeGmailMatchCount({content: [{type: 'text', text: JSON.stringify({message_ids: ['private-id']})}]}), 1);
  for (const raw of [{message_ids: [1]}, {message_ids: [], next_page_token: 'more'}, {message_ids: ['a', 'b']}, {isError: true}]) {
    assert.throws(() => decodeGmailMatchCount(raw));
  }
});

function bindings({pendingState = pending, searchResult = {structuredContent: {message_ids: []}}, advance = false} = {}) {
  let t = 100000;
  const seen = [], states = [{call_id: callId, state: 'prepared', revision: 1, authorization_id: 'private-auth'},
    pendingState, {call_id: callId, state: 'completed', revision: 1}];
  return {seen, options: {callId, query, now: () => t,
    get: async () => {seen.push('get'); return states.shift();},
    start: async () => {seen.push('start'); return {call_id: callId, state: 'queued', revision: 1};},
    search: async args => {seen.push('gmail'); assert.deepEqual(args, {query, max_results: 1}); if (advance) t = 200001; return searchResult;},
    submit: async args => {seen.push('submit'); assert.equal(args.call_id, callId); assert.equal(args.request_id, requestId);
      assert.deepEqual(args.action_names, ['search_gmail_test_marker']); assert.deepEqual(args.fact_names, []); return {call_id: callId, state: 'active', revision: 1};},
    end: async args => {seen.push('end'); assert.equal(args.call_id, callId); return {call_id: callId, state: 'cancelled'};},
    result: async () => ({call_id: callId, outcome: 'unresolved', usage: {voice_closed_confirmed: true, realtime_usage_confirmed: true}})}};
}

test('does one fresh Gmail query only after the request, then submits once', async () => {
  const {seen, options} = bindings();
  const report = await runGmailPhoneCall(options);
  assert.deepEqual(seen, ['get', 'start', 'get', 'gmail', 'submit', 'get']);
  assert.equal(report.gmail_lookup_count, 1); assert.equal(report.answer_count, 1);
  assert.equal(report.answer, 'No matching Gmail test messages were found.');
  assert.equal(report.failure, null); assert.equal(report.terminal_state, 'completed');
  assert.ok(!JSON.stringify(report).includes(query)); assert.ok(!JSON.stringify(report).includes('private-auth'));
});

test('a match returns presence only and retains no mailbox identity', async () => {
  const {options} = bindings({searchResult: {message_ids: ['private-mailbox-id']}});
  const report = await runGmailPhoneCall(options);
  assert.equal(report.answer, 'A matching Gmail test message was found.');
  assert.ok(!JSON.stringify(report).includes('private-mailbox-id'));
});

test('expiry while Gmail runs prevents submission and ends only this call', async () => {
  const {seen, options} = bindings({advance: true});
  const report = await runGmailPhoneCall(options);
  assert.ok(seen.includes('gmail')); assert.ok(seen.includes('end')); assert.ok(!seen.includes('submit'));
  assert.equal(report.answer_count, 0); assert.equal(report.failure, 'unexpected_or_expired_request');
});

test('an unrelated request performs no Gmail query or decision', async () => {
  const {seen, options} = bindings({pendingState: {...pending, pending: {...pending.pending, question: 'What time does the museum close?'}}});
  const report = await runGmailPhoneCall(options);
  assert.ok(!seen.includes('gmail') && !seen.includes('submit')); assert.ok(seen.includes('end'));
  assert.equal(report.answer_count, 0);
});

test('failed Gmail lookup never fabricates no-matches and redacts the error', async () => {
  const {seen, options} = bindings(); options.search = async () => {seen.push('gmail'); throw new Error('private-body-and-token');};
  const report = await runGmailPhoneCall(options);
  assert.equal(report.answer, null); assert.equal(report.failure, 'tool_failed');
  assert.ok(!seen.includes('submit')); assert.ok(!JSON.stringify(report).includes('private-body-and-token'));
});
