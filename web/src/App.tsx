import { useEffect, useState } from 'react'

/**
 * SciDirector 分镜审核台。
 *
 * 阶段一：仅提供服务联通性自检，用于验证「前端 -> Go 网关 -> Python 大脑」三段链路。
 * 阶段四：实现完整的分镜表、视频预览、打回重做与 WebSocket 实时进度。
 *
 * 之所以现在就放一个能跑的页面而不是空目录：
 * docker-compose 里已经有 web 服务，留空会让 `docker compose up` 直接失败；
 * 而一个能自检联通性的最小页面，能在联调早期就把问题定位到具体是哪一段断的。
 */

type HealthState = 'checking' | 'ok' | 'degraded' | 'down'

interface HealthPayload {
  status: string
  version: string
  components?: Record<string, string>
}

export function App() {
  const [apiHealth, setApiHealth] = useState<HealthState>('checking')
  const [apiDetail, setApiDetail] = useState<HealthPayload | null>(null)
  const [error, setError] = useState<string>('')

  useEffect(() => {
    let cancelled = false

    async function probe() {
      try {
        const resp = await fetch('/api/v1/../readyz'.replace('/api/v1/..', ''), {
          headers: { Accept: 'application/json' },
        })
        const body = (await resp.json()) as { ok?: boolean; data?: HealthPayload } & HealthPayload
        if (cancelled) return
        const payload = body.data ?? body
        setApiDetail(payload as HealthPayload)
        if (resp.ok && body.ok !== false) {
          setApiHealth(payload.status === 'degraded' ? 'degraded' : 'ok')
        } else {
          setApiHealth('down')
        }
      } catch (err) {
        if (cancelled) return
        setApiHealth('down')
        setError(err instanceof Error ? err.message : String(err))
      }
    }

    void probe()
    return () => {
      cancelled = true
    }
  }, [])

  const badge: Record<HealthState, { text: string; color: string }> = {
    checking: { text: '检测中…', color: '#8a94a6' },
    ok: { text: '正常', color: '#2ecc71' },
    degraded: { text: '降级', color: '#f1c40f' },
    down: { text: '不可达', color: '#e74c3c' },
  }

  const current = badge[apiHealth]

  return (
    <main
      style={{
        fontFamily: 'system-ui, "Noto Sans CJK SC", sans-serif',
        background: '#0B1020',
        color: '#E6ECFF',
        minHeight: '100vh',
        margin: 0,
        padding: '48px 32px',
        lineHeight: 1.7,
      }}
    >
      <h1 style={{ margin: 0, fontSize: 28 }}>SciDirector · 科学视频导演</h1>
      <p style={{ color: '#8a94a6', marginTop: 8 }}>
        多智能体架构：导演 → 编码 → 渲染 → 视觉审查 → 人类反馈闭环
      </p>

      <section
        style={{
          marginTop: 32,
          padding: 20,
          border: '1px solid #1e2740',
          borderRadius: 12,
          background: '#0F1730',
          maxWidth: 720,
        }}
      >
        <h2 style={{ fontSize: 18, marginTop: 0 }}>链路自检</h2>
        <p>
          Go 网关（<code>/readyz</code>）：
          <strong style={{ color: current.color, marginLeft: 8 }}>{current.text}</strong>
        </p>
        {apiDetail && (
          <pre
            style={{
              background: '#0B1020',
              padding: 12,
              borderRadius: 8,
              overflowX: 'auto',
              fontSize: 13,
              color: '#9fb3d9',
            }}
          >
            {JSON.stringify(apiDetail, null, 2)}
          </pre>
        )}
        {error && <p style={{ color: '#e74c3c' }}>错误：{error}</p>}
      </section>

      <section style={{ marginTop: 24, color: '#8a94a6', maxWidth: 720 }}>
        <h2 style={{ fontSize: 18, color: '#E6ECFF' }}>阶段进度</h2>
        <ul>
          <li>阶段一 · 环境与骨架 —— 已完成</li>
          <li>阶段二 · Python 多智能体核心 —— 进行中</li>
          <li>阶段三 · Go 编排与媒体处理 —— 待办</li>
          <li>阶段四 · 分镜审核台（本页面）—— 待办</li>
        </ul>
      </section>
    </main>
  )
}
