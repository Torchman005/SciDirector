import { describe, expect, it } from 'vitest'
import { generationStyle } from './generationStyle'

describe('generation style submission', () => {
  it('fy sends the selected style and tenant asset IDs, without legacy appearance controls', () => {
    expect(generationStyle({ animation_style: 'fy', fy_style_id: 'X17', preset: 'paper', background_style: 'paper_stage' }, ['old-image']))
      .toEqual({ animation_style: 'fy', fy_style_id: 'X17', scene_assets: [{ asset_id: 'old-image' }] })
  })
  it('fy without illustrations stays compatible with old submissions', () => {
    expect(generationStyle({ animation_style: 'fy', fy_style_id: 'X17', preset: 'paper', background_style: 'paper_stage' }, []))
      .toEqual({ animation_style: 'fy', fy_style_id: 'X17' })
  })
  it('switching back preserves the old workflow without an inactive fy selection', () => {
    expect(generationStyle({ animation_style: 'anime', fy_style_id: 'X17', preset: 'paper', background_style: 'paper_stage' }, ['image']))
      .toEqual({ animation_style: 'anime', preset: 'paper', background_style: 'paper_stage', scene_assets: [{ asset_id: 'image' }] })
  })
})
