const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),os=require('node:os'),path=require('node:path'),crypto=require('node:crypto'),http=require('node:http');
const {discoverEngine}=require('../discovery.cjs');
test('discovery rejects stale, unsafe, and cross-workspace descriptors',async()=>{
  const realWorkspace=fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(),'discovery-'))).split(path.sep).join('/');
  const workspace=process.platform==='win32'?realWorkspace.toLowerCase():realWorkspace,directory=path.join(workspace,'connections');fs.mkdirSync(directory,{mode:0o700});
  const file=path.join(directory,crypto.createHash('sha256').update(workspace).digest('hex')+'.json');
  let hits=0;
  const server=http.createServer((req,res)=>{hits++;res.end(JSON.stringify({project_root:os.tmpdir()}));});
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  const valid={workspace,origin:`http://127.0.0.1:${server.address().port}`,token:'secret',pid:process.pid};
  try{
    for(const override of [{origin:'https://example.org'},{workspace:os.tmpdir()},{pid:2147483647},{}]){
      fs.writeFileSync(file,JSON.stringify({...valid,...override}),{mode:0o600});
      assert.equal(await discoverEngine(workspace,directory),null);
    }
    assert.equal(hits,1,'Invalid identities and off-loopback URLs must never be contacted');
    if(process.platform!=='win32'){fs.chmodSync(file,0o644);assert.equal(await discoverEngine(workspace,directory),null);assert.equal(hits,1);}
  }finally{await new Promise(resolve=>server.close(resolve));fs.rmSync(workspace,{recursive:true,force:true});}
});
