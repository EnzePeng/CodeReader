import { useEffect, useMemo, useRef, useState } from 'react'
import { getJSON, projectSearchUrl } from '../api'
import { useDialogFocus } from '../hooks'
import { Evidence } from '../types'

export type QuickOpenMode = 'file' | 'symbol' | 'results'

interface Props {
  open: boolean
  mode: QuickOpenMode
  projectId: string | null
  title?: string
  presetResults?: Evidence[]
  onClose: () => void
  onSelect: (item: Evidence) => void
}

function normalizeEvidence(value: unknown): Evidence[] {
  const raw = Array.isArray(value)
    ? value
    : Array.isArray((value as any)?.items) ? (value as any).items : []
  return raw.filter((item: any) => item && typeof item.path === 'string').map((item: any) => ({
    ...item,
    path: item.path,
    start_line: Number(item.start_line) || 1,
    end_line: Number(item.end_line) || Number(item.start_line) || 1,
  }))
}

export default function QuickOpen({
  open, mode, projectId, title, presetResults = [], onClose, onSelect,
}: Props) {
  const dialogRef = useRef<HTMLDivElement>(null)
  const [query, setQuery] = useState('')
  const [items, setItems] = useState<Evidence[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [active, setActive] = useState(0)
  useDialogFocus(open, dialogRef, onClose)

  useEffect(() => {
    if (!open) return
    setQuery('')
    setActive(0)
    setError('')
    setItems(mode === 'results' ? presetResults : [])
  }, [open, mode, presetResults])

  useEffect(() => {
    if (!open || mode === 'results' || !projectId) return
    const ctrl = new AbortController()
    const timer = window.setTimeout(() => {
      setLoading(true)
      getJSON<unknown>(projectSearchUrl(projectId, query.trim(), mode, 60))
        .then(value => {
          if (!ctrl.signal.aborted) {
            setItems(normalizeEvidence(value))
            setActive(0)
            setError('')
          }
        })
        .catch(err => {
          if (!ctrl.signal.aborted) setError(String((err as Error).message || err))
        })
        .finally(() => { if (!ctrl.signal.aborted) setLoading(false) })
    }, query ? 140 : 0)
    return () => {
      window.clearTimeout(timer)
      ctrl.abort()
    }
  }, [open, mode, projectId, query])

  const heading = title ?? (mode === 'file' ? '快速打开文件' : mode === 'symbol' ? '转到符号' : '选择位置')
  const hint = mode === 'file' ? '输入文件名' : mode === 'symbol' ? '输入类、函数或方法名' : '筛选结果'
  const shown = useMemo(() => {
    if (mode !== 'results' || !query.trim()) return items
    const q = query.trim().toLocaleLowerCase()
    return items.filter(item => `${item.symbol ?? ''} ${item.path}`.toLocaleLowerCase().includes(q))
  }, [items, mode, query])

  if (!open) return null

  const choose = (item: Evidence) => {
    onSelect(item)
    onClose()
  }

  return (
    <div className="modal-mask quick-open-mask" onMouseDown={e => {
      if (e.target === e.currentTarget) onClose()
    }}>
      <div ref={dialogRef} className="modal quick-open" role="dialog" aria-modal="true"
        aria-labelledby="quick-open-title" tabIndex={-1}>
        <div className="modal-head">
          <span id="quick-open-title">{heading}</span>
          <span className="shortcut-hint">Esc</span>
        </div>
        <input
          className="quick-open-input"
          data-autofocus
          value={query}
          placeholder={hint}
          aria-label={hint}
          aria-controls="quick-open-results"
          aria-activedescendant={shown[active] ? `quick-result-${active}` : undefined}
          onChange={event => setQuery(event.target.value)}
          onKeyDown={event => {
            if (event.key === 'ArrowDown') {
              event.preventDefault()
              setActive(index => Math.min(index + 1, shown.length - 1))
            } else if (event.key === 'ArrowUp') {
              event.preventDefault()
              setActive(index => Math.max(0, index - 1))
            } else if (event.key === 'Enter' && shown[active]) {
              event.preventDefault()
              choose(shown[active])
            }
          }}
        />
        <div id="quick-open-results" className="quick-open-results" role="listbox"
          aria-label="搜索结果">
          {loading && <div className="quick-open-state" role="status">正在搜索…</div>}
          {error && <div className="quick-open-state err" role="alert">{error}</div>}
          {!loading && !error && shown.length === 0 && (
            <div className="quick-open-state">没有匹配结果</div>
          )}
          {shown.map((item, index) => (
            <button
              id={`quick-result-${index}`}
              key={`${item.path}:${item.start_line}:${item.symbol ?? ''}:${index}`}
              className={`quick-open-item${index === active ? ' active' : ''}`}
              role="option"
              aria-selected={index === active}
              onMouseEnter={() => setActive(index)}
              onClick={() => choose(item)}
            >
              <span className="quick-open-primary">{item.symbol || item.path.split(/[\\/]/).pop()}</span>
              <span className="quick-open-secondary">{item.path}{item.start_line > 1 ? `:${item.start_line}` : ''}</span>
              {item.relation && <span className="chip">{item.relation}</span>}
            </button>
          ))}
        </div>
        <div className="quick-open-foot">
          <span>↑↓ 选择</span><span>Enter 打开</span>
        </div>
      </div>
    </div>
  )
}
