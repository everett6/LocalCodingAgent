import { useEffect, useRef } from 'react'
import './UndoDialog.css'

export function UndoDialog({ request, close, confirm }: { request: string; close: () => void; confirm: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null)
  useEffect(() => { dialog.current?.showModal() }, [])
  return <dialog ref={dialog} className="undo-dialog" aria-labelledby="undo-title" aria-describedby="undo-description" onCancel={close}>
    <h2 id="undo-title">Undo task changes?</h2>
    <p className="undo-task-name">{request}</p>
    <p id="undo-description">Restore the workspace to this task’s earlier checkpoint. Newer edits or checkpoints prevent restoration. External command effects cannot be undone.</p>
    <footer>
      <button type="button" autoFocus onClick={close}>Keep changes</button>
      <button type="button" className="primary" onClick={confirm}>Restore checkpoint</button>
    </footer>
  </dialog>
}
