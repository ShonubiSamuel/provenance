import { useCallback, useEffect, useState } from 'react'
import { fetchDownloads } from '../api'
import type { DownloadsResponse } from '../types'

const POLL_MS = 1500

/** Polls the download manager so the header badge and the panel stay live. Cheap enough
 * to run for the whole session on a localhost tool. */
export function useDownloads() {
  const [data, setData] = useState<DownloadsResponse | null>(null)

  const refresh = useCallback(async () => {
    try {
      setData(await fetchDownloads())
    } catch {
      /* backend momentarily unreachable — keep last snapshot */
    }
  }, [])

  useEffect(() => {
    let cancelled = false
    let timer: number | undefined
    const tick = async () => {
      await refresh()
      if (!cancelled) timer = window.setTimeout(tick, POLL_MS)
    }
    void tick()
    return () => {
      cancelled = true
      if (timer !== undefined) window.clearTimeout(timer)
    }
  }, [refresh])

  const items = data?.items ?? []
  const activeCount = items.filter(
    (d) => d.status === 'active' || d.status === 'waiting' || d.status === 'paused',
  ).length

  return {
    available: data?.available ?? false,
    downloadDir: data?.download_dir ?? '',
    freeBytes: data?.free_bytes ?? null,
    items,
    activeCount,
    refresh,
  }
}
