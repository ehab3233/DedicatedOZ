import { useState } from 'react'
import { api } from '../api'
import { Banner, Empty, formatTime } from '../components'
import { useAsync } from '../hooks'

export default function SSHKeys() {
  const keys = useAsync(() => api.sshKeys())
  const [name, setName] = useState('')
  const [publicKey, setPublicKey] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  async function add(event: React.FormEvent) {
    event.preventDefault()
    setBusy(true)
    setError(null)
    try {
      await api.addSshKey(name, publicKey)
      setName('')
      setPublicKey('')
      await keys.reload()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  async function remove(id: string) {
    try {
      await api.deleteSshKey(id)
      await keys.reload()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }

  return (
    <main className="page">
      <h1>SSH keys</h1>
      <p className="subtle">
        Keys are written to a server during install or rescue boot. Removing one here does not
        remove it from a server that is already running — reinstall for that.
      </p>

      {error && <Banner kind="error">{error}</Banner>}

      <div className="card">
        <form onSubmit={add}>
          <div className="field">
            <label htmlFor="key-name">Name</label>
            <input
              id="key-name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="laptop"
              required
            />
          </div>
          <div className="field">
            <label htmlFor="key-body">Public key</label>
            <textarea
              id="key-body"
              value={publicKey}
              onChange={(e) => setPublicKey(e.target.value)}
              placeholder="ssh-ed25519 AAAA… you@host"
              required
            />
          </div>
          <button className="primary" type="submit" disabled={busy}>
            {busy ? 'Adding…' : 'Add key'}
          </button>
        </form>
      </div>

      <div className="card" style={{ padding: 0 }}>
        {!keys.data?.length ? (
          <Empty>No keys yet.</Empty>
        ) : (
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>Name</th>
                  <th>Fingerprint</th>
                  <th>Added</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {keys.data.map((key) => (
                  <tr key={key.id}>
                    <td>{key.name}</td>
                    <td className="mono subtle">{key.fingerprint}</td>
                    <td className="subtle">{formatTime(key.created_at)}</td>
                    <td>
                      <button className="danger" onClick={() => remove(key.id)}>
                        Remove
                      </button>
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
