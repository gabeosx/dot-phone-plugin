// Mechanical source relay. The voice model chooses queries and interprets sources.
import fs from 'node:fs/promises';
import {constants} from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';

const senders=new Set(['reservations@larkspur.invalid','jordan@trip.invalid']);
async function read(file){const h=await fs.open(file,constants.O_RDONLY|constants.O_NOFOLLOW);try{if((await h.stat()).size>16000)throw Error('oversize_source');const raw=await h.readFile('utf8');return {raw,value:JSON.parse(raw)};}finally{await h.close();}}
async function write(file,value){const tmp=path.join(path.dirname(file),'.context-'+crypto.randomUUID());try{await fs.writeFile(tmp,JSON.stringify(value),{flag:'wx',mode:0o600});await fs.rename(tmp,file);}finally{await fs.unlink(tmp).catch(()=>{});}}
const hash=raw=>crypto.createHash('sha256').update(raw).digest('hex');
function validMessage(m){if(!/^[a-z0-9-]{1,60}$/.test(m.id)||!senders.has(m.from)||typeof m.subject!=='string'||typeof m.text!=='string'||m.text.length>1000)throw Error('source_outside_scope');return m;}
export async function searchFixtureMail(directory,query){
  if(typeof query!=='string'||!query.trim()||query.length>240)throw Error('invalid_query');
  const tokens=query.toLowerCase().match(/[a-z0-9]+/g)||[];
  const matches=[];
  for(const filename of (await fs.readdir(directory)).filter(f=>/^[a-z0-9-]+\.json$/.test(f))){const {raw,value}=await read(path.join(directory,filename));const m=validMessage(value);if(filename!==m.id+'.json')throw Error('source_id_mismatch');const text=(m.subject+' '+m.text).toLowerCase();if(tokens.length&&tokens.every(t=>text.includes(t)))matches.push({message_id:m.id,subject:m.subject,snippet:m.text.slice(0,110),source_sha256:hash(raw)});}
  return matches.slice(0,5);
}
export async function readFixtureMail(directory,id){if(!/^[a-z0-9-]{1,60}$/.test(id))throw Error('invalid_message_id');const {raw,value}=await read(path.join(directory,id+'.json'));const m=validMessage(value);if(m.id!==id)throw Error('source_id_mismatch');return {message_id:id,source_sha256:hash(raw),from:m.from,subject:m.subject,text:m.text};}

export class LocalContextSubscriptionWorker{
  static async create(directory,identity){if(!path.isAbsolute(directory)||(await fs.lstat(directory)).isSymbolicLink())throw Error('invalid_directory');const {raw,value:r}=await read(path.join(directory,'registry.json'));if(r.schema!==3||!Array.isArray(r.cases)||r.cases.length<1||r.cases.length>2)throw Error('invalid_registry');for(const c of r.cases)if(!/^[a-f0-9]{32}$/.test(c.call_id)||c.revision!==1||c.provider!=='synthetic_trip_mail'||c.max_requests!==6)throw Error('invalid_call_scope');return Object.assign(new LocalContextSubscriptionWorker(),{directory,identity,cases:r.cases,registryHash:hash(raw),inventory:new Map(),processed:new Set(),reports:[],deadline:Date.now()+30*60*1000});}
  async heartbeat(serving){await write(path.join(this.directory,'worker-ready.json'),{schema:1,serving,registry_hash:this.registryHash,heartbeat_ms:Date.now(),worker_identity:this.identity});}
  async handle(req){
    const c=this.cases.find(c=>c.call_id===req.call_id);
    if(!c||req.schema!==3||req.revision!==c.revision||!/^[a-f0-9]{32}$/.test(req.request_id)||!/^[a-f0-9]{64}$/.test(req.nonce)||!Number.isFinite(req.expires_ms)||Date.now()>=req.expires_ms||req.expires_ms-req.created_ms>61000)throw Error('invalid_live_request');
    if(this.reports.filter(r=>r.call_id===req.call_id).length>=c.max_requests)throw Error('request_limit');
    const observed=Date.now();let output;
    const mailbox=path.join(this.directory,'mailbox');
    if((await fs.lstat(mailbox)).isSymbolicLink())throw Error('invalid_mailbox');
    if(req.operation==='search'&&Object.keys(req.arguments).join(',')==='query'){
      const matches=await searchFixtureMail(mailbox,req.arguments.query);
      for(const m of matches)this.inventory.set(req.call_id+':'+m.message_id,m.source_sha256);
      output={untrusted_data:true,messages:matches.map(({source_sha256,...m})=>m)};
    }else if(req.operation==='read'&&Object.keys(req.arguments).join(',')==='message_id'){
      const id=req.arguments.message_id,key=req.call_id+':'+id;
      if(!this.inventory.has(key))throw Error('read_without_search');
      const m=await readFixtureMail(mailbox,id);
      if(m.source_sha256!==this.inventory.get(key))throw Error('source_changed_search_again');
      output={untrusted_data:true,message_id:m.message_id,source_sha256:m.source_sha256,text:m.text};
    }else throw Error('invalid_operation');
    if(Date.now()>=req.expires_ms)throw Error('expired_after_read');
    const result={schema:3,call_id:req.call_id,request_id:req.request_id,revision:req.revision,nonce:req.nonce,output,request_observed_ms:observed,result_written_ms:Date.now()};
    await write(path.join(this.directory,req.call_id+'-'+req.request_id+'-result.json'),result);
    const {nonce,...safe}=result;this.reports.push({...safe,operation:req.operation,arguments:req.arguments});
    await write(path.join(this.directory,'worker-results.json'),{identity:this.identity,provider:'synthetic_trip_mail',reasoning:'existing_voice_model',reports:this.reports});
    return {operation:req.operation,request_id:req.request_id,answered:true};
  }
  async serveFixtures(){
    try{while(Date.now()<this.deadline){if(await fs.stat(path.join(this.directory,'finished.signal')).then(()=>true,()=>false))break;await this.heartbeat(true);for(const filename of (await fs.readdir(this.directory)).filter(f=>/^[a-f0-9]{32}-[a-f0-9]{32}-request\.json$/.test(f))){if(this.processed.has(filename))continue;let req;try{req=(await read(path.join(this.directory,filename))).value;}catch(e){if(e.code==='ENOENT')continue;throw e;}if(filename!==req.call_id+'-'+req.request_id+'-request.json')throw Error('request_filename_mismatch');if(Date.now()>=req.expires_ms){this.processed.add(filename);continue;}await this.handle(req);this.processed.add(filename);}await new Promise(r=>setTimeout(r,25));}}
    finally{await this.heartbeat(false);this.inventory.clear();}
    return {identity:this.identity,source_operations:this.reports.length,serving:false};
  }
}
