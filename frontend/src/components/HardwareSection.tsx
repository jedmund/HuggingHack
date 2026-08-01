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
import { matchingCatalogItem } from '../hardwareCatalog'
import type {
  HardwareCatalogItem,
  HardwareComponentKind,
  HardwareRig,
  HardwareRigInput,
} from '../types'
import { formatBytes } from '../utils'
import { HardwareCatalogPicker } from './HardwareCatalogPicker'
import { SettingsPageHeader } from './SettingsPageHeader'

type ToastHandler = (message: string, tone?: 'success' | 'error') => void

interface DraftComponent {
  source: 'catalog' | 'custom'
  catalog_id: string
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

function draftFor(
  rig?: HardwareRig,
  first = false,
  catalog: HardwareCatalogItem[] = [],
): DraftRig {
  return rig
    ? {
        name: rig.name,
        notes: rig.notes,
        is_primary: rig.is_primary,
        components: rig.components.map((component) => {
          const match = matchingCatalogItem(catalog, component)
          const memoryGb = Number(memoryValue(component.memory_bytes))
          const catalogMemory = Boolean(
            match &&
            (match.memory_gb.length === 0 || match.memory_gb.includes(memoryGb)),
          )
          return {
            source: catalogMemory ? 'catalog' : 'custom',
            catalog_id: catalogMemory ? match?.id || '' : '',
            kind: component.kind,
            vendor: component.vendor,
            model: component.model,
            memory_gb: memoryValue(component.memory_bytes),
            quantity: component.quantity,
          }
        }),
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
  const [catalog, setCatalog] = useState<HardwareCatalogItem[]>([])
  const [catalogLoading, setCatalogLoading] = useState(true)
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

  useEffect(() => {
    let active = true
    api.hardwareCatalog()
      .then((response) => { if (active) setCatalog(response.items) })
      .catch(() => { if (active) setCatalog([]) })
      .finally(() => { if (active) setCatalogLoading(false) })
    return () => { active = false }
  }, [])

  function startNew() {
    setDraft(draftFor(undefined, rigs.length === 0))
    setEditing('new')
  }

  function startEdit(rig: HardwareRig) {
    setDraft(draftFor(rig, false, catalog))
    setEditing(rig.id)
  }

  function addComponent() {
    setDraft((current) => ({
      ...current,
      components: [
        ...current.components,
        { source: 'catalog', catalog_id: '', kind: 'gpu', vendor: '', model: '', memory_gb: '', quantity: 1 },
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

  function selectCatalogItem(index: number, item: HardwareCatalogItem) {
    updateComponent(index, {
      source: 'catalog',
      catalog_id: item.id,
      kind: item.kind,
      vendor: item.vendor,
      model: item.model,
      memory_gb: item.memory_gb.length === 1 ? String(item.memory_gb[0]) : '',
    })
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
    <article className="settings-content-page settings-content-page-wide hardware-settings">
      <SettingsPageHeader
        eyebrow="Personal"
        icon={Cpu}
        title="My hardware"
        description="Describe the machines you run models on to compare GGUF and MLX weights with runtime headroom."
        action={!editing && <button type="button" className="secondary-button compact" onClick={startNew}><Plus size={14} /> Add rig</button>}
      />

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
          <div className="hardware-editor-heading">
            <span className="hardware-editor-icon"><MemoryStick size={18} /></span>
            <span>
              <strong>{editing === 'new' ? 'Add a hardware rig' : `Edit ${draft.name || 'hardware rig'}`}</strong>
              <small>Describe the memory available to models on this machine.</small>
            </span>
            <button type="button" className="hardware-editor-close" onClick={() => setEditing(null)} aria-label="Close hardware editor"><X size={16} /></button>
          </div>

          <div className="hardware-editor-basics">
            <label className="hardware-field">
              <span>Rig name</span>
              <input value={draft.name} onChange={(event) => setDraft({ ...draft, name: event.target.value })} maxLength={80} placeholder="Mac Studio" required />
            </label>
            <label className="hardware-field">
              <span>Notes <small>optional</small></span>
              <input value={draft.notes} onChange={(event) => setDraft({ ...draft, notes: event.target.value })} maxLength={500} placeholder="Desk, rack, or purpose" />
            </label>
            <label className="hardware-primary-card">
              <input type="checkbox" checked={draft.is_primary} onChange={(event) => setDraft({ ...draft, is_primary: event.target.checked })} />
              <span className="hardware-primary-switch" aria-hidden="true"><span /></span>
              <span className="hardware-primary-copy"><strong>Primary rig</strong><small>Use first in weight comparisons</small></span>
            </label>
          </div>

          <section className="hardware-component-editor" aria-labelledby="hardware-components-heading">
            <div className="hardware-component-heading">
              <span>
                <strong id="hardware-components-heading">Memory components</strong>
                <small>Add usable system RAM, dedicated VRAM, or unified memory.</small>
              </span>
              {draft.components.length > 0 && (
                <button type="button" className="secondary-button compact" onClick={addComponent}><Plus size={14} /> Add component</button>
              )}
            </div>

            {draft.components.length === 0 && (
              <div className="hardware-component-empty">
                <span className="hardware-component-empty-icon"><MemoryStick size={20} /></span>
                <span><strong>No memory components yet</strong><small>Add the memory pool a model can use on this rig.</small></span>
                <button type="button" className="secondary-button compact" onClick={addComponent}><Plus size={14} /> Add first component</button>
              </div>
            )}

            {draft.components.map((component, index) => (
              <article className="hardware-component-card" key={index}>
                <div className="hardware-component-card-heading">
                  <span className="hardware-component-number">{index + 1}</span>
                  <span>
                    <strong>{component.model.trim() || kindLabels[component.kind]}</strong>
                    <small>{component.memory_gb ? `${component.memory_gb} GiB${component.quantity > 1 ? ` × ${component.quantity}` : ''}` : 'Memory details'}</small>
                  </span>
                  <button type="button" className="hardware-remove-component danger-text" onClick={() => setDraft((current) => ({ ...current, components: current.components.filter((_, componentIndex) => componentIndex !== index) }))} aria-label={`Remove component ${index + 1}`}><Trash2 size={14} /></button>
                </div>
                {component.source === 'catalog' ? (() => {
                  const selected = catalog.find((item) => item.id === component.catalog_id) || null
                  const memoryLabel = selected?.kind === 'gpu' ? 'VRAM' : selected?.kind === 'apple_silicon' ? 'Unified memory' : 'Usable system RAM'
                  return (
                    <div className="hardware-component-catalog-body">
                      <HardwareCatalogPicker
                        inputId={`hardware-component-${index}`}
                        items={catalog}
                        loading={catalogLoading}
                        selected={selected}
                        onSelect={(item) => selectCatalogItem(index, item)}
                        onCustom={() => updateComponent(index, { source: 'custom', catalog_id: '' })}
                      />
                      {selected && (
                        <div className="hardware-catalog-configuration">
                          <label className="hardware-field">
                            <span>{memoryLabel} <small>GiB</small></span>
                            {selected.memory_gb.length > 0 ? (
                              <select value={component.memory_gb} onChange={(event) => updateComponent(index, { memory_gb: event.target.value })} required>
                                {selected.memory_gb.length > 1 && <option value="">Select memory</option>}
                                {selected.memory_gb.map((memory) => <option value={memory} key={memory}>{memory} GiB</option>)}
                              </select>
                            ) : (
                              <input type="number" min="0.01" max="1048576" step="0.01" value={component.memory_gb} onChange={(event) => updateComponent(index, { memory_gb: event.target.value })} placeholder="64" required />
                            )}
                          </label>
                          <label className="hardware-field"><span>Quantity</span><input type="number" min="1" max="16" value={component.quantity} onChange={(event) => updateComponent(index, { quantity: Number(event.target.value) })} required /></label>
                        </div>
                      )}
                    </div>
                  )
                })() : (
                  <div className="hardware-custom-component">
                    <div className="hardware-custom-heading">
                      <span><strong>Custom hardware</strong><small>Use this only when the model is missing from the catalog.</small></span>
                      <button type="button" className="text-button" onClick={() => updateComponent(index, { source: 'catalog', catalog_id: '', vendor: '', model: '', memory_gb: '' })}>Choose from catalog</button>
                    </div>
                    <div className="hardware-component-fields">
                      <label className="hardware-field"><span>Type</span><select value={component.kind} onChange={(event) => updateComponent(index, { kind: event.target.value as HardwareComponentKind })}>{Object.entries(kindLabels).map(([value, label]) => <option value={value} key={value}>{label}</option>)}</select></label>
                      <label className="hardware-field"><span>Vendor</span><input value={component.vendor} onChange={(event) => updateComponent(index, { vendor: event.target.value })} maxLength={80} placeholder="NVIDIA" /></label>
                      <label className="hardware-field"><span>Model</span><input value={component.model} onChange={(event) => updateComponent(index, { model: event.target.value })} maxLength={120} placeholder="RTX 4090" required /></label>
                      <label className="hardware-field"><span>Memory <small>GiB</small></span><input type="number" min="0.01" max="1048576" step="0.01" value={component.memory_gb} onChange={(event) => updateComponent(index, { memory_gb: event.target.value })} placeholder="24" required /></label>
                      <label className="hardware-field"><span>Quantity</span><input type="number" min="1" max="16" value={component.quantity} onChange={(event) => updateComponent(index, { quantity: Number(event.target.value) })} required /></label>
                    </div>
                  </div>
                )}
              </article>
            ))}
          </section>

          <div className="hardware-editor-actions">
            <span>{draft.components.length} component{draft.components.length === 1 ? '' : 's'} configured</span>
            <div>
              <button type="button" className="secondary-button compact" onClick={() => setEditing(null)}>Cancel</button>
              <button className="download-button compact" disabled={saving}>{saving ? <LoaderCircle size={14} className="spin" /> : <Save size={14} />} Save rig</button>
            </div>
          </div>
        </form>
      )}
    </article>
  )
}
