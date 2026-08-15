import { useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { api } from '../api'
import { Banner, Empty, Pill, Progress, formatTime } from '../components'
import { useAsync, usePolling } from '../hooks'

const ACTIVE = ['queued', 'running']

export default function JobDetail({ isAdmin }: { isAdmin: boolean }) {
  const { id = '' } = useParams()
  const [showRaw, setShowRaw] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // Admins get the full exchange, including raw BMC requests and responses.
  // That view is the whole point of the job log at 2am.
  const job = useAsync(() => (isAdmin ? api.adminJob(id) : api.job(id)), [id, isAdmin])
  usePolling(job.reload, 3000, ACTIVE.includes(job.data?.state ?? ''))

  if (job.error) return <main className="page"><Banner kind="error">{job.error}</Banner></main>
  if (!job.data) return <main className="page"><Empty>Loading…</Empty></main>

  const j = job.data

  async function cancel() {
    try {
      await api.cancelJob(id)
      await job.reload()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }

  return (
    <main className="page">
      <div className="spread">
        <div>
          <h1>{j.type.replace(/_/g, ' ')}</h1>
          <p className="subtle mono">{j.id}</p>
        </div>
        <div className="row">
          <Pill value={j.state} />
          {ACTIVE.includes(j.state) && <button onClick={cancel}>Cancel</button>}
        </div>
      </div>

      {error && <Banner kind="error">{error}</Banner>}
      {j.error && <Banner kind="error">{j.error}</Banner>}

      <div className="card">
        <div className="spread" style={{ marginBottom: 10 }}>
          <span>{j.stage ?? '—'}</span>
          <span className="subtle">{j.progress}%</span>
        </div>
        <Progress value={j.progress} />
        <div className="row subtle" style={{ marginTop: 12, fontSize: 13 }}>
          <span>Queued {formatTime(j.created_at)}</span>
          <span>Started {formatTime(j.started_at)}</span>
          <span>Finished {formatTime(j.finished_at)}</span>
          {j.server_id && <Link to={`/servers/${j.server_id}`}>Back to server</Link>}
        </div>
      </div>

      <div className="spread">
        <h2>Log</h2>
        {isAdmin && (
          <label className="row subtle" style={{ fontSize: 13 }}>
            <input
              type="checkbox"
              style={{ width: 'auto' }}
              checked={showRaw}
              onChange={(e) => setShowRaw(e.target.checked)}
            />
            Show raw BMC exchange
          </label>
        )}
      </div>

      <div className="log">
        {j.log.length === 0 && <span className="subtle">No entries yet.</span>}
        {j.log.map((entry) => (
          <div key={entry.sequence}>
            <span className="ts">{new Date(entry.timestamp).toLocaleTimeString()} </span>
            <span className={entry.level}>{entry.message}</span>
            {showRaw && (entry.request || entry.response) && (
              <div style={{ paddingLeft: 20, opacity: 0.75 }}>
                {entry.request && `→ ${JSON.stringify(entry.request)}`}
                {entry.request && entry.response && '\n'}
                {entry.response && `← ${JSON.stringify(entry.response)}`}
              </div>
            )}
          </div>
        ))}
      </div>

      {Object.keys(j.result ?? {}).length > 0 && (
        <>
          <h2>Result</h2>
          <div className="card">
            <pre className="mono" style={{ margin: 0, whiteSpace: 'pre-wrap' }}>
              {JSON.stringify(j.result, null, 2)}
            </pre>
          </div>
        </>
      )}
    </main>
  )
}
