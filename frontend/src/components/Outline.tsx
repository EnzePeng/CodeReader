import { OutlineNode } from '../types'

const KIND_LABEL: Record<string, string> = {
  class: '类',
  function: '函数',
  method: '方法',
}

function Item({ node, depth, onJump }: {
  node: OutlineNode; depth: number; onJump: (line: number) => void
}) {
  return (
    <>
      <div
        className="outline-row"
        style={{ paddingLeft: `${depth * 14 + 8}px` }}
        onClick={() => onJump(node.start_line)}
        title={`第 ${node.start_line}~${node.end_line} 行`}
      >
        <span className={`chip chip-${node.kind}`}>{KIND_LABEL[node.kind] || node.kind}</span>
        <span className="outline-name">{node.name}</span>
        <span className="outline-line">{node.start_line}</span>
      </div>
      {node.children.map((c, i) => (
        <Item key={`${c.name}-${c.start_line}-${i}`} node={c} depth={depth + 1} onJump={onJump} />
      ))}
    </>
  )
}

export default function Outline({ nodes, onJump }: {
  nodes: OutlineNode[]; onJump: (line: number) => void
}) {
  if (!nodes.length) return <div className="side-empty">该文件没有可识别的类 / 函数结构</div>
  return (
    <div className="outline">
      {nodes.map((n, i) => (
        <Item key={`${n.name}-${n.start_line}-${i}`} node={n} depth={0} onJump={onJump} />
      ))}
    </div>
  )
}
