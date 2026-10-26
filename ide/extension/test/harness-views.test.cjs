'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {registerHarnessViews} = require('../harness-views.cjs');

function fixture(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'harness-views-'));
  const external = fs.mkdtempSync(path.join(os.tmpdir(), 'harness-outside-'));
  fs.writeFileSync(path.join(root, 'sample.py'), 'def sample(): pass\n');
  fs.writeFileSync(path.join(external, 'outside.py'), 'secret\n');
  fs.symlinkSync(external, path.join(root, 'escape'), 'dir');
  t.after(() => { fs.rmSync(root, {recursive: true, force: true}); fs.rmSync(external, {recursive: true, force: true}); });
  const commands = new Map(), calls = [], errors = [], shown = [], requests = [], providers = new Map(), opened = [];
  const uri = (scheme, file) => ({scheme, fsPath: file, path: file, toString: () => `${scheme}:${file}`});
  const folder = {uri: uri('file', root), name: 'test'};
  const subscriptions = [];
  const ranges = {start: {line: 0, character: 4}, end: {line: 0, character: 6}};
  const ownLocation = {uri: uri('file', path.join(root, 'sample.py')), range: ranges};
  const outsideLocation = {uri: uri('file', path.join(external, 'outside.py')), range: ranges};
  const values = {
    'vscode.executeDocumentSymbolProvider': [{name: 'sample', kind: 12, range: ranges, children: []}],
    'vscode.executeReferenceProvider': [ownLocation, outsideLocation, {uri: uri('file', path.join(root, 'escape', 'outside.py')), range: ranges}, {uri: uri('https', 'example.test'), range: ranges}],
    'vscode.executeDefinitionProvider': [{targetUri: ownLocation.uri, targetSelectionRange: ranges}]
  };
  let cancel;
  const token = {isCancellationRequested: false, onCancellationRequested(callback) { cancel = callback; return {dispose() { cancel = undefined; }}; }};
  const backend = {
    '/snapshots': {snapshots: [{id: 'a'.repeat(32), created_at: 12, status: 'completed', paths: ['sample.py']}]},
    [`/snapshots/${'a'.repeat(32)}/diff`]: {files: [{path: 'sample.py', before: 'before text', after: 'after text'}], diff: 'backend diff'},
    '/memory?limit=50': {memories: [{id: 'm1', content: 'Remember source context', source_ref: 'sample.py:1', source_path: 'sample.py', freshness: 'source_changed', metadata: {start_line: 1, api_key: 'hidden'}, provenance: {path: 'sample.py', line: 1}, private_key: 'never display unknown fields'}]}
  };
  let serviceCalls = 0;
  const vscode = {
    Uri: {from: value => uri(value.scheme, value.path)},
    ProgressLocation: {Notification: 15},
    workspace: {isTrusted: true, getWorkspaceFolder: () => folder,
      registerTextDocumentContentProvider(scheme, provider) { providers.set(scheme, provider); return {dispose() { providers.delete(scheme); }}; },
      onDidCloseTextDocument() { return {dispose() {}}; },
      async openTextDocument(value) { opened.push(value); return {uri: value, getText: () => providers.get(value.scheme).provideTextDocumentContent(value)}; }
    },
    window: {activeTextEditor: {document: {uri: ownLocation.uri}, selection: {active: {line: 0, character: 4}}},
      async showQuickPick(items) { return items[0]; },
      async showTextDocument(document) { shown.push(document); },
      async showErrorMessage(message) { errors.push(message); },
      async showInformationMessage() {},
      async withProgress(_options, task) { return task({}, token); }
    },
    commands: {
      registerCommand(name, handler) { commands.set(name, handler); return {dispose() { commands.delete(name); }}; },
      async executeCommand(name, ...args) { calls.push({name, args}); return typeof values[name] === 'function' ? values[name](...args) : values[name]; }
    },
    languages: {async setTextDocumentLanguage() {}}
  };
  registerHarnessViews(vscode, {subscriptions}, async () => { serviceCalls++; return {folder, engine: {async request(route, method = 'GET', body) { requests.push({route, method, body}); if (backend[route] instanceof Error) throw backend[route]; return backend[route]; }}}; });
  t.after(() => subscriptions.forEach(value => value.dispose()));
  return {root, external, vscode, commands, values, calls, errors, shown, requests, backend, opened, providers, token,
    cancel() { token.isCancellationRequested = true; cancel?.(); },
    serviceCalls() { return serviceCalls; }};
}

test('native language context calls exactly three fixed providers, filters external paths, and never calls backend', async t => {
  const state = fixture(t);
  await state.commands.get('localCoder.languageContext')();
  assert.deepEqual(state.calls.map(call => call.name), ['vscode.executeDocumentSymbolProvider', 'vscode.executeReferenceProvider', 'vscode.executeDefinitionProvider']);
  assert.equal(state.requests.length, 0);
  assert.equal(state.serviceCalls(), 0);
  const result = JSON.parse(state.shown[0].getText());
  assert.equal(result.symbols[0].name, 'sample');
  assert.deepEqual(result.references, [{path: 'sample.py', line: 1, character: 4}]);
  assert.deepEqual(result.definitions, result.references);
  assert.equal(result.position.encoding, 'utf-16');
  assert.equal(state.errors.length, 0);
});

test('snapshot diff uses immutable virtual before/after documents and fixed native diff command', async t => {
  const state = fixture(t);
  await state.commands.get('localCoder.snapshotDiff')();
  const diff = state.calls.find(call => call.name === 'vscode.diff');
  assert.ok(diff);
  const [before, after] = diff.args;
  assert.equal(before.scheme, 'local-coder-snapshot');
  assert.equal(after.scheme, 'local-coder-snapshot');
  assert.notEqual(before.toString(), after.toString());
  const provider = state.providers.get('local-coder-snapshot');
  assert.equal(provider.provideTextDocumentContent(before), 'before text');
  assert.equal(provider.provideTextDocumentContent(after), 'after text');
  state.backend[`/snapshots/${'a'.repeat(32)}/diff`].files[0].before = 'mutated backend';
  assert.equal(provider.provideTextDocumentContent(before), 'before text');
  assert.ok(state.requests.every(request => request.method === 'GET'));
});

test('snapshot paths outside root and symlink escapes are rejected before opening native diffs', async t => {
  const state = fixture(t);
  for (const file of ['../outside.py', path.join(state.external, 'outside.py'), 'escape/outside.py']) {
    state.backend[`/snapshots/${'a'.repeat(32)}/diff`].files[0].path = file;
    await state.commands.get('localCoder.snapshotDiff')();
  }
  assert.equal(state.calls.length, 0);
  assert.equal(state.errors.length, 3);
});

test('memory inspector preserves provenance and excludes unknown fields in a read-only document', async t => {
  const state = fixture(t);
  await state.commands.get('localCoder.memoryInspector')();
  const result = JSON.parse(state.shown[0].getText());
  assert.deepEqual(result.memories[0].provenance, {path: 'sample.py', line: 1});
  assert.equal(result.memories[0].private_key, undefined);
  assert.equal(result.memories[0].source_ref, 'sample.py:1');
  assert.equal(result.memories[0].freshness, 'source_changed');
  assert.equal(result.memories[0].metadata.api_key, undefined);
  assert.ok(result.note.includes('untrusted'));
  assert.deepEqual(state.requests.map(request => request.route), ['/memory?limit=50']);
});

test('untrusted workspace prevents all providers and backend calls', async t => {
  const state = fixture(t);
  state.vscode.workspace.isTrusted = false;
  for (const command of state.commands.values()) await command();
  assert.equal(state.calls.length, 0);
  assert.equal(state.serviceCalls(), 0);
  assert.equal(state.errors.length, 3);
});

test('active documents outside root are rejected before provider execution', async t => {
  const state = fixture(t);
  state.vscode.window.activeTextEditor.document.uri.fsPath = path.join(state.external, 'outside.py');
  await state.commands.get('localCoder.languageContext')();
  assert.equal(state.calls.length, 0);
  assert.equal(state.errors.length, 1);
});

test('cancellation stops waiting for native providers and never opens a late result', async t => {
  const state = fixture(t);
  state.values['vscode.executeDocumentSymbolProvider'] = () => new Promise(() => {});
  const running = state.commands.get('localCoder.languageContext')();
  state.cancel();
  await running;
  assert.equal(state.calls.length, 3);
  assert.equal(state.shown.length, 0);
  assert.match(state.errors[0], /cancelled/);
});

test('provider deadline faults visibly after five seconds', async t => {
  const state = fixture(t);
  t.mock.timers.enable({apis: ['setTimeout']});
  state.values['vscode.executeDocumentSymbolProvider'] = () => new Promise(() => {});
  const running = state.commands.get('localCoder.languageContext')();
  t.mock.timers.tick(5000);
  await running;
  assert.equal(state.shown.length, 0);
  assert.match(state.errors[0], /timed out after 5 seconds/);
});

test('backend and native provider faults are shown to the user', async t => {
  const state = fixture(t);
  state.backend['/snapshots'] = new Error('Backend unavailable');
  await state.commands.get('localCoder.snapshotDiff')();
  state.values['vscode.executeReferenceProvider'] = async () => { throw new Error('Provider unavailable'); };
  await state.commands.get('localCoder.languageContext')();
  assert.match(state.errors[0], /Backend unavailable/);
  assert.match(state.errors[1], /Provider unavailable/);
});

test('large memory collections remain bounded valid JSON', async t => {
  const state = fixture(t);
  state.backend['/memory?limit=50'].memories = Array.from({length: 100}, (_, index) => ({id: index, content: 'a'.repeat(10000), text: 'b'.repeat(10000), provenance: {detail: 'c'.repeat(10000)}}));
  await state.commands.get('localCoder.memoryInspector')();
  const text = state.shown[0].getText();
  assert.ok(text.length <= 200000);
  const value = JSON.parse(text);
  assert.ok(value.memories.length <= 50);
  assert.equal(value.truncated, true);
});
