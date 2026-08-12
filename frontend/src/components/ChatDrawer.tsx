import { memo, useEffect, useRef, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { streamSSE } from '../api'
import { ChatMsg, LineRange } from '../types'

interface Props {
  open: boolean
  onToggle: () => void
  filePath: string | null
  projectRoot: string
  selection: LineRange | null
  ready: boolean
}

/* ---------- 拖拽 / 缩放：常量与工具 ---------- */
const POS_KEY = 'cr_chat_pos'
const SIZE_KEY = 'cr_chat_size'
const MIN_W = 320        // 最小宽度
const MIN_H = 380        // 最小高度
const EDGE = 8           // 距视口边缘的保护间距
const DEFAULT_W = 400    // 与 styles.css 里 .chat-drawer 的默认尺寸一致
const DEFAULT_H = 500

type Pos = { x: number; y: number }
type Size = { w: number; h: number }

function loadStored<T>(key: string): T | null {
  try {
    const raw = localStorage.getItem(key)
    return raw ? JSON.parse(raw) as T : null
  } catch { return null }
}

function saveStored(key: string, val: unknown) {
  try { localStorage.setItem(key, JSON.stringify(val)) } catch { /* 存储失败可忽略 */ }
}

/** 尺寸收拢到 [最小尺寸, 视口 - 边距] */
function clampSize(w: number, h: number): Size {
  const maxW = Math.max(MIN_W, window.innerWidth - EDGE * 2)
  const maxH = Math.max(MIN_H, window.innerHeight - EDGE * 2)
  return { w: Math.min(Math.max(w, MIN_W), maxW), h: Math.min(Math.max(h, MIN_H), maxH) }
}

/** 位置收拢到视口内，保证整个面板（尤其标题栏）不会被拖出屏幕 */
function clampPos(x: number, y: number, w: number, h: number): Pos {
  const maxX = Math.max(EDGE, window.innerWidth - w - EDGE)
  const maxY = Math.max(EDGE, window.innerHeight - h - EDGE)
  return { x: Math.min(Math.max(x, EDGE), maxX), y: Math.min(Math.max(y, EDGE), maxY) }
}

/* 单条消息气泡。memo 化：拖拽/缩放引起的高频重渲染不必重新解析 Markdown */
const Bubble = memo(function Bubble({ role, content, showCaret }: {
  role: ChatMsg['role']; content: string; showCaret: boolean
}) {
  return (
    <div className="chat-bubble md">
      {role === 'assistant'
        ? <>
            <ReactMarkdown remarkPlugins={[remarkGfm]}>{content || '…'}</ReactMarkdown>
            {showCaret && <span className="caret" />}
          </>
        : content}
    </div>
  )
})

export default function ChatDrawer({ open, onToggle, filePath, projectRoot, selection, ready }: Props) {
  const [msgs, setMsgs] = useState<ChatMsg[]>([])
  const [input, setInput] = useState('')
  const [streaming, setStreaming] = useState(false)
  const [useSel, setUseSel] = useState(true)
  const abortRef = useRef<AbortController | null>(null)
  const listRef = useRef<HTMLDivElement | null>(null)

  // 面板位置/尺寸；null 表示未自定义，沿用 styles.css 的右下角默认布局
  const [pos, setPos] = useState<Pos | null>(() => {
    const p = loadStored<Pos>(POS_KEY)
    if (!p || !Number.isFinite(p.x) || !Number.isFinite(p.y)) return null
    const s = loadStored<Size>(SIZE_KEY)
    const { w, h } = s && Number.isFinite(s.w) && Number.isFinite(s.h)
      ? clampSize(s.w, s.h) : { w: DEFAULT_W, h: DEFAULT_H }
    return clampPos(p.x, p.y, w, h)
  })
  const [size, setSize] = useState<Size | null>(() => {
    const s = loadStored<Size>(SIZE_KEY)
    return s && Number.isFinite(s.w) && Number.isFinite(s.h) ? clampSize(s.w, s.h) : null
  })
  const [dragMode, setDragMode] = useState<'move' | 'resize' | null>(null)
  const panelRef = useRef<HTMLDivElement | null>(null)
  const dragRef = useRef<{
    mode: 'move' | 'resize'
    startX: number; startY: number
    base: { x: number; y: number; w: number; h: number }   // 按下时的面板矩形
    lastPos?: Pos; lastSize?: Size                          // 结束时待持久化的值
  } | null>(null)

  useEffect(() => {
    abortRef.current?.abort()
    setMsgs([])
    setStreaming(false)
  }, [filePath])

  useEffect(() => {
    listRef.current?.scrollTo({ top: listRef.current.scrollHeight })
  }, [msgs])

  // 打开面板及窗口尺寸变化时，把位置/尺寸重新收拢到视口内
  useEffect(() => {
    if (!open) return
    const reclamp = () => {
      setSize(s => (s ? clampSize(s.w, s.h) : s))
      setPos(p => {
        if (!p) return p
        const rect = panelRef.current?.getBoundingClientRect()
        return clampPos(p.x, p.y, rect?.width ?? DEFAULT_W, rect?.height ?? DEFAULT_H)
      })
    }
    reclamp()
    window.addEventListener('resize', reclamp)
    return () => window.removeEventListener('resize', reclamp)
  }, [open])

  /** 标题栏按下：开始拖拽移动（点在按钮上时不触发） */
  const onHeadDown = (e: React.PointerEvent<HTMLDivElement>) => {
    if (dragRef.current) return
    if ((e.target as HTMLElement).closest('button')) return
    if (e.pointerType === 'mouse' && e.button !== 0) return
    const rect = panelRef.current!.getBoundingClientRect()
    dragRef.current = {
      mode: 'move', startX: e.clientX, startY: e.clientY,
      base: { x: rect.left, y: rect.top, w: rect.width, h: rect.height },
    }
    setDragMode('move')
    e.currentTarget.setPointerCapture(e.pointerId)
    e.preventDefault()
  }

  /** 右下角手柄按下：开始拖拽缩放 */
  const onGripDown = (e: React.PointerEvent<HTMLDivElement>) => {
    if (dragRef.current) return
    if (e.pointerType === 'mouse' && e.button !== 0) return
    const rect = panelRef.current!.getBoundingClientRect()
    // 先把左上角固定下来（从默认的 right/bottom 定位切换到 left/top）
    const p = clampPos(rect.left, rect.top, rect.width, rect.height)
    setPos(p)
    dragRef.current = {
      mode: 'resize', startX: e.clientX, startY: e.clientY,
      base: { x: p.x, y: p.y, w: rect.width, h: rect.height },
      lastPos: p,
    }
    setDragMode('resize')
    e.currentTarget.setPointerCapture(e.pointerId)
    e.preventDefault()
  }

  /** 拖拽过程：根据模式更新位置或尺寸（已 setPointerCapture，事件持续派发到源元素） */
  const onDragMove = (e: React.PointerEvent<HTMLDivElement>) => {
    const d = dragRef.current
    if (!d) return
    const dx = e.clientX - d.startX
    const dy = e.clientY - d.startY
    if (d.mode === 'move') {
      const p = clampPos(d.base.x + dx, d.base.y + dy, d.base.w, d.base.h)
      d.lastPos = p
      setPos(p)
    } else {
      // 以左上角为锚点向右下缩放，不超出视口边距
      const maxW = Math.max(MIN_W, window.innerWidth - d.base.x - EDGE)
      const maxH = Math.max(MIN_H, window.innerHeight - d.base.y - EDGE)
      const s = {
        w: Math.min(Math.max(d.base.w + dx, MIN_W), maxW),
        h: Math.min(Math.max(d.base.h + dy, MIN_H), maxH),
      }
      d.lastSize = s
      setSize(s)
    }
  }

  /** 拖拽结束：持久化本次调整 */
  const endDrag = () => {
    const d = dragRef.current
    if (!d) return
    dragRef.current = null
    setDragMode(null)
    if (d.lastPos) saveStored(POS_KEY, d.lastPos)
    if (d.lastSize) saveStored(SIZE_KEY, d.lastSize)
  }

  /** 双击标题栏：恢复默认位置和大小 */
  const resetLayout = (e: React.MouseEvent<HTMLDivElement>) => {
    if ((e.target as HTMLElement).closest('button')) return
    setPos(null)
    setSize(null)
    try {
      localStorage.removeItem(POS_KEY)
      localStorage.removeItem(SIZE_KEY)
    } catch { /* 忽略 */ }
  }

  const send = () => {
    const question = input.trim()
    if (!question || streaming || !filePath || !ready) return
    const history = msgs
    setMsgs(m => [...m, { role: 'user', content: question }, { role: 'assistant', content: '' }])
    setInput('')
    setStreaming(true)
    const ctrl = new AbortController()
    abortRef.current = ctrl
    const sel = useSel && selection ? { start_line: selection.start, end_line: selection.end } : null
    streamSSE('/api/chat', {
      path: filePath, question, selection: sel, history,
      project_root: projectRoot || null,
    }, (ev, data) => {
      if (ev === 'delta') {
        setMsgs(m => {
          const next = [...m]
          const last = next[next.length - 1]
          next[next.length - 1] = { ...last, content: last.content + data.text }
          return next
        })
      } else if (ev === 'error') {
        setMsgs(m => {
          const next = [...m]
          const last = next[next.length - 1]
          next[next.length - 1] = { ...last, content: last.content + `\n\n> ${data.message}` }
          return next
        })
      }
    }, ctrl.signal)
      .catch(e => {
        if ((e as Error).name !== 'AbortError') {
          setMsgs(m => [...m.slice(0, -1), { role: 'assistant', content: `请求失败：${(e as Error).message}` }])
        }
      })
      .finally(() => setStreaming(false))
  }

  const stop = () => {
    abortRef.current?.abort()
    setStreaming(false)
  }

  if (!open) {
    return (
      <button className="chat-fab" onClick={onToggle} disabled={!filePath}
        title={filePath ? '就当前代码提问' : '先打开一个文件'}>
        追问
      </button>
    )
  }

  // 动态定位/尺寸走内联样式；为 null 时沿用 CSS 里右下角的默认布局
  const panelStyle: React.CSSProperties = {}
  if (pos) {
    panelStyle.left = pos.x
    panelStyle.top = pos.y
    panelStyle.right = 'auto'
    panelStyle.bottom = 'auto'
  }
  if (size) {
    panelStyle.width = size.w
    panelStyle.height = size.h
    panelStyle.maxHeight = 'none'
  }

  return (
    <div ref={panelRef} style={panelStyle}
      className={`chat-drawer${dragMode ? ` drag-${dragMode}` : ''}`}>
      <div className="chat-head" title="按住拖动位置，双击恢复默认布局"
        onPointerDown={onHeadDown}
        onPointerMove={onDragMove}
        onPointerUp={endDrag}
        onPointerCancel={endDrag}
        onDoubleClick={resetLayout}
      >
        <span>代码追问</span>
        <span className="chat-tools">
          <button className="icon-btn" title="清空对话" onClick={() => setMsgs([])}>清空</button>
          <button className="icon-btn" onClick={onToggle}>×</button>
        </span>
      </div>
      <div className="chat-ctx">
        <span className="chip chip-file" title={filePath || ''}>{filePath?.split('\\').pop()}</span>
        {selection && (
          <label className="sel-toggle">
            <input type="checkbox" checked={useSel} onChange={e => setUseSel(e.target.checked)} />
            引用选中的第 {selection.start}~{selection.end} 行
          </label>
        )}
        {!selection && <span className="dim">在左侧代码中拖选行，可针对性提问</span>}
      </div>
      <div className="chat-list" ref={listRef}>
        {msgs.length === 0 && (
          <div className="chat-empty">
            例如：这个函数的返回值在哪里被用到？这段正则在匹配什么？这个类的生命周期是怎样的？
          </div>
        )}
        {msgs.map((m, i) => (
          <div key={i} className={`chat-msg ${m.role}`}>
            <Bubble role={m.role} content={m.content}
              showCaret={streaming && i === msgs.length - 1} />
          </div>
        ))}
      </div>
      <div className="chat-input">
        <textarea
          rows={2}
          value={input}
          placeholder={ready ? '输入问题，Enter 发送，Shift+Enter 换行' : '等待模型就绪…'}
          disabled={!ready}
          onChange={e => setInput(e.target.value)}
          onKeyDown={e => {
            if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send() }
          }}
        />
        {streaming
          ? <button className="btn-sm warn" onClick={stop}>停止</button>
          : <button className="btn-primary" onClick={send} disabled={!input.trim() || !ready}>发送</button>}
      </div>
      <div className="chat-resize-grip" title="拖拽调整大小"
        onPointerDown={onGripDown}
        onPointerMove={onDragMove}
        onPointerUp={endDrag}
        onPointerCancel={endDrag}
      />
    </div>
  )
}
