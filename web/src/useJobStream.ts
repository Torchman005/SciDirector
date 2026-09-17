/**
 * 任务实时事件流的 WebSocket 连接管理。
 *
 * 这个 hook 承担阶段四最容易做错的部分，因此把三条纪律写在这里：
 *
 *  1. **重连一律以「快照」为基线，而不是只补事件**。
 *     快照同时包含任务明细、统计与历史事件，是唯一能保证
 *     「重连后看到的状态与刷新页面一致」的东西。
 *     只补事件是不够的：断线期间分镜表本身可能已经变了。
 *
 *  2. **用 seq 检测空洞并自动补发**。
 *     订阅建立与快照生成之间存在极小的竞态窗口。
 *     收到 `seq > lastSeq + 1` 的事件时说明中间丢了事件，
 *     此时主动发 `resync`（带上 after_id）把缺口补上。
 *
 *  3. **重连退避要有抖动**。
 *     固定间隔重连在服务端重启时会让所有客户端同时涌上来（惊群）；
 *     加抖动后重连时间被摊开。
 */

import { useCallback, useEffect, useRef, useState } from 'react'

import { applyEvents, applyEventsWithJob, applySnapshot, initialState } from './stream'
import type { StreamState } from './stream'
import type { ConnectionState, DomainEvent, Job, ServerMessage } from './types'

interface Options {
  jobId: string | null
  /** 实时推送里不含分镜明细，需要时可以顺手回源一次。 */
  onNeedJobRefresh?: () => void
}

interface Result {
  state: StreamState
  connection: ConnectionState
  /** 上一次连接断开的原因（供界面展示）。 */
  lastError: string
  /** 手动触发一次重连（界面上的「重试」按钮）。 */
  reconnect: () => void
  /**
   * 合并一份**回源**得到的任务明细。
   *
   * 存在的理由：实时事件里**不含分镜状态**，人工打回/放行之后
   * 必须回源 `GET /jobs/:id` 才能看到状态变化。
   * 不做这件事的话，用户点完按钮会觉得没反应，直到下一条事件到达。
   *
   * 之所以由 hook 提供而不是让 App 自己 setState：
   * 状态归约的规则（去重、排序、lastSeq）只能有一份实现，
   * 两处各写一套迟早会不一致。
   */
  mergeJob: (job: Job) => void
}

/** 重连退避参数。 */
const BASE_DELAY_MS = 500
const MAX_DELAY_MS = 10_000
const MAX_ATTEMPTS = 20

export function useJobStream({ jobId, onNeedJobRefresh }: Options): Result {
  const [state, setState] = useState<StreamState>(initialState)
  const [connection, setConnection] = useState<ConnectionState>('closed')
  const [lastError, setLastError] = useState('')

  // 用 ref 保存会在回调里读到、但不应触发重连的值。
  const stateRef = useRef(state)
  stateRef.current = state
  const jobRef = useRef(jobId)
  jobRef.current = jobId
  const refreshRef = useRef(onNeedJobRefresh)
  refreshRef.current = onNeedJobRefresh
  const wsRef = useRef<WebSocket | null>(null)
  const attemptRef = useRef(0)
  const timerRef = useRef<number | null>(null)
  /** 手动重连时用来打断当前的退避等待。 */
  const manualRef = useRef(0)

  const clearTimer = () => {
    if (timerRef.current !== null) {
      window.clearTimeout(timerRef.current)
      timerRef.current = null
    }
  }

  const connect = useCallback(() => {
    const id = jobRef.current
    if (!id) {
      setConnection('closed')
      return
    }

    clearTimer()

    const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
    const url = `${proto}//${window.location.host}/ws/jobs/${encodeURIComponent(id)}`

    setConnection(attemptRef.current === 0 ? 'connecting' : 'reconnecting')

    let ws: WebSocket
    try {
      ws = new WebSocket(url)
    } catch (err) {
      setLastError(err instanceof Error ? err.message : String(err))
      scheduleReconnect()
      return
    }
    wsRef.current = ws

    ws.onopen = () => {
      attemptRef.current = 0
      setConnection('open')
      setLastError('')
      // 连接建立后服务端会主动推一份快照，因此这里不需要额外请求。
    }

    ws.onmessage = (ev) => {
      let msg: ServerMessage
      try {
        msg = JSON.parse(ev.data as string) as ServerMessage
      } catch {
        return // 非 JSON 帧直接忽略：服务端不该发，但也没必要让整个连接崩掉
      }

      switch (msg.type) {
        case 'snapshot':
          setState((prev) => applySnapshot(prev, msg.data))
          break

        case 'events': {
          // resync 的回应：只补事件，任务明细不动。
          const events = (msg.data as DomainEvent[]) ?? []
          setState((prev) => applyEvents(prev, events))
          break
        }

        case 'event': {
          const evt = msg.data as DomainEvent
          if (!evt) break
          // 空洞检测：seq 不连续说明中间丢过事件。
          const expected = stateRef.current.lastSeq + 1
          const got = evt.event_id ?? 0
          if (got > expected && stateRef.current.lastSeq > 0) {
            requestResync(ws)
          }
          setState((prev) => applyEvents(prev, [evt]))
          break
        }

        case 'error':
          setLastError(typeof msg.data === 'string' ? msg.data : '服务端返回错误')
          break

        case 'ack':
        case 'pong':
          break
      }
    }

    ws.onerror = () => {
      // onerror 不提供有用信息，真正的诊断在 onclose 里。
      setLastError((prev) => prev || '连接出错')
    }

    ws.onclose = (ev) => {
      wsRef.current = null
      if (jobRef.current !== id) return // 已经切换到别的任务，不再重连
      setConnection('reconnecting')
      if (ev.code !== 1000 && ev.reason) {
        setLastError(ev.reason)
      }
      scheduleReconnect()
      // 断线期间分镜表可能已变化，顺手回源一次任务明细。
      refreshRef.current?.()
    }

    function scheduleReconnect() {
      attemptRef.current += 1
      if (attemptRef.current > MAX_ATTEMPTS) {
        setConnection('closed')
        setLastError((prev) => prev || `重连 ${MAX_ATTEMPTS} 次仍未成功，已停止自动重试`)
        return
      }
      // 指数退避 + 抖动：抖动是必须的，否则服务端重启时所有客户端会同时涌上来。
      const backoff = Math.min(BASE_DELAY_MS * 2 ** (attemptRef.current - 1), MAX_DELAY_MS)
      const jitter = Math.random() * backoff * 0.3
      const token = manualRef.current
      timerRef.current = window.setTimeout(() => {
        // 期间用户手动重连过：放弃这次排期，避免两条连接同时建立。
        if (token !== manualRef.current) return
        connect()
      }, backoff + jitter)
    }
  }, [])

  /** 请求补发缺失的事件（只补事件，不动任务明细）。 */
  function requestResync(ws: WebSocket) {
    if (ws.readyState !== WebSocket.OPEN) return
    ws.send(JSON.stringify({ type: 'resync', after_id: stateRef.current.lastSeq }))
  }

  const reconnect = useCallback(() => {
    manualRef.current += 1
    attemptRef.current = 0
    setLastError('')
    const old = wsRef.current
    wsRef.current = null
    if (old) {
      old.onclose = null // 避免关闭旧连接时又排一次重连
      old.close(1000, 'manual reconnect')
    }
    clearTimer()
    connect()
  }, [connect])

  useEffect(() => {
    if (!jobId) {
      setState(initialState())
      setConnection('closed')
      return
    }

    // 切换任务时重置：把上一个任务的事件带到新任务上是明确的错误。
    attemptRef.current = 0
    setState(initialState())
    connect()

    return () => {
      clearTimer()
      const ws = wsRef.current
      wsRef.current = null
      if (ws) {
        ws.onclose = null
        ws.close(1000, 'unmount')
      }
    }
  }, [jobId, connect])

  const mergeJob = useCallback((job: Job) => {
    // 不带新事件，只替换任务明细 —— 状态归约规则复用同一份实现。
    setState((prev) => applyEventsWithJob(prev, [], job))
  }, [])

  return { state, connection, lastError, reconnect, mergeJob }
}
