import fs from 'node:fs/promises';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {execFileSync} from 'node:child_process';
import {createHash} from 'node:crypto';
import {productPatch,packagePatch,patchBuild,patchAccounts,patchChatDefaults,logo,png,ico,icns} from './fork.mjs';
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const upstream=path.join(root,'upstream');
const pin=JSON.parse(await fs.readFile(path.join(root,'source','upstream.json'),'utf8'));
const check=process.argv.includes('--check');
const git=(...args)=>execFileSync('git',['-C',upstream,...args],{encoding:'utf8',maxBuffer:8*1024*1024}).trim();
const digest=data=>createHash('sha256').update(data).digest('hex');
async function exists(file){try{await fs.access(file);return true;}catch{return false;}}
if(!await exists(path.join(upstream,'.git'))){
  if(check)throw new Error('Upstream checkout is absent; run npm run prepare:source');
  await fs.mkdir(upstream,{recursive:true});
  if((await fs.readdir(upstream)).length)throw new Error('Refusing to initialize a nonempty upstream directory');
  execFileSync('git',['init',upstream],{stdio:'inherit'});
  git('remote','add','origin',pin.repository);
  execFileSync('git',['-C',upstream,'fetch','--depth','1','origin',pin.commit],{stdio:'inherit'});
  git('checkout','--detach',pin.commit);
}
if(git('rev-parse','HEAD')!==pin.commit)throw new Error('Upstream HEAD differs from source/upstream.json. Preserve local work and use a fresh ide/upstream checkout.');
const original=file=>execFileSync('git',['-C',upstream,'show',`${pin.commit}:${file}`],{maxBuffer:8*1024*1024});
if(original('.nvmrc').toString().trim()!==pin.node)throw new Error('Pinned Node version differs from upstream .nvmrc');
const outputs=new Map();
outputs.set('product.json',Buffer.from(JSON.stringify(productPatch(JSON.parse(original('product.json'))),null,'\t')+'\n'));
outputs.set('package.json',Buffer.from(JSON.stringify(packagePatch(JSON.parse(original('package.json'))),null,2)+'\n'));
outputs.set('build/gulpfile.vscode.ts',Buffer.from(patchBuild(original('build/gulpfile.vscode.ts').toString())));
const dirs=original('build/npm/dirs.ts').toString();
if(!dirs.includes("\t'extensions/copilot',"))throw new Error('Upstream npm layout changed: review the Copilot exclusion');
outputs.set('build/npm/dirs.ts',Buffer.from(dirs.replace("\t'extensions/copilot',\n",'')));
outputs.set('src/vs/workbench/contrib/welcomeOnboarding/browser/welcomeOnboarding.contribution.ts',await fs.readFile(path.join(root,'patches','welcomeOnboarding.contribution.ts')));
outputs.set('src/vs/workbench/services/accounts/browser/defaultAccount.ts',Buffer.from(patchAccounts(original('src/vs/workbench/services/accounts/browser/defaultAccount.ts').toString())));
outputs.set('src/vs/workbench/contrib/chat/browser/chat.shared.contribution.ts',Buffer.from(patchChatDefaults(original('src/vs/workbench/contrib/chat/browser/chat.shared.contribution.ts').toString())));
outputs.set('resources/linux/code.png',png(512));
outputs.set('resources/win32/code.ico',ico());
outputs.set('resources/darwin/code.icns',icns());
outputs.set('src/vs/workbench/browser/media/code-icon.svg',Buffer.from(logo));
// Include first-party runtime files only; tests and secrets are never bundled.
async function bundle(folder,relative=''){
  for(const item of await fs.readdir(folder,{withFileTypes:true})){
    if(['test','node_modules'].includes(item.name))continue;
    const rel=path.posix.join(relative,item.name),file=path.join(folder,item.name);
    if(item.isSymbolicLink())throw new Error('Extension bundle must not contain symlinks');
    if(item.isDirectory())await bundle(file,rel);
    else outputs.set(`extensions/local-coder/${rel}`,await fs.readFile(file));
  }
}
await bundle(path.join(root,'extension'));
let stamp={files:{}};
const stampPath=path.join(upstream,'.build','local-coder-fork.json');
if(await exists(stampPath))stamp=JSON.parse(await fs.readFile(stampPath,'utf8'));
const tracked=new Set(git('ls-files').split('\n'));
// Check everything before writing anything. Never reset or clean the user's checkout.
for(const [rel,data] of outputs){
  const file=path.join(upstream,rel);
  if(await exists(file)){
    const current=await fs.readFile(file);
    if(current.equals(data))continue;
    if(check)throw new Error(`Fork output differs: ${rel}; rerun prepare:source after preserving local edits`);
    const baseline=tracked.has(rel)?original(rel):null;
    if(!current.equals(baseline||Buffer.alloc(0))&&stamp.files[rel]!==digest(current))throw new Error(`Refusing to overwrite local changes: ${rel}`);
  }else if(check)throw new Error(`Missing fork output: ${rel}`);
}
if(!check){
  for(const [rel,data] of outputs){await fs.mkdir(path.dirname(path.join(upstream,rel)),{recursive:true});await fs.writeFile(path.join(upstream,rel),data);if(rel.includes('/bin/local-coder-engine')&&!rel.endsWith('.exe'))await fs.chmod(path.join(upstream,rel),0o755);}
  // Remove only stale files recorded by our previous successful preparation.
  for(const rel of Object.keys(stamp.files))if(!outputs.has(rel)&&rel.startsWith('extensions/local-coder/')){
    const file=path.join(upstream,rel);if(await exists(file)){if(digest(await fs.readFile(file))!==stamp.files[rel])throw new Error(`Preserve modified stale extension file: ${rel}`);await fs.unlink(file);}
  }
  await fs.mkdir(path.dirname(stampPath),{recursive:true});
  await fs.writeFile(stampPath,JSON.stringify({commit:pin.commit,files:Object.fromEntries([...outputs].map(([name,data])=>[name,digest(data)]))},null,2)+'\n');
}
console.log(`${check?'Verified':'Prepared'} ${pin.forkName}: Code OSS ${pin.tag} (${pin.commit}), ${outputs.size} fork files, Node ${pin.node}.`);
