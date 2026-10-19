// Test-only signed-in inference with local fictional tools. No connected accounts.
import readline from 'node:readline';
import {fileURLToPath} from 'node:url';
import {SubscriptionAppServer} from './subscription_app_server.mjs';

export const LOOKUP_TOOL={type:'function',name:'lookup_customer_record',
  description:'Read one field from the fictional customer record identified by source_record_key in the briefing. '+
    'A successful current, uniquely matching record is evidence; errors, expired records and conflicting matches are not answers. '+
    'Transient failures may be retried once. This tool never changes an account.',
  inputSchema:{type:'object',properties:{record_key:{type:'string'},field:{type:'string'}},
    required:['record_key','field'],additionalProperties:false}};

export function isolatedConfig(apps,config={}) {
  const overrides={'apps._default.enabled':false,'features.shell_tool':false,
    'features.multi_agent':false,'web_search':'disabled',
    'features.fast_mode':false,'service_tier':'default'};
  for(const app of apps) {
    if(!/^[a-zA-Z0-9_-]+$/.test(app.id))throw Error('invalid_app_identity');
    overrides[`apps.${app.id}.enabled`]=false;
  }
  for(const name of Object.keys(config.mcp_servers??{})) {
    if(!/^[a-zA-Z0-9_-]+$/.test(name))throw Error('invalid_mcp_identity');
    overrides[`mcp_servers.${name}.enabled`]=false;
  }
  return overrides;
}

export class FixtureTools {
  constructor(fixtures=[]) {this.fixtures=fixtures;this.calls=[];}
  async call(params) {
    if(params.tool!==LOOKUP_TOOL.name)throw Error('unexpected_tool');
    const args=params.arguments;
    const fixture=this.fixtures.find(f=>f.record_key===args?.record_key&&f.field===args?.field);
    const prior=this.calls.filter(c=>c.arguments.record_key===args?.record_key&&c.arguments.field===args?.field).length;
    let result;
    if(!fixture)result={status:'not_found',records:[]};
    else if(prior>=3)result={status:'retry_limit',records:[]};
    else {
      if(fixture.delay_ms)await new Promise(resolve=>setTimeout(resolve,Math.min(fixture.delay_ms,3000)));
      result=fixture.responses[Math.min(prior,fixture.responses.length-1)];
    }
    this.calls.push({name:LOOKUP_TOOL.name,arguments:args,result});
    return {success:!['timeout','unavailable','permission_denied','rate_limited','retry_limit'].includes(result.status),
      contentItems:[{type:'inputText',text:JSON.stringify(result)}]};
  }
}

function contains(value,quote) {
  return typeof value==='string'?value.includes(quote):!!value&&typeof value==='object'&&Object.values(value).some(v=>contains(v,quote));
}
export class EvalWorker {
  constructor(session,cwd) {this.session=session;this.cwd=cwd;this.active=null;}
  async initialize() {
    await this.session.initialize({requiredApps:[]});
    const installed=await this.session.rpc('app/installed',{});
    if(installed.apps.some(a=>a.enabled))throw Error('connected_apps_enabled');
    this.session.onNotification=m=>{
      if(m.params?.threadId!==this.active?.threadId)return;
      if(m.method==='item/completed')this.active.items.push(m.params.item);
      if(m.method==='turn/completed')this.active.resolve(m.params.turn);
    };
    this.session.onRequest=m=>{
      if(m.method!=='item/tool/call'||m.params.threadId!==this.active?.threadId)throw Error('unexpected_server_request');
      return this.active.tools.call(m.params);
    };
  }
  async run(request) {
    if(this.active)throw Error('worker_busy');
    if(!['reason','simulate'].includes(request.operation)||!request.instructions||!request.schema)throw Error('invalid_eval_request');
    const tools=new FixtureTools(request.fixtures);
    const expires=Math.min(request.deadline_ms??Date.now()+50000,Date.now()+50000);
    const remaining=()=>Math.max(1,expires-Date.now());
    const thread=await this.session.rpc('thread/start',{cwd:this.cwd,ephemeral:true,
      approvalPolicy:'never',sandbox:'read-only',model:request.model,serviceTier:'default',serviceName:'dots_phone_text_eval',
      developerInstructions:request.instructions+'\nReturn only the requested JSON. No shell, files, web, skills, delegation or real connected tools. '+
        'Only explicitly supplied fictional lookup tools may be used. Never ask the human for input.',
      dynamicTools:request.operation==='reason'&&request.fixtures?.length?[LOOKUP_TOOL]:[]},remaining());
    if(!thread.thread.ephemeral)throw Error('non_ephemeral_eval');
    const threadId=thread.thread.id;
    let timer,turnId;
    const completion=new Promise(resolve=>{this.active={threadId,items:[],tools,resolve};});
    const timeout=new Promise((_,reject)=>{timer=setTimeout(()=>reject(Error('eval_worker_timeout')),remaining());});
    try {
      const start=await this.session.rpc('turn/start',{threadId,
        input:[{type:'text',text:JSON.stringify(request.input),text_elements:[]}],
        effort:request.operation==='reason'?'medium':'low',outputSchema:request.schema},remaining());
      turnId=start.turn.id;
      const turn=await Promise.race([completion,timeout]);
      if(turn.id!==turnId||turn.status!=='completed')throw Error('eval_turn_failed');
      const items=[...new Map([...this.active.items,...(turn.items??[])].map(i=>[i.id??Symbol(),i])).values()];
      if(items.some(i=>['commandExecution','fileChange','mcpToolCall','webSearch','collabToolCall'].includes(i.type)))
        throw Error('unexpected_external_tool');
      const final=items.filter(i=>i.type==='agentMessage'&&i.phase!=='commentary').at(-1);
      if(!final)throw Error('eval_missing_answer');
      const output=JSON.parse(final.text);
      return {output,tool_calls:tools.calls,
        tool_quote_verified:!!output.supporting_quote&&tools.calls.some(c=>
          c.result.status==='ok'&&(contains(c.result,output.supporting_quote)||JSON.stringify(c.result).includes(output.supporting_quote)))};
    } finally {
      clearTimeout(timer);
      if(turnId)await this.session.rpc('turn/interrupt',{threadId,turnId},500).catch(()=>{});
      this.active=null;
      await this.session.rpc('thread/archive',{threadId},1000).catch(()=>{});
    }
  }
}

async function main() {
  const bootstrap=new SubscriptionAppServer();
  let session;
  try {
    await bootstrap.initialize({requiredApps:[]});
    const apps=await bootstrap.rpc('app/installed',{});
    const config=await bootstrap.rpc('config/read',{includeLayers:false});
    session=new SubscriptionAppServer('codex',{config:isolatedConfig(apps.apps,config.config)});
  } finally {bootstrap.close();}
  const worker=new EvalWorker(session,process.cwd());
  for(const signal of ['SIGTERM','SIGINT'])process.on(signal,()=>{session.close();process.exit(0);});
  try {
    await worker.initialize();
    process.stdout.write(JSON.stringify({ready:true,connected_tools:0})+'\n');
    for await(const line of readline.createInterface({input:process.stdin})) {
      const request=JSON.parse(line);
      try {process.stdout.write(JSON.stringify({id:request.id,...await worker.run(request)})+'\n');}
      catch(error){process.stdout.write(JSON.stringify({id:request.id,error:error.message})+'\n');}
    }
  } finally {session.close();}
}
if(process.argv[1]===fileURLToPath(import.meta.url))main().catch(()=>{
  process.stdout.write(JSON.stringify({error:'eval_worker_start_failed'})+'\n');process.exitCode=1;
});
