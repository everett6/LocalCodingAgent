'use strict';
const http = require('node:http');
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
      !['coding','cybersecurity','bug_hunting'].includes(job.mode) || !['run','plan','review'].includes(job.action) ||
      (job.fix !== undefined && typeof job.fix !== 'boolean') ||
      (job.target_path !== undefined && (typeof job.target_path !== 'string' || job.target_path.length > 4096))) {
    throw new Error('Invalid task request, mode, action, or scope');
  }
  return {...job,request:job.request.trim()};
}
function validateProvider(provider) {
  if (!provider || !['openai_compatible','anthropic','ollama'].includes(provider.backend) ||
      typeof provider.model_id !== 'string' || !provider.model_id.trim() || provider.model_id.length > 256 ||
      !['generic','glm53','qwen38'].includes(provider.profile) ||
      (provider.max_tokens !== undefined && (!Number.isInteger(provider.max_tokens) || provider.max_tokens < 128 || provider.max_tokens > 32768))) {
    throw new Error('Invalid provider protocol, model, profile, or output token budget');
  }
  const url = new URL(provider.base_url);
  if (!['http:','https:'].includes(url.protocol) || url.username || url.password || url.hash || url.search) throw new Error('Invalid provider endpoint');
  if (url.protocol === 'http:' && !['localhost','127.0.0.1','[::1]'].includes(url.hostname)) throw new Error('Remote providers require HTTPS');
  return {...provider};
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
    if(!this.options.executable)throw new Error('Build the Local Coder engine or configure its executable in user settings');
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
        const onError=()=>finish(new Error('Cannot start Local Coder engine executable'));
        const onExit=()=>finish(new Error('Local Coder engine exited before becoming ready'));
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
    }catch(error){await this.stop();throw error;}
  }
  async request(route,method='GET',body,timeout=30000) {
    if(this.closed&&route!=='/shutdown')throw new Error('Engine connection is closed');
    if(!this.url)await this.start();
    if(!/^\/(status|settings|workspace|diff|files|file|events|jobs|shutdown)(\/[^?#]*)?(\?[^#]*)?$/.test(route))throw new Error('Unsupported engine API route');
    if(!['GET','POST'].includes(method))throw new Error('Unsupported engine API method');
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
      req.on('error',()=>reject(new Error('Cannot reach Local Coder engine')));
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
module.exports={EngineConnection,validateLoopbackURL,validateJob,validateProvider};
