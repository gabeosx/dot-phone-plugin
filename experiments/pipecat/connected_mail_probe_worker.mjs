// Isolated, read-only source worker for the no-dial voice comparison.
import fs from 'node:fs/promises';
import path from 'node:path';
import crypto from 'node:crypto';
import readline from 'node:readline';
import {SubscriptionAppServer} from './subscription_app_server.mjs';
import {ConnectedTripMailWorker,decodeConnector,executeSearchPlan} from './connected_trip_mail_worker.mjs';

const [directory,policyFile]=process.argv.slice(2);
const policyInfo=await fs.lstat(policyFile);
if(!policyInfo.isFile()||(policyInfo.mode&0o077)||!path.isAbsolute(directory))throw Error('owner_only_probe_scope_required');
const policy=JSON.parse(await fs.readFile(policyFile,'utf8'));
const session=new SubscriptionAppServer();
await session.initialize();
const client=await session.freshWorker(process.cwd());
const cid=crypto.randomBytes(16).toString('hex');
await fs.mkdir(directory,{recursive:true,mode:0o700});
await fs.writeFile(path.join(directory,'registry.json'),JSON.stringify({schema:4,cases:[{call_id:cid,revision:1,brief_hash:'a'.repeat(64),provider:'connected_trip_mail',max_requests:10,mail_scope:policy.mail_scope}]}),{mode:0o600});
const worker=await ConnectedTripMailWorker.create(directory,'no-dial-profiling-worker');
let sequence=0;
function pending(operation,args) {
  if(sequence>=64)throw Error('probe_source_limit');
  worker.pending={schema:4,call_id:cid,revision:1,request_id:crypto.randomBytes(16).toString('hex'),nonce:crypto.randomBytes(32).toString('hex'),operation,arguments:args,observed_ms:Date.now(),expires_ms:Date.now()+30000,file:'probe-'+(++sequence)};
}
async function readSource(args,trace) {
  pending('read',args);
  const plan=worker.readPlan(),start=Date.now();
  const raw=plan.cached?null:await client.call(plan.provider==='gmail'?'gmail.read_email':'yahoo_dot.yahoo_read_message',plan.args);
  await worker.finishRead(raw);
  trace.push({operation:'read',provider:plan.provider,cached:plan.cached,elapsed_ms:Date.now()-start,started_epoch_ms:start,finished_epoch_ms:Date.now()});
  return worker.reports.at(-1).output;
}
console.log(JSON.stringify({ready:true}));
try {
  for await(const line of readline.createInterface({input:process.stdin})) {
    const request=JSON.parse(line),trace=[];
    try {
      if(request.operation==='reset') {worker.inventory.clear();worker.cache.clear();console.log(JSON.stringify({id:request.id,output:{reset:true},trace}));continue;}
      if(request.operation==='warm') {const start=Date.now();await client.call('yahoo_dot.yahoo_status',{});console.log(JSON.stringify({id:request.id,output:{ready:true},trace:[{operation:'warm',elapsed_ms:Date.now()-start}]}));continue;}
      let output;
      if(request.operation==='read') output=await readSource(request.args,trace);
      else if(request.operation==='search') {
        pending('search',{provider:request.args.provider,query:request.args.query});
        const plan=worker.searchPlan(),start=Date.now();
        const raw=await executeSearchPlan(plan,(tool,args)=>client.call(tool,args));
        await worker.finishSearch(raw);trace.push({operation:'search',provider:plan.provider,elapsed_ms:Date.now()-start,started_epoch_ms:start,finished_epoch_ms:Date.now()});
        output=worker.reports.at(-1).output;
        // Read only an unambiguous scoped candidate. Never guess an answer or
        // choose an arbitrary first result to make the latency number better.
        if(request.fused && output.messages.length===1) {
          const source=await readSource({message_id:output.messages[0].message_id,focus:request.args.focus||'',offset:0},trace);
          output={...output,source};
        }
      } else throw Error('unsupported_probe_operation');
      console.log(JSON.stringify({id:request.id,output,trace}));
    } catch {console.log(JSON.stringify({id:request.id,error:'scoped_source_request_failed',trace}));}
  }
} finally {await worker.stop();session.close();}
