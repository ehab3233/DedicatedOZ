import { AlertTriangle, Cpu, HardDrive, Server, Wrench, XCircle } from 'lucide-react'
import { Link } from 'react-router-dom'
import { api, type AdminServer, type Job } from '../../api'
import { Card, Empty, PageHeader, Pill, Stat, formatTime, label, relativeTime, useNow } from '../../components'
import { TARGET_FIRMWARE, firmwareBelowTarget } from '../../firmware'
import { useAsync, usePolling } from '../../hooks'


export default function Dashboard() {
  const summary = useAsync(() => api.fleetSummary())
  const fleet = useAsync(() => api.fleet())
  const power = useAsync(() => api.fleetPower())
  const jobs = useAsync(() => api.adminJobs({}))
  const now = useNow()

  usePolling(async () => {
    await Promise.all([summary.reload(), fleet.reload(), jobs.reload()])
  }, 15000, true)
  usePolling(power.reload, 30000, true)

  const byState = summary.data?.servers_by_state ?? {}
  const servers = fleet.data ?? []
  const powerStates = power.data?.servers ?? {}
  const poweredOn = Object.values(powerStates).filter((p) => p.state === 'on').length
  const unreachable = Object.values(powerStates).filter((p) => p.state === 'unknown').length

  const attention = buildAttention(servers, jobs.data ?? [])
  const recent = (jobs.data ?? []).slice(0, 8)
  const serial = (id: string | null) => servers.find((s) => s.id === id)?.serial ?? '—'

  return (
    <main className="page">
      <PageHeader title="Dashboard" sub="The fleet right now. Power is read live from every BMC; the rest is from the last poll." />

      <div className="grid cols-4" style={{ marginBottom: 16 }}>
        <Stat label="Servers" value={summary.data?.total_servers ?? '—'} sub={`${byState.active ?? 0} active · ${byState.in_stock ?? 0} in stock`} to="/admin/servers" />
        <Stat
          label="Powered on"
          value={power.data ? poweredOn : '—'}
          sub={power.data ? `${unreachable ? `${unreachable} BMC${unreachable > 1 ? 's' : ''} unreachable · ` : ''}read ${relativeTime(power.data.checked_at, now)}` : 'reading…'}
          tone={unreachable ? 'warn' : undefined}
        />
        <Stat label="Running jobs" value={summary.data?.active_jobs ?? '—'} sub={`${summary.data?.failed_jobs_24h ?? 0} failed in 24h`} tone={summary.data?.failed_jobs_24h ? 'warn' : undefined} to="/admin/jobs" />
        <Stat label="Unhealthy" value={summary.data?.unhealthy_servers ?? '—'} sub="warning or critical at last poll" tone={summary.data?.unhealthy_servers ? 'crit' : 'ok'} />
      </div>

      <div className="grid" style={{ gridTemplateColumns: 'minmax(0, 3fr) minmax(280px, 2fr)' }}>
        <div className="stack" style={{ gap: 16 }}>
          <Card title="Needs attention" icon={<AlertTriangle />} flush>
            {attention.length === 0 ? (
              <Empty>Nothing needs attention.</Empty>
            ) : (
              <ul className="attention-list">
                {attention.map((item, i) => (
                  <li key={i}>
                    {item.icon}
                    <span style={{ flex: 1 }}>{item.text}</span>
                    <Link to={item.to}>{item.link}</Link>
                  </li>
                ))}
              </ul>
            )}
          </Card>

          <Card title="Recent jobs" actions={<Link to="/admin/jobs">All jobs</Link>} flush>
            {recent.length === 0 ? (
              <Empty>No jobs yet.</Empty>
            ) : (
              <table className="compact">
                <thead>
                  <tr><th>Job</th><th>Server</th><th>State</th><th>Stage</th><th>When</th></tr>
                </thead>
                <tbody>
                  {recent.map((job) => (
                    <tr key={job.id}>
                      <td><Link to={`/jobs/${job.id}`}>{label(job.type)}</Link></td>
                      <td className="mono">
                        {job.server_id ? <Link to={`/admin/servers/${job.server_id}`}>{serial(job.server_id)}</Link> : '—'}
                      </td>
                      <td><Pill value={job.state} /></td>
                      <td className="subtle truncate" style={{ maxWidth: 260 }}>{job.error ? <span style={{ color: 'var(--crit)' }}>{job.error}</span> : job.stage ?? '—'}</td>
                      <td className="faint nowrap" title={formatTime(job.created_at)}>{relativeTime(job.created_at, now)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </Card>
        </div>

        <div className="stack" style={{ gap: 16 }}>
          <SystemCard />
          <Card title="By state" flush>
            <table className="compact">
              <tbody>
                {Object.entries(byState).sort().map(([state, count]) => (
                  <tr key={state}>
                    <td><Pill value={state} /></td>
                    <td className="num right">{count}</td>
                  </tr>
                ))}
                {Object.keys(byState).length === 0 && (
                  <tr><td className="faint">No servers registered.</td></tr>
                )}
              </tbody>
            </table>
          </Card>
        </div>
      </div>
    </main>
  )
}

function buildAttention(servers: AdminServer[], jobs: Job[]) {
  const items: Array<{ icon: React.ReactNode; text: string; to: string; link: string }> = []
  for (const s of servers) {
    if (s.health_status === 'critical' || s.health_status === 'warning') {
      items.push({
        icon: <AlertTriangle style={{ color: s.health_status === 'critical' ? 'var(--crit)' : 'var(--warn)' }} />,
        text: `${s.serial}: health ${s.health_status} at the last poll`,
        to: `/admin/servers/${s.id}/hardware`, link: 'Hardware',
      })
    }
    if (!s.provisioning_mac && s.state !== 'retired') {
      items.push({ icon: <Server style={{ color: 'var(--warn)' }} />, text: `${s.serial}: no PXE MAC, so reinstalls will refuse`, to: `/admin/servers/${s.id}/hardware`, link: 'Pick a NIC' })
    }
    if (firmwareBelowTarget(s.cimc_firmware)) {
      items.push({ icon: <Cpu style={{ color: 'var(--text-3)' }} />, text: `${s.serial}: CIMC ${s.cimc_firmware}, below the ${TARGET_FIRMWARE} baseline`, to: `/admin/servers/${s.id}/bmc`, link: 'BMC' })
    }
    if (!s.bmc_prepared_at && s.state !== 'retired') {
      items.push({ icon: <Wrench style={{ color: 'var(--warn)' }} />, text: `${s.serial}: BMC never prepared, so IPMI over LAN, SOL and KVM may still be off`, to: `/admin/servers/${s.id}/bmc`, link: 'Prepare' })
    }
    const drives = (s.drives as Array<Record<string, unknown>>).filter((d) => d.failure_predicted)
    if (drives.length) {
      items.push({ icon: <HardDrive style={{ color: 'var(--crit)' }} />, text: `${s.serial}: ${drives.length} drive${drives.length > 1 ? 's' : ''} predicting failure`, to: `/admin/servers/${s.id}/hardware`, link: 'Drives' })
    }
  }
  const dayAgo = Date.now() - 86400_000
  for (const j of jobs) {
    if (j.state === 'failed' && new Date(j.finished_at ?? j.created_at).getTime() > dayAgo) {
      const serial = servers.find((s) => s.id === j.server_id)?.serial
      items.push({ icon: <XCircle style={{ color: 'var(--crit)' }} />, text: `${label(j.type)} failed${serial ? ` on ${serial}` : ''}: ${j.error ?? ''}`.slice(0, 160), to: `/jobs/${j.id}`, link: 'Log' })
    }
  }
  return items.slice(0, 12)
}

/**
 * Is everything the panel depends on running? The question this answers is
 * "I clicked restart and nothing happened": usually a queue with no worker.
 */
export function SystemCard() {
  const system = useAsync(() => api.system())
  usePolling(system.reload, 30000, true)
  const d = system.data
  if (!d) return null

  const problems: string[] = []
  if (d.database !== 'ok') problems.push(`database: ${d.database}`)
  if (d.redis !== 'ok') problems.push(`redis: ${d.redis}`)
  for (const [name, q] of Object.entries(d.queues)) {
    if (!q.workers.length) problems.push(`no worker on the "${name}" queue: ${q.handles} will sit queued`)
  }
  if (!d.ipmitool) problems.push('ipmitool is not installed: no IPMI power control, sensors or serial console')

  return (
    <Card
      title="System"
      actions={<Pill value={problems.length ? 'critical' : 'ok'} className="" />}
      flush
    >
      {problems.length > 0 && (
        <ul style={{ margin: 0, padding: '10px 16px 10px 32px', color: 'var(--crit)', fontSize: 13, borderBottom: '1px solid var(--border)' }}>
          {problems.map((p) => <li key={p}>{p}</li>)}
        </ul>
      )}
      <table className="compact">
        <tbody>
          {Object.entries(d.queues).map(([name, q]) => (
            <tr key={name}>
              <td className="faint">{name} queue</td>
              <td className="right">{q.workers.length ? `${q.workers.length} worker${q.workers.length > 1 ? 's' : ''}` : <span style={{ color: 'var(--crit)' }}>no worker</span>}</td>
            </tr>
          ))}
          <tr><td className="faint">BMC control</td><td className="right">{d.bmc_protocol}, cipher suite {d.ipmi_cipher_suite}</td></tr>
          <tr><td className="faint">ipmitool</td><td className="right">{d.ipmitool ? d.ipmitool.version : <span style={{ color: 'var(--crit)' }}>missing</span>}</td></tr>
          <tr><td className="faint">Secrets</td><td className="right">{d.secrets_backend}</td></tr>
        </tbody>
      </table>
    </Card>
  )
}
