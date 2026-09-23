import { useCallback, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { api, type Job, type OSTemplate, type SSHKey } from '../api'
import {
  Banner,
  Empty,
  Modal,
  Pill,
  Progress,
  formatBits,
  formatBytes,
  formatTime,
  relativeTime,
} from '../components'
import { useAsync, usePolling } from '../hooks'
import { PowerControls } from '../power'

const ACTIVE_JOB_STATES = ['queued', 'running']

export default function ServerDetail({ isAdmin }: { isAdmin: boolean }) {
  const { id = '' } = useParams()
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [reinstalling, setReinstalling] = useState(false)
  const [rescuing, setRescuing] = useState(false)
  const [busy] = useState(false)

  const server = useAsync(() => api.server(id), [id])
  const jobs = useAsync(() => api.serverJobs(id), [id])
  const bandwidth = useAsync(() => api.bandwidth(id, '24h'), [id])

  const activeJob = (jobs.data ?? []).find((j) => ACTIVE_JOB_STATES.includes(j.state))

  const refresh = useCallback(async () => {
    await Promise.all([server.reload(), jobs.reload()])
  }, [server.reload, jobs.reload]) // eslint-disable-line react-hooks/exhaustive-deps

  // While a job is in flight the page is a progress display, so poll fast.
  usePolling(refresh, 5000, Boolean(activeJob))

  if (server.error) return <main className="page"><Banner kind="error">{server.error}</Banner></main>
  if (!server.data) return <main className="page"><Empty>Loading…</Empty></main>

  const s = server.data
  const locked = busy || Boolean(activeJob)

  return (
    <main className="page">
      <div className="spread">
        <div>
          <h1>{s.hostname ?? s.serial}</h1>
          <p className="subtle mono">
            {s.serial} · {s.model} {s.datacenter ? `· ${s.datacenter}` : ''}
          </p>
        </div>
        <div className="row">
          <Pill value={s.state} />
          <Pill value={s.health_status} />
        </div>
      </div>

      {error && <Banner kind="error">{error}</Banner>}
      {notice && <Banner kind="info">{notice}</Banner>}

      {activeJob && <ActiveJobCard job={activeJob} />}

      {/* --- power ------------------------------------------------------ */}
      <div className="card">
        <PowerControls serverId={id} activeJob={activeJob ?? null} onChanged={refresh} pollMs={30000} />
      </div>

      <div className="card">
        <div className="spread">
          <div>
            <strong>Operating system</strong>
            <div className="subtle">
              Reinstall wipes the array. Rescue boots to RAM and leaves the disks alone.
            </div>
          </div>
          <div className="row">
            <button disabled={locked} onClick={() => setRescuing(true)}>
              Rescue mode
            </button>
            <button className="danger" disabled={locked} onClick={() => setReinstalling(true)}>
              Reinstall
            </button>
            <Link className="button" to={`/servers/${id}/console`}>
              Serial console
            </Link>
          </div>
        </div>
      </div>

      {/* --- hardware --------------------------------------------------- */}
      <h2>Hardware</h2>
      <div className="grid cols-2">
        <div className="card">
          <table>
            <tbody>
              <tr>
                <td className="subtle">CPU</td>
                <td>{s.cpu_count ? `${s.cpu_count}× ${s.cpu_model}` : '—'}</td>
              </tr>
              <tr>
                <td className="subtle">Threads</td>
                <td>{s.cpu_cores_total ?? '—'}</td>
              </tr>
              <tr>
                <td className="subtle">Memory</td>
                <td>{s.ram_gb ? `${s.ram_gb} GB` : '—'}</td>
              </tr>
              <tr>
                <td className="subtle">Drives</td>
                <td>{s.drives.length || '—'}</td>
              </tr>
            </tbody>
          </table>
        </div>
        <div className="card">
          <strong style={{ fontSize: 14 }}>Health</strong>
          <div className="subtle" style={{ marginBottom: 8 }}>
            Checked {relativeTime(s.health_checked_at)}
          </div>
          {Object.entries(s.health?.subsystems ?? {}).length === 0 ? (
            <div className="subtle">No health data collected yet.</div>
          ) : (
            <table>
              <tbody>
                {Object.entries(s.health?.subsystems ?? {}).map(([name, info]) => (
                  <tr key={name}>
                    <td className="subtle">{name.replace(/_/g, ' ')}</td>
                    <td>
                      <Pill value={(info as { status?: string })?.status} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </div>

      {s.drives.length > 0 && (
        <div className="card table-scroll">
          <table>
            <thead>
              <tr>
                <th>Drive</th>
                <th>Model</th>
                <th>Capacity</th>
                <th>Media</th>
                <th>Health</th>
              </tr>
            </thead>
            <tbody>
              {s.drives.map((drive, i) => {
                const d = drive as Record<string, string | number | boolean | null>
                return (
                  <tr key={i}>
                    <td className="mono">{String(d.name ?? '—')}</td>
                    <td className="subtle">{String(d.model ?? '—')}</td>
                    <td>{d.capacity_gb ? `${d.capacity_gb} GB` : '—'}</td>
                    <td className="subtle">{String(d.media ?? '—')}</td>
                    <td>
                      <Pill value={d.failure_predicted ? 'warning' : String(d.health ?? '')} />
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}

      {/* --- networking ------------------------------------------------- */}
      <h2>Networking</h2>
      <IPTable serverId={id} addresses={s.ip_addresses} onError={setError} />

      <h2>Bandwidth (24h)</h2>
      <div className="card">
        {bandwidth.data?.points.length ? (
          <>
            <BandwidthChart series={bandwidth.data.points} />
            <div className="row" style={{ marginTop: 12 }}>
              <span className="subtle">
                In {formatBytes(bandwidth.data.total_rx_bytes)} · Out{' '}
                {formatBytes(bandwidth.data.total_tx_bytes)}
              </span>
            </div>
          </>
        ) : (
          <div className="subtle">
            No samples yet. Bandwidth is collected from the access switch every five minutes.
          </div>
        )}
      </div>

      {/* --- history ---------------------------------------------------- */}
      <h2>Recent jobs</h2>
      <div className="card" style={{ padding: 0 }}>
        {!jobs.data?.length ? (
          <Empty>Nothing has run on this server yet.</Empty>
        ) : (
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>Action</th>
                  <th>State</th>
                  <th>Started</th>
                  <th>Finished</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {jobs.data.slice(0, 15).map((job) => (
                  <tr key={job.id}>
                    <td>{job.type.replace(/_/g, ' ')}</td>
                    <td>
                      <Pill value={job.state} />
                    </td>
                    <td className="subtle">{formatTime(job.started_at)}</td>
                    <td className="subtle">{formatTime(job.finished_at)}</td>
                    <td>
                      <Link to={`/jobs/${job.id}`}>
                        {isAdmin ? 'Full log' : 'Details'}
                      </Link>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {reinstalling && (
        <ReinstallModal
          serverId={id}
          hostname={s.hostname}
          onClose={() => setReinstalling(false)}
          onQueued={async () => {
            setReinstalling(false)
            setNotice('Reinstall queued. This takes about 20 minutes.')
            await refresh()
          }}
        />
      )}

      {rescuing && (
        <RescueModal
          serverId={id}
          onClose={() => setRescuing(false)}
          onQueued={async () => {
            setRescuing(false)
            setNotice('Rescue boot queued.')
            await refresh()
          }}
        />
      )}
    </main>
  )
}

// ---------------------------------------------------------------------------

function ActiveJobCard({ job }: { job: Job }) {
  return (
    <div className="card">
      <div className="spread" style={{ marginBottom: 10 }}>
        <div>
          <strong>{job.type.replace(/_/g, ' ')} in progress</strong>
          <div className="subtle">{job.stage ?? 'starting…'}</div>
        </div>
        <Link to={`/jobs/${job.id}`}>View log</Link>
      </div>
      <Progress value={job.progress} />
    </div>
  )
}

function IPTable({
  serverId,
  addresses,
  onError,
}: {
  serverId: string
  addresses: Array<{ id: string; address: string; prefix_len: number; gateway: string | null; is_primary: boolean; rdns: string | null }>
  onError: (message: string) => void
}) {
  const [editing, setEditing] = useState<string | null>(null)
  const [value, setValue] = useState('')
  const [rows, setRows] = useState(addresses)

  if (!rows.length) {
    return (
      <div className="card">
        <div className="subtle">No addresses assigned yet.</div>
      </div>
    )
  }

  async function save(assignmentId: string) {
    try {
      const updated = await api.setRdns(serverId, assignmentId, value.trim() || null)
      setRows((current) =>
        current.map((r) => (r.id === assignmentId ? { ...r, rdns: updated.rdns } : r)),
      )
      setEditing(null)
    } catch (e) {
      onError(e instanceof Error ? e.message : String(e))
    }
  }

  return (
    <div className="card table-scroll">
      <table>
        <thead>
          <tr>
            <th>Address</th>
            <th>Gateway</th>
            <th>Reverse DNS</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {rows.map((ip) => (
            <tr key={ip.id}>
              <td className="mono">
                {ip.address}/{ip.prefix_len}
                {ip.is_primary && <span className="pill" style={{ marginLeft: 8 }}>primary</span>}
              </td>
              <td className="mono subtle">{ip.gateway ?? '—'}</td>
              <td>
                {editing === ip.id ? (
                  <input
                    value={value}
                    onChange={(e) => setValue(e.target.value)}
                    placeholder="host.example.com"
                    autoFocus
                  />
                ) : (
                  <span className="mono subtle">{ip.rdns ?? '—'}</span>
                )}
              </td>
              <td>
                {editing === ip.id ? (
                  <div className="row">
                    <button onClick={() => save(ip.id)}>Save</button>
                    <button onClick={() => setEditing(null)}>Cancel</button>
                  </div>
                ) : (
                  <button
                    onClick={() => {
                      setEditing(ip.id)
                      setValue(ip.rdns ?? '')
                    }}
                  >
                    Edit
                  </button>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

/** Inline SVG area chart. A charting library would be more code than this. */
function BandwidthChart({ series }: { series: Array<{ timestamp: string; rx_bps: number; tx_bps: number }> }) {
  const width = 640
  const height = 140
  const peak = Math.max(...series.flatMap((p) => [p.rx_bps, p.tx_bps]), 1)

  const path = (key: 'rx_bps' | 'tx_bps') =>
    series
      .map((point, i) => {
        const x = (i / Math.max(series.length - 1, 1)) * width
        const y = height - (point[key] / peak) * (height - 10)
        return `${i === 0 ? 'M' : 'L'}${x.toFixed(1)},${y.toFixed(1)}`
      })
      .join(' ')

  return (
    <div className="table-scroll">
      <svg viewBox={`0 0 ${width} ${height}`} width="100%" height={height} role="img"
           aria-label={`Bandwidth, peak ${formatBits(peak)}`}>
        <path d={path('rx_bps')} fill="none" stroke="var(--accent)" strokeWidth="1.5" />
        <path d={path('tx_bps')} fill="none" stroke="var(--warn)" strokeWidth="1.5" />
      </svg>
      <div className="row subtle" style={{ fontSize: 12 }}>
        <span style={{ color: 'var(--accent)' }}>■</span> in
        <span style={{ color: 'var(--warn)' }}>■</span> out
        <span>peak {formatBits(peak)}</span>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------

function ReinstallModal({
  serverId,
  hostname,
  onClose,
  onQueued,
}: {
  serverId: string
  hostname: string | null
  onClose: () => void
  onQueued: () => void
}) {
  const templates = useAsync<OSTemplate[]>(() => api.osTemplates())
  const keys = useAsync<SSHKey[]>(() => api.sshKeys())
  const [templateId, setTemplateId] = useState('')
  const [name, setName] = useState(hostname ?? '')
  const [raid, setRaid] = useState('raid1')
  const [selectedKeys, setSelectedKeys] = useState<string[]>([])
  const [confirmText, setConfirmText] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  // Typing the serial is deliberate friction. This destroys everything on the
  // array, and a misplaced click should not be enough to trigger it.
  const confirmed = confirmText.trim().toUpperCase() === 'REINSTALL'

  async function submit() {
    setBusy(true)
    setError(null)
    try {
      await api.reinstall(serverId, {
        os_template_id: templateId,
        hostname: name || undefined,
        raid_level: raid,
        ssh_key_ids: selectedKeys,
        confirm_data_loss: true,
      })
      onQueued()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal title="Reinstall operating system" onClose={onClose}>
      <Banner kind="error">
        This erases every disk on the server. There is no undo and no backup.
      </Banner>
      {error && <Banner kind="error">{error}</Banner>}

      <div className="field">
        <label htmlFor="os">Operating system</label>
        <select id="os" value={templateId} onChange={(e) => setTemplateId(e.target.value)}>
          <option value="">Choose…</option>
          {(templates.data ?? []).map((t) => (
            <option key={t.id} value={t.id}>
              {t.name} {t.version}
            </option>
          ))}
        </select>
      </div>

      <div className="field">
        <label htmlFor="hostname">Hostname</label>
        <input id="hostname" value={name} onChange={(e) => setName(e.target.value)} />
      </div>

      <div className="field">
        <label htmlFor="raid">RAID level</label>
        <select id="raid" value={raid} onChange={(e) => setRaid(e.target.value)}>
          <option value="raid1">RAID 1 (mirror, recommended)</option>
          <option value="raid0">RAID 0 (stripe, no redundancy)</option>
          <option value="raid5">RAID 5</option>
          <option value="raid10">RAID 10</option>
          <option value="none">No RAID (individual disks)</option>
        </select>
      </div>

      <div className="field">
        <label>SSH keys</label>
        {!keys.data?.length ? (
          <div className="subtle">
            No keys on your account. <Link to="/ssh-keys">Add one first</Link> — without a key
            you will not be able to log in.
          </div>
        ) : (
          keys.data.map((key) => (
            <label key={key.id} className="row" style={{ marginBottom: 4 }}>
              <input
                type="checkbox"
                style={{ width: 'auto' }}
                checked={selectedKeys.includes(key.id)}
                onChange={(e) =>
                  setSelectedKeys((current) =>
                    e.target.checked
                      ? [...current, key.id]
                      : current.filter((k) => k !== key.id),
                  )
                }
              />
              <span>{key.name}</span>
              <span className="subtle mono" style={{ fontSize: 11 }}>
                {key.fingerprint}
              </span>
            </label>
          ))
        )}
        <div className="subtle" style={{ marginTop: 6, fontSize: 12 }}>
          Leave all unchecked to install every key on your account.
        </div>
      </div>

      <div className="field">
        <label htmlFor="confirm">Type REINSTALL to confirm</label>
        <input
          id="confirm"
          value={confirmText}
          onChange={(e) => setConfirmText(e.target.value)}
          autoComplete="off"
        />
      </div>

      <div className="row" style={{ justifyContent: 'flex-end' }}>
        <button onClick={onClose}>Cancel</button>
        <button
          className="primary"
          disabled={!templateId || !confirmed || busy}
          onClick={submit}
        >
          {busy ? 'Queueing…' : 'Reinstall'}
        </button>
      </div>
    </Modal>
  )
}

function RescueModal({
  serverId,
  onClose,
  onQueued,
}: {
  serverId: string
  onClose: () => void
  onQueued: () => void
}) {
  const keys = useAsync<SSHKey[]>(() => api.sshKeys())
  const [selectedKeys, setSelectedKeys] = useState<string[]>([])
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  async function submit() {
    setBusy(true)
    setError(null)
    try {
      await api.rescue(serverId, selectedKeys)
      onQueued()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal title="Boot into rescue mode" onClose={onClose}>
      <Banner kind="info">
        The server reboots into a Linux environment running entirely in RAM. Your disks are
        left untouched and can be mounted manually. Reboot from the portal to return to the
        installed system.
      </Banner>
      {error && <Banner kind="error">{error}</Banner>}

      <div className="field">
        <label>SSH keys to authorise</label>
        {!keys.data?.length ? (
          <div className="subtle">
            You need at least one <Link to="/ssh-keys">SSH key</Link> to log into rescue mode.
          </div>
        ) : (
          keys.data.map((key) => (
            <label key={key.id} className="row" style={{ marginBottom: 4 }}>
              <input
                type="checkbox"
                style={{ width: 'auto' }}
                checked={selectedKeys.includes(key.id)}
                onChange={(e) =>
                  setSelectedKeys((current) =>
                    e.target.checked
                      ? [...current, key.id]
                      : current.filter((k) => k !== key.id),
                  )
                }
              />
              <span>{key.name}</span>
            </label>
          ))
        )}
      </div>

      <div className="row" style={{ justifyContent: 'flex-end' }}>
        <button onClick={onClose}>Cancel</button>
        <button className="primary" disabled={busy || !keys.data?.length} onClick={submit}>
          {busy ? 'Queueing…' : 'Boot rescue'}
        </button>
      </div>
    </Modal>
  )
}
