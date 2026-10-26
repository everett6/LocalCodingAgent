'use strict';
const vscode=require('vscode');
const fs=require('node:fs');
const path=require('node:path');
const crypto=require('node:crypto');
const {EngineConnection,validateJob,validateProvider}=require('./engine.cjs');
const {registerHarnessViews}=require('./harness-views.cjs');
let active;
const presets=[
  {label:'DeepSeek V4.1 Flash · OpenRouter',backend:'openai_compatible',model_id:'deepseek/deepseek-v4.1-flash-1017',base_url:'https://openrouter.ai/api/v1',profile:'openrouter_deepseek',api_key_env:'OPENROUTER_API_KEY',max_tokens:16384,context_length:1048576},
  {label:'GLM 5.3 · OpenRouter',backend:'openai_compatible',model_id:'z-ai/glm-5.3',base_url:'https://openrouter.ai/api/v1',profile:'openrouter_glm',api_key_env:'OPENROUTER_API_KEY',max_tokens:16384,context_length:1048576},
  {label:'Kimi K3.1 Pro · OpenRouter',backend:'openai_compatible',model_id:'moonshotai/kimi-k3.1-pro',base_url:'https://openrouter.ai/api/v1',profile:'openrouter_kimi',api_key_env:'OPENROUTER_API_KEY',max_tokens:16384,context_length:1048576},
  {label:'GLM 5.3 · Z.ai Coding Plan',backend:'openai_compatible',model_id:'glm-5.3',base_url:'https://api.z.ai/api/coding/paas/v4',profile:'glm53',max_tokens:16384},
  {label:'Qwen 3.8 · Model Studio',backend:'openai_compatible',model_id:'qwen3.8-max',base_url:'https://dashscope-intl.aliyuncs.com/compatible-mode/v1',profile:'qwen38',max_tokens:16384},
  {label:'Anthropic Messages',backend:'anthropic',model_id:'',base_url:'https://api.anthropic.com/v1',profile:'generic',max_tokens:8192},
  {label:'OpenAI-compatible provider',backend:'openai_compatible',model_id:'',base_url:'https://api.openai.com/v1',profile:'generic',max_tokens:4096},
  {label:'Local OpenAI-compatible server',backend:'openai_compatible',model_id:'',base_url:'http://localhost:8090/v1',profile:'generic',max_tokens:4096},
  {label:'Ollama',backend:'ollama',model_id:'',base_url:'http://localhost:11434',profile:'generic',max_tokens:4096}
];
function keyID(provider){return `provider:${crypto.createHash('sha256').update(JSON.stringify([provider.backend,provider.base_url,provider.model_id])).digest('hex')}`;}
function activate(context){
  const services=new Map();
  const changed=new vscode.EventEmitter();
  const output=vscode.window.createOutputChannel('Local Coder Activity');
  let disposed=false,refreshing=false;
  const documents=new Map();
  let documentID=0;
  context.subscriptions.push(output,changed,vscode.workspace.registerTextDocumentContentProvider('local-coder',{provideTextDocumentContent:uri=>documents.get(uri.toString())||''}));
  async function showDocument(title,text,language){
    const uri=vscode.Uri.from({scheme:'local-coder',path:`/${++documentID}/${title}`});
    documents.set(uri.toString(),String(text));
    const doc=await vscode.workspace.openTextDocument(uri);
    if(language)await vscode.languages.setTextDocumentLanguage(doc,language);
    await vscode.window.showTextDocument(doc,{preview:true});
  }
  context.subscriptions.push(vscode.workspace.onDidCloseTextDocument(doc=>documents.delete(doc.uri.toString())));
  function trusted(){if(!vscode.workspace.isTrusted)throw new Error('Trust this workspace before starting the Local Coder engine');}
  async function chooseFolder(){
    trusted();
    const folders=(vscode.workspace.workspaceFolders||[]).filter(folder=>folder.uri.scheme==='file');
    if(!folders.length)throw new Error('Open a local workspace folder first');
    if(folders.length===1)return folders[0];
    return vscode.window.showQuickPick(folders.map(folder=>({label:folder.name,description:folder.uri.fsPath,folder})),{placeHolder:'Choose the workspace for this task'}).then(value=>value?.folder);
  }
  function configKey(folder){return `provider:${folder.uri.fsPath}`;}
  async function service(folder){
    trusted();
    let entry=services.get(folder.uri.fsPath);
    if(entry)return entry.ready;
    const sharedURL=process.env.LOCAL_CODER_ENGINE_URL;
    const configured=vscode.workspace.getConfiguration('localCoder').inspect('engineExecutable')?.globalValue;
    const bundled=path.join(context.extensionPath,'bin',process.platform==='win32'?'local-coder-engine.exe':'local-coder-engine');
    let executable=configured|| (fs.existsSync(bundled)?bundled:undefined),args=[],cwd;
    if(!executable&&process.env.LOCAL_CODER_REPO_ROOT){
      executable=process.env.LOCAL_CODER_PYTHON||(process.platform==='win32'?'python':'python3');
      args=['-m','local_coder.desktop_engine'];cwd=process.env.LOCAL_CODER_REPO_ROOT;
    }
    const engine=new EngineConnection({workspace:folder.uri.fsPath,sharedURL,sharedToken:process.env.LOCAL_CODER_REMOTE_TOKEN,executable,args,cwd,onExit:()=>{if(services.get(folder.uri.fsPath)?.engine===engine)services.delete(folder.uri.fsPath);output.appendLine(`${folder.name}: engine stopped. Refresh to restart.`);changed.fire();}});
    entry={engine,folder,sessions:[],since:0,instance:null,error:null};
    services.set(folder.uri.fsPath,entry);
    entry.ready=(async()=>{
      const status=await engine.request('/status');
      if(fs.realpathSync(status.project_root)!==fs.realpathSync(folder.uri.fsPath))throw new Error('The shared engine belongs to another workspace; launch this folder separately');
      if(engine.owned){
        const provider=context.globalState.get(configKey(folder));
        if(provider){const api_key=await context.secrets.get(keyID(provider));await engine.request('/settings','POST',{...validateProvider(provider),...(api_key?{api_key}:{})});}
      }
      return entry;
    })().catch(async error=>{services.delete(folder.uri.fsPath);await engine.stop();throw error;});
    return entry.ready;
  }
  registerHarnessViews(vscode,context,async()=>{const folder=await chooseFolder();return folder?service(folder):undefined;});
  const tree={onDidChangeTreeData:changed.event,getTreeItem:item=>item,getChildren:async item=>{
    if(!item)return (vscode.workspace.workspaceFolders||[]).filter(folder=>folder.uri.scheme==='file').map(folder=>{
      const node=new vscode.TreeItem(folder.name,vscode.TreeItemCollapsibleState.Expanded);node.folder=folder;node.contextValue='localCoder.workspace';
      const entry=services.get(folder.uri.fsPath);node.description=entry?.error?'Disconnected':entry?'Connected':'Ready';node.tooltip=entry?.error||folder.uri.fsPath;return node;
    });
    const entry=services.get(item.folder.uri.fsPath);
    if(!entry)return[];
    return entry.sessions.map(session=>{
      const node=new vscode.TreeItem(session.request||session.session_id);node.description=session.status||session.phase;node.tooltip=`${session.mode||'coding'} · ${session.phase||''}`;
      node.iconPath=new vscode.ThemeIcon(['queued','running','cancelling'].includes(session.status)?'sync~spin':session.status==='failed'?'error':'check');
      node.command={command:'localCoder.openResult',title:'Open result',arguments:[{folder:item.folder,session}]};return node;
    });
  }};
  context.subscriptions.push(vscode.window.registerTreeDataProvider('localCoder.tasks',tree));
  async function refresh(connect=false){
    if(refreshing||disposed||!vscode.workspace.isTrusted)return;
    refreshing=true;
    try{
      for(const folder of vscode.workspace.workspaceFolders||[]){
        if(folder.uri.scheme!=='file')continue;
        let entry=services.get(folder.uri.fsPath);
        try{
          if(!entry&&!connect)continue;
          entry=await service(folder);
          const status=await entry.engine.request('/status');entry.sessions=status.sessions||[];entry.error=null;
          for(const session of entry.sessions){
            if(!['queued','running','cancelling'].includes(session.status))continue;
            // The event sequence is global, so each running session needs its own cursor.
            entry.cursors??=new Map();
            const cursor=entry.cursors.get(session.session_id)||{seq:0,instance:null};
            let events=await entry.engine.request(`/events?session_id=${encodeURIComponent(session.session_id)}&since=${cursor.seq}`);
            if(cursor.instance&&cursor.instance!==events.instance_id)events=await entry.engine.request(`/events?session_id=${encodeURIComponent(session.session_id)}&since=0`);
            entry.cursors.set(session.session_id,{seq:events.latest_seq,instance:events.instance_id});
            for(const event of events.events||[])output.appendLine(`[${folder.name} / ${session.session_id}] ${event.source||''} ${event.event_type||''}: ${event.message||''}`);
          }
        }catch(error){if(entry)entry.error=error.message;else output.appendLine(`${folder.name}: ${error.message}`);}
      }
      changed.fire();
    }finally{refreshing=false;}
  }
  const register=(command,handler)=>context.subscriptions.push(vscode.commands.registerCommand(command,async(...args)=>{try{return await handler(...args);}catch(error){vscode.window.showErrorMessage(`Local Coder: ${error.message}`);}}));
  async function startJob(mode,action){
    const folder=await chooseFolder();if(!folder)return;
    const entry=await service(folder);
    const configured=await entry.engine.request('/settings');
    if(!configured.provider){await configureProvider(folder);if(!(await entry.engine.request('/settings')).provider)return;}
    const request=await vscode.window.showInputBox({title:`Local Coder · ${mode.replace('_',' ')}`,prompt:action==='review'?'What should the review focus on?':'Describe the task',value:action==='review'?'Review the current workspace changes for correctness, bugs, and security issues.':'',ignoreFocusOut:true});
    if(!request?.trim())return;
    if(mode==='red_team'){
      const listing=await entry.engine.request('/engagements');
      const choices=[{label:'Create a new engagement'},...(listing.engagements||[]).map(item=>({label:item.name,description:item.id,id:item.id}))];
      const choice=await vscode.window.showQuickPick(choices,{placeHolder:'Choose the saved scope for this red team task'});if(!choice)return;
      let engagement_id=choice.id;
      if(!engagement_id){
        const paths=await vscode.window.showInputBox({title:'In-scope local paths',prompt:'Comma-separated paths inside this workspace',value:'.'});if(paths===undefined)return;
        const excluded=await vscode.window.showInputBox({title:'Excluded local paths',prompt:'Comma-separated paths; optional',value:''});if(excluded===undefined)return;
        const check=await vscode.window.showQuickPick([{label:'Local source review only',active:false},{label:'Enable authorized HTTP observations',active:true}],{placeHolder:'Choose engagement capabilities'});if(!check)return;
        let origins='',authorization='',excluded_urls='';
        if(check.active){
          origins=await vscode.window.showInputBox({title:'Authorized HTTP origins',prompt:'Exact HTTP(S) origins, comma separated; no wildcards'});if(origins===undefined)return;
          authorization=await vscode.window.showInputBox({title:'Authorization reference',prompt:'Rules of engagement or lab approval reference; no credentials'});if(authorization===undefined)return;
          excluded_urls=await vscode.window.showInputBox({title:'Excluded URLs',prompt:'Comma-separated full URL path prefixes; optional',value:''});if(excluded_urls===undefined)return;
        }
        const split=value=>value.split(',').map(item=>item.trim()).filter(Boolean);
        const created=await entry.engine.request('/engagements','POST',{name:request.slice(0,100),objective:request,paths:split(paths),excluded_paths:split(excluded),origins:split(origins),excluded_urls:split(excluded_urls),authorization,active_http:check.active,max_requests:20});
        engagement_id=created.id;
      }
      await entry.engine.request('/jobs','POST',validateJob({request,mode,action:'run',engagement_id,fix:false}));
      output.show(true);await vscode.commands.executeCommand('localCoder.tasks.focus');await refresh();return;
    }
    let target_path='.',fix=false;
    if(mode!=='coding'){
      target_path=await vscode.window.showInputBox({title:'Investigation scope',prompt:'Path inside this workspace',value:'.'});if(target_path===undefined)return;
      const selected=await vscode.window.showQuickPick([{label:'Investigate and report',fix:false},{label:'Apply fixes and run tests',fix:true}],{placeHolder:'Choose investigation behavior'});if(!selected)return;fix=selected.fix;
    }
    await entry.engine.request('/jobs','POST',validateJob({request,mode,action,target_path,fix}));
    output.show(true);await vscode.commands.executeCommand('localCoder.tasks.focus');await refresh();
  }
  async function configureProvider(givenFolder){
    const folder=givenFolder||await chooseFolder();if(!folder)return;
    const preset=await vscode.window.showQuickPick(presets,{placeHolder:'Choose your provider (model access and billing are supplied by your account)'});if(!preset)return;
    const model_id=await vscode.window.showInputBox({title:'Provider model ID',value:preset.model_id,prompt:'Exact model ID enabled on your account'});if(model_id===undefined)return;
    const base_url=await vscode.window.showInputBox({title:'Provider endpoint',value:preset.base_url,prompt:'HTTPS endpoint, or HTTP on localhost for a local model'});if(base_url===undefined)return;
    const profiles=[{label:'Standard harness',value:'generic'},{label:'GLM 5.3 reasoning harness',value:'glm53'},{label:'Qwen 3.8 thinking and tools harness',value:'qwen38'},{label:'OpenRouter DeepSeek harness',value:'openrouter_deepseek'},{label:'OpenRouter GLM harness',value:'openrouter_glm'},{label:'OpenRouter Kimi harness',value:'openrouter_kimi'}];
    profiles.sort((a,b)=>Number(b.value===preset.profile)-Number(a.value===preset.profile));
    const profile=await vscode.window.showQuickPick(profiles,{placeHolder:'Choose the model harness',ignoreFocusOut:true});if(!profile)return;
    const budget=await vscode.window.showInputBox({title:'Maximum output tokens',value:String(preset.max_tokens),validateInput:value=>Number.isInteger(Number(value))&&Number(value)>=128&&Number(value)<=32768?undefined:'Enter an integer from 128 to 32768'});if(budget===undefined)return;
    const provider=validateProvider({backend:preset.backend,model_id,base_url,profile:profile.value,max_tokens:Number(budget),...(preset.context_length?{context_length:preset.context_length}:{})});
    const api_key_env=await vscode.window.showInputBox({title:'Optional API key environment variable',prompt:'Leave blank to use a saved or pasted key; the engine process must inherit this variable',value:preset.api_key_env||''});if(api_key_env===undefined)return;
    if(api_key_env&&!/^[A-Za-z_][A-Za-z0-9_]*$/.test(api_key_env))throw new Error('Enter a valid environment variable name');
    if(api_key_env)provider.api_key_env=api_key_env;
    let api_key;
    if(!api_key_env){api_key=await vscode.window.showInputBox({title:'Provider API key',prompt:'Saved in the editor SecretStorage; leave blank to reuse a saved key or use an unauthenticated local model',password:true,ignoreFocusOut:true});if(api_key===undefined)return;api_key=api_key||await context.secrets.get(keyID(provider));}
    const entry=await service(folder);
    await entry.engine.request('/settings','POST',{...provider,...(api_key?{api_key}:{})});
    if(api_key)await context.secrets.store(keyID(provider),api_key);
    await context.globalState.update(configKey(folder),provider);
    vscode.window.showInformationMessage(`Local Coder connected to ${model_id} with the ${profile.value} harness.`);
    await refresh();
  }
  register('localCoder.run',()=>startJob('coding','run'));
  register('localCoder.plan',()=>startJob('coding','plan'));
  register('localCoder.review',()=>startJob('coding','review'));
  register('localCoder.security',()=>startJob('cybersecurity','run'));
  register('localCoder.bugs',()=>startJob('bug_hunting','run'));
  register('localCoder.redteam',()=>startJob('red_team','run'));
  register('localCoder.engagementReport',async()=>{const folder=await chooseFolder();if(!folder)return;const entry=await service(folder);const listing=await entry.engine.request('/engagements');const chosen=await vscode.window.showQuickPick((listing.engagements||[]).map(item=>({label:item.name,description:item.id,id:item.id})),{placeHolder:'Choose an engagement report'});if(!chosen)return;const report=await entry.engine.request(`/engagements/${chosen.id}/report`);await showDocument('red-team-report.md',report.markdown,'markdown');});
  register('localCoder.configureProvider',()=>configureProvider());
  register('localCoder.forgetProviderKey',async()=>{const folder=await chooseFolder();if(!folder)return;const provider=context.globalState.get(configKey(folder));if(provider)await context.secrets.delete(keyID(provider));vscode.window.showInformationMessage('Saved provider key removed. The running engine retains its in-memory key until it closes.');});
  register('localCoder.refresh',()=>refresh(true));
  register('localCoder.activity',()=>output.show());
  register('localCoder.diff',async()=>{const folder=await chooseFolder();if(!folder)return;const entry=await service(folder);const diff=await entry.engine.request('/diff');await showDocument('workspace.diff',diff.diff||'No workspace changes.','diff');});
  register('localCoder.openResult',async item=>{if(!item)return;const entry=await service(item.folder);const result=await entry.engine.request(`/jobs/${encodeURIComponent(item.session.session_id)}`);await showDocument('task-result.json',JSON.stringify(result,null,2),'json');});
  register('localCoder.cancel',async()=>{
    const folder=await chooseFolder();if(!folder)return;const entry=await service(folder);
    const {sessions}=await entry.engine.request('/status');
    const selected=await vscode.window.showQuickPick((sessions||[]).filter(session=>['queued','running'].includes(session.status)).map(session=>({label:session.request,description:session.session_id,session})),{placeHolder:'Choose a task to cancel'});if(!selected)return;
    await entry.engine.request(`/jobs/${encodeURIComponent(selected.session.session_id)}/cancel`,'POST',{});await refresh();
  });
  let updateBusy=false;
  async function updateIDE(manual=false){
    if(updateBusy)return;
    if([...services.values()].some(entry=>(entry.sessions||[]).some(session=>['queued','running','cancelling'].includes(session.status)))){if(manual)vscode.window.showInformationMessage('Stop active agent tasks before updating the IDE.');return;}
    const updateSetting=vscode.workspace.getConfiguration('localCoder').inspect('autoUpdate');
    if(!manual&&(updateSetting?.globalValue??updateSetting?.defaultValue)!==true)return;
    updateBusy=true;
    try{
      const updater=require('./update.cjs');
      const release=await updater.latest();
      if(!release){if(manual)vscode.window.showInformationMessage('No validated Local Coder IDE release is published yet.');return;}
      if(manual&&await vscode.window.showInformationMessage('Install the latest validated Local Coder IDE fork? Restart the IDE after installation.', 'Install')!=='Install')return;
      const installed=await updater.install(release);
      if(installed)vscode.window.showInformationMessage('Local Coder IDE updated. Restart the IDE to use the new version. Your previous installation is retained beside the current app.');
      else if(manual)vscode.window.showInformationMessage('Local Coder IDE is already up to date.');
    }catch(error){if(manual)vscode.window.showErrorMessage(error.message);else output.appendLine(`IDE update: ${error.message}`);}
    finally{updateBusy=false;}
  }
  register('localCoder.updateIDE',()=>updateIDE(true));
  const updateTimer=setTimeout(()=>void updateIDE(),30000);
  const updateInterval=setInterval(()=>void updateIDE(),6*60*60*1000);
  context.subscriptions.push({dispose:()=>{clearTimeout(updateTimer);clearInterval(updateInterval);}});
  const interval=setInterval(()=>void refresh(),1500);
  context.subscriptions.push({dispose:()=>clearInterval(interval)},vscode.workspace.onDidChangeWorkspaceFolders(async()=>{
    const roots=new Set((vscode.workspace.workspaceFolders||[]).map(folder=>folder.uri.fsPath));
    for(const [root,entry] of services){if(!roots.has(root)){services.delete(root);await entry.engine.stop();}}
    changed.fire();
  }));
  active={stop:async()=>{disposed=true;clearInterval(interval);await Promise.all([...services.values()].map(entry=>entry.engine.stop()));services.clear();}};
}
async function deactivate(){await active?.stop();active=undefined;}
module.exports={activate,deactivate};
