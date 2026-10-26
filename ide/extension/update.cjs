'use strict';
// Our release channel contains rebuilt Local Coder forks; Microsoft's branded binaries are never installed.
const fs = require('node:fs/promises');
const { constants } = require('node:fs');
const path = require('node:path');
const { createHash } = require('node:crypto');
const { execFileSync } = require('node:child_process');
const channel = 'https://api.github.com/repos/everett6/LocalCodingAgent';
async function response(url) {
  const value = await fetch(url, { headers: { Accept: 'application/vnd.github+json' }, signal: AbortSignal.timeout(600000) });
  if (!value.ok) throw new Error(`Fork update download failed (HTTP ${value.status})`);
  return value;
}
async function get(url) {
  const value = await response(url), chunks = []; let size = 0;
  for await (const chunk of value.body) {
    size += chunk.length;
    if (size > 2 * 1024 * 1024) throw new Error('Fork update metadata exceeds size limit');
    chunks.push(chunk);
  }
  return JSON.parse(Buffer.concat(chunks).toString('utf8'));
}
async function download(url, file, expected) {
  const value = await response(url);
  if (Number(value.headers.get('content-length')) > 700 * 1024 * 1024) throw new Error('Fork update exceeds download limit');
  const output = await fs.open(file, 'wx', 0o600), digest = createHash('sha256'); let size = 0;
  try {
    for await (const chunk of value.body) {
      size += chunk.length;
      if (size > 700 * 1024 * 1024) throw new Error('Fork update exceeds download limit');
      digest.update(chunk); await output.write(chunk);
    }
  } finally { await output.close(); }
  if (digest.digest('hex') !== expected) throw new Error('Fork update checksum mismatch');
}
function asset(release, name) {
  const value = release.assets.find(item => item.name === name);
  if (!value || !value.browser_download_url.startsWith('https://github.com/everett6/LocalCodingAgent/releases/download/')) throw new Error(`Fork release is missing ${name}`);
  return value.browser_download_url;
}
async function latest() {
  const releases = await get(`${channel}/releases?per_page=30`);
  return releases.find(release => !release.draft && !release.prerelease && release.tag_name.startsWith('ide-vscode-'));
}
async function sandboxInfo(file, configured) {
  const setup = 'Linux chrome-sandbox cannot be preserved safely; keep the current installation and use manual installation with administrator sandbox setup.';
  let handle;
  try {
    const entry = await fs.lstat(file);
    if (!entry.isFile() || entry.isSymbolicLink()) throw new Error(setup);
    handle = await fs.open(file, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
    const before = await handle.stat();
    if (!before.isFile() || before.nlink !== 1 || before.ino !== entry.ino || before.dev !== entry.dev ||
        before.size < 1 || before.size > 16 * 1024 * 1024 ||
        (configured && (before.uid !== 0 || (before.mode & 0o7777) !== 0o4755))) throw new Error(setup);
    const digest = createHash('sha256'), buffer = Buffer.alloc(65536); let size = 0;
    for (;;) {
      const { bytesRead } = await handle.read(buffer, 0, buffer.length, null);
      if (!bytesRead) break;
      size += bytesRead;
      if (size > before.size) throw new Error(setup);
      digest.update(buffer.subarray(0, bytesRead));
    }
    const after = await handle.stat();
    if (size !== before.size || after.size !== before.size || after.mtimeMs !== before.mtimeMs || after.ctimeMs !== before.ctimeMs) throw new Error(setup);
    return { sha256: digest.digest('hex'), ino: before.ino, dev: before.dev };
  } catch (_) { throw new Error(setup); }
  finally { if (handle) await handle.close(); }
}
async function install(release, executable = process.execPath) {
  if (process.platform !== 'linux' || process.arch !== 'x64') throw new Error('Automatic native installation currently supports Linux x64');
  const destination = path.dirname(await fs.realpath(executable));
  const product = JSON.parse(await fs.readFile(path.join(destination, 'resources/app/product.json'), 'utf8'));
  if (product.applicationName !== 'local-coder-ide') throw new Error('Updates can only replace a Local Coder IDE installation');
  const lock = `${destination}.update-lock`;
  await fs.mkdir(lock); // Atomic lock: a second editor cannot update the same installation concurrently.
  let temporary, retainTemporary = false;
  try {
    const manifest = await get(asset(release, 'ide-linux-x64.json'));
    if (manifest.tag !== release.tag_name || manifest.platform !== 'linux' || manifest.arch !== 'x64' || !/^[a-f0-9]{64}$/.test(manifest.sha256) || manifest.asset !== 'local-coder-ide-linux-x64.tar.gz') throw new Error('Invalid fork release manifest');
    if (product.localCoderRelease === manifest.tag) return false;
    const installedVersion = JSON.parse(await fs.readFile(path.join(destination, 'resources/app/package.json'), 'utf8')).version;
    const parts = value => String(value).split('.').map(Number);
    const a = parts(manifest.version), b = parts(installedVersion);
    if (a.length !== 3 || a.some(value => !Number.isInteger(value))) throw new Error('Invalid fork release version');
    for (let index = 0; index < 3; index++) { if (a[index] < b[index]) return false; if (a[index] > b[index]) break; }
    temporary = await fs.mkdtemp(path.join(path.dirname(destination), '.local-coder-update-'));
    const archive = path.join(temporary, 'app.tar.gz');
    await download(asset(release, manifest.asset), archive, manifest.sha256);
    const members = execFileSync('tar', ['-tzf', archive], { encoding: 'utf8', maxBuffer: 16 * 1024 * 1024 }).trim().split('\n');
    if (members.some(name => name.startsWith('/') || name.split('/').includes('..'))) throw new Error('Unsafe fork archive path');
    const types = execFileSync('tar', ['-tvzf', archive], { encoding: 'utf8', maxBuffer: 16 * 1024 * 1024 }).trim().split('\n');
    if (types.some(line => !['-', 'd'].includes(line[0]))) throw new Error('Fork archives may only contain regular files and directories');
    const staged = path.join(temporary, 'app'); await fs.mkdir(staged);
    execFileSync('tar', ['-xzf', archive, '-C', staged]);
    const next = JSON.parse(await fs.readFile(path.join(staged, 'resources/app/product.json'), 'utf8'));
    if (next.applicationName !== 'local-coder-ide' || next.localCoderRelease !== manifest.tag) throw new Error('Fork package identity does not match release');
    await fs.access(path.join(staged, 'local-coder-ide'), 1);
    const currentSandbox = await sandboxInfo(path.join(destination, 'chrome-sandbox'), true);
    const incomingSandbox = await sandboxInfo(path.join(staged, 'chrome-sandbox'), false);
    if (currentSandbox.sha256 !== incomingSandbox.sha256) throw new Error('Linux chrome-sandbox changed; use manual installation and administrator sandbox setup. The current installation was retained.');
    const backup = `${destination}.previous-${Date.now()}`;
    await fs.rename(destination, backup);
    const oldHelper = path.join(backup, 'chrome-sandbox'), newHelper = path.join(staged, 'chrome-sandbox');
    const heldHelper = path.join(temporary, 'configured-chrome-sandbox');
    let configuredLocation = 'backup', incomingInBackup = false;
    try {
      // Renames preserve the existing root-owned setuid inode without privilege
      // changes. The historical backup receives the incoming unconfigured helper;
      // manual rollback must swap the configured helper back before launching it.
      const checked = await sandboxInfo(oldHelper, true);
      if (checked.ino !== currentSandbox.ino || checked.dev !== currentSandbox.dev || checked.sha256 !== currentSandbox.sha256) throw new Error('Configured Linux chrome-sandbox changed during update; use manual sandbox setup.');
      await fs.rename(oldHelper, heldHelper); configuredLocation = 'held';
      await fs.rename(newHelper, oldHelper); incomingInBackup = true;
      await fs.rename(heldHelper, newHelper); configuredLocation = 'staged';
      await sandboxInfo(newHelper, true);
      await fs.rename(staged, destination);
    } catch (error) {
      try {
        if (configuredLocation === 'staged') { await fs.rename(newHelper, heldHelper); configuredLocation = 'held'; }
        if (incomingInBackup) { await fs.rename(oldHelper, newHelper); incomingInBackup = false; }
        if (configuredLocation === 'held') { await fs.rename(heldHelper, oldHelper); configuredLocation = 'backup'; }
        await fs.rename(backup, destination);
      } catch (rollback) {
        // Never remove the only configured inode if rollback itself is blocked.
        retainTemporary = true;
        throw new Error(`Update failed (${error.message}); automatic rollback failed (${rollback.message}). Preserve ${backup} and ${temporary}; restore the configured chrome-sandbox before launching. Administrator sandbox setup may be required.`);
      }
      throw error;
    }
    return true;
  } finally { if (temporary && !retainTemporary) await fs.rm(temporary, { recursive: true, force: true }); await fs.rmdir(lock); }
}
module.exports = { latest, install, asset };
