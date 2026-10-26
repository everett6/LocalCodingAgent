export type Mode = 'coding' | 'cybersecurity' | 'bug_hunting' | 'red_team'
export interface Session { session_id: string; request: string; status?: string; phase: string; result?: string; error?: string; mode?: Mode; checkpoint?: string; engagement_id?: string }
export interface AgentEvent { seq: number; source: string; event_type: string; message: string; data: Record<string, unknown> }
export interface Provider { name?: string; backend: string; model_id: string; base_url: string; profile: string; api_key_env?: string; key_set?: boolean; max_tokens?: number; context_length?: number; strategy_attempt_limit?: number }
export interface Workspace { branch: string; files: { path: string; additions: number; deletions: number }[]; total_additions: number; total_deletions: number }
declare global { interface Window { localCoder?: { request: (path: string, method: string, body?: unknown) => Promise<{ status: number; data: unknown }>; chooseInferenceFile?: (purpose: 'runtime' | 'model' | 'draft') => Promise<string | null>; chooseProject: () => Promise<void>; openIDE: () => Promise<void> } } }
let token = ''
export function setToken(value: string) { token = value }
export async function api<T>(path: string, method = 'GET', body?: unknown): Promise<T> {
  let status: number, data: unknown
  if (window.localCoder) { const result = await window.localCoder.request(path, method, body); status = result.status; data = result.data }
  else { const result = await fetch(path, { method, headers: { ...(body ? { 'Content-Type': 'application/json' } : {}), ...(token ? { Authorization: `Bearer ${token}` } : {}) }, body: body ? JSON.stringify(body) : undefined }); status = result.status; data = await result.json() }
  if (status >= 400) throw new Error((data as { error?: string }).error || `Request failed (${status})`)
  return data as T
}
export const running = (session: Session) => ['queued', 'running', 'cancelling'].includes(session.status || '')
export const presets = {
  routerDeepseek: { label: 'DeepSeek V4.1 Flash 1017 · OpenRouter', backend: 'openai_compatible', model_id: 'deepseek/deepseek-v4.1-flash-1017', base_url: 'https://openrouter.ai/api/v1', profile: 'openrouter_deepseek', api_key_env: 'OPENROUTER_API_KEY', max_tokens: 16384, context_length: 1048576 },
  routerGLM: { label: 'GLM 5.3 · OpenRouter', backend: 'openai_compatible', model_id: 'z-ai/glm-5.3', base_url: 'https://openrouter.ai/api/v1', profile: 'openrouter_glm', api_key_env: 'OPENROUTER_API_KEY', max_tokens: 16384, context_length: 1048576 },
  routerKimi: { label: 'Kimi K3.1 Pro · OpenRouter', backend: 'openai_compatible', model_id: 'moonshotai/kimi-k3.1-pro', base_url: 'https://openrouter.ai/api/v1', profile: 'openrouter_kimi', api_key_env: 'OPENROUTER_API_KEY', max_tokens: 16384, context_length: 1048576 },
  glm: { label: 'GLM 5.3 · Z.ai Coding Plan', backend: 'openai_compatible', model_id: 'glm-5.3', base_url: 'https://api.z.ai/api/coding/paas/v4', profile: 'glm53', api_key_env: 'ZAI_API_KEY', max_tokens: 16384 },
  qwen: { label: 'Qwen 3.8 · Model Studio', backend: 'openai_compatible', model_id: 'qwen3.8-max', base_url: 'https://dashscope-intl.aliyuncs.com/compatible-mode/v1', profile: 'qwen38', api_key_env: 'DASHSCOPE_API_KEY', max_tokens: 16384 },
  anthropic: { label: 'Anthropic', backend: 'anthropic', model_id: '', base_url: 'https://api.anthropic.com/v1', profile: 'generic', api_key_env: 'ANTHROPIC_API_KEY', max_tokens: 8192 },
  openai: { label: 'OpenAI-compatible', backend: 'openai_compatible', model_id: '', base_url: 'https://api.openai.com/v1', profile: 'generic', api_key_env: 'OPENAI_API_KEY', max_tokens: 4096 },
  local: { label: 'Local OpenAI-compatible server', backend: 'openai_compatible', model_id: '', base_url: 'http://localhost:8090/v1', profile: 'generic', api_key_env: '', max_tokens: 4096 },
  ollama: { label: 'Ollama', backend: 'ollama', model_id: '', base_url: 'http://localhost:11434', profile: 'generic', api_key_env: '', max_tokens: 4096 },
}
