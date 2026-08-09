import { useEffect, useState, type FormEvent } from 'react'
import { detectQuery } from '../api'
import type { DetectResponse, SearchType } from '../types'

// "Auto" is the default and should stay first — the dropdown exists to override a wrong
// guess, not to make the user classify their own input before they can search.
const TYPE_LABELS: Record<SearchType, string> = {
  auto: 'Auto-detect',
  keyword: 'In file contents',
  phrase: 'Exact phrase',
  path: 'Path / folder',
  filename: 'Filename',
  ext: 'Extension',
  language: 'Language',
  repo: 'Repository',
  raw: 'GitHub syntax',
}

// Short label for the chip that shows what auto-detect landed on.
const TYPE_CHIPS: Record<SearchType, string> = {
  auto: 'auto',
  keyword: 'keyword',
  phrase: 'phrase',
  path: 'path',
  filename: 'filename',
  ext: 'extension',
  language: 'language',
  repo: 'repo',
  raw: 'raw query',
}

export function SearchBar({
  onSubmit,
  onInspectRepo,
  busy,
}: {
  onSubmit: (keyword: string, searchType: SearchType) => void
  onInspectRepo: (fullName: string) => void
  busy: boolean
}) {
  const [keyword, setKeyword] = useState('')
  const [searchType, setSearchType] = useState<SearchType>('auto')
  const [detected, setDetected] = useState<DetectResponse | null>(null)

  // Ask the backend what it would do with this input — same code path /index runs, so
  // the preview can't drift from reality. Debounced; a failed probe just hides the chip.
  useEffect(() => {
    const kw = keyword.trim()
    if (!kw || searchType !== 'auto') {
      setDetected(null)
      return
    }
    let cancelled = false
    const timer = window.setTimeout(() => {
      detectQuery(kw)
        .then((d) => !cancelled && setDetected(d))
        .catch(() => !cancelled && setDetected(null))
    }, 200)
    return () => {
      cancelled = true
      window.clearTimeout(timer)
    }
  }, [keyword, searchType])

  function handleSubmit(e: FormEvent) {
    e.preventDefault()
    const kw = keyword.trim()
    if (!kw) return
    // A repo link isn't a search — open it in the inspector, which is what was meant.
    if (detected?.search_type === 'repo' && detected.repo) {
      onInspectRepo(detected.repo)
      return
    }
    onSubmit(kw, searchType)
  }

  const isRepo = detected?.search_type === 'repo'

  return (
    <form onSubmit={handleSubmit}>
      <div className="flex gap-2">
        <input
          value={keyword}
          onChange={(e) => setKeyword(e.target.value)}
          placeholder="Anything: HighlightPlus · Assets/HighlightPlus · Highlight.cs · *.shader · a GitHub link"
          className="flex-1 rounded-md border border-slate-700 bg-slate-800 px-3 py-2 text-sm text-slate-100 placeholder:text-slate-500 focus:border-sky-500 focus:outline-none"
        />
        <button
          type="submit"
          disabled={busy || !keyword.trim()}
          className="rounded-md bg-sky-600 px-4 py-2 text-sm font-medium text-white hover:bg-sky-500 disabled:opacity-40"
        >
          {isRepo ? 'Open repo' : busy ? 'Indexing…' : 'Search'}
        </button>
      </div>

      <div className="mt-2 flex flex-wrap items-center gap-2 text-xs text-slate-500">
        {detected && searchType === 'auto' ? (
          <>
            <span className="rounded-full bg-slate-800 px-2 py-0.5 font-medium text-sky-300">
              {TYPE_CHIPS[detected.search_type]}
            </span>
            <span>{detected.explanation}</span>
            {detected.normalized_query && !isRepo && (
              <code className="rounded bg-slate-900 px-1.5 py-0.5 text-slate-400">
                {detected.normalized_query}
              </code>
            )}
          </>
        ) : (
          <span>
            {searchType === 'auto'
              ? 'Paste a name, a path, a filename, an extension, or a GitHub link — the type is worked out for you.'
              : `Forcing “${TYPE_LABELS[searchType]}” — auto-detection is off.`}
          </span>
        )}
        <label className="ml-auto flex items-center gap-1.5">
          <span className="text-slate-600">search as</span>
          <select
            value={searchType}
            onChange={(e) => setSearchType(e.target.value as SearchType)}
            className="rounded border border-slate-700 bg-slate-800 px-1.5 py-1 text-xs text-slate-300"
          >
            {(Object.keys(TYPE_LABELS) as SearchType[])
              .filter((t) => t !== 'repo' && t !== 'raw')
              .map((t) => (
                <option key={t} value={t}>
                  {TYPE_LABELS[t]}
                </option>
              ))}
          </select>
        </label>
      </div>
    </form>
  )
}
