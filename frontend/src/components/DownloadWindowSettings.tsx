import { ArrowRight, CalendarClock, Globe2, LoaderCircle } from 'lucide-react'
import { useEffect, useMemo, useState, type FormEvent } from 'react'
import { api } from '../api'
import {
  ALL_WEEKDAYS,
  WEEKDAY_LABELS,
  browserTimezone,
  scheduleIsOpen,
  schedulePreset,
  scheduleSummary,
  schedulesMatch,
  timezoneDisplayName,
  timezoneOptions,
  weekdayPreset,
  withBrowserTimezoneDefault,
} from '../downloadSchedule'
import type { DownloadSchedule, User } from '../types'

interface DownloadWindowSettingsProps {
  user: User
  onToast: (message: string, tone?: 'success' | 'error') => void
}

const presets = ['Every day', 'Weekdays', 'Weekends']

export function DownloadWindowSettings({ user, onToast }: DownloadWindowSettingsProps) {
  const [schedule, setSchedule] = useState<DownloadSchedule | null>(null)
  const [savedSchedule, setSavedSchedule] = useState<DownloadSchedule | null>(null)
  const [saving, setSaving] = useState(false)
  const [changingTimezone, setChangingTimezone] = useState(false)
  const canEdit = user.role === 'admin'

  useEffect(() => {
    let active = true
    api.downloadSettings()
      .then((response) => {
        if (!active) return
        const prepared = withBrowserTimezoneDefault(response)
        setSchedule(prepared)
        setSavedSchedule(prepared)
      })
      .catch(() => onToast('Unable to load the download window.', 'error'))
    return () => { active = false }
  }, [onToast])

  const timezones = useMemo(
    () => timezoneOptions(schedule?.timezone || browserTimezone()),
    [schedule?.timezone],
  )
  const dirty = Boolean(schedule && savedSchedule && !schedulesMatch(schedule, savedSchedule))
  const currentPreset = schedule ? weekdayPreset(schedule.weekdays) : ''
  const open = schedule ? scheduleIsOpen(schedule) : true

  function update(patch: Partial<DownloadSchedule>) {
    setSchedule((current) => current ? { ...current, ...patch } : current)
  }

  async function save(event: FormEvent) {
    event.preventDefault()
    if (!schedule || !dirty) return
    setSaving(true)
    try {
      const updated = await api.updateDownloadSettings({
        enabled: schedule.enabled,
        timezone: schedule.timezone,
        weekdays: schedule.weekdays,
        start_time: schedule.start_time,
        end_time: schedule.end_time,
      })
      setSchedule(updated)
      setSavedSchedule(updated)
      setChangingTimezone(false)
      onToast('Download window updated.')
    } catch (reason) {
      onToast(reason instanceof Error ? reason.message : 'Unable to update download window', 'error')
    } finally {
      setSaving(false)
    }
  }

  return (
    <section className="settings-section download-window-settings">
      <div className="settings-section-title download-window-title">
        <CalendarClock size={20} />
        <div>
          <h2>Download window</h2>
          <p>Choose when Hub transfers are allowed to run.</p>
        </div>
        {schedule && (
          <span className={`download-window-status ${schedule.enabled ? open ? 'open' : 'closed' : 'anytime'}`}>
            <span />{schedule.enabled ? open ? 'Open now' : 'Closed now' : 'Any time'}
          </span>
        )}
      </div>

      {!schedule && <div className="empty-compact">Loading download schedule…</div>}
      {schedule && (
        <form onSubmit={save}>
          <label className="download-window-enable">
            <input
              type="checkbox"
              checked={schedule.enabled}
              disabled={!canEdit}
              onChange={(event) => update({
                enabled: event.target.checked,
                weekdays: event.target.checked && schedule.weekdays.length === 0
                  ? ALL_WEEKDAYS
                  : schedule.weekdays,
              })}
            />
            <span className="download-window-switch" aria-hidden="true"><span /></span>
            <span><strong>Limit download hours</strong><small>Transfers pause when the window closes and resume when it opens.</small></span>
          </label>

          {schedule.enabled ? (
            <div className="download-window-editor">
              <div className="download-window-group">
                <span className="download-window-label">Quick schedule</span>
                <div className="download-window-presets">
                  {presets.map((preset) => (
                    <button
                      type="button"
                      className={currentPreset === preset ? 'selected' : ''}
                      disabled={!canEdit}
                      aria-pressed={currentPreset === preset}
                      onClick={() => update({ weekdays: schedulePreset(preset) })}
                      key={preset}
                    >
                      {preset}
                    </button>
                  ))}
                </div>
              </div>

              <div className="download-window-group">
                <span className="download-window-label">Days</span>
                <div className="weekday-picker" aria-label="Download weekdays">
                  {WEEKDAY_LABELS.map((label, day) => (
                    <button
                      type="button"
                      key={label}
                      disabled={!canEdit}
                      aria-pressed={schedule.weekdays.includes(day)}
                      className={schedule.weekdays.includes(day) ? 'selected' : ''}
                      onClick={() => update({
                        weekdays: schedule.weekdays.includes(day)
                          ? schedule.weekdays.filter((value) => value !== day)
                          : [...schedule.weekdays, day].sort(),
                      })}
                    >
                      {label}
                    </button>
                  ))}
                </div>
              </div>

              <div className="download-window-group">
                <span className="download-window-label">Download from</span>
                <div className="download-window-times">
                  <label><span>Starts</span><input type="time" value={schedule.start_time} disabled={!canEdit} onChange={(event) => update({ start_time: event.target.value })} /></label>
                  <ArrowRight size={16} />
                  <label><span>Ends</span><input type="time" value={schedule.end_time} disabled={!canEdit} onChange={(event) => update({ end_time: event.target.value })} /></label>
                </div>
              </div>

              <div className="download-window-group">
                <span className="download-window-label">Timezone</span>
                <div className="download-window-timezone">
                  <span className="download-window-timezone-icon"><Globe2 size={16} /></span>
                  <span>
                    <strong>{timezoneDisplayName(schedule.timezone)}</strong>
                    <small>{schedule.timezone}</small>
                  </span>
                  {canEdit && (
                    <button type="button" className="text-button" onClick={() => setChangingTimezone((current) => !current)}>
                      {changingTimezone ? 'Done' : 'Change'}
                    </button>
                  )}
                </div>
                {changingTimezone && canEdit && (
                  <div className="download-window-timezone-select">
                    <select value={schedule.timezone} onChange={(event) => update({ timezone: event.target.value })} autoFocus>
                      {timezones.map((timezone) => <option value={timezone} key={timezone}>{timezone}</option>)}
                    </select>
                    {schedule.timezone !== browserTimezone() && (
                      <button type="button" className="secondary-button compact" onClick={() => update({ timezone: browserTimezone() })}>Use browser timezone</button>
                    )}
                  </div>
                )}
              </div>

              <div className={`download-window-summary ${open ? 'open' : 'closed'}`}>
                <CalendarClock size={16} />
                <span>
                  <strong>{scheduleSummary(schedule)}</strong>
                  <small>{schedule.start_time > schedule.end_time
                    ? 'Selected days indicate when the overnight window starts.'
                    : 'Selected days indicate when downloads may run.'}</small>
                </span>
              </div>
            </div>
          ) : (
            <div className="download-window-disabled">
              <CalendarClock size={17} />
              <span><strong>No download restrictions</strong><small>Queued downloads can start and continue at any hour.</small></span>
            </div>
          )}

          {canEdit && (
            <div className="download-window-actions">
              <span>{dirty ? 'Unsaved changes' : 'Schedule is up to date'}</span>
              <button className="download-button compact" disabled={!dirty || saving}>
                {saving ? <LoaderCircle size={14} className="spin" /> : <CalendarClock size={14} />}
                Save schedule
              </button>
            </div>
          )}
        </form>
      )}
    </section>
  )
}
