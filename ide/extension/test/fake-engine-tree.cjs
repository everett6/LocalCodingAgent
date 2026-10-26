const {spawn}=require('node:child_process');
const http=require('node:http');
const fs=require('node:fs');
const child=spawn(process.execPath,['-e',"process.on('SIGTERM',()=>{}); console.log('ready'); setInterval(()=>{},1000)"],{detached:process.platform!=='win32',stdio:['ignore','pipe','ignore']});
child.stdout.once('data',()=>{
  fs.writeFileSync(process.env.LOCAL_CODER_DESKTOP_PROJECT+'/descendant.pid',String(child.pid));
  const server=http.createServer((req,res)=>res.end('{}'));
  server.listen(0,'127.0.0.1',()=>console.log(JSON.stringify({port:server.address().port})));
});
