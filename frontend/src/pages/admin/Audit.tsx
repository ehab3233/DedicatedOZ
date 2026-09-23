import { useState } from 'react'
import { api } from '../../api'
import { Banner, Empty, formatTime } from '../../components'
import { useAsync } from '../../hooks'

export default function Audit() {
  const [action, setAction] = useState('')
  const entries = useAsync(() => api.audit({ action: action || undefined, limit: 200 }), [action])

  return (
    <main className="page">
      <div className="spread">
        <div>
          <h1>Audit trail</h1>
          <p className="subtle">Who did what, from where, when.</p>
        </div>
        <input
          placeholder="Filter by action, e.g. server.power.off"
          value={action}
          onChange={(e) => setAction(e.target.value)}
          style={{ maxWidth: 320 }}
          className="mono"
        />
      </div>

      {entries.error && <Banner kind="error">{entries.error}</Banner>}

      <div className="card table-scroll" style={{ padding: 0 }}>
        {!entries.data?.length ? (
          <Empty>Nothing recorded{action ? ' for that action' : ' yet'}.</Empty>
        ) : (
          <table>
            <thead>
              <tr>
                <th>When</th>
                <th>Actor</th>
                <th>Action</th>
                <th>Target</th>
                <th>From</th>
                <th>Detail</th>
              </tr>
            </thead>
            <tbody>
              {entries.data.map((e, i) => (
                <tr key={i}>
                  <td className="subtle" style={{ whiteSpace: 'nowrap' }}>{formatTime(e.timestamp)}</td>
                  <td>
                    <span className="pill">{e.actor_type}</span>
                    <div className="subtle" style={{ fontSize: 12 }}>{e.actor_label ?? ''}</div>
                  </td>
                  <td className="mono">{e.action}</td>
                  <td className="mono subtle" style={{ fontSize: 12 }}>
                    {e.target_type ?? ''} {e.target_id ? e.target_id.slice(0, 18) : ''}
                  </td>
                  <td className="mono subtle">{e.source_ip ?? '—'}</td>
                  <td className="mono subtle" style={{ fontSize: 12, maxWidth: 360, wordBreak: 'break-all' }}>
                    {Object.keys(e.detail).length ? JSON.stringify(e.detail) : ''}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </main>
  )
}
