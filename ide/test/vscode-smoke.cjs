const assert=require('node:assert/strict');
const vscode=require('vscode');
exports.run=async()=>{
  assert.equal(vscode.workspace.isTrusted,true);
  const extension=vscode.extensions.getExtension('local-coder.local-coder');
  assert.ok(extension,'Local Coder is installed in the actual fork');
  await extension.activate();
  assert.equal(extension.isActive,true);
  assert.equal(require('node:fs').realpathSync(extension.extensionPath),require('node:fs').realpathSync(process.env.LOCAL_CODER_SMOKE_EXTENSION_PATH));
  const commands=await vscode.commands.getCommands(true);
  for(const name of ['run','plan','review','security','bugs','configureProvider','cancel','refresh','diff','languageContext','snapshotDiff','memoryInspector'])assert.ok(commands.includes(`localCoder.${name}`),`Missing ${name}`);
  await vscode.commands.executeCommand('localCoder.refresh');
};
