import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { ExplainMode, FileInfo, ScopeMode, SegState, Structure } from '../types'
import SegmentCard from './SegmentCard'
import { encodePath } from '../api'

interface Props {
  fileInfo: FileInfo | null
  projectRoot: string
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
  onModeChange: (m: ExplainMode) => void
  onScopeChange: (m: ScopeMode) => void
  onCardClick: (id: string) => void
  /** 忽略缓存重新生成全部（自动范围） */
  onExplainAll: () => void
  /** 单段解读：mode 为本次模式，force=true 忽略缓存 */
  onExplainSeg: (id: string, mode: ExplainMode, force: boolean) => void
  /** 生成总览；force=true 忽略缓存重新生成 */
  onOverview: (force: boolean) => void
  onStop: () => void
}

const STRATEGY_LABEL: Record<string, string> = {
  ast: 'AST 精准分段',
  indent: '结构分段',
  generic: '通用分段',
}

export default function ExplainPanel(props: Props) {
  const {
    fileInfo, projectRoot, structure, overview, segOrder, segStates, explaining,
    error, notice, ready, activeSeg, explainMode, scopeMode,
    onModeChange, onScopeChange, onCardClick, onExplainAll, onExplainSeg,
    onOverview, onStop,
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
          <span className="panel-file" title={fileInfo.path}>{fileInfo.name}</span>
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
          <a className="btn-sm" href={`/api/export?path=${encodePath(fileInfo.path)}&project_root=${encodePath(projectRoot)}`}
            download title="导出当前已生成的解读为 Markdown 报告">
            导出 MD
          </a>
          <span className="progress-text">{doneCount}/{total} 段</span>
        </div>
        {explaining && total > 0 && (
          <div className="progress-bar"><div style={{ width: `${(doneCount / total) * 100}%` }} /></div>
        )}
      </div>

      {!ready && !notice && (
        <div className="banner busy">
          {manual
            ? '模型加载中（首次启动约需 1 分钟），就绪后可点击卡片解读…'
            : '模型加载中，就绪后自动开始解读（首次启动约需 1 分钟）…'}
        </div>
      )}
      {notice && <div className="banner busy">{notice}</div>}
      {error && <div className="banner err">{error}</div>}

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
              ? <ReactMarkdown remarkPlugins={[remarkGfm]}>{overview.text}</ReactMarkdown>
              : <span className="dim">
                  {overview.status === 'streaming' ? '生成中…'
                    : explaining ? '排队中…'
                    : manual ? '尚未生成，点击上方「生成总览」' : '尚未生成'}
                </span>}
            {overview.status === 'streaming' && <span className="caret" />}
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
              onClick={() => onCardClick(id)}
              onExplain={(mode, force) => onExplainSeg(id, mode, force)}
            />
          )
        })}
      </div>
    </div>
  )
}
