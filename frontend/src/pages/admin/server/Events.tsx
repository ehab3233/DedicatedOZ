import { AlertTriangle, Info, RefreshCw, Trash2, XCircle } from 'lucide-react'
import { useCallback, useEffect, useState } from 'react'
import { api, type SelReport } from '../../../api'
import { Banner, Card, Empty, Spinner, formatTime, relativeTime, useConfirm, useNow } from '../../../components'
import { useToast } from '../../../toast'
import { useServer } from './ServerPage'

export default function Events() {
  const { server } = useServer()
  const toast = useToast()
  const confirm = useConfirm()
  const now = useNow()
  const [report, setReport] = useState<SelReport | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)

  const read = useCallback(async (fresh = false) => {
    setLoading(true)
    try {
      setReport(await api.eventLog(server.id, fresh))
      setError(null)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }, [server.id])

  useEffect(() => {
    void read()
    const timer = window.setInterval(() => {
      if (document.visibilityState === 'visible') void read()
    }, 30000)
    return () => window.clearInterval(timer)
  }, [read])

  async function clear() {
    const ok = await confirm({
      title: 'Clear the System Event Log?',
      body: `${report?.info.entries ?? 0} entries on the BMC are erased. Do this after the cause has been dealt with, so the next fault is easy to see.`,
      confirmLabel: 'Clear log',
      danger: true,
    })
    if (!ok) return
    const result = await toast.run(() => api.clearEventLog(server.id))
    if (result) {
      toast.ok(`Cleared ${result.entries_removed} entries`)
      await read(true)
    }
  }

  const info = report?.info

  return (
    <>
      <div className="toolbar">
        <span className="subtle small">
          {info ? (
            <>
              {info.entries} entries{info.percent_used != null && ` · ${info.percent_used}% of the log used`}
              {info.overflow && <strong style={{ color: 'var(--crit)' }}> · overflowed: older events were lost</strong>}
              {report && ` · read ${relativeTime(report.checked_at, now)} · refreshes every 30 s`}
            </>
          ) : loading ? 'Reading the event log…' : ''}
        </span>
        <span className="spacer" />
        <button className="sm" onClick={() => read(true)} disabled={loading}>{loading ? <Spinner /> : <RefreshCw />}Refresh</button>
        <button className="sm danger" onClick={clear} disabled={!info?.entries}><Trash2 />Clear log</button>
      </div>

      {error && <Banner kind="error">Could not read the event log: {error}</Banner>}

      <Card flush>
        {!report ? (
          <Empty>{error ? 'Nothing to show.' : <><Spinner /> Reading…</>}</Empty>
        ) : report.entries.length === 0 ? (
          <Empty>The System Event Log is empty. Hardware faults, thermal events and power events land here.</Empty>
        ) : (
          <div className="table-scroll">
            <table className="compact">
              <thead>
                <tr><th style={{ width: 24 }} /><th>#</th><th>Time</th><th>Sensor</th><th>Event</th><th>Detail</th></tr>
              </thead>
              <tbody>
                {report.entries.map((e) => (
                  <tr key={e.id}>
                    <td className={`severity-${e.severity}`} title={e.severity}>
                      {e.severity === 'critical' ? <XCircle size={15} /> : e.severity === 'warning' ? <AlertTriangle size={15} /> : <Info size={15} />}
                    </td>
                    <td className="num faint">{e.id}</td>
                    <td className="nowrap subtle" title={e.raw_time}>{e.timestamp ? formatTime(e.timestamp) : <span className="faint">{e.raw_time.replace(/\s+/g, ' ')}</span>}</td>
                    <td className="mono">{e.sensor}</td>
                    <td>
                      {e.event}
                      {e.direction && <span className={`small ${e.direction.toLowerCase().startsWith('de') ? 'faint' : 'subtle'}`}> · {e.direction}</span>}
                    </td>
                    <td className="subtle small">{e.detail ?? ''}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
      <p className="faint small">A time shown as Pre-Init or a raw count means the BMC's clock is not set. Times are what the BMC recorded, in its own zone.</p>
    </>
  )
}
