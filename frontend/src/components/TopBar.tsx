import { useEffect, useState } from 'react'
import { getJSON, postJSON, encodePath } from '../api'
import { BrowseResult, Health } from '../types'

interface ModelItem { name: string; size_gb: number | null }

interface Props {
  health: Health | null
  projectRoot: string
  onOpenProject: (path: string) => void
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

  useEffect(() => {
    getJSON<{ drives: string[] }>('/api/drives').then(r => setDrives(r.drives)).catch(() => undefined)
  }, [])

  useEffect(() => {
    if (!current) { setDirs([]); return }
    getJSON<BrowseResult>(`/api/browse?path=${encodePath(current)}`)
      .then(r => { setDirs(r.dirs.map(d => d.name)); setErr('') })
      .catch(e => setErr(String(e.message || e)))
  }, [current])

  const goUp = () => {
    if (!current) return
    setCurrent(parentOf(current))
  }

  return (
    <div className="modal-mask" onClick={onClose}>
      <div className="modal" onClick={e => e.stopPropagation()}>
        <div className="modal-head">
          <span>选择项目目录</span>
          <button className="btn-ghost" onClick={onClose}>×</button>
        </div>
        <div className="picker-path">
          <button className="btn-sm" onClick={goUp} disabled={!current}>上级</button>
          <span className="picker-current">{current || '请选择磁盘'}</span>
        </div>
        {err && <div className="picker-err">{err}</div>}
        <div className="picker-list">
          {!current
            ? drives.map(d => (
              <div key={d} className="picker-item" onDoubleClick={() => setCurrent(d)} onClick={() => setCurrent(d)}>
                <span className="chip chip-dir">盘</span>{d}
              </div>
            ))
            : dirs.map(d => (
              <div key={d} className="picker-item"
                onClick={() => setCurrent(current.endsWith('\\') ? current + d : current + '\\' + d)}>
                <span className="chip chip-dir">目录</span>{d}
              </div>
            ))}
          {current && dirs.length === 0 && !err && <div className="side-empty">没有子目录</div>}
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
  useEffect(() => { loadRecents() }, [projectRoot])

  const loadRecents = () => {
    getJSON<{ recents: { path: string }[] }>('/api/recents')
      .then(r => setRecents(r.recents)).catch(() => undefined)
  }

  const tryOpen = async (path: string) => {
    const p = path.trim().replace(/["']/g, '')
    if (!p) return
    try {
      await getJSON(`/api/browse?path=${encodePath(p)}`)
      setErr('')
      onOpenProject(p)
    } catch (e) {
      setErr(String((e as Error).message || e))
    }
  }

  const st = health?.llama
  // 刚发起切换、health 还未跟上时，优先按目标模型显示加载态
  const pendingSwitch = !!pendingModel && pendingModel !== health?.model
  const loadingModel = pendingModel ?? health?.model
  const statusClass = !health
    ? 'err'
    : pendingSwitch
      ? 'busy'
      : st?.ready ? 'ok' : st?.phase === 'error' ? 'err' : 'busy'
  const statusText = !health
    ? '后端未连接'
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
          value={input}
          placeholder="输入项目目录，如 D:\work\my-project"
          onChange={e => setInput(e.target.value)}
          onKeyDown={e => { if (e.key === 'Enter') tryOpen(input) }}
        />
        <button className="btn-sm" onClick={() => setShowPicker(true)}>浏览…</button>
        <button className="btn-primary" onClick={() => tryOpen(input)}>打开</button>
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
        {err && <span className="topbar-err">{err}</span>}
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
      <div className={`status-pill ${statusClass}`} title={st?.detail || ''}>
        <span className="dot" />{statusText}
      </div>
      {showPicker && (
        <FolderPicker
          onClose={() => setShowPicker(false)}
          onSelect={p => { setShowPicker(false); setInput(p); tryOpen(p) }}
        />
      )}
    </header>
  )
}
