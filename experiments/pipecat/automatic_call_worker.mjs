// Dedicated source/control worker launched by an exact private handoff ticket.
// GPT Live is the only conversational reasoner. No model turn is needed per tool.
import fs from 'node:fs/promises';
import {constants} from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import {fileURLToPath} from 'node:url';
import {SubscriptionAppServer} from './subscription_app_server.mjs';
import {ConnectedTripMailWorker,decodeConnector,executeSearchPlan} from './connected_trip_mail_worker.mjs';
import {decodePhoneResult} from './prepared_phone_worker.mjs';

const sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms));
async function read(file,max=16000){const h=await fs.open(file,constants.O_RDONLY|constants.O_NOFOLLOW);try{const s=await h.stat();if(!s.isFile()||s.size>max||(s.mode&0o077))throw Error('unsafe_worker_file');return JSON.parse(await h.readFile('utf8'));}finally{await h.close();}}
async function write(file,data){const temp=path.join(path.dirname(file),'.launch-'+crypto.randomUUID());try{await fs.writeFile(temp,JSON.stringify(data),{flag:'wx',mode:0o600});await fs.rename(temp,file);}finally{await fs.unlink(temp).catch(()=>{});}}
export function validateTicket(ticket,entry,now=Date.now()){
  if(!ticket||Object.keys(ticket).sort().join(',')!=='brief_hash,call_id,created_ms,expires_ms,nonce,revision,schema'||ticket.schema!==1||ticket.call_id!==entry?.call_id||ticket.revision!==entry.revision||ticket.brief_hash!==entry.brief_hash||!/^[a-f0-9]{64}$/.test(ticket.nonce)||!Number.isFinite(ticket.created_ms)||!Number.isFinite(ticket.expires_ms)||ticket.created_ms>now+1000||ticket.expires_ms<=now||ticket.expires_ms-ticket.created_ms>60000||ticket.expires_ms<=ticket.created_ms)throw Error('invalid_handoff_ticket');
  return ticket;
}
export async function runCall({directory,ticket,session,cwd,now=Date.now}){
  const cid=ticket.call_id,trace=[];
  let client,worker,started=false,state=null,watch=null,failure=null;
  const terminal=s=>['completed','failed','cancelled'].includes(s);
  const call=async(name,tool,args)=>{const begin=now();const raw=await client.call(tool,args);trace.push({phase:name,started_ms:begin,ended_ms:now()});return raw;};
  try{
    client=await session.freshWorker(cwd);
    state=decodePhoneResult(await call('independent_preflight','dots_phone.get_call',{call_id:cid}),cid);
    if(state.state!=='prepared'||state.revision!==ticket.revision||state.brief_hash!==ticket.brief_hash||!state.authorization_id)throw Error('handoff_state_not_authorized');
    const profile=decodeConnector(await call('gmail_preflight','gmail.get_profile',{}));
    // The connected account is bound by the owner profile, never assumed inherited.
    const policy=await read(path.join(directory,'handoff-policy.json'));
    if(profile.email_address!==policy.gmail_account&&profile.email!==policy.gmail_account)throw Error('gmail_account_mismatch');
    await call('yahoo_inbox_warmup','yahoo_dot.yahoo_status',{}); // Auth + SELECT only; no search or fetch.
    let harness;
    for(let attempt=0;attempt<300;attempt++){
      try{harness=await read(path.join(directory,cid+'-harness-ready.json'));}catch(e){if(e.code!=='ENOENT')throw e;}
      if(harness)break;await sleep(100);
    }
    if(!harness||harness.call_id!==cid||harness.brief_hash!==ticket.brief_hash||harness.revision!==ticket.revision||harness.ready!==true||!Number.isFinite(harness.created_ms)||now()-harness.created_ms>60000||harness.created_ms>now()+1000)throw Error('owned_receiver_harness_not_ready');
    worker=await ConnectedTripMailWorker.create(directory,'automatic-subscription-worker-'+cid);
    const start=await worker.queueStart(state);started=true;trace.push({phase:'private_start',...start});
    const deadline=now()+240000;
    const nextWatch=()=>{const holder={done:false,state:null,error:null};call('watch','dots_phone.get_call',{call_id:cid}).then(raw=>{holder.state=decodePhoneResult(raw,cid);holder.done=true;},()=>{holder.error='state_watch_failed';holder.done=true;});return holder;};
    watch=nextWatch();
    while(now()<deadline){
      if(watch.done){if(watch.error)throw Error(watch.error);state=watch.state;if(terminal(state.state))break;watch=nextWatch();}
      const request=await worker.waitNext(500);
      if(request.status==='finished')throw Error('source_worker_stopped');
      if(request.status!=='request')continue;
      let plan,operation;
      if(request.operation==='search'){plan=worker.searchPlan();operation=plan.provider==='gmail'?'gmail.search_emails':'yahoo_dot.yahoo_list_messages';}
      else if(request.operation==='read'){plan=worker.readPlan();operation=plan.provider==='gmail'?'gmail.read_email':'yahoo_dot.yahoo_read_message';}
      else throw Error('unsupported_source_operation');
      let response,done=false,error;
      if(plan.cached){response=null;done=true;}else (request.operation==='search'?executeSearchPlan(plan,(tool,args)=>call('source_search',tool,args)):call('source_'+request.operation,operation,plan.args)).then(r=>{response=r;done=true;},e=>{error=e;done=true;});
      while(!done){await worker.heartbeat(true);await sleep(100);}
      if(error)throw error;
      const returned=request.operation==='search'?await worker.finishSearch(response):await worker.finishRead(response);
      trace.push({phase:'source_delivered',...returned,at_ms:now()});
    }
    if(!terminal(state?.state))throw Error('worker_deadline');
  }catch(e){failure=e.message; if(started&&!terminal(state?.state)){try{state=decodePhoneResult(await call('abort_end','dots_phone.end_call',{call_id:cid}),cid);}catch{failure='worker_failed_teardown_unconfirmed';}}}
  let result=null;
  if(terminal(state?.state)){try{result=decodePhoneResult(await call('terminal_result','dots_phone.get_call_result',{call_id:cid}),cid);}catch{failure=failure||'terminal_result_unavailable';}}
  const stopped=worker?await worker.stop():{serving:false};
  const report={call_id:cid,revision:ticket.revision,brief_hash:ticket.brief_hash,handoff_nonce:ticket.nonce,ephemeral_worker:true,authentication:'existing_chatgpt_subscription',additional_reasoning_api_calls:0,terminal_state:state?.state||null,failure,result,stopped,trace};
  await write(path.join(directory,cid+'-automatic-result.json'),report);
  return report;
}
export async function serve(directory,{session=new SubscriptionAppServer(),once=false}={}){
  if(!path.isAbsolute(directory))throw Error('absolute_private_directory_required');
  const stat=await fs.lstat(directory);if(!stat.isDirectory()||stat.isSymbolicLink()||(stat.mode&0o077))throw Error('unsafe_private_directory');
  const lock=await fs.open(path.join(directory,'launcher.lock'),constants.O_WRONLY|constants.O_CREAT|constants.O_EXCL|constants.O_NOFOLLOW,0o600);
  const ready=path.join(directory,'launcher-ready.json');
  try{
    await session.initialize();
    for(;;){
      await write(ready,{schema:1,serving:true,heartbeat_ms:Date.now(),pid:process.pid});
      if(await fs.stat(path.join(directory,'launcher-stop.signal')).then(()=>true,()=>false))break;
      const files=(await fs.readdir(directory)).filter(n=>/^[a-f0-9]{32}-handoff.json$/.test(n));
      for(const name of files){
        const file=path.join(directory,name),ticket=await read(file),registry=await read(path.join(directory,'registry.json'));
        const entry=registry.cases.find(c=>c.call_id===ticket.call_id);validateTicket(ticket,entry);
        const output=path.join(directory,ticket.call_id+'-automatic-result.json');
        if(await fs.stat(output).then(()=>true,()=>false)){await fs.unlink(file);continue;}
        // Claim before inference/tool work. A crash never silently redials.
        await fs.rename(file,path.join(directory,ticket.call_id+'-handoff-claimed.json'));
        const heartbeat=setInterval(()=>write(ready,{schema:1,serving:true,heartbeat_ms:Date.now(),pid:process.pid}).catch(()=>{}),1000);
        try{await runCall({directory,ticket,session,cwd:path.resolve(fileURLToPath(new URL('../..',import.meta.url)))});}finally{clearInterval(heartbeat);}
        if(once)return;
      }
      await sleep(100);
    }
  }finally{await write(ready,{schema:1,serving:false,heartbeat_ms:Date.now(),pid:process.pid});session.close();await lock.close();await fs.unlink(path.join(directory,'launcher.lock')).catch(()=>{});}
}
if(process.argv[1]===fileURLToPath(import.meta.url))serve(path.resolve(process.argv[2]),{once:process.argv.includes('--once')}).catch(e=>{process.stderr.write(e.message+'\n');process.exitCode=1;});
