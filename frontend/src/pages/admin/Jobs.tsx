import { useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../../api'
import { Banner, Card, Empty, PageHeader, Pill, Progress, formatTime, label, relativeTime, useNow } from '../../components'
import { useAsync, usePolling } from '../../hooks'

const STATES = ['queued', 'running', 'succeeded', 'failed', 'cancelled']

export default function Jobs() {
  const [state, setState] = useState('')
  const jobs = useAsync(() => api.adminJobs({ state: state || undefined }), [state])
  const fleet = useAsync(() => api.fleet())
  const now = useNow()
  usePolling(jobs.reload, 10000, true)

  const serial = (id: string | null) => fleet.data?.find((s) => s.id === id)?.serial

  return (
    <main className="page">
      <PageHeader title="Jobs" sub="Every action taken on every server, with its raw BMC exchange in the log." />

      <div className="toolbar">
        <div className="chips">
          <button className={`chip ${state === '' ? 'active' : ''}`} onClick={() => setState('')}>All</button>
          {STATES.map((s) => <button key={s} className={`chip ${state === s ? 'active' : ''}`} onClick={() => setState(state === s ? '' : s)}>{s}</button>)}
        </div>
        <span className="spacer" />
        <span className="faint small">refreshes every 10 s</span>
      </div>

      {jobs.error && <Banner kind="error">{jobs.error}</Banner>}

      <Card flush>
        {!jobs.data?.length ? (
          <Empty>No jobs{state ? ` in state ${state}` : ' yet'}.</Empty>
        ) : (
          <div className="table-scroll">
            <table className="compact">
              <thead><tr><th>Job</th><th>Server</th><th>State</th><th>Stage / error</th><th>Created</th><th>Finished</th><th className="actions" /></tr></thead>
              <tbody>
                {jobs.data.map((job) => (
                  <tr key={job.id}>
                    <td><Link to={`/jobs/${job.id}`}>{label(job.type)}</Link></td>
                    <td className="mono">{job.server_id ? <Link to={`/admin/servers/${job.server_id}`}>{serial(job.server_id) ?? 'server'}</Link> : <span className="faint">—</span>}</td>
                    <td><Pill value={job.state} /></td>
                    <td style={{ maxWidth: 420 }}>
                      {job.state === 'running' ? (
                        <div className="stack" style={{ gap: 4 }}><span className="subtle">{job.stage ?? 'starting…'}</span><Progress value={job.progress} active /></div>
                      ) : job.error ? (
                        <span className="small" style={{ color: 'var(--crit)' }}>{job.error.slice(0, 160)}</span>
                      ) : (
                        <span className="subtle">{job.stage ?? '—'}</span>
                      )}
                    </td>
                    <td className="subtle nowrap" title={formatTime(job.created_at)}>{relativeTime(job.created_at, now)}</td>
                    <td className="subtle nowrap">{job.finished_at ? formatTime(job.finished_at) : '—'}</td>
                    <td className="actions"><Link to={`/jobs/${job.id}`}>Log</Link></td>
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
