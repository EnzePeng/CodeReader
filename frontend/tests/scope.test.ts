import { normalizeScope, resolveEventScope } from '../src/scope'

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message)
}

// 后端每个 envelope 的 scope_id 恒为文件相对路径；payload.target 必须优先，
// 否则流式文本会被记到「文件路径」作用域下并在提交时静默丢弃。
let scope = resolveEventScope({ scope_id: 'backend/app/api.py' }, { target: 'overview' })
assert(scope === 'overview', '总览 delta 的 target 必须优先于 envelope.scope_id')
scope = resolveEventScope({ scope_id: 'backend/app/api.py' }, { target: 's3' })
assert(scope === 's3', '分段 delta 的段 id 应正确解析')
scope = resolveEventScope({ scope_id: 'backend/app/api.py' }, { target: 'answer' })
assert(scope === 'answer', 'chat 的 answer 作用域应正确解析')

// 无 target 时回退 envelope.scope_id / payload.scope_id，保持兼容。
scope = resolveEventScope({ scope_id: 'backend/app/api.py' }, {})
assert(scope === 'backend/app/api.py', '缺少 target 时回退 envelope.scope_id')
scope = resolveEventScope({ scope_id: null }, { scope_id: 'legacy-id' })
assert(scope === 'legacy-id', '缺少 target 与 envelope.scope_id 时回退 payload.scope_id')
scope = resolveEventScope({ scope_id: null }, {})
assert(scope === '', '无任何作用域时返回空串')

// 旧协议 "segment:" 前缀兼容。
assert(normalizeScope('segment:s5') === 's5', '剥离 segment: 前缀')
assert(normalizeScope('s5') === 's5', '普通 id 原样返回')
assert(normalizeScope(undefined) === '' && normalizeScope(42) === '', '非字符串返回空串')

globalThis.console.log('scope tests passed')
