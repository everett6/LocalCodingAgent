const { spawn, execFile } = require('node:child_process');
const { discoverEngine } = require('./discovery.cjs');

function startupError(code, signal, diagnostic) {
  const details = {
    engine_dependency_missing: 'A required engine dependency is missing; repair or reinstall the application.',
    engine_permission_denied: 'The engine could not access a required file; check workspace and application permissions.',
    engine_workspace_busy: 'The workspace database is busy; close duplicate workspace engines and retry.',
    engine_state_invalid: 'Saved workspace state is invalid. Preserve the original state and restore a valid copy; reinstalling the app will not repair project history.',
  };
  const classification = details[diagnostic] ? diagnostic : 'engine_exited';
  return Object.assign(new Error(`Local engine exited before readiness (${signal || code}). ${details[classification] || 'Retry startup; if it repeats, check the application installation.'}`),
    {code: classification, exitCode: code, signal, retryable: classification === 'engine_exited' && code === 1 && !signal});
}

function isAllowedApiRequest(path, method) {
  if (typeof path !== 'string' || path.length > 4096 || !path.startsWith('/') || path.startsWith('//') || /[\\#\r\n]/.test(path)) return false;
  const pathname = path.split('?')[0];
  if (pathname === '/undo') return path === pathname && method === 'POST';
  if (pathname === '/integrations/search/doctor') return path === pathname && ['GET', 'POST'].includes(method);
  if (pathname === '/context/debugger') return path === pathname && method === 'GET';
  if (pathname === '/status' && path.includes('?summary')) return path === '/status?summary=1' && method === 'GET';
  if (method === 'GET' && ['/evaluations', '/findings', '/memory/retrievals', '/context/accounting', '/research/advisories/updates', '/inference/tuning/history', '/doctor'].includes(pathname)) return true;
  if (method === 'GET' && (/^\/evaluations\/[a-f0-9]{32}$/.test(pathname) || /^\/(evaluations|findings|inference\/tuning)\/jobs\/[a-f0-9]{32}$/.test(pathname) || /^\/findings\/[a-f0-9]{24}$/.test(pathname) || /^\/memory\/retrievals\/[a-f0-9]{32}$/.test(pathname))) return true;
  if (method === 'POST' && ['/evaluations/run', '/evaluations/model/run', '/evaluations/heldout/run', '/context/tokenizer', '/research/advisories/update', '/inference/tuning/start', '/inference/tuning/apply', '/doctor/repair'].includes(pathname)) return true;
  if (method === 'POST' && (/^\/(evaluations|findings|inference\/tuning)\/jobs\/[a-f0-9]{32}\/cancel$/.test(pathname) || /^\/findings\/[a-f0-9]{24}\/(review|prepare|verify|suppress|reopen)$/.test(pathname))) return true;
  if (method === 'GET' && ['/research/capabilities', '/research/evidence', '/research/report', '/research/dependencies', '/research/advisories', '/memory/structure'].includes(pathname)) return true;
  if (method === 'POST' && ['/research/audit', '/research/sarif/import', '/research/advisories/import'].includes(pathname)) return true;
  if (method === 'GET') return ['/goals', '/bug-hunting/models', '/status', '/settings', '/events', '/workspace', '/diff', '/file', '/files', '/sessions', '/engagements', '/engagements/defaults', '/inference', '/inference/discovery', '/providers/models', '/providers/usage', '/providers/quota', '/providers/profiles', '/security-playbooks', '/memory', '/memory/status', '/memory/search', '/harness', '/snapshots', '/approvals', '/integrations/mcp', '/integrations/skills', '/language/status'].includes(pathname)
    || /^\/engagements\/eng-[a-f0-9]{12}(\/report)?$/.test(pathname) || /^\/jobs\/[a-zA-Z0-9_-]+$/.test(pathname)
    || /^\/snapshots\/[a-f0-9]{32}\/diff$/.test(pathname) || /^\/integrations\/skills\/[a-z][a-z0-9-]{0,63}$/.test(pathname);
  if (method === 'POST') return ['/goals', '/goals/update', '/bug-hunting/models', '/settings', '/jobs', '/engagements', '/engagements/defaults', '/providers/discover', '/providers/profiles/save', '/inference/discover', '/inference/discovery/cancel', '/inference/discovery/select', '/inference/configure', '/inference/start', '/inference/stop', '/inference/install-runtime', '/memory/index', '/harness/configure', '/harness/probe-sandbox', '/integrations/mcp/configure', '/integrations/mcp/probe', '/integrations/skills/save', '/language/context', '/language/configure'].includes(pathname)
    || /^\/jobs\/[a-zA-Z0-9_-]+\/cancel$/.test(pathname) || /^\/memory\/[a-f0-9]{32}\/(update|delete)$/.test(pathname)
    || /^\/snapshots\/[a-f0-9]{32}\/restore$/.test(pathname) || /^\/approvals\/[a-f0-9]{32}\/respond$/.test(pathname)
    || /^\/providers\/profiles\/[a-f0-9]{32}\/(select|delete)$/.test(pathname);
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
      let buffer = '', settled = false, diagnostic = null, stderrTail = '', stderrBytes = 0;
      const finish = (error, origin) => {
        if (settled) return;
        settled = true; clearTimeout(timer);
        child.stdout.off('data', onData); child.stderr.off('data', onStderr);
        buffer = ''; stderrTail = '';
        // Keep draining pipes without retaining runtime output or interpreting it
        // as another readiness message. Never persist raw engine stderr.
        child.stdout.resume(); child.stderr.resume();
        if (error) reject(error); else resolve(origin);
      };
      const timer = setTimeout(() => { finish(new Error('Local engine startup timed out')); void this.stop(); }, this.timeout);
      const onData = chunk => {
        buffer += chunk.toString();
        if (buffer.length > 65536) { finish(new Error('Unexpected engine startup output')); void this.stop(); return; }
        let end;
        while ((end = buffer.indexOf('\n')) >= 0) {
          const line = buffer.slice(0, end); buffer = buffer.slice(end + 1);
          try { const data = JSON.parse(line); if (Number.isInteger(data.port) && data.port > 0 && data.port < 65536) { this.origin = `http://127.0.0.1:${data.port}`; finish(null, this.origin); } } catch { /* Other startup output is not a readiness signal. */ }
        }
      };
      const onStderr = chunk => {
        if (stderrBytes >= 65536) return;
        const sample = chunk.subarray(0, 65536 - stderrBytes); stderrBytes += sample.length;
        const text = stderrTail + sample.toString();
        if (/ModuleNotFoundError:|ImportError:/.test(text)) diagnostic = 'engine_dependency_missing';
        else if (!diagnostic && /PermissionError:/.test(text)) diagnostic = 'engine_permission_denied';
        else if (!diagnostic && /(?:OperationalError:|DatabaseError:) database is locked/.test(text)) diagnostic = 'engine_workspace_busy';
        else if (!diagnostic && /(?:^|\n|\.)SessionError:/.test(text)) diagnostic = 'engine_state_invalid';
        stderrTail = text.slice(-128);
      };
      child.stdout.on('data', onData);
      child.stderr.on('data', onStderr);
      child.once('error', error => finish(new Error(`Cannot start the local engine: ${error.code || 'unknown error'}`)));
      child.once('exit', (code, signal) => { finish(startupError(code, signal, diagnostic)); if (this.child === child) { this.child = null; if (this.origin) this.onExit(code); } });
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
