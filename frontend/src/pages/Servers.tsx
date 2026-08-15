import { Link } from 'react-router-dom'
import { api } from '../api'
import { Banner, Empty, Pill, relativeTime } from '../components'
import { useAsync, usePolling } from '../hooks'

export default function Servers() {
  const { data, error, loading, reload } = useAsync(() => api.servers())

  // A server in a transitional state is mid-job; refresh so the list reflects
  // it without the customer reaching for F5.
  const transitional = (data ?? []).some((s) =>
    ['provisioning', 'wiping', 'rescue'].includes(s.state),
  )
  usePolling(reload, 10000, transitional)

  return (
    <main className="page">
      <h1>Your servers</h1>
      <p className="subtle">
        Every action here is queued as a job — nothing blocks, and everything is logged.
      </p>

      {error && <Banner kind="error">{error}</Banner>}

      <div className="card" style={{ padding: 0 }}>
        {loading && !data ? (
          <Empty>Loading…</Empty>
        ) : !data?.length ? (
          <Empty>No servers on your account yet.</Empty>
        ) : (
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>Server</th>
                  <th>Hardware</th>
                  <th>State</th>
                  <th>Power</th>
                  <th>Health</th>
                </tr>
              </thead>
              <tbody>
                {data.map((server) => (
                  <tr key={server.id}>
                    <td>
                      <Link to={`/servers/${server.id}`}>
                        <strong>{server.hostname ?? server.serial}</strong>
                      </Link>
                      <div className="subtle mono">{server.serial}</div>
                    </td>
                    <td className="subtle">
                      {server.cpu_count && server.cpu_model
                        ? `${server.cpu_count}× ${server.cpu_model}`
                        : server.model}
                      {server.ram_gb ? ` · ${server.ram_gb} GB` : ''}
                    </td>
                    <td>
                      <Pill value={server.state} />
                    </td>
                    <td className="subtle">{server.last_power_state ?? '—'}</td>
                    <td>
                      <Pill value={server.health_status} />
                      <div className="subtle" style={{ fontSize: 12 }}>
                        {relativeTime(server.health_checked_at)}
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </main>
  )
}
