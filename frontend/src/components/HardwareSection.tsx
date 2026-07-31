import {
  Cpu,
  LoaderCircle,
  MemoryStick,
  Pencil,
  Plus,
  Save,
  Star,
  Trash2,
  X,
} from 'lucide-react'
import { useCallback, useEffect, useState, type FormEvent } from 'react'
import { api } from '../api'
import type {
  HardwareComponentKind,
  HardwareRig,
  HardwareRigInput,
} from '../types'
import { formatBytes } from '../utils'

type ToastHandler = (message: string, tone?: 'success' | 'error') => void

interface DraftComponent {
  kind: HardwareComponentKind
  vendor: string
  model: string
  memory_gb: string
  quantity: number
}

interface DraftRig {
  name: string
  notes: string
  is_primary: boolean
  components: DraftComponent[]
}

const GIB = 1024 ** 3
const kindLabels: Record<HardwareComponentKind, string> = {
  cpu: 'CPU / system RAM',
  gpu: 'GPU / VRAM',
  apple_silicon: 'Apple silicon / unified memory',
}

function memoryValue(bytes: number): string {
  return String(Math.round((bytes / GIB) * 100) / 100)
}

function draftFor(rig?: HardwareRig, first = false): DraftRig {
  return rig
    ? {
        name: rig.name,
        notes: rig.notes,
        is_primary: rig.is_primary,
        components: rig.components.map((component) => ({
          kind: component.kind,
          vendor: component.vendor,
          model: component.model,
          memory_gb: memoryValue(component.memory_bytes),
          quantity: component.quantity,
        })),
      }
    : { name: '', notes: '', is_primary: first, components: [] }
}

function payloadFor(draft: DraftRig): HardwareRigInput {
  return {
    name: draft.name.trim(),
    notes: draft.notes.trim(),
    is_primary: draft.is_primary,
    components: draft.components.map((component) => ({
      kind: component.kind,
      vendor: component.vendor.trim(),
      model: component.model.trim(),
      memory_bytes: Math.round(Number(component.memory_gb) * GIB),
      quantity: component.quantity,
    })),
  }
}

export function HardwareSection({ onToast }: { onToast: ToastHandler }) {
  const [rigs, setRigs] = useState<HardwareRig[]>([])
  const [editing, setEditing] = useState<string | 'new' | null>(null)
  const [draft, setDraft] = useState<DraftRig>(() => draftFor(undefined, true))
  const [saving, setSaving] = useState(false)

  const load = useCallback(async () => {
    try {
      setRigs((await api.hardwareRigs()).items)
    } catch (reason) {
      onToast(reason instanceof Error ? reason.message : 'Unable to load hardware', 'error')
    }
  }, [onToast])

  useEffect(() => { load() }, [load])

  function startNew() {
    setDraft(draftFor(undefined, rigs.length === 0))
    setEditing('new')
  }

  function startEdit(rig: HardwareRig) {
    setDraft(draftFor(rig))
    setEditing(rig.id)
  }

  function addComponent() {
    setDraft((current) => ({
      ...current,
      components: [
        ...current.components,
        { kind: 'gpu', vendor: '', model: '', memory_gb: '', quantity: 1 },
      ],
    }))
  }

  function updateComponent(index: number, patch: Partial<DraftComponent>) {
    setDraft((current) => ({
      ...current,
      components: current.components.map((component, componentIndex) =>
        componentIndex === index ? { ...component, ...patch } : component,
      ),
    }))
  }

  async function save(event: FormEvent) {
    event.preventDefault()
    const payload = payloadFor(draft)
    if (payload.components.some((component) => !component.model || component.memory_bytes <= 0)) {
      onToast('Every component needs a model and a positive memory amount.', 'error')
      return
    }
    setSaving(true)
    try {
      if (editing === 'new') await api.createHardwareRig(payload)
      else if (editing) await api.updateHardwareRig(editing, payload)
      await load()
      setEditing(null)
      onToast(editing === 'new' ? 'Hardware rig added.' : 'Hardware rig updated.')
    } catch (reason) {
      onToast(reason instanceof Error ? reason.message : 'Unable to save hardware', 'error')
    } finally {
      setSaving(false)
    }
  }

  async function remove(rig: HardwareRig) {
    if (!window.confirm(`Delete hardware profile “${rig.name}”?`)) return
    try {
      await api.deleteHardwareRig(rig.id)
      await load()
      if (editing === rig.id) setEditing(null)
      onToast(`${rig.name} was deleted.`)
    } catch (reason) {
      onToast(reason instanceof Error ? reason.message : 'Unable to delete hardware', 'error')
    }
  }

  return (
    <section className="settings-section hardware-settings">
      <div className="settings-section-title hardware-section-title">
        <Cpu size={20} />
        <div>
          <h2>My hardware</h2>
          <p>Add named rigs to check GGUF and MLX weight sets with 20% runtime headroom.</p>
        </div>
        {!editing && <button type="button" className="secondary-button compact" onClick={startNew}><Plus size={14} /> Add rig</button>}
      </div>

      <div className="hardware-rig-list">
        {rigs.map((rig) => (
          <article className="hardware-rig-card" key={rig.id}>
            <div className="hardware-rig-heading">
              <span className="hardware-rig-icon"><MemoryStick size={18} /></span>
              <span>
                <strong>{rig.name}</strong>
                <small>{rig.notes || `${rig.components.length} component${rig.components.length === 1 ? '' : 's'}`}</small>
              </span>
              {rig.is_primary && <em><Star size={12} /> Primary</em>}
              <span className="hardware-rig-actions">
                <button type="button" onClick={() => startEdit(rig)} aria-label={`Edit ${rig.name}`}><Pencil size={14} /></button>
                <button type="button" className="danger-text" onClick={() => remove(rig)} aria-label={`Delete ${rig.name}`}><Trash2 size={14} /></button>
              </span>
            </div>
            <div className="hardware-components-summary">
              {rig.components.map((component) => (
                <span key={component.id}>
                  <strong>{component.quantity > 1 ? `${component.quantity}× ` : ''}{component.vendor} {component.model}</strong>
                  <small>{kindLabels[component.kind]} · {formatBytes(component.memory_bytes * component.quantity)}</small>
                </span>
              ))}
              {rig.components.length === 0 && <small>No memory components configured; fit will be unknown.</small>}
            </div>
          </article>
        ))}
        {rigs.length === 0 && !editing && (
          <div className="empty-compact">Add your Mac, GPU workstation, or CPU server to compare weight sizes.</div>
        )}
      </div>

      {editing && (
        <form className="hardware-editor" onSubmit={save}>
          <div className="hardware-editor-grid">
            <label>Rig name<input value={draft.name} onChange={(event) => setDraft({ ...draft, name: event.target.value })} maxLength={80} placeholder="Mac Studio" required /></label>
            <label>Notes<input value={draft.notes} onChange={(event) => setDraft({ ...draft, notes: event.target.value })} maxLength={500} placeholder="Desk, rack, or purpose" /></label>
          </div>
          <label className="safety-toggle compact">
            <input type="checkbox" checked={draft.is_primary} onChange={(event) => setDraft({ ...draft, is_primary: event.target.checked })} />
            <span><strong>Primary rig</strong><small>Shown first when comparing model weights.</small></span>
          </label>
          <div className="hardware-component-editor">
            <div className="selection-browser-heading">
              <span><strong>Components</strong><small>Enter usable system RAM, VRAM, or unified memory.</small></span>
              <button type="button" className="text-button" onClick={addComponent}><Plus size={13} /> Add component</button>
            </div>
            {draft.components.map((component, index) => (
              <div className="hardware-component-row" key={index}>
                <label>Type<select value={component.kind} onChange={(event) => updateComponent(index, { kind: event.target.value as HardwareComponentKind })}>{Object.entries(kindLabels).map(([value, label]) => <option value={value} key={value}>{label}</option>)}</select></label>
                <label>Vendor<input value={component.vendor} onChange={(event) => updateComponent(index, { vendor: event.target.value })} maxLength={80} placeholder="NVIDIA" /></label>
                <label>Model<input value={component.model} onChange={(event) => updateComponent(index, { model: event.target.value })} maxLength={120} placeholder="RTX 4090" required /></label>
                <label>Memory GiB<input type="number" min="0.01" max="1048576" step="0.01" value={component.memory_gb} onChange={(event) => updateComponent(index, { memory_gb: event.target.value })} placeholder="24" required /></label>
                <label>Qty<input type="number" min="1" max="16" value={component.quantity} onChange={(event) => updateComponent(index, { quantity: Number(event.target.value) })} required /></label>
                <button type="button" className="hardware-remove-component danger-text" onClick={() => setDraft((current) => ({ ...current, components: current.components.filter((_, componentIndex) => componentIndex !== index) }))} aria-label="Remove component"><X size={15} /></button>
              </div>
            ))}
          </div>
          <div className="hardware-editor-actions">
            <button className="download-button compact" disabled={saving}>{saving ? <LoaderCircle size={14} className="spin" /> : <Save size={14} />} Save rig</button>
            <button type="button" className="secondary-button compact" onClick={() => setEditing(null)}><X size={14} /> Cancel</button>
          </div>
        </form>
      )}
    </section>
  )
}
