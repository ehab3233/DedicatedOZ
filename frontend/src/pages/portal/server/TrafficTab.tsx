import { useState } from 'react'
import { api } from '../../../api'
import { Card, formatBits, formatBytes } from '../../../components'
import { useAsync } from '../../../hooks'
import { usePortalServer } from './ServerPage'

const PERIODS: Array<[string, string]> = [['24h', 'Last 24 hours'], ['7d', 'Last 7 days'], ['30d', 'Last 30 days']]

export default function TrafficTab() {
  const { server: s } = usePortalServer()
  const [period, setPeriod] = useState('24h')
  const bandwidth = useAsync(() => api.bandwidth(s.id, period), [s.id, period])
  const d = bandwidth.data
  const total = d ? d.total_rx_bytes + d.total_tx_bytes : 0
  const quotaBytes = s.plan?.bandwidth_quota_tb ? s.plan.bandwidth_quota_tb * 1e12 : null
  const used = quotaBytes && period === '30d' ? Math.min(100, Math.round((total / quotaBytes) * 100)) : null

  return (
    <>
      <Card
        title="Traffic"
        note="Measured at the switch port every five minutes: in is towards your server, out is from it."
        actions={
          <span className="row" style={{ gap: 4 }}>
            {PERIODS.map(([value, text]) => <button key={value} className={`sm ${period === value ? 'primary' : ''}`} onClick={() => setPeriod(value)}>{text}</button>)}
          </span>
        }
      >
        {d?.points.length ? (
          <>
            <BandwidthChart series={d.points} />
            <div className="readings" style={{ marginTop: 14 }}>
              <div className="reading"><div className="reading-label">In</div><div className="reading-value">{formatBytes(d.total_rx_bytes)}</div></div>
              <div className="reading"><div className="reading-label">Out</div><div className="reading-value">{formatBytes(d.total_tx_bytes)}</div></div>
              <div className="reading"><div className="reading-label">Total</div><div className="reading-value">{formatBytes(total)}</div></div>
            </div>
          </>
        ) : (
          <div className="subtle">{bandwidth.error ?? 'No samples in this period yet.'}</div>
        )}
      </Card>

      {s.plan && (
        <Card title="Included traffic">
          {quotaBytes ? (
            <>
              <div className="spread"><span>{s.plan.bandwidth_quota_tb} TB a month included</span><span className="subtle">{period === '30d' && d ? `${formatBytes(total)} used in the last 30 days` : 'switch to the last 30 days to see usage against it'}</span></div>
              {used != null && <div className="progress" style={{ marginTop: 10 }}><div style={{ width: `${used}%`, background: used >= 90 ? 'var(--crit)' : used >= 70 ? 'var(--warn)' : undefined }} /></div>}
            </>
          ) : (
            <div className="subtle">Your plan is unmetered.</div>
          )}
        </Card>
      )}
    </>
  )
}

/** Inline SVG area chart. A charting library would be more code than this. */
function BandwidthChart({ series }: { series: Array<{ timestamp: string; rx_bps: number; tx_bps: number }> }) {
  const width = 640
  const height = 160
  const peak = Math.max(...series.flatMap((p) => [p.rx_bps, p.tx_bps]), 1)
  const path = (key: 'rx_bps' | 'tx_bps') =>
    series
      .map((point, i) => {
        const x = (i / Math.max(series.length - 1, 1)) * width
        const y = height - (point[key] / peak) * (height - 10)
        return `${i === 0 ? 'M' : 'L'}${x.toFixed(1)},${y.toFixed(1)}`
      })
      .join(' ')
  return (
    <div className="table-scroll">
      <svg viewBox={`0 0 ${width} ${height}`} width="100%" height={height} role="img" aria-label={`Traffic, peak ${formatBits(peak)}`}>
        <path d={`${path('rx_bps')} L${width},${height} L0,${height} Z`} fill="var(--accent)" opacity="0.12" />
        <path d={path('rx_bps')} fill="none" stroke="var(--accent)" strokeWidth="1.5" />
        <path d={path('tx_bps')} fill="none" stroke="var(--warn)" strokeWidth="1.5" />
      </svg>
      <div className="row subtle" style={{ fontSize: 12, gap: 10 }}>
        <span><span style={{ color: 'var(--accent)' }}>■</span> in</span>
        <span><span style={{ color: 'var(--warn)' }}>■</span> out</span>
        <span>peak {formatBits(peak)}</span>
      </div>
    </div>
  )
}
