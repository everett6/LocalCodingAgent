import { useEffect, useId, useMemo, useRef, useState } from 'react'
import type { KeyboardEvent } from 'react'
import { CornerDownLeft, Search } from 'lucide-react'
import { filterCommands, nextCommandIndex } from '../workspaceCommands'
import type { Command } from '../workspaceCommands'

export type { Command } from '../workspaceCommands'

type CommandPaletteProps = {
  commands: Command[]
  onClose: () => void
  title?: string
  placeholder?: string
}

export function CommandPalette({ commands, onClose, title = 'Search workspace', placeholder = 'Search commands, files and sessions…' }: CommandPaletteProps) {
  const dialog = useRef<HTMLDialogElement>(null)
  const input = useRef<HTMLInputElement>(null)
  const activeOption = useRef<HTMLDivElement>(null)
  const closing = useRef(false)
  const [query, setQuery] = useState('')
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const id = useId()
  const groups = useMemo(() => {
    const grouped = new Map<string, Command[]>()
    for (const command of filterCommands(commands, query)) {
      const items = grouped.get(command.group) ?? []
      items.push(command)
      grouped.set(command.group, items)
    }
    return Array.from(grouped, ([name, items]) => ({ name, items }))
  }, [commands, query])
  const visible = groups.flatMap(group => group.items)
  const preferredIndex = visible.findIndex(command => command.id === selectedId && !command.disabled)
  const selectedIndex = preferredIndex >= 0 ? preferredIndex : nextCommandIndex(visible, -1, 1)
  const selected = selectedIndex >= 0 ? visible[selectedIndex] : undefined
  const optionId = (index: number) => `${id}-command-${index}`

  useEffect(() => {
    const element = dialog.current
    const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null
    if (!element) return
    if (!element.open) element.showModal()
    input.current?.focus()
    return () => {
      if (element.open) element.close()
      const current = element.ownerDocument.activeElement
      // A chosen command may deliberately focus a new input or dialog. Restore
      // only when focus still belongs to this palette or its original opener.
      if (opener?.isConnected && (current === element.ownerDocument.body || current === opener || (current && element.contains(current)))) opener.focus()
    }
  }, [])

  useEffect(() => {
    activeOption.current?.scrollIntoView({ block: 'nearest' })
  }, [selected?.id, query])

  function close() {
    if (closing.current) return
    closing.current = true
    dialog.current?.close()
    onClose()
  }

  function execute(command: Command) {
    if (command.disabled || closing.current) return
    close()
    command.run()
  }

  function navigate(event: KeyboardEvent<HTMLInputElement>) {
    if (event.nativeEvent.isComposing || event.altKey || event.ctrlKey || event.metaKey) return
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault()
      const index = nextCommandIndex(visible, selectedIndex, event.key === 'ArrowDown' ? 1 : -1)
      setSelectedId(index >= 0 ? visible[index].id : null)
    } else if (event.key === 'Enter') {
      event.preventDefault()
      if (selected) execute(selected)
    }
  }

  let index = 0
  return <dialog ref={dialog} className="cmd-palette" aria-labelledby={`${id}-title`} aria-describedby={`${id}-hint`}
    onCancel={event => { event.preventDefault(); close() }}
    onClick={event => {
      if (event.target !== event.currentTarget) return
      const bounds = event.currentTarget.getBoundingClientRect()
      if (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom) close()
    }}>
    <header className="cmd-header">
      <h2 id={`${id}-title`}>{title}</h2>
      <button type="button" aria-label="Close command palette" onClick={close}><kbd>Esc</kbd></button>
    </header>
    <div className="cmd-search">
      <Search size={19} aria-hidden="true" />
      <input ref={input} id={`${id}-search`} role="combobox" type="text" autoFocus autoComplete="off" spellCheck={false}
        aria-label="Search" aria-expanded="true" aria-autocomplete="list" aria-controls={`${id}-results`}
        aria-activedescendant={selected ? optionId(selectedIndex) : undefined}
        placeholder={placeholder} value={query} onKeyDown={navigate}
        onChange={event => { setQuery(event.target.value); setSelectedId(null) }} />
    </div>
    <div id={`${id}-results`} className="cmd-groups" role="listbox" aria-label="Workspace commands">
      {groups.map((group, groupIndex) => <div key={group.name} role="group" aria-labelledby={`${id}-group-${groupIndex}`}>
        <h3 className="cmd-group-title" id={`${id}-group-${groupIndex}`}>{group.name}</h3>
        {group.items.map(command => {
          const commandIndex = index++
          const isActive = commandIndex === selectedIndex
          return <div key={command.id} id={optionId(commandIndex)} role="option" aria-selected={isActive}
            aria-disabled={command.disabled || undefined} className={`cmd-item${isActive ? ' is-active' : ''}`}
            ref={isActive ? activeOption : undefined}
            onPointerMove={() => { if (!command.disabled) setSelectedId(command.id) }}
            onMouseDown={event => event.preventDefault()} onClick={() => execute(command)}>
            <span className="cmd-item-copy"><span className="cmd-item-title">{command.title}</span>
              {command.description && <span className="cmd-item-description">{command.description}</span>}</span>
            {isActive && <CornerDownLeft size={15} aria-hidden="true" />}
          </div>
        })}
      </div>)}
      {!visible.length && <p className="cmd-empty" role="status">{query.trim() ? 'No matching commands. Try another search.' : 'No commands available.'}</p>}
    </div>
    <footer className="cmd-footer" id={`${id}-hint`}>
      <span><kbd>↑</kbd><kbd>↓</kbd> Navigate</span>
      <span><kbd>Enter</kbd> Open</span>
      <span><kbd>Esc</kbd> Close</span>
      <span>{visible.length} {visible.length === 1 ? 'command' : 'commands'}</span>
    </footer>
  </dialog>
}
