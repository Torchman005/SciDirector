import { useEffect, useState } from 'react'
import { Alert, Button, Card, Collapse, Form, Select, Space, Tag, Typography } from 'antd'
import { ReloadOutlined } from '@ant-design/icons'
import { api, ApiError, errorText } from '../api'
import type { FyStyleCatalog, FyStyleSource } from '../types'

const { Text, Paragraph } = Typography

export function FyStyleSettings({ styleId, onReady }: { styleId: string; onReady: (ready: boolean) => void }) {
  const [catalog, setCatalog] = useState<FyStyleCatalog>()
  const [source, setSource] = useState<FyStyleSource>()
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const [tick, setTick] = useState(0)
  useEffect(() => {
    let cancelled = false
    onReady(false); setLoading(true); setError(''); setSource(undefined)
    Promise.all([api.fyStyles(), api.fyStyle(styleId)]).then(([list, detail]) => {
      if (cancelled) return
      setCatalog(list); setSource(detail); onReady(true)
    }).catch(err => { if (!cancelled) setError(err instanceof ApiError && err.status === 404
      ? '当前运行的 API 尚不支持画风库；请重新启动 AI、API 和 worker，再刷新页面。'
      : errorText(err)) })
      .finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
  }, [styleId, tick, onReady])
  const entry = catalog?.entries.find(row => row.id === styleId)
  return <Card size="small" title={<Space wrap><span>fy 画风库</span><Tag color="blue">Canvas 动画</Tag>
    {catalog && <Text type="secondary">{catalog.prompt_count} 种画风</Text>}</Space>}
    extra={<Button size="small" icon={<ReloadOutlined />} loading={loading} onClick={() => setTick(t => t + 1)}>重新加载</Button>}
    style={{ marginBottom: 24 }}>
    <Form.Item name="fy_style_id" label="选择画风" rules={[{ required: true, message: '请选择画风' }]}>
      <Select showSearch loading={loading} placeholder="搜索编号、名称、特征或适用内容"
        optionFilterProp="searchText" options={catalog?.entries.map(row => ({ value: row.id,
          label: `${row.id} · ${row.name}`, searchText: `${row.id} ${row.name} ${row.category} ${row.look ?? ''} ${row.fit ?? ''} ${row.keywords ?? ''}` }))} />
    </Form.Item>
    {error && <Alert type="error" showIcon message="画风未就绪，暂不能提交 fy 任务" description={error} />}
    {source && !loading && !error && <>
      <Space wrap style={{ marginBottom: 8 }}><Tag color="green">原文已加载</Tag><Text strong>{source.id} · {source.name}</Text>
        <Text type="secondary">{entry?.category}</Text></Space>
      {entry?.look && <Paragraph>{entry.look}</Paragraph>}
      {entry?.fit && <Paragraph type="secondary">适合：{entry.fit}</Paragraph>}
      <Paragraph type="secondary">将按原文的构图、材质、排字和动作特征生成 Canvas 动画。使用独立绘制实现，不会照搬库中的示例视频；最终效果仍需审片。</Paragraph>
      <Collapse size="small" items={[{ key: 'source', label: '查看画风原文与来源', children: <>
        <Paragraph><a href={source.repository} target="_blank" rel="noreferrer">code-animation-341-styles</a> · {source.attribution}</Paragraph>
        <Paragraph copyable={{ text: source.sha256 }}>SHA-256：{source.sha256.slice(0, 16)}…</Paragraph>
        <pre className="script-view" style={{ maxHeight: 280, overflow: 'auto', whiteSpace: 'pre-wrap' }}>{source.content}</pre>
        {!!source.dependency_candidates?.length && <Paragraph type="secondary">原文有外部依赖引用，本路径用离线 Canvas 实现视觉特征，不自动安装这些依赖。</Paragraph>}
      </> }]} />
    </>}
    {catalog && <Paragraph type="secondary" style={{ fontSize: 12, marginTop: 12, marginBottom: 0 }}>{catalog.license_note}</Paragraph>}
  </Card>
}
