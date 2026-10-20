import type { Feedback } from './types'

/** Legacy records are interpreted without rewriting historical evidence. */
export function reviewKind(fb: Feedback): NonNullable<Feedback['kind']> {
  if (fb.kind) return fb.kind
  if (fb.source === 'VLM') return 'content_review'
  if (fb.source === 'HUMAN') {
    return !fb.passed && fb.issues?.length === 1 && fb.issues[0].startsWith('自动诊断并修复：')
      ? 'repair_request' : 'human_review'
  }
  if (fb.model?.startsWith('science:')) return 'science_review'
  const unavailable = ['VLM 审查调用失败', '科学源码核对未完成', '审核意见定位补充失败',
    '审核意见缺少有效定位', '渲染产物没有可用的抽帧']
  return fb.issues?.some(issue => unavailable.some(prefix => issue.startsWith(prefix)))
    ? 'review_unavailable' : 'system_error'
}

export function isContentReview(fb: Feedback): boolean {
  return reviewKind(fb) === 'content_review'
}
