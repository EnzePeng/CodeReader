import { KeyboardEvent, useCallback, useEffect, useRef, useState } from 'react'
import { getJSON, projectBrowseUrl } from '../api'
import { BrowseResult } from '../types'

interface Node {
  name: string
  relativePath: string
  isDir: boolean
  isCode: boolean
  children: Node[] | null
  open: boolean
  error: string | null
}

interface Props {
  projectId: string
  rootLabel: string
  currentFile: string | null
  onOpenFile: (relativePath: string) => void
}

function joinRelative(dir: string, name: string): string {
  return [dir.replace(/[\\/]+$/, ''), name].filter(Boolean).join('/')
}

function normalizeBrowse(value: unknown, requestedPath: string): Node[] {
  const result = value as Partial<BrowseResult> & { entries?: any[]; relative_path?: string }
  const base = typeof result.relative_path === 'string' ? result.relative_path : requestedPath
  if (Array.isArray(result.entries)) {
    return result.entries.map(entry => ({
      name: String(entry.name),
      relativePath: String(entry.relative_path ?? joinRelative(base, entry.name)),
      isDir: !!(entry.is_dir ?? entry.type === 'directory'),
      isCode: entry.is_code !== false,
      children: null,
      open: false,
      error: null,
    }))
  }
  const dirs: Node[] = (result.dirs ?? []).map(dir => ({
    name: dir.name,
    relativePath: joinRelative(base, dir.name),
    isDir: true,
    isCode: false,
    children: null,
    open: false,
    error: null,
  }))
  const files: Node[] = (result.files ?? []).map(file => ({
    name: file.name,
    relativePath: joinRelative(base, file.name),
    isDir: false,
    isCode: file.is_code,
    children: null,
    open: false,
    error: null,
  }))
  return [...dirs, ...files]
}

async function fetchChildren(projectId: string, relativePath: string): Promise<Node[]> {
  const result = await getJSON<unknown>(projectBrowseUrl(projectId, relativePath))
  return normalizeBrowse(result, relativePath)
}

function isDescendant(candidate: string, parent: string): boolean {
  return candidate === parent || candidate.startsWith(`${parent}/`)
}

function updateTree(nodes: Node[], path: string, fn: (node: Node) => Node): Node[] {
  return nodes.map(node => {
    if (node.relativePath === path) return fn(node)
    if (node.isDir && node.children && isDescendant(path, node.relativePath)) {
      return { ...node, children: updateTree(node.children, path, fn) }
    }
    return node
  })
}

function extOf(name: string): string {
  const index = name.lastIndexOf('.')
  return index >= 0 ? name.slice(index + 1).toLowerCase() : ''
}

function Row({ node, depth, currentFile, onToggle, onOpenFile }: {
  node: Node
  depth: number
  currentFile: string | null
  onToggle: (node: Node, forceOpen?: boolean) => void
  onOpenFile: (relativePath: string) => void
}) {
  const pad = { paddingLeft: `${depth * 14 + 8}px` }
  if (node.isDir) {
    const onKeyDown = (event: KeyboardEvent<HTMLButtonElement>) => {
      if (event.key === 'ArrowRight' && !node.open) {
        event.preventDefault(); onToggle(node, true)
      } else if (event.key === 'ArrowLeft' && node.open) {
        event.preventDefault(); onToggle(node, false)
      }
    }
    return (
      <div role="none">
        <button className="tree-row dir" style={pad} role="treeitem"
          aria-level={depth + 1} aria-expanded={node.open}
          onClick={() => onToggle(node)} onKeyDown={onKeyDown}>
          <span className={`arrow ${node.open ? 'open' : ''}`} aria-hidden="true" />
          <span className="tree-name">{node.name}</span>
        </button>
        {node.open && node.children && (
          <div role="group">
            {node.children.map(child => (
              <Row key={child.relativePath} node={child} depth={depth + 1}
                currentFile={currentFile} onToggle={onToggle} onOpenFile={onOpenFile} />
            ))}
          </div>
        )}
        {node.open && node.children === null && !node.error && (
          <div className="tree-row loading" style={{ paddingLeft: `${(depth + 1) * 14 + 8}px` }}
            role="status">加载中…</div>
        )}
        {node.open && node.error && (
          <div className="tree-node-error" role="alert">
            <span>{node.error}</span>
            <button className="btn-sm" onClick={() => onToggle(node, true)}>重试</button>
          </div>
        )}
      </div>
    )
  }
  const ext = extOf(node.name)
  const active = currentFile === node.relativePath
  return (
    <button
      className={`tree-row file ${node.isCode ? '' : 'dim'} ${active ? 'active' : ''}`}
      style={pad}
      role="treeitem"
      aria-level={depth + 1}
      aria-current={active ? 'page' : undefined}
      onClick={() => onOpenFile(node.relativePath)}
      title={node.relativePath}
    >
      <span className={`file-chip ext-${ext === 'py' ? 'py' : 'other'}`}>{ext || '·'}</span>
      <span className="tree-name">{node.name}</span>
    </button>
  )
}

export default function FileTree({ projectId, rootLabel, currentFile, onOpenFile }: Props) {
  const [nodes, setNodes] = useState<Node[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const loadSeq = useRef(0)

  const loadRoot = useCallback(() => {
    const seq = ++loadSeq.current
    setLoading(true)
    setError('')
    fetchChildren(projectId, '')
      .then(value => { if (seq === loadSeq.current) setNodes(value) })
      .catch(err => { if (seq === loadSeq.current) setError(String((err as Error).message || err)) })
      .finally(() => { if (seq === loadSeq.current) setLoading(false) })
  }, [projectId])

  useEffect(() => {
    setNodes([])
    loadRoot()
    return () => { loadSeq.current++ }
  }, [loadRoot])

  const toggle = useCallback((node: Node, forceOpen?: boolean) => {
    if (!node.isDir) return
    const nextOpen = forceOpen ?? !node.open
    setNodes(previous => updateTree(previous, node.relativePath, current => ({
      ...current, open: nextOpen, error: nextOpen ? null : current.error,
      children: forceOpen && current.error ? null : current.children,
    })))
    if (nextOpen && (node.children === null || node.error)) {
      fetchChildren(projectId, node.relativePath)
        .then(children => setNodes(previous => updateTree(previous, node.relativePath, current => ({
          ...current, children, error: null,
        }))))
        .catch(err => setNodes(previous => updateTree(previous, node.relativePath, current => ({
          ...current, children: [], error: String((err as Error).message || err),
        }))))
    }
  }, [projectId])

  return (
    <div className="filetree" role="tree" aria-label={`${rootLabel} 文件树`} aria-busy={loading}>
      <div className="tree-root-label" title={rootLabel}>{rootLabel}</div>
      {loading && <div className="side-empty" role="status">正在加载文件…</div>}
      {error && (
        <div className="side-empty" role="alert">
          <p>{error}</p>
          <button className="btn-sm" onClick={loadRoot}>重试</button>
        </div>
      )}
      {!loading && !error && nodes.length === 0 && <div className="side-empty">项目中没有可显示的文件</div>}
      {!error && nodes.map(node => (
        <Row key={node.relativePath} node={node} depth={0} currentFile={currentFile}
          onToggle={toggle} onOpenFile={onOpenFile} />
      ))}
    </div>
  )
}
