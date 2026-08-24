import { useState } from 'react'
import type {
  FacetGroup,
  FacetSelection,
  FacetValue,
  FacetsResponse,
} from '../types'

const GROUP_LABELS: Record<FacetGroup, string> = {
  languages: 'Language',
  extensions: 'Extension',
  path_prefixes: 'Common paths',
  owners: 'Owner',
  licenses: 'License',
}

const GROUP_ORDER: FacetGroup[] = [
  'languages',
  'extensions',
  'path_prefixes',
  'owners',
  'licenses',
]

const MAX_VISIBLE = 12

export function FacetSidebar({
  facets,
  selection,
  onToggle,
  onClear,
}: {
  facets: FacetsResponse
  selection: FacetSelection
  onToggle: (group: FacetGroup, value: string) => void
  onClear: () => void
}) {
  const anySelected = Object.values(selection).some((s) => s && s.size > 0)
  const [expanded, setExpanded] = useState<Set<FacetGroup>>(new Set())
  const toggleExpanded = (group: FacetGroup) =>
    setExpanded((prev) => {
      const next = new Set(prev)
      if (next.has(group)) next.delete(group)
      else next.add(group)
      return next
    })
  return (
    <aside className="w-64 shrink-0 space-y-5">
      {anySelected && (
        <button
          onClick={onClear}
          className="w-full rounded-md border border-slate-700 px-2 py-1 text-xs text-slate-300 hover:bg-slate-800"
        >
          Clear all filters
        </button>
      )}
      {GROUP_ORDER.map((group) => {
        const values: FacetValue[] = facets.facets[group] ?? []
        if (values.length === 0) return null
        const selected = selection[group]
        const isExpanded = expanded.has(group)
        const visible = isExpanded ? values : values.slice(0, MAX_VISIBLE)
        return (
          <section key={group}>
            <h3 className="mb-1.5 text-xs font-semibold uppercase tracking-wide text-slate-500">
              {GROUP_LABELS[group]}
            </h3>
            <ul className={`space-y-0.5 ${isExpanded ? 'max-h-64 overflow-y-auto pr-1' : ''}`}>
              {visible.map(({ value, count }) => {
                const active = selected?.has(value) ?? false
                return (
                  <li key={value}>
                    <button
                      onClick={() => onToggle(group, value)}
                      className={`flex w-full items-center justify-between gap-2 rounded px-2 py-1 text-left text-sm ${
                        active
                          ? 'bg-sky-900/50 text-sky-200'
                          : 'text-slate-300 hover:bg-slate-800'
                      }`}
                    >
                      <span className="truncate" title={value}>
                        {value}
                      </span>
                      <span className="shrink-0 text-xs text-slate-500">{count}</span>
                    </button>
                  </li>
                )
              })}
            </ul>
            {values.length > MAX_VISIBLE && (
              <button
                onClick={() => toggleExpanded(group)}
                className="mt-0.5 w-full rounded px-2 py-1 text-left text-xs text-sky-400 hover:bg-slate-800 hover:text-sky-300"
              >
                {isExpanded ? 'Show less' : `+${values.length - MAX_VISIBLE} more`}
              </button>
            )}
          </section>
        )
      })}
    </aside>
  )
}
