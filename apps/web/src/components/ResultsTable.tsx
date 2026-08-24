import { Fragment, useMemo, useState } from 'react'
import type { ResultRow } from '../types'

function fmtDate(iso: string | null): string {
  if (!iso) return '—'
  return new Date(iso).toLocaleDateString(undefined, {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
  })
}

function fmtStars(n: number | null): string {
  if (n == null) return '—'
  return n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n)
}

function CopyLinkButton({ url }: { url: string }) {
  const [copied, setCopied] = useState(false)
  return (
    <button
      onClick={async (e) => {
        e.stopPropagation()
        await navigator.clipboard.writeText(url)
        setCopied(true)
        setTimeout(() => setCopied(false), 1200)
      }}
      className="shrink-0 rounded px-1 text-xs text-slate-600 hover:text-slate-300"
      title="Copy GitHub link"
    >
      {copied ? '✓' : '⧉'}
    </button>
  )
}

function FileCell({
  r,
  onOpenFile,
}: {
  r: ResultRow
  onOpenFile: (repoFullName: string, path: string) => void
}) {
  return (
    <td className="max-w-md px-3 py-2">
      <div className="flex items-center gap-1.5">
        <button
          onClick={() => onOpenFile(r.repo_full_name, r.path)}
          className="min-w-0 flex-1 truncate text-left text-slate-300 hover:text-sky-300 hover:underline"
          title={`Open ${r.path} in the inspector`}
        >
          {r.path}
        </button>
        <CopyLinkButton url={r.github_url} />
      </div>
      {r.snippet && (
        <details className="mt-0.5">
          <summary className="cursor-pointer select-none text-xs text-slate-500 hover:text-slate-400">
            snippet
          </summary>
          <pre className="mt-1 max-h-40 overflow-auto rounded bg-slate-950 p-2 text-xs text-slate-400">
            {r.snippet}
          </pre>
        </details>
      )}
    </td>
  )
}

function AssetAddedCell({ r }: { r: ResultRow }) {
  return (
    <td className="whitespace-nowrap px-3 py-2 tabular-nums text-slate-300">
      {fmtDate(r.asset_first_appeared_at)}
      {r.history_method && (
        <span
          className="ml-1.5 rounded bg-slate-800 px-1 py-0.5 text-[10px] text-slate-500"
          title={
            r.history_method === 'api-approx'
              ? 'Approximate: commits-for-path API, does not follow renames'
              : 'Precise: from a clone with rename following'
          }
        >
          {r.history_method === 'api-approx' ? '≈' : '✓'}
        </span>
      )}
    </td>
  )
}

export function ResultsTable({
  rows,
  onOpenRepo,
  onOpenFile,
}: {
  rows: ResultRow[]
  onOpenRepo: (repoFullName: string) => void
  onOpenFile: (repoFullName: string, path: string) => void
}) {
  const [expanded, setExpanded] = useState<Set<string>>(new Set())

  // Group by repo, preserving the server's sort order (a group's position follows its
  // first/best-ranked match) — one row per repo instead of the same name repeated once
  // per matched file.
  const groups = useMemo(() => {
    const order: string[] = []
    const byRepo = new Map<string, ResultRow[]>()
    for (const r of rows) {
      let bucket = byRepo.get(r.repo_full_name)
      if (!bucket) {
        bucket = []
        byRepo.set(r.repo_full_name, bucket)
        order.push(r.repo_full_name)
      }
      bucket.push(r)
    }
    return order.map((name) => ({ name, items: byRepo.get(name)! }))
  }, [rows])

  if (rows.length === 0) {
    return (
      <p className="py-12 text-center text-sm text-slate-500">
        No results match the current filters.
      </p>
    )
  }

  function toggle(name: string) {
    setExpanded((prev) => {
      const next = new Set(prev)
      if (next.has(name)) next.delete(name)
      else next.add(name)
      return next
    })
  }

  return (
    <div className="overflow-x-auto rounded-lg border border-slate-800">
      <table className="w-full text-left text-sm">
        <thead className="bg-slate-900 text-xs uppercase tracking-wide text-slate-500">
          <tr>
            <th className="px-3 py-2">Repository</th>
            <th className="px-3 py-2">File</th>
            <th className="px-3 py-2">Lang</th>
            <th className="px-3 py-2 text-right">★</th>
            <th className="px-3 py-2" title="When this asset path first appeared in the repo's history — the headline sort">
              Asset added
            </th>
            <th className="px-3 py-2">Repo created</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-800/70">
          {groups.map(({ name, items }) => {
            // A repo matched exactly once needs no grouping chrome — render it like any
            // other row.
            if (items.length === 1) {
              const r = items[0]
              return (
                <tr key={name} className="hover:bg-slate-900/60">
                  <td className="max-w-56 px-3 py-2">
                    <button
                      onClick={() => onOpenRepo(r.repo_full_name)}
                      className="block max-w-full truncate text-left font-medium text-sky-400 hover:underline"
                      title={`Browse ${r.repo_full_name} in the inspector`}
                    >
                      {r.repo_full_name}
                    </button>
                  </td>
                  <FileCell r={r} onOpenFile={onOpenFile} />
                  <td className="px-3 py-2 text-slate-400">{r.detected_language ?? '—'}</td>
                  <td className="px-3 py-2 text-right tabular-nums text-slate-400">
                    {fmtStars(r.stars)}
                  </td>
                  <AssetAddedCell r={r} />
                  <td className="whitespace-nowrap px-3 py-2 tabular-nums text-slate-500">
                    {fmtDate(r.repo_created_at)}
                  </td>
                </tr>
              )
            }

            const isOpen = expanded.has(name)
            const head = items[0]
            return (
              <Fragment key={name}>
                <tr className="bg-slate-950/40 hover:bg-slate-900/60">
                  <td className="max-w-56 px-3 py-2">
                    <div className="flex items-center gap-1.5">
                      <button
                        onClick={() => toggle(name)}
                        className="shrink-0 text-slate-500 hover:text-slate-300"
                        title={isOpen ? 'Collapse' : `Show all ${items.length} matched files`}
                      >
                        {isOpen ? '▾' : '▸'}
                      </button>
                      <button
                        onClick={() => onOpenRepo(name)}
                        className="min-w-0 flex-1 truncate text-left font-medium text-sky-400 hover:underline"
                        title={`Browse ${name} in the inspector`}
                      >
                        {name}
                      </button>
                    </div>
                  </td>
                  <td className="px-3 py-2">
                    <button
                      onClick={() => toggle(name)}
                      className="text-xs text-slate-500 hover:text-slate-300"
                    >
                      {items.length} matched files{isOpen ? ' — collapse' : ' — show all'}
                    </button>
                  </td>
                  <td className="px-3 py-2 text-slate-600">—</td>
                  <td className="px-3 py-2 text-right tabular-nums text-slate-400">
                    {fmtStars(head.stars)}
                  </td>
                  <td className="px-3 py-2 text-slate-600">—</td>
                  <td className="whitespace-nowrap px-3 py-2 tabular-nums text-slate-500">
                    {fmtDate(head.repo_created_at)}
                  </td>
                </tr>
                {isOpen &&
                  items.map((r) => (
                    <tr key={`${r.repo_full_name}:${r.path}`} className="hover:bg-slate-900/60">
                      <td className="px-3 py-2" />
                      <FileCell r={r} onOpenFile={onOpenFile} />
                      <td className="px-3 py-2 text-slate-400">{r.detected_language ?? '—'}</td>
                      <td className="px-3 py-2 text-right tabular-nums text-slate-600">·</td>
                      <AssetAddedCell r={r} />
                      <td className="px-3 py-2 text-slate-600">·</td>
                    </tr>
                  ))}
              </Fragment>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}
