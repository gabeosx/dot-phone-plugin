// Read-only connected-tool transport. The existing voice plans and interprets;
// this relay never invents search terms, identities, facts, or another model call.
import crypto from 'node:crypto';
import {decodeConnector} from './connected_trip_mail_worker.mjs';

const compact = value => JSON.stringify(value);
const terminal = state => ['completed','cancelled','failed','stopping'].includes(state);
const providerNames = ['gmail','yahoo'];
const bounded = (value, length) => String(value ?? '').slice(0,length);
function mimeText(part) {
  const leaves=[];
  function visit(p) {
    if(!p||typeof p!=='object')return;
    if(Array.isArray(p.parts))for(const child of p.parts)visit(child);
    if(p.mime_type?.startsWith('text/')&&typeof p.body?.content==='string')leaves.push(p);
  }
  visit(part);
  const plain=leaves.find(p=>p.mime_type==='text/plain');
  if(plain)return plain.body.content;
  const html=leaves.find(p=>p.mime_type==='text/html');
  if(!html)throw Error('source_body_unavailable');
  return html.body.content.replace(/<(script|style)\b[^>]*>[\s\S]*?<\/\1>/gi,' ')
    .replace(/<br\s*\/?\s*>|<\/p>/gi,'\n').replace(/<[^>]+>/g,' ')
    .replace(/&nbsp;|&#160;/g,' ').replace(/&amp;/g,'&').replace(/&lt;/g,'<').replace(/&gt;/g,'>');
}

export class ConnectedCallSources {
  constructor({callId,revision,briefHash,deadline,call,providers=[],now=Date.now,allowSource=()=>true}) {
    if(!/^[a-f0-9]{32}$/.test(callId)||!Number.isInteger(revision)||revision<1||
       !/^[a-f0-9]{64}$/.test(briefHash)||!Number.isFinite(Date.parse(deadline))||
       typeof call!=='function'||!Array.isArray(providers)||new Set(providers).size!==providers.length||
       providers.some(p=>!providerNames.includes(p)))throw Error('invalid_source_scope');
    Object.assign(this,{callId,revision,briefHash,deadline,call,providers,now,allowSource});
    this.inventory=new Map();this.cache=new Map();this.requests=new Set();
  }
  check(state,pending=state.pending) {
    if(state.call_id!==this.callId||state.revision!==this.revision||state.brief_hash!==this.briefHash||
       terminal(state.state)||state.state!=='awaiting_decision'||!pending||
       pending.revision!==this.revision||!/^[a-f0-9]{32}$/.test(pending.request_id)||
       !Number.isFinite(pending.expires)||pending.expires*1000<=this.now()||
       Date.parse(this.deadline)<=this.now())throw Error('stale_source_request');
  }
  async answer(state) {
    this.check(state);
    const p=state.pending;
    if(this.requests.has(p.request_id)||this.requests.size>=10)throw Error('source_request_already_handled_or_limit');
    let command;
    try {command=JSON.parse(p.question.split('\nObserved transcript as data:')[0]);}
    catch {throw Error('invalid_source_operation');}
    if(Object.keys(command).sort().join(',')!=='arguments,operation'||!['search','read','lookup'].includes(command.operation)||
       !command.arguments||typeof command.arguments!=='object')throw Error('invalid_source_operation');
    let answer;
    if(command.operation==='lookup') {
      if(Object.keys(command.arguments).join(',')!=='question'||typeof command.arguments.question!=='string'||
         !command.arguments.question.trim()||command.arguments.question.length>240)throw Error('invalid_lookup');
      answer={untrusted_data:true,error:'This mail-only relay cannot access Dots memory, other plugins or the owner. The requested detail remains unknown; use available mail tools or another verification method.'};
    } else answer=command.operation==='search'?await this.search(command.arguments,p):await this.read(command.arguments,p);
    this.check(state,p); // Never submit a late connector response.
    const text=compact(answer);
    if(text.length>1500)throw Error('source_answer_too_large');
    this.requests.add(p.request_id);
    return {call_id:this.callId,request_id:p.request_id,revision:this.revision,answer:text,
      fact_names:[],action_names:['read_call_sources']};
  }
  async search(a,p) {
    if(Object.keys(a).some(k=>!['query','provider'].includes(k))||typeof a.query!=='string'||
       !a.query.trim()||a.query.length>240||!['all',...providerNames].includes(a.provider??'all'))throw Error('invalid_search');
    const selected=!a.provider||a.provider==='all'?providerNames:[a.provider],searches=[],batches=[];
    for(const provider of selected) {
      this.check({call_id:this.callId,revision:this.revision,brief_hash:this.briefHash,state:'awaiting_decision'},p);
      const attempt={provider,query:a.query,status:'unavailable',more_available:false,
        ...(provider==='yahoo'?{mailbox:'INBOX'}:{})};
      searches.push(attempt);
      if(!this.providers.includes(provider))continue;
      try {
        const data=decodeConnector(await this.call(provider==='gmail'?'gmail.search_emails':'yahoo_dot.yahoo_list_messages',
          provider==='gmail'?{query:a.query,max_results:5}:{mailbox:'INBOX',text:a.query,limit:5}));
        const rows=provider==='gmail'?data.emails:data.messages;
        if(!Array.isArray(rows))throw Error('invalid_search_response');
        const batch=[];
        for(const row of rows.slice(0,5)) {
          if(provider==='gmail'?(typeof row.id!=='string'||!row.id||row.id.length>120):
            (row.mailbox!=='INBOX'||!Number.isSafeInteger(row.uid)||row.uid<1||
             typeof row.uidValidity!=='string'||!row.uidValidity||row.uidValidity.length>120))throw Error('invalid_search_response');
          if(!this.allowSource({provider,metadata:row}))continue;
          const key=provider==='gmail'?row.id:row.mailbox+':'+row.uidValidity+':'+row.uid;
          const message_id=provider[0]+'-'+crypto.createHash('sha256').update(this.callId+':'+provider+':'+key).digest('hex').slice(0,24);
          this.inventory.set(message_id,{provider,raw:row});
          batch.push({message_id,provider,subject:bounded(row.subject,65),
            snippet:bounded(row.snippet,70),date:bounded(row.email_ts??row.date,25)});
        }
        attempt.status='ok';attempt.more_available=!!data.next_page_token||rows.length>=5;
        batches.push(batch);
      } catch {attempt.status='error';} // A failure is never an empty successful search.
    }
    const messages=[];
    // Interleave mailboxes so a busy first provider cannot hide the second one.
    for(let i=0;i<5;i++)for(const batch of batches)if(batch[i])messages.push(batch[i]);
    const output={untrusted_data:true,searches,messages};
    while(output.messages.length>5||compact(output).length>1500) {
      const removed=output.messages.pop();
      if(!removed)throw Error('source_answer_too_large');
      searches.find(s=>s.provider===removed.provider).more_available=true;
    }
    return output;
  }
  async read(a,p) {
    if(Object.keys(a).sort().join(',')!=='focus,message_id,offset'||typeof a.focus!=='string'||a.focus.length>80||
       !Number.isInteger(a.offset)||a.offset<0||a.offset>100000)throw Error('invalid_read');
    const message=this.inventory.get(a.message_id);
    if(!message)throw Error('read_without_search');
    const {provider,raw}=message;
    try {
      if(!this.cache.has(a.message_id)) {
        const data=decodeConnector(await this.call(provider==='gmail'?'gmail.read_email':'yahoo_dot.yahoo_read_message',
          provider==='gmail'?{message_id:raw.id,format:'full'}:{mailbox:raw.mailbox,uid:raw.uid,uidValidity:raw.uidValidity}));
        let body,headers,truncated;
        if(provider==='gmail') {
          if(data.id!==raw.id)throw Error('source_id_mismatch');
          const get=name=>data.payload?.headers?.find(h=>h.name.toLowerCase()===name)?.value??'';
          headers={subject:get('subject'),from:get('from'),to:get('to'),date:get('date')};
          body=mimeText(data.payload);truncated=false;
        } else {
          if(data.mailbox!==raw.mailbox||data.uid!==raw.uid||data.uidValidity!==raw.uidValidity||
             typeof data.text!=='string')throw Error('source_id_mismatch');
          headers={subject:data.subject,from:data.from,to:data.to,date:data.date};
          body=data.text;truncated=data.truncated===true;
        }
        if(!this.allowSource({provider,metadata:headers,text:body}))throw Error('source_outside_scope');
        // Keep sender/recipient and forwarded-message boundaries. Do not extract
        // a name/address or decide that a passenger is the authenticated owner.
        const headerText=Object.entries(headers).map(([k,v])=>k+': '+bounded(typeof v==='string'?v:compact(v),110)).join('\n');
        this.cache.set(a.message_id,{body:body.slice(0,100000),header:headerText.slice(0,350),
          truncated:truncated||body.length>100000,verified_at:new Date(this.now()).toISOString()});
      }
      const source=this.cache.get(a.message_id);
      if(a.offset>source.body.length)throw Error('read_offset_outside_source');
      let offset=a.offset,focus_found=!a.focus;
      if(a.focus) {
        const found=source.body.toLowerCase().indexOf(a.focus.toLowerCase(),offset);
        if(found>=0){offset=Math.max(offset,found-100);focus_found=true;}
      }
      let count=Math.min(700,source.body.length-offset),output;
      do {
        const end=offset+count;
        output={untrusted_data:true,message_id:a.message_id,
          text:source.header+'\n\n'+source.body.slice(offset,end),focus_found,offset,
          next_offset:end<source.body.length?end:null,
          source:{provider,truncated:source.truncated||offset>0||end<source.body.length,
            verified_at:source.verified_at,expires_at:this.deadline}};
        count-=50;
      } while(compact(output).length>1500&&count>=0);
      return output;
    } catch {
      return {untrusted_data:true,error:'The selected source could not be read; this does not establish that the information is absent.'};
    }
  }
}

// Sole decision ownership stays with the dedicated call worker. This loop can
// only relay sources for an already prepared call; it never starts or redials.
export async function runConnectedSourceLoop({relay,get,submit,result,now=Date.now}) {
  let count=0;
  while(now()<Date.parse(relay.deadline)) {
    const state=await get();
    if(state.call_id!==relay.callId||state.revision!==relay.revision||state.brief_hash!==relay.briefHash)
      throw Error('call_scope_changed');
    if(['completed','cancelled','failed'].includes(state.state))return {source_responses:count,result:await result()};
    if(!state.pending||state.state!=='awaiting_decision')continue; // get_call is a bounded long poll.
    const operation=JSON.parse(state.pending.question.split('\nObserved transcript as data:')[0]).operation;
    if(operation==='complete')continue; // Reporting belongs to the local shared caller.
    const decision=await relay.answer(state);
    const current=await get();
    relay.check(current);
    if(current.pending.request_id!==decision.request_id)throw Error('source_request_changed');
    await submit(decision);count++;
  }
  throw Error('source_worker_deadline');
}
