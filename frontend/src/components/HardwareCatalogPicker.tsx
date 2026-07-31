import { Check, Cpu, MemoryStick, Search, Wrench } from 'lucide-react'
import { useMemo, useState } from 'react'
import {
  catalogMemoryLabel,
  catalogVendors,
  filterHardwareCatalog,
} from '../hardwareCatalog'
import type { HardwareCatalogItem } from '../types'

interface HardwareCatalogPickerProps {
  inputId: string
  items: HardwareCatalogItem[]
  loading: boolean
  selected: HardwareCatalogItem | null
  onSelect: (item: HardwareCatalogItem) => void
  onCustom: () => void
}

export function HardwareCatalogPicker({
  inputId,
  items,
  loading,
  selected,
  onSelect,
  onCustom,
}: HardwareCatalogPickerProps) {
  const [browsing, setBrowsing] = useState(!selected)
  const [query, setQuery] = useState('')
  const [vendor, setVendor] = useState('')
  const vendors = useMemo(() => catalogVendors(items), [items])
  const results = useMemo(
    () => filterHardwareCatalog(items, query, vendor),
    [items, query, vendor],
  )
  if (selected && !browsing) {
    return (
      <div className="hardware-catalog-selected">
        <span className="hardware-catalog-selected-icon">
          {selected.kind === 'cpu' ? <Cpu size={17} /> : <MemoryStick size={17} />}
        </span>
        <span>
          <strong>{selected.vendor} {selected.model}</strong>
          <small>{selected.category} · {catalogMemoryLabel(selected.memory_gb)}</small>
        </span>
        <button
          type="button"
          className="text-button"
          onClick={() => {
            setQuery(selected.model)
            setVendor(selected.vendor)
            setBrowsing(true)
          }}
        >
          Change
        </button>
      </div>
    )
  }

  return (
    <div className="hardware-catalog-picker">
      <div className="hardware-catalog-picker-heading">
        <span><strong>Choose a hardware model</strong><small>Search the local hardware catalog.</small></span>
        <span>{items.length > 0
          ? query || vendor ? `${results.length} matches` : `${items.length} models`
          : ''}</span>
      </div>

      <div className="hardware-vendor-filters" aria-label="Filter hardware vendors">
        <button type="button" className={!vendor ? 'active' : ''} onClick={() => setVendor('')}>All</button>
        {vendors.map((itemVendor) => (
          <button
            type="button"
            className={vendor === itemVendor ? 'active' : ''}
            onClick={() => setVendor(itemVendor)}
            key={itemVendor}
          >
            {itemVendor}
          </button>
        ))}
      </div>

      <label className="hardware-catalog-search">
        <Search size={15} />
        <input
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder="Search RTX 4090, M4 Max, Ryzen…"
          role="combobox"
          aria-controls={`${inputId}-results`}
          aria-expanded="true"
          autoFocus
        />
      </label>

      <div className="hardware-catalog-results" id={`${inputId}-results`} role="listbox">
        {loading && <div className="hardware-catalog-message">Loading hardware catalog…</div>}
        {!loading && results.map((item) => (
          <button
            type="button"
            role="option"
            aria-selected={selected?.id === item.id}
            onClick={() => {
              onSelect(item)
              setBrowsing(false)
            }}
            key={item.id}
          >
            <span className="hardware-catalog-result-icon">
              {item.kind === 'cpu' ? <Cpu size={15} /> : <MemoryStick size={15} />}
            </span>
            <span>
              <strong>{item.model}</strong>
              <small>{item.vendor} · {item.category}</small>
            </span>
            <span className="hardware-catalog-memory">{catalogMemoryLabel(item.memory_gb)}</span>
            {selected?.id === item.id && <Check size={15} />}
          </button>
        ))}
        {!loading && results.length === 0 && (
          <div className="hardware-catalog-message">No catalog models match that search.</div>
        )}
      </div>

      <button type="button" className="hardware-catalog-custom" onClick={onCustom}>
        <Wrench size={14} />
        <span><strong>Use custom hardware</strong><small>Enter a model that is not in the catalog.</small></span>
      </button>
    </div>
  )
}
