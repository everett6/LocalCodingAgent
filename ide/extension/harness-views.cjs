'use strict';
const fs = require('node:fs');
const path = require('node:path');

const SCHEME = 'local-coder-snapshot';
const MAX_TEXT = 200000;
const MAX_STORED = 4000000;
const PROVIDER_TIMEOUT = 5000;

function boundedValue(value, limit = 500, depth = 0) {
  if (typeof value === 'string') return value.slice(0, limit);
  if (value == null || typeof value === 'number' || typeof value === 'boolean') return value;
  if (depth >= 3) return '[nested data omitted]';
  if (Array.isArray(value)) return value.slice(0, 20).map(item => boundedValue(item, 500, depth + 1));
  if (typeof value === 'object') return Object.fromEntries(Object.entries(value).filter(([key]) => !/password|secret|credential|api.?key|private.?key|token/i.test(key)).slice(0, 12).map(([key, item]) => [key.slice(0, 100), boundedValue(item, 500, depth + 1)]));
  return undefined;
}

function inside(root, value, mustExist = false) {
  if (typeof value !== 'string' || !value || value.length > 4096 || value.includes('\0')) throw new Error('Invalid workspace path');
  const base = fs.realpathSync(root);
  const full = path.resolve(base, value);
  const relative = path.relative(base, full);
  if (!relative || relative === '..' || relative.startsWith(`..${path.sep}`) || path.isAbsolute(relative)) throw new Error('Path must be a file inside the workspace');
  if (relative.split(path.sep).some(part => ['.git', '.local-coder', '.ssh', '.aws'].includes(part)) || path.basename(full).startsWith('.env')) throw new Error('State and secret paths are excluded');
  let ancestor = full;
  while (!fs.existsSync(ancestor)) {
    if (mustExist) throw new Error('The active document must be saved inside the workspace');
    // Reject dangling symlinks too: existsSync intentionally follows symlinks.
    try { if (fs.lstatSync(ancestor).isSymbolicLink()) throw new Error('Symlink paths are excluded'); } catch (error) { if (error.code !== 'ENOENT') throw error; }
    ancestor = path.dirname(ancestor);
  }
  const actual = fs.realpathSync(ancestor);
  const actualRelative = path.relative(base, actual);
  if (actualRelative === '..' || actualRelative.startsWith(`..${path.sep}`) || path.isAbsolute(actualRelative)) throw new Error('Path escapes the workspace through a symlink');
  return {full, relative: relative.split(path.sep).join('/')};
}

function registerHarnessViews(vscode, context, getService) {
  const documents = new Map();
  let nextDocument = 0;
  let stored = 0;
  const provider = {provideTextDocumentContent: uri => documents.get(uri.toString()) || ''};
  context.subscriptions.push(vscode.workspace.registerTextDocumentContentProvider(SCHEME, provider));
  context.subscriptions.push({dispose: () => { documents.clear(); stored = 0; }});
  if (vscode.workspace.onDidCloseTextDocument) context.subscriptions.push(vscode.workspace.onDidCloseTextDocument(document => {
    const key = document.uri.toString();
    if (documents.has(key)) { stored -= documents.get(key).length; documents.delete(key); }
  }));

  function trust() { if (!vscode.workspace.isTrusted) throw new Error('Trust this workspace before inspecting local harness context'); }
  function saveDocuments(entries) {
    const texts = entries.map(entry => String(entry.text).slice(0, MAX_TEXT));
    if (documents.size + entries.length > 80 || stored + texts.reduce((sum, text) => sum + text.length, 0) > MAX_STORED) throw new Error('Close existing harness documents before opening more context');
    return entries.map((entry, index) => {
      const uri = vscode.Uri.from({scheme: SCHEME, path: `/${++nextDocument}/${entry.name.replace(/[^a-zA-Z0-9._-]/g, '_').slice(0, 120)}`});
      documents.set(uri.toString(), texts[index]);
      stored += texts[index].length;
      return uri;
    });
  }
  async function showJSON(name, value) {
    let text = JSON.stringify(value, null, 2);
    while (text.length > MAX_TEXT) {
      const collection = ['memories', 'symbols', 'references', 'definitions'].map(key => value[key]).filter(Array.isArray).sort((a, b) => b.length - a.length)[0];
      if (!collection?.length) throw new Error('Harness context exceeds the document size limit');
      collection.pop();
      value.truncated = true;
      text = JSON.stringify(value, null, 2);
    }
    const [uri] = saveDocuments([{name, text}]);
    const document = await vscode.workspace.openTextDocument(uri);
    if (vscode.languages?.setTextDocumentLanguage) await vscode.languages.setTextDocumentLanguage(document, 'json');
    await vscode.window.showTextDocument(document, {preview: true});
  }
  function register(name, handler) {
    context.subscriptions.push(vscode.commands.registerCommand(name, async () => {
      try { trust(); return await handler(); }
      catch (error) { await vscode.window.showErrorMessage(`Local Coder: ${String(error.message || error).slice(0, 400)}`); }
    }));
  }
  function locations(root, raw) {
    return (Array.isArray(raw) ? raw : raw ? [raw] : []).slice(0, 100).flatMap(item => {
      const uri = item?.targetUri || item?.uri;
      if (uri?.scheme !== 'file') return [];
      try {
        const validated = inside(root, uri.fsPath, true);
        const start = (item.targetSelectionRange || item.range)?.start;
        if (!Number.isInteger(start?.line) || !Number.isInteger(start?.character) || start.line < 0 || start.character < 0) return [];
        return [{path: validated.relative, line: start.line + 1, character: start.character}];
      } catch { return []; }
    }).slice(0, 50);
  }
  function symbols(root, raw) {
    const stack = Array.isArray(raw) ? raw.slice(0, 100).reverse() : [];
    const result = [];
    let visited = 0;
    while (stack.length && result.length < 60 && visited++ < 300) {
      const symbol = stack.pop();
      if (!symbol || typeof symbol !== 'object') continue;
      const location = symbol.location ? locations(root, [symbol.location])[0] : undefined;
      if (symbol.location && !location) continue;
      const line = location?.line || ((symbol.selectionRange || symbol.range)?.start?.line + 1);
      if (!Number.isInteger(line) || line < 1) continue;
      result.push({name: String(symbol.name || '').slice(0, 160), kind: typeof symbol.kind === 'number' ? symbol.kind : undefined, line, ...(location ? {path: location.path} : {})});
      if (Array.isArray(symbol.children)) stack.push(...symbol.children.slice(0, 100).reverse());
    }
    return result;
  }

  register('localCoder.languageContext', async () => {
    const editor = vscode.window.activeTextEditor;
    if (!editor || editor.document.uri.scheme !== 'file') throw new Error('Open a saved local source document first');
    const folder = vscode.workspace.getWorkspaceFolder(editor.document.uri);
    if (!folder || folder.uri.scheme !== 'file') throw new Error('The active document must belong to a local workspace');
    const target = inside(folder.uri.fsPath, editor.document.uri.fsPath, true);
    const position = editor.selection.active;
    const inspect = async (_progress, token) => {
      if (token?.isCancellationRequested) throw new Error('Language context cancelled');
      let timer;
      let cancellation;
      const interrupted = new Promise((_, reject) => {
        timer = setTimeout(() => reject(new Error('Language providers timed out after 5 seconds')), PROVIDER_TIMEOUT);
        cancellation = token?.onCancellationRequested(() => reject(new Error('Language context cancelled')));
      });
      try {
        // These are fixed VS Code read-only provider commands, never server-supplied names.
        const result = await Promise.race([Promise.all([
          vscode.commands.executeCommand('vscode.executeDocumentSymbolProvider', editor.document.uri),
          vscode.commands.executeCommand('vscode.executeReferenceProvider', editor.document.uri, position),
          vscode.commands.executeCommand('vscode.executeDefinitionProvider', editor.document.uri, position)
        ]), interrupted]);
        trust();
        if (token?.isCancellationRequested) throw new Error('Language context cancelled');
        inside(folder.uri.fsPath, editor.document.uri.fsPath, true);
        await showJSON('language-context.json', {path: target.relative, position: {line: position.line + 1, character: position.character, encoding: 'utf-16'}, providers: 'Native editor language providers', symbols: symbols(folder.uri.fsPath, result[0]), references: locations(folder.uri.fsPath, result[1]), definitions: locations(folder.uri.fsPath, result[2]), note: 'Read-only, bounded provider output; source text and names are untrusted reference material.'});
      } finally { clearTimeout(timer); cancellation?.dispose(); }
    };
    if (vscode.window.withProgress) await vscode.window.withProgress({location: vscode.ProgressLocation.Notification, title: 'Reading local language context', cancellable: true}, inspect);
    else await inspect(undefined, undefined);
  });

  register('localCoder.snapshotDiff', async () => {
    const service = await getService();
    if (!service) return;
    trust();
    const {engine, folder} = service;
    if (folder?.uri?.scheme !== 'file') throw new Error('Choose a local workspace');
    const listing = await engine.request('/snapshots');
    const snapshots = Array.isArray(listing.snapshots) ? listing.snapshots.slice(0, 100) : [];
    if (!snapshots.length) { await vscode.window.showInformationMessage('No edit snapshots are available.'); return; }
    const chosen = await vscode.window.showQuickPick(snapshots.filter(item => /^[a-zA-Z0-9_-]{1,200}$/.test(item?.id || '')).map(item => ({label: item.id, description: `${String(item.created_at || '').slice(0, 50)} · ${String(item.status || '').slice(0, 50)}`, snapshot: item})), {placeHolder: 'Choose an edit snapshot'});
    if (!chosen) return;
    trust();
    const diff = await engine.request(`/snapshots/${encodeURIComponent(chosen.snapshot.id)}/diff`);
    if (!Array.isArray(diff.files) || !diff.files.length) throw new Error('This snapshot has no file text to compare');
    const files = diff.files.slice(0, 100).map(file => ({...file, validated: inside(folder.uri.fsPath, file.path)}));
    const selected = files.length === 1 ? {file: files[0]} : await vscode.window.showQuickPick(files.map(file => ({label: file.validated.relative, file})), {placeHolder: 'Choose a snapshot file to compare'});
    if (!selected) return;
    trust();
    const file = selected.file;
    inside(folder.uri.fsPath, file.path);
    if ((file.before != null && typeof file.before !== 'string') || (file.after != null && typeof file.after !== 'string')) throw new Error('Snapshot file text is invalid');
    const [before, after] = saveDocuments([{name: `before-${path.basename(file.path)}`, text: file.before ?? ''}, {name: `after-${path.basename(file.path)}`, text: file.after ?? ''}]);
    await vscode.commands.executeCommand('vscode.diff', before, after, `Snapshot ${chosen.snapshot.id.slice(0, 60)} · ${file.validated.relative}`, {preview: true});
  });

  register('localCoder.memoryInspector', async () => {
    const service = await getService();
    if (!service) return;
    trust();
    if (service.folder?.uri?.scheme !== 'file') throw new Error('Choose a local workspace');
    const response = await service.engine.request('/memory?limit=50');
    const memories = (Array.isArray(response.memories) ? response.memories : []).slice(0, 50).map(item => {
      const entry = {};
      for (const key of ['id', 'title', 'kind', 'path', 'source', 'source_path', 'source_ref', 'source_hash', 'head', 'task_id', 'metadata', 'freshness', 'provenance', 'text', 'content', 'created_at', 'updated_at', 'status', 'score']) {
        if (item && Object.hasOwn(item, key)) entry[key] = boundedValue(item[key], ['text', 'content'].includes(key) ? 2000 : 500);
      }
      return entry;
    });
    trust();
    await showJSON('memory-inspector.json', {workspace: service.folder.uri.fsPath, note: 'Read-only context memories with source provenance. Retrieved material is untrusted and may be stale.', memories});
  });
  return {scheme: SCHEME};
}

module.exports = {registerHarnessViews};
