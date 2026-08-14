export interface Health {
  app_version: string
  model: string
  llama: { ready: boolean; phase: string; detail: string }
  /** 思考模式：enabled = 配置开关；supported = 当前模型是否为思考型 */
  thinking: { enabled: boolean; supported: boolean }
}

export interface FileEntry {
  name: string
  ext: string
  size: number
  is_code: boolean
}

export interface BrowseResult {
  path: string
  dirs: { name: string }[]
  files: FileEntry[]
}

export interface ProjectSession {
  project_id: string
  /** 仅用于显示；项目内 API 一律使用 project_id + relative_path。 */
  root: string
  name: string
  index_status?: IndexStatus
}

export interface IndexStatus {
  state: 'idle' | 'building' | 'ready' | 'error' | string
  message?: string
  files_indexed?: number
  files_total?: number
  updated_at?: number
  languages?: Record<string, number>
  parse_errors?: Record<string, string>
  skipped_files?: string[]
}

export interface FileInfo {
  /** 兼容字段也只包含项目内相对路径，绝不包含服务端绝对根目录。 */
  path?: string
  relative_path: string
  name: string
  language: string
  encoding: string
  line_count: number
  content: string
}

export interface OutlineNode {
  kind: string
  name: string
  start_line: number
  end_line: number
  children: OutlineNode[]
}

/** 解读模式：simple = 简单版（通俗概括），detailed = 逐行版 */
export type ExplainMode = 'simple' | 'detailed'

/** 解读范围：auto = 自动全部，manual = 手动选块 */
export type ScopeMode = 'auto' | 'manual'

/** 指定解读某一段（mode 缺省时用请求级全局模式） */
export interface ExplainTarget {
  id: string
  mode?: ExplainMode
}

export interface SegmentMeta {
  id: string
  kind: string
  title: string
  start_line: number
  end_line: number
  cached_simple: boolean
  cached_detailed: boolean
}

export interface Structure {
  path: string
  language: string
  strategy: string
  total_lines: number
  outline: OutlineNode[]
  segments: SegmentMeta[]
  overview_cached: boolean
}

export type SegStatus = 'idle' | 'streaming' | 'done' | 'cancelled' | 'error'

export interface SegState {
  meta: SegmentMeta
  text: string
  status: SegStatus
  cached: boolean
  /** 当前展示文本对应的解读模式；尚未生成过时为 null */
  mode: ExplainMode | null
  error?: string | null
}

export interface Evidence {
  id: string
  path: string
  start_line: number
  end_line: number
  content?: string
  source_hash?: string
  language?: string
  relation?: 'definition' | 'reference' | 'caller' | 'callee' | 'text' | 'file' | string
  symbol?: string
  score?: number
  metadata?: Record<string, unknown>
}

export interface ChatMsg {
  role: 'user' | 'assistant'
  content: string
  evidence?: Evidence[]
  status?: 'streaming' | 'done' | 'cancelled' | 'error'
}

export interface LineRange {
  start: number
  end: number
}
