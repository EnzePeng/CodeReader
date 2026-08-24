import { useEffect, useState } from 'react'
import type { LineRange } from '../types'

export interface SelectionActionsProps {
  selection: LineRange | null
  relativePath: string
  onAsk: () => void
  onCopyCode: () => void | Promise<void>
  onDismiss: () => void
  status?: string | null
}

export default function SelectionActions({
  selection,
  relativePath,
  onAsk,
  onCopyCode,
  onDismiss,
  status,
}: SelectionActionsProps) {
  const [localStatus, setLocalStatus] = useState('')

  useEffect(() => {
    if (!selection) return
    const handleEscape = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return
      event.preventDefault()
      onDismiss()
    }
    window.addEventListener('keydown', handleEscape)
    return () => window.removeEventListener('keydown', handleEscape)
  }, [selection, onDismiss])

  useEffect(() => {
    setLocalStatus('')
  }, [relativePath, selection?.start, selection?.end])

  useEffect(() => {
    if (!localStatus) return
    const timer = window.setTimeout(() => setLocalStatus(''), 2400)
    return () => window.clearTimeout(timer)
  }, [localStatus])

  if (!selection) return null

  const start = Math.min(selection.start, selection.end)
  const end = Math.max(selection.start, selection.end)
  const rangeLabel = start === end ? `第 ${start} 行` : `第 ${start}–${end} 行`
  const location = start === end
    ? `${relativePath}:${start}`
    : `${relativePath}:${start}-${end}`
  const feedback = status || localStatus

  const handleAsk = () => {
    setLocalStatus('')
    onAsk()
  }

  const handleCopyCode = async () => {
    try {
      await onCopyCode()
      setLocalStatus('代码已复制')
    } catch {
      setLocalStatus('复制代码失败，请重试')
    }
  }

  const handleCopyLocation = async () => {
    if (!navigator.clipboard) {
      setLocalStatus('当前环境不支持复制')
      return
    }
    try {
      await navigator.clipboard.writeText(location)
      setLocalStatus('位置已复制')
    } catch {
      setLocalStatus('复制位置失败，请重试')
    }
  }

  return (
    <div
      className="selection-actions"
      role="toolbar"
      aria-label={`选区快捷操作，${rangeLabel}`}
      aria-keyshortcuts="Escape"
    >
      <span className="selection-actions-range" title={location}>{rangeLabel}</span>
      <button
        type="button"
        className="btn-sm selection-action selection-action-ask"
        onClick={handleAsk}
        aria-label={`就此追问，${rangeLabel}`}
      >
        就此追问
      </button>
      <button
        type="button"
        className="btn-sm selection-action selection-action-copy-code"
        onClick={() => { void handleCopyCode() }}
        aria-label={`复制代码，${rangeLabel}`}
      >
        复制代码
      </button>
      <button
        type="button"
        className="btn-sm selection-action selection-action-copy-location"
        onClick={() => { void handleCopyLocation() }}
        aria-label={`复制位置 ${location}`}
        disabled={!relativePath}
      >
        复制位置
      </button>
      {feedback && (
        <span className="selection-actions-status" role="status" aria-live="polite">
          {feedback}
        </span>
      )}
    </div>
  )
}
