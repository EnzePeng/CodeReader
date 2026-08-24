import { lazy, MouseEvent as ReactMouseEvent, Suspense, useCallback, useEffect, useRef, useState } from 'react'
import TopBar from './components/TopBar'
import FileTree from './components/FileTree'
import Outline from './components/Outline'
import type { CodePaneApi } from './components/CodePane'
import ExplainPanel from './components/ExplainPanel'
import ChatDrawer from './components/ChatDrawer'
import QuickOpen, { QuickOpenMode } from './components/QuickOpen'
import SelectionActions from './components/SelectionActions'
import { createDeltaBatch } from './deltaBatch'
import { normalizeScope, resolveEventScope } from './scope'
import {
  getJSON, postJSON, projectExportUrl, projectFileUrl, projectStructureUrl, SSEEnvelope, streamJobSSE,
} from './api'
import { useViewportMode } from './hooks'
import {
  beginJob, cancelJob, failJob, idleJob, idleResource, JobState, loadResource,
  reduceJobEnvelope, rejectResource, resolveResource, ResourceState,
} from './state'
import {
  Evidence, ExplainMode, ExplainTarget, FileInfo, Health, IndexStatus, LineRange, ProjectSession,
  ScopeMode, SegState, Structure,
} from './types'

type ForceArg = 'none' | 'all' | string[]
type ExplainStatus = 'idle' | 'streaming' | 'done' | 'cancelled' | 'error'
type WorkspaceMode = 'code' | 'split' | 'explain'

const MODE_KEY = 'cr_explain_mode'
const SCOPE_KEY = 'cr_scope_mode'
const SIDEBAR_KEY = 'cr_sidebar_collapsed'
const WORKSPACE_MODE_KEY = 'cr_workspace_mode'
const PANEL_RATIO_KEY = 'cr_panel_ratio'
const CodePane = lazy(() => import('./components/CodePane'))

function storedPanelRatio(): number {
  const value = Number(localStorage.getItem(PANEL_RATIO_KEY))
  return Number.isFinite(value) ? Math.min(0.6, Math.max(0.25, value)) : 0.42
}

function storedWorkspaceMode(): WorkspaceMode {
  const value = localStorage.getItem(WORKSPACE_MODE_KEY)
  return value === 'code' || value === 'explain' ? value : 'split'
}

function messageOf(error: unknown): string {
  return String((error as Error)?.message || error || '发生未知错误')
}

function indexStatusTitle(status?: IndexStatus): string {
  if (!status) return '索引尚未开始'
  const languages = Object.entries(status.languages ?? {})
    .map(([name, count]) => `${name} ${count}`).join('、')
  const failed = Object.keys(status.parse_errors ?? {}).length
  const skipped = status.skipped_files?.length ?? 0
  const updated = status.updated_at
    ? new Date(status.updated_at * 1000).toLocaleString() : ''
  return [languages && `覆盖：${languages}`, `解析失败：${failed}`, `跳过：${skipped}`,
    updated && `更新时间：${updated}`].filter(Boolean).join('\n')
}

/** 所有项目内请求都只接受相对路径，避免意外退回旧的绝对路径协议。 */
function safeRelativePath(path: string): string {
  const normalized = path.replace(/\\/g, '/').replace(/^\.\//, '')
  if (!normalized || normalized.startsWith('/') || /^[A-Za-z]:\//.test(normalized)
    || normalized.split('/').includes('..')) {
    throw new Error(`服务返回了无效的项目相对路径：${path}`)
  }
  return normalized
}

function evidenceItems(payload: Record<string, unknown>): Evidence[] {
  const nested = payload.evidence && typeof payload.evidence === 'object'
    ? (payload.evidence as Record<string, unknown>).items : null
  const values = Array.isArray(payload.items) ? payload.items : Array.isArray(nested) ? nested : []
  return values.filter((item: any) => item && typeof item.path === 'string').map((item: any) => ({
    ...item,
    path: safeRelativePath(item.path),
    start_line: Number(item.start_line) || 1,
    end_line: Number(item.end_line) || Number(item.start_line) || 1,
  }))
}

function normalizeProject(value: any, requestedRoot: string): ProjectSession {
  if (!value || typeof value.project_id !== 'string' || !value.project_id) {
    throw new Error('项目服务未返回 project_id')
  }
  const trimmed = requestedRoot.replace(/[\\/]+$/, '')
  return {
    project_id: value.project_id,
    root: typeof value.root === 'string' ? value.root : requestedRoot,
    name: typeof value.name === 'string' ? value.name : trimmed.split(/[\\/]/).pop() || trimmed,
    index_status: value.index_status,
  }
}

export default function App() {
  const viewport = useViewportMode()
  const [health, setHealth] = useState<Health | null>(null)
  const [projectState, setProjectState] = useState<ResourceState<ProjectSession>>(() => idleResource())
  const [fileState, setFileState] = useState<ResourceState<FileInfo>>(() => idleResource())
  const [structureState, setStructureState] = useState<ResourceState<Structure>>(() => idleResource())
  const [sideTab, setSideTab] = useState<'files' | 'outline'>('files')
  const [sidebarOpen, setSidebarOpen] = useState(false)
  const [mobileView, setMobileView] = useState<'code' | 'explain'>('code')

  const [overview, setOverview] = useState<{ text: string; status: ExplainStatus }>({ text: '', status: 'idle' })
  const [segStates, setSegStates] = useState<Record<string, SegState>>({})
  const [segOrder, setSegOrder] = useState<string[]>([])
  const [explainJob, setExplainJob] = useState<JobState>(() => idleJob())
  const [evidenceByScope, setEvidenceByScope] = useState<Record<string, Evidence[]>>({})
  const [generatedConfig, setGeneratedConfig] = useState<{ model: string; thinking: boolean } | null>(null)
  const [exportAfterComplete, setExportAfterComplete] = useState(false)
  const [globalError, setGlobalError] = useState('')
  const [activeSeg, setActiveSeg] = useState<string | null>(null)
  const [selection, setSelection] = useState<LineRange | null>(null)
  const [chatOpen, setChatOpen] = useState(false)
  const [sidebarCollapsed, setSidebarCollapsed] = useState(
    () => localStorage.getItem(SIDEBAR_KEY) === 'true')
  const [workspaceMode, setWorkspaceMode] = useState<WorkspaceMode>(storedWorkspaceMode)
  const [panelRatio, setPanelRatio] = useState(storedPanelRatio)
  const [toast, setToast] = useState('')

  const [quickOpen, setQuickOpen] = useState<{
    open: boolean
    mode: QuickOpenMode
    title?: string
    results?: Evidence[]
  }>({ open: false, mode: 'file' })

  const [explainMode, setExplainMode] = useState<ExplainMode>(
    () => (localStorage.getItem(MODE_KEY) === 'detailed' ? 'detailed' : 'simple'))
  const [scopeMode, setScopeMode] = useState<ScopeMode>(
    () => (localStorage.getItem(SCOPE_KEY) === 'manual' ? 'manual' : 'auto'))

  const explainAbortRef = useRef<AbortController | null>(null)
  const explainRequestKeyRef = useRef(0)
  const explainJobRef = useRef(explainJob)
  explainJobRef.current = explainJob
  const codeApi = useRef<CodePaneApi | null>(null)
  const cursorRef = useRef({ line: 1, column: 1 })
  const segStatesRef = useRef(segStates)
  segStatesRef.current = segStates
  const projectRef = useRef(projectState.data)
  projectRef.current = projectState.data
  const fileRef = useRef(fileState.data)
  fileRef.current = fileState.data
  const explainModeRef = useRef(explainMode)
  explainModeRef.current = explainMode
  const scopeModeRef = useRef(scopeMode)
  scopeModeRef.current = scopeMode
  const fileLoadSeq = useRef(0)
  const projectLoadSeq = useRef(0)
  const pendingReveal = useRef<LineRange | null>(null)
  const historyRef = useRef<Evidence[]>([])
  const historyIndexRef = useRef(-1)
  const [historyVersion, setHistoryVersion] = useState(0)

  const project = projectState.data
  const fileInfo = fileState.data
  const structure = structureState.data
  const explaining = explainJob.phase === 'running'
  const ready = !!health?.llama.ready

  useEffect(() => {
    try {
      localStorage.setItem(SIDEBAR_KEY, String(sidebarCollapsed))
      localStorage.setItem(WORKSPACE_MODE_KEY, workspaceMode)
      localStorage.setItem(PANEL_RATIO_KEY, String(panelRatio))
    } catch { /* 布局偏好不是关键数据，存储失败时继续使用当前会话状态。 */ }
  }, [sidebarCollapsed, workspaceMode, panelRatio])

  useEffect(() => {
    if (!toast) return
    const timer = window.setTimeout(() => setToast(''), 2200)
    return () => window.clearTimeout(timer)
  }, [toast])

  const showToast = useCallback((message: string) => {
    setToast(message)
  }, [])

  // ---------- 模型状态轮询 ----------
  const healthTimer = useRef<number | undefined>(undefined)
  const healthSeq = useRef(0)
  const healthStopped = useRef(false)

  const refreshHealth = useCallback(async (): Promise<Health | null> => {
    const seq = ++healthSeq.current
    let result: Health | null = null
    try {
      result = await getJSON<Health>('/api/health')
    } catch {
      result = null
    }
    if (healthStopped.current || seq !== healthSeq.current) return result
    setHealth(result)
    window.clearTimeout(healthTimer.current)
    healthTimer.current = window.setTimeout(refreshHealth, result?.llama.ready ? 30000 : 3000)
    return result
  }, [])

  useEffect(() => {
    healthStopped.current = false
    void refreshHealth()
    return () => {
      healthStopped.current = true
      window.clearTimeout(healthTimer.current)
    }
  }, [refreshHealth])

  useEffect(() => {
    if (!project || project.index_status?.state !== 'building') return
    let stopped = false
    const poll = async () => {
      try {
        const response = await getJSON<{ index_status: IndexStatus }>(
          `/api/projects/${encodeURIComponent(project.project_id)}/index/status`)
        if (stopped) return
        setProjectState(previous => previous.data?.project_id === project.project_id
          ? { ...previous, data: { ...previous.data, index_status: response.index_status } }
          : previous)
        if (response.index_status.state === 'building') {
          window.setTimeout(poll, 1000)
        }
      } catch (error) {
        if (!stopped) setGlobalError(`索引状态获取失败：${messageOf(error)}`)
      }
    }
    const timer = window.setTimeout(poll, 250)
    return () => { stopped = true; window.clearTimeout(timer) }
  }, [project?.project_id, project?.index_status?.state])

  const finishStreaming = useCallback((status: 'done' | 'cancelled' | 'error', error?: string) => {
    setOverview(previous => previous.status === 'streaming'
      ? { ...previous, status }
      : previous)
    setSegStates(previous => {
      const next: Record<string, SegState> = {}
      for (const [id, value] of Object.entries(previous)) {
        next[id] = value.status === 'streaming'
          ? { ...value, status, error: status === 'error' ? error ?? '生成失败' : null }
          : value
      }
      return next
    })
  }, [])

  const cancelExplain = useCallback((reason = '已停止') => {
    explainAbortRef.current?.abort()
    explainAbortRef.current = null
    const cancelled = cancelJob(explainJobRef.current, reason)
    explainJobRef.current = cancelled
    setExplainJob(cancelled)
    finishStreaming('cancelled')
  }, [finishStreaming])

  // ---------- 解读任务 ----------
  const startExplain = useCallback((
    relativePath: string,
    force: ForceArg,
    targets?: ExplainTarget[],
    modeArg?: ExplainMode,
  ) => {
    const currentProject = projectRef.current
    if (!currentProject) return
    const safePath = safeRelativePath(relativePath)
    cancelExplain('已被新任务替换')
    const controller = new AbortController()
    explainAbortRef.current = controller
    const requestKey = ++explainRequestKeyRef.current
    const started = beginJob(explainJobRef.current, requestKey)
    explainJobRef.current = started
    setExplainJob(started)
    setGlobalError('')
    if (force === 'all') {
      setOverview({ text: '', status: 'idle' })
      setEvidenceByScope({})
      setSegStates(previous => Object.fromEntries(Object.entries(previous).map(([id, value]) => [
        id, { ...value, text: '', status: 'idle', error: null },
      ])))
    }

    const setScopeEvidence = (scope: string, items: Evidence[]) => {
      if (!scope || !items.length) return
      setEvidenceByScope(previous => ({ ...previous, [scope]: items }))
    }
    const deltaBatch = createDeltaBatch(chunks => {
      const overviewDelta = chunks.get('overview')
      if (overviewDelta) {
        setOverview(previous => ({ text: previous.text + overviewDelta, status: 'streaming' }))
      }
      if (chunks.size > (overviewDelta ? 1 : 0)) {
        setSegStates(previous => {
          const next = { ...previous }
          for (const [scope, text] of chunks) {
            if (scope === 'overview' || !next[scope]) continue
            next[scope] = { ...next[scope], text: next[scope].text + text, status: 'streaming', error: null }
          }
          return next
        })
      }
    })

    const handleEnvelope = (envelope: SSEEnvelope) => {
      const reduced = reduceJobEnvelope(explainJobRef.current, envelope, requestKey)
      if (!reduced.accepted) return
      explainJobRef.current = reduced.state
      setExplainJob(reduced.state)
      const payload = envelope.payload as Record<string, any>
      // 作用域优先取 payload.target（总览/段 id）；envelope.scope_id 恒为文件
      // 路径，只能兜底，否则流式文本会被记到错误作用域而静默丢失。
      const scope = resolveEventScope(envelope, payload)

      switch (envelope.type) {
        // 旧协议兼容事件。新协议只需 status/delta/evidence/complete/cancelled/error。
        case 'meta': {
          if (!Array.isArray(payload.segments)) break
          setSegStates(previous => {
            const next: Record<string, SegState> = {}
            for (const meta of payload.segments) {
              next[meta.id] = previous[meta.id]
                ? { ...previous[meta.id], meta }
                : { meta, text: '', status: 'idle', cached: false, mode: null }
            }
            return next
          })
          setSegOrder(payload.segments.map((meta: any) => meta.id))
          break
        }
        case 'overview_start':
          setOverview({ text: '', status: 'streaming' })
          break
        case 'overview_delta':
          setOverview(previous => ({ text: previous.text + String(payload.text ?? ''), status: 'streaming' }))
          break
        case 'overview_done':
          deltaBatch.flush()
          setOverview({ text: String(payload.text ?? ''), status: 'done' })
          break
        case 'segment_start': {
          const id = normalizeScope(payload.id || scope)
          setSegStates(previous => previous[id] ? {
            ...previous,
            [id]: { ...previous[id], text: '', status: 'streaming', error: null, mode: payload.mode ?? previous[id].mode },
          } : previous)
          break
        }
        case 'segment_delta': {
          const id = normalizeScope(payload.id || scope)
          setSegStates(previous => previous[id] ? {
            ...previous,
            [id]: {
              ...previous[id],
              text: previous[id].text + String(payload.text ?? ''),
              status: 'streaming',
              mode: payload.mode ?? previous[id].mode,
            },
          } : previous)
          break
        }
        case 'segment_done': {
          deltaBatch.flush()
          const id = normalizeScope(payload.id || scope)
          setSegStates(previous => previous[id] ? {
            ...previous,
            [id]: {
              ...previous[id], text: String(payload.text ?? ''), status: 'done',
              cached: !!payload.cached, mode: payload.mode ?? previous[id].mode, error: null,
            },
          } : previous)
          break
        }
        case 'status':
          break // reduceJobEnvelope 已统一写入 message。
        case 'delta': {
          const text = String(payload.text ?? '')
          if (scope && scope !== 'chat') deltaBatch.push(scope, text)
          break
        }
        case 'evidence':
          try {
            const evidenceScope = typeof payload.target === 'string'
              ? payload.target : (scope || 'overview')
            setScopeEvidence(evidenceScope, evidenceItems(payload))
          } catch (error) {
            setGlobalError(messageOf(error))
          }
          break
        case 'complete': {
          deltaBatch.flush()
          const result = payload.result && typeof payload.result === 'object'
            ? payload.result as Record<string, any> : payload
          if (typeof result.model === 'string') {
            setGeneratedConfig({ model: result.model, thinking: !!result.thinking })
          }
          const target = normalizeScope(result.target || scope)
          if (typeof result.text === 'string') {
            if (target === 'overview') setOverview({ text: result.text, status: 'done' })
            else if (target) setSegStates(previous => previous[target] ? {
              ...previous,
              [target]: { ...previous[target], text: result.text, status: 'done', cached: !!result.cached, mode: result.mode ?? previous[target].mode },
            } : previous)
          }
          if (typeof result.overview === 'string') setOverview({ text: result.overview, status: 'done' })
          if (Array.isArray(result.segments)) {
            setSegStates(previous => {
              const next = { ...previous }
              for (const item of result.segments) {
                if (next[item.id]) next[item.id] = {
                  ...next[item.id], text: String(item.text ?? next[item.id].text), status: 'done',
                  cached: !!item.cached, mode: item.mode ?? next[item.id].mode,
                }
              }
              return next
            })
          }
          try {
            const items = evidenceItems(result)
            if (items.length) setScopeEvidence(target || 'overview', items)
          } catch (error) { setGlobalError(messageOf(error)) }
          finishStreaming('done')
          break
        }
        case 'done':
          deltaBatch.flush()
          finishStreaming('done')
          break
        case 'cancelled':
          deltaBatch.flush()
          finishStreaming('cancelled')
          break
        case 'error': {
          deltaBatch.flush()
          const error = String(payload.message ?? '解读失败')
          setGlobalError(error)
          finishStreaming('error', error)
          break
        }
      }
    }

    streamJobSSE('/api/explain', {
      project_id: currentProject.project_id,
      relative_path: safePath,
      force,
      mode: modeArg ?? explainModeRef.current,
      targets: targets ?? null,
    }, handleEnvelope, controller.signal)
      .catch(error => {
        deltaBatch.flush()
        if ((error as Error).name === 'AbortError') return
        if (requestKey !== explainJobRef.current.requestKey) return
        const detail = messageOf(error)
        const failed = failJob(explainJobRef.current, detail)
        explainJobRef.current = failed
        setExplainJob(failed)
        setGlobalError(detail)
        finishStreaming('error', detail)
      })
      .finally(() => {
        deltaBatch.flush()
        if (requestKey !== explainJobRef.current.requestKey) return
        if (explainJobRef.current.phase === 'running') {
          const detail = '解读连接提前结束，请重试'
          const failed = failJob(explainJobRef.current, detail)
          explainJobRef.current = failed
          setExplainJob(failed)
          setGlobalError(detail)
          finishStreaming('error', detail)
        }
      })
  }, [cancelExplain, finishStreaming])

  const explainSegments = useCallback((targets: ExplainTarget[], force: ForceArg = 'none') => {
    if (fileRef.current) startExplain(fileRef.current.relative_path, force, targets)
  }, [startExplain])

  useEffect(() => {
    if (!exportAfterComplete) return
    if (explainJob.phase === 'error' || explainJob.phase === 'cancelled') {
      setExportAfterComplete(false)
      return
    }
    if (explainJob.phase !== 'complete' || !project || !fileInfo) return
    if (segOrder.some(id => segStates[id]?.status !== 'done')) return
    const link = document.createElement('a')
    link.href = projectExportUrl(project.project_id, fileInfo.relative_path)
    link.download = ''
    link.click()
    setExportAfterComplete(false)
  }, [exportAfterComplete, explainJob.phase, project, fileInfo, segOrder, segStates])

  const explainOneSeg = useCallback((id: string, mode: ExplainMode, force: boolean) => {
    explainSegments([{ id, mode }], force ? [id] : 'none')
  }, [explainSegments])

  const explainOverview = useCallback((force: boolean) => {
    const file = fileRef.current
    if (!file) return
    const forceArg: ForceArg = force ? ['overview'] : 'none'
    if (scopeModeRef.current === 'auto') startExplain(file.relative_path, forceArg)
    else startExplain(file.relative_path, forceArg, [])
  }, [startExplain])

  const changeExplainMode = useCallback((mode: ExplainMode) => {
    explainModeRef.current = mode
    setExplainMode(mode)
    localStorage.setItem(MODE_KEY, mode)
    if (scopeModeRef.current === 'auto' && fileRef.current) {
      startExplain(fileRef.current.relative_path, 'none', undefined, mode)
    }
  }, [startExplain])

  const changeScopeMode = useCallback((mode: ScopeMode) => {
    if (mode === scopeModeRef.current) return
    scopeModeRef.current = mode
    setScopeMode(mode)
    localStorage.setItem(SCOPE_KEY, mode)
    if (mode === 'manual') cancelExplain('已切换为手动选块')
    else if (fileRef.current) startExplain(fileRef.current.relative_path, 'none')
  }, [cancelExplain, startExplain])

  const resetWorkspace = useCallback(() => {
    cancelExplain('已切换项目')
    fileLoadSeq.current++
    setFileState(idleResource())
    setStructureState(idleResource())
    setOverview({ text: '', status: 'idle' })
    setSegStates({})
    setSegOrder([])
    setEvidenceByScope({})
    setGeneratedConfig(null)
    setExportAfterComplete(false)
    setSelection(null)
    setActiveSeg(null)
    setChatOpen(false)
    historyRef.current = []
    historyIndexRef.current = -1
    setHistoryVersion(value => value + 1)
  }, [cancelExplain])

  const openProject = useCallback(async (path: string) => {
    const seq = ++projectLoadSeq.current
    resetWorkspace()
    setProjectState(previous => loadResource(previous, true))
    setGlobalError('')
    try {
      const value = await postJSON<unknown>('/api/projects/open', { path })
      if (seq !== projectLoadSeq.current) return
      const session = normalizeProject(value, path)
      projectRef.current = session
      setProjectState(previous => resolveResource(previous, session))
      setSideTab('files')
      setSidebarOpen(viewport !== 'wide')
    } catch (error) {
      if (seq !== projectLoadSeq.current) return
      const detail = messageOf(error)
      setProjectState(previous => rejectResource(previous, detail))
      setGlobalError(detail)
      throw error
    }
  }, [resetWorkspace, viewport])

  const pushHistory = useCallback((entry: Evidence) => {
    const previous = historyRef.current.slice(0, historyIndexRef.current + 1)
    const last = previous[previous.length - 1]
    if (last && last.path === entry.path && last.start_line === entry.start_line) return
    historyRef.current = [...previous, entry]
    historyIndexRef.current = historyRef.current.length - 1
    setHistoryVersion(value => value + 1)
  }, [])

  // ---------- 打开文件：失败保留上一份成功内容 ----------
  const openFile = useCallback(async (
    relativePath: string,
    options: { reveal?: LineRange; recordHistory?: boolean } = {},
  ): Promise<boolean> => {
    const currentProject = projectRef.current
    if (!currentProject) return false
    let safePath: string
    try { safePath = safeRelativePath(relativePath) } catch (error) {
      setGlobalError(messageOf(error)); return false
    }
    const seq = ++fileLoadSeq.current
    cancelExplain('已切换文件')
    setFileState(previous => loadResource(previous))
    setStructureState(previous => loadResource(previous))
    setGlobalError('')
    try {
      const [rawFile, nextStructure] = await Promise.all([
        getJSON<any>(projectFileUrl(currentProject.project_id, safePath)),
        getJSON<Structure>(projectStructureUrl(currentProject.project_id, safePath)),
      ])
      if (seq !== fileLoadSeq.current) return false
      const nextFile: FileInfo = { ...rawFile, relative_path: rawFile.relative_path ?? safePath }
      nextFile.relative_path = safeRelativePath(nextFile.relative_path)
      fileRef.current = nextFile
      setFileState(previous => resolveResource(previous, nextFile))
      setStructureState(previous => resolveResource(previous, nextStructure))
      const states: Record<string, SegState> = {}
      for (const meta of nextStructure.segments) {
        states[meta.id] = { meta, text: '', status: 'idle', cached: false, mode: null, error: null }
      }
      setSegStates(states)
      setSegOrder(nextStructure.segments.map(meta => meta.id))
      setOverview({ text: '', status: 'idle' })
      setEvidenceByScope({})
      setGeneratedConfig(null)
      setExportAfterComplete(false)
      setSelection(null)
      setActiveSeg(null)
      pendingReveal.current = options.reveal ?? { start: 1, end: 1 }
      const focusLine = pendingReveal.current.start
      const focusSegment = nextStructure.segments.find(meta =>
        focusLine >= meta.start_line && focusLine <= meta.end_line)
        ?? nextStructure.segments[0]
      if (focusSegment) setActiveSeg(focusSegment.id)
      if (options.recordHistory !== false) {
        pushHistory({
          id: 'NAV',
          path: nextFile.relative_path,
          start_line: pendingReveal.current.start,
          end_line: pendingReveal.current.end,
          relation: 'file',
        })
      }
      if (scopeModeRef.current === 'auto') {
        // 总览始终先生成；段落按当前可见位置优先，其余保持在同一可抢占队列中。
        const orderedTargets = focusSegment
          ? [focusSegment, ...nextStructure.segments.filter(meta => meta.id !== focusSegment.id)]
          : nextStructure.segments
        startExplain(nextFile.relative_path, 'none', orderedTargets.map(meta => ({
          id: meta.id,
          mode: explainModeRef.current,
        })))
      }
      setSidebarOpen(false)
      return true
    } catch (error) {
      if (seq !== fileLoadSeq.current) return false
      const detail = messageOf(error)
      setFileState(previous => rejectResource(previous, detail))
      setStructureState(previous => rejectResource(previous, detail))
      setGlobalError(detail)
      return false
    }
  }, [cancelExplain, pushHistory, startExplain])

  useEffect(() => {
    if (!fileInfo || !pendingReveal.current) return
    const range = pendingReveal.current
    const frame = window.requestAnimationFrame(() => {
      codeApi.current?.revealRange(range.start, range.end)
      pendingReveal.current = null
    })
    return () => window.cancelAnimationFrame(frame)
  }, [fileInfo?.relative_path])

  const openEvidence = useCallback(async (evidence: Evidence, recordHistory = true) => {
    let path: string
    try { path = safeRelativePath(evidence.path) } catch (error) {
      setGlobalError(messageOf(error)); return
    }
    const range = { start: Math.max(1, evidence.start_line), end: Math.max(evidence.start_line, evidence.end_line) }
    const revealCode = () => {
      setMobileView('code')
      if (window.innerWidth >= 900) setWorkspaceMode('split')
    }
    if (fileRef.current?.relative_path === path) {
      codeApi.current?.revealRange(range.start, range.end)
      if (recordHistory) pushHistory({ ...evidence, path, ...{ start_line: range.start, end_line: range.end } })
      revealCode()
      return
    }
    const opened = await openFile(path, { reveal: range, recordHistory })
    if (opened) revealCode()
  }, [openFile, pushHistory])

  const navigateHistory = useCallback((direction: -1 | 1) => {
    const nextIndex = historyIndexRef.current + direction
    if (nextIndex < 0 || nextIndex >= historyRef.current.length) return
    historyIndexRef.current = nextIndex
    setHistoryVersion(value => value + 1)
    void openEvidence(historyRef.current[nextIndex], false)
  }, [openEvidence])

  const requestNavigation = useCallback(async (
    kind: 'definition' | 'references', line: number, column: number,
  ) => {
    const currentProject = projectRef.current
    const currentFile = fileRef.current
    if (!currentProject || !currentFile) return
    const endpoint = kind === 'definition' ? 'definitions' : 'references'
    const params = new URLSearchParams({
      path: currentFile.relative_path,
      line: String(line),
      column: String(column),
    })
    try {
      const value = await getJSON<any>(
        `/api/projects/${encodeURIComponent(currentProject.project_id)}/${endpoint}?${params}`,
      )
      const items = evidenceItems(Array.isArray(value) ? { items: value } : value ?? {})
      if (!items.length) {
        setGlobalError(kind === 'definition' ? '未找到定义' : '未找到引用')
      } else if (items.length === 1) {
        void openEvidence(items[0])
      } else {
        setQuickOpen({
          open: true,
          mode: 'results',
          title: kind === 'definition' ? '选择定义' : `找到 ${items.length} 处引用`,
          results: items,
        })
      }
    } catch (error) {
      setGlobalError(messageOf(error))
    }
  }, [openEvidence])

  // ---------- 双栏联动 ----------
  const findSegByLine = useCallback((line: number): string | null => {
    for (const [id, state] of Object.entries(segStatesRef.current)) {
      if (line >= state.meta.start_line && line <= state.meta.end_line) return id
    }
    return null
  }, [])

  const handleEditorLineClick = useCallback((line: number) => {
    const id = findSegByLine(line)
    if (!id) return
    setActiveSeg(id)
    document.getElementById(`card-${id}`)?.scrollIntoView({ behavior: 'smooth', block: 'nearest' })
  }, [findSegByLine])

  const handleCardClick = useCallback((id: string) => {
    setActiveSeg(id)
    const state = segStatesRef.current[id]
    if (state) codeApi.current?.revealRange(state.meta.start_line, state.meta.end_line)
    setMobileView('code')
  }, [])

  const handleOutlineClick = useCallback((line: number) => {
    codeApi.current?.revealRange(line, line)
    handleEditorLineClick(line)
    setSidebarOpen(false)
    setMobileView('code')
  }, [handleEditorLineClick])

  const changeWorkspaceMode = useCallback((mode: WorkspaceMode) => {
    setWorkspaceMode(mode)
    const labels: Record<WorkspaceMode, string> = {
      code: '已切换到代码专注视图',
      split: '已切换到双栏视图',
      explain: '已切换到解读专注视图',
    }
    showToast(labels[mode])
  }, [showToast])

  // ---------- 快捷键 ----------
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      const modifier = event.ctrlKey || event.metaKey
      if (modifier && !event.shiftKey && event.key.toLowerCase() === 'p') {
        event.preventDefault()
        if (projectRef.current) setQuickOpen({ open: true, mode: 'file' })
      } else if (modifier && event.shiftKey && event.key.toLowerCase() === 'o') {
        event.preventDefault()
        if (projectRef.current) setQuickOpen({ open: true, mode: 'symbol' })
      } else if (modifier && !event.shiftKey && event.key.toLowerCase() === 'b') {
        event.preventDefault()
        if (window.innerWidth >= 1100) setSidebarCollapsed(value => !value)
        else setSidebarOpen(value => !value)
      } else if (modifier && event.altKey && ['1', '2', '3'].includes(event.key)) {
        event.preventDefault()
        if (fileRef.current) {
          const modes: WorkspaceMode[] = ['code', 'split', 'explain']
          changeWorkspaceMode(modes[Number(event.key) - 1])
        }
      } else if (event.altKey && event.key === 'ArrowLeft') {
        event.preventDefault(); navigateHistory(-1)
      } else if (event.altKey && event.key === 'ArrowRight') {
        event.preventDefault(); navigateHistory(1)
      } else if (event.key === 'F12' && fileRef.current) {
        event.preventDefault()
        void requestNavigation(event.shiftKey ? 'references' : 'definition', cursorRef.current.line, cursorRef.current.column)
      }
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [changeWorkspaceMode, navigateHistory, requestNavigation])

  // ---------- 面板拖拽与键盘调整 ----------
  const onDragDivider = useCallback((event: ReactMouseEvent) => {
    event.preventDefault()
    const startX = event.clientX
    const startRatio = panelRatio
    const total = window.innerWidth
    const onMove = (next: MouseEvent) => {
      const delta = (startX - next.clientX) / total
      setPanelRatio(Math.min(0.6, Math.max(0.25, startRatio + delta)))
    }
    const onUp = () => {
      window.removeEventListener('mousemove', onMove)
      window.removeEventListener('mouseup', onUp)
    }
    window.addEventListener('mousemove', onMove)
    window.addEventListener('mouseup', onUp)
  }, [panelRatio])

  const activeRange: LineRange | null = activeSeg && segStates[activeSeg]
    ? { start: segStates[activeSeg].meta.start_line, end: segStates[activeSeg].meta.end_line }
    : null

  const historyBack = historyIndexRef.current > 0
  const historyForward = historyIndexRef.current >= 0 && historyIndexRef.current < historyRef.current.length - 1
  const sidebarExpanded = viewport === 'wide' ? !sidebarCollapsed : sidebarOpen
  void historyVersion // state changes intentionally trigger the derived disabled states above.

  return (
    <div className={`app viewport-${viewport} focus-${workspaceMode}${sidebarCollapsed ? ' nav-collapsed' : ''}`}>
      <TopBar
        health={health}
        projectRoot={project?.root ?? ''}
        onOpenProject={openProject}
        onRefreshHealth={refreshHealth}
      />

      {globalError && (
        <div className="global-error" role="alert" aria-live="assertive">
          <span>{globalError}</span>
          <button className="icon-btn" onClick={() => setGlobalError('')} aria-label="关闭错误提示">×</button>
        </div>
      )}
      <div className="sr-live" aria-live="polite" aria-atomic="true">
        {fileState.phase === 'loading' ? '正在打开文件' : ''}
        {explainJob.message}
      </div>

      <div className="workspace-toolbar">
        <button className="btn-sm nav-toggle"
          onClick={() => viewport === 'wide'
            ? setSidebarCollapsed(value => !value)
            : setSidebarOpen(value => !value)}
          aria-expanded={sidebarExpanded} aria-controls="project-sidebar"
          title="切换项目导航（Ctrl+B）">
          <svg viewBox="0 0 20 20" aria-hidden="true">
            <rect x="2.5" y="3" width="15" height="14" rx="2" />
            <path d="M7 3v14" />
          </svg>
          <span>{sidebarExpanded ? '收起导航' : '展开导航'}</span>
          <kbd>Ctrl B</kbd>
        </button>
        <div className="history-controls" aria-label="代码导航历史">
          <button className="icon-btn" disabled={!historyBack} onClick={() => navigateHistory(-1)}
            title="后退（Alt+←）" aria-label="后退">
            <svg viewBox="0 0 20 20" aria-hidden="true"><path d="m11.5 5-5 5 5 5" /></svg>
          </button>
          <button className="icon-btn" disabled={!historyForward} onClick={() => navigateHistory(1)}
            title="前进（Alt+→）" aria-label="前进">
            <svg viewBox="0 0 20 20" aria-hidden="true"><path d="m8.5 5 5 5-5 5" /></svg>
          </button>
        </div>
        {fileInfo && (
          <div className="workspace-breadcrumb" title={fileInfo.relative_path}>
            <span>{project?.name ?? '项目'}</span><i aria-hidden="true">/</i>
            <strong>{fileInfo.relative_path}</strong>
          </div>
        )}
        <div className="toolbar-spacer" />
        {project && (
          <div className="workspace-commands">
            <button className="btn-sm command-button" onClick={() => setQuickOpen({ open: true, mode: 'file' })}>
              快速打开 <kbd>Ctrl P</kbd>
            </button>
            <button className="btn-sm command-button" onClick={() => setQuickOpen({ open: true, mode: 'symbol' })}>
              转到符号 <kbd>Ctrl ⇧ O</kbd>
            </button>
            <span className="index-status" role="status" aria-live="polite"
              title={indexStatusTitle(project.index_status)}>
              {project.index_status?.state === 'building'
                ? '索引构建中…'
                : project.index_status?.state === 'ready'
                  ? `已索引 ${project.index_status.files_indexed ?? 0} 个文件`
                  : '索引未就绪'}
            </span>
          </div>
        )}
        {viewport !== 'narrow' && (
          <div className="focus-switcher" role="group" aria-label="工作区视图">
            {([
              ['code', '代码', 'Ctrl+Alt+1'],
              ['split', '双栏', 'Ctrl+Alt+2'],
              ['explain', '解读', 'Ctrl+Alt+3'],
            ] as const).map(([mode, label, shortcut]) => (
              <button key={mode} aria-pressed={workspaceMode === mode}
                className={workspaceMode === mode ? 'active' : ''}
                disabled={!fileInfo} title={shortcut}
                onClick={() => changeWorkspaceMode(mode)}>{label}</button>
            ))}
          </div>
        )}
        {viewport === 'narrow' && (
          <div className="mobile-view-tabs" role="tablist" aria-label="阅读视图">
            <button role="tab" aria-selected={mobileView === 'code'}
              className={mobileView === 'code' ? 'active' : ''} onClick={() => setMobileView('code')}>代码</button>
            <button role="tab" aria-selected={mobileView === 'explain'}
              className={mobileView === 'explain' ? 'active' : ''} onClick={() => setMobileView('explain')}>解读</button>
          </div>
        )}
      </div>

      <div className={`main mode-${workspaceMode}${sidebarOpen ? ' sidebar-open' : ''}${sidebarCollapsed ? ' sidebar-collapsed' : ''}${!fileInfo ? ' no-file' : ''}`}>
        {sidebarOpen && viewport !== 'wide' && (
          <button className="sidebar-backdrop" onClick={() => setSidebarOpen(false)}
            aria-label="关闭项目导航" />
        )}
        <aside id="project-sidebar" className="sidebar" aria-label="项目导航"
          aria-hidden={viewport === 'wide' && sidebarCollapsed}>
          <div className="side-tabs" role="tablist" aria-label="项目导航类型">
            <button role="tab" aria-selected={sideTab === 'files'}
              className={sideTab === 'files' ? 'active' : ''} onClick={() => setSideTab('files')}>文件</button>
            <button role="tab" aria-selected={sideTab === 'outline'}
              className={sideTab === 'outline' ? 'active' : ''} onClick={() => setSideTab('outline')}>大纲</button>
          </div>
          <div className="side-body">
            {sideTab === 'files' ? (
              project
                ? <FileTree projectId={project.project_id} rootLabel={project.root}
                    currentFile={fileInfo?.relative_path ?? null}
                    onOpenFile={path => {
                      if (viewport !== 'narrow') setWorkspaceMode('split')
                      void openFile(path)
                    }} />
                : <div className="side-empty">请先在顶部打开一个项目目录</div>
            ) : (
              structure
                ? <Outline nodes={structure.outline} onJump={handleOutlineClick} />
                : <div className="side-empty">打开文件后显示结构大纲</div>
            )}
          </div>
        </aside>

        <section className={`code-area${viewport === 'narrow' && mobileView !== 'code' ? ' mobile-hidden' : ''}`}
          aria-label="源代码">
          {fileInfo ? (
            <>
              <Suspense fallback={<div className="pane-loading" role="status">正在加载代码查看器…</div>}>
                <CodePane
                  fileInfo={fileInfo}
                  activeRange={activeRange}
                  onLineClick={handleEditorLineClick}
                  onSelection={setSelection}
                  onCursorPosition={(line, column) => { cursorRef.current = { line, column } }}
                  onNavigateRequest={(kind, line, column) => { void requestNavigation(kind, line, column) }}
                  onHistoryRequest={navigateHistory}
                  apiRef={codeApi}
                />
              </Suspense>
              {fileState.phase === 'loading' && <div className="pane-loading" role="status">正在打开文件…</div>}
              <SelectionActions
                selection={selection}
                relativePath={fileInfo.relative_path}
                onAsk={() => {
                  setChatOpen(true)
                  showToast('已将选区加入追问上下文')
                }}
                onCopyCode={async () => {
                  const text = codeApi.current?.getSelectedText() ?? ''
                  if (!text) throw new Error('没有可复制的代码')
                  if (!navigator.clipboard) throw new Error('当前环境不支持复制')
                  await navigator.clipboard.writeText(text)
                }}
                onDismiss={() => codeApi.current?.clearSelection()}
              />
            </>
          ) : (
            <div className="welcome">
              <div className="welcome-kicker">LOCAL CODE INTELLIGENCE</div>
              <div className="welcome-art" aria-hidden="true"><i /><i /><i /></div>
              <h1>{project ? `从 ${project.name} 开始阅读` : '把陌生代码变成可追溯的解释'}</h1>
              <p className="welcome-lead">
                {project
                  ? '项目已就绪。打开一个文件，沿着代码、结构与证据逐层建立理解。'
                  : 'CodeReader 在本机连接源代码、结构大纲与中文解读，让每个结论都能回到具体行。'}
              </p>
              <div className="welcome-actions">
                {project ? (
                  <button className="btn-primary" onClick={() => setQuickOpen({ open: true, mode: 'file' })}>
                    快速打开文件 <kbd>Ctrl P</kbd>
                  </button>
                ) : (
                  <button className="btn-primary"
                    onClick={() => document.querySelector<HTMLInputElement>('#project-path-input')?.focus()}>
                    选择项目目录
                  </button>
                )}
                <span>代码内容与解读缓存均保留在本机</span>
              </div>
              <ol className="welcome-guide">
                <li><strong>01</strong><span>打开项目与文件</span></li>
                <li><strong>02</strong><span>选择代码或生成解读</span></li>
                <li><strong>03</strong><span>追问并回到证据行</span></li>
              </ol>
              {projectState.phase === 'loading' && <p role="status">正在打开项目…</p>}
            </div>
          )}
        </section>

        {viewport !== 'narrow' && fileInfo && workspaceMode === 'split' && (
          <div className="divider" onMouseDown={onDragDivider} role="separator" tabIndex={0}
            aria-label="调整代码与解读区域宽度" aria-orientation="vertical"
            aria-valuemin={25} aria-valuemax={60} aria-valuenow={Math.round(panelRatio * 100)}
            onKeyDown={event => {
              if (event.key === 'ArrowLeft') setPanelRatio(value => Math.min(0.6, value + 0.02))
              else if (event.key === 'ArrowRight') setPanelRatio(value => Math.max(0.25, value - 0.02))
              else if (event.key === 'Home') setPanelRatio(0.25)
              else if (event.key === 'End') setPanelRatio(0.6)
              else return
              event.preventDefault()
            }} />
        )}

        <section className={`explain-area${viewport === 'narrow' && mobileView !== 'explain' ? ' mobile-hidden' : ''}`}
          style={viewport === 'narrow' || workspaceMode !== 'split'
            ? undefined
            : { width: `${panelRatio * 100}%` }} aria-label="代码解读">
          <ExplainPanel
            fileInfo={fileInfo}
            projectId={project?.project_id ?? ''}
            structure={structure}
            overview={overview}
            segOrder={segOrder}
            segStates={segStates}
            explaining={explaining}
            error={explainJob.error ?? ''}
            notice={explainJob.message}
            ready={ready}
            activeSeg={activeSeg}
            explainMode={explainMode}
            scopeMode={scopeMode}
            evidenceByScope={evidenceByScope}
            staleConfig={!!(generatedConfig && health && (
              generatedConfig.model !== health.model
              || generatedConfig.thinking !== health.thinking.enabled
            ))}
            onModeChange={changeExplainMode}
            onScopeChange={changeScopeMode}
            onCardClick={handleCardClick}
            onExplainAll={() => fileInfo && startExplain(fileInfo.relative_path, 'all')}
            onCompleteAndExport={() => {
              if (!fileInfo) return
              setExportAfterComplete(true)
              startExplain(fileInfo.relative_path, 'none')
            }}
            onExplainSeg={explainOneSeg}
            onOverview={explainOverview}
            onStop={() => cancelExplain('已由用户停止')}
            onOpenEvidence={evidence => { void openEvidence(evidence) }}
          />
        </section>
      </div>

      <ChatDrawer
        open={chatOpen}
        onToggle={() => setChatOpen(value => !value)}
        projectId={project?.project_id ?? null}
        relativePath={fileInfo?.relative_path ?? null}
        sessionKey={project?.project_id ?? ''}
        selection={selection}
        ready={ready}
        onOpenEvidence={evidence => { void openEvidence(evidence) }}
      />

      <QuickOpen
        open={quickOpen.open}
        mode={quickOpen.mode}
        title={quickOpen.title}
        presetResults={quickOpen.results}
        projectId={project?.project_id ?? null}
        onClose={() => setQuickOpen(previous => ({ ...previous, open: false }))}
        onSelect={evidence => { void openEvidence(evidence) }}
      />

      {toast && <div className="toast" role="status" aria-live="polite">{toast}</div>}
    </div>
  )
}
