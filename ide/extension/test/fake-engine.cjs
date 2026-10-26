const http=require('node:http');
const server=http.createServer((req,res)=>{
  if(req.headers.authorization!==`Bearer ${process.env.LOCAL_CODER_REMOTE_TOKEN}`){res.writeHead(401);res.end('{}');return;}
  res.setHeader('Content-Type','application/json');
  if(req.url==='/shutdown'){
    res.setHeader('Connection','close');req.resume();res.end('{}');server.close(()=>process.exit(0));return;
  }
  res.end(JSON.stringify({sessions:[],project_root:process.env.LOCAL_CODER_DESKTOP_PROJECT}));
});
server.listen(0,'127.0.0.1',()=>console.log(JSON.stringify({port:server.address().port})));
