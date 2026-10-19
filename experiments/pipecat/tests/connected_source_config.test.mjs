import test from 'node:test';
import assert from 'node:assert/strict';
import {sourceConfig,verifyGmailProfile} from '../connected_source_config.mjs';

test('Yahoo-only installs need no Gmail identity or developer exclusions',()=>{
  const c=sourceConfig({callId:'fixture'});
  assert.deepEqual(c,{callId:'fixture',exclude_terms:[]});
});
test('Gmail identity comes from authenticated profile with optional installer pin',()=>{
  assert.equal(verifyGmailProfile({email_address:'owner@example.com'}),'owner@example.com');
  assert.equal(verifyGmailProfile({email:'Owner@example.com'},'owner@example.com'),'Owner@example.com');
  assert.throws(()=>verifyGmailProfile({email:'other@example.com'},'owner@example.com'),/mismatch/);
  assert.throws(()=>verifyGmailProfile({}),/unavailable/);
});
test('malformed installer source configuration is refused',()=>{
  for(const c of [{exclude_terms:'company'},{exclude_terms:['']},{gmail_account:''}])
    assert.throws(()=>sourceConfig(c),/invalid_source_context/);
  assert.deepEqual(sourceConfig({exclude_terms:['Private department']} ).exclude_terms,['Private department']);
});
