import { useState } from 'react'
import { Link } from 'react-router-dom'
import { api, type OSTemplate, type SSHKey } from '../../../api'
import { Banner, Modal } from '../../../components'
import { useAsync } from '../../../hooks'

export function ReinstallModal({ serverId, hostname, onClose, onQueued }: {
  serverId: string
  hostname: string | null
  onClose: () => void
  onQueued: () => void
}) {
  const templates = useAsync<OSTemplate[]>(() => api.osTemplates())
  const keys = useAsync<SSHKey[]>(() => api.sshKeys())
  const [templateId, setTemplateId] = useState('')
  const [name, setName] = useState(hostname ?? '')
  const [raid, setRaid] = useState('raid1')
  const [selectedKeys, setSelectedKeys] = useState<string[]>([])
  const [confirmText, setConfirmText] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  // Typing the word is deliberate friction: this destroys everything on the
  // array, and a misplaced click must not be enough.
  const confirmed = confirmText.trim().toUpperCase() === 'REINSTALL'

  async function submit() {
    setBusy(true)
    setError(null)
    try {
      await api.reinstall(serverId, {
        os_template_id: templateId,
        hostname: name || undefined,
        raid_level: raid,
        ssh_key_ids: selectedKeys,
        confirm_data_loss: true,
      })
      onQueued()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal title="Reinstall operating system" onClose={onClose}>
      <Banner kind="error">This erases every disk on the server. There is no undo and no backup.</Banner>
      {error && <Banner kind="error">{error}</Banner>}

      <div className="field">
        <label htmlFor="os">Operating system</label>
        <select id="os" value={templateId} onChange={(e) => setTemplateId(e.target.value)}>
          <option value="">Choose…</option>
          {(templates.data ?? []).map((t) => <option key={t.id} value={t.id}>{t.name} {t.version}</option>)}
        </select>
      </div>
      <div className="field">
        <label htmlFor="hostname">Hostname</label>
        <input id="hostname" value={name} onChange={(e) => setName(e.target.value)} />
      </div>
      <div className="field">
        <label htmlFor="raid">Disk layout</label>
        <select id="raid" value={raid} onChange={(e) => setRaid(e.target.value)}>
          <option value="raid1">RAID 1 (mirror, recommended)</option>
          <option value="raid0">RAID 0 (stripe, no redundancy)</option>
          <option value="raid5">RAID 5</option>
          <option value="raid10">RAID 10</option>
          <option value="none">No RAID (individual disks)</option>
        </select>
      </div>
      <div className="field">
        <label>SSH keys</label>
        {!keys.data?.length ? (
          <div className="subtle">No keys on your account. <Link to="/ssh-keys">Add one first</Link>; without a key you cannot log in.</div>
        ) : (
          keys.data.map((key) => (
            <label key={key.id} className="row" style={{ marginBottom: 4 }}>
              <input type="checkbox" style={{ width: 'auto' }} checked={selectedKeys.includes(key.id)} onChange={(e) => setSelectedKeys((cur) => (e.target.checked ? [...cur, key.id] : cur.filter((k) => k !== key.id)))} />
              <span>{key.name}</span>
              <span className="subtle mono" style={{ fontSize: 11 }}>{key.fingerprint}</span>
            </label>
          ))
        )}
        <div className="subtle" style={{ marginTop: 6, fontSize: 12 }}>Leave all unchecked to install every key on your account.</div>
      </div>
      <div className="field">
        <label htmlFor="confirm">Type REINSTALL to confirm</label>
        <input id="confirm" value={confirmText} onChange={(e) => setConfirmText(e.target.value)} autoComplete="off" />
      </div>
      <div className="row" style={{ justifyContent: 'flex-end' }}>
        <button onClick={onClose}>Cancel</button>
        <button className="primary" disabled={!templateId || !confirmed || busy} onClick={submit}>{busy ? 'Queueing…' : 'Reinstall'}</button>
      </div>
    </Modal>
  )
}

export function RescueModal({ serverId, onClose, onQueued }: { serverId: string; onClose: () => void; onQueued: () => void }) {
  const keys = useAsync<SSHKey[]>(() => api.sshKeys())
  const [selectedKeys, setSelectedKeys] = useState<string[]>([])
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  async function submit() {
    setBusy(true)
    setError(null)
    try {
      await api.rescue(serverId, selectedKeys)
      onQueued()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal title="Boot into rescue mode" onClose={onClose}>
      <Banner kind="info">The server reboots into a Linux system running entirely in memory. Your disks are left untouched and can be mounted by hand. Reboot from the portal to return to the installed system.</Banner>
      {error && <Banner kind="error">{error}</Banner>}
      <div className="field">
        <label>SSH keys to authorise</label>
        {!keys.data?.length ? (
          <div className="subtle">You need at least one <Link to="/ssh-keys">SSH key</Link> to log into rescue mode.</div>
        ) : (
          keys.data.map((key) => (
            <label key={key.id} className="row" style={{ marginBottom: 4 }}>
              <input type="checkbox" style={{ width: 'auto' }} checked={selectedKeys.includes(key.id)} onChange={(e) => setSelectedKeys((cur) => (e.target.checked ? [...cur, key.id] : cur.filter((k) => k !== key.id)))} />
              <span>{key.name}</span>
            </label>
          ))
        )}
        <div className="subtle" style={{ marginTop: 6, fontSize: 12 }}>Leave all unchecked to authorise every key on your account.</div>
      </div>
      <div className="row" style={{ justifyContent: 'flex-end' }}>
        <button onClick={onClose}>Cancel</button>
        <button className="primary" disabled={busy || !keys.data?.length} onClick={submit}>{busy ? 'Queueing…' : 'Boot rescue'}</button>
      </div>
    </Modal>
  )
}
