import { ArrowRight, Server as ServerIcon } from 'lucide-react'
import { Link } from 'react-router-dom'
import { api, type Server } from '../../api'
import { Banner, Card, Dot, Empty, PageHeader, Pill, Stat, formatTime, label, relativeTime, useNow } from '../../components'
import { useAsync, usePolling } from '../../hooks'

const ACTIVE = ['queued', 'running']

/** Customer home: every server at a glance, and what has happened lately. */
export default function PortalDashboard() {
  const servers = useAsync(() => api.servers())
  const jobs = useAsync(() => api.jobs(12))
  const now = useNow()
  const running = (jobs.data ?? []).filter((j) => ACTIVE.includes(j.state))
  usePolling(async () => { await Promise.all([servers.reload(), jobs.reload()]) }, 10000, running.length > 0)

  const list = servers.data ?? []
  const on = list.filter((s) => s.last_power_state === 'on').length
  const attention = list.filter((s) => s.health_status === 'warning' || s.health_status === 'critical')
  const byId = new Map(list.map((s) => [s.id, s]))

  return (
    <main className="page">
      <PageHeader title="Overview" sub="Your servers, their state, and what has run on them. Everything here is read from the hardware itself." />
      {servers.error && <Banner kind="error">{servers.error}</Banner>}

      <div className="grid cols-4" style={{ marginBottom: 16 }}>
        <Stat label="Servers" value={servers.data ? list.length : '—'} sub={`${on} powered on`} to="/servers" />
        <Stat label="Needs attention" value={servers.data ? attention.length : '—'} sub={attention.length ? attention.map((s) => s.hostname ?? s.serial).join(', ') : 'all healthy'} tone={attention.some((s) => s.health_status === 'critical') ? 'crit' : attention.length ? 'warn' : 'ok'} />
        <Stat label="Running now" value={jobs.data ? running.length : '—'} sub={running.length ? running.map((j) => label(j.type)).join(', ') : 'nothing in progress'} />
        <Stat label="SSH keys" value={<Link to="/ssh-keys" style={{ fontSize: 15 }}>Manage keys <ArrowRight size={14} /></Link>} sub="installed on reinstall and rescue" />
      </div>

      {servers.data && !list.length ? (
        <Card><Empty>No servers on your account yet. When one is assigned to you it appears here.</Empty></Card>
      ) : (
        <div className="grid cols-2" style={{ marginBottom: 16 }}>
          {list.map((s) => <ServerCard key={s.id} server={s} now={now} />)}
        </div>
      )}

      <Card title="Recent activity" flush note="Reinstalls, rescue boots and power actions, newest first.">
        {!jobs.data?.length ? (
          <Empty>Nothing has run yet.</Empty>
        ) : (
          <div className="table-scroll">
            <table className="compact">
              <thead><tr><th>Action</th><th>Server</th><th>State</th><th>When</th><th className="actions" /></tr></thead>
              <tbody>
                {jobs.data.map((job) => (
                  <tr key={job.id}>
                    <td>{label(job.type)}{job.stage && ACTIVE.includes(job.state) && <div className="cell-sub">{job.stage}</div>}</td>
                    <td>{job.server_id && byId.get(job.server_id) ? <Link to={`/servers/${job.server_id}`}>{byId.get(job.server_id)!.hostname ?? byId.get(job.server_id)!.serial}</Link> : '—'}</td>
                    <td><Pill value={job.state} /></td>
                    <td className="subtle" title={formatTime(job.created_at)}>{relativeTime(job.created_at, now)}</td>
                    <td className="actions"><Link to={`/jobs/${job.id}`}>Details</Link></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </main>
  )
}

function ServerCard({ server: s, now }: { server: Server; now: number }) {
  const hardware = [
    s.cpu_count && s.cpu_model ? `${s.cpu_count}× ${s.cpu_model}` : s.model,
    s.ram_gb ? `${s.ram_gb} GB` : null,
  ].filter(Boolean).join(' · ')
  return (
    <Card
      title={<span className="row" style={{ gap: 8 }}><ServerIcon size={16} />{s.hostname ?? s.serial}</span>}
      actions={<Link className="button sm primary" to={`/servers/${s.id}`}>Manage</Link>}
    >
      <div className="stack" style={{ gap: 8 }}>
        <div className="subtle small">{hardware}</div>
        <div className="row" style={{ gap: 14, flexWrap: 'wrap' }}>
          <span className="status-line"><Dot state={s.last_power_state ?? 'unknown'} /><span style={{ textTransform: 'capitalize' }}>{s.last_power_state ?? 'unknown'}</span></span>
          <Pill value={s.state} />
          <span className="row" style={{ gap: 6 }}><Pill value={s.health_status} /><span className="subtle small">{relativeTime(s.health_checked_at, now)}</span></span>
        </div>
        <div className="mono small">{s.primary_ip ?? <span className="subtle">no address yet</span>}{s.datacenter ? <span className="subtle"> · {s.datacenter}</span> : null}</div>
      </div>
    </Card>
  )
}
