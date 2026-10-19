// Subscription-owned source relay. The existing voice chooses and interprets.
import fs from 'node:fs/promises';
import {constants} from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';

const digest = value => crypto.createHash('sha256').update(value).digest('hex');
async function read(file, max=16000) {
  const h=await fs.open(file,constants.O_RDONLY|constants.O_NOFOLLOW);
  try {if((await h.stat()).size>max)throw Error('payload_too_large');return JSON.parse(await h.readFile('utf8'));}
  finally {await h.close();}
}
async function write(file,value) {
  const temp=path.join(path.dirname(file),'.trip-'+crypto.randomUUID());
  try {await fs.writeFile(temp,JSON.stringify(value),{flag:'wx',mode:0o600});await fs.rename(temp,file);}
  finally {await fs.unlink(temp).catch(()=>{});}
}
export function decodeConnector(raw) {
  if(raw?.isError)throw Error('connector_error');
  if(raw?.structuredContent)return raw.structuredContent;
  const part=raw?.content?.find(p=>p.type==='text');
  return part?JSON.parse(part.text):raw;
}
export async function executeSearchPlan(plan,call) {
  if(plan.provider==='mixed') {const results=[];for(const p of plan.plans)results.push({provider:p.provider,data:decodeConnector(await executeSearchPlan(p,call))});return {results};}
  if(plan.provider==='fixture')return plan.data;
  if(plan.provider==='gmail') {
    let data=decodeConnector(await call('gmail.search_emails',plan.args));
    if(plan.recovery_args&&Array.isArray(data.emails)&&data.emails.length===0)data=decodeConnector(await call('gmail.search_emails',plan.recovery_args));
    return data;
  }
  if(plan.provider!=='yahoo'||!Array.isArray(plan.searches)||!plan.searches.length||plan.searches.length>15)throw Error('invalid_source_search_plan');
  const responses=[];
  // Parallel calls were refused in the live subscription probe. Serialize this
  // bounded set; a connector error is never interpreted as an empty result.
  for(const args of plan.searches)responses.push(decodeConnector(await call('yahoo_dot.yahoo_list_messages',args)));
  const namespaces=new Set(responses.map(r=>r.uidValidity));
  if(namespaces.size!==1||responses.some(r=>!Array.isArray(r.messages)))throw Error('yahoo_namespace_changed');
  const messages=new Map();
  for(const response of responses)for(const message of response.messages)messages.set(message.mailbox+':'+message.uidValidity+':'+message.uid,message);
  if(!messages.size&&plan.recovery_searches)return executeSearchPlan({...plan,searches:plan.recovery_searches,recovery_searches:null},call);
  return {uidValidity:responses[0].uidValidity,messages:[...messages.values()]};
}
const addresses = from => String(from).toLowerCase().match(/[a-z0-9._+-]+@[a-z0-9.-]+\.[a-z]{2,}/g)||[];
function scopeMatches(scope,subject,date,from) {
  const at=Date.parse(date);
  return typeof subject==='string' && scope.subject_contains.some(t=>subject.toLowerCase().includes(t.toLowerCase())) &&
    Number.isFinite(at) && at>=Date.parse(scope.since+'T00:00:00Z') && at<Date.parse(scope.before+'T00:00:00Z') &&
    from.length===1 && scope.allowed_from.includes(from[0]);
}
export function sourceText(text,redactTripFields=true) {
  // Source formatting and irrelevant sensitive lines; never compute an answer.
  return String(text)
    .replace(/<(script|style)\b[^>]*>[\s\S]*?<\/\1>/gi,' ')
    .replace(/<[^>]+>/g,' ')
    .replace(/&nbsp;|&#160;/g,' ').replace(/&amp;/g,'&').replace(/&lt;/g,'<').replace(/&gt;/g,'>')
    .replace(/\p{Cf}/gu,'')
    .replace(/https?:\/\/\S+/gi,'[link]')
    .replace(/[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}/gi,match=>redactTripFields?'[email]':match)
    .split(/\r?\n/).filter(line=>!redactTripFields||!/(credit\s*card|card\s*(?:number|ending)|payment\s*method|loyalty|points\s*balance|account\s*number)/i.test(line)).join('\n')
    .replace(/[^\S\r\n]+/g,' ').replace(/\n{3,}/g,'\n\n').slice(0,30000);
}
function mimeText(part) {
  const leaves=[];
  function walk(p){if(!p||typeof p!=='object')return;if(Array.isArray(p.parts))for(const child of p.parts)walk(child);
    if(p.mime_type?.startsWith('text/')&&typeof p.body?.content==='string')leaves.push({type:p.mime_type,text:p.body.content});}
  walk(part);
  return (leaves.find(p=>p.type==='text/plain')||leaves.find(p=>p.type==='text/html'))?.text;
}
export class ConnectedTripMailWorker {
  static async create(directory,identity) {
    if(!path.isAbsolute(directory)||(await fs.lstat(directory)).isSymbolicLink())throw Error('invalid_private_directory');
    const registry=await read(path.join(directory,'registry.json'));
    if(![4,5,6].includes(registry.schema)||!Array.isArray(registry.cases)||registry.cases.length<1||registry.cases.length>2)throw Error('invalid_trip_registry');
    for(const c of registry.cases)if(!/^[a-f0-9]{32}$/.test(c.call_id)||!/^[a-f0-9]{64}$/.test(c.brief_hash)||c.revision!==1||c.provider!==(registry.schema===6?'call_sources':'connected_trip_mail')||c.max_requests!==10||!c.mail_scope||Object.keys(c.mail_scope).some(p=>!['gmail','yahoo'].includes(p))||(registry.schema===6&&(!Array.isArray(c.fixture_sources)||c.fixture_sources.length>3)))throw Error('invalid_trip_case');
    const raw=await fs.readFile(path.join(directory,'registry.json'));
    return Object.assign(new ConnectedTripMailWorker(),{directory,identity,schema:registry.schema,cases:registry.cases,registryHash:digest(raw),pending:null,inventory:new Map(),cache:new Map(),processed:new Set(),reports:[],trace:[],deadline:Date.now()+30*60*1000});
  }
  async heartbeat(serving) {
    await write(path.join(this.directory,'worker-ready.json'),{schema:1,serving,registry_hash:this.registryHash,heartbeat_ms:Date.now(),worker_identity:this.identity});
  }
  async queueStart(state) {
    if(this.schema!==4)throw Error('information_fixture_cannot_start_phone');
    const c=this.cases.find(c=>c.call_id===state.call_id);
    if(!c||state.state!=='prepared'||state.revision!==c.revision||state.brief_hash!==c.brief_hash||typeof state.authorization_id!=='string'||!state.authorization_id)throw Error('start_not_verified_or_authorized');
    const requestFile=path.join(this.directory,c.call_id+'-start-request.json'),resultFile=path.join(this.directory,c.call_id+'-start-result.json');
    if(await fs.lstat(requestFile).then(()=>true,()=>false)||await fs.lstat(resultFile).then(()=>true,()=>false))throw Error('start_already_requested');
    const begin=Date.now(),nonce=crypto.randomBytes(32).toString('hex');
    await write(requestFile,{schema:4,call_id:c.call_id,revision:c.revision,brief_hash:c.brief_hash,authorization_id:state.authorization_id,nonce,created_ms:begin,expires_ms:begin+30000});
    try {
      while(Date.now()<begin+30000){await this.heartbeat(true);let r;
        try{r=await read(resultFile);}catch(e){if(e.code!=='ENOENT')throw e;}
        if(r){if(r.schema!==4||r.call_id!==c.call_id||r.nonce!==nonce)throw Error('start_result_mismatch');if(r.error)throw Error('private_start_refused');if(r.result?.call_id!==c.call_id||!['queued','dialing','active'].includes(r.result.state))throw Error('invalid_start_state');
          return {call_id:c.call_id,state:r.result.state,revision:r.result.revision,transport:'private_local_owner_grant',started_ms:begin,ended_ms:Date.now(),controller_received_ms:r.received_ms,controller_finished_ms:r.finished_ms};}
        await new Promise(resolve=>setTimeout(resolve,10));}
      throw Error('private_start_timeout');
    }finally{await fs.unlink(requestFile).catch(()=>{});await fs.unlink(resultFile).catch(()=>{});}
  }
  async waitNext(timeoutMs=10000) {
    if(this.pending)throw Error('source_request_already_pending');
    const until=Math.min(this.deadline,Date.now()+Math.min(timeoutMs,10000));
    while(Date.now()<until) {
      if(await fs.stat(path.join(this.directory,'finished.signal')).then(()=>true,()=>false)){await this.stop();return {status:'finished'};}
      await this.heartbeat(true);
      for(const file of (await fs.readdir(this.directory)).filter(f=>/^[a-f0-9]{32}-[a-f0-9]{32}-request\.json$/.test(f))) {
        if(!this.cases.some(c=>file.startsWith(c.call_id+'-')))continue;
        if(this.processed.has(file))continue;let r;
        try{r=await read(path.join(this.directory,file));}catch(e){if(e.code==='ENOENT')continue;throw e;}
        const c=this.cases.find(c=>c.call_id===r.call_id);
        if(!c||r.schema!==this.schema||r.revision!==c.revision||file!==r.call_id+'-'+r.request_id+'-request.json'||!/^[a-f0-9]{32}$/.test(r.request_id)||!/^[a-f0-9]{64}$/.test(r.nonce)||!Number.isFinite(r.created_ms)||!Number.isFinite(r.expires_ms)||r.created_ms>Date.now()+1000||r.expires_ms-r.created_ms>61000)throw Error('invalid_source_request');
        if(Date.now()>=r.expires_ms){this.processed.add(file);continue;}
        if(this.reports.filter(x=>x.call_id===c.call_id).length>=c.max_requests)throw Error('source_request_limit');
        this.pending={...r,file,observed_ms:Date.now(),plugin_started_ms:null,plugin_finished_ms:null};
        return {status:'request',call_id:r.call_id,request_id:r.request_id,operation:r.operation,arguments:r.arguments,expires_ms:r.expires_ms};
      }
      await new Promise(resolve=>setTimeout(resolve,25));
    }
    return {status:Date.now()>=this.deadline?'finished':'waiting'};
  }
  current() {
    if(!this.pending||Date.now()>=this.pending.expires_ms)throw Error('expired_source_request');
    return this.cases.find(c=>c.call_id===this.pending.call_id);
  }
  searchPlan() {
    const c=this.current(),p=this.pending,a=p.arguments;
    if(this.schema===6) {
      if(p.operation!=='search'||Object.keys(a).join(',')!=='query'||typeof a.query!=='string'||!a.query.trim()||a.query.length>240)throw Error('search_outside_scope');
      p.plugin_started_ms=Date.now();
      const words=a.query.toLowerCase().match(/[\p{L}\p{N}_'-]+/gu)||[];
      const stop=new Set(['a','an','the','what','which','is','are','of','for','and','in','on','my','their','please','email','booking','reservation','details','show','shows','find','check']);
      const terms=words.filter(w=>!stop.has(w));
      const plans=Object.keys(c.mail_scope).map(provider=>this.providerSearch(provider,provider==='yahoo'?(terms[0]||words[0]||a.query):a.query,c.mail_scope[provider],true));
      if(c.fixture_sources.length) {
        // A failed query must remain empty. The caller, not this transport,
        // chooses recovery; require every meaningful term as real search does.
        const sources=c.fixture_sources.filter(s=>terms.length&&terms.every(w=>(s.subject+' '+s.text).toLowerCase().includes(w)));
        plans.push({provider:'fixture',data:{sources:sources.map(s=>({source_id:s.source_id}))}});
      }
      return {provider:'mixed',plans};
    }
    if(p.operation!=='search'||Object.keys(a).sort().join(',')!=='provider,query'||!['gmail','yahoo'].includes(a.provider)||!c.mail_scope[a.provider]||typeof a.query!=='string'||!a.query.trim()||a.query.length>240)throw Error('search_outside_scope');
    p.plugin_started_ms=Date.now();
    return this.providerSearch(a.provider,a.query,c.mail_scope[a.provider]);
  }
  providerSearch(provider,query,scope,recover=false) {
    if(provider==='yahoo') {
      // Apply the approved sender and subject boundary before Yahoo's result
      // limit. Otherwise a broad, reasonable query can hide every relevant hit.
      const searches=scope.allowed_from.flatMap(from=>scope.subject_contains.map(subject=>({mailbox:scope.mailbox,since:scope.since,from,subject,text:query,limit:8})));
      return {provider:'yahoo',args:searches[0],searches,...(recover?{recovery_searches:searches.map(({text,...args})=>args)}:{})};
    }
    const words=query.match(/[\p{L}\p{N}_'-]+/gu)||[];
    if(!words.length||words.length>20)throw Error('invalid_gmail_query');
    const quoted=s=>JSON.stringify(s);
    const boundary='{'+scope.allowed_from.map(s=>'from:'+s).join(' ')+'} {'+scope.subject_contains.map(s=>'subject:'+quoted(s)).join(' ')+'} after:'+scope.since.replaceAll('-','/')+' before:'+scope.before.replaceAll('-','/')+' ';
    return {provider:'gmail',args:{query:boundary+words.map(quoted).join(' ')+' -in:spam -in:trash',max_results:5},...(recover?{recovery_args:{query:boundary+'-in:spam -in:trash',max_results:5}}:{})};
  }
  async finishSearch(raw) {
    const c=this.current(),p=this.pending,data=decodeConnector(raw);
    const candidates=[];
    const batches=this.schema===6?data.results:[{provider:p.arguments.provider,data}];
    if(!Array.isArray(batches))throw Error('invalid_source_search');
    for(const batch of batches) {
    const provider=batch.provider,scope=c.mail_scope[provider],data=batch.data;
    if(provider==='fixture'&&this.schema===6) {
      for(const s of data.sources||[]) {
        const original=c.fixture_sources.find(x=>x.source_id===s.source_id);
        if(!original)throw Error('fixture_source_outside_scope');
        candidates.push({provider,raw_id:original.source_id,subject:original.subject,date:'2026-01-01T00:00:00Z',text:original.text});
      }
      continue;
    }
    if(!scope)throw Error('search_outside_scope');
    if(provider==='yahoo') {
      if(!Array.isArray(data.messages)||data.uidValidity!==scope.uidValidity)throw Error('yahoo_namespace_changed');
      for(const m of data.messages) {
        const from=(m.from||[]).map(x=>String(x.address).toLowerCase());
        if(m.mailbox!==scope.mailbox||m.uidValidity!==scope.uidValidity||!Number.isSafeInteger(m.uid)||m.uid<=0||!scopeMatches(scope,m.subject,m.date,from))continue;
        candidates.push({provider,raw_id:m.uid,uidValidity:m.uidValidity,mailbox:m.mailbox,subject:m.subject,date:m.date,from});
      }
    } else {
      if(!Array.isArray(data.emails))throw Error('invalid_gmail_search');
      for(const m of data.emails) {
        const from=addresses(m.from_);
        if(!/^[a-f0-9]{1,32}$/.test(m.id)||!scopeMatches(scope,m.subject,m.email_ts,from))continue;
        candidates.push({provider,raw_id:m.id,subject:m.subject,date:m.email_ts,from,snippet:m.snippet});
      }
    }
    }
    candidates.sort((a,b)=>Date.parse(b.date)-Date.parse(a.date));
    const messages=candidates.slice(0,5).map(m=>{
      const provider=m.provider;
      const id=provider[0]+'-'+digest(c.call_id+':'+provider+':'+m.raw_id+':'+(m.uidValidity||'')).slice(0,32);
      this.inventory.set(c.call_id+':'+id,m);
      return {message_id:id,subject:(m.date.slice(0,10)+' '+m.subject).slice(0,95),snippet:sourceText(m.snippet||'').slice(0,35)};
    });
    p.plugin_finished_ms=Date.now();return this.publish({untrusted_data:true,messages});
  }
  readPlan() {
    const c=this.current(),p=this.pending,a=p.arguments;
    if(p.operation!=='read'||Object.keys(a).sort().join(',')!=='focus,message_id,offset'||typeof a.focus!=='string'||a.focus.length>80||!Number.isInteger(a.offset)||a.offset<0||a.offset>30000)throw Error('invalid_read_arguments');
    const key=c.call_id+':'+a.message_id,m=this.inventory.get(key);
    if(!m)throw Error('read_without_actual_search');
    p.plugin_started_ms=Date.now();
    if(m.provider==='fixture') {const clean=sourceText(m.text,false);this.cache.set(key,{text:clean,hash:digest(clean),truncated:false});return {provider:'fixture',cached:true};}
    if(this.cache.has(key))return {provider:m.provider,cached:true};
    return {provider:m.provider,cached:false,args:m.provider==='gmail'?{message_id:m.raw_id,format:'full'}:{mailbox:m.mailbox,uid:m.raw_id,uidValidity:m.uidValidity}};
  }
  async finishRead(raw) {
    const c=this.current(),p=this.pending,a=p.arguments,key=c.call_id+':'+a.message_id,m=this.inventory.get(key),scope=c.mail_scope[m?.provider];
    if(!m)throw Error('source_inventory_missing');
    if(!this.cache.has(key)) {
      const data=decodeConnector(raw);let text,subject,date,from,truncated=false;
      if(m.provider==='yahoo') {
        if(data.mailbox!==m.mailbox||data.uid!==m.raw_id||data.uidValidity!==m.uidValidity||typeof data.text!=='string')throw Error('yahoo_read_mismatch');
        ({text,subject,date}=data);from=(data.from||[]).map(x=>String(x.address).toLowerCase());truncated=data.truncated===true;
      } else {
        if(data.id!==m.raw_id)throw Error('gmail_read_mismatch');
        const headers=data.payload?.headers||[],get=n=>headers.find(h=>h.name.toLowerCase()===n)?.value;
        subject=get('subject');date=new Date(Number(data.internal_date)).toISOString();from=addresses(get('from'));text=mimeText(data.payload);
        if(typeof text!=='string')throw Error('gmail_body_unavailable');
      }
      if(!scopeMatches(scope,subject,date,from)||subject!==m.subject)throw Error('read_source_outside_scope');
      const clean=sourceText(text);this.cache.set(key,{text:clean,hash:digest(clean),truncated:truncated||clean.length===30000});
    }
    const source=this.cache.get(key);
    if(a.offset>source.text.length)throw Error('read_offset_outside_source');
    let offset=a.offset,found=!a.focus;
    if(a.focus){const at=source.text.toLowerCase().indexOf(a.focus.toLowerCase(),a.offset);if(at>=0){offset=source.text.length<=850&&a.offset===0?0:Math.max(a.offset,at-100);found=true;}}
    const text=source.text.slice(offset,offset+850),end=offset+text.length;
    p.plugin_finished_ms=Date.now();
    return this.publish({untrusted_data:true,message_id:a.message_id,source_sha256:source.hash,text,offset,requested_offset:a.offset,next_offset:end<source.text.length?end:null,total_chars:source.text.length,focus:a.focus,focus_found:found,source_truncated:source.truncated});
  }
  async publish(output) {
    const c=this.current(),p=this.pending;if(JSON.stringify(output).length>1400)throw Error('source_page_too_large');
    const written=Date.now();
    const result={schema:this.schema,call_id:c.call_id,request_id:p.request_id,revision:p.revision,nonce:p.nonce,output,request_observed_ms:p.observed_ms,result_written_ms:written};
    await write(path.join(this.directory,c.call_id+'-'+p.request_id+'-result.json'),result);
    const {nonce,...safe}=result;
    this.reports.push({...safe,operation:p.operation,arguments:p.arguments,plugin_started_ms:p.plugin_started_ms,plugin_finished_ms:p.plugin_finished_ms});
    this.processed.add(p.file);this.pending=null;
    await write(path.join(this.directory,'worker-results.json'),{identity:this.identity,provider:'connected_trip_mail',reasoning:'existing_voice_model',reports:this.reports});
    return {call_id:c.call_id,request_id:p.request_id,operation:p.operation,source_returned:true,connector_ms:p.plugin_finished_ms-p.plugin_started_ms};
  }
  async stop(){this.pending=null;this.inventory.clear();this.cache.clear();await this.heartbeat(false);return {identity:this.identity,source_operations:this.reports.length,serving:false,inventory_size:0,cached_bodies:0};}
}
