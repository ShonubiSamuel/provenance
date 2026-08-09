import { useEffect, useMemo, useState } from 'react'
import { fetchFacets, fetchSearch, selectionKey } from '../api'
import type { FacetSelection, FacetsResponse, SearchResponse, SortKey } from '../types'

const ACTIVE = new Set(['pending', 'discovering', 'enriching'])
const POLL_MS = 2000
const ERROR_RETRY_MS = 4000

/** Polls /search + /facets while the backend pipeline is still running, then stops.
 * Re-runs when the search id, sort, fork-collapse toggle, or selected facets change.
 * Facets are fetched unfiltered (the sidebar shows every option); only results are
 * filtered server-side.
 */
export function useSearch(
  searchId: number | null,
  sort: SortKey,
  collapseForks: boolean,
  filters: FacetSelection,
) {
  const [data, setData] = useState<SearchResponse | null>(null)
  const [facets, setFacets] = useState<FacetsResponse | null>(null)
  const [error, setError] = useState<string | null>(null)

  // Derive a primitive key so the effect re-runs on filter *content* changes, not on
  // every new selection object identity from an unrelated re-render.
  const filterKey = useMemo(() => selectionKey(filters), [filters])

  useEffect(() => {
    if (searchId === null) {
      setData(null)
      setFacets(null)
      setError(null)
      return
    }
    let cancelled = false
    let timer: number | undefined

    async function tick() {
      try {
        const [s, f] = await Promise.all([
          fetchSearch(searchId!, sort, collapseForks, filters),
          fetchFacets(searchId!, collapseForks, filters),
        ])
        if (cancelled) return
        setData(s)
        setFacets(f)
        setError(null)
        if (ACTIVE.has(s.status)) timer = window.setTimeout(tick, POLL_MS)
      } catch (e) {
        if (cancelled) return
        setError(e instanceof Error ? e.message : String(e))
        timer = window.setTimeout(tick, ERROR_RETRY_MS)
      }
    }
    void tick()
    return () => {
      cancelled = true
      if (timer !== undefined) window.clearTimeout(timer)
    }
    // `filters` is intentionally tracked via `filterKey` (its stable content hash).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchId, sort, collapseForks, filterKey])

  return { data, facets, error }
}
