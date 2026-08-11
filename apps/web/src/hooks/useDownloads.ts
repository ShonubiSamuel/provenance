import { useCallback, useEffect, useState } from 'react'
import { fetchDownloads } from '../api'
import type { DownloadsResponse } from '../types'

// Fast enough to animate a transfer's progress; only used while one is running.
const ACTIVE_POLL_MS = 1500
// Nothing is moving, so this only has to notice a download started from elsewhere.
// Anything the user does here calls refresh() directly, so idle latency is invisible.
const IDLE_POLL_MS = 30_000

/** Polls the download manager so the header badge and the panel stay live.
 *
 * The cadence follows the work: 1.5s while a transfer is in flight, 30s when the list is
 * idle, and nothing at all while the tab is hidden. A flat 1.5s poll was ~2,400 requests
 * an hour for a list that changes only when a download is running — enough to bury every
 * other line in the server log, on a tool that is normally left open all day.
 */
export function useDownloads() {
  const [data, setData] = useState<DownloadsResponse | null>(null)

  const refresh = useCallback(async () => {
    try {
      setData(await fetchDownloads())
    } catch {
      /* backend momentarily unreachable — keep last snapshot */
    }
  }, [])

  const items = data?.items ?? []
  const activeCount = items.filter(
    (d) => d.status === 'active' || d.status === 'waiting' || d.status === 'paused',
  ).length
  const busy = activeCount > 0

  useEffect(() => {
    let cancelled = false
    let timer: number | undefined

    const stop = () => {
      if (timer !== undefined) window.clearTimeout(timer)
      timer = undefined
    }

    const tick = async () => {
      await refresh()
      // Re-checked every tick rather than captured: a tab hidden mid-transfer stops
      // polling, and shows fresh numbers the moment it comes back.
      if (cancelled || document.hidden) return
      timer = window.setTimeout(tick, busy ? ACTIVE_POLL_MS : IDLE_POLL_MS)
    }

    const onVisibility = () => {
      if (document.hidden) {
        stop()
      } else if (timer === undefined) {
        void tick()
      }
    }

    if (!document.hidden) void tick()
    document.addEventListener('visibilitychange', onVisibility)
    return () => {
      cancelled = true
      stop()
      document.removeEventListener('visibilitychange', onVisibility)
    }
  }, [refresh, busy])

  return {
    available: data?.available ?? false,
    downloadDir: data?.download_dir ?? '',
    freeBytes: data?.free_bytes ?? null,
    items,
    activeCount,
    refresh,
  }
}
