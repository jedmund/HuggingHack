#!/usr/bin/env node

import { mkdir, writeFile } from 'node:fs/promises'
import path from 'node:path'
import process from 'node:process'

const SOURCE_URL = 'https://huggingface.co/hardware?view=table'
const outputPath = path.resolve(
  process.cwd(),
  process.argv[2] || 'backend/app/data/hardware_catalog.json',
)

function decodeHtml(value) {
  return value.replace(
    /&(?:quot|amp|lt|gt|#39|#x27);|&#(?:\d+|x[\da-f]+);/gi,
    (entity) => {
      const named = {
        '&quot;': '"',
        '&amp;': '&',
        '&lt;': '<',
        '&gt;': '>',
        '&#39;': "'",
        '&#x27;': "'",
      }
      if (named[entity.toLowerCase()]) return named[entity.toLowerCase()]
      const hex = entity.toLowerCase().startsWith('&#x')
      const digits = entity.slice(hex ? 3 : 2, -1)
      return String.fromCodePoint(Number.parseInt(digits, hex ? 16 : 10))
    },
  )
}

function stripHtml(value) {
  return decodeHtml(value.replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim())
}

function slug(value) {
  return value
    .normalize('NFKD')
    .toLowerCase()
    .replace(/\+/g, '-plus-')
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-|-$/g, '')
}

const response = await fetch(SOURCE_URL, {
  headers: { 'User-Agent': 'HuggingHack hardware catalog updater' },
})
if (!response.ok) throw new Error(`Hugging Face returned HTTP ${response.status}`)
const html = await response.text()

const propsMatch = html.match(/data-target="HardwareContent"\s+data-props="([^"]+)"/)
if (!propsMatch) throw new Error('HardwareContent data was not found')
const props = JSON.parse(decodeHtml(propsMatch[1]))

const rows = [...html.matchAll(/<tr\b[^>]*>([\s\S]*?)<\/tr>/g)].slice(1)
if (rows.length !== props.skus.length) {
  throw new Error(`Expected ${props.skus.length} table rows, found ${rows.length}`)
}

const kinds = {
  GPU: 'gpu',
  CPU: 'cpu',
  'Apple Silicon': 'apple_silicon',
}
const vendorNames = {
  INTEL: 'Intel',
  QUALCOMM: 'Qualcomm',
}

const items = rows.map((rowMatch, index) => {
  const [category, sourceVendor, sourceModel] = props.skus[index].sku
  const cells = [...rowMatch[1].matchAll(/<td\b[^>]*>([\s\S]*?)<\/td>/g)]
  const modelMatch = cells[0]?.[1].match(/<span class="font-medium">([\s\S]*?)<\/span>/)
  const model = modelMatch ? stripHtml(modelMatch[1]) : ''
  const tableVendor = cells[1] ? stripHtml(cells[1][1]) : ''
  const vendor =
    category === 'Apple Silicon' ? 'Apple' : vendorNames[sourceVendor] || sourceVendor

  if (model !== sourceModel || tableVendor.toLowerCase() !== vendor.toLowerCase()) {
    throw new Error(
      `Catalog row ${index + 1} did not match its SKU: ${tableVendor} ${model}`,
    )
  }

  const memoryGb = [
    ...cells[0][1].matchAll(/<span class="text-\[10px\]">([\d.]+)GB<\/span>/g),
  ].map((match) => Number(match[1]))

  return {
    id: slug(`${category}-${vendor}-${model}`),
    kind: kinds[category],
    category,
    vendor,
    model,
    memory_gb: [...new Set(memoryGb)].sort((left, right) => left - right),
    owners: Math.max(0, Number(props.skus[index].uniqueUsers) || 0),
  }
})

const ids = new Set(items.map((item) => item.id))
if (ids.size !== items.length) {
  const duplicates = items
    .map((item) => item.id)
    .filter((id, index, allIds) => allIds.indexOf(id) !== index)
  throw new Error(`Generated catalog IDs are not unique: ${duplicates.join(', ')}`)
}

const catalog = {
  source: {
    name: 'Hugging Face Hardware',
    url: SOURCE_URL,
    retrieved_at: new Date().toISOString(),
  },
  items: items.sort(
    (left, right) =>
      right.owners - left.owners ||
      left.vendor.localeCompare(right.vendor) ||
      left.model.localeCompare(right.model),
  ),
}

await mkdir(path.dirname(outputPath), { recursive: true })
await writeFile(outputPath, `${JSON.stringify(catalog, null, 2)}\n`)
console.log(`Wrote ${items.length} hardware models to ${outputPath}`)
