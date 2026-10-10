import { useState } from 'react'
import { Alert, Button, Space, Typography } from 'antd'
import { api } from '../api'
import type { Artifact } from '../types'

export function ShotMedia({ jobId, shotId, artifact, onRefresh }: {
  jobId: string; shotId: string; artifact: Artifact; onRefresh: () => void
}) {
  const [failed, setFailed] = useState(false)
  return <Space direction="vertical" size={12} style={{ width: '100%' }}>
    <Typography.Text strong>镜头预览 · 第 {artifact.attempt} 版</Typography.Text>
    <Typography.Text type="secondary">当前渲染结果，尚未应用全片后期与 Live2D。下方为本版审核抽帧，点击可打开原图。</Typography.Text>
    {failed && <Alert type="warning" showIcon message="部分预览无法加载，文件可能已被清理或镜头已更新。"
      action={<Button size="small" onClick={onRefresh}>刷新任务</Button>} />}
    <video controls preload="metadata" playsInline className="film-video" aria-label={`第 ${artifact.attempt} 版镜头视频`}
      src={api.shotMediaUrl(jobId, shotId, artifact.artifact_id)} onError={() => setFailed(true)} />
    <div className="evidence-grid">
      {(artifact.frame_samples ?? []).map((_, i) => <a key={i}
        href={api.shotMediaUrl(jobId, shotId, artifact.artifact_id, i)} target="_blank" rel="noreferrer">
        <img loading="lazy" src={api.shotMediaUrl(jobId, shotId, artifact.artifact_id, i)}
          alt={`审核证据帧 ${i + 1}`} onError={() => setFailed(true)} />
        <span>证据帧 {i + 1}</span>
      </a>)}
    </div>
    {!artifact.frame_samples?.length && <Typography.Text type="secondary">本版暂无抽帧证据。</Typography.Text>}
  </Space>
}
