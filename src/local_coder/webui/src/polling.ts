/** Schedule the next refresh after the previous one settles, including errors. */
export function startSerialPolling(poll: (signal: AbortSignal) => Promise<void>, intervalMs: number, onError: (cause: unknown) => void): () => void {
  const controller = new AbortController()
  let stopped = false
  let timer: ReturnType<typeof setTimeout> | undefined
  async function run() {
    try { await poll(controller.signal) }
    catch (cause) { if (!stopped) onError(cause) }
    finally { if (!stopped) timer = setTimeout(() => { void run() }, intervalMs) }
  }
  void run()
  return () => { stopped = true; controller.abort(); clearTimeout(timer) }
}
