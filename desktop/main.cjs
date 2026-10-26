const { app, BrowserWindow, dialog, ipcMain, Menu } = require('electron');
const { randomBytes, createHash } = require('node:crypto');
const path = require('node:path');
const fs = require('node:fs');
const os = require('node:os');
const { pathToFileURL } = require('node:url');
const { spawn } = require('node:child_process');
const { Sidecar, isAllowedApiRequest } = require('./sidecar.cjs');
let window, sidecar, token, origin, project, quitting = false, switching = false;
const root = path.resolve(__dirname, '..');
function trusted(event) { return window && event.sender === window.webContents && event.senderFrame === window.webContents.mainFrame && event.senderFrame.url.startsWith(origin + '/'); }
async function request(pathname, method = 'GET', body) {
  if (!origin || !isAllowedApiRequest(pathname, method)) throw new Error('Unsupported engine request');
  const payload = body === undefined ? undefined : JSON.stringify(body);
  if (payload && payload.length > 100000) throw new Error('Request is too large');
  const response = await fetch(origin + pathname, { method, headers: { Authorization: `Bearer ${token}`, ...(payload ? { 'Content-Type': 'application/json' } : {}) }, body: payload, redirect: 'error', signal: AbortSignal.timeout(30000) });
  return { status: response.status, data: await response.json() };
}
function startupStatus(message) {
  const file = path.join(app.getPath('userData'), 'logs', 'startup.log');
  fs.mkdirSync(path.dirname(file), { recursive: true, mode: 0o700 });
  const fd = fs.openSync(file, 'a', 0o600);
  try { fs.fchmodSync(fd, 0o600); fs.writeSync(fd, `${new Date().toISOString()} ${message}\n`); } finally { fs.closeSync(fd); }
}
async function startEngine(folder) {
  token = randomBytes(32).toString('hex');
  const command = app.isPackaged ? path.join(process.resourcesPath, 'backend', process.platform === 'win32' ? 'local-coder-engine.exe' : 'local-coder-engine') : (process.env.LOCAL_CODER_PYTHON || 'python');
  sidecar = new Sidecar({ command, token, args: app.isPackaged ? [] : ['-m', 'local_coder.desktop_engine'], cwd: folder,
    env: { ...process.env, PYTHONPATH: app.isPackaged ? '' : path.join(root, 'src'), LOCAL_CODER_DESKTOP_PROJECT: folder, LOCAL_CODER_REMOTE_TOKEN: token },
    onExit: () => { if (!quitting && !switching) { origin = null; dialog.showErrorBox('Local engine stopped', 'The engine exited unexpectedly. Close and reopen Local Coder. Saved task reports remain in your workspace.'); } } });
  origin = await sidecar.start(); token = sidecar.token; project = folder;
  startupStatus(sidecar.child ? 'engine_ready owned' : 'engine_ready shared');
}
async function chooseProject() {
  if (switching) return;
  if (origin) { const status = await request('/status'); if (status.data.sessions.some(session => ['queued', 'running'].includes(session.status))) throw new Error('Stop the active task before changing workspace'); }
  const result = await dialog.showOpenDialog(window, { title: 'Open project folder', properties: ['openDirectory'] });
  if (result.canceled) return;
  switching = true;
  try { await sidecar?.stop(); await startEngine(result.filePaths[0]); fs.writeFileSync(path.join(app.getPath('userData'), 'workspace.json'), JSON.stringify({ project })); await window.loadURL(origin); }
  finally { switching = false; }
}
function installedIDE({ managerExecutable = process.execPath, platform = process.platform, exists = fs.existsSync } = {}) {
  const names = platform === 'darwin' ? ['Local Coder IDE.app/Contents/MacOS/Local Coder IDE'] : [platform === 'win32' ? 'Local Coder IDE.exe' : 'local-coder-ide'];
  const candidates = names.flatMap(name => [path.resolve(path.dirname(managerExecutable), '..', 'ide', name), path.join(os.homedir(), '.local', 'opt', 'local-coder', 'current', 'ide', name)]);
  return candidates.find(exists);
}
async function spawnIDE(executable, args, { cwd, env, logPath, spawnProcess = spawn, startupMs = 3000 }) {
  fs.mkdirSync(path.dirname(logPath), { recursive: true, mode: 0o700 });
  // Never put credentials or child output in the UI. The private log records native startup diagnostics.
  const fd = fs.openSync(logPath, 'w', 0o600);
  fs.fchmodSync(fd, 0o600);
  let child;
  try { child = spawnProcess(executable, args, { cwd, detached: true, stdio: ['ignore', fd, fd], windowsHide: false, env }); }
  finally { fs.closeSync(fd); }
  await new Promise((resolve, reject) => {
    const timer = setTimeout(() => { cleanup(); resolve(); }, startupMs);
    const cleanup = () => { clearTimeout(timer); child.removeListener('error', failed); child.removeListener('exit', exited); };
    const failed = error => { cleanup(); reject(new Error(`Cannot start Local Coder IDE: ${error.message}. Startup diagnostics: ${logPath}`)); };
    const exited = (code, signal) => { cleanup(); code === 0 ? resolve() : reject(new Error(`Local Coder IDE stopped during startup (${signal || `exit ${code}`}). Startup diagnostics: ${logPath}`)); };
    child.once('error', failed); child.once('exit', exited);
  });
  child.unref();
}
async function openIDE() {
  const environment = { ...process.env, LOCAL_CODER_ENGINE_URL: origin, LOCAL_CODER_REMOTE_TOKEN: token,
    LOCAL_CODER_DESKTOP_PROJECT: project, LOCAL_CODER_IDE_DATA_HOME: path.join(app.getPath('userData'), 'ide-profiles') };
  delete environment.ELECTRON_RUN_AS_NODE;
  let executable, args, cwd;
  const launcher = path.join(root, 'ide', 'scripts', 'launch.mjs');
  if (!app.isPackaged && fs.existsSync(launcher) && fs.existsSync(path.join(root, 'ide', 'upstream', 'out', 'main.js'))) {
    const { findLaunch, launchArguments } = await import(pathToFileURL(launcher).href);
    const spec = findLaunch();
    executable = spec.executable; args = [...spec.args, ...launchArguments(project, environment)]; cwd = spec.cwd;
    if (spec.development) Object.assign(environment, { VSCODE_DEV: '1', VSCODE_CLI: '1', NODE_ENV: 'development', LOCAL_CODER_REPO_ROOT: root });
  } else {
    const settingsPath = path.join(app.getPath('userData'), 'ide.json');
    executable = process.env.LOCAL_CODER_IDE_EXECUTABLE;
    if (!executable) try { executable = JSON.parse(fs.readFileSync(settingsPath, 'utf8')).executable; } catch {}
    if (!executable || !fs.existsSync(executable)) executable = installedIDE();
    if (!executable || !fs.existsSync(executable)) {
      const result = await dialog.showOpenDialog(window, { title: 'Select your built Local Coder IDE application', properties: ['openFile'],
        ...(process.platform === 'win32' ? { filters: [{ name: 'Application', extensions: ['exe'] }] } : {}) });
      if (result.canceled) return;
      executable = result.filePaths[0];
      if (process.platform === 'darwin' && executable.endsWith('.app')) {
        const binaries = path.join(executable, 'Contents', 'MacOS');
        const names = fs.readdirSync(binaries).filter(name => fs.statSync(path.join(binaries, name)).isFile());
        if (names.length !== 1) throw new Error('Select the IDE executable inside the application bundle');
        executable = path.join(binaries, names[0]);
      }
      fs.writeFileSync(settingsPath, JSON.stringify({ executable }));
    }
    const profile = path.join(app.getPath('userData'), 'ide-profiles', createHash('sha256').update(token).digest('hex').slice(0, 16));
    args = ['--new-window', '--user-data-dir', profile, project];
  }
  await spawnIDE(executable, args, { cwd, env: environment, logPath: path.join(app.getPath('userData'), 'logs', 'ide-startup.log') });
}
app.whenReady().then(async () => {
  try {
    let folder = process.env.LOCAL_CODER_DESKTOP_PROJECT;
    if (!folder) try { folder = JSON.parse(fs.readFileSync(path.join(app.getPath('userData'), 'workspace.json'), 'utf8')).project; } catch {}
    if (!folder || !fs.existsSync(folder)) { const result = await dialog.showOpenDialog({ title: 'Choose your project workspace', properties: ['openDirectory'] }); if (result.canceled) { app.quit(); return; } folder = result.filePaths[0]; }
    await startEngine(folder);
    window = new BrowserWindow({ width: 1440, height: 940, minWidth: 760, minHeight: 550, backgroundColor: '#171717', title: 'Local Coder', show: false,
      webPreferences: { preload: path.join(__dirname, 'preload.cjs'), nodeIntegration: false, contextIsolation: true, sandbox: true, webSecurity: true } });
    window.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
    window.webContents.on('will-navigate', (event, url) => { if (!url.startsWith(origin + '/')) event.preventDefault(); });
    window.webContents.session.setPermissionRequestHandler((_contents, _permission, callback) => callback(false));
    window.webContents.session.on('will-download', event => event.preventDefault());
    ipcMain.handle('engine:request', (event, pathname, method, body) => { if (!trusted(event)) throw new Error('Untrusted sender'); return request(pathname, method, body); });
    ipcMain.handle('inference:choose-file', async (event, purpose) => {
      if (!trusted(event)) throw new Error('Untrusted sender');
      if (!['runtime', 'model', 'draft'].includes(purpose)) throw new Error('Unsupported inference file purpose');
      const result = await dialog.showOpenDialog(window, {
        title: purpose === 'runtime' ? 'Choose local inference runtime' : purpose === 'draft' ? 'Choose draft GGUF model' : 'Choose GGUF model',
        properties: ['openFile'],
        ...(purpose !== 'runtime' ? { filters: [{ name: 'GGUF model', extensions: ['gguf'] }] } : process.platform === 'win32' ? { filters: [{ name: 'Application', extensions: ['exe'] }] } : {})
      });
      return result.canceled ? null : result.filePaths[0];
    });
    ipcMain.handle('ide:open', event => { if (!trusted(event)) throw new Error('Untrusted sender'); return openIDE(); });
    ipcMain.handle('workspace:choose', event => { if (!trusted(event)) throw new Error('Untrusted sender'); return chooseProject(); });
    Menu.setApplicationMenu(Menu.buildFromTemplate([
      ...(process.platform === 'darwin' ? [{ label: app.name, submenu: [{ role: 'about' }, { role: 'quit' }] }] : []),
      { label: 'File', submenu: [{ label: 'Open IDE', accelerator: 'CmdOrCtrl+Shift+I', click: () => { void openIDE().catch(error => dialog.showErrorBox('Cannot open IDE', error.message)); } }, { label: 'Open Project…', accelerator: 'CmdOrCtrl+O', click: () => { void chooseProject().catch(error => dialog.showErrorBox('Cannot open workspace', error.message)); } }, { role: 'close' }] },
      { label: 'Edit', submenu: [{ role: 'undo' }, { role: 'redo' }, { type: 'separator' }, { role: 'cut' }, { role: 'copy' }, { role: 'paste' }, { role: 'selectAll' }] },
      { label: 'View', submenu: [{ role: 'reload' }, { role: 'resetZoom' }, { role: 'zoomIn' }, { role: 'zoomOut' }, { role: 'togglefullscreen' }] },
    ]));
    window.on('close', () => startupStatus('window_close_requested'));
    window.webContents.on('render-process-gone', (_event, details) => startupStatus(`renderer_stopped ${details.reason} ${details.exitCode}`));
    window.once('ready-to-show', () => { window.show(); startupStatus('window_ready'); });
    await window.loadURL(origin);
  } catch (error) { startupStatus('startup_failed: ' + error.message); dialog.showErrorBox('Cannot start Local Coder', error.message + '\nFor development, install the Python project and build the web UI first.'); app.quit(); }
});
app.on('window-all-closed', () => app.quit());
app.on('before-quit', event => { startupStatus('quit_requested'); if (!quitting) { event.preventDefault(); quitting = true; void sidecar?.stop().finally(() => app.quit()); } });

module.exports = { installedIDE, spawnIDE };
