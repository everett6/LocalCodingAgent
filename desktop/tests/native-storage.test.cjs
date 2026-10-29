'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const storage = require('../native-storage.cjs');

async function fixture(t) {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'clin-storage-'));
  t.after(() => fs.rm(root, { recursive: true, force: true }));
  const userData = path.join(root, 'profile'), project = path.join(root, 'project');
  await fs.mkdir(userData); await fs.mkdir(project);
  return { root, userData, project };
}

test('Markdown export atomically replaces only the selected md file privately', async t => {
  const { root } = await fixture(t), target = path.join(root, 'report.md');
  await fs.writeFile(target, 'old');
  await storage.writeReport(target, '# Clin report\nMeasured result.');
  assert.equal(await fs.readFile(target, 'utf8'), '# Clin report\nMeasured result.');
  assert.equal((await fs.stat(target)).mode & 0o777, 0o600);
  await assert.rejects(storage.writeReport(path.join(root, 'report.txt'), 'text'), /Markdown/);
  assert.ok(!(await fs.readdir(root)).some(name => name.endsWith('.tmp')));
});

test('report input rejects invalid names, nontext, and UTF-8 byte overflow', () => {
  assert.equal(storage.validateReport('text', 'clin-report.md'), 'clin-report.md');
  for (const name of ['../secret.md', 'file.txt', '/tmp/file.md', 'bad\nname.md']) assert.throws(() => storage.validateReport('text', name));
  assert.throws(() => storage.validateReport({ text: 'report' }, 'report.md'));
  assert.throws(() => storage.validateReport('🤖'.repeat(2097153), 'report.md'), /8 MiB/);
});

test('task pins persist IDs privately and isolate canonical workspaces', async t => {
  const { root, userData, project } = await fixture(t), second = path.join(root, 'second');
  await fs.mkdir(second);
  assert.deepEqual(await storage.loadTaskPins(userData, project), []);
  await storage.saveTaskPins(userData, project, ['session-one', 'a'.repeat(32)]);
  assert.deepEqual(await storage.loadTaskPins(userData, project), ['session-one', 'a'.repeat(32)]);
  assert.deepEqual(await storage.loadTaskPins(userData, second), []);
  const directory = path.join(userData, 'workspace-pins'), names = await fs.readdir(directory);
  assert.equal(names.length, 1); assert.match(names[0], /^[a-f0-9]{64}\.json$/);
  assert.equal((await fs.stat(directory)).mode & 0o777, 0o700);
  assert.equal((await fs.stat(path.join(directory, names[0]))).mode & 0o777, 0o600);
  assert.deepEqual(JSON.parse(await fs.readFile(path.join(directory, names[0]), 'utf8')), ['session-one', 'a'.repeat(32)]);
});

test('pin validation rejects oversized or arbitrary content before writing', async t => {
  const { userData, project } = await fixture(t);
  for (const ids of [{}, [''], ['task with prompt'], ['../escape'], ['x'.repeat(129)], Array(201).fill('task')]) {
    await assert.rejects(storage.saveTaskPins(userData, project, ids));
  }
  assert.deepEqual(await fs.readdir(userData), []);
});

test('pins reject symlink directories and files without modifying their targets', async t => {
  const { root, userData, project } = await fixture(t), outside = path.join(root, 'outside');
  await fs.mkdir(outside);
  await fs.symlink(outside, path.join(userData, 'workspace-pins'));
  await assert.rejects(storage.saveTaskPins(userData, project, ['task']), /directory/);
  assert.deepEqual(await fs.readdir(outside), []);
  await fs.unlink(path.join(userData, 'workspace-pins'));
  await storage.saveTaskPins(userData, project, ['original']);
  const directory = path.join(userData, 'workspace-pins'), file = path.join(directory, (await fs.readdir(directory))[0]);
  const other = path.join(outside, 'unchanged'); await fs.writeFile(other, 'preserve');
  await fs.unlink(file); await fs.symlink(other, file);
  await assert.rejects(storage.loadTaskPins(userData, project), /regular/);
  await assert.rejects(storage.saveTaskPins(userData, project, ['replacement']), /regular/);
  assert.equal(await fs.readFile(other, 'utf8'), 'preserve');
});

test('failed atomic export retains the old file and cleans staging files', async t => {
  const { root } = await fixture(t), file = path.join(root, 'report.md');
  const vm = require('node:vm');
  await fs.writeFile(file, 'original');
  const adapter = { ...fs, rename: async () => { throw new Error('simulated publish failure'); } };
  const context = { module: { exports: {} }, Buffer,
    require: name => name === 'node:fs/promises' ? adapter : require(name) };
  vm.runInNewContext(await fs.readFile(path.resolve(__dirname, '../native-storage.cjs'), 'utf8'), context);
  await assert.rejects(context.module.exports.writeReport(file, 'replacement'), /publish failure/);
  assert.equal(await fs.readFile(file, 'utf8'), 'original');
  assert.ok(!(await fs.readdir(root)).some(name => name.endsWith('.tmp')));
});

test('pins use canonical workspace identity and reject corrupted or oversized state', async t => {
  const { root, userData, project } = await fixture(t), alias = path.join(root, 'alias');
  await fs.symlink(project, alias);
  await storage.saveTaskPins(userData, project, ['task']);
  assert.deepEqual(await storage.loadTaskPins(userData, alias), ['task']);
  const directory = path.join(userData, 'workspace-pins'), file = path.join(directory, (await fs.readdir(directory))[0]);
  await fs.writeFile(file, '{"ids":[]}');
  await assert.rejects(storage.loadTaskPins(userData, project), /task IDs/);
  await fs.writeFile(file, Buffer.alloc(32769));
  await assert.rejects(storage.loadTaskPins(userData, project), /bounded/);
});

test('preload exposes only report text and implicit-project pin operations', async () => {
  const vm = require('node:vm'), calls = []; let exposed;
  const electron = { contextBridge: { exposeInMainWorld(name, value) { assert.equal(name, 'localCoder'); exposed = value; } },
    ipcRenderer: { invoke(...args) { calls.push(args); return Promise.resolve(); } } };
  vm.runInNewContext(await fs.readFile(path.resolve(__dirname, '../preload.cjs'), 'utf8'), {
    require: name => { assert.equal(name, 'electron'); return electron; },
  });
  await exposed.saveReport('report', 'clin-report.md'); await exposed.loadTaskPins(); await exposed.saveTaskPins(['task']);
  assert.deepEqual(calls, [['report:save', 'report', 'clin-report.md'], ['task-pins:load'], ['task-pins:save', ['task']]]);
});
