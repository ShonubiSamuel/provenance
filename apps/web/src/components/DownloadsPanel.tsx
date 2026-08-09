import { useEffect, useState } from 'react'
import {
  cancelDownload,
  clearFinishedDownloads,
  fetchDownloadFiles,
  pauseDownload,
  resumeDownload,
  retryDownload,
  revealDownload,
} from '../api'
import type { DownloadFile, DownloadItem } from '../types'

function fmtBytes(n: number): string {
  if (n <= 0) return '0 B'
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)} KB`
  if (n < 1024 * 1024 * 1024) return `${(n / (1024 * 1024)).toFixed(1)} MB`
  return `${(n / (1024 * 1024 * 1024)).toFixed(2)} GB`
}

const STATUS_COLOR: Record<string, string> = {
  active: 'text-sky-300',
  waiting: 'text-slate-400',
  paused: 'text-amber-300',
  complete: 'text-emerald-300',
  error: 'text-red-300',
  removed: 'text-slate-500',
  unknown: 'text-slate-500',
}

const FILE_DOT: Record<string, string> = {
  active: '●',
  waiting: '○',
  paused: '◐',
  complete: '✓',
  error: '✗',
  unknown: '?',
}

function fmtEta(remaining: number, speed: number): string {
  const secs = Math.round(remaining / speed)
  if (secs < 60) return `${secs}s left`
  if (secs < 3600) return `${Math.round(secs / 60)}m left`
  return `${(secs / 3600).toFixed(1)}h left`
}

function FileList({ id, tick }: { id: number; tick: number }) {
  const [files, setFiles] = useState<DownloadFile[] | null>(null)

  // Refetch whenever the parent's progress "tick" changes, so the list stays live
  // while the download runs but goes quiet once it settles.
  useEffect(() => {
    let cancelled = false
    fetchDownloadFiles(id)
      .then((r) => !cancelled && setFiles(r.files))
      .catch(() => !cancelled && setFiles([]))
    return () => {
      cancelled = true
    }
  }, [id, tick])

  if (files === null)
    return <p className="px-2 py-1 text-xs text-slate-600">Loading…</p>
  return (
    <ul className="mt-1 max-h-48 space-y-0.5 overflow-y-auto rounded bg-slate-950/60 p-2">
      {files.map((f) => (
        <li key={f.path} className="flex items-center gap-2 text-xs">
          <span className={`shrink-0 ${STATUS_COLOR[f.status] ?? 'text-slate-500'}`}>
            {FILE_DOT[f.status] ?? '·'}
          </span>
          <span className="min-w-0 flex-1 truncate text-slate-400" title={f.path}>
            {f.path}
          </span>
          <span className="shrink-0 tabular-nums text-slate-600">
            {f.status === 'complete' || !f.size
              ? fmtBytes(f.size || f.completed_bytes)
              : `${fmtBytes(f.completed_bytes)} / ${fmtBytes(f.size)}`}
          </span>
        </li>
      ))}
    </ul>
  )
}

function DownloadRow({ d, onChanged }: { d: DownloadItem; onChanged: () => void }) {
  const [expanded, setExpanded] = useState(false)
  const [retrying, setRetrying] = useState(false)
  // With an ESTIMATED total, never show a full bar before the download actually
  // finishes — cap at 99% so 100% always means done.
  const pct = Math.min(
    d.total_is_estimate && d.status !== 'complete' ? 99 : 100,
    Math.round(d.progress * 100),
  )
  // "active" with zero bytes means the server is still preparing the response (GitHub
  // generates repo archives on demand) — label the wait so it doesn't read as stuck.
  const preparing = d.status === 'active' && d.completed_bytes === 0
  const act = async (fn: () => Promise<unknown>) => {
    try {
      await fn()
    } finally {
      onChanged()
    }
  }
  return (
    <li className="border-b border-slate-800/60 px-4 py-2.5">
      <div className="flex items-center justify-between gap-2">
        <span className="min-w-0 flex-1 truncate text-sm text-slate-200" title={d.label}>
          {d.kind === 'folder' && (
            <button
              onClick={() => setExpanded((e) => !e)}
              className="mr-1 text-slate-500 hover:text-slate-300"
              title={expanded ? 'Hide files' : 'Show files'}
            >
              {expanded ? '▾' : '▸'}
            </button>
          )}
          {d.label}
        </span>
        <div className="flex shrink-0 items-center gap-1.5">
          {d.status === 'error' && (
            <button
              onClick={async () => {
                setRetrying(true)
                await act(() => retryDownload(d.id)).finally(() => setRetrying(false))
              }}
              disabled={retrying}
              className="rounded border border-slate-700 px-1.5 py-0.5 text-xs text-sky-300 hover:bg-slate-800 disabled:opacity-50"
              title="Retry — folders only re-fetch the missing files"
            >
              {retrying ? '…' : 'Retry'}
            </button>
          )}
          {d.status === 'active' && (
            <button onClick={() => act(() => pauseDownload(d.id))}
              className="rounded px-1.5 py-0.5 text-xs text-slate-400 hover:bg-slate-800" title="Pause">
              ⏸
            </button>
          )}
          {d.status === 'paused' && (
            <button onClick={() => act(() => resumeDownload(d.id))}
              className="rounded px-1.5 py-0.5 text-xs text-sky-300 hover:bg-slate-800" title="Resume">
              ▶
            </button>
          )}
          {d.status === 'complete' && (
            <button
              onClick={() => revealDownload(d.id)}
              className="rounded border border-slate-700 px-1.5 py-0.5 text-xs text-slate-300 hover:bg-slate-800"
              title="Show in Finder"
            >
              📂 Show
            </button>
          )}
          {d.status !== 'removed' && (
            <button onClick={() => act(() => cancelDownload(d.id))}
              className="rounded px-1.5 py-0.5 text-xs text-slate-500 hover:text-red-300 hover:bg-slate-800"
              title={d.status === 'complete' ? 'Remove from list' : 'Cancel'}>
              ✕
            </button>
          )}
        </div>
      </div>

      {(d.status === 'active' || d.status === 'paused' || d.status === 'waiting') && (
        <div className="mt-1.5 h-1.5 overflow-hidden rounded-full bg-slate-800">
          {d.total_bytes && !preparing ? (
            <div
              className="h-full rounded-full bg-sky-500 transition-all"
              style={{ width: `${pct}%` }}
            />
          ) : (
            // Size unknown (GitHub streams repo zips without a length) or still
            // connecting — a sweeping stripe keeps visible motion so it never reads
            // as frozen.
            <div className="h-full w-1/4 animate-progress-sweep rounded-full bg-sky-500/70" />
          )}
        </div>
      )}

      <div className="mt-1 flex items-center justify-between text-xs">
        <span className={STATUS_COLOR[d.status] ?? 'text-slate-400'}>
          {preparing
            ? 'preparing — GitHub is generating the file…'
            : d.status === 'error' && d.error
              ? `error — ${d.error}`
              : d.status}
        </span>
        <span className="tabular-nums text-slate-500">
          {d.total_bytes
            ? `${fmtBytes(d.completed_bytes)} / ${d.total_is_estimate ? '~' : ''}${fmtBytes(d.total_bytes)}`
            : preparing
              ? ''
              : `${fmtBytes(d.completed_bytes)} · size unknown`}
          {d.status === 'active' && d.speed_bytes > 0 && ` · ${fmtBytes(d.speed_bytes)}/s`}
          {d.status === 'active' &&
            d.speed_bytes > 0 &&
            d.total_bytes > d.completed_bytes &&
            ` · ${d.total_is_estimate ? '~' : ''}${fmtEta(d.total_bytes - d.completed_bytes, d.speed_bytes)}`}
        </span>
      </div>

      {expanded && d.kind === 'folder' && (
        <FileList id={d.id} tick={d.completed_bytes} />
      )}
    </li>
  )
}

const TERMINAL = new Set(['complete', 'error', 'removed'])

export function DownloadsPanel({
  available,
  downloadDir,
  freeBytes,
  items,
  onChanged,
  onClose,
}: {
  available: boolean
  downloadDir: string
  freeBytes: number | null
  items: DownloadItem[]
  onChanged: () => void
  onClose: () => void
}) {
  const anyFinished = items.some((d) => TERMINAL.has(d.status))
  return (
    <div className="fixed inset-0 z-50 flex justify-end">
      <div className="absolute inset-0 bg-black/50" onClick={onClose} />
      <div className="relative z-10 flex h-full w-full max-w-md flex-col bg-slate-900 shadow-2xl">
        <div className="flex items-center justify-between border-b border-slate-800 px-4 py-3">
          <div className="min-w-0">
            <div className="text-sm font-semibold text-slate-100">Downloads</div>
            {available && downloadDir && (
              <div className="truncate text-xs text-slate-500" title={downloadDir}>
                saving to {downloadDir}
                {freeBytes != null && ` · ${fmtBytes(freeBytes)} free`}
              </div>
            )}
          </div>
          <div className="flex shrink-0 items-center gap-1.5">
            {anyFinished && (
              <button
                onClick={async () => {
                  await clearFinishedDownloads()
                  onChanged()
                }}
                className="rounded-md border border-slate-700 px-2 py-1 text-xs text-slate-400 hover:bg-slate-800"
              >
                Clear finished
              </button>
            )}
            <button onClick={onClose}
              className="rounded-md px-2 py-1 text-slate-400 hover:bg-slate-800 hover:text-slate-200"
              aria-label="Close">
              ✕
            </button>
          </div>
        </div>

        {!available ? (
          <div className="p-4 text-sm text-slate-400">
            The download manager needs <span className="font-mono text-slate-200">aria2</span>.
            Install it with <span className="font-mono text-slate-200">brew install aria2</span> and
            restart the backend.
          </div>
        ) : items.length === 0 ? (
          <p className="p-6 text-center text-sm text-slate-600">
            No downloads yet. Use the download buttons in the inspector.
          </p>
        ) : (
          <ul className="flex-1 overflow-y-auto">
            {items.map((d) => (
              <DownloadRow key={d.id} d={d} onChanged={onChanged} />
            ))}
          </ul>
        )}
      </div>
    </div>
  )
}
