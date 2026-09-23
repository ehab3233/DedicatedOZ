import { Search } from 'lucide-react'
import { useState } from 'react'
import { api } from '../../api'
import { Banner, Card, Empty, PageHeader, formatTime, useDebounced } from '../../components'
import { useAsync } from '../../hooks'

export default function Audit() {
  const [action, setAction] = useState('')
  const query = useDebounced(action, 250)
  const entries = useAsync(() => api.audit({ action: query || undefined, limit: 200 }), [query])

  return (
    <main className="page">
      <PageHeader
        title="Audit trail"
        sub="Who did what, from where, when. Every power action, console session, credential change and lifecycle move."
        actions={
          <div className="search" style={{ width: 300 }}>
            <Search />
            <input placeholder="Filter by action, e.g. server.power" value={action} onChange={(e) => setAction(e.target.value)} className="mono" />
          </div>
        }
      />

      {entries.error && <Banner kind="error">{entries.error}</Banner>}

      <Card flush>
        {!entries.data?.length ? (
          <Empty>Nothing recorded{action ? ' for that action' : ' yet'}.</Empty>
        ) : (
          <div className="table-scroll">
            <table className="compact">
              <thead><tr><th>When</th><th>Actor</th><th>Action</th><th>Target</th><th>From</th><th>Detail</th></tr></thead>
              <tbody>
                {entries.data.map((e, i) => (
                  <tr key={i}>
                    <td className="subtle nowrap">{formatTime(e.timestamp)}</td>
                    <td>
                      <span className="pill">{e.actor_type}</span>
                      <div className="cell-sub">{e.actor_label ?? ''}</div>
                    </td>
                    <td className="mono">{e.action}</td>
                    <td className="mono faint small">{e.target_type ?? ''} {e.target_id ? e.target_id.slice(0, 18) : ''}</td>
                    <td className="mono subtle">{e.source_ip ?? '—'}</td>
                    <td className="mono faint small" style={{ maxWidth: 380, wordBreak: 'break-all' }}>{Object.keys(e.detail).length ? JSON.stringify(e.detail) : ''}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </main>
  )
}
