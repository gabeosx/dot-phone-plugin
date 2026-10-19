// Reusable single-call controller. Requires a genuine grant and native events;
// never creates approval, subscriptions, or a second start after uncertainty.
import fs from 'node:fs/promises';
import {constants} from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {spawn} from 'node:child_process';
import {SubscriptionAppServer} from './subscription_app_server.mjs';
import {decodePhoneResult} from './prepared_phone_worker.mjs';

const terminal = state => ['completed','failed','cancelled'].includes(state?.state);
export function validateConfig(config, maxSeconds, now = Date.now()) {
  if (!/^[a-f0-9]{32}$/.test(config?.callId) || !Number.isInteger(config.revision) || config.revision < 1 ||
      !/^[a-f0-9]{64}$/.test(config.briefHash) || !Number.isFinite(Date.parse(config.deadline)) ||
      !Number.isInteger(maxSeconds) || maxSeconds < 1 || maxSeconds > 300 ||
      Date.parse(config.deadline) < now + maxSeconds * 1000) throw Error('invalid_or_expired_call_scope');
}

export async function runApprovedCall({config,maxSeconds,phone,preflight,claim,serve,
  now=Date.now,setTimer=setTimeout,clearTimer=clearTimeout}) {
  validateConfig(config,maxSeconds,now());
  const bound = state => {
    if (state.call_id !== config.callId || state.revision !== config.revision || state.brief_hash !== config.briefHash)
      throw Error('call_scope_changed');
    return state;
  };
  const report = {call_id:config.callId,revision:config.revision,brief_hash:config.briefHash,
    start_count:0,failure:null,result:null};
  let state, timer, attempted=false, timedOut=false;
  try {
    state=bound(await phone('get_call',{call_id:config.callId}));
    if (state.state!=='prepared'||!state.authorization_id) throw Error('fresh_owner_approval_required');
    await preflight(); // Authenticate sources before any paid start; no mail prefetch.
    state=bound(await phone('get_call',{call_id:config.callId}));
    if (state.state!=='prepared'||!state.authorization_id) throw Error('fresh_owner_approval_required');
    validateConfig(config,maxSeconds,now());
    await claim(); // Exclusive persistent marker. A crash cannot silently redial.
    let timeout;
    const expired=new Promise(resolve=>{timeout=resolve;});
    timer=setTimer(()=>{timedOut=true;timeout();},maxSeconds*1000);
    attempted=true;report.start_count=1;
    // Race the START too: a stuck/ambiguous start still reaches teardown.
    state=await Promise.race([
      phone('start_call',{call_id:config.callId,revision:config.revision,authorization_id:state.authorization_id}).then(bound),
      expired.then(()=>{throw Error('call_time_limit');}),
    ]);
    if (!['queued','dialing','active','awaiting_decision'].includes(state.state)) throw Error('start_not_live');
    await Promise.race([serve(),expired]);
    if(timedOut) throw Error('call_time_limit');
    state=bound(await phone('get_call',{call_id:config.callId}));
    if(!terminal(state)) throw Error('source_worker_stopped_before_call');
  } catch(error) {
    // Tool errors may contain private source data; keep operational codes only.
    const known=new Set(['call_scope_changed','fresh_owner_approval_required','invalid_or_expired_call_scope',
      'call_time_limit','start_not_live','source_worker_stopped_before_call','start_already_attempted']);
    report.failure=known.has(error.message)?error.message:'call_worker_failed';
  } finally {
    clearTimer(timer);
    if(attempted) {
      try {
        state=bound(await phone('get_call',{call_id:config.callId}));
        if(!terminal(state)) state=bound(await phone('end_call',{call_id:config.callId}));
        report.terminal_state=state.state;
        if(terminal(state)) report.result=bound(await phone('get_call_result',{call_id:config.callId}));
        else report.teardown_unconfirmed=true;
      } catch {report.teardown_unconfirmed=true;}
    }
  }
  return report;
}

async function main() {
  const [mode,configPath,duration]=process.argv.slice(2);
  if(!['preflight','run'].includes(mode)||!configPath||!path.isAbsolute(configPath)) throw Error('absolute_private_config_required');
  const directory=path.dirname(configPath),stat=await fs.lstat(directory);
  if(!stat.isDirectory()||stat.isSymbolicLink()||(stat.mode&0o077)) throw Error('private_directory_required');
  const file=await fs.open(configPath,constants.O_RDONLY|constants.O_NOFOLLOW);
  let config;
  try {const s=await file.stat();if(!s.isFile()||(s.mode&0o077)||s.size>16000)throw Error('private_config_required');config=JSON.parse(await file.readFile('utf8'));}
  finally {await file.close();}
  const maxSeconds=Number(duration);validateConfig(config,maxSeconds);
  const root=fileURLToPath(new URL('../..',import.meta.url));
  let child;
  const relay = operation => new Promise((resolve,reject)=>{
    child=spawn(process.execPath,[path.join(root,'experiments/pipecat/serve_connected_call_sources.mjs'),operation,configPath],
      {cwd:root,stdio:['ignore','pipe','pipe']});
    let output='';child.stdout.on('data',chunk=>{output+=chunk;if(output.length>4*1024*1024){child.kill();reject(Error('source_output_limit'));}});
    child.stderr.resume();
    child.on('error',()=>reject(Error('source_worker_failed')));
    child.on('exit',code=>code===0?resolve():reject(Error('source_worker_failed')));
  });
  if(mode==='preflight'){await relay('preflight');console.log(JSON.stringify({ready:true,call_id:config.callId,source_prefetch:false}));return;}
  const session=new SubscriptionAppServer();
  try {
    await session.initialize({requiredApps:['Dots Phone']});
    const client=await session.freshWorker(root,{requiredTools:['dots_phone.get_call','dots_phone.start_call','dots_phone.end_call','dots_phone.get_call_result']});
    const report=await runApprovedCall({config,maxSeconds,
      phone:async(name,args)=>decodePhoneResult(await client.call('dots_phone.'+name,args),config.callId),
      preflight:()=>relay('preflight'),serve:()=>relay('serve'),claim:async()=>{
        let marker;
        try {marker=await fs.open(path.join(directory,'worker-start-attempt.json'),'wx',0o600);}
        catch(error){if(error.code==='EEXIST')throw Error('start_already_attempted');throw error;}
        try {await marker.writeFile(JSON.stringify({call_id:config.callId,at:new Date().toISOString()}));}
        finally {await marker.close();}
      }});
    child?.kill('SIGTERM');
    const reportPath=path.join(directory,'approved-call-result.json');
    await fs.writeFile(reportPath,JSON.stringify(report,null,2),{mode:0o600,flag:'wx'});
    console.log(JSON.stringify({call_id:config.callId,state:report.terminal_state??null,failure:report.failure,report_path:reportPath}));
    if(report.failure||report.teardown_unconfirmed)process.exitCode=1;
  } finally {child?.kill('SIGTERM');session.close();}
}
if(process.argv[1]===fileURLToPath(import.meta.url))main().catch(()=>{process.stderr.write('Call worker refused or failed; inspect the private setup. No automatic retry.\n');process.exitCode=1;});
