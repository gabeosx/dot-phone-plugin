import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp,writeFile,rm} from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {CodexCallAgent,callSourceTools,contextInput,evalRequest,privateRead,readOnlyTools,validateRequest,workerConfig} from '../codex_call_reasoner.mjs';
import {configValue} from '../subscription_app_server.mjs';

const schemas={
  'mail.read':{annotations:{readOnlyHint:true},_meta:{connector_id:'mail_app'}},
  'mail.send':{annotations:{readOnlyHint:false},_meta:{connector_id:'mail_app'}},
  'unsafe.read':{annotations:{readOnlyHint:true,destructiveHint:true}},
  'dots_phone.get_call':{annotations:{readOnlyHint:true}},
  'dots_phone_legacy.get_call':{annotations:{readOnlyHint:true}},
};
const advice={status:'ready',next_step:'Press 2 for balances.',answer:'',supporting_quote:'',report_supported:false};
test('evaluation closed review retires conversation before review and reviewer after completion',async()=>{
  const events=[],scope={call_id:'a'.repeat(32),revision:1,brief_hash:'d'.repeat(64)};
  const agent={retire:()=>events.push('retire'),advise:async request=>{
    events.push(request.operation);assert.deepEqual(callSourceTools(request,['mail.read']),{allowed:[],selection:'none'});
    return {advice:{verdicts:[]},tool_quote_verified:false,lifecycle:{thread_creations:2}};
  }};
  const result=await evalRequest(agent,{operation:'review_closed_call',call_scope:scope,
    deadline_ms:Date.now()+50000,model:'gpt-6-sol',instructions:'Review fictional evidence only.',
    input:{brief:{actions:[]}},schema:{type:'object'}});
  assert.deepEqual(events,['retire','review_closed_call','retire']);
  assert.deepEqual(result.output,{verdicts:[]});assert.deepEqual(result.tool_calls,[]);
});
function request() {
  return {schema:1,call_id:'a'.repeat(32),request_id:'b'.repeat(32),nonce:'c'.repeat(64),
    revision:1,brief_hash:'d'.repeat(64),created_ms:1000,expires_ms:2000,model:'gpt-6-sol',
    instructions:'Fictional test instructions',output_schema:{type:'object'},
    context:{brief:{actions:['read_call_sources']}}};
}
test('connected tool selection excludes writes, destructive tools and phone controls',()=>{
  assert.deepEqual(readOnlyTools(schemas),['mail.read']);
  const config=workerConfig(schemas,['mail.read']);
  assert.equal(config['apps._default.enabled'],false);
  assert.equal(config['apps.mail_app.default_tools_enabled'],false);
  assert.equal(config['apps.mail_app.tools.read.enabled'],true);
  assert.equal(config['apps.mail_app.tools.send.enabled'],false);
  assert.equal(config['features.shell_tool'],false);
  assert.equal(config['features.fast_mode'],false);
  assert.equal(config['service_tier'],'default');
});
test('request envelope binds filename, nonce, scope and deadline',()=>{
  const r=request(),name=r.call_id+'-'+r.request_id+'-reason.json';
  assert.equal(validateRequest(r,name,1500),r);
  for(const change of [{nonce:'bad'},{revision:0},{expires_ms:1499},{created_ms:3000},{brief_hash:'x'}])
    assert.throws(()=>validateRequest({...r,...change},name,1500));
  assert.throws(()=>validateRequest(r,'other-reason.json',1500));
});
test('expanded conversation guidance fits while instructions remain bounded',()=>{
  const r={...request(),instructions:'x'.repeat(24000)};
  const name=r.call_id+'-'+r.request_id+'-reason.json';
  assert.equal(validateRequest(r,name,1500),r);
  assert.throws(()=>validateRequest({...r,instructions:r.instructions+'x'},name,1500),/invalid_reasoning_request/);
});
test('closed-call review has no connected tool access even for a source-enabled brief',()=>{
  const r={...request(),operation:'review_closed_call'};
  assert.equal(validateRequest(r,r.call_id+'-'+r.request_id+'-reason.json',1500),r);
  assert.deepEqual(callSourceTools(r,['mail.read']),{allowed:[],selection:'none'});
  const config=workerConfig(schemas,[]);
  assert.equal(config['apps._default.enabled'],false);
  assert.equal(config['apps.mail_app.tools.read.enabled'],false);
  assert.equal(config['features.shell_tool'],false);
});
test('bounded Unicode context plus schema fits the private transport; oversized files are refused',async()=>{
  const directory=await mkdtemp(path.join(os.tmpdir(),'dots-protocol-'));
  const filename=path.join(directory,'request.json');
  try {
    const value={...request(),instructions:'instructions '.repeat(400),
      context:{evidence:'é'.repeat(7000)},output_schema:{description:'schema '.repeat(500)}};
    // Match Python private_json's ensure_ascii=True encoding.
    const wire=JSON.stringify(value).replace(/[^\x00-\x7f]/g,char=>'\\u'+char.charCodeAt(0).toString(16).padStart(4,'0'));
    assert.ok(Buffer.byteLength(wire)>24000&&Buffer.byteLength(wire)<64000);
    await writeFile(filename,wire,{mode:0o600});
    assert.deepEqual(await privateRead(filename),value);
    await writeFile(filename,' '.repeat(64001));
    await assert.rejects(()=>privateRead(filename),/private_payload_required/);
  } finally {await rm(directory,{recursive:true,force:true});}
});
test('only closed reviews can carry the larger bounded audio evidence envelope',async()=>{
  const directory=await mkdtemp(path.join(os.tmpdir(),'dots-review-protocol-'));
  const filename=path.join(directory,'request.json');
  const wire=value=>JSON.stringify(value).replace(/[^\x00-\x7f]/g,char=>'\\u'+char.charCodeAt(0).toString(16).padStart(4,'0'));
  try {
    const value={...request(),operation:'review_closed_call',context:{evidence:'é'.repeat(20000)}};
    await writeFile(filename,wire(value),{mode:0o600});
    assert.deepEqual(await privateRead(filename),value);
    for(const operation of ['reason','prepare_call']) {
      await writeFile(filename,wire({...value,operation}));
      await assert.rejects(()=>privateRead(filename),/private_payload_required/);
    }
    await writeFile(filename,wire({...value,context:{evidence:'é'.repeat(50000)}}));
    await assert.rejects(()=>privateRead(filename),/private_payload_required/);
  } finally {await rm(directory,{recursive:true,force:true});}
});
class Session {
  constructor(){this.calls=[];this.closed=false;this.sourceAllowed=true;}
  async initialize(){return {};}
  async freshWorker(cwd,options){this.options=options;return {threadId:'fixture-thread',
    availableTools:options.model?(this.sourceAllowed?['mail.read']:[]):Object.keys(schemas),toolSchemas:schemas};}
  async rpc(method,params){
    this.calls.push({method,params});
    if(method==='app/installed')return {apps:[{id:'mail_app',enabled:this.sourceAllowed,callable:this.sourceAllowed}]};
    if(method==='turn/start'){
      const preparing=params.input[0].text.includes('Return decision kind prepared.');
      const output=params.outputSchema?.properties?.decision?
        {decision:preparing?{kind:'prepared'}:{kind:'guide',status:'ready',text:'Press 2 for balances.'}}:advice;
      setTimeout(()=>this.onNotification({method:'turn/completed',params:{threadId:'fixture-thread',turn:{
        id:'fixture-turn',status:'completed',items:this.turnItems??[{type:'agentMessage',phase:'final_answer',text:JSON.stringify(output)}]
      }}}),0);
      return {turn:{id:'fixture-turn'}};
    }
    return {};
  }
  close(){this.closed=true;}
}
test('a dedicated agent reasons in its own ephemeral signed-in session and returns structured advice',async()=>{
  const discovery=new Session(),sessions=[];
  const agent=new CodexCallAgent(discovery,'/tmp',{createSession:config=>{
    const session=new Session();session.sourceAllowed=!!config['apps.mail_app.enabled'];sessions.push(session);return session;
  }});
  await agent.initialize();
  assert.equal(discovery.closed,true);
  const r={...request(),created_ms:Date.now(),expires_ms:Date.now()+5000};
  const result=await agent.advise(r);
  assert.deepEqual(result.advice,advice);
  assert.equal(sessions[0].options.ephemeral,undefined); // freshWorker itself guarantees ephemeral.
  assert.equal(sessions[0].options.model,'gpt-6-sol');
  assert.equal(sessions[0].calls.some(call=>call.method==='turn/start'),true);
  await assert.rejects(()=>agent.worker({...r,brief_hash:'e'.repeat(64)}),/scope_changed/);
  agent.retire(r.call_id,'wrong-hash');
  assert.equal(sessions[0].closed,false);
  agent.retire(r.call_id,r.brief_hash);
  assert.equal(sessions[0].closed,true);
  agent.close();
});
test('context deltas reconstruct the exact latest state, including deleted evidence and array replacement',()=>{
  const previous={brief:{purpose:'long approved task '.repeat(100)},question:'old',
    recent_transcript:[{role:'user',text:'first'}],retrieved_sources:[{text:'expired'}],obsolete:'discard'};
  const current={brief:previous.brief,question:'new',recent_transcript:[...previous.recent_transcript,{role:'user',text:'second'}],
    retrieved_sources:[]};
  const input=contextInput(previous,current);
  assert.equal(input.mode,'delta');
  const delta=JSON.parse(input.text).context_delta;
  const reconstructed=structuredClone(previous);
  for(const key of delta.remove)delete reconstructed[key];
  Object.assign(reconstructed,delta.set);
  for(const [key,value] of Object.entries(delta.append))reconstructed[key].push(...value);
  assert.deepEqual(reconstructed,current);
  assert.deepEqual(delta.set.retrieved_sources,[]);
  assert.equal(contextInput(null,current).mode,'snapshot');
  assert.equal(contextInput({x:1},{x:2}).mode,'snapshot');
});
test('prewarming performs inference once and the first live decision reuses that thread and context',async()=>{
  const sessions=[];
  const agent=new CodexCallAgent(new Session(),'/tmp',{createSession:()=>{
    const session=new Session();sessions.push(session);return session;
  }});
  await agent.initialize();
  const r={...request(),expires_ms:Date.now()+5000,effort:'low',
    output_schema:{type:'object',properties:{decision:{type:'object'}}},
    context:{brief:{actions:['read_call_sources'],purpose:'approved purpose '.repeat(80)},question:'Await first turn.'}};
  const prepared=await agent.prepare(r);
  await agent.prepare(r);
  assert.equal(prepared.prepared,true);
  assert.equal(sessions[0].calls.filter(c=>c.method==='turn/start').length,1);
  const result=await agent.advise({...r,context:{...r.context,question:'Which route?',recent_transcript:[{role:'user',text:'Press 2.'}]}});
  assert.equal(result.lifecycle.thread_id,prepared.lifecycle.thread_id);
  assert.equal(result.lifecycle.input_mode,'delta');
  assert.ok(result.lifecycle.input_bytes<result.lifecycle.full_context_bytes);
  assert.equal(sessions[0].calls.filter(c=>c.method==='turn/start').at(-1).params.effort,'low');
  const turns=sessions[0].calls.filter(c=>c.method==='turn/start');
  assert.deepEqual(turns[0].params.outputSchema,turns[1].params.outputSchema);
  assert.equal(prepared.lifecycle.output_schema_sha256,result.lifecycle.output_schema_sha256);
  assert.equal(sessions[0].options.serviceTier,'default');
  await assert.rejects(()=>agent.worker({...r,effort:'high'}),/configuration_changed/);
  await assert.rejects(()=>agent.worker({...r,output_schema:{type:'string'}}),/configuration_changed/);
  await assert.rejects(()=>agent.worker({...r,model:'different-model'}),/configuration_changed/);
  agent.close();
});
test('failed preparation retires the thread and cannot authorize live decisions',async()=>{
  const sessions=[];
  const agent=new CodexCallAgent(new Session(),'/tmp',{createSession:()=>{
    const session=new Session();session.turnItems=[{type:'agentMessage',text:'{"prepared":false}'}];
    sessions.push(session);return session;
  }});
  await agent.initialize();
  await assert.rejects(()=>agent.prepare({...request(),expires_ms:Date.now()+5000}),/preparation_failed/);
  assert.equal(sessions[0].closed,true);
  assert.equal(agent.workers.size,0);
  agent.close();
});
test('multiple interactions reuse exactly one call thread and retire it at the end',async()=>{
  const sessions=[];
  const agent=new CodexCallAgent(new Session(),'/tmp',{createSession:config=>{
    const session=new Session();session.sourceAllowed=!!config['apps.mail_app.enabled'];sessions.push(session);return session;
  }});
  await agent.initialize();
  const r={...request(),expires_ms:Date.now()+5000};
  const first=await agent.advise(r),second=await agent.advise({...r,request_id:'e'.repeat(32)});
  assert.equal(sessions.length,1);
  assert.equal(second.lifecycle.thread_id,first.lifecycle.thread_id);
  assert.equal(second.lifecycle.thread_creations,1);
  assert.equal(second.lifecycle.thread_reuses,1);
  assert.equal(sessions[0].calls.filter(c=>c.method==='turn/start').length,2);
  agent.retire(r.call_id,r.brief_hash);
  assert.equal(sessions[0].closed,true);
  agent.close();
});
test('worker removes inherited interactive plugins, MCP servers, explicit apps and skills',()=>{
  const config=workerConfig(schemas,['mail.read'],{
    config:{mcp_servers:{node_repl:{}},plugins:{'browser@bundled':{enabled:true}},apps:{unrelated:{enabled:true}}},
    apps:[{id:'calendar'}],skillPaths:['/tmp/example/SKILL.md'],
  });
  assert.equal(config['features.multi_agent'],false);
  assert.equal(config['mcp_servers.node_repl.enabled'],false);
  assert.equal(config['plugins.browser@bundled.enabled'],false);
  assert.equal(config['apps.calendar.enabled'],false);
  assert.equal(config['apps.unrelated.enabled'],false);
  assert.equal(config['apps.mail_app.tools.send.enabled'],false);
  assert.equal(config['apps.mail_app.tools.read.enabled'],true);
  assert.deepEqual(config['skills.config'],[{path:'/tmp/example/SKILL.md',enabled:false}]);
  assert.equal(configValue(config['skills.config']),'[{"path"="/tmp/example/SKILL.md","enabled"=false}]');
  assert.throws(()=>configValue(null),/unsupported_config/);
});
test('an explicit call-worker source allowlist excludes unrelated connected tools',async()=>{
  const agent=new CodexCallAgent(new Session(),'/tmp',{sourceToolNames:[]});
  await agent.initialize();assert.deepEqual(agent.allowed,[]);agent.close();
  const bad=new CodexCallAgent(new Session(),'/tmp',{sourceToolNames:['mail.send']});
  await assert.rejects(()=>bad.initialize(),/unavailable_source_tool/);bad.close();
});
test('new revisions replace the reasoning session while preserving the frozen scope',async()=>{
  const sessions=[];
  const agent=new CodexCallAgent(new Session(),'/tmp',{createSession:config=>{
    const session=new Session();session.sourceAllowed=!!config['apps.mail_app.enabled'];sessions.push(session);return session;
  }});
  await agent.initialize();const r=request();
  await agent.worker(r);await agent.worker({...r,revision:2});
  assert.equal(sessions.length,2);assert.equal(sessions[0].closed,true);
  await assert.rejects(()=>agent.worker(r),/scope_changed/);
  agent.close();
});
test('supplied-facts-only scope disables connected tools even when the installation has them',async()=>{
  let selectedConfig;
  const agent=new CodexCallAgent(new Session(),'/tmp',{createSession:config=>{
    selectedConfig=config;const session=new Session();session.sourceAllowed=!!config['apps.mail_app.enabled'];return session;
  }});
  await agent.initialize();
  await agent.worker({...request(),context:{brief:{actions:[]}}});
  assert.equal(selectedConfig['apps._default.enabled'],false);
  assert.equal(selectedConfig['apps.mail_app.enabled'],false);
  agent.close();
});
test('unexpected non-app MCP tools refuse a call worker before inference',async()=>{
  const agent=new CodexCallAgent(new Session(),'/tmp',{createSession:()=>{
    const session=new Session();const fresh=session.freshWorker.bind(session);
    session.freshWorker=async(...args)=>({...await fresh(...args),mcpServers:[{name:'unexpected_executor',tools:['run']}]});
    return session;
  }});
  await agent.initialize();
  await assert.rejects(()=>agent.worker(request()),/tool_filter_not_enforced/);
  agent.close();
});
test('an app disabled in the signed-in account is not enabled for a call',async()=>{
  const discovery=new Session();discovery.sourceAllowed=false;
  const agent=new CodexCallAgent(discovery,'/tmp');
  assert.deepEqual(await agent.initialize(),{connected_read_tools:0,source_selection_protocol:1});
  assert.deepEqual(agent.allowed,[]);
  agent.close();
});
test('answers from connected tools need a quote in an actual allowed tool result',async()=>{
  const quote='The current postal code is 02139.';
  const answer={...advice,answer:'02139',supporting_quote:quote};
  const agent=new CodexCallAgent(new Session(),'/tmp',{createSession:()=>{
    const session=new Session();
    const source={id:'source',type:'mcpToolCall',tool:'mail.read',status:'completed',result:{content:[{type:'text',text:quote}]}};
    session.turnItems=[source,source,{id:'final',type:'agentMessage',phase:'final_answer',text:JSON.stringify(answer)}];
    return session;
  }});
  await agent.initialize();
  const result=await agent.advise({...request(),expires_ms:Date.now()+5000});
  assert.equal(result.tool_quote_verified,true);
  assert.equal(result.tool_calls,1);
  agent.close();
});
test('compact source evidence is verified against an actual tool call and usage is retained',async()=>{
  const quote='The current postal code is 02139.';
  const agent=new CodexCallAgent(new Session(),'/tmp',{createSession:()=>{
    const session=new Session();
    const rpc=session.rpc.bind(session);
    session.rpc=async(method,params)=>{
      if(method==='turn/start')session.onNotification({method:'thread/tokenUsage/updated',params:{
        threadId:'fixture-thread',tokenUsage:{last:{inputTokens:15000,cachedInputTokens:14000,outputTokens:30}}
      }});
      return rpc(method,params);
    };
    session.turnItems=[{type:'mcpToolCall',tool:'mail.read',status:'completed',result:{content:[{type:'text',text:quote}]}},
      {type:'agentMessage',phase:'final_answer',text:JSON.stringify({decision:{kind:'say',text:'02139',facts:[],quote}})}];
    return session;
  }});
  await agent.initialize();
  const result=await agent.advise({...request(),expires_ms:Date.now()+5000,
    output_schema:{type:'object',properties:{decision:{type:'object'}}}});
  assert.equal(result.tool_quote_verified,true);
  assert.equal(result.lifecycle.token_usage.last.cachedInputTokens,14000);
  assert.ok(result.lifecycle.output_bytes>0);
  agent.close();
});

const generalSchemas={...schemas,
  'records.lookup':{annotations:{readOnlyHint:true},_meta:{connector_id:'records_app'}},
  'notes.search':{annotations:{readOnlyHint:true},_meta:{connector_id:'notes_app'}},
};
class ScopedSession extends Session {
  constructor(config=null){super();this.config=config;this.schemas=generalSchemas;this.inherited={};}
  async freshWorker(cwd,options){
    this.options=options;
    return {threadId:'fixture-thread',availableTools:Object.keys(this.schemas),toolSchemas:this.schemas};
  }
  async rpc(method,params){
    if(method==='app/installed')return {apps:['mail_app','records_app','notes_app'].map(id=>({
      id,enabled:this.config===null||this.config[`apps.${id}.enabled`]===true,
      callable:!this.unavailableApp||this.unavailableApp!==id,
    }))};
    if(method==='config/read')return {config:this.inherited};
    return super.rpc(method,params);
  }
}
function scopedAgent(options={}) {
  const sessions=[],discovery=options.discovery??new ScopedSession();
  const agent=new CodexCallAgent(discovery,'/tmp',{...options,createSession:config=>{
    const session=new ScopedSession(config);options.modifySession?.(session);sessions.push(session);return session;
  }});
  return {agent,sessions};
}
const preparation=brief=>({...request(),expires_ms:Date.now()+5000,
  output_schema:{type:'object',properties:{decision:{type:'object'}}},context:{brief}});

test('a call selects general read-only plugins and keeps its scoped prewarmed thread',async()=>{
  const {agent,sessions}=scopedAgent();
  await agent.initialize();
  const r=preparation({actions:['read_call_sources'],source_tools:['records.lookup']});
  const prepared=await agent.prepare(r),again=await agent.prepare(r);
  assert.deepEqual(prepared.source_capabilities,{enabled:true,selection:'explicit',
    tool_names:['records.lookup'],connector_ids:['records_app']});
  assert.deepEqual(again.source_capabilities,prepared.source_capabilities);
  assert.equal(sessions[0].config['apps.records_app.tools.lookup.enabled'],true);
  assert.equal(sessions[0].config['apps.mail_app.enabled'],false);
  assert.equal(sessions[0].config['apps.notes_app.enabled'],false);
  const result=await agent.advise({...r,context:{...r.context,question:'Read the requested record.'}});
  assert.equal(result.lifecycle.thread_id,prepared.lifecycle.thread_id);
  assert.equal(sessions.length,1);
  assert.equal(sessions[0].calls.filter(call=>call.method==='turn/start').length,2);
  await assert.rejects(()=>agent.worker({...r,context:{brief:{...r.context.brief,source_tools:['notes.search']}}}),
    /configuration_changed/);
  agent.close();
});

test('explicit call selection cannot broaden daemon scope or select unavailable or write tools',async()=>{
  const {agent,sessions}=scopedAgent({sourceToolNames:['records.lookup']});
  await agent.initialize();
  for(const source_tools of [['notes.search'],['mail.send'],['missing.read'],['custom_mcp.read']])
    await assert.rejects(()=>agent.prepare(preparation({actions:['read_call_sources'],source_tools})),/unavailable_source_tool/);
  assert.equal(sessions.length,0);
  agent.close();
});

test('explicit source permission is necessary; an empty selection disables retrieval',async()=>{
  const {agent,sessions}=scopedAgent();
  await agent.initialize();
  await assert.rejects(()=>agent.prepare(preparation({actions:[],source_tools:['records.lookup']})),/source_access_not_authorized/);
  assert.equal(sessions.length,0);
  const prepared=await agent.prepare(preparation({actions:['read_call_sources'],source_tools:[]}));
  assert.deepEqual(prepared.source_capabilities,{enabled:false,selection:'none',tool_names:[],connector_ids:[]});
  assert.equal(sessions[0].config['apps.records_app.enabled'],false);
  assert.equal(sessions[0].config['apps.mail_app.enabled'],false);
  agent.close();
  const inherited=callSourceTools({context:{brief:{actions:[]}}},['records.lookup']);
  assert.deepEqual(inherited,{allowed:[],selection:'none'});
});

test('source-enabled calls cannot inherit an empty worker tool set before inference',async()=>{
  const {agent,sessions}=scopedAgent({connectedTools:false});
  await agent.initialize();
  await assert.rejects(()=>agent.prepare(preparation({actions:['read_call_sources']})),/unavailable_source_tool/);
  assert.equal(sessions.length,0);
  const suppliedOnly=await agent.prepare(preparation({actions:['read_call_sources'],source_tools:[]}));
  assert.equal(suppliedOnly.source_capabilities.selection,'none');
  agent.close();
});

test('missing or changed plugin capabilities fail prewarm before model inference',async()=>{
  for(const modifySession of [
    session=>{session.unavailableApp='records_app';},
    session=>{session.schemas={...generalSchemas};delete session.schemas['records.lookup'];},
    session=>{session.schemas={...generalSchemas,'records.lookup':{
      ...generalSchemas['records.lookup'],annotations:{readOnlyHint:false}}};},
    session=>{session.schemas={...generalSchemas,'records.lookup':{
      ...generalSchemas['records.lookup'],_meta:{connector_id:'different_app'}}};},
  ]) {
    const {agent,sessions}=scopedAgent({modifySession});
    await agent.initialize();
    await assert.rejects(()=>agent.prepare(preparation({actions:['read_call_sources'],source_tools:['records.lookup']})),
      /unavailable_source_tool/);
    assert.equal(sessions[0].closed,true);
    assert.equal(sessions[0].calls.some(call=>call.method==='turn/start'),false);
    agent.close();
  }
});

test('inherited selection respects installed per-tool restrictions without guessing relevant providers',async()=>{
  const discovery=new ScopedSession();
  discovery.inherited={apps:{mail_app:{tools:{read:{enabled:false}}},
    records_app:{default_tools_enabled:false,tools:{lookup:{enabled:true}}}}};
  const {agent}=scopedAgent({discovery});
  await agent.initialize();
  assert.deepEqual(agent.allowed,['records.lookup','notes.search']);
  const prepared=await agent.prepare(preparation({actions:['read_call_sources'],source_tools:null}));
  assert.deepEqual(prepared.source_capabilities,{enabled:true,selection:'inherited',
    tool_names:['records.lookup','notes.search'],connector_ids:['records_app','notes_app']});
  agent.close();
});

test('an observed tool call outside this call selection fails closed despite the broader daemon allowlist',async()=>{
  const quote='The record is current.';
  const {agent,sessions}=scopedAgent({modifySession:session=>{
    session.turnItems=[{type:'mcpToolCall',tool:'notes.search',status:'completed',result:{text:quote}},
      {type:'agentMessage',text:JSON.stringify({decision:{kind:'say',text:quote,facts:[],quote}})}];
  }});
  await agent.initialize();
  await assert.rejects(()=>agent.advise(preparation({actions:['read_call_sources'],source_tools:['records.lookup']})),
    /reasoner_tool_filter_not_enforced/);
  assert.equal(sessions[0].closed,true);
  assert.equal(agent.workers.size,0);
  agent.close();
});

test('malformed call source selections are rejected independently of the brief parser',()=>{
  for(const source_tools of ['records.lookup',['records.lookup','records.lookup'],['records. lookup'],
    [''],[true],Array.from({length:65},(_,index)=>`records.read_${index}`)])
    assert.throws(()=>callSourceTools({context:{brief:{actions:['read_call_sources'],source_tools}}},['records.lookup']),
      /invalid_source_tool_selection/);
});
