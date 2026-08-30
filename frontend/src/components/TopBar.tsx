import { useEffect, useRef, useState } from 'react'
import { getJSON, postJSON, encodePath } from '../api'
import { useDialogFocus } from '../hooks'
import { BrowseResult, Health } from '../types'
import ModelSettingsDialog from './ModelSettingsDialog'

interface ModelItem { name: string; size_gb: number | null }

interface Props {
  health: Health | null
  projectRoot: string
  onOpenProject: (path: string) => Promise<void>
  onRefreshHealth: () => Promise<Health | null>
}

function parentOf(path: string): string | null {
  const trimmed = path.replace(/[\\/]+$/, '')
  const idx = trimmed.lastIndexOf('\\')
  if (idx <= 1) {
    // "E:" 或更短 -> 返回盘符根 or 无上级
    if (/^[A-Za-z]:$/.test(trimmed)) return null
    return trimmed.slice(0, 2) + '\\'
  }
  return trimmed.slice(0, idx) || null
}

function FolderPicker({ onSelect, onClose }: { onSelect: (p: string) => void; onClose: () => void }) {
  const [current, setCurrent] = useState<string | null>(null)
  const [drives, setDrives] = useState<string[]>([])
  const [dirs, setDirs] = useState<string[]>([])
  const [err, setErr] = useState('')
  const [loading, setLoading] = useState(false)
  const dialogRef = useRef<HTMLDivElement>(null)
  const browseSeq = useRef(0)
  useDialogFocus(true, dialogRef, onClose)

  useEffect(() => {
    getJSON<{ drives: string[] }>('/api/drives')
      .then(r => setDrives(r.drives))
      .catch(e => setErr(String((e as Error).message || e)))
  }, [])

  useEffect(() => {
    const seq = ++browseSeq.current
    if (!current) { setDirs([]); setLoading(false); return }
    setDirs([])
    setLoading(true)
    getJSON<BrowseResult>(`/api/browse?path=${encodePath(current)}`)
      .then(r => {
        if (seq !== browseSeq.current) return
        setDirs(r.dirs.map(d => d.name)); setErr('')
      })
      .catch(e => { if (seq === browseSeq.current) setErr(String(e.message || e)) })
      .finally(() => { if (seq === browseSeq.current) setLoading(false) })
  }, [current])

  const goUp = () => {
    if (!current) return
    setCurrent(parentOf(current))
  }

  return (
    <div className="modal-mask" onMouseDown={e => { if (e.target === e.currentTarget) onClose() }}>
      <div ref={dialogRef} className="modal" role="dialog" aria-modal="true"
        aria-labelledby="folder-picker-title" tabIndex={-1}>
        <div className="modal-head">
          <span id="folder-picker-title">选择项目目录</span>
          <button className="btn-ghost" onClick={onClose} aria-label="关闭目录选择器">×</button>
        </div>
        <div className="picker-path">
          <button className="btn-sm" onClick={goUp} disabled={!current}>上级</button>
          <span className="picker-current">{current || '请选择磁盘'}</span>
        </div>
        {err && <div className="picker-err" role="alert">{err}</div>}
        <div className="picker-list" role="list" aria-busy={loading}>
          {loading && <div className="side-empty" role="status">正在读取目录…</div>}
          {!current
            ? drives.map(d => (
              <button key={d} className="picker-item" role="listitem" onClick={() => setCurrent(d)}>
                <span className="chip chip-dir">盘</span>{d}
              </button>
            ))
            : dirs.map(d => (
              <button key={d} className="picker-item" role="listitem"
                onClick={() => setCurrent(current.endsWith('\\') ? current + d : current + '\\' + d)}>
                <span className="chip chip-dir">目录</span>{d}
              </button>
            ))}
          {current && dirs.length === 0 && !err && !loading && <div className="side-empty">没有子目录</div>}
        </div>
        <div className="modal-foot">
          <button className="btn-primary" disabled={!current}
            onClick={() => current && onSelect(current)}>
            选用当前目录
          </button>
        </div>
      </div>
    </div>
  )
}

export default function TopBar({ health, projectRoot, onOpenProject, onRefreshHealth }: Props) {
  const [input, setInput] = useState(projectRoot)
  const [recents, setRecents] = useState<{ path: string }[]>([])
  const [showPicker, setShowPicker] = useState(false)
  const [err, setErr] = useState('')
  const [models, setModels] = useState<ModelItem[]>([])
  const [switching, setSwitching] = useState(false)
  // 刚选中的目标模型：health 尚未反映新模型时，用它先行显示下拉框与加载态
  const [pendingModel, setPendingModel] = useState<string | null>(null)
  const [togglingThink, setTogglingThink] = useState(false)
  const [opening, setOpening] = useState(false)
  const [showModelSettings, setShowModelSettings] = useState(false)

  const toggleThinking = async () => {
    if (!health?.thinking?.supported || togglingThink) return
    setTogglingThink(true)
    try {
      await postJSON('/api/thinking', { enabled: !health.thinking.enabled })
      await onRefreshHealth()
    } catch (e) {
      setErr(String((e as Error).message || e))
    } finally {
      setTogglingThink(false)
    }
  }

  useEffect(() => {
    getJSON<{ current: string; models: ModelItem[] }>('/api/models')
      .then(r => setModels(r.models)).catch(() => undefined)
  }, [health?.model])

  // health 已反映目标模型后清除待定态，后续就绪/失败显示均以 health 为准
  useEffect(() => {
    if (pendingModel && health?.model === pendingModel) setPendingModel(null)
  }, [health?.model, pendingModel])

  const switchModel = async (name: string) => {
    if (!name || name === (pendingModel ?? health?.model) || switching) return
    setSwitching(true)
    setPendingModel(name)
    setErr('')
    try {
      await postJSON('/api/models/switch', { name })
      // 切换已受理，立即刷新 health，让界面马上显示新模型（加载中）
      await onRefreshHealth()
    } catch (e) {
      // 切换失败：回退为当前模型并提示错误
      setPendingModel(null)
      setErr(String((e as Error).message || e))
    } finally {
      setSwitching(false)
    }
  }

  useEffect(() => { setInput(projectRoot) }, [projectRoot])
  useEffect(() => {
    try {
      const value = JSON.parse(localStorage.getItem('cr_recent_projects') || '[]')
      setRecents(Array.isArray(value)
        ? value.filter(item => item && typeof item.path === 'string').slice(0, 10)
        : [])
    } catch { setRecents([]) }
  }, [])

  const tryOpen = async (path: string) => {
    const p = path.trim().replace(/["']/g, '')
    if (!p) return
    setOpening(true)
    try {
      await onOpenProject(p)
      setRecents(previous => {
        const next = [{ path: p }, ...previous.filter(item => item.path !== p)].slice(0, 10)
        try { localStorage.setItem('cr_recent_projects', JSON.stringify(next)) } catch { /* optional */ }
        return next
      })
      setErr('')
    } catch (e) {
      setErr(String((e as Error).message || e))
    } finally {
      setOpening(false)
    }
  }

  const st = health?.llama
  // 刚发起切换、health 还未跟上时，优先按目标模型显示加载态
  const pendingSwitch = !!pendingModel && pendingModel !== health?.model
  const loadingModel = pendingModel ?? health?.model
  const statusClass = !health
    ? 'busy'
    : pendingSwitch
      ? 'busy'
      : st?.ready ? 'ok' : st?.phase === 'error' ? 'err' : 'busy'
  const statusText = !health
    ? '正在连接后端…'
    : pendingSwitch
      ? `模型加载中… · ${pendingModel}`
      : st?.ready
        ? `模型就绪 · ${health.model}`
        : st?.phase === 'error'
          ? '模型启动失败'
          : loadingModel ? `模型加载中… · ${loadingModel}` : '模型加载中…'

  return (
    <header className="topbar">
      <div className="brand">
        <span className="brand-mark">CR</span>
        CodeReader
        <span className="brand-sub">离线代码解读</span>
      </div>
      <div className="proj-open">
        <input
          id="project-path-input"
          value={input}
          placeholder="输入项目目录，如 D:\work\my-project"
          onChange={e => setInput(e.target.value)}
          onKeyDown={e => { if (e.key === 'Enter') tryOpen(input) }}
          disabled={opening}
        />
        <button className="btn-sm" onClick={() => setShowPicker(true)} disabled={opening}>浏览…</button>
        <button className="btn-primary" onClick={() => tryOpen(input)} disabled={opening}>
          {opening ? '打开中…' : '打开'}
        </button>
        {recents.length > 0 && (
          <select
            className="recents"
            value=""
            onChange={e => { if (e.target.value) tryOpen(e.target.value) }}
          >
            <option value="">最近打开</option>
            {recents.map(r => <option key={r.path} value={r.path}>{r.path}</option>)}
          </select>
        )}
        {err && <span className="topbar-err" role="alert">{err}</span>}
      </div>
      {models.length > 1 && (
        <select
          className="recents model-select"
          value={pendingModel ?? health?.model ?? ''}
          disabled={switching || (!!health && !st?.ready && st?.phase !== 'error')}
          onChange={e => switchModel(e.target.value)}
          title="切换模型（约需 10~30 秒重新加载，已生成的解读按模型分别缓存）"
        >
          {models.map(m => (
            <option key={m.name} value={m.name}>
              {m.name}{m.size_gb ? ` · ${m.size_gb}GB` : ''}
            </option>
          ))}
        </select>
      )}
      {health?.thinking?.supported && (
        <button
          className={`think-toggle${health.thinking.enabled ? ' on' : ''}`}
          disabled={togglingThink || switching}
          onClick={toggleThinking}
          title={health.thinking.enabled
            ? '思考模式已开启：模型先深度推理再作答，解读更透彻，但每段约需 1~3 分钟。点击关闭。'
            : '思考模式已关闭（秒级响应）。开启后模型先深度推理再作答，解读更透彻但每段约需 1~3 分钟；开关前后的结果分别缓存，互不覆盖。'}
        >
          <span className="think-dot" />思考{health.thinking.enabled ? '开' : '关'}
        </button>
      )}
      <button className="model-settings-button" onClick={() => setShowModelSettings(true)}
        aria-label="打开模型参数设置" title="模型参数与硬件推荐">
        <span aria-hidden="true">⚙</span><span className="model-settings-button-label">参数</span>
      </button>
      <div className={`status-pill ${statusClass}`} title={st?.detail || ''}>
        <span className="dot" />{statusText}
      </div>
      {showPicker && (
        <FolderPicker
          onClose={() => setShowPicker(false)}
          onSelect={p => { setShowPicker(false); setInput(p); tryOpen(p) }}
        />
      )}
      {showModelSettings && (
        <ModelSettingsDialog
          modelReady={!!st?.ready}
          onClose={() => setShowModelSettings(false)}
          onApplied={onRefreshHealth}
        />
      )}
    </header>
  )
}
