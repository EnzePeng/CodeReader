import { MutableRefObject, useEffect, useRef } from 'react'
import Editor from '@monaco-editor/react'
import { loader } from '@monaco-editor/react'
import * as monaco from 'monaco-editor/esm/vs/editor/editor.api'
import editorWorker from 'monaco-editor/esm/vs/editor/editor.worker?worker'
import 'monaco-editor/esm/vs/basic-languages/python/python.contribution'
import 'monaco-editor/esm/vs/basic-languages/markdown/markdown.contribution'
import 'monaco-editor/esm/vs/basic-languages/yaml/yaml.contribution'
import 'monaco-editor/esm/vs/basic-languages/sql/sql.contribution'
import 'monaco-editor/esm/vs/basic-languages/shell/shell.contribution'
import { FileInfo, LineRange } from '../types'
import { registerCodeReaderTheme } from '../monacoTheme'

// 本组件本身由 React.lazy 延迟加载；只有真正打开文件时才下载 Monaco 代码块。
;(self as any).MonacoEnvironment = { getWorker: () => new editorWorker() }
loader.config({ monaco })
registerCodeReaderTheme()

export interface CodePaneApi {
  revealRange: (startLine: number, endLine: number) => void
  getPosition: () => { line: number; column: number }
}

interface Props {
  fileInfo: FileInfo
  activeRange: LineRange | null
  onLineClick: (line: number) => void
  onSelection: (range: LineRange | null) => void
  onCursorPosition: (line: number, column: number) => void
  onNavigateRequest?: (kind: 'definition' | 'references', line: number, column: number) => void
  onHistoryRequest?: (direction: -1 | 1) => void
  apiRef: MutableRefObject<CodePaneApi | null>
}

export default function CodePane({
  fileInfo, activeRange, onLineClick, onSelection, onCursorPosition,
  onNavigateRequest, onHistoryRequest, apiRef,
}: Props) {
  const editorRef = useRef<monaco.editor.IStandaloneCodeEditor | null>(null)
  const decorRef = useRef<monaco.editor.IEditorDecorationsCollection | null>(null)
  // 用 ref 转发回调，避免 Monaco 事件闭包捕获旧值
  const cbRef = useRef({ onLineClick, onSelection, onCursorPosition, onNavigateRequest, onHistoryRequest })
  cbRef.current = { onLineClick, onSelection, onCursorPosition, onNavigateRequest, onHistoryRequest }

  const handleMount = (editor: monaco.editor.IStandaloneCodeEditor) => {
    editorRef.current = editor
    decorRef.current = editor.createDecorationsCollection([])
    apiRef.current = {
      revealRange: (s: number, e: number) => {
        editor.setSelection(new monaco.Range(s, 1, Math.max(s, e), 1))
        editor.revealRangeNearTop(new monaco.Range(s, 1, Math.max(s, e), 1), monaco.editor.ScrollType.Smooth)
      },
      getPosition: () => {
        const position = editor.getPosition()
        return { line: position?.lineNumber ?? 1, column: position?.column ?? 1 }
      },
    }
    editor.onDidChangeCursorSelection(ev => {
      if (ev.source === 'api') return
      const sel = ev.selection
      cbRef.current.onCursorPosition(sel.positionLineNumber, sel.positionColumn)
      const empty = sel.startLineNumber === sel.endLineNumber && sel.startColumn === sel.endColumn
      if (empty) {
        cbRef.current.onSelection(null)
        cbRef.current.onLineClick(sel.startLineNumber)
      } else {
        cbRef.current.onSelection({ start: sel.startLineNumber, end: sel.endLineNumber })
      }
    })
    editor.onKeyDown(event => {
      const position = editor.getPosition()
      if (!position) return
      if (event.keyCode === monaco.KeyCode.F12) {
        event.preventDefault()
        event.stopPropagation()
        cbRef.current.onNavigateRequest?.(
          event.shiftKey ? 'references' : 'definition',
          position.lineNumber,
          position.column,
        )
      } else if (event.altKey && event.keyCode === monaco.KeyCode.LeftArrow) {
        event.preventDefault(); cbRef.current.onHistoryRequest?.(-1)
      } else if (event.altKey && event.keyCode === monaco.KeyCode.RightArrow) {
        event.preventDefault(); cbRef.current.onHistoryRequest?.(1)
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
      path={fileInfo.relative_path}
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
