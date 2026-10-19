import test from 'node:test';
import assert from 'node:assert/strict';
import {validateTicket} from './automatic_call_worker.mjs';
const entry={call_id:'a'.repeat(32),brief_hash:'b'.repeat(64),revision:1};
const ticket={schema:1,...entry,nonce:'c'.repeat(64),created_ms:1000,expires_ms:61000};
test('handoff ticket binds call, revision, scope and a bounded fresh nonce',()=>assert.equal(validateTicket(ticket,entry,2000),ticket));
for(const [name,changes] of Object.entries({scope:{brief_hash:'d'.repeat(64)},revision:{revision:2},expiry:{expires_ms:1500},future:{created_ms:9000},lifetime:{expires_ms:62000},nonce:{nonce:'bad'},extra:{approved:true}}))
  test('refuse '+name,()=>assert.throws(()=>validateTicket({...ticket,...changes},entry,2000)));
