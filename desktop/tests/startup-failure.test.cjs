'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const vm = require('node:vm');
const {createRequire} = require('node:module');

test('packaged startup failure uses an asynchronous close dialog and quits after it settles', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'clin-startup-failure-'));
  const file = path.resolve(__dirname, '../main.cjs'), localRequire = createRequire(file);
  let options, closeDialog, quits = 0;
  const electron = {
    app: {getPath: () => root, setPath() {}, setName() {}, whenReady: () => new Promise(() => {}), on() {},
      isPackaged: true, quit() {quits++;}},
    dialog: {showMessageBox(value) {options = value; return new Promise(resolve => {closeDialog = resolve;});}},
  };
  const sandbox = {require: name => name === 'electron' ? electron : localRequire(name),
    module: {exports: {}}, __dirname: path.dirname(file), process, fetch, AbortSignal, setTimeout, clearTimeout};
  try {
    vm.runInNewContext(fs.readFileSync(file, 'utf8'), sandbox, {filename: file});
    const pending = sandbox.module.exports.showStartupFailure(new Error('fixture engine exit'));
    assert.equal(quits, 0);
    assert.equal(options.type, 'error');
    assert.match(options.detail, /project files are retained/);
    assert.doesNotMatch(options.detail, /install the Python project/);
    assert.equal(options.buttons[0], 'Close');
    closeDialog({response: 0}); await pending;
    assert.equal(quits, 1);
    const log = path.join(root, 'logs/startup.log');
    assert.match(fs.readFileSync(log, 'utf8'), /startup_failed: fixture engine exit/);
    if (process.platform !== 'win32') assert.equal(fs.statSync(log).mode & 0o777, 0o600);
  } finally {fs.rmSync(root, {recursive: true, force: true});}
});

test('cancelling the initial project picker exits even before a sidecar exists', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'clin-startup-cancel-'));
  const file = path.resolve(__dirname, '../main.cjs'), localRequire = createRequire(file);
  const events = new Map();
  let finished;
  const exited = new Promise(resolve => {finished = resolve;});
  let quits = 0;
  const electron = {
    app: {getPath: () => root, setPath() {}, setName() {}, whenReady: () => Promise.resolve(),
      on(name, callback) {events.set(name, callback);}, quit() {
        quits++; let prevented = false;
        events.get('before-quit')?.({preventDefault() {prevented = true;}});
        if (!prevented) finished();
      }},
    dialog: {showOpenDialog: async () => ({canceled: true})},
  };
  const sandbox = {require: name => name === 'electron' ? electron : localRequire(name), module: {exports: {}},
    __dirname: path.dirname(file), process: {...process, env: {}}, fetch, AbortSignal, setTimeout, clearTimeout};
  try {
    vm.runInNewContext(fs.readFileSync(file, 'utf8'), sandbox, {filename: file});
    await Promise.race([exited, new Promise((_, reject) => setTimeout(() => reject(new Error('Cancelled picker left Clin running')), 200))]);
    assert.equal(quits, 2);
  } finally {fs.rmSync(root, {recursive: true, force: true});}
});
