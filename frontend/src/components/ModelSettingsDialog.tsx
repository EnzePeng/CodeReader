import { useEffect, useRef, useState } from 'react'
import { getJSON, postJSON } from '../api'
import { useDialogFocus } from '../hooks'

export interface ModelTuningValues {
  ctx_size: number
  n_gpu_layers: number
  cache_type_k: 'q4_0' | 'q8_0' | 'f16'
  cache_type_v: 'q4_0' | 'q8_0' | 'f16'
  parallel: number
  temperature: number
  top_p: number
  top_k: number
  thinking: boolean
}

type SettingKey = keyof ModelTuningValues

interface HardwareInfo {
  platform: string
  cpu_logical_cores: number | null
  system_ram_gb: number | null
  gpus: { name: string; total_vram_gb: number; free_vram_gb: number }[]
  gpu_note: string
}

interface SettingsResponse {
  model: string
  model_size_gb: number | null
  current: ModelTuningValues
  hardware: HardwareInfo
  restart_fields: SettingKey[]
  cache_types: ModelTuningValues['cache_type_k'][]
}

interface Recommendation {
  source: 'model'
  generated_at: string
  values: ModelTuningValues
  summary: string
  rationale: Partial<Record<SettingKey, string>>
  warnings: string[]
  confidence: 'low' | 'medium' | 'high'
}

interface Props {
  modelReady: boolean
  onClose: () => void
  onApplied: () => Promise<unknown>
}

const FIELDS: {
  key: SettingKey
  label: string
  hint: string
  min?: number
  max?: number
  step?: number
  kind?: 'number' | 'cache' | 'boolean'
}[] = [
  { key: 'ctx_size', label: '上下文窗口', hint: '单次请求可容纳的 token 数；越大越占 KV 缓存。', min: 2048, max: 262144, step: 1024 },
  { key: 'n_gpu_layers', label: 'GPU 卸载层数', hint: '999 表示尽可能全部放入 GPU；0 表示仅使用 CPU。', min: 0, max: 999, step: 1 },
  { key: 'cache_type_k', label: 'K 缓存精度', hint: 'q4_0 最省显存，q8_0 较均衡，f16 精度最高。', kind: 'cache' },
  { key: 'cache_type_v', label: 'V 缓存精度', hint: '通常与 K 缓存保持一致；大上下文可选择更低精度。', kind: 'cache' },
  { key: 'parallel', label: '并发槽位', hint: '同时生成的请求数；单用户通常设为 1。', min: 1, max: 8, step: 1 },
  { key: 'temperature', label: '温度', hint: '越低越稳定；代码解释通常适合 0.1–0.3。', min: 0, max: 2, step: 0.05 },
  { key: 'top_p', label: 'Top‑P', hint: '控制候选词累计概率范围，常用 0.8–0.95。', min: 0.05, max: 1, step: 0.05 },
  { key: 'top_k', label: 'Top‑K', hint: '限制候选词数量；0 表示不限制。', min: 0, max: 1000, step: 1 },
  { key: 'thinking', label: '思考模式', hint: '更深入但更慢，并会消耗更多输出 token。', kind: 'boolean' },
]

function errorMessage(value: unknown): string {
  return String((value as Error)?.message || value || '发生未知错误')
}

function valueLabel(value: ModelTuningValues[SettingKey]): string {
  if (typeof value === 'boolean') return value ? '开启' : '关闭'
  return String(value)
}

export default function ModelSettingsDialog({ modelReady, onClose, onApplied }: Props) {
  const dialogRef = useRef<HTMLDivElement>(null)
  const [info, setInfo] = useState<SettingsResponse | null>(null)
  const [draft, setDraft] = useState<ModelTuningValues | null>(null)
  const [recommendation, setRecommendation] = useState<Recommendation | null>(null)
  const [selected, setSelected] = useState<Partial<Record<SettingKey, boolean>>>({})
  const [loading, setLoading] = useState(true)
  const [recommending, setRecommending] = useState(false)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  useDialogFocus(true, dialogRef, onClose)

  const requestRecommendation = async () => {
    if (!modelReady || recommending) return
    setRecommending(true)
    setError('')
    setNotice('')
    try {
      const value = await postJSON<Recommendation>('/api/model-settings/recommend', {})
      setRecommendation(value)
      setSelected(Object.fromEntries(FIELDS.map(field => [field.key, true])))
    } catch (requestError) {
      setError(errorMessage(requestError))
    } finally {
      setRecommending(false)
    }
  }

  useEffect(() => {
    let active = true
    getJSON<SettingsResponse>('/api/model-settings')
      .then(value => {
        if (!active) return
        setInfo(value)
        setDraft(value.current)
        setLoading(false)
        if (modelReady) void requestRecommendation()
      })
      .catch(requestError => {
        if (!active) return
        setError(errorMessage(requestError))
        setLoading(false)
      })
    return () => { active = false }
    // 弹窗每次挂载只加载一次；模型状态用于决定是否自动请求建议。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const updateNumber = (key: SettingKey, value: string) => {
    if (!draft) return
    const number = Number(value)
    if (Number.isFinite(number)) setDraft({ ...draft, [key]: number })
  }

  const fillSelectedRecommendation = () => {
    if (!draft || !recommendation) return
    const next = { ...draft }
    for (const field of FIELDS) {
      if (selected[field.key]) (next as Record<SettingKey, unknown>)[field.key] = recommendation.values[field.key]
    }
    setDraft(next)
    setNotice('已把勾选的建议填入编辑区，尚未保存。')
  }

  const save = async () => {
    if (!draft || saving) return
    setSaving(true)
    setError('')
    setNotice('')
    try {
      const result = await postJSON<SettingsResponse & { restarting: boolean }>('/api/model-settings', draft)
      setInfo(result)
      setDraft(result.current)
      setNotice(result.restarting ? '参数已保存，模型正在按新参数重新加载。' : '参数已保存并生效。')
      await onApplied()
    } catch (requestError) {
      setError(errorMessage(requestError))
    } finally {
      setSaving(false)
    }
  }

  const changed = !!draft && !!info && FIELDS.some(field => draft[field.key] !== info.current[field.key])
  const hardware = info?.hardware

  return (
    <div className="modal-mask" onMouseDown={event => { if (event.target === event.currentTarget) onClose() }}>
      <div ref={dialogRef} className="modal model-settings-modal" role="dialog" aria-modal="true"
        aria-labelledby="model-settings-title" tabIndex={-1}>
        <div className="modal-head">
          <div>
            <span id="model-settings-title">模型参数</span>
            {info && <span className="settings-model-name">{info.model}{info.model_size_gb ? ` · ${info.model_size_gb} GiB` : ''}</span>}
          </div>
          <button className="btn-ghost" onClick={onClose} aria-label="关闭模型参数设置">×</button>
        </div>

        <div className="model-settings-body">
          {loading && <div className="settings-loading" role="status">正在读取模型与硬件信息…</div>}
          {error && <div className="settings-error" role="alert">{error}</div>}
          {notice && <div className="settings-notice" role="status">{notice}</div>}

          {hardware && (
            <section className="hardware-summary" aria-label="本机资源">
              <div><span>GPU</span><strong>{hardware.gpus.length ? hardware.gpus.map(gpu => gpu.name).join(' / ') : '未检测到'}</strong></div>
              <div><span>显存</span><strong>{hardware.gpus.length ? hardware.gpus.map(gpu => `${gpu.free_vram_gb}/${gpu.total_vram_gb} GiB 可用`).join(' / ') : '未知'}</strong></div>
              <div><span>内存</span><strong>{hardware.system_ram_gb ? `${hardware.system_ram_gb} GiB` : '未知'}</strong></div>
              <div><span>CPU</span><strong>{hardware.cpu_logical_cores ? `${hardware.cpu_logical_cores} 线程` : '未知'}</strong></div>
              <p>{hardware.gpu_note}</p>
            </section>
          )}

          {draft && info && (
            <section className="settings-section">
              <div className="settings-section-head">
                <div><h3>当前编辑值</h3><p>推荐值不会自动覆盖；保存前可逐项调整。</p></div>
                <span className="restart-legend"><i />资源参数需重启</span>
              </div>
              <div className="settings-grid">
                {FIELDS.map(field => {
                  const restart = info.restart_fields.includes(field.key)
                  return (
                    <label className="setting-field" key={field.key}>
                      <span className="setting-label">{field.label}{restart && <i title="修改后会重启模型" />}</span>
                      {field.kind === 'cache' ? (
                        <select value={String(draft[field.key])}
                          onChange={event => setDraft({ ...draft, [field.key]: event.target.value })}>
                          {info.cache_types.map(value => <option key={value} value={value}>{value}</option>)}
                        </select>
                      ) : field.kind === 'boolean' ? (
                        <button type="button" className={`settings-switch${draft.thinking ? ' on' : ''}`}
                          role="switch" aria-checked={draft.thinking}
                          onClick={() => setDraft({ ...draft, thinking: !draft.thinking })}>
                          <span />{draft.thinking ? '开启' : '关闭'}
                        </button>
                      ) : (
                        <input type="number" value={Number(draft[field.key])}
                          min={field.min} max={field.max} step={field.step}
                          onChange={event => updateNumber(field.key, event.target.value)} />
                      )}
                      <small>{field.hint}</small>
                    </label>
                  )
                })}
              </div>
            </section>
          )}

          <section className="settings-section recommendation-section">
            <div className="settings-section-head">
              <div><h3>模型推荐</h3><p>模型基于当前默认参数和检测到的本机容量给出建议。</p></div>
              <button className="btn-sm" onClick={requestRecommendation}
                disabled={!modelReady || recommending}>
                {recommending ? '模型分析中…' : recommendation ? '重新生成' : '生成建议'}
              </button>
            </div>
            {!modelReady && <div className="recommendation-empty">模型加载完成后即可生成建议。</div>}
            {modelReady && recommending && !recommendation && (
              <div className="recommendation-empty" role="status">正在分析模型大小、显存、内存与当前参数…</div>
            )}
            {recommendation && (
              <div className="recommendation-result">
                <div className="recommendation-summary">
                  <span>模型建议 · 置信度{recommendation.confidence === 'high' ? '高' : recommendation.confidence === 'medium' ? '中' : '低'}</span>
                  <p>{recommendation.summary}</p>
                </div>
                <div className="recommendation-list">
                  {FIELDS.map(field => (
                    <label key={field.key} className="recommendation-row">
                      <input type="checkbox" checked={selected[field.key] ?? false}
                        onChange={event => setSelected({ ...selected, [field.key]: event.target.checked })} />
                      <span className="recommendation-name">{field.label}</span>
                      <span className="recommendation-value">
                        {info ? <del>{valueLabel(info.current[field.key])}</del> : null}
                        <strong>{valueLabel(recommendation.values[field.key])}</strong>
                      </span>
                      <small>{recommendation.rationale[field.key]}</small>
                    </label>
                  ))}
                </div>
                {recommendation.warnings.length > 0 && (
                  <ul className="recommendation-warnings">
                    {recommendation.warnings.map((warning, index) => <li key={index}>{warning}</li>)}
                  </ul>
                )}
                <button className="btn-sm apply-recommendation" onClick={fillSelectedRecommendation}>
                  填入已选建议
                </button>
              </div>
            )}
          </section>
        </div>

        <div className="modal-foot settings-actions">
          <button className="btn-ghost" disabled={!info || saving}
            onClick={() => { if (info) { setDraft(info.current); setNotice('已恢复为当前生效值。') } }}>
            恢复当前值
          </button>
          <div>
            <button className="btn-sm" onClick={onClose}>取消</button>
            <button className="btn-primary" disabled={!draft || !changed || saving} onClick={save}>
              {saving ? '保存中…' : '保存并应用'}
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}
