import { Copy, KeyRound, Plus, Trash2 } from 'lucide-react'
import { useState } from 'react'
import { Link } from 'react-router-dom'
import { api, type ApiToken, type Me } from '../../api'
import { Banner, Card, Empty, KV, PageHeader, formatTime, useConfirm } from '../../components'
import { useAsync } from '../../hooks'
import { useToast } from '../../toast'

/** Who you are to us, how you sign in, and what talks to the API for you. */
export default function Account() {
  const me = useAsync<Me>(() => api.me())
  if (me.error) return <main className="page"><Banner kind="error">{me.error}</Banner></main>
  if (!me.data) return <main className="page"><Empty>Loading…</Empty></main>
  return (
    <main className="page">
      <PageHeader title="Account" sub="Contact details, sign-in, notifications and API access. Your email is the login and the billing reference; ask us to change it." />
      <div className="grid cols-2">
        <Profile me={me.data} onSaved={me.reload} />
        <Password />
      </div>
      <Tokens />
    </main>
  )
}

function Profile({ me, onSaved }: { me: Me; onSaved: () => Promise<void> | void }) {
  const toast = useToast()
  const [form, setForm] = useState({ contact_name: me.contact_name ?? '', company_name: me.company_name ?? '', phone: me.phone ?? '' })
  const [notify, setNotify] = useState(me.notify_jobs)
  const [busy, setBusy] = useState(false)

  async function save(e: React.FormEvent) {
    e.preventDefault()
    setBusy(true)
    try {
      await toast.run(async () => {
        await api.updateProfile({ contact_name: form.contact_name || null, company_name: form.company_name || null, phone: form.phone || null })
        await onSaved()
      }, 'Details saved')
    } finally {
      setBusy(false)
    }
  }

  async function toggleNotify(value: boolean) {
    setNotify(value)
    await toast.run(async () => { await api.updateProfile({ notify_jobs: value }); await onSaved() }, value ? 'You will be emailed when a reinstall, rescue or wipe finishes' : 'Notifications off')
  }

  return (
    <Card title="Your details">
      <form onSubmit={save}>
        <KV items={[['Email', <span className="mono">{me.email}</span>]]} />
        <div className="field" style={{ marginTop: 12 }}><label htmlFor="contact">Contact name</label><input id="contact" value={form.contact_name} onChange={(e) => setForm({ ...form, contact_name: e.target.value })} /></div>
        <div className="field"><label htmlFor="company">Company</label><input id="company" value={form.company_name} onChange={(e) => setForm({ ...form, company_name: e.target.value })} /></div>
        <div className="field"><label htmlFor="phone">Phone</label><input id="phone" value={form.phone} onChange={(e) => setForm({ ...form, phone: e.target.value })} placeholder="+61 …" /></div>
        <div className="row" style={{ justifyContent: 'space-between', alignItems: 'center' }}>
          <label className="row" style={{ gap: 8, cursor: 'pointer' }}>
            <input type="checkbox" style={{ width: 'auto' }} checked={notify} onChange={(e) => void toggleNotify(e.target.checked)} />
            <span>Email me when a reinstall, rescue boot or wipe finishes</span>
          </label>
          <button className="primary" type="submit" disabled={busy}>{busy ? 'Saving…' : 'Save'}</button>
        </div>
      </form>
    </Card>
  )
}

function Password() {
  const toast = useToast()
  const [current, setCurrent] = useState('')
  const [next, setNext] = useState('')
  const [again, setAgain] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const mismatch = again.length > 0 && next !== again

  async function change(e: React.FormEvent) {
    e.preventDefault()
    if (mismatch) return
    setBusy(true)
    setError(null)
    try {
      await api.changePassword(current, next)
      setCurrent(''); setNext(''); setAgain('')
      toast.ok('Password changed. Sessions already signed in stay signed in until they expire.')
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Card title="Password" note="At least 12 characters. API tokens are separate and are not affected.">
      <form onSubmit={change}>
        {error && <Banner kind="error">{error}</Banner>}
        <div className="field"><label htmlFor="pw-current">Current password</label><input id="pw-current" type="password" autoComplete="current-password" value={current} onChange={(e) => setCurrent(e.target.value)} required /></div>
        <div className="field"><label htmlFor="pw-new">New password</label><input id="pw-new" type="password" autoComplete="new-password" minLength={12} value={next} onChange={(e) => setNext(e.target.value)} required /></div>
        <div className="field"><label htmlFor="pw-again">New password again</label><input id="pw-again" type="password" autoComplete="new-password" value={again} onChange={(e) => setAgain(e.target.value)} required />{mismatch && <div className="small" style={{ color: 'var(--crit)', marginTop: 4 }}>They do not match.</div>}</div>
        <button className="primary" type="submit" disabled={busy || mismatch || next.length < 12}>{busy ? 'Changing…' : 'Change password'}</button>
      </form>
    </Card>
  )
}

function Tokens() {
  const tokens = useAsync<ApiToken[]>(() => api.tokens())
  const toast = useToast()
  const confirm = useConfirm()
  const [name, setName] = useState('')
  const [days, setDays] = useState('365')
  const [minted, setMinted] = useState<{ name: string; token: string } | null>(null)
  const [busy, setBusy] = useState(false)

  async function create(e: React.FormEvent) {
    e.preventDefault()
    setBusy(true)
    try {
      const created = await api.createToken(name.trim(), days === 'never' ? null : Number(days))
      setMinted({ name: created.name, token: created.token })
      setName('')
      await tokens.reload()
    } catch (err) {
      toast.error(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  async function revoke(token: ApiToken) {
    if (!(await confirm({ title: `Revoke ${token.name}?`, body: 'Anything using this token stops working immediately.', confirmLabel: 'Revoke', danger: true }))) return
    await toast.run(async () => { await api.revokeToken(token.id); await tokens.reload() }, 'Token revoked')
  }

  async function copy() {
    if (!minted) return
    try { await navigator.clipboard.writeText(minted.token); toast.ok('Copied') } catch { toast.error('Could not copy; select it and copy by hand') }
  }

  return (
    <Card
      title="API tokens"
      icon={<KeyRound />}
      note={<>Send a token as <span className="mono">Authorization: Bearer …</span> to the same API the portal uses; it can do exactly what you can here. The <Link to="/docs" target="_blank">API reference</Link> lists every call.</>}
      style={{ marginTop: 16 }}
    >
      {minted && (
        <Banner kind="info">
          <div><strong>{minted.name}</strong>: copy this token now; it is not shown again.</div>
          <div className="row" style={{ gap: 8, marginTop: 6, flexWrap: 'wrap' }}>
            <code className="mono" style={{ wordBreak: 'break-all' }}>{minted.token}</code>
            <button className="sm" onClick={copy}><Copy />Copy</button>
            <button className="sm ghost" onClick={() => setMinted(null)}>Done</button>
          </div>
        </Banner>
      )}
      <form onSubmit={create} className="row" style={{ gap: 8, flexWrap: 'wrap', alignItems: 'flex-end', marginBottom: 12 }}>
        <div className="field" style={{ margin: 0, flex: '1 1 200px' }}><label htmlFor="tok-name">Name</label><input id="tok-name" value={name} onChange={(e) => setName(e.target.value)} placeholder="deploy script" required /></div>
        <div className="field" style={{ margin: 0 }}><label htmlFor="tok-days">Expires</label>
          <select id="tok-days" value={days} onChange={(e) => setDays(e.target.value)} style={{ width: 'auto' }}>
            <option value="30">in 30 days</option><option value="90">in 90 days</option><option value="365">in a year</option><option value="never">never</option>
          </select>
        </div>
        <button className="primary" type="submit" disabled={busy || !name.trim()}><Plus />{busy ? 'Creating…' : 'Create token'}</button>
      </form>
      {!tokens.data?.length ? (
        <Empty>No tokens. Create one for scripts and integrations instead of using your password.</Empty>
      ) : (
        <table className="compact">
          <thead><tr><th>Name</th><th>Starts with</th><th>Created</th><th>Last used</th><th>Expires</th><th className="actions" /></tr></thead>
          <tbody>
            {tokens.data.map((t) => (
              <tr key={t.id}>
                <td>{t.name}</td>
                <td className="mono subtle">{t.token_prefix}…</td>
                <td className="subtle">{formatTime(t.created_at)}</td>
                <td className="subtle">{t.last_used_at ? formatTime(t.last_used_at) : 'never'}</td>
                <td className="subtle">{t.expires_at ? formatTime(t.expires_at) : 'never'}</td>
                <td className="actions"><button className="sm danger" onClick={() => revoke(t)}><Trash2 />Revoke</button></td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Card>
  )
}
