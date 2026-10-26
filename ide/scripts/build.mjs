import fs from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {spawn} from 'node:child_process';
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const upstream=path.join(root,'upstream');
const pin=JSON.parse(fs.readFileSync(path.join(root,'source','upstream.json'),'utf8'));
const operation=process.argv[2]||'compile';
if(!['install','compile','package','electron'].includes(operation))throw new Error('Use build.mjs install|compile|package|electron');
const current=process.versions.node.split('.').map(Number),required=pin.node.split('.').map(Number);
if(current[0]!==required[0]||current[1]<required[1]||(current[1]===required[1]&&current[2]<required[2]))throw new Error(`Upstream requires Node ${pin.node} or later in major ${required[0]}. Select the version in ide/upstream/.nvmrc first. Current: ${process.versions.node}`);
async function run(command,args,cwd=upstream){
  await new Promise((resolve,reject)=>{const child=spawn(command,args,{cwd,stdio:'inherit',shell:false});child.once('error',reject);child.once('exit',code=>code===0?resolve():reject(new Error(`${command} exited with ${code}`)));});
}
await run(process.execPath,[path.join(root,'scripts','prepare.mjs'),'--check'],root);
// Invoke npm's JS entry directly: Windows .cmd parsing must never touch workspace paths.
let npmCLI=process.env.npm_execpath;
if(!npmCLI){
  const candidates=[path.join(path.dirname(process.execPath),'node_modules/npm/bin/npm-cli.js'),path.resolve(path.dirname(process.execPath),'../lib/node_modules/npm/bin/npm-cli.js')];
  npmCLI=candidates.find(file=>fs.existsSync(file));
}
if(!npmCLI)throw new Error('Run via npm run install:upstream, npm run compile, or npm run build so npm_execpath identifies npm');
const npm=(args)=>run(process.execPath,[npmCLI,...args]);
if(operation==='install')await npm(['ci']);
if(operation==='compile')await npm(['run','compile']);
if(operation==='electron')await npm(['run','electron']);
if(operation==='package'){
  const arch=process.env.LOCAL_CODER_BUILD_ARCH||process.arch;
  if(!['x64','arm64','armhf'].includes(arch)||!['win32','darwin','linux'].includes(process.platform))throw new Error('Unsupported native build target');
  const engine=path.join(root,'extension','bin',process.platform==='win32'?'local-coder-engine.exe':'local-coder-engine');
  if(!fs.existsSync(engine))throw new Error('Package the Python engine first and copy it into ide/extension/bin, then rerun prepare:source');
  await npm(['run','gulp',`vscode-${process.platform}-${arch}`]);
  console.log(`Unsigned native application output: ${path.join(root,`VSCode-${process.platform}-${arch}`)}`);
}
