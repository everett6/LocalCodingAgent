'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const os = require('node:os');
const path = require('node:path');
const vm = require('node:vm');
const {createRequire} = require('node:module');
const {isAllowedApiRequest} = require('../sidecar.cjs');

test('real Electron main IPC forwards permitted provider requests with auth and blocks untrusted frames', async () => {
  const workspace = fs.mkdtempSync(path.join(os.tmpdir(), 'local-coder-main-api-'));
  const requests = [], handlers = new Map(), startupErrors = [], saveDialogs = [], sessionEvents = new Map();
  let saveResult = { canceled: true };
  let pinGate, firstPinStarted;
  const realStorage = require('../native-storage.cjs');
  const storage = { ...realStorage, saveTaskPins: async (...args) => {
    if (args[2][0] === 'queued-first') { firstPinStarted(); await pinGate; }
    if (args[2][0] === 'failed-write') throw new Error('simulated pin failure');
    return realStorage.saveTaskPins(...args);
  } };
  const server = http.createServer(async (request, response) => {
    let raw = '';
    for await (const chunk of request) raw += chunk;
    requests.push({url: request.url, method: request.method, auth: request.headers.authorization, body: raw ? JSON.parse(raw) : undefined});
    response.setHeader('Content-Type', 'application/json'); response.end('{"ok":true}');
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const origin = `http://127.0.0.1:${server.address().port}`;
  let window, ready;
  const loaded = new Promise(resolve => { ready = resolve; });
  class FakeWindow {
    constructor(options) {
      window = this;
      this.options = options;
      this.webContents = {mainFrame: {url: ''}, setWindowOpenHandler() {}, on() {}, session: {setPermissionRequestHandler() {}, on(name, handler) { sessionEvents.set(name, handler); }}};
    }
    on() {} once() {} show() {}
    async loadURL(url) { this.webContents.mainFrame.url = url + '/'; ready(); }
  }
  class FakeSidecar {
    constructor(options) { this.token = options.token; this.child = null; }
    async start() { return origin; }
    async stop() {}
  }
  const file = path.resolve(__dirname, '../main.cjs'), localRequire = createRequire(file);
  const electron = {
    app: {whenReady: () => Promise.resolve(), on() {}, getPath: () => workspace, setPath() {}, setName() {}, isPackaged: false, quit() {}},
    BrowserWindow: FakeWindow,
    dialog: {showErrorBox: (_title, message) => { startupErrors.push(message); ready(); },
      showSaveDialog: async (_window, options) => { saveDialogs.push(options); return saveResult; }},
    ipcMain: {handle: (name, handler) => handlers.set(name, handler)},
    Menu: {setApplicationMenu() {}, buildFromTemplate: value => value},
  };
  const sandbox = {
    require: name => name === 'electron' ? electron : name === './sidecar.cjs' ? {Sidecar: FakeSidecar, isAllowedApiRequest} : name === './native-storage.cjs' ? storage : localRequire(name),
    module: {exports: {}}, __dirname: path.dirname(file),
    process: {env: {LOCAL_CODER_DESKTOP_PROJECT: workspace}, platform: process.platform, execPath: process.execPath},
    fetch, AbortSignal, setTimeout, clearTimeout,
  };
  try {
    vm.runInNewContext(fs.readFileSync(file, 'utf8'), sandbox, {filename: file});
    await loaded;
    assert.deepEqual(startupErrors, []);
    assert.equal(window.options.icon, path.resolve(__dirname, '../build/icon.png'));
    let blockedDownload = false;
    sessionEvents.get('will-download')({ preventDefault() { blockedDownload = true; } });
    assert.ok(blockedDownload);
    const request = handlers.get('engine:request');
    assert.equal(typeof request, 'function');
    const event = {sender: window.webContents, senderFrame: window.webContents.mainFrame};
    assert.equal((await request(event, '/providers/models?source=openrouter', 'GET')).status, 200);
    assert.equal((await request(event, '/inference/discovery/select', 'POST', {id: 'model-fixture'})).status, 200);
    assert.equal((await request(event, '/engagements/defaults', 'POST', {settings: {active_http: false}})).status, 200);
    assert.equal(requests.length, 3);
    const save = handlers.get('report:save'), loadPins = handlers.get('task-pins:load'), savePins = handlers.get('task-pins:save');
    assert.equal(typeof save, 'function'); assert.equal(typeof loadPins, 'function'); assert.equal(typeof savePins, 'function');
    const untrusted = { ...event, senderFrame: { url: origin + '/' } };
    await assert.rejects(save(untrusted, 'private report', 'report.md'), /Untrusted/);
    await assert.rejects(loadPins(untrusted), /Untrusted/);
    await assert.rejects(savePins(untrusted, ['session-one']), /Untrusted/);
    await assert.rejects(save(event, {}, 'report.md'), /Markdown/);
    await assert.rejects(save(event, 'text', '../report.md'), /Markdown/);
    await assert.rejects(save(event, 'x'.repeat(8 * 1024 * 1024 + 1), 'report.md'), /8 MiB/);
    assert.equal(saveDialogs.length, 0);
    assert.equal((await save(event, 'cancelled text', 'report.md')).saved, false);
    assert.equal(saveDialogs[0].filters[0].extensions[0], 'md');
    const exported = path.join(workspace, 'chosen.md');
    saveResult = { canceled: false, filePath: exported };
    assert.equal((await save(event, '# Clin report', 'report.md')).saved, true);
    assert.equal(fs.readFileSync(exported, 'utf8'), '# Clin report');
    assert.equal(fs.statSync(exported).mode & 0o777, 0o600);
    assert.equal((await loadPins(event)).length, 0);
    await savePins(event, ['session-one']);
    assert.equal((await loadPins(event))[0], 'session-one');
    await assert.rejects(savePins(event, ['private prompt text']), /task IDs/);
    let release;
    pinGate = new Promise(resolve => { release = resolve; });
    const started = new Promise(resolve => { firstPinStarted = resolve; });
    const first = savePins(event, ['queued-first']);
    await started;
    const second = savePins(event, ['queued-last']);
    let didLoad = false;
    const afterQueue = loadPins(event).then(value => { didLoad = true; return value; });
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(didLoad, false);
    release();
    await Promise.all([first, second]);
    assert.equal((await afterQueue)[0], 'queued-last');
    await assert.rejects(savePins(event, ['failed-write']), /simulated pin failure/);
    await savePins(event, ['recovered']);
    assert.equal((await loadPins(event))[0], 'recovered');
    assert.ok(requests.every(item => /^Bearer [a-f0-9]{64}$/.test(item.auth)));
    assert.deepEqual(requests[1].body, {id: 'model-fixture'});
    assert.throws(() => request({...event, senderFrame: {url: origin + '/'}}, '/providers/models', 'GET'), /Untrusted/);
    await assert.rejects(request(event, '/providers/profiles/../settings', 'POST', {}), /Unsupported/);
    assert.equal(requests.length, 3);
  } finally {
    await new Promise(resolve => server.close(resolve));
    fs.rmSync(workspace, {recursive: true, force: true});
  }
});
