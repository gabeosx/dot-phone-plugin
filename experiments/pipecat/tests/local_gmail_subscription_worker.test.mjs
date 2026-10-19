import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import {LocalGmailSubscriptionWorker} from '../local_gmail_subscription_worker.mjs';

const id='1'.repeat(32), rid='2'.repeat(32), nonce='3'.repeat(64), query='"dots-phone-gmail-probe-'+ '4'.repeat(32)+'"';
async function fixture(t) {
  const dir=await fs.mkdtemp(path.join(os.tmpdir(),'local-gmail-worker-'));
  t.after(()=>fs.rm(dir,{recursive:true,force:true}));
  await fs.writeFile(path.join(dir,'registry.json'),JSON.stringify({schema:1,cases:[{call_id:id,revision:1,brief_hash:'5'.repeat(64),query,narrow_tool:true}]}));
  return {dir,worker:await LocalGmailSubscriptionWorker.create(dir,'subscription-test')};
}
function request(){return{schema:1,call_id:id,request_id:rid,revision:1,nonce,query,created_ms:Date.now(),expires_ms:Date.now()+30000};}

test('serves a bound request and publishes one result without reporting its capability',async t=>{
  const{dir,worker}=await fixture(t);await fs.writeFile(path.join(dir,id+'-request.json'),JSON.stringify(request()));
  const publicRequest=await worker.waitNext();assert.equal(publicRequest.call_id,id);assert.ok(!JSON.stringify(publicRequest).includes(nonce));
  const now=Date.now();const report=await worker.publish(0,now,now);assert.ok(!JSON.stringify(report).includes(nonce));
  const result=JSON.parse(await fs.readFile(path.join(dir,id+'-result.json')));assert.equal(result.nonce,nonce);assert.equal(result.match_count,0);
  assert.equal((await fs.stat(path.join(dir,id+'-result.json'))).mode&0o777,0o600);
  assert.equal((await worker.waitNext()).status,'finished');
});

test('wrong query and expired requests cannot trigger a plugin search',async t=>{
  const{dir,worker}=await fixture(t);
  for(const change of[{query:'in:inbox'},{expires_ms:Date.now()-1},{request_id:'../escape'}]){
    await fs.writeFile(path.join(dir,id+'-request.json'),JSON.stringify({...request(),...change}));
    await assert.rejects(worker.waitNext(),/invalid_worker_request/);assert.equal(worker.pending,null);
  }
});

test('serving heartbeat is emitted from the waiting loop and finished signal stops it',async t=>{
  const{dir,worker}=await fixture(t);assert.equal((await worker.waitNext(5)).status,'waiting');
  assert.equal(JSON.parse(await fs.readFile(path.join(dir,'worker-ready.json'))).serving,true);
  await fs.writeFile(path.join(dir,'finished.signal'),'done');assert.equal((await worker.waitNext()).status,'finished');
  assert.equal(JSON.parse(await fs.readFile(path.join(dir,'worker-ready.json'))).serving,false);
});
