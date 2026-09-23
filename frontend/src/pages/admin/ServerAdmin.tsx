import { useCallback, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import {
  api,
  type AdminCustomer,
  type AdminServer,
  type IPBlock,
  type Job,
  type OSTemplate,
  type ServerState,
  type Subscription,
} from '../../api'
import { Banner, Empty, Modal, Pill, Progress, formatTime, relativeTime } from '../../components'
import { useAsync, usePolling } from '../../hooks'
import { PowerControls } from '../../power'

const ACTIVE_JOB_STATES = ['queued', 'running']

//: Legal targets per current state — mirrors SERVER_TRANSITIONS in the backend
//: so the dropdown only offers moves that will not 409.
const TRANSITIONS: Record<string, ServerState[]> = {
  in_stock: ['provisioning', 'rma', 'retired'],
  provisioning: ['active', 'in_stock', 'rma'],
  active: ['suspended', 'provisioning', 'rescue', 'wiping', 'rma'],
  suspended: ['active', 'wiping', 'rma'],
  rescue: ['active', 'provisioning', 'wiping'],
  wiping: ['in_stock', 'rma'],
  rma: ['in_stock', 'retired'],
  retired: [],
}

export default function ServerAdmin() {
  const { id = '' } = useParams()
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [modal, setModal] = useState<null | 'assign' | 'reinstall' | 'wipe' | 'ip' | 'edit'>(null)

  const server = useAsync(() => api.adminServer(id), [id])
  const jobs = useAsync(() => api.adminJobs({ server_id: id }), [id])
  const subs = useAsync(() => api.subscriptions({ server_id: id }), [id])

  const activeJob = (jobs.data ?? []).find((j) => ACTIVE_JOB_STATES.includes(j.state))
  const subscription = (subs.data ?? [])[0] ?? null

  const refresh = useCallback(async () => {
    await Promise.all([server.reload(), jobs.reload(), subs.reload()])
  }, [server.reload, jobs.reload, subs.reload]) // eslint-disable-line react-hooks/exhaustive-deps

  usePolling(refresh, 5000, Boolean(activeJob))

  async function act(fn: () => Promise<unknown>, message: string) {
    setBusy(true)
    setError(null)
    setNotice(null)
    try {
      await fn()
      setNotice(message)
      await refresh()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  if (server.error) return <main className="page"><Banner kind="error">{server.error}</Banner></main>
  if (!server.data) return <main className="page"><Empty>Loading…</Empty></main>

  const s = server.data
  const locked = busy || Boolean(activeJob)
  const legalStates = TRANSITIONS[s.state] ?? []

  return (
    <main className="page">
      <div className="spread">
        <div>
          <div className="subtle" style={{ fontSize: 13 }}><Link to="/admin">Fleet</Link> / {s.serial}</div>
          <h1>{s.serial}</h1>
          <p className="subtle">
            {s.hostname ? `${s.hostname} · ` : ''}{s.model}
            {s.rack ? ` · ${s.datacenter ?? ''} ${s.rack} U${s.rack_unit ?? '?'}` : ''}
          </p>
        </div>
        <div className="row">
          <Pill value={s.state} />
          <Pill value={s.health_status} />
          <button onClick={() => setModal('edit')}>Edit</button>
        </div>
      </div>

      {error && <Banner kind="error">{error}</Banner>}
      {notice && <Banner kind="info">{notice}</Banner>}

      {activeJob && (
        <div className="card">
          <div className="spread" style={{ marginBottom: 10 }}>
            <div>
              <strong>{activeJob.type.replace(/_/g, ' ')} in progress</strong>
              <div className="subtle">{activeJob.stage ?? 'starting…'}</div>
            </div>
            <Link to={`/jobs/${activeJob.id}`}>Raw log</Link>
          </div>
          <Progress value={activeJob.progress} />
        </div>
      )}

      {/* ---- Out-of-band ------------------------------------------------- */}
      <div className="grid cols-2">
        <div className="card">
          <strong style={{ fontSize: 14 }}>Out-of-band</strong>
          <table style={{ marginTop: 8 }}>
            <tbody>
              <tr><td className="subtle">CIMC</td><td className="mono">{s.cimc_ip}</td></tr>
              <tr>
                <td className="subtle">Control</td>
                <td>
                  {(s.bmc_protocol ?? 'default').toUpperCase()}
                  <span className="subtle" style={{ fontSize: 12 }}>
                    {' '}· IPMI :{s.ipmi_port ?? 623} · HTTPS :{s.redfish_port ?? 443}
                  </span>
                </td>
              </tr>
              <tr><td className="subtle">Credential ref</td><td className="mono">{s.cimc_credential_ref}</td></tr>
              <tr>
                <td className="subtle">Firmware</td>
                <td>
                  {s.cimc_firmware ?? <span className="subtle">unknown — run inventory sync</span>}
                  {s.cimc_firmware && s.cimc_firmware !== '4.1(2f)' && (
                    <span className="pill warning" style={{ marginLeft: 6 }}>not 4.1(2f)</span>
                  )}
                </td>
              </tr>
              <tr><td className="subtle">BIOS</td><td className="mono subtle">{s.bios_version ?? '—'}</td></tr>
              <tr>
                <td className="subtle">PXE MAC</td>
                <td className="mono">
                  {s.provisioning_mac ?? <span className="pill warning">not set — installs will refuse</span>}
                </td>
              </tr>
            </tbody>
          </table>
          <div className="row" style={{ marginTop: 12 }}>
            <button
              disabled={busy}
              title="Switch on IPMI over LAN and Serial-over-LAN, point BIOS console redirection at it, then check both work"
              onClick={() => act(() => api.prepareBmc(id), 'Prepare BMC queued: enabling IPMI over LAN and SOL.')}
            >
              Prepare BMC
            </button>
            <button disabled={busy} onClick={() => act(() => api.syncInventory(id), 'Inventory sync queued.')}>
              Sync inventory
            </button>
            <button disabled={busy} onClick={() => act(() => api.healthCheck(id), 'Health check queued.')}>
              Check health
            </button>
          </div>
        </div>

        <div className="card">
          <strong style={{ fontSize: 14 }}>Customer</strong>
          {subscription ? (
            <>
              <table style={{ marginTop: 8 }}>
                <tbody>
                  <tr><td className="subtle">Account</td><td>{subscription.customer_email}</td></tr>
                  <tr><td className="subtle">Plan</td><td>{subscription.plan_name}</td></tr>
                  <tr><td className="subtle">Price</td><td>{subscription.monthly_price != null ? `${subscription.currency} ${subscription.monthly_price}/mo` : '—'}</td></tr>
                  <tr><td className="subtle">Since</td><td className="subtle">{formatTime(subscription.started_at)}</td></tr>
                </tbody>
              </table>
              <div className="row" style={{ marginTop: 12 }}>
                <button className="danger" disabled={busy} onClick={() => {
                  if (confirm(`End this subscription for ${subscription.customer_email}? The server keeps running until you wipe it.`))
                    void act(() => api.endSubscription(subscription.id), 'Subscription ended. Wipe the server before reassigning it.')
                }}>
                  End subscription
                </button>
              </div>
            </>
          ) : (
            <>
              <div className="subtle" style={{ margin: '8px 0 12px' }}>
                Unassigned. {s.state === 'in_stock' || s.state === 'active' ? 'Ready to hand to a customer.' : `Cannot be assigned while ${s.state.replace(/_/g, ' ')}.`}
              </div>
              <button className="primary" disabled={busy || !(s.state === 'in_stock' || s.state === 'active')} onClick={() => setModal('assign')}>
                Assign to customer
              </button>
            </>
          )}
        </div>
      </div>

      {/* ---- Power and console ------------------------------------------ */}
      <h2>Power</h2>
      <div className="card">
        <PowerControls serverId={id} isAdmin activeJob={activeJob ?? null} onChanged={refresh} pollMs={30000} />
      </div>

      <h2>Console</h2>
      <div className="card">
        <div className="spread" style={{ flexWrap: 'wrap' }}>
          <div>
            <strong>Serial console</strong>
            <div className="subtle">
              In the browser, through the platform. BIOS, boot loader and OS, from power-on.
            </div>
          </div>
          <Link className="button primary" to={`/servers/${id}/console`}>Open serial console</Link>
        </div>
        <hr className="divider" />
        <div className="spread" style={{ flexWrap: 'wrap', alignItems: 'flex-start' }}>
          <div style={{ flex: '1 1 360px' }}>
            <strong>KVM (graphical)</strong>
            <div className="subtle">
              The CIMC's own vKVM, opened with one-time tokens. Your browser connects to the CIMC
              directly, so it has to be able to reach <span className="mono">{s.cimc_ip}</span>.
            </div>
          </div>
          <KvmLauncher serverId={id} cimcIp={s.cimc_ip} port={s.redfish_port} />
        </div>
      </div>

      <h2>Actions</h2>
      <div className="card">

        <div className="spread" style={{ marginBottom: 12 }}>
          <div>
            <strong>Provisioning</strong>
            <div className="subtle">Reinstall and wipe destroy data. Rescue does not.</div>
          </div>
          <div className="row">
            <button disabled={locked || !s.provisioning_mac} onClick={() => setModal('reinstall')}>Reinstall OS</button>
            <button disabled={locked || !s.provisioning_mac} onClick={() => act(() => api.rescue(id, []), 'Rescue boot queued.')}>Rescue mode</button>
            <button className="danger" disabled={locked || !s.provisioning_mac} onClick={() => setModal('wipe')}>Secure wipe</button>
          </div>
        </div>

        <div className="spread">
          <div>
            <strong>Lifecycle</strong>
            <div className="subtle">
              Currently <strong>{s.state.replace(/_/g, ' ')}</strong>.
              {s.state === 'wiping' && !s.last_wiped_at && ' Needs a completed wipe before it can return to stock.'}
              {s.last_wiped_at && ` Last wiped ${relativeTime(s.last_wiped_at)}.`}
            </div>
          </div>
          <div className="row">
            {s.state === 'suspended' ? (
              <button disabled={busy} onClick={() => act(() => api.unsuspend(id), 'Unsuspended. Re-enable the switch port by hand.')}>
                Unsuspend
              </button>
            ) : (
              <button className="danger" disabled={busy || s.state !== 'active'} onClick={() => {
                const reason = prompt('Reason for suspension (goes in the audit log):', 'abuse')
                if (reason) void act(() => api.suspend(id, reason), 'Suspended. Shut the switch port by hand — that is not automated yet.')
              }}>
                Suspend
              </button>
            )}
            <select
              disabled={busy || legalStates.length === 0}
              value=""
              onChange={(e) => {
                const target = e.target.value as ServerState
                if (!target) return
                if (confirm(`Move ${s.serial} from ${s.state} to ${target}?`))
                  void act(() => api.changeServerState(id, target, 'manual from admin panel'), `State set to ${target}.`)
              }}
              style={{ width: 'auto' }}
            >
              <option value="">Set state…</option>
              {legalStates.map((st) => <option key={st} value={st}>{st.replace(/_/g, ' ')}</option>)}
            </select>
          </div>
        </div>
      </div>

      {/* ---- Networking --------------------------------------------------- */}
      <div className="spread">
        <h2>Addresses</h2>
        <button disabled={busy} onClick={() => setModal('ip')}>Assign address</button>
      </div>
      <div className="card table-scroll" style={{ padding: 0 }}>
        {!s.ip_addresses.length ? (
          <Empty>No addresses. The installer configures the <strong>primary</strong> address statically — assign one before reinstalling.</Empty>
        ) : (
          <table>
            <thead><tr><th>Address</th><th>Gateway</th><th>rDNS</th><th /></tr></thead>
            <tbody>
              {s.ip_addresses.map((ip) => (
                <tr key={ip.id}>
                  <td className="mono">
                    {ip.address}/{ip.prefix_len}
                    {ip.is_primary && <span className="pill" style={{ marginLeft: 8 }}>primary</span>}
                  </td>
                  <td className="mono subtle">{ip.gateway ?? '—'}</td>
                  <td className="mono subtle">{ip.rdns ?? '—'}</td>
                  <td>
                    <button className="danger" disabled={busy} onClick={() => {
                      if (confirm(`Release ${ip.address}?`)) void act(() => api.releaseIp(ip.id), `${ip.address} released.`)
                    }}>Release</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {/* ---- Hardware ----------------------------------------------------- */}
      <h2>Hardware</h2>
      <div className="grid cols-2">
        <div className="card">
          <table>
            <tbody>
              <tr><td className="subtle">CPU</td><td>{s.cpu_count ? `${s.cpu_count}× ${s.cpu_model}` : '—'}</td></tr>
              <tr><td className="subtle">Threads</td><td>{s.cpu_cores_total ?? '—'}</td></tr>
              <tr><td className="subtle">Memory</td><td>{s.ram_gb ? `${s.ram_gb} GB` : '—'}</td></tr>
              <tr><td className="subtle">Switch</td><td className="mono subtle">{s.switch_name ?? ''} {s.switch_port ?? '—'}</td></tr>
              <tr><td className="subtle">Customer VLAN</td><td>{s.customer_vlan ?? '—'}</td></tr>
            </tbody>
          </table>
        </div>
        <div className="card">
          <strong style={{ fontSize: 14 }}>Health</strong>
          <div className="subtle" style={{ marginBottom: 8 }}>Checked {relativeTime(s.health_checked_at)}</div>
          {Object.keys(s.health?.subsystems ?? {}).length === 0 ? (
            <div className="subtle">Nothing collected yet.</div>
          ) : (
            <table>
              <tbody>
                {Object.entries(s.health?.subsystems ?? {}).map(([name, info]) => (
                  <tr key={name}>
                    <td className="subtle">{name.replace(/_/g, ' ')}</td>
                    <td><Pill value={(info as { status?: string })?.status} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </div>

      {s.nics.length > 0 && (
        <div className="card table-scroll">
          <table>
            <thead><tr><th>NIC</th><th>MAC</th><th>Speed</th><th>Link</th><th /></tr></thead>
            <tbody>
              {s.nics.map((n, i) => {
                const nic = n as Record<string, string | number | null>
                const isPxe = nic.mac === s.provisioning_mac
                return (
                  <tr key={i}>
                    <td className="mono">{String(nic.name ?? '—')}</td>
                    <td className="mono subtle">{String(nic.mac ?? '—')}</td>
                    <td className="subtle">{nic.speed_mbps ? `${nic.speed_mbps} Mbps` : '—'}</td>
                    <td className="subtle">{String(nic.link_status ?? '—')}</td>
                    <td>
                      {isPxe ? (
                        <span className="pill ok">PXE</span>
                      ) : (
                        <button disabled={busy} onClick={() => act(() => api.updateServer(id, { provisioning_mac: nic.mac }), `PXE MAC set to ${nic.mac}.`)}>
                          Use for PXE
                        </button>
                      )}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}

      {s.drives.length > 0 && (
        <div className="card table-scroll">
          <table>
            <thead><tr><th>Drive</th><th>Model</th><th>Serial</th><th>Capacity</th><th>Health</th></tr></thead>
            <tbody>
              {s.drives.map((d, i) => {
                const drive = d as Record<string, string | number | boolean | null>
                return (
                  <tr key={i}>
                    <td className="mono">{String(drive.name ?? '—')}</td>
                    <td className="subtle">{String(drive.model ?? '—')}</td>
                    <td className="mono subtle">{String(drive.serial ?? '—')}</td>
                    <td>{drive.capacity_gb ? `${drive.capacity_gb} GB` : '—'}</td>
                    <td><Pill value={drive.failure_predicted ? 'warning' : String(drive.health ?? '')} /></td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}

      {s.notes && (
        <div className="card">
          <strong style={{ fontSize: 14 }}>Notes</strong>
          <div style={{ whiteSpace: 'pre-wrap', marginTop: 6 }}>{s.notes}</div>
        </div>
      )}

      {/* ---- Jobs --------------------------------------------------------- */}
      <h2>Jobs</h2>
      <div className="card table-scroll" style={{ padding: 0 }}>
        {!jobs.data?.length ? (
          <Empty>Nothing has run on this server yet.</Empty>
        ) : (
          <table>
            <thead><tr><th>Job</th><th>State</th><th>Stage / error</th><th>Started</th><th /></tr></thead>
            <tbody>
              {jobs.data.slice(0, 20).map((job: Job) => (
                <tr key={job.id}>
                  <td>{job.type.replace(/_/g, ' ')}</td>
                  <td><Pill value={job.state} /></td>
                  <td className="subtle">
                    {job.stage ?? '—'}
                    {job.error && <div style={{ color: 'var(--crit)', fontSize: 12 }}>{job.error.slice(0, 140)}</div>}
                  </td>
                  <td className="subtle">{formatTime(job.started_at ?? job.created_at)}</td>
                  <td><Link to={`/jobs/${job.id}`}>Raw log</Link></td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {modal === 'assign' && (
        <AssignModal serverId={id} onClose={() => setModal(null)} onDone={async (m) => { setModal(null); setNotice(m); await refresh() }} />
      )}
      {modal === 'reinstall' && (
        <AdminReinstallModal serverId={id} customerEmail={subscription?.customer_email ?? null} onClose={() => setModal(null)} onDone={async (m) => { setModal(null); setNotice(m); await refresh() }} />
      )}
      {modal === 'wipe' && (
        <WipeModal serverId={id} serial={s.serial} onClose={() => setModal(null)} onDone={async (m) => { setModal(null); setNotice(m); await refresh() }} />
      )}
      {modal === 'ip' && (
        <AssignIpModal serverId={id} hasPrimary={s.ip_addresses.some((a) => a.is_primary)} onClose={() => setModal(null)} onDone={async (m) => { setModal(null); setNotice(m); await refresh() }} />
      )}
      {modal === 'edit' && (
        <EditModal server={s} onClose={() => setModal(null)} onDone={async (m) => { setModal(null); setNotice(m); await refresh() }} />
      )}
    </main>
  )
}

// ---------------------------------------------------------------------------

type Done = (message: string) => void

function AssignModal({ serverId, onClose, onDone }: { serverId: string; onClose: () => void; onDone: Done }) {
  const customers = useAsync<AdminCustomer[]>(() => api.customers())
  const [customerId, setCustomerId] = useState('')
  const [plan, setPlan] = useState('C220-M4')
  const [price, setPrice] = useState('')
  const [currency, setCurrency] = useState('USD')
  const [quota, setQuota] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  async function submit(event: React.FormEvent) {
    event.preventDefault()
    setBusy(true)
    setError(null)
    try {
      const sub: Subscription = await api.createSubscription({
        customer_id: customerId,
        server_id: serverId,
        plan_name: plan,
        monthly_price: price ? Number(price) : null,
        currency,
        bandwidth_quota_tb: quota ? Number(quota) : null,
      })
      onDone(`Assigned to ${sub.customer_email}. They can see it in their portal now.`)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal title="Assign to a customer" onClose={onClose}>
      <form onSubmit={submit}>
        {error && <Banner kind="error">{error}</Banner>}
        <div className="field">
          <label htmlFor="cust">Customer</label>
          <select id="cust" value={customerId} onChange={(e) => setCustomerId(e.target.value)} required>
            <option value="">Choose…</option>
            {(customers.data ?? []).filter((c) => !c.is_admin).map((c) => (
              <option key={c.id} value={c.id}>{c.email}{c.company_name ? ` — ${c.company_name}` : ''}</option>
            ))}
          </select>
          {customers.data && customers.data.filter((c) => !c.is_admin).length === 0 && (
            <div className="subtle" style={{ fontSize: 12, marginTop: 4 }}>
              No customer accounts yet — <Link to="/admin/customers">create one</Link> first.
            </div>
          )}
        </div>
        <div className="grid cols-2">
          <div className="field">
            <label htmlFor="plan">Plan name</label>
            <input id="plan" value={plan} onChange={(e) => setPlan(e.target.value)} required />
          </div>
          <div className="field">
            <label htmlFor="price">Monthly price</label>
            <div className="row">
              <input id="price" type="number" step="0.01" value={price} onChange={(e) => setPrice(e.target.value)} style={{ flex: 1 }} />
              <input value={currency} onChange={(e) => setCurrency(e.target.value.toUpperCase())} maxLength={3} style={{ width: 70 }} />
            </div>
          </div>
          <div className="field">
            <label htmlFor="quota">Bandwidth quota (TB/mo)</label>
            <input id="quota" type="number" value={quota} onChange={(e) => setQuota(e.target.value)} />
          </div>
        </div>
        <div className="subtle" style={{ fontSize: 13, marginBottom: 12 }}>
          Billing stays in WHMCS / HostBill; record the plan here so the portal can show it.
        </div>
        <div className="row" style={{ justifyContent: 'flex-end' }}>
          <button type="button" onClick={onClose}>Cancel</button>
          <button type="submit" className="primary" disabled={busy || !customerId}>{busy ? 'Assigning…' : 'Assign'}</button>
        </div>
      </form>
    </Modal>
  )
}

function AdminReinstallModal({ serverId, customerEmail, onClose, onDone }: { serverId: string; customerEmail: string | null; onClose: () => void; onDone: Done }) {
  const templates = useAsync<OSTemplate[]>(() => api.osTemplates())
  const [templateId, setTemplateId] = useState('')
  const [hostname, setHostname] = useState('')
  const [raid, setRaid] = useState('raid1')
  const [rootPassword, setRootPassword] = useState('')
  const [confirmText, setConfirmText] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  async function submit(event: React.FormEvent) {
    event.preventDefault()
    setBusy(true)
    setError(null)
    try {
      await api.reinstall(serverId, {
        os_template_id: templateId,
        hostname: hostname || undefined,
        raid_level: raid,
        ssh_key_ids: [],
        confirm_data_loss: true,
        ...(rootPassword ? { root_password: rootPassword } : {}),
      })
      onDone('Reinstall queued. Watch the progress card; ~20 minutes.')
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal title="Reinstall operating system" onClose={onClose}>
      <form onSubmit={submit}>
        <Banner kind="error">Erases every disk on the server.</Banner>
        {error && <Banner kind="error">{error}</Banner>}
        <Banner kind="info">
          {customerEmail
            ? <>SSH keys from <strong>{customerEmail}</strong>'s account will be installed.</>
            : <>Unassigned server: no customer keys. Set a root password below or the install will be unreachable.</>}
        </Banner>
        <div className="field">
          <label htmlFor="os">Operating system</label>
          <select id="os" value={templateId} onChange={(e) => setTemplateId(e.target.value)} required>
            <option value="">Choose…</option>
            {(templates.data ?? []).map((t) => <option key={t.id} value={t.id}>{t.name} {t.version}</option>)}
          </select>
        </div>
        <div className="grid cols-2">
          <div className="field">
            <label htmlFor="host">Hostname</label>
            <input id="host" value={hostname} onChange={(e) => setHostname(e.target.value)} />
          </div>
          <div className="field">
            <label htmlFor="raid">RAID</label>
            <select id="raid" value={raid} onChange={(e) => setRaid(e.target.value)}>
              <option value="raid1">RAID 1</option>
              <option value="raid0">RAID 0</option>
              <option value="raid5">RAID 5</option>
              <option value="raid10">RAID 10</option>
              <option value="none">None (JBOD)</option>
            </select>
          </div>
        </div>
        <div className="field">
          <label htmlFor="rootpw">Root password (optional, 12+ chars; stored hashed)</label>
          <input id="rootpw" type="password" value={rootPassword} onChange={(e) => setRootPassword(e.target.value)} autoComplete="new-password" />
        </div>
        <div className="field">
          <label htmlFor="confirm">Type REINSTALL to confirm</label>
          <input id="confirm" value={confirmText} onChange={(e) => setConfirmText(e.target.value)} autoComplete="off" />
        </div>
        <div className="row" style={{ justifyContent: 'flex-end' }}>
          <button type="button" onClick={onClose}>Cancel</button>
          <button type="submit" className="primary" disabled={busy || !templateId || confirmText.trim().toUpperCase() !== 'REINSTALL'}>
            {busy ? 'Queueing…' : 'Reinstall'}
          </button>
        </div>
      </form>
    </Modal>
  )
}

function WipeModal({ serverId, serial, onClose, onDone }: { serverId: string; serial: string; onClose: () => void; onDone: Done }) {
  const [method, setMethod] = useState<'secure' | 'zero'>('secure')
  const [confirmText, setConfirmText] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  async function submit(event: React.FormEvent) {
    event.preventDefault()
    setBusy(true)
    setError(null)
    try {
      await api.wipe(serverId, method)
      onDone('Wipe queued. The server powers off when it finishes and returns to stock.')
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal title="Secure wipe" onClose={onClose}>
      <form onSubmit={submit}>
        <Banner kind="error">Destroys all data on every drive in {serial}. Required before resale.</Banner>
        {error && <Banner kind="error">{error}</Banner>}
        <div className="field">
          <label htmlFor="method">Method</label>
          <select id="method" value={method} onChange={(e) => setMethod(e.target.value as 'secure' | 'zero')}>
            <option value="secure">Secure erase (ATA / NVMe / SCSI format, falls back to overwrite)</option>
            <option value="zero">Zero overwrite only</option>
          </select>
        </div>
        <div className="field">
          <label htmlFor="confirm">Type the serial <span className="mono">{serial}</span> to confirm</label>
          <input id="confirm" className="mono" value={confirmText} onChange={(e) => setConfirmText(e.target.value)} autoComplete="off" />
        </div>
        <div className="row" style={{ justifyContent: 'flex-end' }}>
          <button type="button" onClick={onClose}>Cancel</button>
          <button type="submit" className="primary" disabled={busy || confirmText.trim() !== serial}>{busy ? 'Queueing…' : 'Wipe'}</button>
        </div>
      </form>
    </Modal>
  )
}

function AssignIpModal({ serverId, hasPrimary, onClose, onDone }: { serverId: string; hasPrimary: boolean; onClose: () => void; onDone: Done }) {
  const blocks = useAsync<IPBlock[]>(() => api.ipBlocks())
  const [blockId, setBlockId] = useState('')
  const [address, setAddress] = useState('')
  const [isPrimary, setIsPrimary] = useState(!hasPrimary)
  const [suggestions, setSuggestions] = useState<string[]>([])
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  async function pickBlock(id: string) {
    setBlockId(id)
    setSuggestions([])
    if (!id) return
    try {
      const free = await api.freeAddresses(id, 12)
      setSuggestions(free.free_sample)
      if (!address && free.free_sample[0]) setAddress(free.free_sample[0])
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }

  async function submit(event: React.FormEvent) {
    event.preventDefault()
    setBusy(true)
    setError(null)
    try {
      await api.assignIp(serverId, { block_id: blockId, address: address.trim(), is_primary: isPrimary })
      onDone(`${address} assigned${isPrimary ? ' as primary' : ''}.`)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal title="Assign an address" onClose={onClose}>
      <form onSubmit={submit}>
        {error && <Banner kind="error">{error}</Banner>}
        <div className="field">
          <label htmlFor="block">Block</label>
          <select id="block" value={blockId} onChange={(e) => pickBlock(e.target.value)} required>
            <option value="">Choose…</option>
            {(blocks.data ?? []).map((b) => (
              <option key={b.id} value={b.id}>{b.cidr} — {b.total_hosts - b.assigned} free{b.gateway ? ` · gw ${b.gateway}` : ''}</option>
            ))}
          </select>
          {blocks.data && blocks.data.length === 0 && (
            <div className="subtle" style={{ fontSize: 12, marginTop: 4 }}>
              No blocks yet — <Link to="/admin/ipam">add one</Link> first.
            </div>
          )}
        </div>
        <div className="field">
          <label htmlFor="addr">Address</label>
          <input id="addr" className="mono" value={address} onChange={(e) => setAddress(e.target.value)} required />
          {suggestions.length > 0 && (
            <div className="row" style={{ marginTop: 6, gap: 6 }}>
              {suggestions.map((a) => (
                <button type="button" key={a} className="mono" style={{ fontSize: 12, padding: '3px 8px' }} onClick={() => setAddress(a)}>{a}</button>
              ))}
            </div>
          )}
        </div>
        <label className="row" style={{ marginBottom: 14 }}>
          <input type="checkbox" style={{ width: 'auto' }} checked={isPrimary} onChange={(e) => setIsPrimary(e.target.checked)} />
          <span>Primary — the installer configures this one statically</span>
        </label>
        <div className="row" style={{ justifyContent: 'flex-end' }}>
          <button type="button" onClick={onClose}>Cancel</button>
          <button type="submit" className="primary" disabled={busy || !blockId}>{busy ? 'Assigning…' : 'Assign'}</button>
        </div>
      </form>
    </Modal>
  )
}

function EditModal({ server, onClose, onDone }: { server: AdminServer; onClose: () => void; onDone: Done }) {
  const [form, setForm] = useState({
    hostname: server.hostname ?? '',
    datacenter: server.datacenter ?? '',
    rack: server.rack ?? '',
    rack_unit: server.rack_unit?.toString() ?? '',
    switch_name: server.switch_name ?? '',
    switch_port: server.switch_port ?? '',
    customer_vlan: server.customer_vlan?.toString() ?? '',
    provisioning_mac: server.provisioning_mac ?? '',
    cimc_credential_ref: server.cimc_credential_ref,
    cimc_ip: server.cimc_ip,
    bmc_protocol: server.bmc_protocol ?? '',
    ipmi_port: server.ipmi_port?.toString() ?? '',
    redfish_port: server.redfish_port?.toString() ?? '',
    notes: server.notes ?? '',
  })
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const set = (k: keyof typeof form) => (e: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement>) =>
    setForm((f) => ({ ...f, [k]: e.target.value }))

  async function submit(event: React.FormEvent) {
    event.preventDefault()
    setBusy(true)
    setError(null)
    try {
      await api.updateServer(server.id, {
        hostname: form.hostname || null,
        datacenter: form.datacenter || null,
        rack: form.rack || null,
        rack_unit: form.rack_unit ? Number(form.rack_unit) : null,
        switch_name: form.switch_name || null,
        switch_port: form.switch_port || null,
        customer_vlan: form.customer_vlan ? Number(form.customer_vlan) : null,
        provisioning_mac: form.provisioning_mac || null,
        cimc_credential_ref: form.cimc_credential_ref,
        cimc_ip: form.cimc_ip,
        bmc_protocol: form.bmc_protocol || null,
        ipmi_port: form.ipmi_port ? Number(form.ipmi_port) : null,
        redfish_port: form.redfish_port ? Number(form.redfish_port) : null,
        notes: form.notes || null,
      })
      onDone('Saved.')
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal title={`Edit ${server.serial}`} onClose={onClose}>
      <form onSubmit={submit}>
        {error && <Banner kind="error">{error}</Banner>}
        <div className="grid cols-2">
          <div className="field"><label>Hostname</label><input value={form.hostname} onChange={set('hostname')} /></div>
          <div className="field"><label>Credential ref</label><input className="mono" value={form.cimc_credential_ref} onChange={set('cimc_credential_ref')} /></div>
          <div className="field"><label>CIMC IP</label><input className="mono" value={form.cimc_ip} onChange={set('cimc_ip')} /></div>
          <div className="field">
            <label>Power/boot control</label>
            <select value={form.bmc_protocol} onChange={set('bmc_protocol')}>
              <option value="">Platform default</option>
              <option value="auto">Auto — IPMI, then Redfish</option>
              <option value="ipmi">IPMI only</option>
              <option value="redfish">Redfish only</option>
            </select>
          </div>
          <div className="field"><label>IPMI port (blank = 623)</label><input type="number" min={1} max={65535} value={form.ipmi_port} onChange={set('ipmi_port')} /></div>
          <div className="field"><label>HTTPS port (blank = 443)</label><input type="number" min={1} max={65535} value={form.redfish_port} onChange={set('redfish_port')} /></div>
          <div className="field"><label>Datacenter</label><input value={form.datacenter} onChange={set('datacenter')} /></div>
          <div className="field"><label>Rack</label><input value={form.rack} onChange={set('rack')} /></div>
          <div className="field"><label>Rack unit</label><input type="number" min={1} max={60} value={form.rack_unit} onChange={set('rack_unit')} /></div>
          <div className="field"><label>PXE MAC</label><input className="mono" value={form.provisioning_mac} onChange={set('provisioning_mac')} /></div>
          <div className="field"><label>Switch</label><input value={form.switch_name} onChange={set('switch_name')} /></div>
          <div className="field"><label>Switch port</label><input value={form.switch_port} onChange={set('switch_port')} /></div>
          <div className="field"><label>Customer VLAN</label><input type="number" min={1} max={4094} value={form.customer_vlan} onChange={set('customer_vlan')} /></div>
        </div>
        <div className="field"><label>Notes</label><textarea value={form.notes} onChange={set('notes')} /></div>
        <div className="row" style={{ justifyContent: 'flex-end' }}>
          <button type="button" onClick={onClose}>Cancel</button>
          <button type="submit" className="primary" disabled={busy}>{busy ? 'Saving…' : 'Save'}</button>
        </div>
      </form>
    </Modal>
  )
}

// ---------------------------------------------------------------------------

function KvmLauncher({ serverId, cimcIp, port }: { serverId: string; cimcIp: string; port: number | null }) {
  const [busy, setBusy] = useState(false)
  const [links, setLinks] = useState<{ html5: string | null; java: string; cimc: string } | null>(null)
  const [error, setError] = useState<string | null>(null)
  const cimcUrl = `https://${cimcIp}${port ? `:${port}` : ''}/`

  async function launch() {
    setBusy(true)
    setError(null)
    // Open the window synchronously, inside the click, or popup blockers
    // swallow it; point it at the viewer once the tokens arrive.
    const win = window.open('about:blank', '_blank')
    try {
      const result = await api.launchKvm(serverId)
      setLinks(result)
      if (win) {
        if (result.html5) win.location.href = result.html5
        else win.close()
      }
    } catch (e) {
      win?.close()
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div style={{ textAlign: 'right', marginLeft: 'auto' }}>
      <div className="row" style={{ justifyContent: 'flex-end' }}>
        <button className="primary" onClick={launch} disabled={busy}>
          {busy ? 'Getting tokens…' : 'Launch KVM'}
        </button>
        <a className="button" href={cimcUrl} target="_blank" rel="noreferrer">
          CIMC web UI
        </a>
      </div>
      {links && !links.html5 && (
        <div className="subtle" style={{ fontSize: 12, marginTop: 6 }}>
          No HTML5 viewer found on this firmware.{' '}
          <a href={links.java}>Java launcher (.jnlp)</a> or use the CIMC web UI.
        </div>
      )}
      {error && (
        <div style={{ fontSize: 12, marginTop: 6, color: 'var(--crit)', maxWidth: 420 }}>{error}</div>
      )}
      <div className="subtle" style={{ fontSize: 12, marginTop: 6, maxWidth: 420 }}>
        First time? Open the CIMC web UI once and accept its certificate, or the KVM
        window will be blocked.
      </div>
    </div>
  )
}
