import { useState } from 'react'
import { Link } from 'react-router-dom'
import { Card, Empty, Pill, Progress, formatTime, label, relativeTime, useNow } from '../../../components'
import { useServer } from './ServerPage'

const STATES = ['queued', 'running', 'succeeded', 'failed', 'cancelled']

export default function JobsTab() {
  const { jobs } = useServer()
  const [state, setState] = useState('')
  const now = useNow()
  const rows = jobs.filter((j) => !state || j.state === state)

  return (
    <>
      <div className="toolbar">
        <div className="chips">
          <button className={`chip ${state === '' ? 'active' : ''}`} onClick={() => setState('')}>All</button>
          {STATES.map((s) => (
            <button key={s} className={`chip ${state === s ? 'active' : ''}`} onClick={() => setState(state === s ? '' : s)}>{s}</button>
          ))}
        </div>
        <span className="spacer" />
        <span className="faint small">{rows.length} jobs</span>
      </div>
      <Card flush>
        {!rows.length ? (
          <Empty>Nothing has run on this server{state ? ` in state ${state}` : ''}.</Empty>
        ) : (
          <div className="table-scroll">
            <table className="compact">
              <thead><tr><th>Job</th><th>State</th><th>Stage / error</th><th>Requested by</th><th>Started</th><th>Took</th><th className="actions" /></tr></thead>
              <tbody>
                {rows.map((job) => (
                  <tr key={job.id}>
                    <td><Link to={`/jobs/${job.id}`}>{label(job.type)}</Link></td>
                    <td><Pill value={job.state} /></td>
                    <td style={{ maxWidth: 420 }}>
                      {job.state === 'running' ? (
                        <div className="stack" style={{ gap: 4 }}>
                          <span className="subtle">{job.stage ?? 'starting…'}</span>
                          <Progress value={job.progress} active />
                        </div>
                      ) : job.error ? (
                        <span className="small" style={{ color: 'var(--crit)' }}>{job.error.slice(0, 200)}</span>
                      ) : (
                        <span className="subtle">{job.stage ?? '—'}</span>
                      )}
                    </td>
                    <td className="subtle">{(job as unknown as { requested_by_type?: string }).requested_by_type ?? '—'}</td>
                    <td className="subtle nowrap" title={formatTime(job.started_at ?? job.created_at)}>{relativeTime(job.started_at ?? job.created_at, now)}</td>
                    <td className="subtle num">{duration(job.started_at, job.finished_at)}</td>
                    <td className="actions"><Link to={`/jobs/${job.id}`}>Log</Link></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </>
  )
}

function duration(start: string | null, end: string | null): string {
  if (!start || !end) return '—'
  const seconds = Math.round((new Date(end).getTime() - new Date(start).getTime()) / 1000)
  if (seconds < 60) return `${seconds}s`
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${seconds % 60}s`
  return `${Math.floor(seconds / 3600)}h ${Math.floor((seconds % 3600) / 60)}m`
}
