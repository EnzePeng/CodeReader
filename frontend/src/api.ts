async function parseError(resp: Response): Promise<string> {
  try {
    const j = await resp.json()
    return j?.error?.message || j.detail?.message || j.detail || j.message || resp.statusText
  } catch {
    return `${resp.status} ${resp.statusText}`
  }
}

let devSessionPromise: Promise<void> | null = null

async function ensureDevSession(): Promise<void> {
  if (!import.meta.env.DEV) return
  if (!devSessionPromise) {
    devSessionPromise = fetch('/__codereader_session', { credentials: 'same-origin' })
      .then(response => {
        if (!response.ok) throw new Error(`开发会话初始化失败：${response.status}`)
      })
      .catch(error => {
        // 允许后续请求重试 bootstrap，而不是永久缓存一次临时失败。
        devSessionPromise = null
        throw error
      })
  }
  return devSessionPromise
}

export async function getJSON<T>(url: string): Promise<T> {
  await ensureDevSession()
  const resp = await fetch(url, { credentials: 'same-origin' })
  if (!resp.ok) throw new Error(await parseError(resp))
  return resp.json()
}

export async function postJSON<T>(url: string, body: unknown): Promise<T> {
  await ensureDevSession()
  const resp = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    credentials: 'same-origin',
  })
  if (!resp.ok) throw new Error(await parseError(resp))
  return resp.json()
}

export type SSEHandler = (event: string, data: any) => void

export interface SSEEnvelope<T = Record<string, unknown>> {
  job_id: string
  seq: number
  type: string
  scope_id: string | null
  payload: T
}

export type JobSSEHandler = (envelope: SSEEnvelope) => void

/** POST 请求 + SSE 流式响应解析。 */
export async function streamSSE(
  url: string,
  body: unknown,
  onEvent: SSEHandler,
  signal?: AbortSignal,
): Promise<void> {
  await ensureDevSession()
  const resp = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    signal,
    credentials: 'same-origin',
  })
  if (!resp.ok || !resp.body) throw new Error(await parseError(resp))

  const reader = resp.body.getReader()
  const decoder = new TextDecoder()
  let buf = ''
  const consume = (block: string) => {
    let event = 'message'
    let data = ''
    for (const rawLine of block.split('\n')) {
      const line = rawLine.endsWith('\r') ? rawLine.slice(0, -1) : rawLine
      if (line.startsWith('event:')) event = line.slice(6).trim()
      else if (line.startsWith('data:')) data += line.slice(5).trim()
    }
    if (data) {
      try {
        onEvent(event, JSON.parse(data))
      } catch {
        /* 忽略无法解析的块；业务层不会接收半截 JSON。 */
      }
    }
  }

  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    buf += decoder.decode(value, { stream: true })
    // 统一 CRLF，避免 Windows/代理返回 \r\n 时无法识别事件边界。
    buf = buf.replace(/\r\n/g, '\n')
    let idx: number
    while ((idx = buf.indexOf('\n\n')) >= 0) {
      const block = buf.slice(0, idx)
      buf = buf.slice(idx + 2)
      consume(block)
    }
  }
  buf += decoder.decode()
  if (buf.trim()) consume(buf)
}

function isEnvelope(data: unknown): data is SSEEnvelope {
  if (!data || typeof data !== 'object') return false
  const value = data as Record<string, unknown>
  return typeof value.job_id === 'string'
    && typeof value.seq === 'number'
    && typeof value.type === 'string'
}

/**
 * 新协议统一入口。过渡期只兼容“读取”旧 event/data，调用方始终发送
 * project_id + relative_path，不再发送旧的绝对文件路径。
 */
export async function streamJobSSE(
  url: string,
  body: unknown,
  onEnvelope: JobSSEHandler,
  signal?: AbortSignal,
): Promise<void> {
  const legacyJob = `legacy-${Date.now()}-${Math.random().toString(36).slice(2)}`
  let legacySeq = -1
  return streamSSE(url, body, (event, data) => {
    if (isEnvelope(data)) {
      onEnvelope({
        ...data,
        scope_id: typeof data.scope_id === 'string' ? data.scope_id : null,
        payload: data.payload && typeof data.payload === 'object'
          ? data.payload as Record<string, unknown>
          : {},
      })
      return
    }
    // 旧协议数据仅在本地包装，不改变事件含义，便于迁移期 UI 解析。
    onEnvelope({
      job_id: legacyJob,
      seq: ++legacySeq,
      type: event,
      scope_id: typeof data?.id === 'string' ? data.id : null,
      payload: data && typeof data === 'object' ? data : {},
    })
  }, signal)
}

export function encodePath(path: string): string {
  return encodeURIComponent(path)
}

export function encodeRelativePath(path: string): string {
  return path.replace(/\\/g, '/').split('/').filter(Boolean).map(encodeURIComponent).join('/')
}

export function projectFileUrl(projectId: string, relativePath = ''): string {
  const base = `/api/projects/${encodeURIComponent(projectId)}/files/`
  return base + encodeRelativePath(relativePath)
}

export function projectStructureUrl(projectId: string, relativePath: string): string {
  return `/api/projects/${encodeURIComponent(projectId)}/structure/${encodeRelativePath(relativePath)}`
}

export function projectBrowseUrl(projectId: string, relativePath = ''): string {
  const base = `/api/projects/${encodeURIComponent(projectId)}/browse`
  const encoded = encodeRelativePath(relativePath)
  return encoded ? `${base}/${encoded}` : base
}

export function projectExportUrl(projectId: string, relativePath: string): string {
  return `/api/projects/${encodeURIComponent(projectId)}/export/${encodeRelativePath(relativePath)}`
}

export function projectSearchUrl(
  projectId: string,
  query: string,
  kind: 'file' | 'symbol' | 'text',
  limit = 50,
): string {
  const params = new URLSearchParams({ q: query, kind, limit: String(limit) })
  return `/api/projects/${encodeURIComponent(projectId)}/search?${params}`
}
