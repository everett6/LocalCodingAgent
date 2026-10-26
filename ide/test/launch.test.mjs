import test from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';
import {findLaunch,launchArguments} from '../scripts/launch.mjs';
test('requires an actual built executable and never substitutes a hosted editor',()=>{
  assert.throws(()=>findLaunch({exists:()=>false}),/not built/);
  const executable=path.resolve('/tmp/Local Coder IDE');
  assert.equal(findLaunch({executable,exists:()=>true}).executable,executable);
  assert.throws(()=>findLaunch({executable:'relative-file',exists:()=>true}),/absolute/);
});
test('shared engine launches isolate its environment without exposing raw token in arguments',()=>{
  const token='private-provider-independent-token';
  const args=launchArguments('/project with spaces',{LOCAL_CODER_ENGINE_URL:'http://127.0.0.1:1234',LOCAL_CODER_REMOTE_TOKEN:token});
  assert.ok(args.includes('--user-data-dir'));assert.equal(args.at(-1),'/project with spaces');assert.ok(!args.join(' ').includes(token));
  assert.ok(!launchArguments('/tmp',{}).includes('--user-data-dir'));
  assert.throws(()=>launchArguments('/tmp',{LOCAL_CODER_ENGINE_URL:'http://evil.invalid:1234',LOCAL_CODER_REMOTE_TOKEN:token}),/loopback/);
});
