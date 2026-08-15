import { useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api'
import { Banner, Empty, Pill, Stat, formatTime, relativeTime } from '../components'
import { useAsync, usePolling } from '../hooks'

type FleetRow = Record<string, string | number | boolean | null | undefined>

export default function Admin() {
  const [tab, setTab] = useState<'fleet' | 'jobs'>('fleet')
  const summary = useAsync(() => api.fleetSummary())
  const fleet = useAsync(() => api.fleet())
  const jobs = useAsync(() => api.adminJobs())

  // Only refresh the tab that is on screen; the other query is wasted work.
  usePolling(
    async () => {
      await summary.reload()
      await (tab === 'fleet' ? fleet.reload() : jobs.reload())
    },
    15000,
    true,
  )

  const byState = (summary.data?.servers_by_state ?? {}) as Record<string, number>

  return (
    <main className="page">
      <h1>Fleet</h1>
      <p className="subtle">Inventory, lifecycle and the job log.</p>

      {summary.error && <Banner kind="error">{summary.error}</Banner>}

      <div className="grid cols-4">
        <Stat label="Servers" value={String(summary.data?.total_servers ?? '—')} />
        <Stat label="Active" value={String(byState.active ?? 0)} />
        <Stat label="In stock" value={String(byState.in_stock ?? 0)} />
        <Stat label="Running jobs" value={String(summary.data?.active_jobs ?? 0)} />
        <Stat label="Failed jobs (24h)" value={String(summary.data?.failed_jobs_24h ?? 0)} />
        <Stat label="Unhealthy" value={String(summary.data?.unhealthy_servers ?? 0)} />
        <Stat label="Open abuse" value={String(summary.data?.open_abuse_reports ?? 0)} />
      </div>

      <div className="row" style={{ margin: '24px 0 12px' }}>
        <button className={tab === 'fleet' ? 'primary' : ''} onClick={() => setTab('fleet')}>
          Inventory
        </button>
        <button className={tab === 'jobs' ? 'primary' : ''} onClick={() => setTab('jobs')}>
          Jobs
        </button>
      </div>

      {tab === 'fleet' ? <FleetTable rows={(fleet.data ?? []) as FleetRow[]} /> : <JobsTable jobs={jobs.data ?? []} />}
    </main>
  )
}

function FleetTable({ rows }: { rows: FleetRow[] }) {
  if (!rows.length) {
    return (
      <div className="card">
        <Empty>No servers registered. Add one through POST /api/v1/admin/servers.</Empty>
      </div>
    )
  }

  return (
    <div className="card table-scroll" style={{ padding: 0 }}>
      <table>
        <thead>
          <tr>
            <th>Serial</th>
            <th>Location</th>
            <th>CIMC</th>
            <th>Firmware</th>
            <th>PXE MAC</th>
            <th>State</th>
            <th>Health</th>
            <th>Customer</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((server) => (
            <tr key={String(server.id)}>
              <td>
                <Link to={`/servers/${server.id}`} className="mono">
                  {String(server.serial)}
                </Link>
              </td>
              <td className="subtle">
                {[server.datacenter, server.rack, server.rack_unit && `U${server.rack_unit}`]
                  .filter(Boolean)
                  .join(' · ') || '—'}
                {server.switch_port ? (
                  <div style={{ fontSize: 12 }}>
                    {String(server.switch_name ?? '')} {String(server.switch_port)}
                  </div>
                ) : null}
              </td>
              <td className="mono subtle">{String(server.cimc_ip ?? '—')}</td>
              <td className="subtle">
                {String(server.cimc_firmware ?? '—')}
                {server.cimc_firmware && server.cimc_firmware !== '4.1(2f)' && (
                  <span className="pill warning" style={{ marginLeft: 6 }}>
                    off baseline
                  </span>
                )}
              </td>
              <td className="mono subtle">
                {server.provisioning_mac ? (
                  String(server.provisioning_mac)
                ) : (
                  <span className="pill warning">not synced</span>
                )}
              </td>
              <td>
                <Pill value={String(server.state ?? '')} />
              </td>
              <td>
                <Pill value={server.health_status ? String(server.health_status) : null} />
                <div className="subtle" style={{ fontSize: 12 }}>
                  {relativeTime(server.health_checked_at ? String(server.health_checked_at) : null)}
                </div>
              </td>
              <td className="subtle">{String(server.customer_email ?? '—')}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function JobsTable({ jobs }: { jobs: Array<{ id: string; type: string; state: string; server_id: string | null; stage: string | null; created_at: string }> }) {
  if (!jobs.length) {
    return (
      <div className="card">
        <Empty>No jobs yet.</Empty>
      </div>
    )
  }

  return (
    <div className="card table-scroll" style={{ padding: 0 }}>
      <table>
        <thead>
          <tr>
            <th>Job</th>
            <th>State</th>
            <th>Stage</th>
            <th>Created</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {jobs.map((job) => (
            <tr key={job.id}>
              <td>{job.type.replace(/_/g, ' ')}</td>
              <td>
                <Pill value={job.state} />
              </td>
              <td className="subtle">{job.stage ?? '—'}</td>
              <td className="subtle">{formatTime(job.created_at)}</td>
              <td>
                <Link to={`/jobs/${job.id}`}>Raw log</Link>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
