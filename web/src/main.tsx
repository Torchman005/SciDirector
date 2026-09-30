import React from 'react'
import ReactDOM from 'react-dom/client'
import { App as AntApp, ConfigProvider } from 'antd'
import zhCN from 'antd/locale/zh_CN'
import 'dayjs/locale/zh-cn'

import { App } from './App'
import { antdTheme } from './theme'
import './styles.css'

const rootEl = document.getElementById('root')
if (!rootEl) {
  // 显式报错而不是静默失败：挂载点缺失是模板问题，越早暴露越好。
  throw new Error('找不到 #root 挂载点，请检查 index.html')
}

ReactDOM.createRoot(rootEl).render(
  <React.StrictMode>
    {/*
      ConfigProvider 做三件事：
        - 注入主题（暗色算法 + 项目主色），见 theme.ts；
        - 注入中文语言包 —— 这一页全是中文，留着 antd 默认的英文会出现
          「No Data」和「暂无数据」混排；
        - 外层再套 antd 的 App，它是 message/notification/modal 的上下文宿主。
          不套的话 `App.useApp()` 会拿到一个只会在控制台 warning 的降级实例，
          表现是"提示弹不出来"。
    */}
    <ConfigProvider theme={antdTheme} locale={zhCN}>
      <AntApp>
        <App />
      </AntApp>
    </ConfigProvider>
  </React.StrictMode>,
)
