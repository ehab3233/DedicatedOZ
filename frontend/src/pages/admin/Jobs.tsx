import { useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../../api'
import { Banner, Empty, Pill, formatTime } from '../../components'
import { useAsync, usePolling } from '../../hooks'

export default function Jobs() {
  const [state, setState] = useState('')
  const jobs = useAsync(() => api.adminJobs({ state: state || undefined }), [state])
  usePolling(jobs.reload, 10000, true)

  return (
    <main className="page">
      <div className="spread">
        <div>
          <h1>Jobs</h1>
          <p className="subtle">Every action taken on every server, with its raw BMC exchange.</p>
        </div>
        <select value={state} onChange={(e) => setState(e.target.value)} style={{ width: 'auto' }}>
          <option value="">All states</option>
          <option value="queued">Queued</option>
          <option value="running">Running</option>
          <option value="succeeded">Succeeded</option>
          <option value="failed">Failed</option>
          <option value="cancelled">Cancelled</option>
        </select>
      </div>

      {jobs.error && <Banner kind="error">{jobs.error}</Banner>}

      <div className="card table-scroll" style={{ padding: 0 }}>
        {!jobs.data?.length ? (
          <Empty>No jobs{state ? ` in state ${state}` : ' yet'}.</Empty>
        ) : (
          <table>
            <thead>
              <tr>
                <th>Job</th>
                <th>State</th>
                <th>Stage</th>
                <th>Server</th>
                <th>Created</th>
                <th>Finished</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {jobs.data.map((job) => (
                <tr key={job.id}>
                  <td>{job.type.replace(/_/g, ' ')}</td>
                  <td><Pill value={job.state} /></td>
                  <td className="subtle">
                    {job.stage ?? '—'}
                    {job.error && <div style={{ color: 'var(--crit)', fontSize: 12 }}>{job.error.slice(0, 120)}</div>}
                  </td>
                  <td>{job.server_id ? <Link to={`/admin/servers/${job.server_id}`}>server</Link> : '—'}</td>
                  <td className="subtle">{formatTime(job.created_at)}</td>
                  <td className="subtle">{formatTime(job.finished_at)}</td>
                  <td><Link to={`/jobs/${job.id}`}>Raw log</Link></td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </main>
  )
}
