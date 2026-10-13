import type { VisualAsset } from './types'

export interface PoseGroup {
  subject: string
  role: VisualAsset['role']
  poses: string[]
  switchable: boolean
  dimensions: string
}

export interface SceneAssetReadiness {
  groups: PoseGroup[]
  warnings: string[]
  notes: string[]
}

/** Metadata check only: artwork quality and script relevance still need review. */
export function inspectSceneAssets(assets: VisualAsset[]): SceneAssetReadiness {
  const warnings: string[] = []
  const notes: string[] = []
  const grouped = new Map<string, VisualAsset[]>()
  for (const asset of assets) {
    const subject = asset.subject?.trim()
    if (!subject || asset.role === 'background') continue
    const key = `${asset.role}\0${subject}`
    grouped.set(key, [...(grouped.get(key) ?? []), asset])
  }
  const groups = [...grouped.values()].map((items): PoseGroup => {
    const [first] = items
    const subject = first.subject!.trim()
    const dimensions = `${first.width} × ${first.height}`
    const matchingCanvas = items.every(item => item.width === first.width && item.height === first.height)
    const poses = items.map(item => item.pose?.trim() || '默认')
    const uniquePoses = new Set(poses).size === poses.length
    if (items.length > 1 && !matchingCanvas) warnings.push(`${subject} 的图片画布尺寸不同，不能在同一位置切换姿态。`)
    if (items.length > 1 && !uniquePoses) warnings.push(`${subject} 有重复的姿态名称，请区分每张图的动作。`)
    return { subject, role: first.role, poses, switchable: items.length > 1 && matchingCanvas,
      dimensions: matchingCanvas ? dimensions : '尺寸不一致' }
  })
  if (assets.length === 0) {
    warnings.push('本次未选分镜图片。科普图解仍可生成，但画面不会有参考片中的手绘人物与道具质感。')
  } else {
    if (assets.some(asset => asset.role === 'character' && !asset.subject?.trim()))
      warnings.push('有人物图片未填写同一对象名称，系统无法将它与其他姿态归为同一角色。')
    if (assets.some(asset => asset.role === 'character' && asset.mime_type !== 'image/png'))
      warnings.push('人物图片使用 JPEG 时没有透明通道，叠加在纸纹舞台上可能出现矩形底色。')
    if (!assets.some(asset => asset.role === 'prop'))
      notes.push('还没有叙事道具图。需要复现参考片的道具演出时，请按具体脚本导入原创或有权使用的插画。')
    if (!assets.some(asset => asset.role === 'character'))
      notes.push('还没有分镜角色立绘；右下角持续讲解员可独立使用已导入的 Live2D。')
    if (groups.some(group => group.switchable))
      notes.push('同一对象的姿态可在镜头关键帧中切换；姿态含义仍应与旁白节拍对应。')
  }
  return { groups, warnings, notes }
}
