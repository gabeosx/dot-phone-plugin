// Invoked by the sole dedicated call worker after start_call. No start/redial,
// credentials, paid reasoning API, mailbox prefetch, or transcript interpretation.
import fs from 'node:fs/promises';
import path from 'node:path';
import {constants} from 'node:fs';
import {sourceConfig,verifyGmailProfile} from './connected_source_config.mjs';
import {SubscriptionAppServer} from './subscription_app_server.mjs';
import {decodeConnector} from './connected_trip_mail_worker.mjs';
import {decodePhoneResult} from './prepared_phone_worker.mjs';
import {ConnectedCallSources,runConnectedSourceLoop} from './connected_call_sources.mjs';

const [mode,configPath]=process.argv.slice(2);
if(!['preflight','serve'].includes(mode)||!configPath||!path.isAbsolute(configPath))throw Error('private_config_required');
const file=await fs.open(configPath,constants.O_RDONLY|constants.O_NOFOLLOW);
let config;
try {
  const stat=await file.stat();
  if(!stat.isFile()||(stat.mode&0o077)||stat.size>16000)throw Error('private_config_required');
  config=JSON.parse(await file.readFile('utf8'));
} finally {await file.close();}
config=sourceConfig(config);
const session=new SubscriptionAppServer();
try {
  await session.initialize({requiredApps:['Dots Phone']});
  const client=await session.freshWorker(process.cwd(),{requiredTools:[
    'dots_phone.get_call','dots_phone.get_call_result','dots_phone.submit_call_decision']});
  // Limit the injected transport even if the connected client has other tools.
  const allowed=new Set(['gmail.search_emails','gmail.read_email','yahoo_dot.yahoo_list_messages','yahoo_dot.yahoo_read_message']);
  const calls=[];
  const sourceCall=async(name,args)=>{
    if(!allowed.has(name))throw Error('read_only_source_tool_required');
    const at=new Date().toISOString();
    try {const result=await client.call(name,args);calls.push({at,tool:name,arguments:args,status:'returned'});return result;}
    catch(e){calls.push({at,tool:name,arguments:args,status:'error'});throw e;}
  };
  const phone=async(name,args)=>decodePhoneResult(await client.call('dots_phone.'+name,args),config.callId);
  const get=()=>phone('get_call',{call_id:config.callId});
  const state=await get();
  if(state.revision!==config.revision||state.brief_hash!==config.briefHash)throw Error('call_scope_changed');
  const providers=[];
  if(['gmail.get_profile','gmail.search_emails','gmail.read_email'].every(n=>client.availableTools.includes(n))) {
    let profile;
    try {profile=decodeConnector(await client.call('gmail.get_profile',{}));} catch {}
    if(profile) {
      verifyGmailProfile(profile,config.gmail_account);
      providers.push('gmail');
    }
  }
  if(['yahoo_dot.yahoo_status','yahoo_dot.yahoo_list_messages','yahoo_dot.yahoo_read_message'].every(n=>client.availableTools.includes(n))) {
    let yahoo;
    try {yahoo=decodeConnector(await client.call('yahoo_dot.yahoo_status',{}));} catch {}
    if(yahoo?.imapAuthenticated===true&&yahoo.mailboxes?.some(m=>(typeof m==='string'?m:m.path)==='INBOX'))providers.push('yahoo');
  }
  // Mail is optional. With no authenticated connector, supervise the call and
  // return explicit unavailable coverage rather than blocking prepared facts.
  const relay=new ConnectedCallSources({...config,providers,call:sourceCall,allowSource:source=>
    !config.exclude_terms.some(term=>JSON.stringify(source).toLowerCase().includes(term.toLowerCase()))});
  process.stdout.write(JSON.stringify({ready:true,call_id:config.callId,mode,providers,source_prefetch:false})+'\n');
  if(mode==='serve') {
    if(!['queued','dialing','active','awaiting_decision'].includes(state.state))throw Error('already_started_call_required');
    try {
      const report=await runConnectedSourceLoop({relay,get,
        submit:args=>phone('submit_call_decision',args),
        result:()=>phone('get_call_result',{call_id:config.callId})});
      process.stdout.write(JSON.stringify(report)+'\n');
    } finally {
      if(config.trace_path) {
        if(!path.isAbsolute(config.trace_path))throw Error('private_trace_path_required');
        await fs.writeFile(config.trace_path,JSON.stringify({call_id:config.callId,calls},null,2),{flag:'wx',mode:0o600});
      }
    }
  }
} finally {session.close();}
