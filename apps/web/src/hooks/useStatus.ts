import { useEffect, useState } from 'react'
import { fetchStatus } from '../api'
import type { StatusResponse } from '../types'

const POLL_MS = 10_000
const BUSY_POLL_MS = 4_000

/** Polls backend + GitHub health. Faster while a search is running, because that is
 * exactly when "why is nothing happening?" needs an answer. */
export function useStatus(busy: boolean) {
  const [status, setStatus] = useState<StatusResponse | null>(null)

  useEffect(() => {
    let cancelled = false
    let timer: number | undefined

    async function tick() {
      try {
        const s = await fetchStatus()
        if (!cancelled) setStatus(s)
      } catch {
        // The backend being unreachable is its own visible failure (search calls error
        // out); don't stack a second banner on top of it.
        if (!cancelled) setStatus(null)
      }
      if (!cancelled) timer = window.setTimeout(tick, busy ? BUSY_POLL_MS : POLL_MS)
    }
    void tick()
    return () => {
      cancelled = true
      if (timer !== undefined) window.clearTimeout(timer)
    }
  }, [busy])

  return status
}
