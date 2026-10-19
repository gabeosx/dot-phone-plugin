// No phone tools or model reasoning: serve an already registered audio fixture.
import fs from 'node:fs/promises';
import path from 'node:path';
import {SubscriptionAppServer} from './subscription_app_server.mjs';
import {ConnectedTripMailWorker,decodeConnector,executeSearchPlan} from './connected_trip_mail_worker.mjs';

const [directory,account,delayArg='0',durationArg='180']=process.argv.slice(2);
const delay=Number(delayArg);
const duration=Number(durationArg);
if(!path.isAbsolute(directory)||!Number.isInteger(delay)||delay<0||delay>12000||!Number.isInteger(duration)||duration<1||duration>300)throw Error('invalid_fixture_scope');
let session;
let worker;
const trace=[];
const sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms));
try {
  worker=await ConnectedTripMailWorker.create(directory,'information-audio-subscription-worker');
  if(![4,5,6].includes(worker.schema))throw Error('audio_source_fixture_required');
  let client;
  if(worker.cases.some(c=>Object.keys(c.mail_scope).length)) {
    session=new SubscriptionAppServer();await session.initialize();client=await session.freshWorker(process.cwd());
    const profile=decodeConnector(await client.call('gmail.get_profile',{}));
    if(profile.email_address!==account&&profile.email!==account)throw Error('gmail_account_mismatch');
  }
  await worker.heartbeat(true);
  process.stdout.write(JSON.stringify({ready:true})+'\n');
  const deadline=Date.now()+duration*1000;
  while(Date.now()<deadline) {
    const request=await worker.waitNext(500);
    if(request.status==='finished')break;
    if(request.status!=='request')continue;
    let plan,tool;
    if(request.operation==='search'){plan=worker.searchPlan();tool=plan.provider==='gmail'?'gmail.search_emails':'yahoo_dot.yahoo_list_messages';}
    else if(request.operation==='read'){plan=worker.readPlan();tool=plan.provider==='gmail'?'gmail.read_email':'yahoo_dot.yahoo_read_message';}
    else throw Error('unsupported_source_operation');
    const start=Date.now();
    let response,done=plan.cached,error;
    if(!done)(request.operation==='search'?executeSearchPlan(plan,(name,args)=>client.call(name,args)):client.call(tool,plan.args)).then(r=>{response=r;done=true;},e=>{error=e;done=true;});
    while(!done){await worker.heartbeat(true);await sleep(50);}
    if(error)throw error;
    const connectorFinished=Date.now();
    if(request.operation==='read') {
      const until=Date.now()+delay;
      while(Date.now()<until){await worker.heartbeat(true);await sleep(Math.min(100,until-Date.now()));}
    }
    const result=request.operation==='search'?await worker.finishSearch(response):await worker.finishRead(response);
    trace.push({operation:request.operation,request_id:request.request_id,started_ms:start,connector_finished_ms:connectorFinished,connector_ms:connectorFinished-start,injected_delay_ms:request.operation==='read'?delay:0,published_ms:Date.now(),source_returned:result.source_returned});
  }
} finally {
  const stopped=worker?await worker.stop():null;
  await fs.writeFile(path.join(directory,'source-timing.json'),JSON.stringify({trace,stopped,additional_reasoning_api_calls:0}),{mode:0o600});
  session?.close();
}
