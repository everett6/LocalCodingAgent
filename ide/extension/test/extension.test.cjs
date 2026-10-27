const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const http=require('node:http');
const {createRequire}=require('node:module');

test('native commands keep credentials in SecretStorage and submit real provider, harness, and investigation payloads',async()=>{
  const requests=[],commands=new Map(),inputs=[],picks=[],messages=[],state=new Map(),secrets=new Map(),subscriptions=[];
  let provider=null,sessions=[],catalogUnavailable=false;
  const server=http.createServer(async(req,res)=>{
    let text='';for await(const chunk of req)text+=chunk;
    const body=text?JSON.parse(text):undefined;
    requests.push({url:req.url,method:req.method,body,auth:req.headers.authorization});
    res.setHeader('Content-Type','application/json');
    let value={};
    if(req.url==='/status')value={project_root:process.cwd(),sessions};
    if(req.url==='/settings'&&req.method==='GET')value={provider};
    if(req.url==='/settings'&&req.method==='POST'){provider={...body,api_key:undefined,key_set:!!body.api_key};value={provider};}
    if(req.url==='/providers/models?source=openrouter'){if(catalogUnavailable){res.writeHead(503);res.end('{}');return;}value={models:[{id:'vendor/catalog-model',name:'Catalog model',context_length:262144,max_output_tokens:16384,profile:'generic',input_cost_per_million:0,output_cost_per_million:0.2}],source:'network'};}
    if(req.url==='/engagements'&&req.method==='GET')value={engagements:[]};
    if(req.url==='/engagements'&&req.method==='POST')value={id:'eng-0123456789ab',scope:body};
    if(req.url==='/jobs'){sessions=[{session_id:'test-job',request:body.request,status:'running',phase:'run'}];value=sessions[0];}
    if(req.url.startsWith('/events'))value={events:[{source:'test',event_type:'progress',message:'Investigating'}],latest_seq:1,instance_id:'first'};
    res.end(JSON.stringify(value));
  });
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  const folder={name:'workspace',uri:{scheme:'file',fsPath:process.cwd()}};
  const noop=()=>({dispose(){}});
  const outputs=[],outputNames=[];
  const fake={
    EventEmitter:class{constructor(){this.event=()=>({dispose(){}});}fire(){}dispose(){}},
    TreeItem:class{constructor(label,state){this.label=label;this.collapsibleState=state;}},TreeItemCollapsibleState:{Expanded:2},ThemeIcon:class{},
    workspace:{isTrusted:true,workspaceFolders:[folder],registerTextDocumentContentProvider:noop,onDidCloseTextDocument:noop,onDidChangeWorkspaceFolders:noop,getConfiguration:()=>({inspect:()=>({})})},
    window:{
      createOutputChannel:name=>{outputNames.push(name);return{appendLine:value=>outputs.push(value),show(){},dispose(){}};},registerTreeDataProvider:noop,
      showQuickPick:async items=>{const selected=picks.shift();return typeof selected==='function'?selected(items):items[selected??0];},
      showInputBox:async options=>{inputs.push(options);return answers.shift();},
      showErrorMessage:value=>messages.push(value),showInformationMessage:value=>messages.push(value)
    },
    commands:{registerCommand:(id,fn)=>{commands.set(id,fn);return{dispose:()=>commands.delete(id)};},executeCommand:async()=>{}},
    Uri:{from:value=>({toString:()=>JSON.stringify(value)})},languages:{}
  };
  const answers=['my-model','https://provider.invalid/v1','8192','','provider-super-secret'];
  const context={subscriptions,extensionPath:path.resolve(__dirname,'..'),globalState:{get:key=>state.get(key),update:async(key,value)=>state.set(key,value)},secrets:{get:async key=>secrets.get(key),store:async(key,value)=>secrets.set(key,value),delete:async key=>secrets.delete(key)}};
  const extensionPath=path.resolve(__dirname,'../extension.cjs');
  const localRequire=createRequire(extensionPath);
  const sandbox={require:name=>name==='vscode'?fake:localRequire(name),module:{exports:{}},process:{platform:process.platform,env:{LOCAL_CODER_ENGINE_URL:`http://127.0.0.1:${server.address().port}`,LOCAL_CODER_REMOTE_TOKEN:'engine-secret'}},setInterval,clearInterval,setTimeout,clearTimeout,console};
  vm.runInNewContext(fs.readFileSync(extensionPath,'utf8'),sandbox,{filename:extensionPath});
  try{
    sandbox.module.exports.activate(context);
    const manifest=require('../package.json');
    assert.equal(manifest.displayName,'Clin');assert.equal(manifest.name,'local-coder');assert.equal(manifest.publisher,'local-coder');
    assert.ok(manifest.contributes.commands.every(item=>item.title.startsWith('Clin:')));
    assert.deepEqual(outputNames,['Clin Activity']);
    for(const command of ['localCoder.languageContext','localCoder.snapshotDiff','localCoder.memoryInspector']){
      assert.equal(typeof commands.get(command),'function');
      assert.ok(manifest.contributes.commands.some(item=>item.command===command));
    }
    picks.push(items=>items.find(item=>item.label.includes('Z.ai')));
    await commands.get('localCoder.configureProvider')();
    const posted=requests.find(item=>item.url==='/settings'&&item.method==='POST').body;
    assert.equal(posted.api_key,'provider-super-secret');assert.equal(posted.profile,'glm53');assert.equal(posted.model_id,'my-model');
    assert.deepEqual([...secrets.values()],['provider-super-secret']);assert.ok(!JSON.stringify([...state.values()]).includes('provider-super-secret'));
    assert.equal(inputs.at(-1).password,true);
    answers.push('deepseek/deepseek-v4.1-flash-1017','https://openrouter.ai/api/v1','16384','OPENROUTER_API_KEY');
    picks.push(items=>items.find(item=>item.profile==='openrouter_deepseek'));
    await commands.get('localCoder.configureProvider')();
    const openrouter=requests.filter(item=>item.url==='/settings'&&item.method==='POST').at(-1).body;
    assert.equal(openrouter.profile,'openrouter_deepseek');assert.equal(openrouter.context_length,1048576);assert.equal(openrouter.api_key_env,'OPENROUTER_API_KEY');
    answers.push('8192','OPENROUTER_API_KEY');
    picks.push(items=>items.find(item=>item.catalog),items=>items.find(item=>item.model?.id==='vendor/catalog-model'));
    await commands.get('localCoder.configureProvider')();
    const catalogProvider=requests.filter(item=>item.url==='/settings'&&item.method==='POST').at(-1).body;
    assert.equal(catalogProvider.model_id,'vendor/catalog-model');assert.equal(catalogProvider.context_length,262144);assert.equal(catalogProvider.auto_context,true);assert.equal(catalogProvider.input_cost_per_million,0);assert.equal(catalogProvider.output_cost_per_million,0.2);
    catalogUnavailable=true;
    answers.push('vendor/custom-manual','https://openrouter.ai/api/v1','4096','OPENROUTER_API_KEY');
    picks.push(items=>items.find(item=>item.catalog));
    await commands.get('localCoder.configureProvider')();
    const manualProvider=requests.filter(item=>item.url==='/settings'&&item.method==='POST').at(-1).body;
    assert.equal(manualProvider.model_id,'vendor/custom-manual');assert.equal(manualProvider.auto_context,true);
    assert.ok(messages.some(message=>message.includes('catalog is unavailable')));
    answers.push('Find reproducible security issues','.');picks.push(1);
    await commands.get('localCoder.security')();
    const job=requests.find(item=>item.url==='/jobs').body;
    assert.equal(job.mode,'cybersecurity');assert.equal(job.fix,true);assert.equal(job.target_path,'.');assert.equal(job.action,'run');
    await commands.get('localCoder.cancel')();
    assert.ok(requests.some(item=>item.url==='/jobs/test-job/cancel'));
    answers.push('Map application trust boundaries','src','src/private');picks.push(0,0);
    await commands.get('localCoder.redteam')();
    const engagement=requests.find(item=>item.url==='/engagements'&&item.method==='POST').body;
    assert.deepEqual(engagement.paths,['src']);assert.deepEqual(engagement.excluded_paths,['src/private']);assert.equal(engagement.active_http,false);
    const redteam=requests.filter(item=>item.url==='/jobs').at(-1).body;
    assert.equal(redteam.mode,'red_team');assert.equal(redteam.engagement_id,'eng-0123456789ab');assert.equal(redteam.fix,false);
    assert.ok(requests.every(item=>item.auth==='Bearer engine-secret'));
    assert.ok(!outputs.join('\n').includes('secret'));
    assert.ok(!messages.some(message=>message.startsWith('Clin:')));
    fake.workspace.isTrusted=false;
    const before=requests.length;await commands.get('localCoder.run')();
    assert.equal(requests.length,before);assert.ok(messages.at(-1).includes('Trust this workspace'));
    assert.ok(messages.at(-1).startsWith('Clin:'));
  }finally{
    await sandbox.module.exports.deactivate();
    for(const item of subscriptions)item.dispose();
    await new Promise(resolve=>server.close(resolve));
  }
});
