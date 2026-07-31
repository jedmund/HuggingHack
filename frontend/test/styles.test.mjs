import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

const styles = await readFile(new URL('../src/styles.css', import.meta.url), 'utf8')

test('does not use 9px text', () => {
  assert.doesNotMatch(styles, /font-size:\s*9px\b/)
})
