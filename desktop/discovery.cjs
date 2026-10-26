'use strict';
const fs=require('node:fs');
const path=require('node:path');
const os=require('node:os');
const http=require('node:http');
const {createHash}=require('node:crypto');
function canonical(workspace) {
  const value=fs.realpathSync(workspace).split(path.sep).join('/');
  return process.platform==='win32'?value.toLowerCase():value;
}
async function discoverEngine(workspace, directory=path.join(os.homedir(),'.local-coder','connections')) {
  if(!workspace)return null;
  try {
    const root=canonical(workspace),file=path.join(directory,createHash('sha256').update(root).digest('hex')+'.json');
    for(const target of [directory,file]) {
      const stat=fs.lstatSync(target);
      if((target===directory?!stat.isDirectory():!stat.isFile())||stat.isSymbolicLink()||(process.platform!=='win32'&&(stat.uid!==process.getuid()||(stat.mode&0o077))))return null;
    }
    if(fs.statSync(file).size>4096)return null;
    const value=JSON.parse(fs.readFileSync(file,'utf8')),url=new URL(value.origin);
    if(value.workspace!==root||typeof value.token!=='string'||!value.token||value.token.length>512||!Number.isInteger(value.pid)||value.pid<=0)return null;
    if(url.protocol!=='http:'||url.hostname!=='127.0.0.1'||!url.port||url.username||url.password||url.pathname!=='/'||url.search||url.hash)return null;
    process.kill(value.pid,0);
    const matches=await new Promise(resolve=>{
      const request=http.get(new URL('/identity',url),{headers:{Authorization:`Bearer ${value.token}`},timeout:1000},response=>{
        let body='';response.setEncoding('utf8');
        response.on('data',chunk=>{body+=chunk;if(body.length>65536)request.destroy();});
        response.on('error',()=>resolve(false));
        response.on('end',()=>{try{resolve(response.statusCode===200&&canonical(JSON.parse(body).project_root)===root);}catch{resolve(false);}});
      });
      request.on('timeout',()=>request.destroy());request.on('error',()=>resolve(false));
    });
    return matches?{origin:url.origin,token:value.token}:null;
  } catch {return null;}
}
module.exports={discoverEngine};
