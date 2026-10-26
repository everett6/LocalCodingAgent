const assert=require('node:assert/strict'),fs=require('node:fs'),os=require('node:os'),path=require('node:path');
const root=path.resolve(__dirname,'../../..');
const {Sidecar}=require(path.join(root,'desktop/sidecar.cjs')); 
const {EngineConnection}=require(path.join(root,'ide/extension/engine.cjs'));
(async()=>{
const executable=path.resolve(process.argv[2]||path.join(root,'dist/local-coder-engine'));
const args=process.argv.includes('--python')?['-m','local_coder.desktop_engine']:[];
for(const order of ['manager-first','ide-first','simultaneous']){
 const workspace=fs.mkdtempSync(path.join(os.tmpdir(),'actual-engine-sharing-'));
 const manager=new Sidecar({command:executable,args,cwd:workspace,env:{...process.env,LOCAL_CODER_DESKTOP_PROJECT:workspace,LOCAL_CODER_REMOTE_TOKEN:'manager-test-token'},token:'manager-test-token'});
 const ide=new EngineConnection({workspace,executable,args});
 try{
  if(order==='manager-first'){await manager.start();await ide.start();}
  else if(order==='ide-first'){await ide.start();await manager.start();}
  else await Promise.all([manager.start(),ide.start()]);
  assert.equal(manager.origin,ide.url);assert.equal(manager.token,ide.token);
  assert.equal(Number(!!manager.child)+Number(!!ide.child),1);
  assert.equal((await ide.request('/status')).project_root,workspace);
  if(manager.child){await ide.stop();assert.equal((await fetch(manager.origin+'/status',{headers:{Authorization:'Bearer '+manager.token}})).status,200);await manager.stop();}
  else{await manager.stop();assert.equal((await ide.request('/status')).project_root,workspace);await ide.stop();}
  console.log(order+': one authenticated engine; both clients connected; borrower shutdown preserved owner');
 }finally{await manager.stop();await ide.stop();fs.rmSync(workspace,{recursive:true,force:true});}
}
})().catch(e=>{console.error(e.message);process.exitCode=1;});
