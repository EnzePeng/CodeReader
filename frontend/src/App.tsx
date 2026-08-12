import { MouseEvent as ReactMouseEvent, useCallback, useEffect, useRef, useState } from 'react'
import TopBar from './components/TopBar'
import FileTree from './components/FileTree'
import Outline from './components/Outline'
import CodePane, { CodePaneApi } from './components/CodePane'
import ExplainPanel from './components/ExplainPanel'
import ChatDrawer from './components/ChatDrawer'
import { getJSON, postJSON, streamSSE, encodePath } from './api'
import {
  ExplainMode, ExplainTarget, FileInfo, Health, LineRange, ScopeMode, SegState, Structure,
} from './types'

type ForceArg = 'none' | 'all' | string[]

const MODE_KEY = 'cr_explain_mode'
const SCOPE_KEY = 'cr_scope_mode'

export default function App() {
  const [health, setHealth] = useState<Health | null>(null)
  const [projectRoot, setProjectRoot] = useState<string>('')
  const [sideTab, setSideTab] = useState<'files' | 'outline'>('files')

  const [fileInfo, setFileInfo] = useState<FileInfo | null>(null)
  const [structure, setStructure] = useState<Structure | null>(null)
  const [overview, setOverview] = useState<{ text: string; status: string }>({ text: '', status: 'idle' })
  const [segStates, setSegStates] = useState<Record<string, SegState>>({})
  const [segOrder, setSegOrder] = useState<string[]>([])
  const [explaining, setExplaining] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [activeSeg, setActiveSeg] = useState<string | null>(null)
  const [selection, setSelection] = useState<LineRange | null>(null)
  const [chatOpen, setChatOpen] = useState(false)
  const [panelRatio, setPanelRatio] = useState(0.42)

  // 解读模式（简单/逐行）与范围（自动全部/手动选块），持久化到 localStorage
  const [explainMode, setExplainMode] = useState<ExplainMode>(
    () => (localStorage.getItem(MODE_KEY) === 'detailed' ? 'detailed' : 'simple'))
  const [scopeMode, setScopeMode] = useState<ScopeMode>(
    () => (localStorage.getItem(SCOPE_KEY) === 'manual' ? 'manual' : 'auto'))

  const abortRef = useRef<AbortController | null>(null)
  const codeApi = useRef<CodePaneApi | null>(null)
  const segStatesRef = useRef(segStates)
  segStatesRef.current = segStates
  const projectRootRef = useRef(projectRoot)
  projectRootRef.current = projectRoot
  const fileInfoRef = useRef(fileInfo)
  fileInfoRef.current = fileInfo
  const explainModeRef = useRef(explainMode)
  explainModeRef.current = explainMode
  const scopeModeRef = useRef(scopeMode)
  scopeModeRef.current = scopeMode

  // ---------- 模型状态轮询 ----------
  const healthTimer = useRef<number | undefined>(undefined)
  const healthSeq = useRef(0)
  const healthStopped = useRef(false)

  // 拉取一次健康状态，并按结果重排下一次轮询（就绪 30s / 未就绪 3s）；
  // 除定时轮询外，切换模型后也会立即调用，让界面马上拿到新模型名与加载状态
  const refreshHealth = useCallback(async (): Promise<Health | null> => {
    const seq = ++healthSeq.current
    let h: Health | null = null
    try {
      h = await getJSON<Health>('/api/health')
    } catch {
      h = null
    }
    // 只采纳最近一次请求的结果，避免更早发出的轮询响应覆盖刚刷新的状态
    if (healthStopped.current || seq !== healthSeq.current) return h
    setHealth(h)
    window.clearTimeout(healthTimer.current)
    healthTimer.current = window.setTimeout(refreshHealth, h?.llama.ready ? 30000 : 3000)
    return h
  }, [])

  useEffect(() => {
    healthStopped.current = false
    void refreshHealth()
    return () => {
      healthStopped.current = true
      window.clearTimeout(healthTimer.current)
    }
  }, [refreshHealth])

  // ---------- 解读流程 ----------
  // targets 为空（undefined）= 解读全部分段；传数组 = 只解读列出的段（空数组则只生成总览）。
  // modeArg 缺省时使用当前全局模式；每个 target 可携带自己的 mode 覆盖全局。
  const startExplain = useCallback((
    path: string, force: ForceArg, targets?: ExplainTarget[], modeArg?: ExplainMode,
  ) => {
    abortRef.current?.abort()
    const ctrl = new AbortController()
    abortRef.current = ctrl
    setExplaining(true)
    setError('')
    setNotice('')
    if (force === 'all') {
      setOverview({ text: '', status: 'idle' })
      setSegStates(prev => {
        const next: Record<string, SegState> = {}
        for (const [k, v] of Object.entries(prev)) next[k] = { ...v, text: '', status: 'idle' }
        return next
      })
    }
    const body = {
      path,
      force,
      project_root: projectRootRef.current || null,
      mode: modeArg ?? explainModeRef.current,
      targets: targets ?? null,
    }
    streamSSE('/api/explain', body, (event, data) => {
      switch (event) {
        case 'meta':
          setSegStates(prev => {
            const next: Record<string, SegState> = {}
            for (const m of data.segments) {
              next[m.id] = prev[m.id]
                ? { ...prev[m.id], meta: m }
                : { meta: m, text: '', status: 'idle', cached: false, mode: null }
            }
            return next
          })
          setSegOrder(data.segments.map((m: any) => m.id))
          break
        case 'status':
          setNotice(data.message)
          break
        case 'overview_start':
          setNotice('')
          setOverview({ text: '', status: 'streaming' })
          break
        case 'overview_delta':
          setOverview(o => ({ text: o.text + data.text, status: 'streaming' }))
          break
        case 'overview_done':
          setNotice('')
          setOverview({ text: data.text, status: 'done' })
          break
        case 'segment_start':
          setSegStates(s => s[data.id]
            ? { ...s, [data.id]: { ...s[data.id], text: '', status: 'streaming', mode: data.mode ?? s[data.id].mode } }
            : s)
          break
        case 'segment_delta':
          setSegStates(s => s[data.id]
            ? { ...s, [data.id]: { ...s[data.id], text: s[data.id].text + data.text, status: 'streaming', mode: data.mode ?? s[data.id].mode } }
            : s)
          break
        case 'segment_done':
          setSegStates(s => s[data.id]
            ? { ...s, [data.id]: { ...s[data.id], text: data.text, status: 'done', cached: data.cached, mode: data.mode ?? s[data.id].mode } }
            : s)
          break
        case 'error':
          setError(data.message)
          break
      }
    }, ctrl.signal)
      .catch(e => {
        if ((e as Error).name !== 'AbortError') setError(String((e as Error).message || e))
      })
      .finally(() => {
        if (abortRef.current === ctrl) setExplaining(false)
      })
  }, [])

  const stopExplain = useCallback(() => {
    abortRef.current?.abort()
    setExplaining(false)
    setSegStates(prev => {
      const next: Record<string, SegState> = {}
      for (const [k, v] of Object.entries(prev)) {
        next[k] = v.status === 'streaming' ? { ...v, status: 'idle' } : v
      }
      return next
    })
  }, [])

  // 只解读指定的段（每个 target 可带自己的模式）；复用同一条 SSE 处理逻辑
  const explainSegments = useCallback((targets: ExplainTarget[], force: ForceArg = 'none') => {
    const fi = fileInfoRef.current
    if (fi) startExplain(fi.path, force, targets)
  }, [startExplain])

  // 单卡片操作：mode 指定本次解读模式，force=true 表示忽略缓存重新生成
  const explainOneSeg = useCallback((id: string, mode: ExplainMode, force: boolean) => {
    explainSegments([{ id, mode }], force ? [id] : 'none')
  }, [explainSegments])

  // 生成/重新生成文件总览：自动范围下保持整轮解读（缓存段瞬间回放），手动范围下只跑总览
  const explainOverview = useCallback((force: boolean) => {
    const fi = fileInfoRef.current
    if (!fi) return
    const forceArg: ForceArg = force ? ['overview'] : 'none'
    if (scopeModeRef.current === 'auto') startExplain(fi.path, forceArg)
    else startExplain(fi.path, forceArg, [])
  }, [startExplain])

  const changeExplainMode = useCallback((m: ExplainMode) => {
    explainModeRef.current = m
    setExplainMode(m)
    localStorage.setItem(MODE_KEY, m)
    // 自动范围下切换模式立即按新模式解读（走缓存，命中的段瞬间恢复）
    if (scopeModeRef.current === 'auto' && fileInfoRef.current) {
      startExplain(fileInfoRef.current.path, 'none', undefined, m)
    }
  }, [startExplain])

  const changeScopeMode = useCallback((m: ScopeMode) => {
    scopeModeRef.current = m
    setScopeMode(m)
    localStorage.setItem(SCOPE_KEY, m)
    // 切回自动范围时补跑一轮全量解读（已缓存的段瞬间恢复）
    if (m === 'auto' && fileInfoRef.current) {
      startExplain(fileInfoRef.current.path, 'none')
    }
  }, [startExplain])

  // ---------- 打开文件 ----------
  const openFile = useCallback(async (path: string) => {
    abortRef.current?.abort()
    setError('')
    setSelection(null)
    setActiveSeg(null)
    try {
      const [fi, st] = await Promise.all([
        getJSON<FileInfo>(`/api/file?path=${encodePath(path)}`),
        getJSON<Structure>(`/api/structure?path=${encodePath(path)}&project_root=${encodePath(projectRootRef.current)}`),
      ])
      setFileInfo(fi)
      fileInfoRef.current = fi
      setStructure(st)
      const states: Record<string, SegState> = {}
      for (const m of st.segments) states[m.id] = { meta: m, text: '', status: 'idle', cached: false, mode: null }
      setSegStates(states)
      setSegOrder(st.segments.map(m => m.id))
      setOverview({ text: '', status: 'idle' })
      // 自动范围：打开即全量解读；手动范围：不自动跑，由用户在卡片上按需触发
      if (scopeModeRef.current === 'auto') startExplain(path, 'none')
    } catch (e) {
      setFileInfo(null)
      setStructure(null)
      setSegStates({})
      setSegOrder([])
      setError(String((e as Error).message || e))
    }
  }, [startExplain])

  const openProject = useCallback(async (path: string) => {
    setProjectRoot(path)
    setSideTab('files')
    postJSON('/api/recents', { path }).catch(() => undefined)
    // 预热项目符号索引（跨文件解读上下文）
    getJSON(`/api/project/summary?root=${encodePath(path)}`).catch(() => undefined)
  }, [])

  // ---------- 双栏联动 ----------
  const findSegByLine = useCallback((line: number): string | null => {
    for (const [id, s] of Object.entries(segStatesRef.current)) {
      if (line >= s.meta.start_line && line <= s.meta.end_line) return id
    }
    return null
  }, [])

  const handleEditorLineClick = useCallback((line: number) => {
    const id = findSegByLine(line)
    if (id) {
      setActiveSeg(id)
      document.getElementById(`card-${id}`)?.scrollIntoView({ behavior: 'smooth', block: 'nearest' })
    }
  }, [findSegByLine])

  const handleCardClick = useCallback((id: string) => {
    setActiveSeg(id)
    const s = segStatesRef.current[id]
    if (s) codeApi.current?.revealRange(s.meta.start_line, s.meta.end_line)
  }, [])

  const handleOutlineClick = useCallback((line: number) => {
    codeApi.current?.revealRange(line, line)
    handleEditorLineClick(line)
  }, [handleEditorLineClick])

  // ---------- 面板拖拽 ----------
  const onDragDivider = useCallback((e: ReactMouseEvent) => {
    e.preventDefault()
    const startX = e.clientX
    const startRatio = panelRatio
    const total = window.innerWidth
    const onMove = (ev: MouseEvent) => {
      const delta = (startX - ev.clientX) / total
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

  const ready = !!health?.llama.ready

  return (
    <div className="app">
      <TopBar
        health={health}
        projectRoot={projectRoot}
        onOpenProject={openProject}
        onRefreshHealth={refreshHealth}
      />
      <div className="main">
        <aside className="sidebar">
          <div className="side-tabs">
            <button className={sideTab === 'files' ? 'active' : ''} onClick={() => setSideTab('files')}>文件</button>
            <button className={sideTab === 'outline' ? 'active' : ''} onClick={() => setSideTab('outline')}>大纲</button>
          </div>
          <div className="side-body">
            {sideTab === 'files' ? (
              projectRoot
                ? <FileTree root={projectRoot} currentFile={fileInfo?.path || null} onOpenFile={openFile} />
                : <div className="side-empty">请先在顶部打开一个项目目录</div>
            ) : (
              structure
                ? <Outline nodes={structure.outline} onJump={handleOutlineClick} />
                : <div className="side-empty">打开文件后显示结构大纲</div>
            )}
          </div>
        </aside>

        <section className="code-area">
          {fileInfo ? (
            <CodePane
              fileInfo={fileInfo}
              activeRange={activeRange}
              onLineClick={handleEditorLineClick}
              onSelection={setSelection}
              apiRef={codeApi}
            />
          ) : (
            <div className="welcome">
              <div className="welcome-art" aria-hidden="true"><i /><i /><i /></div>
              <h2>CodeReader</h2>
              <p>纯离线 · 本地大模型代码解读</p>
              <ol>
                <li>顶部输入或浏览选择项目目录</li>
                <li>左侧文件树中选择一个代码文件</li>
                <li>右侧将自动逐段生成中文解读</li>
              </ol>
              <p className="dim">解读结果会缓存在本地，重开同一文件瞬间加载；代码内容不出本机。</p>
            </div>
          )}
        </section>

        <div className="divider" onMouseDown={onDragDivider} />

        <section className="explain-area" style={{ width: `${panelRatio * 100}%` }}>
          <ExplainPanel
            fileInfo={fileInfo}
            projectRoot={projectRoot}
            structure={structure}
            overview={overview}
            segOrder={segOrder}
            segStates={segStates}
            explaining={explaining}
            error={error}
            notice={notice}
            ready={ready}
            activeSeg={activeSeg}
            explainMode={explainMode}
            scopeMode={scopeMode}
            onModeChange={changeExplainMode}
            onScopeChange={changeScopeMode}
            onCardClick={handleCardClick}
            onExplainAll={() => fileInfo && startExplain(fileInfo.path, 'all')}
            onExplainSeg={explainOneSeg}
            onOverview={explainOverview}
            onStop={stopExplain}
          />
        </section>
      </div>

      <ChatDrawer
        open={chatOpen}
        onToggle={() => setChatOpen(o => !o)}
        filePath={fileInfo?.path || null}
        projectRoot={projectRoot}
        selection={selection}
        ready={ready}
      />
    </div>
  )
}
