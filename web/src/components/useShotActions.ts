import { useState } from 'react'
import { App } from 'antd'

import { api, errorText } from '../api'
import type { Shot } from '../types'

/**
 * 一个分镜上的三种人工操作，抽成 hook 供两处复用。
 *
 * 为什么要抽出来：表格行里要给「待人工」的镜头一个**快速放行**按钮，
 * 而详情面板里也要有同一组按钮。两处各写一份的话，迟早会出现
 * 「从表格放行」与「从详情放行」行为不一致（比如只有一处刷新了数据）。
 *
 * 三种操作的分工（审核台的核心心智模型）：
 *   - **放行**：熔断镜头的出口。接受当前效果继续走，不再自动重试。
 *   - **打回**：画面不对。意见会回灌给编码智能体重写代码再重渲。
 *   - **编辑文案**：narration/visual_brief 不对。直接改分镜字段再重渲，
 *     比整镜重写更省，也保留导演的原始意图。
 */
export function useShotActions(jobId: string, shot: Shot, onChanged: () => void) {
  const { message } = App.useApp()
  const [busy, setBusy] = useState(false)

  /** 统一处理「忙碌态 + 成功提示 + 失败提示 + 回源」。返回是否成功。 */
  async function run(fn: () => Promise<string>): Promise<boolean> {
    setBusy(true)
    try {
      const msg = await fn()
      message.success(msg)
      // 操作成功后立刻回源任务明细：事件里不含分镜状态，
      // 不等回源的话用户点完按钮会觉得「没反应」。
      onChanged()
      return true
    } catch (err) {
      message.error(errorText(err))
      return false
    } finally {
      setBusy(false)
    }
  }

  return {
    busy,
    approve: () =>
      run(async () => {
        const resp = await api.approve(jobId, shot.shot_id)
        return resp.compose_enqueued
          ? '已放行。全部镜头都已通过，成片开始合成。'
          : '已放行该镜头。'
      }),
    reject: (comment: string) =>
      run(async () => {
        await api.reject(jobId, shot.shot_id, { comment: comment.trim() })
        return '已打回，正在重写并重渲该镜头。'
      }),
    patch: (narration: string, visualBrief: string, redo: boolean, comment: string, backgroundStyle?: string) =>
      run(async () => {
        const resp = await api.patchShot(jobId, shot.shot_id, {
          narration,
          visual_brief: visualBrief,
          redo,
          comment: comment.trim() || undefined,
          // 只有真的改了才带上：把"没动这一项"也发过去，
          // 会让后端无法区分"用户想清空"与"用户没碰"。
          ...(backgroundStyle !== undefined && backgroundStyle !== shot.background_style
            ? { background_style: backgroundStyle }
            : {}),
        })
        const changed = resp.changed.length ? resp.changed.join('、') : '无'
        return redo ? `已保存（${changed}）并开始重做。` : `已保存（${changed}），未触发重做。`
      }),
  }
}
