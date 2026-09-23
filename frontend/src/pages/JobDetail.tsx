import { useEffect, useRef, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { api } from '../api'
import { Banner, Card, Empty, KV, PageHeader, Pill, Progress, formatTime, label, useConfirm } from '../components'
import { useAsync, usePolling } from '../hooks'
import { useToast } from '../toast'

const ACTIVE = ['queued', 'running']

export default function JobDetail({ isAdmin }: { isAdmin: boolean }) {
  const { id = '' } = useParams()
  const [showRaw, setShowRaw] = useState(false)
  const [follow, setFollow] = useState(true)
  const toast = useToast()
  const confirm = useConfirm()
  const logRef = useRef<HTMLDivElement>(null)

  // Admins get the full exchange, including raw BMC requests and responses.
  // That view is the whole point of the job log at 2am.
  const job = useAsync(() => (isAdmin ? api.adminJob(id) : api.job(id)), [id, isAdmin])
  const active = ACTIVE.includes(job.data?.state ?? '')
  usePolling(job.reload, 2000, active)

  useEffect(() => {
    if (follow && logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight
  }, [job.data?.log.length, follow])

  if (job.error) return <main className="page"><Banner kind="error">{job.error}</Banner></main>
  if (!job.data) return <main className="page"><Empty>Loading…</Empty></main>

  const j = job.data
  const serverLink = j.server_id ? (isAdmin ? `/admin/servers/${j.server_id}/jobs` : `/servers/${j.server_id}`) : null
  const admin = j as unknown as { payload?: Record<string, unknown>; attempts?: number; celery_task_id?: string | null }

  async function cancel() {
    if (!(await confirm({ title: 'Cancel this job?', body: 'Cancellation is cooperative: the worker stops at the next stage boundary. A reinstall mid-write finishes the current stage first.', confirmLabel: 'Cancel job', danger: true }))) return
    await toast.run(async () => { await api.cancelJob(id); await job.reload() }, 'Cancellation requested')
  }

  return (
    <main className="page">
      <PageHeader
        crumbs={[{ label: isAdmin ? 'Jobs' : 'Servers', to: isAdmin ? '/admin/jobs' : '/servers' }, { label: label(j.type) }]}
        title={<span className="row" style={{ gap: 10 }}>{label(j.type)}<Pill value={j.state} /></span>}
        sub={<span className="mono">{j.id}</span>}
        actions={
          <>
            {serverLink && <Link className="button" to={serverLink}>Server</Link>}
            {active && <button className="danger" onClick={cancel}>Cancel job</button>}
          </>
        }
      />

      {j.error && <Banner kind="error">{j.error}</Banner>}

      <Card>
        <div className="spread" style={{ marginBottom: 8 }}>
          <span>{j.stage ?? '—'}</span>
          <span className="subtle num">{j.progress}%</span>
        </div>
        <Progress value={j.progress} active={active} />
        <div style={{ marginTop: 14 }}>
          <KV items={[
            ['Queued', formatTime(j.created_at)],
            ['Started', formatTime(j.started_at)],
            ['Finished', formatTime(j.finished_at)],
            ...(isAdmin && admin.attempts != null ? [['Attempts', String(admin.attempts)] as [React.ReactNode, React.ReactNode]] : []),
          ]} />
        </div>
      </Card>

      <Card
        title="Log"
        actions={
          <>
            {isAdmin && <label className="check" style={{ marginBottom: 0 }}><input type="checkbox" checked={showRaw} onChange={(e) => setShowRaw(e.target.checked)} /> Raw BMC exchange</label>}
            <label className="check" style={{ marginBottom: 0 }}><input type="checkbox" checked={follow} onChange={(e) => setFollow(e.target.checked)} /> Follow</label>
          </>
        }
        flush
      >
        <div className="log" ref={logRef} style={{ borderRadius: '0 0 var(--radius) var(--radius)' }}>
          {j.log.length === 0 && <span style={{ color: '#66717f' }}>No entries yet.</span>}
          {j.log.map((entry) => (
            <div key={entry.sequence}>
              <span className="ts">{new Date(entry.timestamp).toLocaleTimeString()} </span>
              <span className={entry.level}>{entry.message}</span>
              {showRaw && (entry.request || entry.response) && (
                <div className="raw">
                  {entry.request && `→ ${JSON.stringify(entry.request)}`}
                  {entry.request && entry.response && '\n'}
                  {entry.response && `← ${JSON.stringify(entry.response)}`}
                </div>
              )}
            </div>
          ))}
        </div>
      </Card>

      {isAdmin && admin.payload && Object.keys(admin.payload).length > 0 && (
        <Card title="Payload">
          <pre className="mono" style={{ margin: 0, whiteSpace: 'pre-wrap' }}>{JSON.stringify(admin.payload, null, 2)}</pre>
        </Card>
      )}

      {Object.keys(j.result ?? {}).length > 0 && (
        <Card title="Result">
          <pre className="mono" style={{ margin: 0, whiteSpace: 'pre-wrap' }}>{JSON.stringify(j.result, null, 2)}</pre>
        </Card>
      )}
    </main>
  )
}
