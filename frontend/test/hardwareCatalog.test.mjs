import assert from 'node:assert/strict'
import test from 'node:test'

import {
  catalogMemoryLabel,
  catalogVendors,
  filterHardwareCatalog,
  matchingCatalogItem,
} from '../src/hardwareCatalog.ts'

const items = [
  { id: 'rtx-3060', kind: 'gpu', category: 'GPU', vendor: 'NVIDIA', model: 'RTX 3060', memory_gb: [8, 12], owners: 20 },
  { id: 'm4-max', kind: 'apple_silicon', category: 'Apple Silicon', vendor: 'Apple', model: 'Apple M4 Max', memory_gb: [36, 48, 64, 128], owners: 10 },
  { id: 'ryzen', kind: 'cpu', category: 'CPU', vendor: 'AMD', model: 'Ryzen 9 9950X', memory_gb: [], owners: 5 },
]

test('filters catalog models by vendor and multi-word search', () => {
  assert.deepEqual(filterHardwareCatalog(items, 'rtx 30').map((item) => item.id), ['rtx-3060'])
  assert.deepEqual(filterHardwareCatalog(items, '', 'Apple').map((item) => item.id), ['m4-max'])
  assert.deepEqual(catalogVendors(items), ['NVIDIA', 'AMD', 'Apple'])
})

test('returns the complete filtered catalog', () => {
  const catalog = Array.from({ length: 12 }, (_, index) => ({
    ...items[0],
    id: `rtx-${index}`,
    model: `RTX ${index}`,
  }))

  assert.equal(filterHardwareCatalog(catalog, '').length, 12)
})

test('matches existing components without relying on catalog IDs', () => {
  assert.equal(matchingCatalogItem(items, {
    kind: 'gpu',
    vendor: 'nvidia',
    model: 'RTX 3060',
  })?.id, 'rtx-3060')
  assert.equal(matchingCatalogItem(items, {
    kind: 'gpu',
    vendor: 'NVIDIA',
    model: 'Custom GPU',
  }), null)
})

test('describes fixed, configurable, and unknown memory', () => {
  assert.equal(catalogMemoryLabel([24]), '24 GiB')
  assert.equal(catalogMemoryLabel([8, 12]), '8 / 12 GiB')
  assert.equal(catalogMemoryLabel([]), 'Memory entered separately')
})
