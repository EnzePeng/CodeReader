import React from 'react'
import ReactDOM from 'react-dom/client'
import * as monaco from 'monaco-editor'
import editorWorker from 'monaco-editor/esm/vs/editor/editor.worker?worker'
import { loader } from '@monaco-editor/react'
import App from './App'
import './styles.css'

// 完全离线：使用本地打包的 monaco，不走 CDN
;(self as any).MonacoEnvironment = { getWorker: () => new editorWorker() }
loader.config({ monaco })

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
)
