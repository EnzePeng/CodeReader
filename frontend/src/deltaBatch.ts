/**
 * 将高频 token delta 合并到一帧附近再提交，避免每个 token 都触发 React 树与
 * Markdown 渲染。flush 会在 complete/error/cancelled 前同步提交剩余文本。
 */
export interface DeltaBatch {
  push: (scope: string, text: string) => void
  flush: () => void
  cancel: () => void
}

export function createDeltaBatch(
  commit: (chunks: ReadonlyMap<string, string>) => void,
  intervalMs = 40,
): DeltaBatch {
  const pending = new Map<string, string>()
  let timer: number | null = null

  const flush = () => {
    if (timer !== null) {
      window.clearTimeout(timer)
      timer = null
    }
    if (!pending.size) return
    const snapshot = new Map(pending)
    pending.clear()
    commit(snapshot)
  }

  return {
    push(scope, text) {
      if (!text) return
      pending.set(scope, (pending.get(scope) ?? '') + text)
      if (timer === null) timer = window.setTimeout(flush, intervalMs)
    },
    flush,
    cancel() {
      if (timer !== null) window.clearTimeout(timer)
      timer = null
      pending.clear()
    },
  }
}
