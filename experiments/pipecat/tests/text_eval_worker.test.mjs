import test from 'node:test';
import assert from 'node:assert/strict';
import {FixtureTools,isolatedConfig} from '../text_eval_worker.mjs';

test('evaluation disables explicit apps and configured code execution MCPs',()=>{
  const config=isolatedConfig([{id:'gmail',enabled:true},{id:'calendar',enabled:true}],{mcp_servers:{node_repl:{},shell:{}}});
  assert.equal(config['apps.gmail.enabled'],false);
  assert.equal(config['apps.calendar.enabled'],false);
  assert.equal(config['mcp_servers.node_repl.enabled'],false);
  assert.equal(config['mcp_servers.shell.enabled'],false);
  assert.equal(config['features.shell_tool'],false);
  assert.equal(config['features.fast_mode'],false);
  assert.equal(config.service_tier,'default');
});
test('lookup fixtures execute failures, then success, without crossing record boundaries',async()=>{
  const tools=new FixtureTools([{record_key:'owned',field:'order_number',responses:[{status:'timeout',records:[]},{status:'ok',records:[{value:'0012'}]}]}]);
  const params={tool:'lookup_customer_record',arguments:{record_key:'owned',field:'order_number'}};
  assert.equal((await tools.call(params)).success,false);
  assert.equal((await tools.call(params)).success,true);
  await tools.call({...params,arguments:{record_key:'other',field:'order_number'}});
  assert.equal(tools.calls[2].result.status,'not_found');
  await assert.rejects(()=>tools.call({...params,tool:'gmail.read_email'}));
  assert.equal(new FixtureTools().calls.length,0);
});
