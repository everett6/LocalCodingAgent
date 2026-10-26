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
  const requests = [], handlers = new Map(), startupErrors = [];
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
    constructor() {
      window = this;
      this.webContents = {mainFrame: {url: ''}, setWindowOpenHandler() {}, on() {}, session: {setPermissionRequestHandler() {}, on() {}}};
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
    app: {whenReady: () => Promise.resolve(), on() {}, getPath: () => workspace, isPackaged: false, quit() {}},
    BrowserWindow: FakeWindow,
    dialog: {showErrorBox: (_title, message) => { startupErrors.push(message); ready(); }},
    ipcMain: {handle: (name, handler) => handlers.set(name, handler)},
    Menu: {setApplicationMenu() {}, buildFromTemplate: value => value},
  };
  const sandbox = {
    require: name => name === 'electron' ? electron : name === './sidecar.cjs' ? {Sidecar: FakeSidecar, isAllowedApiRequest} : localRequire(name),
    module: {exports: {}}, __dirname: path.dirname(file),
    process: {env: {LOCAL_CODER_DESKTOP_PROJECT: workspace}, platform: process.platform, execPath: process.execPath},
    fetch, AbortSignal, setTimeout, clearTimeout,
  };
  try {
    vm.runInNewContext(fs.readFileSync(file, 'utf8'), sandbox, {filename: file});
    await loaded;
    assert.deepEqual(startupErrors, []);
    const request = handlers.get('engine:request');
    assert.equal(typeof request, 'function');
    const event = {sender: window.webContents, senderFrame: window.webContents.mainFrame};
    assert.equal((await request(event, '/providers/models?source=openrouter', 'GET')).status, 200);
    assert.equal((await request(event, '/inference/discovery/select', 'POST', {id: 'model-fixture'})).status, 200);
    assert.equal((await request(event, '/engagements/defaults', 'POST', {settings: {active_http: false}})).status, 200);
    assert.equal(requests.length, 3);
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
