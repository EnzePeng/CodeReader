export type ResourcePhase = 'idle' | 'loading' | 'ready' | 'error'

/**
 * 远端资源统一状态。loading/error 都允许保留上一份可用数据，避免一次失败把
 * 正在阅读的文件替换成空白页。
 */
export interface ResourceState<T> {
  phase: ResourcePhase
  data: T | null
  error: string | null
}

export function idleResource<T>(data: T | null = null): ResourceState<T> {
  return { phase: data === null ? 'idle' : 'ready', data, error: null }
}

export function loadResource<T>(state: ResourceState<T>, clear = false): ResourceState<T> {
  return { phase: 'loading', data: clear ? null : state.data, error: null }
}

export function resolveResource<T>(_state: ResourceState<T>, data: T): ResourceState<T> {
  return { phase: 'ready', data, error: null }
}

export function rejectResource<T>(state: ResourceState<T>, error: string): ResourceState<T> {
  return { phase: 'error', data: state.data, error }
}

export type JobPhase = 'idle' | 'running' | 'complete' | 'cancelled' | 'error'

export interface JobState {
  phase: JobPhase
  /** 客户端请求代次；切换文件/项目后，旧闭包携带的代次会被拒绝。 */
  requestKey: number
  /** 首个服务端 envelope 到达后绑定，之后拒绝其他 job_id。 */
  jobId: string | null
  lastSeq: number
  message: string
  error: string | null
}

export interface JobEnvelopeLike {
  job_id: string
  seq: number
  type: string
  payload?: unknown
}

export interface JobReduction {
  accepted: boolean
  state: JobState
}

export function idleJob(requestKey = 0): JobState {
  return {
    phase: 'idle', requestKey, jobId: null, lastSeq: -1, message: '', error: null,
  }
}

export function beginJob(state: JobState, requestKey = state.requestKey + 1): JobState {
  return {
    phase: 'running', requestKey, jobId: null, lastSeq: -1, message: '', error: null,
  }
}

/**
 * 只负责任务身份、顺序和终态；业务 payload 由调用方在 accepted=true 后处理。
 */
export function reduceJobEnvelope(
  state: JobState,
  envelope: JobEnvelopeLike,
  requestKey: number,
): JobReduction {
  if (state.phase !== 'running' || requestKey !== state.requestKey) {
    return { accepted: false, state }
  }
  if (state.jobId !== null && envelope.job_id !== state.jobId) {
    return { accepted: false, state }
  }
  if (!Number.isFinite(envelope.seq) || envelope.seq <= state.lastSeq) {
    return { accepted: false, state }
  }

  const payload = (envelope.payload ?? {}) as Record<string, unknown>
  const next: JobState = {
    ...state,
    jobId: state.jobId ?? envelope.job_id,
    lastSeq: envelope.seq,
  }
  if (envelope.type === 'status') {
    next.message = typeof payload.message === 'string' ? payload.message : state.message
  } else if (envelope.type === 'complete' || envelope.type === 'done') {
    next.phase = 'complete'
    next.message = ''
  } else if (envelope.type === 'cancelled') {
    next.phase = 'cancelled'
    next.message = typeof payload.reason === 'string' ? payload.reason : '已取消'
  } else if (envelope.type === 'error') {
    next.phase = 'error'
    next.message = ''
    next.error = typeof payload.message === 'string' ? payload.message : '任务失败'
  }
  return { accepted: true, state: next }
}

export function cancelJob(state: JobState, message = '已取消'): JobState {
  if (state.phase !== 'running') return state
  return { ...state, phase: 'cancelled', message, error: null }
}

export function failJob(state: JobState, error: string): JobState {
  return { ...state, phase: 'error', message: '', error }
}
