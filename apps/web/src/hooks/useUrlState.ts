import { useEffect, useRef } from 'react'

/** The app state that belongs in the URL: which search you're viewing and what repo/file
 * the inspector has open. Everything else (sort, facets) is ephemeral by design. */
export interface UrlState {
  searchId: number | null
  inspecting: { fullName: string; file?: string } | null
}

export function parseUrlState(): UrlState {
  const params = new URLSearchParams(window.location.hash.replace(/^#\/?/, ''))
  const s = params.get('s')
  const r = params.get('r')
  const f = params.get('f')
  return {
    searchId: s && /^\d+$/.test(s) ? Number(s) : null,
    inspecting: r ? { fullName: r, file: f ?? undefined } : null,
  }
}

function toHash(state: UrlState): string {
  const params = new URLSearchParams()
  if (state.searchId !== null) params.set('s', String(state.searchId))
  if (state.inspecting) {
    params.set('r', state.inspecting.fullName)
    if (state.inspecting.file) params.set('f', state.inspecting.file)
  }
  const qs = params.toString()
  return qs ? `#/${qs}` : ''
}

/** Two-way sync between app state and the URL hash, so the browser's Back/Forward
 * buttons work (closing the inspector = Back) and a refresh restores where you were.
 */
export function useUrlState(state: UrlState, onPop: (restored: UrlState) => void) {
  const lastPushed = useRef<string>(window.location.hash)

  // State → URL: push a history entry whenever the navigable state changes.
  useEffect(() => {
    const hash = toHash(state)
    if (hash === lastPushed.current) return
    lastPushed.current = hash
    // pathname+search preserved; empty hash means "home".
    const url = window.location.pathname + window.location.search + hash
    window.history.pushState(null, '', url)
  }, [state])

  // URL → state: browser Back/Forward fires popstate; restore without re-pushing.
  useEffect(() => {
    const handler = () => {
      lastPushed.current = window.location.hash
      onPop(parseUrlState())
    }
    window.addEventListener('popstate', handler)
    return () => window.removeEventListener('popstate', handler)
  }, [onPop])
}
