import type {
  BlobResponse,
  DetectResponse,
  DownloadFilesResponse,
  DownloadItem,
  DownloadRequest,
  DownloadsResponse,
  RecentSearch,
  FacetSelection,
  FacetsResponse,
  SearchResponse,
  SearchType,
  SizesResponse,
  SortKey,
  StatusResponse,
  TreeResponse,
} from './types'

// Proxied by Vite to the FastAPI backend (see vite.config.ts).
const BASE = '/api'

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(`${BASE}${path}`, init)
  if (!resp.ok) {
    // FastAPI puts the human-readable reason in `detail`. Surfacing that instead of the
    // raw JSON envelope is the difference between an explanation and a wall of braces.
    const body = await resp.text().catch(() => '')
    let detail = body
    try {
      const parsed = JSON.parse(body)
      if (typeof parsed?.detail === 'string') detail = parsed.detail
    } catch {
      /* not JSON — keep the raw text */
    }
    throw new Error(detail || `${resp.status} ${resp.statusText}`)
  }
  return resp.json() as Promise<T>
}

export function startIndex(
  keyword: string,
  searchType: SearchType,
): Promise<{
  search_id: number
  status: string
  search_type: SearchType
  normalized_query: string
  explanation: string
}> {
  return request('/index', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ keyword, search_type: searchType }),
  })
}

/** Preview how an input will be interpreted, without running anything. */
export function detectQuery(q: string): Promise<DetectResponse> {
  return request(`/detect?q=${encodeURIComponent(q)}`)
}

/** Backend + GitHub health. Cheap and safe to poll. */
export function fetchStatus(): Promise<StatusResponse> {
  return request('/status')
}

/** Quit the app: stops the backend and the dev server it launched. The response
 * arrives before the process exits, so the UI can show a clean stopped screen. */
export function shutdownApp(): Promise<{ status: string; stopping: string[] }> {
  return request('/shutdown', { method: 'POST' })
}

/** Stable string key for a selection, order-independent — for React effect deps so
 * polling restarts only when the chosen filters actually change. */
export function selectionKey(filters: FacetSelection): string {
  return Object.entries(filters)
    .filter(([, set]) => set && set.size > 0)
    .map(([group, set]) => `${group}:${[...set!].sort().join(',')}`)
    .sort()
    .join('|')
}

export function fetchSearch(
  searchId: number,
  sort: SortKey,
  collapseForks: boolean,
  filters: FacetSelection,
  limit = 500,
): Promise<SearchResponse> {
  const params = new URLSearchParams({
    sort,
    limit: String(limit),
    collapse_forks: String(collapseForks),
  })
  for (const [group, set] of Object.entries(filters)) {
    if (!set) continue
    for (const value of set) params.append(group, value)
  }
  return request(`/search/${searchId}?${params}`)
}

// --- Repo inspection --- //
export function fetchTree(
  owner: string,
  repo: string,
  path = '',
  ref?: string,
): Promise<TreeResponse> {
  const params = new URLSearchParams({ path })
  if (ref) params.set('ref', ref)
  return request(`/repo/${owner}/${repo}/contents?${params}`)
}

export function fetchBlob(
  owner: string,
  repo: string,
  path: string,
  ref?: string,
): Promise<BlobResponse> {
  const params = new URLSearchParams({ path })
  if (ref) params.set('ref', ref)
  return request(`/repo/${owner}/${repo}/blob?${params}`)
}

// --- Download manager --- //
export function enqueueDownload(req: DownloadRequest): Promise<DownloadItem> {
  return request('/downloads', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(req),
  })
}

export function fetchDownloads(): Promise<DownloadsResponse> {
  return request('/downloads')
}

// Action endpoints return 204 (no body), so they don't go through `request`.
async function downloadAction(path: string, method: 'POST' | 'DELETE'): Promise<void> {
  const resp = await fetch(`${BASE}${path}`, { method })
  if (!resp.ok) throw new Error(`${resp.status} ${resp.statusText}`)
}

export const pauseDownload = (id: number) => downloadAction(`/downloads/${id}/pause`, 'POST')
export const resumeDownload = (id: number) => downloadAction(`/downloads/${id}/resume`, 'POST')
export const cancelDownload = (id: number) => downloadAction(`/downloads/${id}`, 'DELETE')

export function retryDownload(id: number): Promise<DownloadItem> {
  return request(`/downloads/${id}/retry`, { method: 'POST' })
}

export function fetchDownloadFiles(id: number): Promise<DownloadFilesResponse> {
  return request(`/downloads/${id}/files`)
}

export function clearFinishedDownloads(): Promise<{ removed: number }> {
  return request('/downloads/clear-finished', { method: 'POST' })
}

export const revealDownload = (id: number) =>
  downloadAction(`/downloads/${id}/reveal`, 'POST')

export function fetchRecentSearches(): Promise<{ searches: RecentSearch[] }> {
  return request('/searches')
}

/** Delete a finished search, or cancel an in-flight one (its discovery aborts at the
 * next heartbeat). */
export const deleteSearch = (id: number) => downloadAction(`/searches/${id}`, 'DELETE')

export function fetchSizes(
  owner: string,
  repo: string,
  ref?: string,
): Promise<SizesResponse> {
  const params = new URLSearchParams()
  if (ref) params.set('ref', ref)
  const qs = params.toString()
  return request(`/repo/${owner}/${repo}/sizes${qs ? `?${qs}` : ''}`)
}

/** Direct-download URLs — used as plain <a href> so the browser handles the download
 * via the Content-Disposition header. Same-origin through the Vite proxy. */
export function fileDownloadUrl(owner: string, repo: string, path: string, ref?: string): string {
  const params = new URLSearchParams({ path })
  if (ref) params.set('ref', ref)
  return `${BASE}/repo/${owner}/${repo}/file?${params}`
}

export function archiveUrl(owner: string, repo: string, path = '', ref?: string): string {
  const params = new URLSearchParams()
  if (path) params.set('path', path)
  if (ref) params.set('ref', ref)
  const qs = params.toString()
  return `${BASE}/repo/${owner}/${repo}/archive${qs ? `?${qs}` : ''}`
}

export function fetchFacets(
  searchId: number,
  collapseForks: boolean,
  filters: FacetSelection,
): Promise<FacetsResponse> {
  // Same filter params as /search: counts come back cross-filtered by the active
  // selection (the server excludes each group's own filter).
  const params = new URLSearchParams({ collapse_forks: String(collapseForks) })
  for (const [group, set] of Object.entries(filters)) {
    if (!set) continue
    for (const value of set) params.append(group, value)
  }
  return request(`/facets/${searchId}?${params}`)
}
