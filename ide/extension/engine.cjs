'use strict';
const http = require('node:http');
const path = require('node:path');
const fs = require('node:fs');
const {discoverEngine}=require(fs.existsSync(path.join(__dirname,'discovery.cjs'))?'./discovery.cjs':'../../desktop/discovery.cjs');
const {spawn,execFile} = require('node:child_process');
const {randomBytes} = require('node:crypto');

function validateLoopbackURL(value) {
  const url = new URL(value);
  if (url.protocol !== 'http:' || url.hostname !== '127.0.0.1' || !url.port || url.username || url.password || url.pathname !== '/' || url.search || url.hash) {
    throw new Error('Engine must use an exact http://127.0.0.1:port loopback origin');
  }
  return url.origin;
}
function validateJob(job) {
  if (!job || typeof job.request !== 'string' || !job.request.trim() || job.request.length > 100000 ||
      !['coding','cybersecurity','bug_hunting','red_team'].includes(job.mode) || !['run','plan','review'].includes(job.action) ||
      (job.fix !== undefined && typeof job.fix !== 'boolean') ||
      (job.target_path !== undefined && (typeof job.target_path !== 'string' || job.target_path.length > 4096))) {
    throw new Error('Invalid task request, mode, action, or scope');
  }
  if(job.mode==='red_team'&&(!/^eng-[a-f0-9]{12}$/.test(job.engagement_id||'')||job.action!=='run'||job.fix))throw new Error('Red team requires a saved scope and run action');
  return {...job,request:job.request.trim()};
}
function validateProvider(provider) {
  if (!provider || !['openai_compatible','anthropic','ollama'].includes(provider.backend) ||
      typeof provider.model_id !== 'string' || !provider.model_id.trim() || provider.model_id.length > 256 ||
      !['generic','glm53','qwen38','openrouter_deepseek','openrouter_glm','openrouter_kimi'].includes(provider.profile) ||
      (provider.max_tokens !== undefined && (!Number.isInteger(provider.max_tokens) || provider.max_tokens < 128 || provider.max_tokens > 32768))) {
    throw new Error('Invalid provider protocol, model, profile, or output token budget');
  }
  const url = new URL(provider.base_url);
  if (!['http:','https:'].includes(url.protocol) || url.username || url.password || url.hash || url.search) throw new Error('Invalid provider endpoint');
  if (url.protocol === 'http:' && !['localhost','127.0.0.1','[::1]'].includes(url.hostname)) throw new Error('Remote providers require HTTPS');
  if(provider.auto_context!==undefined&&typeof provider.auto_context!=='boolean')throw new Error('Automatic context must be true or false');
  if(provider.context_length!==undefined&&(!Number.isInteger(provider.context_length)||provider.context_length<1024||provider.context_length>2000000))throw new Error('Invalid provider context limit');
  if(provider.context_source!==undefined&&(typeof provider.context_source!=='string'||provider.context_source.length>100||/[\x00-\x1f]/.test(provider.context_source)))throw new Error('Invalid context metadata source');
  for(const key of ['input_cost_per_million','output_cost_per_million','request_cost_usd'])if(provider[key]!==undefined&&provider[key]!==null&&(typeof provider[key]!=='number'||!Number.isFinite(provider[key])||provider[key]<0))throw new Error('Invalid provider pricing metadata');
  return {...provider};
}
function isAllowedEngineRequest(route, method) {
  if (typeof route !== 'string' || route.length > 4096 || /[\\#\r\n]/.test(route) || !['GET','POST'].includes(method)) return false;
  const pathname = route.split('?')[0];
  if (pathname === '/undo') return route === pathname && method === 'POST';
  if (pathname === '/integrations/search/doctor') return route === pathname && ['GET', 'POST'].includes(method);
  if (pathname === '/context/debugger') return route === pathname && method === 'GET';
  if (pathname === '/status' && route.includes('?summary')) return route === '/status?summary=1' && method === 'GET';
  if (!route.startsWith('/') || route.startsWith('//') || /%(2e|2f|5c|00)/i.test(pathname)) return false;
  try { if (new URL(route, 'http://127.0.0.1').pathname !== pathname) return false; } catch { return false; }

  if (method === 'GET' && ['/evaluations', '/findings', '/memory/retrievals', '/context/accounting', '/research/advisories/updates', '/inference/tuning/history', '/doctor'].includes(pathname)) return true;
  if (method === 'GET' && (/^\/evaluations\/[a-f0-9]{32}$/.test(pathname) || /^\/(evaluations|findings|inference\/tuning)\/jobs\/[a-f0-9]{32}$/.test(pathname) || /^\/findings\/[a-f0-9]{24}$/.test(pathname) || /^\/memory\/retrievals\/[a-f0-9]{32}$/.test(pathname))) return true;
  if (method === 'POST' && ['/evaluations/run', '/evaluations/model/run', '/evaluations/heldout/run', '/context/tokenizer', '/research/advisories/update', '/inference/tuning/start', '/inference/tuning/apply', '/doctor/repair'].includes(pathname)) return true;
  if (method === 'POST' && (/^\/(evaluations|findings|inference\/tuning)\/jobs\/[a-f0-9]{32}\/cancel$/.test(pathname) || /^\/findings\/[a-f0-9]{24}\/(review|prepare|verify|suppress|reopen)$/.test(pathname))) return true;

  if(method==='GET'&&['/research/capabilities','/research/evidence','/research/report','/research/dependencies','/research/advisories','/memory/structure'].includes(pathname))return true;
  if(method==='POST'&&['/research/audit','/research/sarif/import','/research/advisories/import'].includes(pathname))return true;
  if(method==='GET'&&['/providers/models','/providers/usage','/providers/quota','/providers/profiles','/inference/discovery'].includes(pathname))return true;
  if(method==='POST'&&(['/providers/discover','/providers/profiles/save','/inference/discover','/inference/discovery/cancel','/inference/discovery/select'].includes(pathname)||/^\/providers\/profiles\/[a-f0-9]{32}\/(select|delete)$/.test(pathname)))return true;
  if (/^\/(status|settings|workspace|diff|files|file|events|jobs|shutdown|engagements)(\/[^?#]*)?(\?[^#]*)?$/.test(route)) return true;
  if (method !== 'GET') return false;
  return ['/memory','/memory/status','/memory/search','/snapshots','/language/status'].includes(pathname)
    || /^\/snapshots\/[a-f0-9]{32}\/diff$/.test(pathname);
}
class EngineConnection {
  constructor(options) {
    this.options=options;
    this.child=null;
    this.url=null;
    this.token=null;
    this.stopping=false;
    this.closed=false;
    this.starting=null;
    this.owned=!options.sharedURL;
  }
  async start() {
    if(this.closed)throw new Error('Engine connection is closed');
    if(this.url)return;
    if(this.starting)return this.starting;
    this.starting=this._start().finally(()=>{this.starting=null;});
    return this.starting;
  }
  async _start() {
    this.stopping=false;
    if(!this.owned){
      this.url=validateLoopbackURL(this.options.sharedURL);
      if(!this.options.sharedToken){this.url=null;throw new Error('Shared engine access token is missing');}
      this.token=this.options.sharedToken;
      return;
    }
    if(await this._attachDiscovered())return;
    if(!this.options.executable)throw new Error('Build the Clin engine or configure its executable in user settings');
    this.token=randomBytes(32).toString('hex');
    const child=spawn(this.options.executable,this.options.args||[],{
      cwd:this.options.cwd||this.options.workspace,
      env:{...process.env,LOCAL_CODER_DESKTOP_PROJECT:this.options.workspace,LOCAL_CODER_REMOTE_TOKEN:this.token},
      stdio:['ignore','pipe','pipe'],windowsHide:true,shell:false,detached:process.platform!=='win32'
    });
    this.child=child;
    // Never log raw sidecar output: provider exceptions can include credentials.
    child.stderr.resume();
    try {
      await new Promise((resolve,reject)=>{
        let buffer='';
        const timer=setTimeout(()=>finish(new Error('Engine startup timed out')),this.options.startupTimeout||30000);
        const onError=()=>finish(new Error('Cannot start Clin engine executable'));
        const onExit=()=>finish(new Error('Clin engine exited before becoming ready'));
        const onData=chunk=>{
          buffer+=chunk.toString();
          if(buffer.length>65536){finish(new Error('Invalid engine readiness response'));return;}
          let index;
          while((index=buffer.indexOf('\n'))>=0){
            const line=buffer.slice(0,index);buffer=buffer.slice(index+1);
            try {
              const value=JSON.parse(line);
              if(Number.isInteger(value.port)&&value.port>0&&value.port<=65535){this.url=`http://127.0.0.1:${value.port}`;finish();return;}
            }catch{}
          }
        };
        const finish=error=>{
          clearTimeout(timer);child.off('error',onError);child.off('exit',onExit);child.stdout.off('data',onData);
          if(error)reject(error);else resolve();
        };
        child.once('error',onError);child.once('exit',onExit);child.stdout.on('data',onData);
      });
      child.stdout.resume();
      child.on('exit',()=>{this.url=null;this.token=null;this.child=null;if(!this.stopping)this.options.onExit?.();});
    }catch(error){
      await this.stop();
      for(let attempt=0;error.message.includes('exited')&&attempt<10;attempt++){
        if(await this._attachDiscovered()){this.closed=false;this.stopping=false;return;}
        await new Promise(resolve=>setTimeout(resolve,100));
      }
      throw error;
    }
  }
  async _attachDiscovered(){
    const connection=await discoverEngine(this.options.workspace,this.options.discoveryDirectory);
    if(!connection)return false;
    this.url=connection.origin;this.token=connection.token;this.owned=false;return true;
  }
  async request(route,method='GET',body,timeout=30000) {
    if(this.closed&&route!=='/shutdown')throw new Error('Engine connection is closed');
    if(!isAllowedEngineRequest(route,method))throw new Error('Unsupported engine API route or method');
    if(!this.url)await this.start();
    const payload=body===undefined?null:JSON.stringify(body);
    return new Promise((resolve,reject)=>{
      const req=http.request(new URL(route,this.url),{method,headers:{Authorization:`Bearer ${this.token}`,...(payload?{'Content-Type':'application/json','Content-Length':Buffer.byteLength(payload)}:{})},timeout},res=>{
        let data='';
        res.setEncoding('utf8');
        res.on('data',chunk=>{data+=chunk;if(data.length>8*1024*1024)req.destroy(new Error('Engine response exceeded size limit'));});
        res.on('end',()=>{
          try{
            const value=JSON.parse(data);
            if(res.statusCode>=400){reject(new Error(`Engine request failed (${res.statusCode}); check task status and provider settings`));return;}
            resolve(value);
          }catch{reject(new Error('Engine returned invalid JSON'));}
        });
        res.on('error',()=>reject(new Error('Engine response interrupted')));
      });
      req.on('timeout',()=>req.destroy(new Error('Engine request timed out')));
      req.on('error',()=>reject(new Error('Cannot reach Clin engine')));
      if(payload)req.write(payload);
      req.end();
    });
  }
  async stop() {
    this.stopping=true;this.closed=true;
    const child=this.child;
    if(this.owned&&child?.pid&&this.url){
      // The engine drains jobs and reaps tools started in separate sessions.
      // Older or unresponsive engines still receive process-tree cleanup below.
      try{
        await this.request('/shutdown','POST',{},15000);
        if(child.exitCode===null&&child.signalCode===null)await new Promise(resolve=>{
          const timer=setTimeout(resolve,1000);
          child.once('exit',()=>{clearTimeout(timer);resolve();});
        });
      }catch{}
    }
    this.url=null;this.token=null;this.child=null;
    if(!this.owned||!child||!child.pid||child.exitCode!==null||child.signalCode!==null)return;
    if(process.platform==='win32'){
      // The engine can own compiler/scanner grandchildren. Waiting for taskkill
      // ensures no owned tool survives the extension host shutdown.
      await new Promise(resolve=>execFile('taskkill',['/PID',String(child.pid),'/T','/F'],{windowsHide:true,timeout:5000},()=>resolve()));
      return;
    }
    // Python tools may start separate process groups. Snapshot their ancestry
    // before the engine exits so they cannot escape cleanup by being reparented.
    const groups=new Set([child.pid]);
    await new Promise(resolve=>execFile('ps',['-axo','pid=,ppid=,pgid='],{timeout:1000,maxBuffer:4*1024*1024},(error,stdout)=>{
      if(!error){
        const rows=stdout.trim().split('\n').map(line=>line.trim().split(/\s+/).map(Number));
        const descendants=new Set([child.pid]);
        let added=true;
        while(added){added=false;for(const [pid,ppid] of rows)if(descendants.has(ppid)&&!descendants.has(pid)){descendants.add(pid);added=true;}}
        for(const [pid,,group] of rows)if(descendants.has(pid)&&descendants.has(group))groups.add(group);
      }
      resolve();
    }));
    const signal=kind=>{for(const group of [...groups].reverse()){try{process.kill(-group,kind);}catch{}}};
    const alive=()=>[...groups].some(group=>{try{process.kill(-group,0);return true;}catch{return false;}});
    signal('SIGTERM');
    const deadline=Date.now()+1500;
    while(alive()&&Date.now()<deadline)await new Promise(resolve=>setTimeout(resolve,25));
    if(alive())signal('SIGKILL');
    // Reap the direct child before resolving; detached descendants are now killed.
    if(child.exitCode===null&&child.signalCode===null)await new Promise(resolve=>{
      const timer=setTimeout(resolve,1000);
      child.once('exit',()=>{clearTimeout(timer);resolve();});
    });
  }

}
module.exports={EngineConnection,validateLoopbackURL,validateJob,validateProvider,isAllowedEngineRequest};
