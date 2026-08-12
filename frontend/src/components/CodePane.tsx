import { MutableRefObject, useEffect, useRef } from 'react'
import Editor from '@monaco-editor/react'
import * as monaco from 'monaco-editor'
import { FileInfo, LineRange } from '../types'
import { registerCodeReaderTheme } from '../monacoTheme'

// 模块加载时注册一次自定义主题（main.tsx 已完成 monaco 离线初始化）
registerCodeReaderTheme()

export interface CodePaneApi {
  revealRange: (startLine: number, endLine: number) => void
}

interface Props {
  fileInfo: FileInfo
  activeRange: LineRange | null
  onLineClick: (line: number) => void
  onSelection: (range: LineRange | null) => void
  apiRef: MutableRefObject<CodePaneApi | null>
}

export default function CodePane({ fileInfo, activeRange, onLineClick, onSelection, apiRef }: Props) {
  const editorRef = useRef<monaco.editor.IStandaloneCodeEditor | null>(null)
  const decorRef = useRef<monaco.editor.IEditorDecorationsCollection | null>(null)
  // 用 ref 转发回调，避免 Monaco 事件闭包捕获旧值
  const cbRef = useRef({ onLineClick, onSelection })
  cbRef.current = { onLineClick, onSelection }

  const handleMount = (editor: monaco.editor.IStandaloneCodeEditor) => {
    editorRef.current = editor
    decorRef.current = editor.createDecorationsCollection([])
    apiRef.current = {
      revealRange: (s: number, _e: number) => {
        editor.revealLineNearTop(s, monaco.editor.ScrollType.Smooth)
      },
    }
    editor.onDidChangeCursorSelection(ev => {
      if (ev.source === 'api') return
      const sel = ev.selection
      const empty = sel.startLineNumber === sel.endLineNumber && sel.startColumn === sel.endColumn
      if (empty) {
        cbRef.current.onSelection(null)
        cbRef.current.onLineClick(sel.startLineNumber)
      } else {
        cbRef.current.onSelection({ start: sel.startLineNumber, end: sel.endLineNumber })
      }
    })
  }

  useEffect(() => {
    const dc = decorRef.current
    if (!dc) return
    if (!activeRange) {
      dc.set([])
      return
    }
    dc.set([{
      range: new monaco.Range(activeRange.start, 1, activeRange.end, 1),
      options: {
        isWholeLine: true,
        className: 'seg-active-line',
        linesDecorationsClassName: 'seg-active-gutter',
      },
    }])
  }, [activeRange, fileInfo.path])

  return (
    <Editor
      height="100%"
      path={fileInfo.path}
      language={fileInfo.language}
      value={fileInfo.content}
      theme="codereader-dark"
      options={{
        readOnly: true,
        domReadOnly: true,
        fontSize: 13,
        lineHeight: 20,
        fontFamily: "'Cascadia Code', 'Cascadia Mono', Consolas, 'Courier New', monospace",
        fontLigatures: true,
        padding: { top: 8 },
        minimap: { enabled: true, renderCharacters: false, maxColumn: 80 },
        scrollBeyondLastLine: false,
        renderLineHighlight: 'all',
        automaticLayout: true,
        folding: true,
        wordWrap: 'off',
        unicodeHighlight: { ambiguousCharacters: false, invisibleCharacters: false },
        stickyScroll: { enabled: false },
        contextmenu: false,
      }}
      onMount={handleMount}
    />
  )
}
