import { useCallback, useEffect, useState } from 'react'
import { deleteSearch, fetchRecentSearches } from '../api'
import type { RecentSearch } from '../types'

const STATUS_STYLES: Record<string, string> = {
  ready: 'bg-emerald-900/60 text-emerald-200',
  error: 'bg-red-900/60 text-red-200',
  enriching: 'bg-sky-900/60 text-sky-200',
  discovering: 'bg-amber-900/60 text-amber-200',
  pending: 'bg-slate-700 text-slate-200',
}

const IN_FLIGHT = new Set(['pending', 'discovering', 'enriching'])

function fmtWhen(iso: string | null): string {
  if (!iso) return ''
  const d = new Date(iso)
  const days = Math.floor((Date.now() - d.getTime()) / 86_400_000)
  if (days === 0) return d.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' })
  if (days === 1) return 'yesterday'
  if (days < 7) return `${days} days ago`
  return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
}

export function RecentSearches({ onOpen }: { onOpen: (searchId: number) => void }) {
  const [searches, setSearches] = useState<RecentSearch[] | null>(null)

  const refresh = useCallback(() => {
    fetchRecentSearches()
      .then((r) => setSearches(r.searches))
      .catch(() => setSearches((prev) => prev ?? []))
  }, [])

  // Refresh on mount, and keep polling while any search is still working so
  // "discovering" rows visibly progress to ready/error instead of looking frozen.
  useEffect(() => {
    refresh()
  }, [refresh])
  useEffect(() => {
    if (!searches?.some((s) => IN_FLIGHT.has(s.status))) return
    const t = window.setInterval(refresh, 4000)
    return () => window.clearInterval(t)
  }, [searches, refresh])

  async function remove(s: RecentSearch) {
    const inFlight = IN_FLIGHT.has(s.status)
    if (inFlight && !window.confirm(`Cancel the running search “${s.keyword}”?`)) return
    try {
      await deleteSearch(s.search_id)
    } finally {
      refresh()
    }
  }

  if (searches === null || searches.length === 0) {
    return (
      <p className="mt-16 text-center text-sm text-slate-600">
        Index a keyword to build a local, historical view of every repo using it.
      </p>
    )
  }

  return (
    <div className="mx-auto mt-10 max-w-2xl">
      <h2 className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-500">
        Recent searches
      </h2>
      <ul className="divide-y divide-slate-800/70 rounded-lg border border-slate-800">
        {searches.map((s) => (
          <li key={s.search_id} className="flex items-center hover:bg-slate-900">
            <button
              onClick={() => onOpen(s.search_id)}
              className="flex min-w-0 flex-1 items-center gap-3 px-4 py-2.5 text-left"
            >
              <span className="min-w-0 flex-1 truncate text-sm font-medium text-slate-200">
                {s.keyword}
                {s.search_type !== 'keyword' && (
                  <span className="ml-1.5 text-xs text-slate-500">{s.search_type}</span>
                )}
              </span>
              <span
                className={`shrink-0 rounded-full px-2 py-0.5 text-[11px] ${STATUS_STYLES[s.status] ?? STATUS_STYLES.pending}`}
              >
                {s.status}
                {IN_FLIGHT.has(s.status) && s.total_matches > 0 && ` · ${s.total_matches}`}
              </span>
              <span className="w-20 shrink-0 text-right text-xs tabular-nums text-slate-500">
                {s.status === 'ready' && s.total_matches ? `${s.total_matches} hits` : ''}
              </span>
              <span className="w-20 shrink-0 text-right text-xs text-slate-600">
                {fmtWhen(s.created_at)}
              </span>
            </button>
            <button
              onClick={() => remove(s)}
              className="shrink-0 px-3 py-2.5 text-xs text-slate-600 hover:text-red-300"
              title={IN_FLIGHT.has(s.status) ? 'Cancel this search' : 'Delete from history'}
            >
              ✕
            </button>
          </li>
        ))}
      </ul>
    </div>
  )
}
