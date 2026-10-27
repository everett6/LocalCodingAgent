'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { createRequire } = require('node:module');

test('display rename preserves the existing Electron profile before changing app name', () => {
  const file = path.resolve(__dirname, '../main.cjs'), localRequire = createRequire(file);
  let name = 'local-coder-desktop';
  const pinned = new Map(), operations = [];
  const app = {
    getPath: key => pinned.get(key) || `/legacy-config/${name}`,
    setPath: (key, value) => { operations.push(['path', key, value]); pinned.set(key, value); },
    setName: value => { operations.push(['name', value]); name = value; },
    whenReady: () => new Promise(() => {}), on() {},
  };
  vm.runInNewContext(fs.readFileSync(file, 'utf8'), {
    require: id => id === 'electron' ? { app } : localRequire(id),
    __dirname: path.dirname(file), module: { exports: {} },
  });
  assert.equal(name, 'Clin');
  assert.equal(app.getPath('userData'), '/legacy-config/local-coder-desktop');
  assert.deepEqual(operations, [['path', 'userData', '/legacy-config/local-coder-desktop'], ['name', 'Clin']]);
});

test('desktop display branding keeps package, application and launch identities compatible', () => {
  const manifest = require('../package.json');
  assert.equal(manifest.build.productName, 'Clin');
  assert.equal(manifest.name, 'local-coder-desktop');
  assert.equal(manifest.build.appId, 'dev.localcoder.desktop');
  assert.equal(manifest.build.executableName, 'local-coder-desktop');
});

test('native desktop branding packages real icons for every platform and runtime window', () => {
  const manifest = require('../package.json');
  for (const [platform, extension] of [['linux', 'png'], ['win', 'ico'], ['mac', 'icns']]) {
    assert.equal(manifest.build[platform].icon, `build/icon.${extension}`);
    const bytes = fs.readFileSync(path.resolve(__dirname, '..', manifest.build[platform].icon));
    assert.ok(bytes.length > 100);
    if (extension === 'png') assert.equal(bytes.subarray(1, 4).toString(), 'PNG');
    if (extension === 'ico') assert.equal(bytes.readUInt16LE(2), 1);
    if (extension === 'icns') assert.equal(bytes.subarray(0, 4).toString(), 'icns');
  }
  assert.ok(manifest.build.extraResources.some(item => item.from === 'build/icon.png' && item.to === 'icons/icon.png'));
});
