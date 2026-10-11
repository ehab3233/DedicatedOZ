import { useState } from 'react'
import { api } from '../../../api'
import { Card, Empty, Pill } from '../../../components'
import { useToast } from '../../../toast'
import { usePortalServer } from './ServerPage'

export default function NetworkTab() {
  const { server: s, refresh } = usePortalServer()
  const toast = useToast()
  const [editing, setEditing] = useState<string | null>(null)
  const [value, setValue] = useState('')
  const nics = s.nics as Array<{ name?: string; speed_mbps?: number | null; link?: string | null }>

  async function save(assignmentId: string) {
    await toast.run(async () => {
      await api.setRdns(s.id, assignmentId, value.trim() || null)
      setEditing(null)
      await refresh()
    }, 'Reverse DNS saved')
  }

  return (
    <>
      <Card title="Addresses" flush note="Reverse DNS is yours to set; it is what the address resolves back to. Forward DNS lives with your domain's registrar.">
        {!s.ip_addresses.length ? (
          <Empty>No addresses assigned yet.</Empty>
        ) : (
          <div className="table-scroll">
            <table>
              <thead><tr><th>Address</th><th>Gateway</th><th>Reverse DNS</th><th className="actions" /></tr></thead>
              <tbody>
                {s.ip_addresses.map((ip) => (
                  <tr key={ip.id}>
                    <td className="mono">{ip.address}/{ip.prefix_len}{ip.is_primary && <span className="pill info" style={{ marginLeft: 8 }}>primary</span>}</td>
                    <td className="mono subtle">{ip.gateway ?? '—'}</td>
                    <td>
                      {editing === ip.id ? (
                        <input value={value} onChange={(e) => setValue(e.target.value)} placeholder="host.example.com" autoFocus onKeyDown={(e) => { if (e.key === 'Enter') void save(ip.id); if (e.key === 'Escape') setEditing(null) }} />
                      ) : (
                        <span className="mono subtle">{ip.rdns ?? '—'}</span>
                      )}
                    </td>
                    <td className="actions">
                      {editing === ip.id ? (
                        <div className="row" style={{ justifyContent: 'flex-end' }}>
                          <button className="sm primary" onClick={() => save(ip.id)}>Save</button>
                          <button className="sm" onClick={() => setEditing(null)}>Cancel</button>
                        </div>
                      ) : (
                        <button className="sm" onClick={() => { setEditing(ip.id); setValue(ip.rdns ?? '') }}>Edit rDNS</button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      <Card title="Network ports" flush note="As the hardware reports them. The first port carries your address.">
        {!nics.length ? (
          <Empty>No port information recorded yet.</Empty>
        ) : (
          <table className="compact">
            <thead><tr><th>Port</th><th>Speed</th><th>Link</th></tr></thead>
            <tbody>
              {nics.map((n, i) => (
                <tr key={i}>
                  <td className="mono">{n.name ?? `port ${i + 1}`}</td>
                  <td className="subtle">{n.speed_mbps ? `${n.speed_mbps >= 1000 ? `${n.speed_mbps / 1000} Gbit/s` : `${n.speed_mbps} Mbit/s`}` : '—'}</td>
                  <td><Pill value={n.link ? (String(n.link).toLowerCase() === 'linkup' || String(n.link).toLowerCase() === 'up' ? 'ok' : String(n.link).toLowerCase()) : 'unknown'} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>
    </>
  )
}
