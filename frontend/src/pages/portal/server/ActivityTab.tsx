import { Link } from 'react-router-dom'
import { Card, Empty, Pill, formatTime, label, relativeTime, useNow } from '../../../components'
import { usePortalServer } from './ServerPage'

export default function ActivityTab() {
  const { jobs } = usePortalServer()
  const now = useNow()
  return (
    <Card title="Activity" flush note="Everything that has been done to this server, newest first. Each entry has its own log.">
      {!jobs.length ? (
        <Empty>Nothing has run on this server yet.</Empty>
      ) : (
        <div className="table-scroll">
          <table>
            <thead><tr><th>Action</th><th>State</th><th>Started</th><th>Finished</th><th className="actions" /></tr></thead>
            <tbody>
              {jobs.map((job) => (
                <tr key={job.id}>
                  <td>{label(job.type)}{job.stage && <div className="cell-sub">{job.error ?? job.stage}</div>}</td>
                  <td><Pill value={job.state} /></td>
                  <td className="subtle" title={formatTime(job.started_at)}>{job.started_at ? relativeTime(job.started_at, now) : '—'}</td>
                  <td className="subtle" title={formatTime(job.finished_at)}>{job.finished_at ? relativeTime(job.finished_at, now) : '—'}</td>
                  <td className="actions"><Link to={`/jobs/${job.id}`}>Details</Link></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  )
}
