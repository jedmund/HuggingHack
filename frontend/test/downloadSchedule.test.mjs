import assert from 'node:assert/strict'
import test from 'node:test'

import {
  scheduleIsOpen,
  schedulePreset,
  scheduleSummary,
  schedulesMatch,
  timezoneDisplayName,
  weekdayPreset,
  withBrowserTimezoneDefault,
} from '../src/downloadSchedule.ts'

const schedule = {
  enabled: false,
  timezone: 'UTC',
  weekdays: [],
  start_time: '22:00',
  end_time: '06:00',
  window_open: true,
  updated_by: null,
  updated_at: null,
}

test('uses the browser timezone only for an untouched schedule', () => {
  assert.equal(withBrowserTimezoneDefault(schedule, 'America/Los_Angeles').timezone, 'America/Los_Angeles')
  assert.equal(withBrowserTimezoneDefault({ ...schedule, updated_by: 'admin' }, 'America/Los_Angeles').timezone, 'UTC')
  assert.equal(withBrowserTimezoneDefault({ ...schedule, enabled: true }, 'America/Los_Angeles').timezone, 'UTC')
})

test('recognizes weekday presets and produces a readable overnight summary', () => {
  assert.deepEqual(schedulePreset('Weekdays'), [0, 1, 2, 3, 4])
  assert.equal(weekdayPreset([0, 1, 2, 3, 4]), 'Weekdays')
  assert.equal(weekdayPreset([0, 2, 4]), 'Custom')
  assert.equal(
    scheduleSummary({ ...schedule, enabled: true, weekdays: [0, 1, 2, 3, 4] }, 'en-US'),
    'Weekdays · 10:00 PM–6:00 AM the next day',
  )
})

test('evaluates overnight windows using the selected start day', () => {
  const overnight = { ...schedule, enabled: true, weekdays: [0], timezone: 'UTC' }
  assert.equal(scheduleIsOpen(overnight, new Date('2026-07-27T23:00:00Z')), true)
  assert.equal(scheduleIsOpen(overnight, new Date('2026-07-28T03:00:00Z')), true)
  assert.equal(scheduleIsOpen(overnight, new Date('2026-07-28T23:00:00Z')), false)
})

test('compares editable schedule fields and formats timezone names safely', () => {
  assert.equal(schedulesMatch(schedule, { ...schedule }), true)
  assert.equal(schedulesMatch(schedule, { ...schedule, timezone: 'Europe/London' }), false)
  assert.ok(timezoneDisplayName('America/Los_Angeles', 'en-US').length > 0)
  assert.equal(timezoneDisplayName('Not/A_Timezone', 'en-US'), 'Not/A_Timezone')
})
