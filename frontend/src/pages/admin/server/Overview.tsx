import { Disc3, HardDriveDownload, LifeBuoy, Trash2 } from 'lucide-react'
import { useState } from 'react'
import { Link } from 'react-router-dom'
import { api, type BootDevice, type BootFollowUp, type ServerState } from '../../../api'
import { Banner, Card, Empty, KV, Pill, formatTime, label, relativeTime, useConfirm, useNow } from '../../../components'
import { TARGET_FIRMWARE, firmwareBelowTarget } from '../../../firmware'
import { waitForJob } from '../../../hooks'
import { useToast } from '../../../toast'
import { AdminReinstallModal, AssignModal, InstallFromImageModal, WipeModal } from './modals'
import { POWER_SOURCE, UtilisationCard, formatValue, useLiveSensors } from './Sensors'
import { useServer } from './ServerPage'


//: Legal targets per current state, mirroring SERVER_TRANSITIONS in the backend
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

const BOOT_DEVICES: Array<{ value: BootDevice; label: string }> = [
  { value: 'pxe', label: 'Network (PXE)' },
  { value: 'disk', label: 'First disk' },
  { value: 'cdrom', label: 'CD / virtual media' },
  { value: 'bios', label: 'BIOS setup' },
]

export default function Overview() {
  const { server: s, activeJob, subscription, refresh } = useServer()
  const toast = useToast()
  const confirm = useConfirm()
  const now = useNow()
  const [modal, setModal] = useState<null | 'assign' | 'reinstall' | 'wipe' | 'image'>(null)
  const [bootDevice, setBootDevice] = useState<BootDevice>('pxe')
  const [bootThen, setBootThen] = useState<BootFollowUp>('reset')
  const readOnly = s.role === 'management'
  const locked = Boolean(activeJob) || readOnly

  const sensors = useLiveSensors(s.id, 10000)
  const readings = sensors.report?.sensors ?? []
  const temps = readings.filter((r) => r.kind === 'temperature' && r.value != null)
  const fans = readings.filter((r) => r.kind === 'fan' && r.value != null)
  // The spread, not an average: one slow fan is the thing worth seeing.
  const fanSpeeds = fans.map((f) => Math.round(f.value ?? 0))
  const fanLow = Math.min(...fanSpeeds)
  const fanHigh = Math.max(...fanSpeeds)
  // DCMI, else the PSU output sensors (never input and output added together).
  const power = sensors.report?.power ?? null
  const hottest = temps.slice().sort((a, b) => (b.value ?? 0) - (a.value ?? 0))[0]
  const worst = readings.some((r) => r.status === 'critical') ? 'critical' : readings.some((r) => r.status === 'warning') ? 'warning' : readings.length ? 'ok' : null

  async function bootOnce() {
    const device = BOOT_DEVICES.find((d) => d.value === bootDevice)!.label
    if (bootThen !== 'none' && !(await confirm({ title: `Boot from ${device} now?`, body: bootThen === 'cycle' ? 'The server is power-cycled straight away.' : 'The server is reset (or powered on) straight away.', confirmLabel: 'Boot', danger: true }))) return
    await toast.run(async () => {
      const job = await api.bootOverride(s.id, bootDevice, bootThen)
      await refresh()
      const done = await waitForJob(job.id)
      await refresh()
      toast.ok(done.stage ?? 'Boot device set')
    })
  }

  async function rescue() {
    if (!(await confirm({ title: 'Boot into rescue mode?', body: 'The server reboots into a Linux environment in RAM. Disks are not touched. Staff SSH keys are installed.', confirmLabel: 'Rescue boot' }))) return
    await toast.run(async () => { await api.rescue(s.id, []); await refresh() }, 'Rescue boot queued')
  }

  async function suspend() {
    const reason = window.prompt('Reason for suspension (goes in the audit log):', 'abuse')
    if (!reason) return
    await toast.run(async () => { await api.suspend(s.id, reason); await refresh() }, 'Suspended. Shut the switch port by hand; that is not automated yet.')
  }

  async function setState(target: ServerState) {
    if (!(await confirm({ title: `Move ${s.serial} to ${label(target)}?`, body: `Current state: ${label(s.state)}. This changes the lifecycle state only; it does not touch the server.` }))) return
    await toast.run(async () => { await api.changeServerState(s.id, target, 'manual from admin panel'); await refresh() }, `State set to ${label(target)}`)
  }

  return (
    <>
      <div className="grid" style={{ gridTemplateColumns: 'minmax(0, 3fr) minmax(280px, 2fr)' }}>
        <div className="stack" style={{ gap: 16 }}>
          <Card
            title="Live readings"
            actions={worst ? <Pill value={worst} /> : null}
            note={sensors.error ? 'Neither IPMI nor Redfish is answering on this server.' : sensors.report ? `${sensors.report.stale ? 'Last good reading' : 'Read'} ${relativeTime(sensors.report.checked_at, now)} · every 10 s${sensors.report.via === 'redfish' ? ' · via Redfish, IPMI is not answering' : ''} · ${readings.length} sensors on the Sensors tab${sensors.report.stale ? ' · the BMC missed the last poll' : ''}` : 'Reading sensors…'}
          >
            {sensors.error ? (
              <Banner kind="warning">No readings: the BMC is answering neither IPMI nor Redfish. <Link to={`/admin/servers/${s.id}/bmc`}>Test the connection</Link> on the BMC tab to see why.</Banner>
            ) : readings.length === 0 ? (
              <div className="subtle">Waiting for the first reading…</div>
            ) : (
              <div className="readings">
                {hottest && (
                  <div className={`reading ${hottest.status}`}><div className="reading-label">Hottest · {hottest.name}</div><div className="reading-value">{formatValue(hottest)}</div></div>
                )}
                {temps.filter((t) => /inlet|ambient|front/i.test(t.name)).slice(0, 1).map((t) => (
                  <div key={t.name} className={`reading ${t.status}`}><div className="reading-label">{t.name}</div><div className="reading-value">{formatValue(t)}</div></div>
                ))}
                {fans.length > 0 && (
                  <div className={`reading ${fans.some((f) => f.status !== 'ok') ? 'warning' : ''}`}><div className="reading-label">Fans ({fans.length})</div><div className="reading-value">{fanLow === fanHigh ? fanLow : `${fanLow}–${fanHigh}`}<span className="sensor-unit">{fans[0].unit ?? 'RPM'}</span></div></div>
                )}
                {power && power.watts > 0 && (
                  <div className="reading"><div className="reading-label">Power draw · {POWER_SOURCE[power.source ?? ''] ?? 'BMC'}</div><div className="reading-value">{Math.round(power.watts)}<span className="sensor-unit">W</span></div></div>
                )}
              </div>
            )}
          </Card>

          <UtilisationCard report={sensors.report} />

          {!readOnly && <Card title="Provisioning" note="Reinstall and wipe destroy data. Rescue and boot-device changes do not.">
            <div className="stack" style={{ gap: 14 }}>
              <div className="spread">
                <div>
                  <div className="strong">Operating system</div>
                  <div className="subtle small">Automated install from a template over PXE, or boot an ISO from the image store and drive the installer over the KVM.</div>
                </div>
                <div className="row">
                  <button disabled={locked || !s.provisioning_mac} title={!s.provisioning_mac ? 'Set the PXE MAC first (Hardware tab)' : ''} onClick={() => setModal('reinstall')}><HardDriveDownload />Reinstall</button>
                  <button disabled={locked} onClick={() => setModal('image')}><Disc3 />Install from image</button>
                  <button disabled={locked || !s.provisioning_mac} onClick={rescue}><LifeBuoy />Rescue</button>
                  <button className="danger" disabled={locked || !s.provisioning_mac} onClick={() => setModal('wipe')}><Trash2 />Secure wipe</button>
                </div>
              </div>
              <hr className="divider" style={{ margin: 0 }} />
              <div className="spread">
                <div>
                  <div className="strong">Boot once from</div>
                  <div className="subtle small">A one-time boot device on the BMC. Set and reset now, or set only and restart within a minute.</div>
                </div>
                <div className="row">
                  <select value={bootDevice} onChange={(e) => setBootDevice(e.target.value as BootDevice)} style={{ width: 'auto' }}>
                    {BOOT_DEVICES.map((d) => <option key={d.value} value={d.value}>{d.label}</option>)}
                  </select>
                  <select value={bootThen} onChange={(e) => setBootThen(e.target.value as BootFollowUp)} style={{ width: 'auto' }}>
                    <option value="reset">then reset now</option>
                    <option value="cycle">then power cycle</option>
                    <option value="on">then power on</option>
                    <option value="none">set only</option>
                  </select>
                  <button disabled={locked} onClick={bootOnce}>Boot</button>
                </div>
              </div>
            </div>
          </Card>}

          <Card title="Lifecycle">
            <div className="spread">
              <div>
                Currently <strong>{label(s.state)}</strong>
                <span className="subtle"> since {formatTime(s.state_changed_at)}</span>
                <div className="subtle small">
                  {s.state === 'wiping' && !s.last_wiped_at && 'Needs a completed wipe before it can return to stock. '}
                  {s.last_wiped_at && `Last wiped ${relativeTime(s.last_wiped_at, now)}. `}
                  {s.state === 'suspended' && 'Suspended: the customer cannot use the portal for it. Shut the switch port by hand.'}
                </div>
              </div>
              <div className="row">
                {s.state === 'suspended' ? (
                  <button onClick={() => toast.run(async () => { await api.unsuspend(s.id); await refresh() }, 'Unsuspended. Re-enable the switch port by hand.')}>Unsuspend</button>
                ) : (
                  <button className="danger" disabled={s.state !== 'active'} onClick={suspend}>Suspend</button>
                )}
                <select value="" disabled={(TRANSITIONS[s.state] ?? []).length === 0} onChange={(e) => e.target.value && setState(e.target.value as ServerState)} style={{ width: 'auto' }}>
                  <option value="">Set state…</option>
                  {(TRANSITIONS[s.state] ?? []).map((st) => <option key={st} value={st}>{label(st)}</option>)}
                </select>
              </div>
            </div>
          </Card>
        </div>

        <div className="stack" style={{ gap: 16 }}>
          <Card title="Customer">
            {subscription ? (
              <>
                <KV items={[
                  ['Account', subscription.customer_email],
                  ['Plan', subscription.plan_name],
                  ['Price', subscription.monthly_price != null ? `${subscription.currency} ${subscription.monthly_price}/mo` : null],
                  ['Since', formatTime(subscription.started_at)],
                ]} />
                <div style={{ marginTop: 12 }}>
                  <button className="sm danger" onClick={async () => {
                    if (await confirm({ title: `End the subscription for ${subscription.customer_email}?`, body: 'The server keeps running until you wipe it. Wipe before reassigning.', confirmLabel: 'End subscription', danger: true }))
                      await toast.run(async () => { await api.endSubscription(subscription.id); await refresh() }, 'Subscription ended')
                  }}>End subscription</button>
                </div>
              </>
            ) : (
              <>
                <p className="subtle" style={{ marginBottom: 12 }}>
                  Unassigned. {s.state === 'in_stock' || s.state === 'active' ? 'Ready to hand to a customer.' : `Cannot be assigned while ${label(s.state)}.`}
                </p>
                <button className="primary sm" disabled={!(s.state === 'in_stock' || s.state === 'active')} onClick={() => setModal('assign')}>Assign to customer</button>
              </>
            )}
          </Card>

          <Card title="Management">
            <KV items={[
              ['CIMC', <a className="mono" href={`https://${s.cimc_ip}${s.redfish_port ? `:${s.redfish_port}` : ''}/`} target="_blank" rel="noreferrer" title="Open the CIMC web UI">{s.cimc_ip}</a>],
              ['Firmware', s.cimc_firmware ? <span className="row" style={{ gap: 6 }}><span className="mono">{s.cimc_firmware}</span>{firmwareBelowTarget(s.cimc_firmware) && <span className="pill warning">below {TARGET_FIRMWARE}</span>}</span> : <span className="faint">unknown until inventory sync</span>],
              ['Prepared', s.bmc_prepared_at ? <span className="subtle">{relativeTime(s.bmc_prepared_at, now)}</span> : <span className="pill warning">never</span>],
              ['PXE MAC', s.provisioning_mac ? <span className="mono">{s.provisioning_mac}</span> : <span className="pill warning">not set</span>],
              ['Health', <span className="row" style={{ gap: 6 }}><Pill value={s.health_status} /><span className="faint small">{relativeTime(s.health_checked_at, now)}</span></span>],
            ]} />
            <div className="small" style={{ marginTop: 10 }}><Link to={`/admin/servers/${s.id}/bmc`}>BMC settings and tools</Link></div>
          </Card>

          <Card title="Location">
            <KV items={[
              ['Datacenter', s.datacenter],
              ['Rack', s.rack ? `${s.rack}${s.rack_unit ? ` U${s.rack_unit}` : ''}` : null],
              ['Switch port', s.switch_port ? <span className="mono">{s.switch_name ?? ''} {s.switch_port}</span> : null],
              ['Customer VLAN', s.customer_vlan],
            ]} />
          </Card>

          {s.notes && (
            <Card title="Notes"><div style={{ whiteSpace: 'pre-wrap' }}>{s.notes}</div></Card>
          )}
        </div>
      </div>

      <RecentJobs />

      {modal === 'assign' && <AssignModal serverId={s.id} onClose={() => setModal(null)} onDone={async (m) => { setModal(null); toast.ok(m); await refresh() }} />}
      {modal === 'reinstall' && <AdminReinstallModal serverId={s.id} customerEmail={subscription?.customer_email ?? null} onClose={() => setModal(null)} onDone={async (m) => { setModal(null); toast.ok(m); await refresh() }} />}
      {modal === 'wipe' && <WipeModal serverId={s.id} serial={s.serial} onClose={() => setModal(null)} onDone={async (m) => { setModal(null); toast.ok(m); await refresh() }} />}
      {modal === 'image' && <InstallFromImageModal serverId={s.id} onClose={() => setModal(null)} onDone={async (m) => { setModal(null); toast.ok(m); await refresh() }} />}
    </>
  )
}

function RecentJobs() {
  const { server: s, jobs } = useServer()
  const now = useNow()
  const recent = jobs.slice(0, 6)
  return (
    <Card title="Recent jobs" actions={<Link to={`/admin/servers/${s.id}/jobs`}>All jobs</Link>} flush>
      {recent.length === 0 ? (
        <Empty>Nothing has run on this server yet.</Empty>
      ) : (
        <table className="compact">
          <thead><tr><th>Job</th><th>State</th><th>Stage / error</th><th>When</th></tr></thead>
          <tbody>
            {recent.map((job) => (
              <tr key={job.id}>
                <td><Link to={`/jobs/${job.id}`}>{label(job.type)}</Link></td>
                <td><Pill value={job.state} /></td>
                <td className="subtle truncate" style={{ maxWidth: 420 }}>{job.error ? <span style={{ color: 'var(--crit)' }}>{job.error}</span> : job.stage ?? '—'}</td>
                <td className="faint nowrap" title={formatTime(job.created_at)}>{relativeTime(job.created_at, now)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Card>
  )
}
