import { Plus, Search } from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { api, type AdminServer, type PowerAction, type PowerState } from '../../api'
import { Banner, Card, Dot, Empty, Modal, PageHeader, Pill, relativeTime, useConfirm, useNow } from '../../components'
import { firmwareBelowTarget } from '../../firmware'
import { useAsync, usePolling } from '../../hooks'
import { useToast } from '../../toast'

const STATES = ['in_stock', 'provisioning', 'active', 'suspended', 'rescue', 'wiping', 'rma', 'retired']

export default function Servers() {
  const fleet = useAsync(() => api.fleet())
  const power = useAsync(() => api.fleetPower())
  const [adding, setAdding] = useState(false)
  const [filter, setFilter] = useState('')
  const [state, setState] = useState('')
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const navigate = useNavigate()
  const now = useNow()

  usePolling(fleet.reload, 15000, true)
  usePolling(power.reload, 30000, true)

  const rows = useMemo(() => {
    const q = filter.trim().toLowerCase()
    return (fleet.data ?? []).filter((s) => {
      if (state && s.state !== state) return false
      if (!q) return true
      const hay = `${s.serial} ${s.hostname ?? ''} ${s.cimc_ip} ${s.datacenter ?? ''} ${s.rack ?? ''} ${s.customer_email ?? ''} ${s.model}`.toLowerCase()
      return hay.includes(q)
    })
  }, [fleet.data, filter, state])

  // Drop selections for rows that no longer exist or are filtered away.
  useEffect(() => {
    setSelected((current) => new Set([...current].filter((id) => rows.some((r) => r.id === id))))
  }, [rows])

  const powerOf = (id: string): PowerState | undefined => power.data?.servers[id]
  const allSelected = rows.length > 0 && rows.every((r) => selected.has(r.id))

  return (
    <main className="page wide">
      <PageHeader
        title="Servers"
        sub="Every server, where it is, who has it, and whether it is on. Power is read from each BMC."
        actions={<button className="primary" onClick={() => setAdding(true)}><Plus />Add server</button>}
      />

      {fleet.error && <Banner kind="error">{fleet.error}</Banner>}

      <div className="toolbar">
        <div className="search" style={{ width: 320 }}>
          <Search />
          <input placeholder="Search serial, hostname, CIMC, rack, customer…" value={filter} onChange={(e) => setFilter(e.target.value)} />
        </div>
        <div className="chips">
          <button className={`chip ${state === '' ? 'active' : ''}`} onClick={() => setState('')}>All</button>
          {STATES.map((s) => (
            <button key={s} className={`chip ${state === s ? 'active' : ''}`} onClick={() => setState(state === s ? '' : s)}>
              {s.replace(/_/g, ' ')}
            </button>
          ))}
        </div>
        <span className="spacer" />
        <span className="faint small">
          {rows.length} of {fleet.data?.length ?? 0}
          {power.data && ` · power read ${relativeTime(power.data.checked_at, now)}`}
        </span>
      </div>

      {selected.size > 0 && <BulkBar ids={[...selected]} servers={rows} onDone={() => { setSelected(new Set()); void power.reload() }} />}

      <Card flush>
        {!fleet.data ? (
          <Empty>Loading…</Empty>
        ) : !fleet.data.length ? (
          <Empty>No servers registered yet. <strong>Add server</strong> registers the first one and reads its hardware over Redfish.</Empty>
        ) : !rows.length ? (
          <Empty>Nothing matches.</Empty>
        ) : (
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th style={{ width: 28 }}>
                    <input type="checkbox" checked={allSelected} onChange={(e) => setSelected(e.target.checked ? new Set(rows.map((r) => r.id)) : new Set())} aria-label="Select all" />
                  </th>
                  <th>Server</th>
                  <th>Power</th>
                  <th>State</th>
                  <th>Health</th>
                  <th>Location</th>
                  <th>CIMC</th>
                  <th>Firmware</th>
                  <th>Customer</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((server) => {
                  const p = powerOf(server.id)
                  return (
                    <tr key={server.id} className="clickable" onClick={() => navigate(`/admin/servers/${server.id}`)}>
                      <td onClick={(e) => e.stopPropagation()}>
                        <input
                          type="checkbox"
                          checked={selected.has(server.id)}
                          onChange={(e) => setSelected((cur) => { const next = new Set(cur); if (e.target.checked) next.add(server.id); else next.delete(server.id); return next })}
                          aria-label={`Select ${server.serial}`}
                        />
                      </td>
                      <td>
                        <Link to={`/admin/servers/${server.id}`} className="mono strong" onClick={(e) => e.stopPropagation()}>{server.serial}</Link>
                        <div className="cell-sub">{server.hostname ?? server.model}</div>
                      </td>
                      <td>
                        <span className="status-line" title={p?.error ?? (p ? `via ${p.via}` : 'reading…')}>
                          <Dot state={p?.state ?? 'unknown'} pulse={!p} />
                          <span style={{ textTransform: 'capitalize' }}>{p ? (p.state === 'unknown' ? 'Unreachable' : p.state) : '…'}</span>
                          {p?.stale && <span className="small" style={{ color: 'var(--warn)' }} title={p.error ?? ''}>· last seen {relativeTime(p.last_seen ?? p.checked_at, now)}</span>}
                        </span>
                      </td>
                      <td><Pill value={server.state} /></td>
                      <td>
                        <Pill value={server.health_status} />
                        <div className="cell-sub">{relativeTime(server.health_checked_at, now)}</div>
                      </td>
                      <td className="subtle">
                        {[server.datacenter, server.rack, server.rack_unit && `U${server.rack_unit}`].filter(Boolean).join(' · ') || '—'}
                        {server.switch_port && <div className="cell-sub">{server.switch_name ?? ''} {server.switch_port}</div>}
                      </td>
                      <td className="mono subtle">{server.cimc_ip}</td>
                      <td className="subtle">
                        {server.cimc_firmware ?? '—'}
                        {firmwareBelowTarget(server.cimc_firmware) && <span className="pill warning" style={{ marginLeft: 6 }}>below baseline</span>}
                      </td>
                      <td className="subtle">{server.customer_email ?? '—'}</td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      {adding && (
        <AddServerModal
          onClose={() => setAdding(false)}
          onCreated={async () => { setAdding(false); await fleet.reload() }}
        />
      )}
    </main>
  )
}

/** Power actions on every selected server, one job each. */
function BulkBar({ ids, servers, onDone }: { ids: string[]; servers: AdminServer[]; onDone: () => void }) {
  const toast = useToast()
  const confirm = useConfirm()
  const [busy, setBusy] = useState(false)

  async function run(action: PowerAction, title: string) {
    const ok = await confirm({
      title,
      body: `${ids.length} server${ids.length > 1 ? 's' : ''}: ${ids.map((id) => servers.find((s) => s.id === id)?.serial).filter(Boolean).join(', ')}`,
      confirmLabel: title,
      danger: action !== 'on' && action !== 'off',
    })
    if (!ok) return
    setBusy(true)
    let queued = 0
    const failed: string[] = []
    for (const id of ids) {
      try {
        await api.power(id, action)
        queued++
      } catch (e) {
        failed.push(`${servers.find((s) => s.id === id)?.serial}: ${e instanceof Error ? e.message : e}`)
      }
    }
    setBusy(false)
    if (queued) toast.ok(`${title}: ${queued} job${queued > 1 ? 's' : ''} queued`)
    for (const f of failed) toast.error(f)
    onDone()
  }

  return (
    <div className="banner info" style={{ alignItems: 'center' }}>
      <div className="banner-body row">
        <strong>{ids.length} selected</strong>
        <button className="sm" disabled={busy} onClick={() => run('on', 'Power on')}>Power on</button>
        <button className="sm" disabled={busy} onClick={() => run('off', 'Shut down')}>Shut down</button>
        <button className="sm danger" disabled={busy} onClick={() => run('reset', 'Reset')}>Reset</button>
        <button className="sm danger" disabled={busy} onClick={() => run('force_off', 'Force off')}>Force off</button>
        <button className="sm ghost" onClick={onDone}>Clear</button>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------

function AddServerModal({ onClose, onCreated }: { onClose: () => void; onCreated: () => void }) {
  const backend = useAsync(() => api.credentialBackend())
  const [serial, setSerial] = useState('')
  const [cimcIp, setCimcIp] = useState('')
  const [username, setUsername] = useState('admin')
  const [password, setPassword] = useState('')
  const [ref, setRef] = useState('')
  const [refStatus, setRefStatus] = useState<'unknown' | 'ok' | 'missing'>('unknown')
  const [datacenter, setDatacenter] = useState('')
  const [rack, setRack] = useState('')
  const [rackUnit, setRackUnit] = useState('')
  const [switchName, setSwitchName] = useState('')
  const [switchPort, setSwitchPort] = useState('')
  const [mac, setMac] = useState('')
  const [notes, setNotes] = useState('')
  const [prepare, setPrepare] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  // Default the ref to a per-server key so a password rotation on one box
  // does not mean re-registering the rest.
  useEffect(() => {
    if (serial && !ref) setRef(`cimc/${serial.trim()}`)
  }, [serial, ref])

  const writable = backend.data?.writable ?? false

  async function checkRef() {
    if (!ref) return
    try {
      const result = await api.checkCredential(ref)
      setRefStatus(result.resolves ? 'ok' : 'missing')
    } catch {
      setRefStatus('missing')
    }
  }

  async function submit(event: React.FormEvent) {
    event.preventDefault()
    setBusy(true)
    setError(null)
    try {
      if (writable && password) await api.storeCredential({ ref, username, password })
      await api.createServer({
        serial: serial.trim(),
        cimc_ip: cimcIp.trim(),
        cimc_credential_ref: ref.trim(),
        datacenter: datacenter || null,
        rack: rack || null,
        rack_unit: rackUnit ? Number(rackUnit) : null,
        switch_name: switchName || null,
        switch_port: switchPort || null,
        provisioning_mac: mac || null,
        notes: notes || null,
        prepare_bmc: prepare,
      })
      onCreated()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal
      title="Add a server"
      onClose={onClose}
      wide
      footer={
        <>
          <button type="button" onClick={onClose}>Cancel</button>
          <button type="submit" form="add-server" className="primary" disabled={busy}>{busy ? 'Adding…' : 'Add server'}</button>
        </>
      }
    >
      <form id="add-server" onSubmit={submit}>
        {error && <Banner kind="error">{error}</Banner>}
        <div className="grid cols-2">
          <div className="field">
            <label htmlFor="serial">Serial (from the pull-out tab)</label>
            <input id="serial" className="mono" value={serial} onChange={(e) => setSerial(e.target.value)} placeholder="FCH2045V1AB" required autoFocus />
          </div>
          <div className="field">
            <label htmlFor="cimc">CIMC IP address</label>
            <input id="cimc" className="mono" value={cimcIp} onChange={(e) => setCimcIp(e.target.value)} placeholder="10.0.0.101" required />
          </div>
        </div>

        <div className="card" style={{ marginBottom: 14 }}>
          <div className="card-header"><div className="card-title">CIMC credential</div></div>
          <div className="card-body">
            {backend.data && !writable && (
              <Banner kind="info">
                The <code>{backend.data.backend}</code> secrets backend is read-only. Set <code>DOZ_CIMC_DEFAULT_USER</code> / <code>DOZ_CIMC_DEFAULT_PASS</code> (or a per-ref variable) before adding the server, then check the ref resolves.
              </Banner>
            )}
            <div className="field">
              <label htmlFor="ref">Credential ref</label>
              <div className="row">
                <input id="ref" className="mono" value={ref} onChange={(e) => { setRef(e.target.value); setRefStatus('unknown') }} style={{ flex: 1 }} required />
                <button type="button" onClick={checkRef}>Check</button>
                {refStatus === 'ok' && <span className="pill ok">resolves</span>}
                {refStatus === 'missing' && <span className="pill critical">not found</span>}
              </div>
            </div>
            {writable && (
              <div className="grid cols-2">
                <div className="field"><label htmlFor="user">CIMC username</label><input id="user" value={username} onChange={(e) => setUsername(e.target.value)} /></div>
                <div className="field"><label htmlFor="pass">CIMC password</label><input id="pass" type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="new-password" /></div>
              </div>
            )}
          </div>
        </div>

        <div className="grid cols-3">
          <div className="field"><label htmlFor="dc">Datacenter</label><input id="dc" value={datacenter} onChange={(e) => setDatacenter(e.target.value)} placeholder="dc1" /></div>
          <div className="field"><label htmlFor="rack">Rack</label><input id="rack" value={rack} onChange={(e) => setRack(e.target.value)} placeholder="R1" /></div>
          <div className="field"><label htmlFor="ru">Rack unit</label><input id="ru" type="number" min={1} max={60} value={rackUnit} onChange={(e) => setRackUnit(e.target.value)} /></div>
          <div className="field"><label htmlFor="sw">Switch</label><input id="sw" value={switchName} onChange={(e) => setSwitchName(e.target.value)} placeholder="sw-core-1" /></div>
          <div className="field"><label htmlFor="port">Switch port</label><input id="port" value={switchPort} onChange={(e) => setSwitchPort(e.target.value)} placeholder="Gi1/0/12" /></div>
          <div className="field"><label htmlFor="mac">PXE MAC (optional)</label><input id="mac" className="mono" value={mac} onChange={(e) => setMac(e.target.value)} placeholder="aa:bb:cc:dd:ee:ff" /></div>
        </div>
        <div className="field">
          <label htmlFor="notes">Notes</label>
          <textarea id="notes" value={notes} onChange={(e) => setNotes(e.target.value)} style={{ minHeight: 56 }} />
        </div>
        <label className="check"><input type="checkbox" checked={prepare} onChange={(e) => setPrepare(e.target.checked)} /> Prepare the BMC on save: switch on IPMI over LAN, SOL, KVM, virtual media, Redfish and PXE in the CIMC, set the boot order, then prove IPMI works. One-time; the settings stay in the CIMC.</label>
        <p className="faint small">Then an inventory sync reads the hardware over Redfish and records the PXE MAC. If the CIMC is unreachable, the job fails and says why.</p>
      </form>
    </Modal>
  )
}
