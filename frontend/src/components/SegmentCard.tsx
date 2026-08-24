import { MouseEvent as ReactMouseEvent, memo } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { Evidence, ExplainMode, SegState } from '../types'

const KIND_LABEL: Record<string, string> = {
  docstring: '说明',
  imports: '导入',
  globals: '全局',
  class: '类',
  class_header: '类',
  method: '方法',
  function: '函数',
  main: '入口',
  code: '代码',
  chunk: '片段',
}

const MODE_LABEL: Record<ExplainMode, string> = { simple: '简', detailed: '行' }
const MODE_NAME: Record<ExplainMode, string> = { simple: '简单版', detailed: '逐行版' }

interface Props {
  state: SegState
  active: boolean
  collapsed: boolean
  explaining: boolean
  ready: boolean
  /** 是否处于「手动选块」范围 */
  manual: boolean
  onClick: () => void
  onToggleCollapsed: () => void
  evidence?: Evidence[]
  onOpenEvidence?: (evidence: Evidence) => void
  /** 以指定模式解读本段；force=true 忽略缓存重新生成 */
  onExplain: (mode: ExplainMode, force: boolean) => void
}

function SegmentCard({
  state, active, collapsed, explaining, ready, manual, onClick, onToggleCollapsed, onExplain,
  evidence = [], onOpenEvidence,
}: Props) {
  const { meta, text, status, cached, mode } = state
  const otherMode: ExplainMode = mode === 'detailed' ? 'simple' : 'detailed'

  const copy = (e: ReactMouseEvent) => {
    e.stopPropagation()
    navigator.clipboard?.writeText(text).catch(() => undefined)
  }
  const regen = (e: ReactMouseEvent) => {
    e.stopPropagation()
    onExplain(mode ?? 'simple', true)
  }
  const switchMode = (e: ReactMouseEvent) => {
    e.stopPropagation()
    onExplain(otherMode, false) // 先走缓存，另一模式已生成过时瞬间切换
  }
  const explainAs = (m: ExplainMode) => (e: ReactMouseEvent) => {
    e.stopPropagation()
    onExplain(m, false)
  }

  // 手动模式下未生成的段：卡片主体显示两个模式按钮
  const showModeButtons = manual && !text && status !== 'streaming'
  // 自动模式下排队等待的段：骨架屏占位 + 整卡弱化
  const queued = !showModeButtons && !text && status !== 'streaming' && explaining
  // 当前定位段与流式生成段必须保持展开，避免隐藏用户正在关注的内容。
  const isCollapsed = collapsed && !active && status !== 'streaming'
  const bodyId = `card-body-${meta.id}`

  const collapsedStatus = queued
    ? '排队中'
    : status === 'done'
      ? '已完成'
      : status === 'cancelled'
        ? '已停止'
        : status === 'error'
          ? '生成失败'
          : '待解读'

  const cardCls = [
    'card', 'seg-card',
    active ? 'active' : '',
    status === 'streaming' ? 'streaming' : '',
    queued ? 'queued' : '',
    isCollapsed ? 'seg-card-collapsed' : '',
  ].filter(Boolean).join(' ')

  return (
    <article id={`card-${meta.id}`} className={cardCls}
      aria-current={active ? 'location' : undefined}
      aria-busy={status === 'streaming' || queued ? true : undefined}>
      <div className="card-head">
        <button
          type="button"
          className="icon-btn card-disclosure"
          aria-expanded={!isCollapsed}
          aria-controls={bodyId}
          aria-label={`${isCollapsed ? '展开' : '收起'} ${meta.title} 的解读`}
          title={active || status === 'streaming' ? '当前活动段会保持展开' : `${isCollapsed ? '展开' : '收起'}本段解读`}
          disabled={active || status === 'streaming'}
          onClick={e => { e.stopPropagation(); onToggleCollapsed() }}
        >
          <svg viewBox="0 0 16 16" aria-hidden="true">
            <path d="m5.5 3.5 4.5 4.5-4.5 4.5" />
          </svg>
        </button>
        <button className="card-jump" onClick={onClick}
          aria-label={`在代码中定位 ${meta.title}，第 ${meta.start_line} 到 ${meta.end_line} 行`}>
          <span className={`chip chip-${meta.kind}`}>{KIND_LABEL[meta.kind] || meta.kind}</span>
          <span className="card-title" title={meta.title}>{meta.title}</span>
          <span className="card-lines">{meta.start_line}~{meta.end_line} 行</span>
        </button>
        <span className="card-tools">
          {mode && (text || status === 'streaming') && (
            <span className={`chip chip-mode-${mode}`} title={`当前为${MODE_NAME[mode]}解读`}>
              {MODE_LABEL[mode]}
            </span>
          )}
          {status === 'done' && cached && <span className="chip chip-cache" title="来自本地缓存">缓存</span>}
          {isCollapsed && (
            <span className={`seg-card-collapsed-status status-${status}`}>
              {collapsedStatus}
            </span>
          )}
          {status === 'streaming' && (
            <span className="seg-card-progress" role="status" aria-label="正在生成本段解读">
              <span className="spinner" aria-hidden="true" />
            </span>
          )}
          {status === 'done' && (
            <>
              <button className="icon-btn" title="复制解读" onClick={copy}>⧉</button>
              {mode && (
                <button className="icon-btn" title={`切换为${MODE_NAME[otherMode]}解读（已生成过则瞬间切换）`}
                  onClick={switchMode} disabled={explaining || !ready}>
                  {MODE_LABEL[otherMode]}
                </button>
              )}
              <button className="icon-btn" title={`按${MODE_NAME[mode ?? 'simple']}重新生成本段`}
                onClick={regen} disabled={explaining || !ready}>↺</button>
            </>
          )}
        </span>
      </div>
      {!isCollapsed && (
      <div id={bodyId} className="card-body md seg-card-body">
        {showModeButtons ? (
          <div className="seg-mode-actions">
            <button className="btn-sm" disabled={!ready || explaining} onClick={explainAs('simple')}
              title="用 2~4 句通俗的话概括这段代码">
              简单解读
              {meta.cached_simple && <span className="cache-dot" title="已有缓存，瞬间返回" />}
            </button>
            <button className="btn-sm" disabled={!ready || explaining} onClick={explainAs('detailed')}
              title="按行号逐条讲解这段代码（依然通俗易懂）">
              逐行解读
              {meta.cached_detailed && <span className="cache-dot" title="已有缓存，瞬间返回" />}
            </button>
          </div>
        ) : text ? (
          status === 'streaming'
            ? <div className="stream-plain">{text}</div>
            : <ReactMarkdown remarkPlugins={[remarkGfm]}>{text}</ReactMarkdown>
        ) : queued ? (
          <div title="排队等待解读…" aria-label="排队等待解读">
            <div className="skl" />
            <div className="skl" />
          </div>
        ) : (
          <span className="dim">{status === 'streaming' ? '生成中…' : '尚未解读'}</span>
        )}
        {status === 'streaming' && <span className="caret" />}
        {status === 'cancelled' && !text && <span className="dim">已停止，可重新生成</span>}
        {status === 'error' && <span className="inline-error" role="alert">{state.error || '生成失败，可重试'}</span>}
        {evidence.length > 0 && (
          <div className="evidence-list" aria-label="本段解读引用的代码证据">
            {evidence.map((item, index) => (
              <button key={`${item.path}:${item.start_line}:${index}`} className="evidence-item"
                onClick={() => onOpenEvidence?.(item)}
                title={item.content || `${item.path}:${item.start_line}`}>
                <span className="evidence-id">{item.id || `E${index + 1}`}</span>
                <span>{item.symbol || item.path.split(/[\\/]/).pop()}</span>
                <span className="evidence-loc">{item.path}:{item.start_line}</span>
              </button>
            ))}
          </div>
        )}
      </div>
      )}
    </article>
  )
}

export default memo(SegmentCard)
