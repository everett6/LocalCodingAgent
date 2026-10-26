const { spawn, execFile } = require('node:child_process');
const { discoverEngine } = require('./discovery.cjs');

function isAllowedApiRequest(path, method) {
  if (typeof path !== 'string' || path.length > 4096 || !path.startsWith('/') || path.startsWith('//') || /[\\#\r\n]/.test(path)) return false;
  const pathname = path.split('?')[0];
  if (method === 'GET') return ['/status', '/settings', '/events', '/workspace', '/diff', '/file', '/files', '/sessions', '/engagements', '/inference', '/security-playbooks', '/memory', '/memory/status', '/memory/search', '/harness', '/snapshots', '/approvals', '/integrations/mcp', '/integrations/skills', '/language/status'].includes(pathname)
    || /^\/engagements\/eng-[a-f0-9]{12}(\/report)?$/.test(pathname) || /^\/jobs\/[a-zA-Z0-9_-]+$/.test(pathname)
    || /^\/snapshots\/[a-f0-9]{32}\/diff$/.test(pathname) || /^\/integrations\/skills\/[a-z][a-z0-9-]{0,63}$/.test(pathname);
  if (method === 'POST') return ['/settings', '/jobs', '/engagements', '/inference/configure', '/inference/start', '/inference/stop', '/inference/install-runtime', '/memory/index', '/harness/configure', '/harness/probe-sandbox', '/integrations/mcp/configure', '/integrations/mcp/probe', '/integrations/skills/save', '/language/context', '/language/configure'].includes(pathname)
    || /^\/jobs\/[a-zA-Z0-9_-]+\/cancel$/.test(pathname) || /^\/memory\/[a-f0-9]{32}\/(update|delete)$/.test(pathname)
    || /^\/snapshots\/[a-f0-9]{32}\/restore$/.test(pathname) || /^\/approvals\/[a-f0-9]{32}\/respond$/.test(pathname);
  return false;
}
class Sidecar {
  constructor({ command, args = [], cwd, env = process.env, timeout = 30000, token, discoveryDirectory, onExit = () => {} }) {
    Object.assign(this, { command, args, cwd, env, timeout, token, discoveryDirectory, onExit }); this.child = null; this.origin = null;
  }
  async start() {
    if (this.child) throw new Error('Engine already started');
    const attach = async () => {
      const connection = await discoverEngine(this.cwd, this.discoveryDirectory);
      if (!connection) return false;
      this.origin = connection.origin; this.token = connection.token; return true;
    };
    if (await attach()) return this.origin;
    try { return await this._spawn(); } catch (error) {
      await this.stop();
      for (let attempt = 0; error.message.includes('exited') && attempt < 10; attempt++) {
        if (await attach()) return this.origin;
        await new Promise(resolve => setTimeout(resolve, 100));
      }
      throw error;
    }
  }
  async _spawn() {
    return new Promise((resolve, reject) => {
      const child = spawn(this.command, this.args, { cwd: this.cwd, env: this.env, windowsHide: true, detached: process.platform !== 'win32', stdio: ['ignore', 'pipe', 'pipe'] });
      this.child = child;
      let buffer = '', settled = false;
      const finish = (error, origin) => { if (settled) return; settled = true; clearTimeout(timer); if (error) reject(error); else resolve(origin); };
      const timer = setTimeout(() => { finish(new Error('Local engine startup timed out')); void this.stop(); }, this.timeout);
      child.stdout.on('data', chunk => {
        buffer += chunk.toString();
        if (buffer.length > 65536) { finish(new Error('Unexpected engine startup output')); void this.stop(); return; }
        let end;
        while ((end = buffer.indexOf('\n')) >= 0) {
          const line = buffer.slice(0, end); buffer = buffer.slice(end + 1);
          try { const data = JSON.parse(line); if (Number.isInteger(data.port) && data.port > 0 && data.port < 65536) { this.origin = `http://127.0.0.1:${data.port}`; finish(null, this.origin); } } catch { /* Other startup output is not a readiness signal. */ }
        }
      });
      child.stderr.on('data', () => {}); // Do not persist model prompts or credentials in desktop logs.
      child.once('error', error => finish(new Error(`Cannot start the local engine: ${error.code || 'unknown error'}`)));
      child.once('exit', code => { finish(new Error(`Local engine exited before readiness (${code})`)); if (this.child === child) { this.child = null; if (this.origin) this.onExit(code); } });
    });
  }
  async stop() {
    const child = this.child, origin = this.origin; this.child = null; this.origin = null;
    if (!child || child.exitCode !== null) return;
    if (origin && this.token) {
      try { await fetch(origin + '/shutdown', { method: 'POST', headers: { Authorization: `Bearer ${this.token}`, 'Content-Type': 'application/json' }, body: '{}', signal: AbortSignal.timeout(15000) }); } catch {}
      if (child.exitCode !== null) return;
    }
    await new Promise(resolve => {
      const timer = setTimeout(() => { try { if (process.platform !== 'win32') process.kill(-child.pid, 'SIGKILL'); else child.kill(); } catch {} resolve(); }, 3000);
      child.once('exit', () => { clearTimeout(timer); resolve(); });
      if (process.platform === 'win32') execFile('taskkill', ['/pid', String(child.pid), '/T', '/F'], { windowsHide: true }, () => {});
      else { try { process.kill(-child.pid, 'SIGTERM'); } catch { clearTimeout(timer); resolve(); } }
    });
  }
}
module.exports = { Sidecar, isAllowedApiRequest };
