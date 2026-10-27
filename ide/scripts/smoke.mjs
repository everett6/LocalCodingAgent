import fs from 'node:fs/promises';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {randomBytes} from 'node:crypto';
import {createServer} from 'node:http';
import {spawn} from 'node:child_process';
import assert from 'node:assert/strict';
import {findLaunch} from './launch.mjs';
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
await fs.mkdir(path.join(root,'.build'),{recursive:true});
const workspace=await fs.mkdtemp(path.join(root,'.build','vscode-smoke-'));
const profile=path.join(workspace,'profile');
const token=randomBytes(32).toString('hex');
const seen=[];
const server=createServer((req,res)=>{
  seen.push({route:req.url,authenticated:req.headers.authorization===`Bearer ${token}`});
  res.setHeader('Content-Type','application/json');
  if(req.headers.authorization!==`Bearer ${token}`){res.writeHead(401);res.end('{}');return;}
  if(req.url==='/status'){res.end(JSON.stringify({project_root:workspace,sessions:[]}));return;}
  res.end('{}');
});
await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
const spec=findLaunch({preferDevelopment:process.env.LOCAL_CODER_SMOKE_DEV==='1'});
const env={...process.env,LOCAL_CODER_ENGINE_URL:`http://127.0.0.1:${server.address().port}`,LOCAL_CODER_REMOTE_TOKEN:token};
delete env.ELECTRON_RUN_AS_NODE;
if(spec.development){env.VSCODE_DEV='1';env.VSCODE_CLI='1';env.NODE_ENV='development';}
const extensionPath=spec.development?path.join(root,'extension'):path.resolve(spec.cwd,process.platform==='darwin'?'../Resources/app/extensions/local-coder':'resources/app/extensions/local-coder');
env.LOCAL_CODER_SMOKE_EXTENSION_PATH=extensionPath;
const log=await fs.open(path.join(root,'.build','vscode-smoke.log'),'w');
try{
  const args=[...spec.args,'--new-window','--user-data-dir',profile,'--extensions-dir',path.join(workspace,'extensions'),'--extensionDevelopmentPath',extensionPath,'--extensionTestsPath',path.join(root,'test','vscode-smoke.cjs'),'--disable-workspace-trust','--skip-welcome','--skip-release-notes','--disable-extension=GitHub.copilot','--disable-extension=GitHub.copilot-chat',workspace];
  if(process.platform==='linux'&&process.env.LOCAL_CODER_SMOKE_NATIVE!=='1')args.unshift('--ozone-platform=x11','--disable-gpu');
  if(process.env.LOCAL_CODER_SMOKE_DEBUG_PORT)args.push(`--remote-debugging-port=${process.env.LOCAL_CODER_SMOKE_DEBUG_PORT}`);
  if(process.env.LOCAL_CODER_SMOKE_NO_SANDBOX==='1')args.unshift('--no-sandbox');
  const code=await new Promise((resolve,reject)=>{
    const child=spawn(spec.executable,args,{cwd:spec.cwd,env,stdio:['ignore',log.fd,log.fd],shell:false});
    const timeout=setTimeout(()=>{child.kill('SIGKILL');reject(new Error('IDE smoke timed out; see ide/.build/vscode-smoke.log'));},90000);
    child.once('error',error=>{clearTimeout(timeout);reject(error);});child.once('exit',code=>{clearTimeout(timeout);resolve(code);});
  });
  assert.equal(code,0,'Actual IDE extension host smoke failed; see ide/.build/vscode-smoke.log');
  assert.ok(seen.filter(value=>value.route==='/status').length>=2,'The real extension host must check workspace and refresh status');
  assert.ok(seen.every(value=>value.authenticated),'Every real extension-host request must authenticate');
  console.log('Actual Clin IDE extension-host smoke passed: native activation, command registration, workspace verification, authenticated shared-engine refresh.');
}finally{await log.close();await new Promise(resolve=>server.close(resolve));}
