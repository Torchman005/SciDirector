import { describe, expect, it } from 'vitest'
import { renderToStaticMarkup } from 'react-dom/server'
import { createElement } from 'react'
import { ReviewHistory } from './components/ReviewHistory'
import { reviewKind } from './review'
import type { Feedback } from './types'

const base: Feedback = { passed: false, score: 0, logic_score: 0, readability_score: 0,
  pacing_score: 0, aesthetics_score: 0, source: 'SYSTEM', attempt: 8, created_at: '' }
const render = (fb: Feedback) => renderToStaticMarkup(createElement(ReviewHistory, {
  feedbacks: [fb], jobId: 'job', shotId: 'shot',
}))

describe('review records', () => {
  it.each(['repair_request', 'review_unavailable', 'system_error'] as const)('does not score %s', kind => {
    const html = render({ ...base, kind, message: 'record detail' })
    expect(html).not.toContain('0.00')
    expect(html).not.toContain('未通过')
    expect(html).toContain('record detail')
  })
  it('keeps a real zero content score visible', () => {
    const html = render({ ...base, kind: 'content_review', source: 'VLM' })
    expect(html).toContain('总分')
    expect(html).toContain('0.00')
    expect(html).toContain('未通过')
  })
  it('shows only the evaluated dimension for a science check', () => {
    const html = render({ ...base, model: 'science:text-model', logic_score: .2 })
    expect(html).toContain('科学核对')
    expect(html).not.toContain('渲染错误')
    expect(html).not.toContain('可读 0.00')
    expect(html).toContain('0.20')
  })
  it('interprets old automatic repair clicks without inventing a new review', () => {
    const fb = { ...base, source: 'HUMAN', issues: ['自动诊断并修复：根据系统错误修复。'] }
    expect(reviewKind(fb)).toBe('repair_request')
    const html = render(fb)
    expect(html).toContain('已发起修复')
    expect(html).not.toContain('低于下限')
    expect(html).not.toContain('问题：')
  })
})
