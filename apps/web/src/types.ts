// Mirrors apps/api/schemas.py — keep the two in sync by hand until we generate types
// from the OpenAPI schema.

export type SearchType =
  | 'auto'
  | 'keyword'
  | 'phrase'
  | 'path'
  | 'filename'
  | 'ext'
  | 'language'
  | 'repo'
  | 'raw'

/** What the backend decided a raw input means. Same code path /index runs, so the
 * preview shown under the box can never disagree with what actually executes. */
export interface DetectResponse {
  search_type: SearchType
  query: string
  normalized_query: string
  explanation: string
  repo: string | null
  path: string | null
  ref: string | null
}

/** Credentials, GitHub's mood, and our own pacing — polled so a stalled search always
 * has a stated reason instead of an endless spinner. */
export interface StatusResponse {
  credential: 'github-app' | 'personal-access-token' | 'none'
  credential_ok: boolean
  token_expires_at: string | null
  search_limit: number | null
  search_remaining: number | null
  search_reset_in: number | null
  github_degraded: boolean
  last_error: string | null
  buckets: Record<string, { rate_per_minute: number; tokens: number; parked_seconds: number; wait_seconds: number }>
  message: string | null
}

export type SearchStatus =
  | 'pending'
  | 'discovering'
  | 'enriching'
  | 'ready'
  | 'error'

export interface ResultRow {
  repo_full_name: string
  owner: string
  stars: number | null
  license_spdx: string | null
  path: string
  filename: string
  extension: string | null
  path_prefix: string | null
  detected_language: string | null
  snippet: string | null
  repo_created_at: string | null
  repo_pushed_at: string | null
  asset_first_appeared_at: string | null
  history_method: string | null
  github_url: string
}

export interface SearchResponse {
  search_id: number
  keyword: string
  search_type: string
  normalized_query: string
  status: SearchStatus
  truncated: boolean
  sampled: boolean
  total_matches: number // collected locally
  reported_matches: number // what GitHub claimed existed
  note: string | null // why the outcome is imperfect, in plain words
  results: ResultRow[]
}

export interface FacetValue {
  value: string
  count: number
}

export type FacetGroup =
  | 'languages'
  | 'extensions'
  | 'path_prefixes'
  | 'owners'
  | 'licenses'

export interface FacetsResponse {
  search_id: number
  facets: Record<string, FacetValue[]>
}

// Which facet values are selected per group. Sent to /search as repeatable query params
// keyed by group name (which matches the backend's FACET_COLUMNS keys).
export type FacetSelection = Partial<Record<FacetGroup, Set<string>>>

// --- Repo inspection --- //
export interface TreeEntry {
  name: string
  path: string
  type: 'dir' | 'file'
  size: number | null
  sha: string | null
}

export interface TreeResponse {
  owner: string
  repo: string
  ref: string | null
  path: string
  entries: TreeEntry[]
}

export interface BlobResponse {
  owner: string
  repo: string
  path: string
  size: number
  encoding: 'text' | 'binary' | 'too_large'
  text: string | null
}

export interface SizesResponse {
  owner: string
  repo: string
  ref: string | null
  truncated: boolean
  sizes: Record<string, number> // repo-relative folder path -> total bytes
}

// --- Download manager --- //
export interface DownloadItem {
  id: number
  kind: 'file' | 'repo' | 'folder'
  label: string
  status: string // waiting | active | paused | complete | error | removed
  total_bytes: number
  total_is_estimate: boolean
  completed_bytes: number
  speed_bytes: number
  progress: number
  file_count: number | null
  error: string | null
}

export interface DownloadsResponse {
  available: boolean
  download_dir: string
  free_bytes: number | null
  items: DownloadItem[]
}

export interface DownloadFile {
  path: string
  size: number
  status: string
  completed_bytes: number
}

export interface DownloadFilesResponse {
  id: number
  files: DownloadFile[]
}

export interface DownloadRequest {
  kind: 'file' | 'repo' | 'folder'
  owner: string
  repo: string
  path?: string
  ref?: string
}

export interface RecentSearch {
  search_id: number
  keyword: string
  search_type: string
  status: string
  truncated: boolean
  total_matches: number
  reported_matches: number
  note: string | null
  created_at: string | null
}

export const SORTS = {
  asset_added_desc: 'Asset added — newest',
  asset_added_asc: 'Asset added — oldest',
  repo_created_desc: 'Repo created — newest',
  repo_updated_desc: 'Recently pushed',
  stars_desc: 'Most stars',
  forks_desc: 'Most forks',
  relevance: 'Relevance',
} as const

export type SortKey = keyof typeof SORTS
