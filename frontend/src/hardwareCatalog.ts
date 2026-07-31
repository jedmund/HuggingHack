import type { HardwareCatalogItem, HardwareComponentKind } from './types'

export interface HardwareIdentity {
  kind: HardwareComponentKind
  vendor: string
  model: string
}

function normalized(value: string): string {
  return value.trim().toLocaleLowerCase()
}

export function matchingCatalogItem(
  items: HardwareCatalogItem[],
  component: HardwareIdentity,
): HardwareCatalogItem | null {
  const vendor = normalized(component.vendor)
  const model = normalized(component.model)
  return items.find(
    (item) =>
      item.kind === component.kind &&
      normalized(item.vendor) === vendor &&
      normalized(item.model) === model,
  ) || null
}

export function catalogVendors(items: HardwareCatalogItem[]): string[] {
  const preferred = ['NVIDIA', 'AMD', 'Intel', 'Qualcomm', 'Apple']
  const available = new Set(items.map((item) => item.vendor))
  return [
    ...preferred.filter((vendor) => available.has(vendor)),
    ...[...available].filter((vendor) => !preferred.includes(vendor)).sort(),
  ]
}

export function filterHardwareCatalog(
  items: HardwareCatalogItem[],
  query: string,
  vendor = '',
): HardwareCatalogItem[] {
  const terms = normalized(query).split(/\s+/).filter(Boolean)
  const vendorKey = normalized(vendor)
  return items
    .filter((item) => !vendorKey || normalized(item.vendor) === vendorKey)
    .filter((item) => {
      const haystack = normalized(`${item.vendor} ${item.model} ${item.category}`)
      return terms.every((term) => haystack.includes(term))
    })
}

export function catalogMemoryLabel(memory: number[]): string {
  if (memory.length === 0) return 'Memory entered separately'
  if (memory.length === 1) return `${memory[0]} GiB`
  return `${memory.join(' / ')} GiB`
}
