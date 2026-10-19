import assert from 'node:assert/strict';
import test from 'node:test';
import {runApprovedCall,validateConfig} from '../approved_call_worker.mjs';

const config={callId:'1'.repeat(32),revision:1,briefHash:'2'.repeat(64),deadline:'2030-01-01T00:00:00Z'};
const state=(status,extra={})=>({call_id:config.callId,revision:1,brief_hash:config.briefHash,state:status,...extra});
function setup(overrides={}){
  let current=state('prepared',{authorization_id:'private-fixture'});
  const calls=[];
  const deps={config,maxSeconds:300,claim:async()=>calls.push('claim'),preflight:async()=>calls.push('preflight'),
    serve:async()=>{calls.push('serve');current=state('completed');},
    phone:async(name,args)=>{
      calls.push(name);assert.equal(args.call_id,config.callId);
      if(name==='start_call'){assert.equal(args.authorization_id,'private-fixture');current=state('queued');}
      if(name==='end_call')current=state('cancelled');
      return name==='get_call_result'?{...current,outcome:'completed'}:current;
    },...overrides};
  return {deps,calls,setState:s=>current=s};
}

test('authenticates sources before one start and independently retrieves bound result',async()=>{
  const {deps,calls}=setup();const r=await runApprovedCall(deps);
  assert.equal(r.failure,null);assert.equal(r.result.state,'completed');assert.equal(r.start_count,1);
  assert(calls.indexOf('preflight')<calls.indexOf('start_call'));
  assert.deepEqual(calls,['get_call','preflight','get_call','claim','start_call','serve','get_call','get_call','get_call_result']);
  assert(!JSON.stringify(r).includes('private-fixture'));
});
test('missing approval, changed scope, source failure and existing marker never start',async()=>{
  for(const change of ['approval','scope','source','marker']){
    const {deps,calls,setState}=setup();
    if(change==='approval')setState(state('prepared'));
    if(change==='scope')setState({...state('prepared'),brief_hash:'3'.repeat(64)});
    if(change==='source')deps.preflight=async()=>{throw Error('private source details');};
    if(change==='marker')deps.claim=async()=>{throw Error('start_already_attempted');};
    const r=await runApprovedCall(deps);assert(r.failure);assert.equal(r.start_count,0);
    assert(!calls.includes('start_call'));assert(!calls.includes('end_call'));
    assert(!JSON.stringify(r).includes('private source details'));
  }
});
test('ambiguous start is ended without a redial',async()=>{
  const {deps,calls}=setup();const phone=deps.phone;
  deps.phone=async(name,args)=>{if(name==='start_call'){calls.push(name);throw Error('lost response');}return phone(name,args);};
  const r=await runApprovedCall(deps);
  assert.equal(r.start_count,1);assert.equal(calls.filter(x=>x==='start_call').length,1);
  assert(calls.includes('end_call'));assert.equal(r.terminal_state,'cancelled');
});
test('watchdog covers a hanging start and closes that exact call',async()=>{
  let expire;const {deps,calls}=setup({setTimer:fn=>{expire=fn;return 1;},clearTimer:()=>{}});const phone=deps.phone;
  deps.phone=async(name,args)=>{if(name==='start_call'){calls.push(name);queueMicrotask(expire);return new Promise(()=>{});}return phone(name,args);};
  const r=await runApprovedCall(deps);assert.equal(r.failure,'call_time_limit');assert(calls.includes('end_call'));
});
test('relay failure and changed terminal result cannot be reported as success',async()=>{
  const {deps,calls}=setup({serve:async()=>{throw Error('relay failed');}});
  const r=await runApprovedCall(deps);assert(r.failure);assert(calls.includes('end_call'));
  const clean=setup();const phone=clean.deps.phone;
  clean.deps.phone=async(name,args)=>name==='get_call_result'?{...state('completed'),revision:2}:phone(name,args);
  const changed=await runApprovedCall(clean.deps);assert.equal(changed.result,null);assert(changed.teardown_unconfirmed);
});
test('rejects expired or overlong scopes before tools',()=>{
  assert.throws(()=>validateConfig(config,301),/scope/);
  assert.throws(()=>validateConfig({...config,deadline:'2000-01-01T00:00:00Z'},300),/scope/);
});
