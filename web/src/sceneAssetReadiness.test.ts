import { describe, expect, it } from 'vitest'
import { inspectSceneAssets } from './sceneAssetReadiness'
import type { VisualAsset } from './types'

const image = (subject: string, pose: string, width = 100, mime_type: VisualAsset['mime_type'] = 'image/png'): VisualAsset => ({
  asset_id: pose.padEnd(32, 'a').slice(0, 32), filename: `${pose}.png`, mime_type, role: 'character',
  subject, pose, width, height: 120, size_bytes: 100,
})

describe('scene asset readiness', () => {
  it('reports a matching pose set without treating artwork as reviewed', () => {
    const report = inspectSceneAssets([image('小葵', '待机'), image('小葵', '思考')])
    expect(report.groups[0]).toMatchObject({ subject: '小葵', poses: ['待机', '思考'], switchable: true })
    expect(report.warnings).toEqual([])
    expect(report.notes.some(note => note.includes('姿态可在镜头关键帧中切换'))).toBe(true)
  })

  it('explains canvas, identity and transparency gaps', () => {
    const report = inspectSceneAssets([image('小葵', '待机'), image('小葵', '思考', 120), image('', '讲解', 100, 'image/jpeg')])
    expect(report.groups[0].switchable).toBe(false)
    expect(report.warnings.join(' ')).toContain('尺寸不同')
    expect(report.warnings.join(' ')).toContain('同一对象名称')
    expect(report.warnings.join(' ')).toContain('透明通道')
  })

  it('does not claim reference artwork exists when nothing is selected', () => {
    expect(inspectSceneAssets([]).warnings[0]).toContain('本次未选分镜图片')
  })
})
