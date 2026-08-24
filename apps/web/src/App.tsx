import { useCallback, useState } from 'react'
import { deleteSearch, enqueueDownload, shutdownApp, startIndex } from './api'
import { DownloadsPanel } from './components/DownloadsPanel'
import { FacetSidebar } from './components/FacetSidebar'
import { HealthBanner } from './components/HealthBanner'
import { InspectDrawer } from './components/InspectDrawer'
import { RecentSearches } from './components/RecentSearches'
import { ResultsTable } from './components/ResultsTable'
import { SearchBar } from './components/SearchBar'
import { SearchNote, StatusBar } from './components/StatusBar'
import { useDownloads } from './hooks/useDownloads'
import { useSearch } from './hooks/useSearch'
import { useStatus } from './hooks/useStatus'
import { parseUrlState, useUrlState } from './hooks/useUrlState'
import {
  SORTS,
  type DownloadRequest,
  type FacetGroup,
  type FacetSelection,
  type SearchType,
  type SortKey,
} from './types'

// Restore search + inspector from the URL on load, so a refresh (or a reopened tab)
// lands exactly where the user was instead of a blank home.
const initial = parseUrlState()

export default function App() {
  const [searchId, setSearchId] = useState<number | null>(initial.searchId)
  const [sort, setSort] = useState<SortKey>('asset_added_desc')
  const [collapseForks, setCollapseForks] = useState(true)
  const [selection, setSelection] = useState<FacetSelection>({})
  const [submitError, setSubmitError] = useState<string | null>(null)
  // What the inspector is opened on: a repo, optionally deep-linked to a file.
  const [inspecting, setInspecting] = useState<{ fullName: string; file?: string } | null>(
    initial.inspecting,
  )
  const [showDownloads, setShowDownloads] = useState(false)
  const [downloadToast, setDownloadToast] = useState<string | null>(null)
  const [stopped, setStopped] = useState(false)

  // Keep the URL in sync so browser Back/Forward navigate the app (closing the
  // inspector = Back) and every state is shareable/restorable.
  useUrlState(
    { searchId, inspecting },
    useCallback((restored) => {
      setSearchId(restored.searchId)
      setInspecting(restored.inspecting)
    }, []),
  )

  const { data, facets, error } = useSearch(searchId, sort, collapseForks, selection)
  const downloads = useDownloads()
  // Declared before `busy` is derived below; polling just speeds up while work runs.
  const searchBusy = data !== null && data.status !== 'ready' && data.status !== 'error'
  const status = useStatus(searchBusy)

  async function enqueue(req: DownloadRequest) {
    setDownloadToast(null)
    try {
      await enqueueDownload(req)
      setShowDownloads(true)
      void downloads.refresh()
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e)
      // Surface the backend's "install aria2"/error detail if present.
      const detail = msg.includes('detail') ? msg.slice(msg.indexOf('—') + 1).trim() : msg
      setDownloadToast(detail)
    }
  }

  async function handleSubmit(keyword: string, searchType: SearchType) {
    setSubmitError(null)
    setSelection({})
    try {
      const { search_id } = await startIndex(keyword, searchType)
      setSearchId(search_id)
    } catch (e) {
      setSubmitError(e instanceof Error ? e.message : String(e))
    }
  }

  async function stopSearch() {
    if (searchId === null) return
    if (!window.confirm('Stop this search? Collected progress will be discarded.')) return
    try {
      await deleteSearch(searchId)
    } finally {
      setSearchId(null)
      setSelection({})
    }
  }

  async function quit() {
    const running = downloads.items.filter((d) =>
      ['waiting', 'active', 'paused'].includes(d.status),
    ).length
    const warning = running
      ? `\n\n${running} download${running > 1 ? 's are' : ' is'} still running. They will be paused and can be retried after you relaunch.`
      : ''
    if (!window.confirm(`Quit Provenance?${warning}`)) return
    try {
      await shutdownApp()
    } catch {
      // The process can win the race and die before the response lands — which means
      // the shutdown worked. Either way the app is going down.
    }
    setStopped(true)
  }

  if (stopped) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-slate-950 px-6 text-center text-slate-300">
        <div>
          <p className="text-lg font-semibold text-slate-100">Provenance stopped</p>
          <p className="mt-2 text-sm text-slate-500">
            The backend and dev server have shut down. Your index, download history and
            search history are saved — relaunch from the app to pick up where you left off.
          </p>
          <p className="mt-4 text-xs text-slate-600">You can close this tab.</p>
        </div>
      </div>
    )
  }

  function toggleFacet(group: FacetGroup, value: string) {
    setSelection((prev) => {
      const next = { ...prev }
      const set = new Set(next[group] ?? [])
      if (set.has(value)) set.delete(value)
      else set.add(value)
      next[group] = set
      return next
    })
  }

  // Filtering is now server-side (see useSearch → /search facet params), so results
  // reflect the whole matched set, not just the fetched page.
  const results = data?.results ?? []
  const busy = searchBusy

  // Files from the current results that belong to the repo being inspected — the drawer
  // highlights these in the file tree so you can see what actually matched.
  const inspectedMatches = new Set(
    inspecting
      ? results.filter((r) => r.repo_full_name === inspecting.fullName).map((r) => r.path)
      : [],
  )

  return (
    <div className="min-h-screen bg-slate-950 text-slate-100">
      <div className="mx-auto max-w-7xl px-4 py-6">
        <header className="mb-6 flex items-center justify-between gap-3">
          <h1 className="text-lg font-semibold">
            Provenance
            <span className="ml-2 text-sm font-normal text-slate-500">
              local index · sort by when an asset first appeared
            </span>
          </h1>
          <div className="flex shrink-0 items-center gap-2">
            <button
              onClick={() => setShowDownloads(true)}
              className="relative rounded-md border border-slate-700 px-3 py-1.5 text-sm text-slate-300 hover:bg-slate-800"
            >
              Downloads
              {downloads.activeCount > 0 && (
                <span className="ml-1.5 rounded-full bg-sky-600 px-1.5 py-0.5 text-xs font-medium text-white">
                  {downloads.activeCount}
                </span>
              )}
            </button>
            {/* This is a local desktop-style app; quitting it should not require
                hunting for a terminal to kill uvicorn in. */}
            <button
              onClick={quit}
              title="Stop the backend and dev server"
              className="rounded-md border border-slate-800 px-3 py-1.5 text-sm text-slate-500 hover:border-red-900/70 hover:bg-red-950/40 hover:text-red-300"
            >
              Quit
            </button>
          </div>
        </header>

        <SearchBar
          onSubmit={handleSubmit}
          onInspectRepo={(fullName) => setInspecting({ fullName })}
          busy={busy}
        />
        {(submitError || error) && (
          <p className="mt-3 rounded-md border border-red-800 bg-red-950/50 px-3 py-2 text-sm text-red-300">
            {submitError ?? error}
          </p>
        )}
        <HealthBanner status={status} />

        {data && (
          <>
            <div className="mt-5 flex flex-wrap items-center justify-between gap-3">
              <StatusBar search={data} shown={results.length} />
              <div className="flex items-center gap-3 text-sm">
                {busy && (
                  <button
                    onClick={stopSearch}
                    className="rounded-md border border-red-900/70 px-2.5 py-1.5 text-xs text-red-300 hover:bg-red-950/50"
                    title="Cancel this search and free the GitHub rate limit"
                  >
                    ⏹ Stop search
                  </button>
                )}
                <label className="flex items-center gap-1.5 text-slate-400">
                  <input
                    type="checkbox"
                    checked={collapseForks}
                    onChange={(e) => setCollapseForks(e.target.checked)}
                    className="accent-sky-500"
                  />
                  Hide forks
                </label>
                <select
                  value={sort}
                  onChange={(e) => setSort(e.target.value as SortKey)}
                  className="rounded-md border border-slate-700 bg-slate-800 px-2 py-1.5 text-sm text-slate-200"
                >
                  {(Object.entries(SORTS) as [SortKey, string][]).map(([k, label]) => (
                    <option key={k} value={k}>
                      {label}
                    </option>
                  ))}
                </select>
              </div>
            </div>

            {/* Full width, under the status row: these explanations are sentences, not
                chips, and must not be squeezed into a column. */}
            <SearchNote
              search={data}
              onRetry={() => handleSubmit(data.keyword, data.search_type as SearchType)}
            />

            <div className="mt-4 flex gap-6">
              {facets && (
                <FacetSidebar
                  facets={facets}
                  selection={selection}
                  onToggle={toggleFacet}
                  onClear={() => setSelection({})}
                />
              )}
              <main className="min-w-0 flex-1">
                {results.length === 0 && busy ? (
                  // Never say "no results" while we're still collecting — it reads
                  // as failure when the search simply hasn't finished.
                  <div className="py-12 text-center">
                    <div className="mx-auto mb-3 h-1.5 w-48 overflow-hidden rounded-full bg-slate-800">
                      <div className="h-full w-1/4 animate-progress-sweep rounded-full bg-sky-500/70" />
                    </div>
                    <p className="text-sm text-slate-500">
                      Collecting matches from GitHub — results appear here as they land.
                    </p>
                  </div>
                ) : (
                  <ResultsTable
                    rows={results}
                    onOpenRepo={(fullName) => setInspecting({ fullName })}
                    onOpenFile={(fullName, file) => setInspecting({ fullName, file })}
                  />
                )}
              </main>
            </div>
          </>
        )}

        {!data && !submitError && <RecentSearches onOpen={setSearchId} />}
      </div>

      {downloadToast && (
        <div className="fixed bottom-4 left-1/2 z-[60] -translate-x-1/2 rounded-md border border-amber-700/60 bg-amber-950/80 px-4 py-2 text-sm text-amber-200 shadow-lg">
          {downloadToast}
          <button onClick={() => setDownloadToast(null)} className="ml-3 text-amber-400 hover:text-amber-200">
            ✕
          </button>
        </div>
      )}

      {inspecting && (
        <InspectDrawer
          key={`${inspecting.fullName}:${inspecting.file ?? ''}`}
          fullName={inspecting.fullName}
          initialFile={inspecting.file}
          matchedPaths={inspectedMatches}
          onEnqueue={enqueue}
          onOpenDownloads={() => setShowDownloads(true)}
          onClose={() => setInspecting(null)}
        />
      )}

      {showDownloads && (
        <DownloadsPanel
          available={downloads.available}
          downloadDir={downloads.downloadDir}
          freeBytes={downloads.freeBytes}
          items={downloads.items}
          onChanged={downloads.refresh}
          onClose={() => setShowDownloads(false)}
        />
      )}
    </div>
  )
}
