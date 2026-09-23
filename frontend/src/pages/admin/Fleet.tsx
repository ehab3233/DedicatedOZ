import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../../api'
import { Banner, Empty, Modal, Pill, Stat, relativeTime } from '../../components'
import { useAsync, usePolling } from '../../hooks'

const TARGET_FIRMWARE = '4.1(2f)'

export default function Fleet() {
  const summary = useAsync(() => api.fleetSummary())
  const fleet = useAsync(() => api.fleet())
  const [adding, setAdding] = useState(false)
  const [filter, setFilter] = useState('')

  usePolling(async () => {
    await summary.reload()
    await fleet.reload()
  }, 15000, true)

  const byState = summary.data?.servers_by_state ?? {}
  const rows = (fleet.data ?? []).filter((s) => {
    if (!filter) return true
    const hay = `${s.serial} ${s.hostname ?? ''} ${s.cimc_ip} ${s.rack ?? ''} ${s.customer_email ?? ''} ${s.state}`.toLowerCase()
    return hay.includes(filter.toLowerCase())
  })

  return (
    <main className="page">
      <div className="spread">
        <div>
          <h1>Fleet</h1>
          <p className="subtle">Every server, where it is, and who has it.</p>
        </div>
        <button className="primary" onClick={() => setAdding(true)}>Add server</button>
      </div>

      {summary.error && <Banner kind="error">{summary.error}</Banner>}
      {fleet.error && <Banner kind="error">{fleet.error}</Banner>}

      <div className="grid cols-4">
        <Stat label="Servers" value={summary.data?.total_servers ?? '—'} />
        <Stat label="Active" value={byState.active ?? 0} />
        <Stat label="In stock" value={byState.in_stock ?? 0} />
        <Stat label="Provisioning" value={(byState.provisioning ?? 0) + (byState.wiping ?? 0)} />
        <Stat label="Running jobs" value={summary.data?.active_jobs ?? 0} />
        <Stat label="Failed jobs (24h)" value={summary.data?.failed_jobs_24h ?? 0} />
        <Stat label="Unhealthy" value={summary.data?.unhealthy_servers ?? 0} />
        <Stat label="Open abuse" value={summary.data?.open_abuse_reports ?? 0} />
      </div>

      <SystemCard />

      <div className="row" style={{ margin: '20px 0 10px' }}>
        <input
          placeholder="Filter by serial, hostname, CIMC IP, rack, customer, state…"
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          style={{ maxWidth: 480 }}
        />
        <span className="subtle">{rows.length} of {fleet.data?.length ?? 0}</span>
      </div>

      <div className="card table-scroll" style={{ padding: 0 }}>
        {!fleet.data?.length ? (
          <Empty>
            No servers registered yet. Click <strong>Add server</strong> to rack the first one.
          </Empty>
        ) : (
          <table>
            <thead>
              <tr>
                <th>Serial</th>
                <th>Location</th>
                <th>CIMC</th>
                <th>Firmware</th>
                <th>PXE MAC</th>
                <th>State</th>
                <th>Power</th>
                <th>Health</th>
                <th>Customer</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((server) => (
                <tr key={server.id}>
                  <td>
                    <Link to={`/admin/servers/${server.id}`} className="mono">
                      <strong>{server.serial}</strong>
                    </Link>
                    {server.hostname && <div className="subtle">{server.hostname}</div>}
                  </td>
                  <td className="subtle">
                    {[server.datacenter, server.rack, server.rack_unit && `U${server.rack_unit}`]
                      .filter(Boolean)
                      .join(' · ') || '—'}
                    {server.switch_port && (
                      <div style={{ fontSize: 12 }}>
                        {server.switch_name ?? ''} {server.switch_port}
                      </div>
                    )}
                  </td>
                  <td className="mono subtle">{server.cimc_ip}</td>
                  <td className="subtle">
                    {server.cimc_firmware ?? '—'}
                    {server.cimc_firmware && server.cimc_firmware !== TARGET_FIRMWARE && (
                      <span className="pill warning" style={{ marginLeft: 6 }}>off baseline</span>
                    )}
                  </td>
                  <td className="mono subtle">
                    {server.provisioning_mac ?? <span className="pill warning">not synced</span>}
                  </td>
                  <td><Pill value={server.state} /></td>
                  <td className="subtle">{server.last_power_state ?? '—'}</td>
                  <td>
                    <Pill value={server.health_status} />
                    <div className="subtle" style={{ fontSize: 12 }}>
                      {relativeTime(server.health_checked_at)}
                    </div>
                  </td>
                  <td className="subtle">{server.customer_email ?? '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {adding && (
        <AddServerModal
          onClose={() => setAdding(false)}
          onCreated={async () => {
            setAdding(false)
            await fleet.reload()
            await summary.reload()
          }}
        />
      )}
    </main>
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
      if (writable && password) {
        await api.storeCredential({ ref, username, password })
      }
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
      })
      onCreated()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal title="Add a server to inventory" onClose={onClose}>
      <form onSubmit={submit}>
        {error && <Banner kind="error">{error}</Banner>}

        <div className="grid cols-2">
          <div className="field">
            <label htmlFor="serial">Serial (from the pull-out tab)</label>
            <input id="serial" value={serial} onChange={(e) => setSerial(e.target.value)} placeholder="FCH2045V1AB" required />
          </div>
          <div className="field">
            <label htmlFor="cimc">CIMC IP address</label>
            <input id="cimc" value={cimcIp} onChange={(e) => setCimcIp(e.target.value)} placeholder="10.0.0.101" required />
          </div>
        </div>

        <fieldset style={{ border: '1px solid var(--border)', borderRadius: 6, padding: 12, marginBottom: 14 }}>
          <legend className="subtle" style={{ padding: '0 6px', fontSize: 13 }}>CIMC credential</legend>
          {backend.data && !writable && (
            <Banner kind="info">
              The <code>{backend.data.backend}</code> secrets backend is read-only. Set{' '}
              <code>DOZ_CIMC_DEFAULT_USER</code> / <code>DOZ_CIMC_DEFAULT_PASS</code> in the
              environment (or a per-ref variable) before adding the server, then check the ref
              below resolves.
            </Banner>
          )}
          <div className="field">
            <label htmlFor="ref">Credential ref</label>
            <div className="row">
              <input id="ref" value={ref} onChange={(e) => { setRef(e.target.value); setRefStatus('unknown') }} style={{ flex: 1 }} required />
              <button type="button" onClick={checkRef}>Check</button>
              {refStatus === 'ok' && <span className="pill ok">resolves</span>}
              {refStatus === 'missing' && <span className="pill critical">not found</span>}
            </div>
          </div>
          {writable && (
            <div className="grid cols-2">
              <div className="field">
                <label htmlFor="user">CIMC username</label>
                <input id="user" value={username} onChange={(e) => setUsername(e.target.value)} />
              </div>
              <div className="field">
                <label htmlFor="pass">CIMC password</label>
                <input id="pass" type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="new-password" />
              </div>
            </div>
          )}
        </fieldset>

        <div className="grid cols-2">
          <div className="field">
            <label htmlFor="dc">Datacenter</label>
            <input id="dc" value={datacenter} onChange={(e) => setDatacenter(e.target.value)} placeholder="dc1" />
          </div>
          <div className="field">
            <label htmlFor="rack">Rack</label>
            <input id="rack" value={rack} onChange={(e) => setRack(e.target.value)} placeholder="R1" />
          </div>
          <div className="field">
            <label htmlFor="ru">Rack unit</label>
            <input id="ru" type="number" min={1} max={60} value={rackUnit} onChange={(e) => setRackUnit(e.target.value)} />
          </div>
          <div className="field">
            <label htmlFor="mac">PXE MAC (optional — inventory sync fills this)</label>
            <input id="mac" value={mac} onChange={(e) => setMac(e.target.value)} placeholder="aa:bb:cc:dd:ee:ff" className="mono" />
          </div>
          <div className="field">
            <label htmlFor="sw">Switch</label>
            <input id="sw" value={switchName} onChange={(e) => setSwitchName(e.target.value)} placeholder="sw-core-1" />
          </div>
          <div className="field">
            <label htmlFor="port">Switch port</label>
            <input id="port" value={switchPort} onChange={(e) => setSwitchPort(e.target.value)} placeholder="Gi1/0/12" />
          </div>
        </div>

        <div className="field">
          <label htmlFor="notes">Notes</label>
          <textarea id="notes" value={notes} onChange={(e) => setNotes(e.target.value)} style={{ minHeight: 60 }} />
        </div>

        <div className="subtle" style={{ fontSize: 13, marginBottom: 12 }}>
          On save, an inventory sync job reads the hardware over Redfish and records the PXE MAC.
          If the CIMC is unreachable the job fails and says why.
        </div>

        <div className="row" style={{ justifyContent: 'flex-end' }}>
          <button type="button" onClick={onClose}>Cancel</button>
          <button type="submit" className="primary" disabled={busy}>
            {busy ? 'Adding…' : 'Add server'}
          </button>
        </div>
      </form>
    </Modal>
  )
}

// ---------------------------------------------------------------------------

/**
 * Is everything the panel depends on running? The question this answers is
 * "I clicked restart and nothing happened": usually a queue with no worker.
 */
function SystemCard() {
  const system = useAsync(() => api.system())
  usePolling(system.reload, 30000, true)
  const d = system.data
  if (!d) return null

  const problems: string[] = []
  if (d.database !== 'ok') problems.push(`database: ${d.database}`)
  if (d.redis !== 'ok') problems.push(`redis: ${d.redis}`)
  for (const [name, q] of Object.entries(d.queues)) {
    if (!q.workers.length) problems.push(`no worker on the "${name}" queue — ${q.handles} will sit queued`)
  }
  if (!d.ipmitool) problems.push('ipmitool is not installed: no IPMI power control and no serial console')

  return (
    <div className="card" style={{ marginTop: 16 }}>
      <div className="spread">
        <strong style={{ fontSize: 14 }}>System</strong>
        <span className={`pill ${problems.length ? 'critical' : 'ok'}`}>
          {problems.length ? `${problems.length} problem${problems.length > 1 ? 's' : ''}` : 'all running'}
        </span>
      </div>
      {problems.length > 0 && (
        <ul style={{ margin: '10px 0 0', paddingLeft: 18, color: 'var(--crit)', fontSize: 14 }}>
          {problems.map((p) => <li key={p}>{p}</li>)}
        </ul>
      )}
      <div className="row subtle" style={{ fontSize: 13, marginTop: 10, gap: 18 }}>
        {Object.entries(d.queues).map(([name, q]) => (
          <span key={name}>
            {name}: {q.workers.length ? `${q.workers.length} worker${q.workers.length > 1 ? 's' : ''}` : 'none'}
          </span>
        ))}
        <span>BMC control: {d.bmc_protocol}</span>
        <span>{d.ipmitool ? d.ipmitool.version : 'no ipmitool'}</span>
      </div>
    </div>
  )
}
