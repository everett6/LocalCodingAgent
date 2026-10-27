export function productPatch(source){
  const product={...source,
    nameShort:'Clin IDE',nameLong:'Clin IDE',applicationName:'local-coder-ide',
    dataFolderName:'.local-coder-ide',sharedDataFolderName:'.local-coder-ide-shared',
    win32MutexName:'localcoderide',win32DirName:'Local Coder IDE',win32NameVersion:'Local Coder IDE',
    win32RegValueName:'LocalCoderIDE',win32AppUserModelId:'LocalCoder.IDE',win32ShellNameShort:'Local Coder IDE',
    win32x64AppId:'{{D33FC175-3797-41ED-82D7-31F1D38A97C1}',win32arm64AppId:'{{AFF16ABA-08EA-474D-B072-127BA425AF1C}',
    win32x64UserAppId:'{{BD8472CE-4BC6-435C-ACB1-888FDC33892E}',win32arm64UserAppId:'{{FF6BB02D-51A5-4D24-B13C-222491BEBC14}',
    win32TunnelServiceMutex:'localcoderide-tunnelservice',win32TunnelMutex:'localcoderide-tunnel',
    darwinBundleIdentifier:'dev.localcoder.ide',darwinProfileUUID:'B456454C-137D-4657-9417-E3746BB0F77A',
    darwinProfilePayloadUUID:'19AC4E84-1B3B-4898-AC3A-5E26DB32240B',linuxDesktopName:'dev.localcoder.IDE',linuxIconName:'local-coder-ide',
    urlProtocol:'local-coder-ide',serverApplicationName:'local-coder-server',serverDataFolderName:'.local-coder-server',
    tunnelApplicationName:'local-coder-tunnel',enableTelemetry:false,builtInExtensions:[],
    licenseName:'MIT',licenseUrl:'https://github.com/microsoft/vscode/blob/e646868227c3e3a9e3f24277c8c7220be42c0be7/LICENSE.txt',
    serverLicenseUrl:'https://github.com/microsoft/vscode/blob/e646868227c3e3a9e3f24277c8c7220be42c0be7/LICENSE.txt'
  };
  for(const key of Object.keys(product))if(/telemetry|applicationInsights|aiConfig|copilot|defaultChatAgent|extensionsGallery|updateUrl|downloadUrl|reportIssueUrl|webviewContentExternal|experiments|recommendations/i.test(key))delete product[key];
  product.enableTelemetry=false;
  return product;
}
export function packagePatch(source){
  const value=structuredClone(source);
  value.scripts.compile='npm run compile-client';
  value.scripts.watch='npm-run-all2 -lp watch-client-transpile watch-client watch-extensions';
  value.scripts['watch-transpile']='npm-run-all2 -lp watch-client-transpile watch-extensions';
  return value;
}
export function patchBuild(source){
  const required=[', compileCopilotExtensionBuildTask','\t\t\tcompileCopilotExtensionBuildTask,','\t\t\tprepareCopilotRipgrepShimTask(platform, arch, destinationFolderName)'];
  for(const needle of required)if(!source.includes(needle))throw new Error('Upstream build changed: review the Copilot exclusion patch');
  return source.replace(', compileCopilotExtensionBuildTask','')
    .replace('\t\t\tcompileCopilotExtensionBuildTask,','')
    .replace('\t\t\tprepareCopilotRipgrepShimTask(platform, arch, destinationFolderName)','');
}
export const logo='<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 128 128"><rect width="128" height="128" rx="28" fill="#25272a"/><path d="M84 38a36 36 0 1 0 0 52M75 64h20" fill="none" stroke="#b5b9ed" stroke-width="10" stroke-linecap="round"/></svg>\n';
// Stored DEFLATE blocks make generated icons byte-identical across Node/zlib versions.
function deflateStored(data){
  const blocks=[Buffer.from([0x78,0x01])];
  for(let offset=0;offset<data.length;offset+=65535){
    const length=Math.min(65535,data.length-offset),header=Buffer.alloc(5);
    header[0]=offset+length===data.length?1:0;header.writeUInt16LE(length,1);header.writeUInt16LE((~length)&65535,3);
    blocks.push(header,data.subarray(offset,offset+length));
  }
  let a=1,b=0;for(const value of data){a=(a+value)%65521;b=(b+a)%65521;}
  const checksum=Buffer.alloc(4);checksum.writeUInt32BE(((b<<16)|a)>>>0);blocks.push(checksum);
  return Buffer.concat(blocks);
}
function crc32(buffer){let crc=0xffffffff;for(const byte of buffer){crc^=byte;for(let n=0;n<8;n++)crc=(crc>>>1)^((crc&1)?0xedb88320:0);}return(crc^0xffffffff)>>>0;}
function chunk(type,data){const name=Buffer.from(type);const result=Buffer.alloc(data.length+12);result.writeUInt32BE(data.length,0);name.copy(result,4);data.copy(result,8);result.writeUInt32BE(crc32(Buffer.concat([name,data])),result.length-4);return result;}
function segmentDistance(x,y,a,b,c,d){const dx=c-a,dy=d-b,t=Math.max(0,Math.min(1,((x-a)*dx+(y-b)*dy)/(dx*dx+dy*dy)));return Math.hypot(x-a-t*dx,y-b-t*dy);}
function markSegments(){
  // Same 36-unit SVG arc, approximated by short round-capped segments.
  const cx=84-Math.sqrt(36*36-26*26),start=Math.atan2(-26,84-cx),end=-start-2*Math.PI;
  const points=Array.from({length:65},(_,index)=>{const angle=start+(end-start)*index/64;return[cx+36*Math.cos(angle),64+36*Math.sin(angle)];});
  return [...points.slice(1).map((point,index)=>[...points[index],...point]),[75,64,95,64]]
    .map(line=>({line,left:Math.min(line[0],line[2])-5,right:Math.max(line[0],line[2])+5,top:Math.min(line[1],line[3])-5,bottom:Math.max(line[1],line[3])+5}));
}
export function png(size){
  const data=Buffer.alloc((size*4+1)*size);
  const segments=markSegments(),background=[37,39,42],foreground=[181,185,237];
  for(let y=0;y<size;y++)for(let x=0;x<size;x++){
    let inside=0,ink=0;
    for(const sy of [.25,.75])for(const sx of [.25,.75]){
      const px=(x+sx)/size*128,py=(y+sy)/size*128;
      if(Math.hypot(Math.max(28-px,px-100,0),Math.max(28-py,py-100,0))>28)continue;
      inside++;
      if(segments.some(segment=>px>=segment.left&&px<=segment.right&&py>=segment.top&&py<=segment.bottom&&segmentDistance(px,py,...segment.line)<=5))ink++;
    }
    const i=y*(size*4+1)+1+x*4;
    for(let channel=0;channel<3;channel++)data[i+channel]=inside?Math.round((background[channel]*(inside-ink)+foreground[channel]*ink)/inside):0;
    data[i+3]=Math.round(inside/4*255);
  }
  const header=Buffer.alloc(13);header.writeUInt32BE(size,0);header.writeUInt32BE(size,4);header[8]=8;header[9]=6;
  return Buffer.concat([Buffer.from([137,80,78,71,13,10,26,10]),chunk('IHDR',header),chunk('IDAT',deflateStored(data)),chunk('IEND',Buffer.alloc(0))]);
}
export function ico(){const image=png(256),header=Buffer.alloc(22);header.writeUInt16LE(1,2);header.writeUInt16LE(1,4);header.writeUInt16LE(1,10);header.writeUInt16LE(32,12);header.writeUInt32LE(image.length,14);header.writeUInt32LE(22,18);return Buffer.concat([header,image]);}
export function icns(){const image=png(1024),header=Buffer.alloc(16);header.write('icns',0);header.writeUInt32BE(image.length+16,4);header.write('ic10',8);header.writeUInt32BE(image.length+8,12);return Buffer.concat([header,image]);}
export function patchAccounts(source){
  const replacements=[
    ['private readonly defaultAccountConfig: IDefaultAccountConfig;', 'private readonly defaultAccountConfig: IDefaultAccountConfig | undefined;'],
    ['this.defaultAccountConfig = toDefaultAccountConfig(productService.defaultChatAgent);', 'this.defaultAccountConfig = productService.defaultChatAgent ? toDefaultAccountConfig(productService.defaultChatAgent) : undefined;\n\t\tif (!this.defaultAccountConfig) { this.initBarrier.open(); }'],
    ['...this.defaultAccountConfig.authenticationProvider.default,', "...(this.defaultAccountConfig?.authenticationProvider.default ?? { id: '', name: 'Clin' }),"],
    ["\t\tconst defaultAccountProvider = this._register(instantiationService.createInstance(DefaultAccountProvider, toDefaultAccountConfig(productService.defaultChatAgent)));", "\t\tif (!productService.defaultChatAgent) { return; }\n\t\tconst defaultAccountProvider = this._register(instantiationService.createInstance(DefaultAccountProvider, toDefaultAccountConfig(productService.defaultChatAgent)));"]
  ];
  for(const [before,after] of replacements){if(!source.includes(before))throw new Error('Upstream default account service changed; review the vendor-account exclusion patch');source=source.replace(before,after);}
  return source;
}
export function patchChatDefaults(source){
  for(const [marker,before,after] of [['[ChatAIDisabledSettingId]: {','default: false','default: true'],['[ChatConfiguration.CopilotCliCommandEnabled]: {','default: true','default: false']]){
    const start=source.indexOf(marker),end=source.indexOf('\n\t\t},',start);
    if(start<0||end<0||!source.slice(start,end).includes(before))throw new Error('Upstream chat defaults changed; review the built-in vendor feature defaults');
    const block=source.slice(start,end).replace(before,after);source=source.slice(0,start)+block+source.slice(end);
  }
  return source;
}
