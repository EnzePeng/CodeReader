async function parseError(resp: Response): Promise<string> {
  try {
    const j = await resp.json()
    return j.detail || j.message || resp.statusText
  } catch {
    return `${resp.status} ${resp.statusText}`
  }
}

export async function getJSON<T>(url: string): Promise<T> {
  const resp = await fetch(url)
  if (!resp.ok) throw new Error(await parseError(resp))
  return resp.json()
}

export async function postJSON<T>(url: string, body: unknown): Promise<T> {
  const resp = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!resp.ok) throw new Error(await parseError(resp))
  return resp.json()
}

export type SSEHandler = (event: string, data: any) => void

/** POST 请求 + SSE 流式响应解析。 */
export async function streamSSE(
  url: string,
  body: unknown,
  onEvent: SSEHandler,
  signal?: AbortSignal,
): Promise<void> {
  const resp = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    signal,
  })
  if (!resp.ok || !resp.body) throw new Error(await parseError(resp))

  const reader = resp.body.getReader()
  const decoder = new TextDecoder()
  let buf = ''
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    buf += decoder.decode(value, { stream: true })
    let idx: number
    while ((idx = buf.indexOf('\n\n')) >= 0) {
      const block = buf.slice(0, idx)
      buf = buf.slice(idx + 2)
      let event = 'message'
      let data = ''
      for (const line of block.split('\n')) {
        if (line.startsWith('event:')) event = line.slice(6).trim()
        else if (line.startsWith('data:')) data += line.slice(5).trim()
      }
      if (data) {
        try {
          onEvent(event, JSON.parse(data))
        } catch {
          /* 忽略无法解析的块 */
        }
      }
    }
  }
}

export function encodePath(path: string): string {
  return encodeURIComponent(path)
}
