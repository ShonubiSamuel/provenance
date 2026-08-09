import type { SearchResponse } from '../types'

const STATUS_STYLES: Record<string, string> = {
  pending: 'bg-slate-700 text-slate-200',
  discovering: 'bg-amber-900/60 text-amber-200',
  enriching: 'bg-sky-900/60 text-sky-200',
  ready: 'bg-emerald-900/60 text-emerald-200',
  error: 'bg-red-900/60 text-red-200',
}

const n = (v: number) => v.toLocaleString()

/** The backend's own explanation of an imperfect outcome, verbatim. This is the line
 * that used to be missing entirely: why a search ended empty, stalled, or incomplete.
 * Rendered full-width by the caller so a long sentence stays readable. */
export function SearchNote({
  search,
  onRetry,
}: {
  search: SearchResponse
  onRetry: () => void
}) {
  if (!search.note) return null
  const failed = search.status === 'error'
  return (
    <div
      className={`mt-3 flex flex-wrap items-center gap-3 rounded-md border px-3 py-2 text-sm ${
        failed
          ? 'border-red-800/70 bg-red-950/40 text-red-200'
          : 'border-amber-800/60 bg-amber-950/30 text-amber-200'
      }`}
    >
      <p className="min-w-0 flex-1">{search.note}</p>
      {failed && (
        // Most of these failures are transient on GitHub's side, so the single most
        // useful next action is "do it again" — without retyping the query.
        <button
          onClick={onRetry}
          className="shrink-0 rounded-md border border-red-700/70 px-2.5 py-1 text-xs font-medium text-red-200 hover:bg-red-900/40"
        >
          Try again
        </button>
      )}
    </div>
  )
}

export function StatusBar({ search, shown }: { search: SearchResponse; shown: number }) {
  const live = search.status !== 'ready' && search.status !== 'error'
  const discovering = search.status === 'discovering' || search.status === 'pending'
  const collected = search.total_matches
  const reported = search.reported_matches

  return (
    <div className="flex min-w-0 flex-wrap items-center gap-3 text-sm text-slate-400">
      <span
        className={`rounded-full px-2.5 py-0.5 text-xs font-medium ${STATUS_STYLES[search.status] ?? STATUS_STYLES.pending}`}
      >
        {search.status}
        {live && '…'}
      </span>

      {discovering ? (
        // Say what we HAVE, not just what GitHub claims exists. "0 collected of
        // ≈212,860,928 reported" is the honest reading of a stalled collection; a lone
        // eight-figure number reads like progress when nothing is happening.
        <span>
          <span className="font-medium text-slate-200">{n(collected)}</span> collected
          {reported > 0 && (
            <>
              {' '}
              of <span className="font-medium text-slate-200">≈{n(reported)}</span>{' '}
              reported by GitHub
            </>
          )}{' '}
          for <span className="font-medium text-slate-200">“{search.keyword}”</span>
        </span>
      ) : (
        <span>
          <span className="font-medium text-slate-200">{n(shown)}</span> shown of{' '}
          <span className="font-medium text-slate-200">{n(collected)}</span> collected
          {reported > collected && <> (GitHub reported ≈{n(reported)})</>} for{' '}
          <span className="font-medium text-slate-200">“{search.keyword}”</span>
        </span>
      )}

      {search.normalized_query && (
        <code
          className="rounded bg-slate-900 px-1.5 py-0.5 text-xs text-slate-500"
          title="The query sent to GitHub"
        >
          {search.normalized_query}
        </code>
      )}

      {discovering && (
        <span className="text-xs text-slate-500">
          GitHub allows 10 code searches/minute — large collections take minutes
        </span>
      )}
      {live && !discovering && (
        <span className="text-xs text-slate-500">
          results stream in as enrichment jobs finish — dates fill in live
        </span>
      )}
      {search.sampled && (
        <span className="rounded-md border border-amber-700/60 bg-amber-950/50 px-2 py-0.5 text-xs text-amber-300">
          sampled — too broad to index in full
        </span>
      )}
      {search.truncated && !search.sampled && (
        <span
          className="rounded-md border border-amber-700/60 bg-amber-950/50 px-2 py-0.5 text-xs text-amber-300"
          title="GitHub's 1000-result ceiling was hit somewhere in this search even after size-band splitting. Facet counts are biased toward what was collected."
        >
          ⚠ truncated — facets may be biased
        </span>
      )}
    </div>
  )
}
