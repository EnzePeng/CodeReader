import {
  beginJob, cancelJob, idleJob, idleResource, loadResource, reduceJobEnvelope,
  rejectResource,
} from '../src/state'

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message)
}

// 旧请求、其他 job_id、重复/倒序 seq 都不能污染当前任务。
let job = beginJob(idleJob(), 1)
let reduced = reduceJobEnvelope(job, { job_id: 'job-a', seq: 1, type: 'status' }, 1)
assert(reduced.accepted, '首个 envelope 应绑定 job_id')
job = reduced.state
assert(job.jobId === 'job-a', '必须绑定服务端 job_id')
assert(!reduceJobEnvelope(job, { job_id: 'job-a', seq: 1, type: 'status' }, 1).accepted,
  '重复 seq 必须丢弃')
assert(!reduceJobEnvelope(job, { job_id: 'job-b', seq: 2, type: 'status' }, 1).accepted,
  '不同 job_id 必须丢弃')

job = beginJob(job, 2)
assert(!reduceJobEnvelope(job, { job_id: 'job-a', seq: 3, type: 'status' }, 1).accepted,
  '旧客户端请求代次必须丢弃')
reduced = reduceJobEnvelope(job, { job_id: 'job-new', seq: 0, type: 'status' }, 2)
assert(reduced.accepted && reduced.state.jobId === 'job-new', '新请求应正常接收')

// 文件打开失败时保留上一份可阅读内容。
const previous = { name: 'stable.py' }
let resource = idleResource(previous)
resource = loadResource(resource)
resource = rejectResource(resource, '读取失败')
assert(resource.data === previous, '资源失败不能清空上一份成功数据')
assert(resource.phase === 'error', '资源应进入 error 终态')

// 用户取消必须退出 running。
job = cancelJob(reduced.state)
assert(job.phase === 'cancelled', '取消必须退出 running')

// eslint-free/no-framework executable test marker.
globalThis.console.log('state tests passed')
