// Private question/result transport. Search filters and answers come from the agent.
import fs from 'node:fs/promises';
import {constants} from 'node:fs';
import crypto from 'node:crypto';
import path from 'node:path';

async function read(file) {
  const h = await fs.open(file, constants.O_RDONLY | constants.O_NOFOLLOW);
  try {if ((await h.stat()).size > 16000) throw Error('oversize_mail_payload'); return JSON.parse(await h.readFile('utf8'));}
  finally {await h.close();}
}
async function write(file, value) {
  const temp = path.join(path.dirname(file), '.mail-' + crypto.randomUUID());
  try {await fs.writeFile(temp, JSON.stringify(value), {mode:0o600,flag:'wx'}); await fs.rename(temp,file);}
  finally {await fs.unlink(temp).catch(()=>{});}
}
function decoded(raw) {
  if (raw?.isError) throw Error('mail_connector_error');
  if (raw?.structuredContent) return raw.structuredContent;
  const part=raw?.content?.find(c=>c.type==='text');
  if (part) return JSON.parse(part.text);
  return raw;
}
function clean(text) {
  return String(text).replace(/https?:\/\/\S+/gi,'[link]').replace(/[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}/gi,'[email]').slice(0,10000);
}
export class LocalMailSubscriptionWorker {
  static async create(directory,identity) {
    if (!path.isAbsolute(directory)||(await fs.lstat(directory)).isSymbolicLink()) throw Error('invalid_directory');
    const raw=await fs.readFile(path.join(directory,'registry.json'),'utf8'), registry=JSON.parse(raw);
    if(registry.schema!==2||!Array.isArray(registry.cases)||!registry.cases.length||registry.cases.length>2) throw Error('invalid_registry');
    for(const c of registry.cases) if(!/^[a-f0-9]{32}$/.test(c.call_id)||c.revision!==1||c.mailbox!=='Test folder'||c.allowed_from!=='store-news@woot.com') throw Error('invalid_scope');
    return Object.assign(new LocalMailSubscriptionWorker(), {directory,identity,cases:registry.cases,registryHash:crypto.createHash('sha256').update(raw).digest('hex'),pending:null,activeTool:null,
      processed:new Set(),counts:new Map(),inventory:new Map(),cache:new Map(),reports:[],trace:[],sessionDeadline:Date.now()+30*60*1000});
  }
  async heartbeat(serving) {await write(path.join(this.directory,'worker-ready.json'),{schema:1,serving,registry_hash:this.registryHash,heartbeat_ms:Date.now(),worker_identity:this.identity});}
  case() {if(!this.pending||Date.now()>=this.pending.expires_ms) throw Error('no_live_mail_request');return this.cases.find(c=>c.call_id===this.pending.call_id);}
  async waitNext(timeoutMs=60000) {
    if(this.pending) throw Error('mail_answer_pending');
    const deadline=Math.min(this.sessionDeadline,Date.now()+Math.min(timeoutMs,60000));
    while(Date.now()<deadline) {
      if(await fs.stat(path.join(this.directory,'finished.signal')).then(()=>true,()=>false)){await this.heartbeat(false);return {status:'finished'};}
      await this.heartbeat(true);
      const files=await fs.readdir(this.directory);
      for(const file of files.filter(f=>/^[a-f0-9]{32}-[a-f0-9]{32}-request\.json$/.test(f))) {
        if(this.processed.has(file))continue;
        const req=await read(path.join(this.directory,file)), c=this.cases.find(c=>c.call_id===req.call_id);
        if(!c||Object.keys(req).sort().join(',')!==['schema','call_id','request_id','revision','nonce','question','created_ms','expires_ms'].sort().join(',')||req.schema!==2||req.revision!==c.revision||file!==req.call_id+'-'+req.request_id+'-request.json'||!/^[a-f0-9]{64}$/.test(req.nonce)||typeof req.question!=='string'||!req.question.length||req.question.length>1500||!Number.isFinite(req.created_ms)||!Number.isFinite(req.expires_ms)||req.expires_ms-req.created_ms>61000||Date.now()>=req.expires_ms)throw Error('invalid_mail_request');
        const counts=this.counts.get(c.call_id)||{search_count:0,read_count:0,request_count:0};
        if(counts.request_count>=c.max_requests)throw Error('mail_request_limit');
        this.counts.set(c.call_id,counts);this.pending={...req,request_observed_ms:Date.now(),file};
        return {status:'question',call_id:req.call_id,request_id:req.request_id,question:req.question,expires_ms:req.expires_ms,request_observed_ms:this.pending.request_observed_ms,scope:{mailbox:c.mailbox,allowed_from:c.allowed_from}};
      }
      await new Promise(r=>setTimeout(r,25));
    }
    if(Date.now()>=this.sessionDeadline){await this.heartbeat(false);return {status:'finished'};}
    return {status:'waiting'};
  }
  startSearch(filters) {
    const c=this.case(), n=this.counts.get(c.call_id);
    if(this.activeTool||n.search_count>=c.max_searches||!filters||Object.getPrototypeOf(filters)!==Object.prototype||Object.keys(filters).some(k=>!['subject','text','since','unseen'].includes(k)))throw Error('search_outside_scope');
    for(const [k,v]of Object.entries(filters))if(k==='unseen'?typeof v!=='boolean':typeof v!=='string'||!v.trim()||v.length>240)throw Error('invalid_search_filter');
    const args={mailbox:c.mailbox,from:c.allowed_from,limit:5,...filters};
    this.activeTool={kind:'search',started_ms:Date.now(),args};n.search_count++;
    return args;
  }
  finishSearch(raw) {
    this.case();const t=this.activeTool;if(t?.kind!=='search')throw Error('search_not_started');this.activeTool=null;
    const d=decoded(raw), c=this.case();if(!Array.isArray(d.messages)||typeof d.uidValidity!=='string')throw Error('invalid_search_result');
    const messages=d.messages.filter(m=>m.mailbox===c.mailbox&&m.from?.some(f=>f.address?.toLowerCase()===c.allowed_from)&&Number.isSafeInteger(m.uid)&&m.uid>0&&m.uidValidity===d.uidValidity).slice(0,5).map(m=>({mailbox:m.mailbox,uid:m.uid,uidValidity:m.uidValidity,subject:clean(m.subject),date:m.date,from:c.allowed_from}));
    for(const m of messages)this.inventory.set(c.call_id+':'+m.uidValidity+':'+m.uid,m);
    this.trace.push({phase:'search',call_id:c.call_id,request_id:this.pending.request_id,started_ms:t.started_ms,ended_ms:Date.now(),filters:t.args,raw_match_count:d.messages.length,match_count:messages.length});
    return {messages,untrustedContent:true};
  }
  startRead({uid,uidValidity}) {
    const c=this.case(), n=this.counts.get(c.call_id), key=c.call_id+':'+uidValidity+':'+uid;
    if(this.activeTool||n.read_count>=c.max_reads||!this.inventory.has(key))throw Error('read_outside_search_results');
    const args={mailbox:c.mailbox,uid,uidValidity};this.activeTool={kind:'read',started_ms:Date.now(),args,key};n.read_count++;return args;
  }
  finishRead(raw) {
    this.case();const t=this.activeTool;if(t?.kind!=='read')throw Error('read_not_started');this.activeTool=null;
    const d=decoded(raw),c=this.case();
    if(d.mailbox!==c.mailbox||d.uid!==t.args.uid||d.uidValidity!==t.args.uidValidity||!d.from?.some(f=>f.address?.toLowerCase()===c.allowed_from)||typeof d.text!=='string')throw Error('read_result_outside_scope');
    const result={...this.inventory.get(t.key),text:clean(d.text),truncated:d.truncated===true,untrustedContent:true};this.cache.set(t.key,result);
    this.trace.push({phase:'read',call_id:c.call_id,request_id:this.pending.request_id,started_ms:t.started_ms,ended_ms:Date.now(),uid:d.uid,uidValidity:d.uidValidity});return result;
  }
  async publish({status='answered',answer,evidence}) {
    const c=this.case(),p=this.pending,n=this.counts.get(c.call_id);
    if(this.activeTool||!['answered','unresolved'].includes(status)||typeof answer!=='string'||!answer.trim()||answer.length>1500||!Array.isArray(evidence)||evidence.length>4||(status==='answered'&&!evidence.length))throw Error('invalid_mail_answer');
    const sources=evidence.map(e=>{const m=this.cache.get(c.call_id+':'+e.uidValidity+':'+e.uid);if(!m||typeof e.quote!=='string'||!e.quote.length||e.quote.length>500||!m.text.includes(e.quote))throw Error('unsupported_mail_evidence');return {mailbox:c.mailbox,uid:m.uid,uidValidity:m.uidValidity,from:c.allowed_from,quote:e.quote};});
    const written=Date.now(),result={schema:2,call_id:p.call_id,request_id:p.request_id,revision:p.revision,nonce:p.nonce,status,answer,evidence:sources,search_count:n.search_count,read_count:n.read_count,request_observed_ms:p.request_observed_ms,result_written_ms:written};
    await write(path.join(this.directory,p.call_id+'-'+p.request_id+'-result.json'),result);
    const report={...result};delete report.nonce;this.reports.push(report);n.request_count++;this.processed.add(p.file);this.pending=null;
    await write(path.join(this.directory,'worker-results.json'),{identity:this.identity,reports:this.reports,trace:this.trace});return {call_id:report.call_id,request_id:report.request_id,status,search_count:n.search_count,read_count:n.read_count,result_written_ms:written};
  }
  async stop(){this.pending=null;this.activeTool=null;await this.heartbeat(false);this.cache.clear();this.inventory.clear();return {identity:this.identity,answers:this.reports.length,counts:[...this.counts.values()]};}
}
