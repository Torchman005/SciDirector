import type { StyleGuide } from './types'

/** fy owns its palette/background. Send only its ID, never a browser source. */
export function generationStyle(values: { animation_style: 'precise' | 'anime' | 'fy'; fy_style_id?: string; preset: string; background_style: string }, sceneAssetIds: string[]): StyleGuide {
  if (values.animation_style === 'fy') return { animation_style: 'fy', fy_style_id: values.fy_style_id }
  return { animation_style: values.animation_style, preset: values.preset, background_style: values.background_style,
    scene_assets: sceneAssetIds.map(asset_id => ({ asset_id })) }
}
