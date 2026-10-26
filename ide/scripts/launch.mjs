import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {createHash} from 'node:crypto';
import {spawn} from 'node:child_process';
import {createRequire} from 'node:module';
const require=createRequire(import.meta.url);
const {validateLoopbackURL}=require('../extension/engine.cjs');
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
export function findLaunch({platform=process.platform,arch=process.arch,executable=process.env.LOCAL_CODER_IDE_EXECUTABLE,preferDevelopment=false,exists=fs.existsSync}={}){
  const native=platform==='darwin'?['Local Coder IDE.app','Contents','MacOS','Local Coder IDE']: [platform==='win32'?'Local Coder IDE.exe':'local-coder-ide'];
  const packaged=path.join(root,`VSCode-${platform}-${arch}`,...native);
  if(executable){if(!path.isAbsolute(executable)||!exists(executable))throw new Error('LOCAL_CODER_IDE_EXECUTABLE must name an existing absolute executable path');return{executable,args:[],cwd:path.dirname(executable),development:false};}
  const dev=path.join(root,'upstream','.build','electron',...native);
  const devReady=exists(dev)&&exists(path.join(root,'upstream','out','main.js'));
  const development={executable:dev,args:['.'],cwd:path.join(root,'upstream'),development:true};
  if(preferDevelopment&&devReady)return development;
  if(exists(packaged))return{executable:packaged,args:[],cwd:path.dirname(packaged),development:false};
  if(devReady)return development;
  throw new Error('Local Coder IDE is not built. In ide/: prepare:source, install:upstream, compile, then build or fetch Electron. See ide/README.md.');
}
export function launchArguments(workspace,env=process.env){
  const args=['--new-window','--disable-extension=GitHub.copilot','--disable-extension=GitHub.copilot-chat'];
  if(env.LOCAL_CODER_ENGINE_URL){
    validateLoopbackURL(env.LOCAL_CODER_ENGINE_URL);
    if(!env.LOCAL_CODER_REMOTE_TOKEN)throw new Error('A shared engine URL requires its access token');
    // Editor CLI normally reuses an existing process. A manager-instance profile
    // prevents the extension host from inheriting an expired shared engine token.
    const instance=createHash('sha256').update(env.LOCAL_CODER_REMOTE_TOKEN).digest('hex').slice(0,16);
    args.push('--user-data-dir',path.join(env.LOCAL_CODER_IDE_DATA_HOME||path.join(os.homedir(),'.local-coder-ide-manager'),instance));
  }
  args.push(workspace);
  return args;
}
if(process.argv[1]&&path.resolve(process.argv[1])===fileURLToPath(import.meta.url)){
  try{
    const workspace=fs.realpathSync(process.argv[2]||process.cwd());
    if(!fs.statSync(workspace).isDirectory())throw new Error('Choose a workspace directory');
    const spec=findLaunch();
    const env={...process.env};
    // Electron's manager parent may be running Node mode to execute this launcher.
    delete env.ELECTRON_RUN_AS_NODE;
    if(spec.development){env.VSCODE_DEV='1';env.VSCODE_CLI='1';env.NODE_ENV='development';env.LOCAL_CODER_REPO_ROOT=path.dirname(root);}
    const child=spawn(spec.executable,[...spec.args,...launchArguments(workspace,env)],{cwd:spec.cwd,env,stdio:'inherit',shell:false});
    child.once('error',error=>{console.error(`Cannot launch Local Coder IDE: ${error.message}`);process.exitCode=1;});
    child.once('exit',code=>{process.exitCode=code??1;});
  }catch(error){console.error(error.message);process.exitCode=1;}
}
