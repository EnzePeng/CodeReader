import { useCallback, useEffect, useState } from 'react'
import { getJSON, encodePath } from '../api'
import { BrowseResult } from '../types'

interface Node {
  name: string
  path: string
  isDir: boolean
  isCode: boolean
  children: Node[] | null
  open: boolean
}

interface Props {
  root: string
  currentFile: string | null
  onOpenFile: (path: string) => void
}

function joinPath(dir: string, name: string): string {
  return dir.endsWith('\\') || dir.endsWith('/') ? dir + name : dir + '\\' + name
}

async function fetchChildren(path: string): Promise<Node[]> {
  const r = await getJSON<BrowseResult>(`/api/browse?path=${encodePath(path)}`)
  const dirs: Node[] = r.dirs.map(d => ({
    name: d.name, path: joinPath(r.path, d.name), isDir: true, isCode: false,
    children: null, open: false,
  }))
  const files: Node[] = r.files.map(f => ({
    name: f.name, path: joinPath(r.path, f.name), isDir: false, isCode: f.is_code,
    children: null, open: false,
  }))
  return [...dirs, ...files]
}

function updateTree(nodes: Node[], path: string, fn: (n: Node) => Node): Node[] {
  return nodes.map(n => {
    if (n.path === path) return fn(n)
    if (n.isDir && n.children && path.startsWith(n.path)) {
      return { ...n, children: updateTree(n.children, path, fn) }
    }
    return n
  })
}

function extOf(name: string): string {
  const i = name.lastIndexOf('.')
  return i >= 0 ? name.slice(i + 1).toLowerCase() : ''
}

function Row({ node, depth, currentFile, onToggle, onOpenFile }: {
  node: Node; depth: number; currentFile: string | null
  onToggle: (n: Node) => void; onOpenFile: (p: string) => void
}) {
  const pad = { paddingLeft: `${depth * 14 + 8}px` }
  if (node.isDir) {
    return (
      <>
        <div className="tree-row dir" style={pad} onClick={() => onToggle(node)}>
          <span className={`arrow ${node.open ? 'open' : ''}`} />
          <span className="tree-name">{node.name}</span>
        </div>
        {node.open && node.children && node.children.map(c => (
          <Row key={c.path} node={c} depth={depth + 1} currentFile={currentFile}
            onToggle={onToggle} onOpenFile={onOpenFile} />
        ))}
        {node.open && node.children === null && (
          <div className="tree-row loading" style={{ paddingLeft: `${(depth + 1) * 14 + 8}px` }}>加载中…</div>
        )}
      </>
    )
  }
  const ext = extOf(node.name)
  const active = currentFile === node.path
  return (
    <div
      className={`tree-row file ${node.isCode ? '' : 'dim'} ${active ? 'active' : ''}`}
      style={pad}
      onClick={() => onOpenFile(node.path)}
      title={node.path}
    >
      <span className={`file-chip ext-${ext === 'py' ? 'py' : 'other'}`}>{ext || '·'}</span>
      <span className="tree-name">{node.name}</span>
    </div>
  )
}

export default function FileTree({ root, currentFile, onOpenFile }: Props) {
  const [nodes, setNodes] = useState<Node[]>([])
  const [err, setErr] = useState('')

  useEffect(() => {
    setNodes([])
    setErr('')
    fetchChildren(root)
      .then(setNodes)
      .catch(e => setErr(String(e.message || e)))
  }, [root])

  const toggle = useCallback((node: Node) => {
    if (!node.isDir) return
    setNodes(prev => updateTree(prev, node.path, n => ({ ...n, open: !n.open })))
    if (node.children === null) {
      fetchChildren(node.path)
        .then(children => setNodes(prev => updateTree(prev, node.path, n => ({ ...n, children }))))
        .catch(() => setNodes(prev => updateTree(prev, node.path, n => ({ ...n, children: [] }))))
    }
  }, [])

  if (err) return <div className="side-empty">{err}</div>
  return (
    <div className="filetree">
      <div className="tree-root-label" title={root}>{root}</div>
      {nodes.map(n => (
        <Row key={n.path} node={n} depth={0} currentFile={currentFile}
          onToggle={toggle} onOpenFile={onOpenFile} />
      ))}
    </div>
  )
}
