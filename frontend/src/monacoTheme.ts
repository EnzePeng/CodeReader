import * as monaco from 'monaco-editor/esm/vs/editor/editor.api'

// 自定义 Monaco 主题：与 styles.css 的设计 token 对齐（docs/ui-redesign.md §5.4）。
// 编辑器背景 = --bg-app(#131318)，语法色全部从强调色 #7A9BFF 同族派生，克制的 6~7 色。
let registered = false

export function registerCodeReaderTheme() {
  if (registered) return
  registered = true
  monaco.editor.defineTheme('codereader-dark', {
    base: 'vs-dark',
    inherit: true,
    rules: [
      { token: '', foreground: 'D5D9E8', background: '131318' },
      { token: 'comment', foreground: '6B7390', fontStyle: 'italic' },
      { token: 'keyword', foreground: '82A0FF' },
      { token: 'string', foreground: '83C892' },
      { token: 'number', foreground: 'E3B15F' },
      { token: 'constant', foreground: 'E3B15F' },
      { token: 'type', foreground: '56C1D6' },
      { token: 'class', foreground: '56C1D6' },
      { token: 'function', foreground: 'B9A3F2' },
      { token: 'variable', foreground: 'D5D9E8' },
      { token: 'operator', foreground: '8E95AD' },
      { token: 'delimiter', foreground: '8E95AD' },
      { token: 'tag', foreground: 'F2949C' },
      { token: 'attribute.name', foreground: 'E3B15F' },
      { token: 'regexp', foreground: '83C892' },
    ],
    colors: {
      'editor.background': '#131318',
      'editor.foreground': '#D5D9E8',
      'editor.lineHighlightBackground': '#FFFFFF06',
      'editor.selectionBackground': '#7A9BFF2E',
      'editor.inactiveSelectionBackground': '#7A9BFF17',
      'editorCursor.foreground': '#7A9BFF',
      'editorLineNumber.foreground': '#4E5366',
      'editorLineNumber.activeForeground': '#9AA3BF',
      /* 新老版本 monaco 的缩进参考线 key 都写上，多余的会被忽略 */
      'editorIndentGuide.background': '#22232C',
      'editorIndentGuide.activeBackground': '#333546',
      'editorIndentGuide.background1': '#22232C',
      'editorIndentGuide.activeBackground1': '#333546',
      'editorGutter.background': '#131318',
      'editorWidget.background': '#1C1D24',
      'editorWidget.border': '#262733',
      'minimap.background': '#131318',
      'minimapSlider.background': '#FFFFFF0F',
      'minimapSlider.hoverBackground': '#FFFFFF1A',
      'minimapSlider.activeBackground': '#FFFFFF24',
      'scrollbarSlider.background': '#FFFFFF14',
      'scrollbarSlider.hoverBackground': '#FFFFFF22',
      'scrollbarSlider.activeBackground': '#FFFFFF2E',
      'editor.findMatchBackground': '#E8B45A40',
      'editor.findMatchHighlightBackground': '#E8B45A22',
      'editorBracketMatch.background': '#7A9BFF22',
      'editorBracketMatch.border': '#7A9BFF55',
      'editorOverviewRuler.border': '#00000000',
      'scrollbar.shadow': '#00000000',
    },
  })
}
