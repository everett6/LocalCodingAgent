export const modelName = (path: string) => path.split(/[\\/]/).filter(Boolean).pop() || 'No model selected'
export const modelSize = (bytes: number) => bytes >= 1073741824 ? `${(bytes / 1073741824).toFixed(2)} GiB` : `${(bytes / 1048576).toFixed(1)} MiB`
export const modelQuantization = (path: string) => modelName(path).match(/(?:^|[-_.])(IQ\d(?:_[A-Z0-9]+)*|Q\d(?:_[A-Z0-9]+)*|BF16|F16|F32)(?=[-_.]|$)/i)?.[1].toUpperCase() || null

/** Selection is a draft. Only host readiness establishes a loaded model. */
export function runtimePresentation(state: string, ready: boolean, loadedPath: string) {
  if (ready && state === 'running') return { label: 'Loaded', model: modelName(loadedPath), tone: 'ready' }
  if (state === 'starting') return { label: 'Loading model', model: modelName(loadedPath), tone: 'loading' }
  if (state === 'stopping') return { label: 'Unloading', model: modelName(loadedPath), tone: 'loading' }
  if (state === 'installing') return { label: 'Installing runtime', model: 'Preparing local inference', tone: 'loading' }
  if (state === 'error') return { label: 'Needs attention', model: 'Check runtime diagnostics', tone: 'error' }
  return { label: 'Not loaded', model: 'Choose a model from your library', tone: 'idle' }
}
