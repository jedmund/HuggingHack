import type { DownloadSchedule } from './types'

export const ALL_WEEKDAYS = [0, 1, 2, 3, 4, 5, 6]
export const WEEKDAY_LABELS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']

const WEEKDAY_PRESETS = [
  { label: 'Every day', weekdays: ALL_WEEKDAYS },
  { label: 'Weekdays', weekdays: [0, 1, 2, 3, 4] },
  { label: 'Weekends', weekdays: [5, 6] },
]

function sameDays(left: number[], right: number[]): boolean {
  return left.length === right.length && left.every((day, index) => day === right[index])
}

export function browserTimezone(): string {
  return Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC'
}

export function withBrowserTimezoneDefault(
  schedule: DownloadSchedule,
  timezone = browserTimezone(),
): DownloadSchedule {
  if (schedule.enabled || schedule.updated_by) return schedule
  return { ...schedule, timezone }
}

export function timezoneDisplayName(timezone: string, locale?: string): string {
  try {
    const formatter = new Intl.DateTimeFormat(locale, {
      timeZone: timezone,
      timeZoneName: 'longGeneric',
    })
    return formatter.formatToParts(new Date()).find((part) => part.type === 'timeZoneName')?.value || timezone
  } catch {
    return timezone
  }
}

export function timezoneOptions(current: string): string[] {
  const supportedValuesOf = (Intl as typeof Intl & {
    supportedValuesOf?: (key: 'timeZone') => string[]
  }).supportedValuesOf
  const supported = supportedValuesOf ? supportedValuesOf('timeZone') : []
  return [...new Set([current, browserTimezone(), ...supported])].sort()
}

export function weekdayPreset(weekdays: number[]): string {
  const normalized = [...weekdays].sort()
  return WEEKDAY_PRESETS.find((preset) => sameDays(preset.weekdays, normalized))?.label || 'Custom'
}

export function schedulePreset(label: string): number[] {
  return [...(WEEKDAY_PRESETS.find((preset) => preset.label === label)?.weekdays || [])]
}

export function formatScheduleTime(value: string, locale?: string): string {
  const [hour, minute] = value.split(':').map(Number)
  const date = new Date(Date.UTC(2020, 0, 1, hour, minute))
  return new Intl.DateTimeFormat(locale, {
    hour: 'numeric',
    minute: '2-digit',
    timeZone: 'UTC',
  }).format(date)
}

export function scheduleSummary(schedule: DownloadSchedule, locale?: string): string {
  if (!schedule.enabled) return 'Downloads can run at any time.'
  const preset = weekdayPreset(schedule.weekdays)
  const days = preset === 'Custom'
    ? schedule.weekdays.map((day) => WEEKDAY_LABELS[day]).join(', ')
    : preset
  const overnight = schedule.start_time > schedule.end_time
  return `${days} · ${formatScheduleTime(schedule.start_time, locale)}–${formatScheduleTime(schedule.end_time, locale)}${overnight ? ' the next day' : ''}`
}

export function scheduleIsOpen(schedule: DownloadSchedule, now = new Date()): boolean {
  if (!schedule.enabled) return true
  try {
    const parts = new Intl.DateTimeFormat('en-US', {
      timeZone: schedule.timezone,
      weekday: 'short',
      hour: '2-digit',
      minute: '2-digit',
      hourCycle: 'h23',
    }).formatToParts(now)
    const value = (type: Intl.DateTimeFormatPartTypes) =>
      parts.find((part) => part.type === type)?.value || ''
    const weekday = WEEKDAY_LABELS.indexOf(value('weekday'))
    const currentTime = `${value('hour')}:${value('minute')}`
    const weekdays = new Set(schedule.weekdays)
    if (schedule.start_time < schedule.end_time) {
      return weekdays.has(weekday) && currentTime >= schedule.start_time && currentTime < schedule.end_time
    }
    if (currentTime >= schedule.start_time) return weekdays.has(weekday)
    return currentTime < schedule.end_time && weekdays.has((weekday + 6) % 7)
  } catch {
    return schedule.window_open
  }
}

export function schedulesMatch(left: DownloadSchedule, right: DownloadSchedule): boolean {
  return left.enabled === right.enabled &&
    left.timezone === right.timezone &&
    left.start_time === right.start_time &&
    left.end_time === right.end_time &&
    sameDays([...left.weekdays].sort(), [...right.weekdays].sort())
}
