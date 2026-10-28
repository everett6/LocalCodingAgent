const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const os = require('node:os');
const vm = require('node:vm');
const { execFileSync } = require('node:child_process');
const { createHash } = require('node:crypto');
const updater = require('../update.cjs');
const url = name => `https://github.com/everett6/LocalCodingAgent/releases/download/ide-vscode-test/${name}`;

test('fork updater rejects assets outside the owned release channel', () => {
  assert.throws(() => updater.asset({ assets: [{ name: 'manifest', browser_download_url: 'https://evil.invalid/file' }] }, 'manifest'), /missing/);
});

// Root ownership is represented only in this VM filesystem adapter. Production
// code still performs actual lstat/open/fstat; fixture renames use the real disk.
async function fixture(t, { incoming = 'trusted-sandbox', trusted = true, owner = 0, helperMode = 0o4755, failRename = 0 } = {}) {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'local-coder-update-'));
  t.after(() => fs.rm(root, { recursive: true, force: true }));
  const destination = path.join(root, 'ide'), payload = path.join(root, 'payload');
  const tag = 'ide-vscode-test';
  async function create(folder, release, helper) {
    await fs.mkdir(path.join(folder, 'resources/app'), { recursive: true });
    await fs.writeFile(path.join(folder, 'resources/app/product.json'), JSON.stringify({ applicationName: 'local-coder-ide', ...(release ? { localCoderRelease: release } : {}) }));
    await fs.writeFile(path.join(folder, 'resources/app/package.json'), JSON.stringify({ version: '1.143.1' }));
    await fs.writeFile(path.join(folder, 'local-coder-ide'), 'executable', { mode: 0o755 });
    await fs.writeFile(path.join(folder, 'chrome-sandbox'), helper, { mode: 0o755 });
  }
  await create(destination, null, 'trusted-sandbox'); await create(payload, tag, incoming);
  const originalInode = (await fs.stat(path.join(destination, 'chrome-sandbox'))).ino;
  const archive = path.join(root, 'release.tar.gz');
  execFileSync('tar', ['-czf', archive, '-C', payload, '.']);
  const bytes = await fs.readFile(archive);
  const manifest = { tag, version: '1.143.1', platform: 'linux', arch: 'x64', asset: 'local-coder-ide-linux-x64.tar.gz', sha256: createHash('sha256').update(bytes).digest('hex') };
  const release = { tag_name: tag, assets: ['ide-linux-x64.json', manifest.asset].map(name => ({ name, browser_download_url: url(name) })) };
  const metadata = details => {
    if (trusted && details.ino === originalInode) { details.uid = owner; details.mode = (details.mode & ~0o7777) | helperMode; }
    return details;
  };
  let renameCount = 0;
  const adapter = {
    ...fs,
    lstat: async file => metadata(await fs.lstat(file)),
    open: async (...args) => {
      const handle = await fs.open(...args);
      return new Proxy(handle, { get(target, key) {
        if (key === 'stat') return async (...values) => metadata(await target.stat(...values));
        const value = target[key]; return typeof value === 'function' ? value.bind(target) : value;
      } });
    },
    rename: async (from, to) => {
      renameCount++;
      if ((Array.isArray(failRename) ? failRename : [failRename]).includes(renameCount)) throw new Error('simulated publish failure');
      return fs.rename(from, to);
    },
  };
  const context = {
    module: { exports: {} }, Buffer, AbortSignal, process: { platform: 'linux', arch: 'x64' },
    fetch: async location => new Response(location.endsWith('.json') ? JSON.stringify(manifest) : bytes, { status: 200 }),
    require: name => name === 'node:fs/promises' ? adapter : require(name),
  };
  vm.runInNewContext(await fs.readFile(path.join(__dirname, '../update.cjs'), 'utf8'), context);
  return { root, destination, originalInode, manifest, release, updater: context.module.exports,
    install: () => context.module.exports.install(release, path.join(destination, 'local-coder-ide')) };
}

const linux = { skip: process.platform !== 'linux' || process.arch !== 'x64' };
test('native update preserves the configured sandbox inode and retains unconfigured backup', linux, async t => {
  const value = await fixture(t);
  assert.equal(await value.install(), true);
  const helper = await fs.stat(path.join(value.destination, 'chrome-sandbox'));
  assert.equal(helper.ino, value.originalInode, 'the original configured helper must be moved into the published app');
  const backup = (await fs.readdir(value.root)).find(name => name.startsWith('ide.previous-'));
  assert.ok(backup);
  assert.notEqual((await fs.stat(path.join(value.root, backup, 'chrome-sandbox'))).ino, value.originalInode);
  assert.equal(JSON.parse(await fs.readFile(path.join(value.destination, 'resources/app/product.json'))).localCoderRelease, 'ide-vscode-test');
  assert.equal(await value.install(), false);
});

test('checksum failure leaves the configured installation intact', linux, async t => {
  const value = await fixture(t);
  value.manifest.sha256 = '0'.repeat(64);
  await assert.rejects(value.install(), /checksum/);
  assert.equal((await fs.stat(path.join(value.destination, 'chrome-sandbox'))).ino, value.originalInode);
});

test('different sandbox bytes stop update before replacing the configured installation', linux, async t => {
  const value = await fixture(t, { incoming: 'different-helper' });
  await assert.rejects(value.install(), /sandbox.*(?:setup|administrator|manual)/i);
  assert.equal((await fs.stat(path.join(value.destination, 'chrome-sandbox'))).ino, value.originalInode);
  assert.equal(JSON.parse(await fs.readFile(path.join(value.destination, 'resources/app/product.json'))).localCoderRelease, undefined);
  assert.ok(!(await fs.readdir(value.root)).some(name => name.startsWith('ide.previous-')));
});

test('unconfigured current sandbox is rejected without replacing the installation', linux, async t => {
  const value = await fixture(t, { trusted: false });
  await assert.rejects(value.install(), /sandbox.*(?:setup|administrator|manual)/i);
  assert.equal((await fs.stat(path.join(value.destination, 'chrome-sandbox'))).ino, value.originalInode);
});

for (const settings of [{ owner: 1000 }, { helperMode: 0o4777 }]) {
  test(`unsafe current sandbox metadata ${JSON.stringify(settings)} stops installation`, linux, async t => {
    const value = await fixture(t, settings);
    await assert.rejects(value.install(), /sandbox.*setup/i);
    assert.equal((await fs.stat(path.join(value.destination, 'chrome-sandbox'))).ino, value.originalInode);
  });
}

test('linked current helper is rejected without changing its target', linux, async t => {
  const value = await fixture(t);
  const target = path.join(value.root, 'linked-helper');
  await fs.rename(path.join(value.destination, 'chrome-sandbox'), target);
  await fs.symlink(target, path.join(value.destination, 'chrome-sandbox'));
  await assert.rejects(value.install(), /sandbox.*setup/i);
  assert.equal((await fs.stat(target)).ino, value.originalInode);
  assert.ok((await fs.lstat(path.join(value.destination, 'chrome-sandbox'))).isSymbolicLink());
});

test('oversized staged sandbox is rejected before installation', linux, async t => {
  const value = await fixture(t, { incoming: Buffer.alloc(16 * 1024 * 1024 + 1) });
  await assert.rejects(value.install(), /sandbox.*setup/i);
  assert.equal((await fs.stat(path.join(value.destination, 'chrome-sandbox'))).ino, value.originalInode);
});

for (const failRename of [2, 3, 4, 5]) {
  test(`sandbox transfer or publish failure ${failRename} restores original configured helper`, linux, async t => {
    const value = await fixture(t, { failRename });
    await assert.rejects(value.install(), /simulated publish failure/);
    assert.equal((await fs.stat(path.join(value.destination, 'chrome-sandbox'))).ino, value.originalInode);
    assert.equal(JSON.parse(await fs.readFile(path.join(value.destination, 'resources/app/product.json'))).localCoderRelease, undefined);
    assert.ok(!(await fs.readdir(value.root)).includes('ide.update-lock'));
  });
}

test('rollback failure preserves the sole configured helper and recovery paths', linux, async t => {
  const value = await fixture(t, { failRename: [5, 6] });
  await assert.rejects(value.install(), /automatic rollback failed.*Preserve.*chrome-sandbox/);
  const entries = await fs.readdir(value.root);
  const retained = entries.find(name => name.startsWith('.local-coder-update-'));
  assert.ok(retained);
  assert.equal((await fs.stat(path.join(value.root, retained, 'app/chrome-sandbox'))).ino, value.originalInode);
  assert.ok(entries.some(name => name.startsWith('ide.previous-')));
  assert.ok(!entries.includes('ide.update-lock'));
});
