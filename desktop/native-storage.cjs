'use strict';
const fs = require('node:fs/promises');
const { constants } = require('node:fs');
const path = require('node:path');
const { createHash, randomBytes } = require('node:crypto');
const REPORT_LIMIT = 8 * 1024 * 1024;
const PIN_LIMIT = 32768;

function validateReport(text, suggestedName = 'clin-report.md') {
  if (typeof text !== 'string' || text.length > REPORT_LIMIT || Buffer.byteLength(text, 'utf8') > REPORT_LIMIT) throw new Error('Report must be Markdown text of at most 8 MiB');
  if (typeof suggestedName !== 'string' || suggestedName.length < 4 || suggestedName.length > 200 || /[\\/\x00-\x1f\x7f]/.test(suggestedName) || !/\.md$/i.test(suggestedName)) throw new Error('Choose a Markdown filename ending in .md');
  return suggestedName;
}
function validateTaskPins(ids) {
  if (!Array.isArray(ids) || ids.length > 200 || ids.some(id => typeof id !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/.test(id))) throw new Error('Task pins must contain at most 200 valid task IDs');
  return [...new Set(ids)];
}
async function regular(file, missing = false) {
  try {
    const info = await fs.lstat(file);
    if (!info.isFile() || info.isSymbolicLink() || info.nlink !== 1) throw new Error('Storage target must be a regular file without links');
    return info;
  } catch (error) { if (missing && error.code === 'ENOENT') return null; throw error; }
}
async function atomicWrite(file, text) {
  await regular(file, true);
  const temporary = path.join(path.dirname(file), `.clin-${randomBytes(12).toString('hex')}.tmp`);
  let handle;
  try {
    handle = await fs.open(temporary, 'wx', 0o600);
    await handle.chmod(0o600);
    await handle.writeFile(text, 'utf8');
    await handle.sync();
    await handle.close(); handle = null;
    await regular(file, true);
    await fs.rename(temporary, file);
  } finally {
    if (handle) await handle.close();
    await fs.unlink(temporary).catch(error => { if (error.code !== 'ENOENT') throw error; });
  }
}
async function writeReport(file, text) {
  if (typeof file !== 'string' || !path.isAbsolute(file) || path.extname(file).toLowerCase() !== '.md') throw new Error('Choose a Markdown file ending in .md');
  validateReport(text, path.basename(file));
  await atomicWrite(file, text);
}
async function pinsPath(userData, project, create) {
  const canonical = await fs.realpath(project);
  const directory = path.join(userData, 'workspace-pins');
  let details;
  try { details = await fs.lstat(directory); }
  catch (error) {
    if (error.code !== 'ENOENT') throw error;
    if (!create) return null;
    await fs.mkdir(directory, { mode: 0o700 }).catch(error => { if (error.code !== 'EEXIST') throw error; });
    details = await fs.lstat(directory);
  }
  if (!details.isDirectory() || details.isSymbolicLink()) throw new Error('Task pin storage directory must not be a symlink');
  if (create) await fs.chmod(directory, 0o700);
  return path.join(directory, createHash('sha256').update(canonical).digest('hex') + '.json');
}
async function loadTaskPins(userData, project) {
  const file = await pinsPath(userData, project, false);
  if (!file) return [];
  const entry = await regular(file, true);
  if (!entry) return [];
  const handle = await fs.open(file, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
  try {
    const details = await handle.stat();
    if (!details.isFile() || details.nlink !== 1 || details.ino !== entry.ino || details.dev !== entry.dev || details.size > PIN_LIMIT) throw new Error('Task pin storage must be a bounded regular file');
    const bytes = Buffer.alloc(PIN_LIMIT + 1); let size = 0;
    while (size < bytes.length) {
      const { bytesRead } = await handle.read(bytes, size, bytes.length - size, size);
      if (!bytesRead) break;
      size += bytesRead;
    }
    if (size > PIN_LIMIT) throw new Error('Task pin storage exceeds its size limit');
    return validateTaskPins(JSON.parse(bytes.subarray(0, size).toString('utf8')));
  } finally { await handle.close(); }
}
async function saveTaskPins(userData, project, ids) {
  const pins = validateTaskPins(ids);
  const file = await pinsPath(userData, project, true);
  await atomicWrite(file, JSON.stringify(pins) + '\n');
}
module.exports = { validateReport, validateTaskPins, writeReport, loadTaskPins, saveTaskPins };
