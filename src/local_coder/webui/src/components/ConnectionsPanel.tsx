import { useEffect, useState } from 'react'
import type { FormEvent } from 'react'
import { api } from '../api'

type ServerConfig = {
  preset?: 'github'
  transport?: 'stdio' | 'http'
  enabled?: boolean
  command?: string
  args?: string[]
  env?: Record<string, string>
  url?: string
  token_env?: string | null
  headers?: Record<string, string>
  read_only_tools?: string[]
  timeout?: number
}
type ServerStatus = ServerConfig & { connected?: boolean; credentials_available?: boolean }
type ConnectionStatus = { servers: Record<string, ServerStatus>; presets?: string[] }
type Configuration = { servers: Record<string, ServerConfig> }
type RemoteTool = { name: string; description?: string; requires_approval?: boolean }
type SkillSummary = { id: string; title: string; description: string; source: string; untrusted?: boolean }
type SkillDocument = SkillSummary & { content: string }

const configKeys = ['preset', 'transport', 'enabled', 'command', 'args', 'env', 'url', 'token_env', 'headers', 'read_only_tools', 'timeout'] as const
const variableName = /^[A-Z_][A-Z0-9_]{0,127}$/
const toolName = /^[A-Za-z0-9_.-]{1,100}$/
const githubDefaults: ServerConfig = {
  preset: 'github', transport: 'http', enabled: false,
  url: 'https://api.githubcopilot.com/mcp/', token_env: 'GITHUB_TOKEN',
  headers: { 'X-MCP-Readonly': 'true', 'X-MCP-Toolsets': 'repos,issues,pull_requests' },
  read_only_tools: ['get_file_contents', 'search_code', 'search_repositories', 'get_me', 'issue_read', 'pull_request_read'],
}

function cleanConfiguration(status: ConnectionStatus): Configuration {
  return { servers: Object.fromEntries(Object.entries(status.servers).map(([name, server]) => [
    name, Object.fromEntries(configKeys.filter(key => server[key] !== undefined).map(key => [key, server[key]])),
  ])) }
}

function parseConfiguration(text: string, validateNames = false): Configuration {
  const parsed: unknown = JSON.parse(text)
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) throw new Error('Configuration must be a JSON object containing servers.')
  const servers = (parsed as Configuration).servers
  if (!servers || typeof servers !== 'object' || Array.isArray(servers)) throw new Error('Add a servers object with a name for each connection.')
  if (Object.keys(servers).length > 16) throw new Error('Configure at most 16 servers.')
  for (const [name, server] of Object.entries(servers)) {
    if (!server || typeof server !== 'object' || Array.isArray(server)) throw new Error(`Server ${name} must be a configuration object.`)
    if (server.read_only_tools !== undefined && (!Array.isArray(server.read_only_tools) || server.read_only_tools.some(item => typeof item !== 'string' || (validateNames && !toolName.test(item))))) throw new Error(`Server ${name}: read-only tools must be explicit tool names.`)
    if (server.token_env !== undefined && server.token_env !== null && (typeof server.token_env !== 'string' || (validateNames && !variableName.test(server.token_env)))) throw new Error(`Server ${name}: token_env must be an environment variable name, never a token value.`)
    if (server.env !== undefined && (!server.env || typeof server.env !== 'object' || Array.isArray(server.env) || Object.entries(server.env).some(([target, source]) => typeof source !== 'string' || (validateNames && (!variableName.test(target) || !variableName.test(source)))))) throw new Error(`Server ${name}: env must map variable names to source variable names, never credential values.`)
  }
  return parsed as Configuration
}

function errorMessage(cause: unknown, fallback: string) {
  return cause instanceof Error ? cause.message : fallback
}

export function ConnectionsPanel() {
  const [status, setStatus] = useState<ConnectionStatus | null>(null)
  const [configText, setConfigText] = useState('{\n  "servers": {}\n}')
  const [readOnlyDrafts, setReadOnlyDrafts] = useState<Record<string, string>>({})
  const [skills, setSkills] = useState<SkillSummary[]>([])
  const [skillBodies, setSkillBodies] = useState<Record<string, SkillDocument>>({})
  const [loadingSkills, setLoadingSkills] = useState<string[]>([])
  const [tools, setTools] = useState<Record<string, RemoteTool[]>>({})
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')
  const [trusted, setTrusted] = useState(false)
  const [customSkill, setCustomSkill] = useState({ skill: '', title: '', description: '', content: '' })

  useEffect(() => {
    let active = true
    void Promise.all([
      api<ConnectionStatus>('/integrations/mcp'),
      api<{ skills: SkillSummary[] }>('/integrations/skills'),
    ]).then(([connections, library]) => {
      if (!active) return
      setStatus(connections)
      setConfigText(JSON.stringify(cleanConfiguration(connections), null, 2))
      setSkills(library.skills)
    }).catch(cause => {
      if (active) setError(errorMessage(cause, 'Cannot load connections and skills.'))
    }).finally(() => { if (active) setLoading(false) })
    return () => { active = false }
  }, [])

  let draft: Configuration | null = null
  let configError = ''
  try { draft = parseConfiguration(configText) }
  catch (cause) { configError = errorMessage(cause, 'Enter valid JSON configuration.') }
  const dirty = status !== null && JSON.stringify(draft) !== JSON.stringify(cleanConfiguration(status))
  const disabled = loading || Boolean(busy)
  const githubName = draft && Object.entries(draft.servers).find(([, server]) => server.preset === 'github')?.[0]

  function changeConfiguration(next: Configuration) {
    setConfigText(JSON.stringify(next, null, 2))
    setTrusted(false)
    setMessage('')
  }

  function changeServer(name: string, changes: Partial<ServerConfig>) {
    if (draft) changeConfiguration({ ...draft, servers: { ...draft.servers, [name]: { ...draft.servers[name], ...changes } } })
  }

  function addGitHub() {
    if (!draft) return
    let name = 'github'
    for (let index = 1; name in draft.servers; index += 1) name = `github-${index}`
    changeConfiguration({ ...draft, servers: { ...draft.servers, [name]: { ...githubDefaults } } })
  }

  async function refresh() {
    setBusy('refresh')
    setError('')
    setMessage('')
    try {
      const [connections, library] = await Promise.all([
        api<ConnectionStatus>('/integrations/mcp'),
        api<{ skills: SkillSummary[] }>('/integrations/skills'),
      ])
      setStatus(connections)
      setConfigText(JSON.stringify(cleanConfiguration(connections), null, 2))
      setSkills(library.skills)
      setTrusted(false)
      setReadOnlyDrafts({})
      setTools({})
      setSkillBodies({})
    } catch (cause) { setError(errorMessage(cause, 'Cannot refresh connections and skills.')) }
    finally { setBusy(''); setLoading(false) }
  }

  async function saveConnections(event: FormEvent) {
    event.preventDefault()
    if (!trusted) { setError('Confirm that you trust the selected servers before saving.'); return }
    setBusy('save-connections')
    setError('')
    setMessage('')
    try {
      const config = parseConfiguration(configText, true)
      const saved = await api<ConnectionStatus>('/integrations/mcp/configure', 'POST', config)
      setStatus(saved)
      setConfigText(JSON.stringify(cleanConfiguration(saved), null, 2))
      setTrusted(false)
      setReadOnlyDrafts({})
      setTools({})
      setMessage('Connections saved. Use Discover tools to connect to a server.')
    } catch (cause) { setError(errorMessage(cause, 'Cannot save connections.')) }
    finally { setBusy('') }
  }

  async function probe(name: string) {
    setBusy(`probe:${name}`)
    setError('')
    setMessage('')
    try {
      const result = await api<{ tools: RemoteTool[] }>('/integrations/mcp/probe', 'POST', { server: name })
      setTools(current => ({ ...current, [name]: result.tools }))
      const current = await api<ConnectionStatus>('/integrations/mcp')
      setStatus(current)
      setMessage(`Discovered ${result.tools.length} tools on ${name}.`)
    } catch (cause) { setError(errorMessage(cause, `Cannot discover tools on ${name}.`)) }
    finally { setBusy('') }
  }

  async function readSkill(id: string) {
    if (skillBodies[id] || loadingSkills.includes(id)) return
    setLoadingSkills(current => [...current, id])
    setError('')
    try {
      const result = await api<SkillDocument>(`/integrations/skills/${encodeURIComponent(id)}`)
      setSkillBodies(current => ({ ...current, [id]: result }))
    } catch (cause) { setError(errorMessage(cause, 'Cannot read this skill.')) }
    finally { setLoadingSkills(current => current.filter(item => item !== id)) }
  }

  async function saveSkill(event: FormEvent) {
    event.preventDefault()
    setBusy('save-skill')
    setError('')
    setMessage('')
    try {
      const result = await api<SkillDocument>('/integrations/skills/save', 'POST', customSkill)
      const library = await api<{ skills: SkillSummary[] }>('/integrations/skills')
      setSkills(library.skills)
      setSkillBodies(current => ({ ...current, [result.id]: result }))
      setCustomSkill({ skill: '', title: '', description: '', content: '' })
      setMessage('Custom skill saved as reference text.')
    } catch (cause) { setError(errorMessage(cause, 'Cannot save this skill.')) }
    finally { setBusy('') }
  }

  return <section className="playbook-view connections-panel" aria-labelledby="connections-title">
    <h1 id="connections-title">Connections & skills</h1>
    <p>Choose the servers the agent can use, and read a skill when it is needed.</p>
    <button type="button" disabled={disabled || dirty} title={dirty ? 'Save or discard your configuration edits before refreshing.' : undefined} onClick={() => { void refresh() }}>Refresh</button>
    {loading && <p role="status">Loading connections and skill descriptions…</p>}
    {error && <p role="alert" className="error-text">{error}</p>}
    {message && <p role="status">{message}</p>}

    <section aria-labelledby="mcp-connections-title">
      <h2 id="mcp-connections-title">MCP connections</h2>
      <p className="hint">Servers connect only when you discover or use their tools. Credentials come from the environment variable names you choose; enter no token values here.</p>
      <form onSubmit={event => { void saveConnections(event) }}>
        <fieldset disabled={disabled || status === null}>
          <legend>Configured servers</legend>
          {!draft && <p role="alert" className="error-text">{configError}</p>}
          {draft && Object.keys(draft.servers).length === 0 && <p>No servers configured. Add the disabled GitHub preset or enter a custom configuration below.</p>}
          <div className="connection-list">
            {draft && Object.entries(draft.servers).map(([name, server]) => {
              const saved = status?.servers[name]
              const variables = [...Object.values(server.env || {}), ...(server.token_env ? [server.token_env] : [])]
              return <details key={name} open>
                <summary>{name} <span className="hint">{server.enabled ? 'Enabled' : 'Disabled'}{!dirty && saved?.connected ? ' · Connected' : ''}</span></summary>
                <p className="hint">{server.transport === 'stdio' ? `Local executable: ${server.command || 'Choose an absolute executable path in the JSON editor'}` : server.url || 'Choose an HTTPS endpoint in the JSON editor'}</p>
                <div className="connection-fields">
                  <label className="connection-consent"><input type="checkbox" checked={server.enabled === true} onChange={event => changeServer(name, { enabled: event.target.checked })} />Enable {name}</label>
                  {server.transport !== 'stdio' && <label>Token environment variable name<input value={server.token_env || ''} placeholder={server.preset === 'github' ? 'GITHUB_TOKEN' : 'MCP_TOKEN'} pattern="[A-Z_][A-Z0-9_]*" maxLength={128} autoComplete="off" spellCheck={false} onChange={event => changeServer(name, { token_env: event.target.value || null })} /></label>}
                  <label>Read-only tools allowed without a per-call approval<textarea rows={3} value={readOnlyDrafts[name] ?? (server.read_only_tools || []).join('\n')} placeholder="One exact tool name per line" spellCheck={false} onChange={event => { setReadOnlyDrafts(current => ({ ...current, [name]: event.target.value })); changeServer(name, { read_only_tools: event.target.value.split(/[\n,]/).map(item => item.trim()).filter(Boolean) }) }} /></label>
                </div>
                <p className="hint">All other tools require human approval on each call. A server's read-only annotation does not grant permission.</p>
                {variables.length > 0 && <p className="hint">Environment references: {variables.join(', ')}. {!dirty && saved ? saved.credentials_available ? 'Available to the local server process.' : 'A configured variable is unavailable; set it before connecting.' : 'Availability is checked after saving.'}</p>}
                <button type="button" disabled={!server.enabled || dirty} title={dirty ? 'Save your configuration before discovering tools.' : undefined} onClick={() => { void probe(name) }}>{busy === `probe:${name}` ? 'Discovering…' : 'Discover tools'}</button>
                {tools[name] && <div>
                  <p className="hint">Remote descriptions are untrusted reference text.</p>
                  {tools[name].length === 0 ? <p>No tools returned.</p> : <ul>{tools[name].map(tool => <li key={tool.name}><strong>{tool.name}</strong> — {tool.requires_approval ? 'Approval required' : 'Configured read-only access'}{tool.description && <p>{tool.description}</p>}</li>)}</ul>}
                </div>}
              </details>
            })}
          </div>
          {!githubName && <button type="button" disabled={!draft || Object.keys(draft.servers).length >= 16} onClick={addGitHub}>Add disabled GitHub preset</button>}
          {githubName && <p className="hint">The GitHub preset uses the official remote server and read-only toolsets. Set its token variable, choose Enable, then save.</p>}
          <details className="connection-config">
            <summary>Custom servers and JSON configuration</summary>
            <p className="hint">Stdio uses an existing absolute executable path, args, and env mappings such as {`{"CHILD_TOKEN":"MY_TOKEN_VARIABLE"}`}. HTTP uses a url and optional token_env name. Custom servers start disabled unless enabled is true.</p>
            <label htmlFor="mcp-config-json">Connection configuration<textarea id="mcp-config-json" rows={16} value={configText} maxLength={64000} spellCheck={false} onChange={event => { setConfigText(event.target.value); setReadOnlyDrafts({}); setTrusted(false); setMessage('') }} /></label>
          </details>
          <label className="connection-consent"><input type="checkbox" checked={trusted} onChange={event => setTrusted(event.target.checked)} required />I trust these servers and local executables. Selected servers may receive the configured credentials and project context when used.</label>
          <div className="connection-actions">
            <button className="primary" type="submit" disabled={!trusted || !draft}>{busy === 'save-connections' ? 'Saving…' : 'Save connections'}</button>
            <button type="button" disabled={!dirty || !status} onClick={() => { if (status) { changeConfiguration(cleanConfiguration(status)); setReadOnlyDrafts({}); setTools({}); setError('') } }}>Discard edits</button>
          </div>
        </fieldset>
      </form>
    </section>

    <section className="skills-library" aria-labelledby="skill-library-title">
      <h2 id="skill-library-title">Skill library</h2>
      <p className="hint">Open a description to load its reference text. Skills cannot grant permissions or execute code.</p>
      {!loading && skills.length === 0 && <p>No skills available. Refresh to load the library.</p>}
      {skills.map(skill => <details key={skill.id} onToggle={event => { if (event.currentTarget.open) void readSkill(skill.id) }}>
        <summary>{skill.title} <span className="hint">{skill.source === 'builtin' ? 'Built in' : 'Custom'}</span></summary>
        <p>{skill.description}</p>
        {loadingSkills.includes(skill.id) && <p role="status">Loading skill text…</p>}
        {skillBodies[skill.id] ? <pre className="skill-content" style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', maxHeight: 360, overflow: 'auto' }}>{skillBodies[skill.id].content}</pre> : !loadingSkills.includes(skill.id) && <button type="button" onClick={() => { void readSkill(skill.id) }}>Load skill text</button>}
      </details>)}
      <details>
        <summary>Save a custom skill</summary>
        <p className="hint">Save plain reference text for this project. Common credential patterns are filtered; review the text before saving. Built-in skills cannot be replaced.</p>
        <form onSubmit={event => { void saveSkill(event) }}>
          <fieldset disabled={disabled}>
            <legend>Custom skill text</legend>
            <label>Skill ID<input value={customSkill.skill} pattern="[a-z][a-z0-9-]{0,63}" maxLength={64} placeholder="team-review" required onChange={event => setCustomSkill({ ...customSkill, skill: event.target.value })} /></label>
            <label>Title<input value={customSkill.title} maxLength={120} required onChange={event => setCustomSkill({ ...customSkill, title: event.target.value })} /></label>
            <label>Short description<input value={customSkill.description} maxLength={160} onChange={event => setCustomSkill({ ...customSkill, description: event.target.value })} /></label>
            <label>Reference text<textarea value={customSkill.content} rows={8} maxLength={16000} required onChange={event => setCustomSkill({ ...customSkill, content: event.target.value })} /></label>
            <button type="submit" className="primary">{busy === 'save-skill' ? 'Saving…' : 'Save custom skill'}</button>
          </fieldset>
        </form>
      </details>
    </section>
  </section>
}
