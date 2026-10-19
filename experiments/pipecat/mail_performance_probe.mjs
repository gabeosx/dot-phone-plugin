// Read-only profiling: no model turn, writes, phone call, or public mail output.
import fs from 'node:fs/promises';
import crypto from 'node:crypto';
import {pathToFileURL} from 'node:url';
import {performance} from 'node:perf_hooks';
import {SubscriptionAppServer} from './subscription_app_server.mjs';
import {decodeConnector,sourceText} from './connected_trip_mail_worker.mjs';

const hash = value => crypto.createHash('sha256').update(String(value)).digest('hex');
export async function measure(name, operation, rows) {
  const start = performance.now();
  try {
    const value = await operation();
    rows.push({name, elapsed_ms: +(performance.now()-start).toFixed(3), success: true});
    return value;
  } catch (error) {
    rows.push({name, elapsed_ms: +(performance.now()-start).toFixed(3), success: false});
    throw error;
  }
}
export function variants(config) {
  const base={mailbox:config.mailbox, since:config.since, limit:8};
  return [
    {name:'body',args:{...base,text:config.query}},
    {name:'subject',args:{...base,subject:config.query}},
    {name:'sender_body',args:{...base,from:config.from,text:config.query}},
  ];
}
async function run(config, mode) {
  const stages=[],samples=[],reads=[];
  let mail, session, client;
  if(mode==='native'||mode==='idle'||mode==='source') {
    const {YahooMail}=await import(pathToFileURL(config.mail_module));
    mail=new YahooMail({email:process.env.YAHOO_EMAIL,password:process.env.YAHOO_APP_PW});
    const original=mail.client.bind(mail);
    mail.client=()=>{
      const c=original();
      if(typeof config.enable_auto_idle==='boolean') c.options.disableAutoIdle=!config.enable_auto_idle;
      for(const name of ['connect','getMailboxLock','search','fetchAll','fetchOne']) {
        const method=c[name].bind(c);
        c[name]=(...args)=>measure(name,()=>method(...args),stages);
      }
      return c;
    };
    client={call:async(name,args)=> name.endsWith('list_messages') ? mail.list(args) : mail.read(args)};
  } else if(mode==='subscription') {
    session=new SubscriptionAppServer();
    await session.initialize();
    client=await session.freshWorker(process.cwd());
  } else throw Error('invalid_probe_mode');
  try {
    if(mode==='idle') {
      await mail.mailboxState(config.mailbox);
      const warmClient=mail.readClient;
      await new Promise(resolve=>setTimeout(resolve,config.idle_ms));
      const retained=mail.readClient===warmClient&&warmClient.usable;
      const start=performance.now(),stageStart=stages.length;
      const result=await mail.list(variants(config)[0].args);
      return {schema:1,mode,recorded_at:new Date().toISOString(),reasoning_api_calls:0,phone_calls:0,idle_ms:config.idle_ms,enable_auto_idle:!!config.enable_auto_idle,retained_client:retained,elapsed_ms:+(performance.now()-start).toFixed(3),returned:result.messages.length,stages:stages.slice(stageStart),samples:[],reads:[]};
    }
    let actual;
    const options=variants(config);
    for(let repeat=0;repeat<config.repeats;repeat++) {
      // Rotate ordering; record first/cold separately instead of discarding it.
      for(let j=0;j<options.length;j++) {
        const v=options[(repeat+j)%options.length],start=performance.now(),stageStart=stages.length;
        const result=decodeConnector(await client.call('yahoo_dot.yahoo_list_messages',v.args));
        if(result.uidValidity!==config.uidValidity)throw Error('mailbox_namespace_changed');
        const messages=result.messages.filter(m=>m.subject.toLowerCase().includes(config.query.toLowerCase()) && m.from?.some(a=>a.address===config.from));
        const includesTarget=messages.some(m=>m.uid===config.read_uid);
        samples.push({variant:v.name,repeat,elapsed_ms:+(performance.now()-start).toFixed(3),returned:result.messages.length,scoped_matches:messages.length,includes_target:includesTarget,ids_hash:hash(messages.map(m=>m.uid).sort().join(',')),stages:stages.slice(stageStart)});
        if(includesTarget)actual={mailbox:config.mailbox,uid:config.read_uid,uidValidity:result.uidValidity};
      }
    }
    if(!actual)throw Error('target_not_found_in_actual_search');
    for(let repeat=0;repeat<config.repeats;repeat++) {
      const start=performance.now(),stageStart=stages.length;
      const result=decodeConnector(await client.call('yahoo_dot.yahoo_read_message',actual));
      if(result.uid!==actual.uid||result.uidValidity!==actual.uidValidity||typeof result.text!=='string')throw Error('invalid_actual_read');
      reads.push({repeat,elapsed_ms:+(performance.now()-start).toFixed(3),text_chars:result.text.length,source_hash:hash(result.text),stages:stages.slice(stageStart)});
      if(mode==='source')return {schema:1,mode,samples,reads,source:{untrusted_data:true,provider:'yahoo',subject:result.subject,source_sha256:hash(sourceText(result.text)),text:sourceText(result.text),source_truncated:result.truncated===true,prepared_at:new Date().toISOString()}};
    }
    return {schema:1,mode,recorded_at:new Date().toISOString(),reasoning_api_calls:0,phone_calls:0,samples,reads};
  } finally {await mail?.close();session?.close();}
}
async function main() {
  const [configPath,mode,output]=process.argv.slice(2);
  const info=await fs.lstat(configPath);
  if(!info.isFile()||(info.mode&0o077))throw Error('private_probe_config_required');
  const config=JSON.parse(await fs.readFile(configPath,'utf8'));
  if(!Number.isInteger(config.repeats)||config.repeats<1||config.repeats>10)throw Error('invalid_repeat_limit');
  const result=await run(config,mode);
  await fs.writeFile(output,JSON.stringify(result,null,2),{mode:0o600,flag:'wx'});
  console.log(JSON.stringify({mode,samples:result.samples.length,reads:result.reads.length,output}));
}
if(process.argv[1] && import.meta.url===pathToFileURL(process.argv[1]).href)main().catch(()=>{console.error('Read-only mail probe failed; no mail or credentials printed.');process.exitCode=1;});
