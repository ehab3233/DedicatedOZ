import { useState } from 'react'
import { api } from '../api'
import { Banner, Card, Empty, PageHeader, formatTime, useConfirm } from '../components'
import { useAsync } from '../hooks'
import { useToast } from '../toast'

export default function SSHKeys() {
  const keys = useAsync(() => api.sshKeys())
  const toast = useToast()
  const confirm = useConfirm()
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
      toast.ok('Key added')
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  async function remove(id: string, keyName: string) {
    if (!(await confirm({ title: `Remove ${keyName}?`, body: 'Servers that already have this key keep it until they are reinstalled.', confirmLabel: 'Remove', danger: true }))) return
    await toast.run(async () => { await api.deleteSshKey(id); await keys.reload() }, 'Key removed')
  }

  return (
    <main className="page">
      <PageHeader title="SSH keys" sub="Keys are written to a server during install or rescue boot. Removing one here does not remove it from a server that is already running." />

      <div className="grid" style={{ gridTemplateColumns: 'minmax(280px, 1fr) minmax(0, 2fr)' }}>
        <Card title="Add a key">
          <form onSubmit={add}>
            {error && <Banner kind="error">{error}</Banner>}
            <div className="field"><label htmlFor="key-name">Name</label><input id="key-name" value={name} onChange={(e) => setName(e.target.value)} placeholder="laptop" required /></div>
            <div className="field"><label htmlFor="key-body">Public key</label><textarea id="key-body" value={publicKey} onChange={(e) => setPublicKey(e.target.value)} placeholder="ssh-ed25519 AAAA… you@host" required /></div>
            <button className="primary" type="submit" disabled={busy}>{busy ? 'Adding…' : 'Add key'}</button>
          </form>
        </Card>

        <Card flush>
          {!keys.data?.length ? (
            <Empty>No keys yet.</Empty>
          ) : (
            <table>
              <thead><tr><th>Name</th><th>Fingerprint</th><th>Added</th><th className="actions" /></tr></thead>
              <tbody>
                {keys.data.map((key) => (
                  <tr key={key.id}>
                    <td>{key.name}</td>
                    <td className="mono subtle">{key.fingerprint}</td>
                    <td className="subtle">{formatTime(key.created_at)}</td>
                    <td className="actions"><button className="sm danger" onClick={() => remove(key.id, key.name)}>Remove</button></td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Card>
      </div>
    </main>
  )
}
