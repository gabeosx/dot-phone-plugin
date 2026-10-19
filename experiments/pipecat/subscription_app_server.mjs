// Supported Codex app-server RPC using existing ChatGPT sign-in. No API keys.
import {spawn} from 'node:child_process';

export function configValue(value) {
  if(Array.isArray(value))return '['+value.map(configValue).join(',')+']';
  if(value&&typeof value==='object')return '{'+Object.entries(value).map(([k,v])=>
    JSON.stringify(k)+'='+configValue(v)).join(',')+'}';
  if(value===null||!['string','number','boolean'].includes(typeof value))throw Error('unsupported_config_value');
  return JSON.stringify(value);
}

export class SubscriptionAppServer {
  constructor(command='codex',{config={}}={}) {
    this.nextId=0; this.pending=new Map(); this.buffer=''; this.closed=false;
    const overrides=Object.entries(config).flatMap(([key,value])=>['-c',key+'='+configValue(value)]);
    this.process=spawn(command,['app-server','--listen','stdio://','--enable','apps',...overrides],{stdio:['pipe','pipe','ignore']});
    this.process.stdout.setEncoding('utf8');
    this.process.stdout.on('data',chunk=>this.receive(chunk));
    this.process.stdin.on('error',()=>this.fail('app_server_input_closed'));
    this.process.on('error',()=>this.fail('app_server_start_failed'));
    this.process.on('exit',()=>this.fail('app_server_closed'));
  }
  fail(code) {this.closed=true;for(const p of this.pending.values()){clearTimeout(p.timer);p.reject(Error(code));}this.pending.clear();}
  send(message) {if(this.closed)throw Error('app_server_closed');this.process.stdin.write(JSON.stringify(message)+'\n');}
  receive(chunk) {
    this.buffer+=chunk;
    if(this.buffer.length>16*1024*1024){this.fail('app_server_payload_limit');this.process.kill();return;}
    let at;
    while((at=this.buffer.indexOf('\n'))>=0){
      const line=this.buffer.slice(0,at);this.buffer=this.buffer.slice(at+1);let m;
      try{m=JSON.parse(line);}catch{this.fail('invalid_app_server_message');return;}
      if(m.method&&m.id!==undefined){
        // No unattended blanket approval of arbitrary app actions.
        if(this.onRequest)Promise.resolve().then(()=>this.onRequest(m)).then(result=>
          this.send({id:m.id,result}),()=>this.send({id:m.id,error:{code:-32000,message:'Client tool refused'}})).catch(()=>{});
        else this.send({id:m.id,error:{code:-32000,message:'Dedicated owned call worker cannot approve new actions'}});
        continue;
      }
      if(m.method&&m.id===undefined)this.onNotification?.(m);
      if(m.id!==undefined){const p=this.pending.get(m.id);if(!p)continue;this.pending.delete(m.id);clearTimeout(p.timer);
        if(m.error)p.reject(Error('app_server_rpc_refused'));else p.resolve(m.result);}
    }
  }
  rpc(method,params,timeout=45000) {
    const id=++this.nextId;
    return new Promise((resolve,reject)=>{
      const timer=setTimeout(()=>{this.pending.delete(id);reject(Error('app_server_rpc_timeout'));},timeout);
      this.pending.set(id,{resolve,reject,timer});
      try{this.send({id,method,params});}catch(e){clearTimeout(timer);this.pending.delete(id);reject(e);}
    });
  }
  async initialize({requiredApps=['Dots Phone','Gmail','Yahoo Dot']}={}) {
    await this.rpc('initialize',{clientInfo:{name:'dots_phone_subscription_worker',version:'0.1.0'},capabilities:{experimentalApi:true}});
    this.send({method:'initialized'});
    const account=await this.rpc('account/read',{refreshToken:false});
    if(account.account?.type!=='chatgpt')throw Error('chatgpt_subscription_required');
    const installed=await this.rpc('app/installed',{forceRefresh:true});
    for(const name of requiredApps)
      if(!installed.apps?.some(a=>a.runtimeName===name&&a.enabled&&a.callable))throw Error('required_connected_app_unavailable');
    return {account_type:'chatgpt',apps:requiredApps};
  }
  async freshWorker(cwd,{requiredTools=null,...options}={}) {
    const result=await this.rpc('thread/start',{cwd,ephemeral:true,approvalPolicy:'never',sandbox:'read-only',serviceName:'dots_phone_owned_worker',...options});
    if(result.thread?.ephemeral!==true)throw Error('worker_must_be_ephemeral');
    const threadId=result.thread.id;
    const inventory=await this.rpc('mcpServerStatus/list',{threadId,detail:'toolsAndAuthOnly',limit:100});
    const server=inventory.data?.find(s=>s.name==='codex_apps');
    const names=new Set(Object.keys(server?.tools||{}));
    for(const name of requiredTools??['dots_phone.get_call','dots_phone.get_call_result','dots_phone.end_call','gmail.search_emails','gmail.read_email','gmail.get_profile','yahoo_dot.yahoo_status','yahoo_dot.yahoo_list_messages','yahoo_dot.yahoo_read_message'])
      if(!names.has(name))throw Error('worker_tool_unavailable');
    return {threadId,ephemeral:true,availableTools:[...names],toolSchemas:server?.tools??{},
      mcpServers:(inventory.data??[]).map(s=>({name:s.name,tools:Object.keys(s.tools??{})})),call:async(tool,args,timeout=30000)=>{
      if(!names.has(tool))throw Error('worker_tool_unavailable');
      const r=await this.rpc('mcpServer/tool/call',{threadId,server:'codex_apps',tool,arguments:args},timeout);
      if(r.isError)throw Error('connected_tool_failed');return r;
    }};
  }
  close(){this.process.kill('SIGTERM');this.fail('app_server_closed');}
}
