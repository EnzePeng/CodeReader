import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { Evidence, ExplainMode, FileInfo, ScopeMode, SegState, Structure } from '../types'
import { projectExportUrl } from '../api'
import SegmentCard from './SegmentCard'

interface Props {
  fileInfo: FileInfo | null
  projectId: string
  structure: Structure | null
  overview: { text: string; status: string }
  segOrder: string[]
  segStates: Record<string, SegState>
  explaining: boolean
  error: string
  notice: string
  ready: boolean
  activeSeg: string | null
  explainMode: ExplainMode
  scopeMode: ScopeMode
  evidenceByScope: Record<string, Evidence[]>
  staleConfig: boolean
  onModeChange: (m: ExplainMode) => void
  onScopeChange: (m: ScopeMode) => void
  onCardClick: (id: string) => void
  /** 忽略缓存重新生成全部（自动范围） */
  onExplainAll: () => void
  onCompleteAndExport: () => void
  /** 单段解读：mode 为本次模式，force=true 忽略缓存 */
  onExplainSeg: (id: string, mode: ExplainMode, force: boolean) => void
  /** 生成总览；force=true 忽略缓存重新生成 */
  onOverview: (force: boolean) => void
  onStop: () => void
  onOpenEvidence: (evidence: Evidence) => void
}

const STRATEGY_LABEL: Record<string, string> = {
  ast: 'AST 精准分段',
  indent: '结构分段',
  generic: '通用分段',
}

export default function ExplainPanel(props: Props) {
  const {
    fileInfo, projectId, structure, overview, segOrder, segStates, explaining,
    error, notice, ready, activeSeg, explainMode, scopeMode,
    evidenceByScope, staleConfig,
    onModeChange, onScopeChange, onCardClick, onExplainAll, onCompleteAndExport, onExplainSeg,
    onOverview, onStop, onOpenEvidence,
  } = props

  if (!fileInfo || !structure) {
    return (
      <div className="explain-panel">
        <div className="panel-placeholder">
          <p>选择左侧文件后，这里将逐段生成中文解读</p>
        </div>
      </div>
    )
  }

  const doneCount = segOrder.filter(id => segStates[id]?.status === 'done').length
  const total = segOrder.length
  const manual = scopeMode === 'manual'

  return (
    <div className="explain-panel">
      <div className="panel-head">
        <div className="panel-title-row">
          <span className="panel-file" title={fileInfo.relative_path}>{fileInfo.name}</span>
          <span className="panel-meta">
            {fileInfo.line_count} 行 · {fileInfo.encoding} · {STRATEGY_LABEL[structure.strategy] || structure.strategy}
          </span>
        </div>
        <div className="panel-actions">
          <div className="seg-ctrl" title={manual ? '手动选块时在每个卡片上单独选择模式' : '解读模式'}>
            <button
              className={explainMode === 'simple' ? 'on' : ''}
              disabled={manual}
              onClick={() => onModeChange('simple')}
              title="简单版：每段用 2~4 句通俗概括"
            >简单</button>
            <button
              className={explainMode === 'detailed' ? 'on' : ''}
              disabled={manual}
              onClick={() => onModeChange('detailed')}
              title="逐行版：按行号逐条讲解（依然通俗易懂）"
            >逐行</button>
          </div>
          <div className="seg-ctrl" title="解读范围">
            <button
              className={scopeMode === 'auto' ? 'on' : ''}
              onClick={() => onScopeChange('auto')}
              title="打开文件后自动解读全部分段"
            >自动全部</button>
            <button
              className={scopeMode === 'manual' ? 'on' : ''}
              onClick={() => onScopeChange('manual')}
              title="只解读你在卡片上点选的段，可为每段单独选简单/逐行"
            >手动选块</button>
          </div>
          {explaining ? (
            <button className="btn-sm warn" onClick={onStop}>停止</button>
          ) : manual ? (
            <button className="btn-sm" onClick={() => onOverview(false)} disabled={!ready}
              title="手动模式：只生成文件总览，分段解读请在下方卡片上选择模式">
              生成总览
            </button>
          ) : (
            <button className="btn-sm" onClick={onExplainAll} disabled={!ready}
              title="忽略缓存，按当前模式重新生成全部解读">
              全部重新生成
            </button>
          )}
          <a className="btn-sm" href={projectExportUrl(projectId, fileInfo.relative_path)}
            download title="导出当前已生成的解读为 Markdown 报告">
            导出当前
          </a>
          {doneCount < total && (
            <button className="btn-sm" onClick={onCompleteAndExport}
              disabled={!ready || explaining} title="补全尚未生成的段落后导出完整报告">
              补全后导出
            </button>
          )}
          <span className="progress-text" role="status" aria-live="polite">{doneCount}/{total} 段</span>
        </div>
        {explaining && total > 0 && (
          <div className="progress-bar" role="progressbar" aria-label="解读进度"
            aria-valuemin={0} aria-valuemax={total} aria-valuenow={doneCount}>
            <div style={{ width: `${(doneCount / total) * 100}%` }} />
          </div>
        )}
      </div>

      {!ready && !notice && (
        <div className="banner busy">
          {manual
            ? '模型加载中（首次启动约需 1 分钟），就绪后可点击卡片解读…'
            : '模型加载中，就绪后自动开始解读（首次启动约需 1 分钟）…'}
        </div>
      )}
      {notice && <div className="banner busy" role="status" aria-live="polite">{notice}</div>}
      {staleConfig && !explaining && (
        <div className="banner busy" role="status">
          当前内容由旧模型或旧思考配置生成。
          <button className="btn-sm" onClick={onExplainAll} disabled={!ready}>刷新当前文件</button>
        </div>
      )}
      {error && <div className="banner err" role="alert">{error}</div>}

      <div className="cards">
        <div className={`card overview-card${overview.status === 'streaming' ? ' streaming' : ''}${!overview.text && overview.status !== 'streaming' && explaining ? ' queued' : ''}`}>
          <div className="card-head">
            <span className="chip chip-overview">总览</span>
            <span className="card-title">文件总览</span>
            <span className="card-tools">
              {overview.status === 'done' && (
                <button className="icon-btn" title="重新生成总览"
                  onClick={e => { e.stopPropagation(); onOverview(true) }}>↺</button>
              )}
            </span>
          </div>
          <div className="card-body md">
            {overview.text
              ? overview.status === 'streaming'
                ? <div className="stream-plain">{overview.text}</div>
                : <ReactMarkdown remarkPlugins={[remarkGfm]}>{overview.text}</ReactMarkdown>
              : <span className="dim">
                  {overview.status === 'streaming' ? '生成中…'
                    : explaining ? '排队中…'
                    : manual ? '尚未生成，点击上方「生成总览」' : '尚未生成'}
                </span>}
            {overview.status === 'streaming' && <span className="caret" />}
            {(evidenceByScope.overview ?? []).length > 0 && (
              <div className="evidence-list" aria-label="文件总览引用的代码证据">
                {evidenceByScope.overview.map((item, index) => (
                  <button key={`${item.path}:${item.start_line}:${index}`} className="evidence-item"
                    onClick={() => onOpenEvidence(item)} title={item.content || item.path}>
                    <span className="evidence-id">{item.id || `E${index + 1}`}</span>
                    <span>{item.symbol || item.path.split(/[\\/]/).pop()}</span>
                    <span className="evidence-loc">{item.path}:{item.start_line}</span>
                  </button>
                ))}
              </div>
            )}
          </div>
        </div>

        {segOrder.map(id => {
          const s = segStates[id]
          if (!s) return null
          return (
            <SegmentCard
              key={id}
              state={s}
              active={activeSeg === id}
              explaining={explaining}
              ready={ready}
              manual={manual}
              evidence={evidenceByScope[id] ?? []}
              onOpenEvidence={onOpenEvidence}
              onClick={() => onCardClick(id)}
              onExplain={(mode, force) => onExplainSeg(id, mode, force)}
            />
          )
        })}
      </div>
    </div>
  )
}
