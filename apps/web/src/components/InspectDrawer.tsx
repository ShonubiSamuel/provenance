import { useCallback, useEffect, useState, type PointerEvent as ReactPointerEvent } from 'react'
import { fetchBlob, fetchSizes, fetchTree } from '../api'
import type { BlobResponse, DownloadRequest, TreeEntry } from '../types'

function splitFullName(fullName: string): [string, string] {
  const i = fullName.indexOf('/')
  return [fullName.slice(0, i), fullName.slice(i + 1)]
}

function dirname(path: string): string {
  const i = path.lastIndexOf('/')
  return i === -1 ? '' : path.slice(0, i)
}

function fmtSize(bytes: number | null): string {
  if (bytes == null) return ''
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

export function InspectDrawer({
  fullName,
  matchedPaths,
  initialFile,
  onEnqueue,
  onOpenDownloads,
  onClose,
}: {
  fullName: string
  matchedPaths: Set<string>
  initialFile?: string
  onEnqueue: (req: DownloadRequest) => void
  onOpenDownloads: () => void
  onClose: () => void
}) {
  const [owner, repo] = splitFullName(fullName)

  // Browser-like navigation over directory paths, so Back/Forward work.
  const initialDir = initialFile ? dirname(initialFile) : ''
  const [nav, setNav] = useState<{ stack: string[]; idx: number }>({
    stack: [initialDir],
    idx: 0,
  })
  const path = nav.stack[nav.idx]
  const canBack = nav.idx > 0
  const canForward = nav.idx < nav.stack.length - 1

  const [entries, setEntries] = useState<TreeEntry[]>([])
  const [treeError, setTreeError] = useState<string | null>(null)
  const [treeLoading, setTreeLoading] = useState(false)
  const [selected, setSelected] = useState<string | null>(initialFile ?? null)
  const [blob, setBlob] = useState<BlobResponse | null>(null)
  const [blobLoading, setBlobLoading] = useState(false)
  const [sizes, setSizes] = useState<Record<string, number> | null>(null)
  const [sizesTruncated, setSizesTruncated] = useState(false)
  const [copied, setCopied] = useState(false)
  // Resizable split between the file browser and the viewer.
  const [treeWidth, setTreeWidth] = useState(420)

  const startResize = useCallback((e: ReactPointerEvent) => {
    e.preventDefault()
    const startX = e.clientX
    const startWidth = treeWidth
    const onMove = (ev: PointerEvent) => {
      setTreeWidth(Math.min(800, Math.max(240, startWidth + ev.clientX - startX)))
    }
    const onUp = () => {
      window.removeEventListener('pointermove', onMove)
      window.removeEventListener('pointerup', onUp)
    }
    window.addEventListener('pointermove', onMove)
    window.addEventListener('pointerup', onUp)
  }, [treeWidth])

  const navigate = useCallback((to: string) => {
    setNav((n) => ({ stack: [...n.stack.slice(0, n.idx + 1), to], idx: n.idx + 1 }))
  }, [])
  const back = useCallback(() => setNav((n) => (n.idx > 0 ? { ...n, idx: n.idx - 1 } : n)), [])
  const forward = useCallback(
    () => setNav((n) => (n.idx < n.stack.length - 1 ? { ...n, idx: n.idx + 1 } : n)),
    [],
  )

  const openFile = useCallback(
    (filePath: string) => {
      setSelected(filePath)
      setBlob(null)
      setBlobLoading(true)
      fetchBlob(owner, repo, filePath)
        .then(setBlob)
        .catch((e) =>
          setBlob({
            owner, repo, path: filePath, size: 0,
            encoding: 'too_large', text: `Could not load file: ${e}`,
          }),
        )
        .finally(() => setBlobLoading(false))
    },
    [owner, repo],
  )

  // Load the current directory whenever the path changes.
  useEffect(() => {
    let cancelled = false
    setTreeLoading(true)
    setTreeError(null)
    fetchTree(owner, repo, path)
      .then((t) => !cancelled && setEntries(t.entries))
      .catch((e) => !cancelled && setTreeError(e instanceof Error ? e.message : String(e)))
      .finally(() => !cancelled && setTreeLoading(false))
    return () => {
      cancelled = true
    }
  }, [owner, repo, path])

  // Preview the deep-linked file once, on open.
  useEffect(() => {
    if (initialFile) openFile(initialFile)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // Folder sizes for the whole repo, fetched once (contents API reports 0 for dirs).
  useEffect(() => {
    let cancelled = false
    fetchSizes(owner, repo)
      .then((s) => {
        if (cancelled) return
        setSizes(s.sizes)
        setSizesTruncated(s.truncated)
      })
      .catch(() => !cancelled && setSizes({}))
    return () => {
      cancelled = true
    }
  }, [owner, repo])

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && onClose()
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  async function copyLink() {
    const gh = selected
      ? `https://github.com/${fullName}/blob/HEAD/${selected}`
      : path
        ? `https://github.com/${fullName}/tree/HEAD/${path}`
        : `https://github.com/${fullName}`
    await navigator.clipboard.writeText(gh)
    setCopied(true)
    setTimeout(() => setCopied(false), 1400)
  }

  // Folder downloads fan out through the backend download manager (aria2): every file
  // in the folder downloads in parallel and is individually resumable.
  const downloadFolder = (folderPath: string) =>
    onEnqueue({ kind: 'folder', owner, repo, path: folderPath })

  const crumbs = path ? path.split('/') : []

  return (
    <div className="fixed inset-0 z-40 flex flex-col bg-slate-950 text-slate-100">
      {/* Top bar */}
      <div className="flex items-center justify-between gap-3 border-b border-slate-800 px-4 py-2.5">
        <div className="flex min-w-0 items-center gap-2">
          <button
            onClick={onClose}
            className="shrink-0 rounded-md border border-slate-700 px-2.5 py-1.5 text-xs text-slate-300 hover:bg-slate-800"
            title="Back to search results (Esc, or the browser Back button)"
          >
            ← Results
          </button>
          <div className="flex items-center gap-0.5">
            <button
              onClick={back}
              disabled={!canBack}
              className="rounded px-2 py-1 text-slate-400 hover:bg-slate-800 disabled:opacity-30"
              title="Back a folder"
            >
              ←
            </button>
            <button
              onClick={forward}
              disabled={!canForward}
              className="rounded px-2 py-1 text-slate-400 hover:bg-slate-800 disabled:opacity-30"
              title="Forward a folder"
            >
              →
            </button>
          </div>
          <div className="min-w-0">
            <div className="truncate text-base font-semibold text-slate-100">{fullName}</div>
            <div className="text-xs text-slate-500">
              default branch · {matchedPaths.size} matched file
              {matchedPaths.size === 1 ? '' : 's'}
              {sizesTruncated && (
                <span
                  className="ml-1 text-amber-500"
                  title="This repo's tree exceeds GitHub's cap, so some folder sizes are undercounted."
                >
                  · folder sizes approximate
                </span>
              )}
            </div>
          </div>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <button
            onClick={copyLink}
            className="rounded-md border border-slate-700 px-2.5 py-1.5 text-xs text-slate-300 hover:bg-slate-800"
            title="Copy a github.com link to the current file or folder"
          >
            {copied ? 'Link copied!' : 'Copy GitHub link'}
          </button>
          <button
            onClick={onOpenDownloads}
            className="rounded-md border border-slate-700 px-2.5 py-1.5 text-xs text-slate-300 hover:bg-slate-800"
          >
            Downloads
          </button>
          <button
            onClick={() => onEnqueue({ kind: 'repo', owner, repo })}
            className="rounded-md bg-sky-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-sky-500"
          >
            Download repo .zip
          </button>
          <button
            onClick={onClose}
            className="rounded-md px-2 py-1.5 text-slate-400 hover:bg-slate-800 hover:text-slate-200"
            aria-label="Close (Esc)"
            title="Close (Esc)"
          >
            ✕
          </button>
        </div>
      </div>

      {/* Breadcrumb + download-this-folder */}
      <div className="flex items-center justify-between gap-2 border-b border-slate-800 px-4 py-2 text-sm">
        <div className="flex flex-wrap items-center gap-1 text-slate-400">
          <button onClick={() => navigate('')} className="hover:text-sky-300">
            {repo}
          </button>
          {crumbs.map((seg, i) => {
            const target = crumbs.slice(0, i + 1).join('/')
            return (
              <span key={target} className="flex items-center gap-1">
                <span className="text-slate-600">/</span>
                <button onClick={() => navigate(target)} className="hover:text-sky-300">
                  {seg}
                </button>
              </span>
            )
          })}
        </div>
        {path && (
          <button
            onClick={() => downloadFolder(path)}
            className="shrink-0 rounded-md border border-slate-700 px-2 py-1 text-xs text-slate-300 hover:bg-slate-800"
            title="Download every file in this folder (parallel, resumable)"
          >
            Download {crumbs[crumbs.length - 1]}
          </button>
        )}
      </div>

      {/* Two panes with a draggable divider */}
      <div className="flex min-h-0 flex-1">
        <div style={{ width: treeWidth }} className="shrink-0 overflow-y-auto">
          {treeLoading && <p className="p-4 text-sm text-slate-500">Loading…</p>}
          {treeError && <p className="p-4 text-sm text-red-400">{treeError}</p>}
          {!treeLoading && !treeError && (
            <ul className="divide-y divide-slate-800/60">
              {entries.map((entry) => {
                const matched = matchedPaths.has(entry.path)
                const isSelected = selected === entry.path
                const isDir = entry.type === 'dir'
                return (
                  <li
                    key={entry.path}
                    className={`flex items-center gap-2 px-3 py-1.5 text-sm ${
                      isSelected ? 'bg-slate-800' : 'hover:bg-slate-800/50'
                    }`}
                  >
                    <button
                      onClick={() => (isDir ? navigate(entry.path) : openFile(entry.path))}
                      className="flex min-w-0 flex-1 items-center gap-2 text-left"
                    >
                      <span className="shrink-0">{isDir ? '📁' : '📄'}</span>
                      <span
                        className={`truncate ${
                          matched ? 'font-medium text-amber-300' : 'text-slate-300'
                        }`}
                        title={matched ? `${entry.name} — matched your search` : entry.name}
                      >
                        {entry.name}
                      </span>
                      {matched && <span className="shrink-0 text-xs text-amber-500">●</span>}
                    </button>
                    <span className="shrink-0 text-xs tabular-nums text-slate-600">
                      {isDir ? (sizes ? fmtSize(sizes[entry.path] ?? 0) : '…') : fmtSize(entry.size)}
                    </span>
                    {isDir ? (
                      <button
                        onClick={() => downloadFolder(entry.path)}
                        className="shrink-0 px-1 text-xs text-slate-500 hover:text-sky-300"
                        title="Download folder (parallel, resumable)"
                      >
                        ↓
                      </button>
                    ) : (
                      <button
                        onClick={(e) => {
                          e.stopPropagation()
                          onEnqueue({ kind: 'file', owner, repo, path: entry.path })
                        }}
                        className="shrink-0 px-1 text-xs text-slate-500 hover:text-sky-300"
                        title="Download file"
                      >
                        ↓
                      </button>
                    )}
                  </li>
                )
              })}
              {entries.length === 0 && (
                <li className="px-3 py-4 text-sm text-slate-600">Empty directory.</li>
              )}
            </ul>
          )}
        </div>

        <div
          onPointerDown={startResize}
          className="w-1 shrink-0 cursor-col-resize bg-slate-800 transition-colors hover:bg-sky-600"
          title="Drag to resize"
        />

        {/* Viewer */}
        <div className="min-w-0 flex-1 overflow-auto">
          {!selected && (
            <p className="p-6 text-sm text-slate-600">
              Select a file to preview it, or use the ↓ buttons to download.
            </p>
          )}
          {selected && (
            <div className="flex h-full flex-col">
              <div className="flex items-center justify-between gap-2 border-b border-slate-800 px-4 py-2">
                <span className="truncate font-mono text-xs text-slate-400" title={selected}>
                  {selected}
                </span>
                <button
                  onClick={() => onEnqueue({ kind: 'file', owner, repo, path: selected })}
                  className="shrink-0 rounded border border-slate-700 px-2 py-0.5 text-xs text-slate-300 hover:bg-slate-800"
                >
                  Download
                </button>
              </div>
              {blobLoading && <p className="p-4 text-sm text-slate-500">Loading…</p>}
              {blob && blob.encoding === 'text' && (
                <pre className="flex-1 overflow-auto p-4 font-mono text-xs leading-relaxed text-slate-300">
                  {blob.text}
                </pre>
              )}
              {blob && blob.encoding === 'binary' && (
                <p className="p-4 text-sm text-slate-500">
                  Binary file ({fmtSize(blob.size)}) — use Download to save it.
                </p>
              )}
              {blob && blob.encoding === 'too_large' && (
                <p className="p-4 text-sm text-slate-500">
                  {blob.text ?? `File too large to preview (${fmtSize(blob.size)}) — use Download.`}
                </p>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
