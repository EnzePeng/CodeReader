/** SSE 事件作用域解析：把 envelope 归一到它实际作用的解读目标。 */

/** 归一化事件作用域：剥离旧协议遗留的 "segment:" 前缀。 */
export function normalizeScope(value: unknown): string {
  const scope = typeof value === 'string' ? value : ''
  return scope.startsWith('segment:') ? scope.slice('segment:'.length) : scope
}

/**
 * 解析 SSE 事件的实际作用域。
 *
 * 优先级：payload.target（"overview" / 段 id / "answer"）> envelope.scope_id
 * （恒为文件相对路径，仅作兜底）> payload.scope_id。
 *
 * 注意：后端每个 envelope 都把 scope_id 设为当前文件路径（StreamSequence
 * 以 relative_path 为 scope），若把 envelope.scope_id 排在 payload.target
 * 前面，所有流式文本会被记到「文件路径」这个错误作用域下，解读面板将静默
 * 丢字——这是本模块存在的原因，任何改动都必须保持 target 优先。
 */
export function resolveEventScope(
  envelope: { scope_id: string | null },
  payload: Record<string, unknown>,
): string {
  const target = typeof payload.target === 'string' ? payload.target : ''
  if (target) return normalizeScope(target)
  const scopeId = typeof envelope.scope_id === 'string' ? envelope.scope_id : ''
  if (scopeId) return normalizeScope(scopeId)
  return normalizeScope(payload.scope_id)
}
