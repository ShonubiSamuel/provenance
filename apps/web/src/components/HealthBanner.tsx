import type { StatusResponse } from '../types'

function quotaLabel(status: StatusResponse): string | null {
  if (status.search_remaining === null || status.search_limit === null) return null
  const reset = status.search_reset_in
  const resetLabel =
    reset && reset > 0 ? ` · resets in ${reset >= 60 ? `${Math.ceil(reset / 60)}m` : `${reset}s`}` : ''
  return `code search ${status.search_remaining}/${status.search_limit} per minute${resetLabel}`
}

/** Whatever is wrong that the user cannot see: no token, a rejected token, GitHub's
 * code search refusing to return results, or a spent quota. Renders nothing when
 * everything is fine — a healthy system should be silent. */
export function HealthBanner({ status }: { status: StatusResponse | null }) {
  if (!status) return null

  const quota = quotaLabel(status)
  const severe = !status.credential_ok || status.github_degraded

  if (!status.message) {
    // Nothing wrong — but the quota is still worth a quiet line, since it is the one
    // number that explains why collection is slow.
    return quota ? (
      <p className="mt-2 text-xs text-slate-600">GitHub quota: {quota}</p>
    ) : null
  }

  return (
    <div
      className={`mt-3 rounded-md border px-3 py-2 text-sm ${
        severe
          ? 'border-red-800 bg-red-950/50 text-red-200'
          : 'border-amber-800/70 bg-amber-950/40 text-amber-200'
      }`}
    >
      <p>{status.message}</p>
      <p className="mt-1 text-xs opacity-70">
        {status.credential === 'none'
          ? 'No credentials loaded'
          : `Signed in with a ${status.credential === 'github-app' ? 'GitHub App token' : 'personal access token'}`}
        {status.token_expires_at ? ` · expires ${status.token_expires_at}` : ''}
        {quota ? ` · ${quota}` : ''}
      </p>
    </div>
  )
}
