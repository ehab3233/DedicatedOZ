import { Link } from 'react-router-dom'
import { api, type CustomerSensorReport } from '../../../api'
import { Card, Empty, KV, Pill, formatTime, label, relativeTime, useNow } from '../../../components'
import { useAsync, usePolling } from '../../../hooks'
import { usePortalServer } from './ServerPage'

const POWER_SOURCE: Record<string, string> = { dcmi: 'BMC', redfish: 'BMC', psu_output: 'PSU output', psu_input: 'PSU input', sensors: 'sensors' }

export default function Overview() {
  const { server: s } = usePortalServer()
  const now = useNow()
  const sensors = useAsync<CustomerSensorReport>(() => api.serverSensors(s.id), [s.id])
  usePolling(sensors.reload, 20000, true)
  const report = sensors.data
  const readings = report?.sensors ?? []
  const temps = readings.filter((r) => r.kind === 'temperature' && r.value != null)
  const fans = readings.filter((r) => r.kind === 'fan' && r.value != null)
  const hottest = temps.slice().sort((a, b) => (b.value ?? 0) - (a.value ?? 0))[0]
  const inlet = temps.find((t) => /inlet|ambient|front/i.test(t.name))
  const fanSpeeds = fans.map((f) => Math.round(f.value ?? 0))
  const u = report?.utilization
  const drives = s.drives as Array<Record<string, string | number | boolean | null>>
  const volumes = s.volumes as Array<Record<string, string | number | null>>
  const primary = s.ip_addresses.find((ip) => ip.is_primary) ?? s.ip_addresses[0]
  const subsystems = Object.entries(s.health?.subsystems ?? {}) as Array<[string, { status?: string }]>

  return (
    <div className="grid" style={{ gridTemplateColumns: 'minmax(0, 3fr) minmax(280px, 2fr)' }}>
      <div className="stack" style={{ gap: 16 }}>
        <Card
          title="Live readings"
          note={report?.checked_at ? `Read ${relativeTime(report.checked_at, now)} by the platform, which re-reads every minute.` : 'The platform reads the hardware every minute; the first reading appears shortly.'}
        >
          {!readings.length ? (
            <div className="subtle">{sensors.error ? sensors.error : 'No readings yet.'}</div>
          ) : (
            <div className="readings">
              {hottest && <Reading label={`Hottest · ${hottest.name}`} status={hottest.status} value={Math.round(hottest.value ?? 0)} unit={hottest.unit ?? '°C'} />}
              {inlet && inlet !== hottest && <Reading label={inlet.name} status={inlet.status} value={Math.round(inlet.value ?? 0)} unit={inlet.unit ?? '°C'} />}
              {fans.length > 0 && <Reading label={`Fans (${fans.length})`} status={fans.some((f) => f.status !== 'ok') ? 'warning' : 'ok'} value={Math.min(...fanSpeeds) === Math.max(...fanSpeeds) ? Math.min(...fanSpeeds) : `${Math.min(...fanSpeeds)}–${Math.max(...fanSpeeds)}`} unit={fans[0].unit ?? 'RPM'} />}
              {report?.power && report.power.watts > 0 && <Reading label={`Power draw · ${POWER_SOURCE[report.power.source ?? ''] ?? 'BMC'}`} status="ok" value={Math.round(report.power.watts)} unit="W" />}
            </div>
          )}
          {u && (u.cpu != null || u.memory != null) && (
            <div className="stack" style={{ gap: 8, marginTop: 14 }}>
              {([['CPU', u.cpu], ['Memory', u.memory], ['IO', u.io]] as Array<[string, number | null]>).map(([name, value]) => (
                <div key={name} className="row" style={{ gap: 12, alignItems: 'center' }}>
                  <div className="small" style={{ width: 64 }}>{name}</div>
                  <div className="progress" style={{ flex: 1 }}><div style={{ width: `${value ?? 0}%` }} /></div>
                  <div className="num small" style={{ width: 44, textAlign: 'right' }}>{value == null ? '—' : `${value}%`}</div>
                </div>
              ))}
              <div className="faint small">Utilisation as the hardware measures it, independent of the operating system.</div>
            </div>
          )}
        </Card>

        <Card title="Hardware">
          <KV wide items={[
            ['Processor', s.cpu_count && s.cpu_model ? `${s.cpu_count}× ${s.cpu_model}${s.cpu_cores_total ? ` (${s.cpu_cores_total} threads)` : ''}` : s.model],
            ['Memory', s.ram_gb ? `${s.ram_gb} GB` : null],
            ['Drives', drives.length ? drives.map((d) => `${d.capacity_gb ? `${d.capacity_gb} GB` : ''} ${d.media ?? ''}`.trim()).join(' · ') : null],
            ['Array', volumes.length ? volumes.map((v) => `${String(v.raid_type ?? '').replace(/^RAID(\d)/i, 'RAID $1')}${v.capacity_gb ? ` · ${v.capacity_gb} GB` : ''}`).join(' · ') : 'none built'],
            ['Model', s.model],
          ]} />
        </Card>
      </div>

      <div className="stack" style={{ gap: 16 }}>
        <Card title="Address" actions={<Link className="button sm" to={`/servers/${s.id}/network`}>Network</Link>}>
          {primary ? (
            <KV items={[
              ['Primary', <span className="mono">{primary.address}/{primary.prefix_len}</span>],
              ['Gateway', primary.gateway ? <span className="mono">{primary.gateway}</span> : null],
              ['Reverse DNS', primary.rdns ? <span className="mono">{primary.rdns}</span> : <span className="subtle">not set</span>],
              ['Addresses', s.ip_addresses.length],
            ]} />
          ) : <Empty>No address assigned yet.</Empty>}
        </Card>

        <Card title="Plan">
          {s.plan ? (
            <KV items={[
              ['Plan', s.plan.plan_name],
              ['Price', s.plan.monthly_price != null ? `${s.plan.monthly_price.toFixed(2)} ${s.plan.currency} / month` : null],
              ['Traffic included', s.plan.bandwidth_quota_tb ? `${s.plan.bandwidth_quota_tb} TB / month` : 'unmetered'],
              ['Since', formatTime(s.plan.started_at)],
            ]} />
          ) : <div className="subtle">No plan recorded.</div>}
        </Card>

        <Card title="Health" actions={<Pill value={s.health_status} />} note={s.health_checked_at ? `Checked ${relativeTime(s.health_checked_at, now)}` : undefined}>
          {!subsystems.length ? (
            <div className="subtle">No health data collected yet.</div>
          ) : (
            <KV items={subsystems.map(([name, info]) => [label(name), <Pill value={info?.status} />] as [string, React.ReactNode])} />
          )}
        </Card>
      </div>
    </div>
  )
}

function Reading({ label: text, status, value, unit }: { label: string; status: string; value: number | string; unit: string }) {
  return (
    <div className={`reading ${status === 'ok' ? '' : status}`}>
      <div className="reading-label">{text}</div>
      <div className="reading-value">{value}<span className="sensor-unit">{unit}</span></div>
    </div>
  )
}
