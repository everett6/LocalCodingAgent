import test from 'node:test';
import assert from 'node:assert/strict';
import {productPatch,packagePatch,patchBuild,png,ico,icns} from '../scripts/fork.mjs';
test('fork has distinct application identities and no hosted vendor services',()=>{
  const value=productPatch({nameShort:'Old',extensionsGallery:{serviceUrl:'https://marketplace.invalid'},defaultChatAgent:{id:'vendor'},enableTelemetry:true,licenseFileName:'LICENSE.txt',builtInExtensions:[{}],webviewContentExternalBaseUrlTemplate:'https://vendor.invalid',updateUrl:'https://update.invalid'});
  assert.equal(value.nameShort,'Local Coder IDE');assert.equal(value.applicationName,'local-coder-ide');assert.equal(value.enableTelemetry,false);
  assert.equal(value.licenseFileName,'LICENSE.txt');assert.deepEqual(value.builtInExtensions,[]);
  for(const key of ['extensionsGallery','defaultChatAgent','updateUrl','webviewContentExternalBaseUrlTemplate'])assert.equal(key in value,false);
  assert.deepEqual(productPatch(value),value);
});
test('fork excludes optional Copilot extension compilation and mandatory post-package shim',()=>{
  const source='import { clean, compileCopilotExtensionBuildTask } from "./ext";\n\t\t\tcompileCopilotExtensionBuildTask,\n\t\t\tprepareCopilotRipgrepShimTask(platform, arch, destinationFolderName)\n';
  const result=patchBuild(source);assert.ok(!result.includes('compileCopilotExtensionBuildTask'));assert.ok(!result.includes('prepareCopilotRipgrepShimTask'));
  assert.throws(()=>patchBuild('upstream changed'),/Upstream build changed/);
  assert.equal(packagePatch({scripts:{compile:'old',watch:'old'}}).scripts.compile,'npm run compile-client');
});
test('custom application icons have native PNG ICO and ICNS containers',()=>{
  assert.equal(png(32).subarray(1,4).toString(),'PNG');
  const win=ico();assert.equal(win.readUInt16LE(2),1);assert.equal(win.readUInt32LE(18),22);
  const mac=icns();assert.equal(mac.subarray(0,4).toString(),'icns');assert.equal(mac.readUInt32BE(4),mac.length);assert.equal(mac.subarray(8,12).toString(),'ic10');
});
test('vendor account service can initialize without hosted chat product metadata',async()=>{
  const {patchAccounts}=await import('../scripts/fork.mjs');
  const input=['private readonly defaultAccountConfig: IDefaultAccountConfig;','this.defaultAccountConfig = toDefaultAccountConfig(productService.defaultChatAgent);','...this.defaultAccountConfig.authenticationProvider.default,','\t\tconst defaultAccountProvider = this._register(instantiationService.createInstance(DefaultAccountProvider, toDefaultAccountConfig(productService.defaultChatAgent)));'].join('\n');
  const output=patchAccounts(input);
  assert.ok(output.includes('this.initBarrier.open()'));assert.ok(output.includes('if (!productService.defaultChatAgent) { return; }'));assert.ok(output.includes('this.defaultAccountConfig?.authenticationProvider.default'));
  assert.throws(()=>patchAccounts('unknown source'),/Upstream default account service changed/);
});
test('built-in vendor chat and terminal CLI are disabled before extension activation',async()=>{
  const {patchChatDefaults}=await import('../scripts/fork.mjs');
  const source='[ChatAIDisabledSettingId]: {\n type: boolean, default: false\n\t\t},\n[ChatConfiguration.CopilotCliCommandEnabled]: {\n type: boolean, default: true\n\t\t},';
  const result=patchChatDefaults(source);assert.match(result,/DisabledSettingId\]: \{\s*type: boolean, default: true/);assert.match(result,/CopilotCliCommandEnabled\]: \{\s*type: boolean, default: false/);
  assert.throws(()=>patchChatDefaults('unknown source'),/Upstream chat defaults changed/);
});
