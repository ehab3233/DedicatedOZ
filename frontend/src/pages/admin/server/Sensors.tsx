import { Pause, Play, RefreshCw } from 'lucide-react'
import { useCallback, useEffect, useState } from 'react'
import { api, type Sensor, type SensorReport } from '../../../api'
import { Banner, Card, Empty, Pill, Spinner, relativeTime, useNow } from '../../../components'
import { useServer } from './ServerPage'

const GROUPS: Array<{ kind: Sensor['kind'] | 'other'; title: string }> = [
  { kind: 'temperature', title: 'Temperatures' },
  { kind: 'fan', title: 'Fans' },
  { kind: 'voltage', title: 'Voltages' },
  { kind: 'power', title: 'Power' },
  { kind: 'current', title: 'Current' },
  { kind: 'other', title: 'Other sensors' },
]

/** Live sensor readings from the BMC, re-read every `intervalMs` while the tab is visible. */
export function useLiveSensors(serverId: string, intervalMs = 10000, enabled = true) {
  const [report, setReport] = useState<SensorReport | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)

  const read = useCallback(async (fresh = false) => {
    setLoading(true)
    try {
      setReport(await api.sensors(serverId, fresh))
      setError(null)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }, [serverId])

  useEffect(() => {
    void read()
    if (!enabled) return
    const timer = window.setInterval(() => {
      if (document.visibilityState === 'visible') void read()
    }, intervalMs)
    return () => window.clearInterval(timer)
  }, [read, intervalMs, enabled])

  return { report, error, loading, read }
}

export const POWER_SOURCE: Record<string, string> = {
  dcmi: 'DCMI',
  redfish: 'Redfish',
  psu_output: 'PSU output',
  psu_input: 'PSU input',
  sensors: 'power sensors',
}

/**
 * The CIMC's own utilisation figures, the ones its summary page charts. They
 * come from the Intel management engine (CUPS), not from the OS, so they are
 * there whatever is installed, and blank while the host is off.
 */
export function UtilisationCard({ report }: { report: SensorReport | null }) {
  if (!report) return null
  const u = report.utilization
  const rows: Array<[string, number | null]> = u
    ? [['Overall', u.overall], ['CPU', u.cpu], ['Memory', u.memory], ['IO', u.io]]
    : []
  return (
    <Card title="Utilisation" note="As the CIMC measures it: CPU, memory and IO from the management engine, independent of the OS. Blank while the host is off.">
      {!u ? (
        <div className="subtle small">Not reported by this BMC{report.utilization_error ? ` (${report.utilization_error})` : ''}.</div>
      ) : (
        <div className="stack" style={{ gap: 10 }}>
          {rows.map(([name, value]) => (
            <div key={name} className="row" style={{ gap: 12, alignItems: 'center' }}>
              <div className="small" style={{ width: 64 }}>{name}</div>
              <div className="progress" style={{ flex: 1 }}><div style={{ width: `${value ?? 0}%` }} /></div>
              <div className="num small" style={{ width: 44, textAlign: 'right' }}>{value == null ? '—' : `${value}%`}</div>
            </div>
          ))}
        </div>
      )}
    </Card>
  )
}

export function formatValue(s: Sensor): React.ReactNode {
  if (s.value == null) return <span className="subtle">{s.reading}</span>
  const digits = s.unit === 'V' ? 2 : 0
  return (
    <span className="sensor-value">
      {s.value.toFixed(digits)}
      <span className="sensor-unit">{s.unit}</span>
    </span>
  )
}

export default function Sensors() {
  const { server } = useServer()
  const [paused, setPaused] = useState(false)
  const { report, error, loading, read } = useLiveSensors(server.id, 10000, !paused)
  const now = useNow()

  const byKind = (kind: string) =>
    (report?.sensors ?? []).filter((s) => (kind === 'other' ? !['temperature', 'fan', 'voltage', 'power', 'current'].includes(s.kind) : s.kind === kind))

  return (
    <>
      <div className="toolbar">
        <span className="subtle small">
          {report ? (
            <>
              {report.sensors.length} sensors · read {relativeTime(report.checked_at, now)}
              {report.via === 'redfish' && ' · via Redfish, IPMI is not answering'}
              {paused ? ' · paused' : ' · refreshes every 10 s'}
              {report.stale && <span style={{ color: 'var(--warn)' }}> · the BMC missed the last poll; these are the last good readings</span>}
            </>
          ) : loading ? 'Reading sensors from the BMC…' : ''}
        </span>
        <span className="spacer" />
        <button className="sm" onClick={() => setPaused((p) => !p)}>{paused ? <Play /> : <Pause />}{paused ? 'Resume' : 'Pause'}</button>
        <button className="sm" onClick={() => read(true)} disabled={loading}>{loading ? <Spinner /> : <RefreshCw />}Read now</button>
      </div>

      {error && <Banner kind="error">Could not read sensors: {error}</Banner>}

      <UtilisationCard report={report} />

      {report?.power && (
        <Card title={`Power draw (${POWER_SOURCE[report.power.source ?? ''] ?? 'BMC'})`}>
          <div className="readings">
            <div className="reading"><div className="reading-label">Now</div><div className="reading-value">{report.power.watts}<span className="sensor-unit">W</span></div></div>
            {report.power.average != null && <div className="reading"><div className="reading-label">Average</div><div className="reading-value">{report.power.average}<span className="sensor-unit">W</span></div></div>}
            {report.power.minimum != null && <div className="reading"><div className="reading-label">Min</div><div className="reading-value">{report.power.minimum}<span className="sensor-unit">W</span></div></div>}
            {report.power.maximum != null && <div className="reading"><div className="reading-label">Max</div><div className="reading-value">{report.power.maximum}<span className="sensor-unit">W</span></div></div>}
          </div>
        </Card>
      )}

      {report && report.sensors.length === 0 && (
        <Card><Empty>The BMC reports no sensors. On a CIMC that usually means IPMI over LAN was just enabled; try again in a minute.</Empty></Card>
      )}

      <div className="grid cols-2">
        {GROUPS.map(({ kind, title }) => {
          const rows = byKind(kind)
          if (!rows.length) return null
          const worst = rows.some((r) => r.status === 'critical') ? 'critical' : rows.some((r) => r.status === 'warning') ? 'warning' : 'ok'
          return (
            <Card key={kind} title={title} actions={<Pill value={worst} />} flush className="sensor-group">
              <table className="compact">
                <thead><tr><th>Sensor</th><th className="right">Reading</th><th>State</th></tr></thead>
                <tbody>
                  {rows.map((s) => (
                    <tr key={`${s.number}-${s.name}`}>
                      <td><span className="sensor-name">{s.name}</span></td>
                      <td className="right num">{formatValue(s)}</td>
                      <td><Pill value={s.status === 'no_reading' ? 'no_reading' : s.status} /></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </Card>
          )
        })}
      </div>
      {!report && !error && <Card><Empty><Spinner /> Reading the sensor repository…</Empty></Card>}
    </>
  )
}
