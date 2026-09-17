import React from 'react'
import ReactDOM from 'react-dom/client'
import { App } from './App'
import './styles.css'

const rootEl = document.getElementById('root')
if (!rootEl) {
  // 显式报错而不是静默失败：挂载点缺失是模板问题，越早暴露越好。
  throw new Error('找不到 #root 挂载点，请检查 index.html')
}

ReactDOM.createRoot(rootEl).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
)
