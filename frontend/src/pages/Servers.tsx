import { Link } from 'react-router-dom'
import { api } from '../api'
import { Banner, Card, Dot, Empty, PageHeader, Pill, relativeTime, useNow } from '../components'
import { useAsync, usePolling } from '../hooks'

/** Customer portal: the servers on your account. */
export default function Servers() {
  const { data, error, loading, reload } = useAsync(() => api.servers())
  const now = useNow()

  // A server in a transitional state is mid-job; refresh so the list reflects
  // it without the customer reaching for F5.
  const transitional = (data ?? []).some((s) => ['provisioning', 'wiping', 'rescue'].includes(s.state))
  usePolling(reload, 10000, transitional)

  return (
    <main className="page">
      <PageHeader title="Your servers" sub="Power, reinstall, rescue, console and network for each server. Every action is queued as a job you can follow." />

      {error && <Banner kind="error">{error}</Banner>}

      <Card flush>
        {loading && !data ? (
          <Empty>Loading…</Empty>
        ) : !data?.length ? (
          <Empty>No servers on your account yet.</Empty>
        ) : (
          <div className="table-scroll">
            <table>
              <thead><tr><th>Server</th><th>Hardware</th><th>Address</th><th>State</th><th>Power</th><th>Health</th><th className="actions" /></tr></thead>
              <tbody>
                {data.map((server) => (
                  <tr key={server.id}>
                    <td>
                      <Link to={`/servers/${server.id}`}><strong>{server.hostname ?? server.serial}</strong></Link>
                      <div className="cell-sub mono">{server.serial}{server.datacenter ? ` · ${server.datacenter}` : ''}</div>
                    </td>
                    <td className="subtle">
                      {server.cpu_count && server.cpu_model ? `${server.cpu_count}× ${server.cpu_model}` : server.model}
                      {server.ram_gb ? ` · ${server.ram_gb} GB` : ''}
                    </td>
                    <td className="mono">{server.primary_ip ?? <span className="subtle">—</span>}</td>
                    <td><Pill value={server.state} /></td>
                    <td><span className="status-line"><Dot state={server.last_power_state ?? 'unknown'} /><span style={{ textTransform: 'capitalize' }}>{server.last_power_state ?? 'unknown'}</span></span></td>
                    <td>
                      <Pill value={server.health_status} />
                      <div className="cell-sub">{relativeTime(server.health_checked_at, now)}</div>
                    </td>
                    <td className="actions"><Link className="button sm" to={`/servers/${server.id}`}>Manage</Link></td>
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
