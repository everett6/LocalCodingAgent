const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs/promises');
const path=require('node:path');
const os=require('node:os');
const {execFileSync}=require('node:child_process');
const {createHash}=require('node:crypto');
const updater=require('../update.cjs');
const url=name=>`https://github.com/everett6/LocalCodingAgent/releases/download/ide-vscode-test/${name}`;
test('fork updater rejects assets outside the owned release channel',()=>{
  assert.throws(()=>updater.asset({assets:[{name:'manifest',browser_download_url:'https://evil.invalid/file'}]},'manifest'),/missing/);
});
test('native update verifies checksum, atomically installs fork and retains rollback copy',{skip:process.platform!=='linux'||process.arch!=='x64'},async()=>{
  const root=await fs.mkdtemp(path.join(os.tmpdir(),'local-coder-update-'));
  const destination=path.join(root,'ide'),payload=path.join(root,'payload');
  const tag='ide-vscode-test';
  const create=async(folder,release)=>{
    await fs.mkdir(path.join(folder,'resources/app'),{recursive:true});
    await fs.writeFile(path.join(folder,'resources/app/product.json'),JSON.stringify({applicationName:'local-coder-ide',...(release?{localCoderRelease:release}:{})}));
    await fs.writeFile(path.join(folder,'resources/app/package.json'),JSON.stringify({version:'1.143.1'}));
    await fs.writeFile(path.join(folder,'local-coder-ide'),'executable',{mode:0o755});
  };
  const original=global.fetch;
  try{
    await create(destination);await create(payload,tag);
    const archive=path.join(root,'release.tar.gz');execFileSync('tar',['-czf',archive,'-C',payload,'.']);
    const bytes=await fs.readFile(archive);
    const manifest={tag,version:'1.143.1',platform:'linux',arch:'x64',asset:'local-coder-ide-linux-x64.tar.gz',sha256:'0'.repeat(64)};
    const release={tag_name:tag,assets:['ide-linux-x64.json',manifest.asset].map(name=>({name,browser_download_url:url(name)}))};
    global.fetch=async location=>new Response(location.endsWith('.json')?JSON.stringify(manifest):bytes,{status:200});
    await assert.rejects(updater.install(release,path.join(destination,'local-coder-ide')),/checksum/);
    assert.equal(JSON.parse(await fs.readFile(path.join(destination,'resources/app/product.json'))).localCoderRelease,undefined);
    manifest.sha256=createHash('sha256').update(bytes).digest('hex');
    assert.equal(await updater.install(release,path.join(destination,'local-coder-ide')),true);
    assert.equal(JSON.parse(await fs.readFile(path.join(destination,'resources/app/product.json'))).localCoderRelease,tag);
    assert.ok((await fs.readdir(root)).some(name=>name.startsWith('ide.previous-')));
    assert.equal(await updater.install(release,path.join(destination,'local-coder-ide')),false);
  }finally{global.fetch=original;await fs.rm(root,{recursive:true,force:true});}
});
