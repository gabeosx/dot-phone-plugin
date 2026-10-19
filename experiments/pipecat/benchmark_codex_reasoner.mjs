// Text-only model comparison through the production signed-in call agent.
// No Realtime client, API key, carrier, or enabled connected source tools.
import fs from 'node:fs/promises';
import path from 'node:path';
import crypto from 'node:crypto';
import {CodexCallAgent} from './codex_call_reasoner.mjs';
import {SubscriptionAppServer} from './subscription_app_server.mjs';

const [inputPath,outputPath]=process.argv.slice(2);
if(!inputPath||!outputPath)throw Error('input_and_output_required');
const input=JSON.parse(await fs.readFile(inputPath,'utf8'));
const directory=path.dirname(path.resolve(outputPath));
await fs.mkdir(directory,{recursive:true,mode:0o700});
const claim=await fs.open(outputPath,'wx',0o600);
await claim.close();
const report={mode:'signed_in_codex_text_only',phone_calls:0,realtime_requests:0,
  prewarmed:input.prewarm===true,input_sha256:crypto.createHash('sha256').update(await fs.readFile(inputPath)).digest('hex'),
  connected_source_queries:0,repeats:input.repeats,rows:[],setups:[],errors:[],
  configurations:input.configurations,source_sha256:{}};
for(const name of ['codex_call_reasoner.mjs','subscription_app_server.mjs'])
  report.source_sha256[name]=crypto.createHash('sha256').update(
    await fs.readFile(new URL(name,import.meta.url))).digest('hex');
const save=()=>fs.writeFile(outputPath,JSON.stringify(report,null,2)+'\n',{mode:0o600});
let agent,measurement,confirmedModel;
const stop=()=>{agent?.close();};
process.on('SIGTERM',()=>{stop();process.exit(1);});
process.on('SIGINT',()=>{stop();process.exit(1);});
try {
  for(let repeat=0;repeat<input.repeats;repeat++) {
    // Rotate model order across repetitions to reduce time/order bias.
    const configurations=[...input.configurations.slice(repeat),...input.configurations.slice(0,repeat)];
    for(const configuration of configurations) {
      if(report.errors.some(e=>e.configuration===configuration.name))continue;
      const createSession=config=>{
        const session=new SubscriptionAppServer('codex',{config});
        const rpc=session.rpc.bind(session);
        session.rpc=async(method,params,...rest)=>{
          const result=await rpc(method,method==='turn/start'?{...params,effort:configuration.effort}:params,...rest);
          if(method==='thread/start')confirmedModel=result.model??null;
          return result;
        };
        return session;
      };
      agent=new CodexCallAgent(new SubscriptionAppServer(),directory,{connectedTools:false,createSession});
      const observe=agent.observe.bind(agent);
      agent.observe=message=>{
        if(measurement&&message.params?.threadId===measurement.thread_id) {
          if(message.method==='item/agentMessage/delta'&&measurement.first_token_ms===null)
            measurement.first_token_ms=Date.now()-measurement.started;
          if(message.method==='turn/completed')measurement.turn_error=message.params.turn.error??null;
          if(message.method==='thread/tokenUsage/updated')measurement.token_usage=message.params.tokenUsage;
        }
        observe(message);
      };
      try {
        await agent.initialize();
        const scope={call_id:crypto.randomBytes(16).toString('hex'),revision:1,brief_hash:input.brief_hash};
        const request=(item)=>({...scope,schema:1,request_id:crypto.randomBytes(16).toString('hex'),
          nonce:crypto.randomBytes(32).toString('hex'),created_ms:Date.now(),expires_ms:Date.now()+50000,
          instructions:input.instructions,model:configuration.model,effort:configuration.effort,
          output_schema:input.schema,context:item.context});
        const client=await agent.worker(request(input.cases[0]));
        let preparation;
        if(input.prewarm)preparation=await agent.prepare(request({context:{...input.cases[0].context,
          recent_transcript:[],prior_advice:[],retrieved_sources:[],review_report:null,question:'Await the first recipient turn.'}}));
        report.setups.push({configuration:configuration.name,repeat:repeat+1,...client.audit,
          confirmed_model:confirmedModel,preparation:preparation?.lifecycle??null});
        for(const [index,item] of input.cases.entries()) {
          measurement={started:Date.now(),thread_id:client.threadId,first_token_ms:null};
          const result=await agent.advise(request(item));
          const row={configuration:configuration.name,model:configuration.model,effort:configuration.effort,
            confirmed_model:confirmedModel,repeat:repeat+1,case:item.id,first_interaction_turn:index===0,
            first_model_turn:index===0&&!input.prewarm,
            elapsed_ms:Date.now()-measurement.started,first_token_ms:measurement.first_token_ms,
            token_usage:measurement.token_usage??null,advice:result.advice,lifecycle:result.lifecycle};
          report.rows.push(row);measurement=null;await save();
          process.stdout.write(JSON.stringify({configuration:row.configuration,repeat:row.repeat,
            case:row.case,elapsed_ms:row.elapsed_ms})+'\n');
        }
      } catch(error) {
        report.errors.push({configuration:configuration.name,repeat:repeat+1,error:error.message,
          turn_error:measurement?.turn_error??null});
        await save();
        process.stdout.write(JSON.stringify(report.errors.at(-1))+'\n');
      } finally {measurement=null;agent.close();agent=null;}
    }
  }
} finally {stop();await save();}
