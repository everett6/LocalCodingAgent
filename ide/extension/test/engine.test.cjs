const test = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');
const path = require('node:path');
const {EngineConnection, validateLoopbackURL, validateJob, validateProvider} = require('../engine.cjs');

test('accepts only exact loopback HTTP engine origins', () => {
  assert.equal(validateLoopbackURL('http://127.0.0.1:1234'), 'http://127.0.0.1:1234');
  for (const url of ['https://example.org', 'http://127.0.0.1.evil:1234', 'http://localhost:1234', 'http://127.0.0.1:1234/path', 'http://u:p@127.0.0.1:1234']) {
    assert.throws(() => validateLoopbackURL(url), /loopback/);
  }
});
test('validates bounded job input and provider protocol before transport', () => {
  assert.equal(validateJob({request:'Inspect',mode:'bug_hunting',action:'run',target_path:'.',fix:false}).fix,false);
  assert.throws(() => validateJob({request:' ',mode:'coding',action:'run'}));
  assert.throws(() => validateJob({request:'x',mode:'bad',action:'run'}));
  assert.throws(() => validateJob({request:'x',mode:'coding',action:'run',fix:'yes'}));
  assert.throws(() => validateProvider({backend:'other',model_id:'x',base_url:'http://localhost:1',profile:'generic'}));
  assert.throws(() => validateProvider({backend:'ollama',model_id:'x',base_url:'file:///tmp/key',profile:'generic'}));
  assert.equal(validateProvider({backend:'openai_compatible',model_id:'qwen',base_url:'https://example.org/v1',profile:'qwen38',max_tokens:8192}).profile,'qwen38');
});
test('shared engine authenticates requests and is not terminated on dispose', async () => {
  let seen,shutdowns=0;
  const server=http.createServer((req,res)=>{if(req.url==='/shutdown')shutdowns++;seen=req.headers.authorization;res.setHeader('Content-Type','application/json');res.end(JSON.stringify({project_root:'/tmp',sessions:[]}));});
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  try {
    const engine=new EngineConnection({workspace:'/tmp',sharedURL:`http://127.0.0.1:${server.address().port}`,sharedToken:'secret'});
    await engine.start();
    assert.deepEqual(await engine.request('/status'),{project_root:'/tmp',sessions:[]});
    assert.equal(seen,'Bearer secret');
    await engine.stop();
    assert.equal(server.listening,true);
    assert.equal(shutdowns,0);
    await assert.rejects(engine.request('/status'),/closed/);
  } finally {await new Promise(resolve=>server.close(resolve));}
});
test('starts owned sidecar, exchanges requests, and terminates it', async () => {
  const engine=new EngineConnection({workspace:process.cwd(),executable:process.execPath,args:[path.join(__dirname,'fake-engine.cjs')],startupTimeout:3000});
  await engine.start();
  assert.ok(engine.child.pid);
  assert.deepEqual(await engine.request('/status'),{sessions:[],project_root:process.cwd()});
  const child=engine.child,pid=child.pid;
  await engine.stop();
  assert.throws(()=>process.kill(pid,0));
  assert.equal(child.exitCode,0,'Owned engine should drain through authenticated /shutdown');
  assert.equal(child.signalCode,null);
});
test('startup failure is reported without exposing child output or credentials', async () => {
  const engine=new EngineConnection({workspace:process.cwd(),executable:process.execPath,args:['-e',"console.error(process.env.LOCAL_CODER_REMOTE_TOKEN); process.exit(2)"],startupTimeout:2000});
  await assert.rejects(engine.start(), /exited/);
  assert.equal(engine.child,null);
});
test('missing owned executable rejects without hanging cleanup', async () => {
  const engine=new EngineConnection({workspace:process.cwd(),executable:path.join(__dirname,'does-not-exist'),startupTimeout:200});
  await Promise.race([
    assert.rejects(engine.start(),/Cannot start/),
    new Promise((_,reject)=>{const timer=setTimeout(()=>reject(new Error('cleanup hung')),1000);timer.unref();})
  ]);
});
test('owned shutdown kills a tool descendant even if it has its own process group and ignores termination', async () => {
  const fs=require('node:fs');
  const os=require('node:os');
  const workspace=fs.mkdtempSync(path.join(os.tmpdir(),'local-coder-engine-tree-'));
  const engine=new EngineConnection({workspace,executable:process.execPath,args:[path.join(__dirname,'fake-engine-tree.cjs')],startupTimeout:3000});
  let descendant;
  try{
    await engine.start();
    descendant=Number(fs.readFileSync(path.join(workspace,'descendant.pid'),'utf8'));
    assert.ok(descendant>0);
    await engine.stop();
    let alive=true;
    for(let n=0;n<30;n++){
      try{
        process.kill(descendant,0);
        if(process.platform==='linux'&&fs.readFileSync(`/proc/${descendant}/stat`,'utf8').split(') ')[1].startsWith('Z')){alive=false;break;}
      }catch{alive=false;break;}
      await new Promise(resolve=>setTimeout(resolve,20));
    }
    assert.equal(alive,false,'Owned tool descendant must not survive engine shutdown');
  }finally{
    if(descendant){try{process.kill(descendant,'SIGKILL');}catch{}}
    await engine.stop();fs.rmSync(workspace,{recursive:true,force:true});
  }
});
