import { Plus } from 'lucide-react'
import { useState } from 'react'
import { Link } from 'react-router-dom'
import { api, type AdminCustomer, type Subscription } from '../../api'
import { Banner, Card, Empty, Modal, PageHeader, formatTime, useConfirm } from '../../components'
import { useAsync } from '../../hooks'
import { useToast } from '../../toast'

export default function Customers() {
  const customers = useAsync(() => api.customers())
  const [creating, setCreating] = useState(false)
  const [selected, setSelected] = useState<AdminCustomer | null>(null)

  return (
    <main className="page">
      <PageHeader
        title="Customers"
        sub="Accounts, and which servers each one holds. Assign a server to a customer from the server's page."
        actions={<button className="primary" onClick={() => setCreating(true)}><Plus />New customer</button>}
      />

      {customers.error && <Banner kind="error">{customers.error}</Banner>}

      <Card flush>
        {!customers.data?.length ? (
          <Empty>No customers yet.</Empty>
        ) : (
          <div className="table-scroll">
            <table>
              <thead><tr><th>Email</th><th>Company</th><th>Contact</th><th className="right">Servers</th><th>Billing ref</th><th>Status</th><th className="actions" /></tr></thead>
              <tbody>
                {customers.data.map((c) => (
                  <tr key={c.id}>
                    <td><strong>{c.email}</strong>{c.is_admin && <span className="pill info" style={{ marginLeft: 8 }}>staff</span>}</td>
                    <td className="subtle">{c.company_name ?? '—'}</td>
                    <td className="subtle">{c.contact_name ?? '—'}</td>
                    <td className="right num">{c.active_servers}</td>
                    <td className="mono subtle">{c.billing_ref ?? '—'}</td>
                    <td><span className={`pill ${c.is_active ? 'ok' : 'critical'}`}>{c.is_active ? 'active' : 'disabled'}</span></td>
                    <td className="actions"><button className="sm" onClick={() => setSelected(c)}>Manage</button></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      {creating && <CreateCustomerModal onClose={() => setCreating(false)} onCreated={async () => { setCreating(false); await customers.reload() }} />}
      {selected && <CustomerModal customer={selected} onClose={() => setSelected(null)} onChanged={customers.reload} />}
    </main>
  )
}

function generatePassword(): string {
  const bytes = new Uint8Array(18)
  crypto.getRandomValues(bytes)
  return btoa(String.fromCharCode(...bytes)).replace(/[+/=]/g, '').slice(0, 20)
}

function CreateCustomerModal({ onClose, onCreated }: { onClose: () => void; onCreated: () => void }) {
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [company, setCompany] = useState('')
  const [contact, setContact] = useState('')
  const [phone, setPhone] = useState('')
  const [billingRef, setBillingRef] = useState('')
  const [isAdmin, setIsAdmin] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  async function submit(event: React.FormEvent) {
    event.preventDefault()
    setBusy(true)
    setError(null)
    try {
      await api.createCustomer({ email: email.trim(), password, company_name: company || null, contact_name: contact || null, phone: phone || null, billing_ref: billingRef || null, is_admin: isAdmin })
      onCreated()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal
      title="New customer"
      onClose={onClose}
      footer={<><button onClick={onClose}>Cancel</button><button type="submit" form="new-customer" className="primary" disabled={busy}>{busy ? 'Creating…' : 'Create'}</button></>}
    >
      <form id="new-customer" onSubmit={submit}>
        {error && <Banner kind="error">{error}</Banner>}
        <div className="field"><label htmlFor="email">Email (their login)</label><input id="email" type="email" value={email} onChange={(e) => setEmail(e.target.value)} required autoFocus /></div>
        <div className="field">
          <label htmlFor="pw">Initial password (12+ characters)</label>
          <div className="row">
            <input id="pw" className="mono" value={password} onChange={(e) => setPassword(e.target.value)} minLength={12} required style={{ flex: 1 }} />
            <button type="button" onClick={() => setPassword(generatePassword())}>Generate</button>
          </div>
          <div className="hint">Shown here only. Send it to the customer and have them change it.</div>
        </div>
        <div className="grid cols-2">
          <div className="field"><label htmlFor="company">Company</label><input id="company" value={company} onChange={(e) => setCompany(e.target.value)} /></div>
          <div className="field"><label htmlFor="contact">Contact name</label><input id="contact" value={contact} onChange={(e) => setContact(e.target.value)} /></div>
          <div className="field"><label htmlFor="phone">Phone</label><input id="phone" value={phone} onChange={(e) => setPhone(e.target.value)} /></div>
          <div className="field"><label htmlFor="billing">Billing ref (WHMCS / HostBill id)</label><input id="billing" value={billingRef} onChange={(e) => setBillingRef(e.target.value)} /></div>
        </div>
        <label className="check"><input type="checkbox" checked={isAdmin} onChange={(e) => setIsAdmin(e.target.checked)} /> Staff account (full admin access)</label>
      </form>
    </Modal>
  )
}

function CustomerModal({ customer, onClose, onChanged }: { customer: AdminCustomer; onClose: () => void; onChanged: () => Promise<void> }) {
  const subs = useAsync<Subscription[]>(() => api.subscriptions({ customer_id: customer.id, include_ended: true }), [customer.id])
  const toast = useToast()
  const confirm = useConfirm()
  const [newPassword, setNewPassword] = useState<string | null>(null)
  const [active, setActive] = useState(customer.is_active)

  async function act(fn: () => Promise<unknown>, message: string) {
    const ok = await toast.run(async () => { await fn(); await subs.reload(); await onChanged() }, message)
    return ok !== undefined
  }

  async function resetPassword() {
    if (!(await confirm({ title: `Reset the password for ${customer.email}?`, body: 'A new password is generated and shown once.', confirmLabel: 'Reset password' }))) return
    const password = generatePassword()
    if (await act(() => api.updateCustomer(customer.id, { password }), 'Password reset')) setNewPassword(password)
  }

  const current = (subs.data ?? []).filter((s) => !s.ended_at)
  const ended = (subs.data ?? []).filter((s) => s.ended_at)

  return (
    <Modal title={customer.email} onClose={onClose} wide footer={<button onClick={onClose}>Close</button>}>
      {newPassword && <Banner kind="info">New password: <code>{newPassword}</code> — shown once.</Banner>}
      <div className="row" style={{ marginBottom: 16 }}>
        <button onClick={resetPassword}>Reset password</button>
        {active ? (
          <button className="danger" onClick={async () => { if (await act(() => api.updateCustomer(customer.id, { is_active: false }), 'Login disabled; their servers keep running')) setActive(false) }}>Disable login</button>
        ) : (
          <button onClick={async () => { if (await act(() => api.updateCustomer(customer.id, { is_active: true }), 'Login re-enabled')) setActive(true) }}>Enable login</button>
        )}
      </div>

      <h4 style={{ marginBottom: 8, fontSize: 13 }}>Servers</h4>
      {!current.length ? (
        <p className="subtle" style={{ marginBottom: 12 }}>No active servers. Assign one from the server's page.</p>
      ) : (
        <table className="compact" style={{ marginBottom: 12 }}>
          <thead><tr><th>Server</th><th>Plan</th><th>Since</th><th className="actions" /></tr></thead>
          <tbody>
            {current.map((s) => (
              <tr key={s.id}>
                <td><Link to={`/admin/servers/${s.server_id}`} className="mono">{s.server_serial}</Link></td>
                <td className="subtle">{s.plan_name}{s.monthly_price != null && ` · ${s.currency} ${s.monthly_price}/mo`}</td>
                <td className="subtle">{formatTime(s.started_at)}</td>
                <td className="actions">
                  <button className="sm danger" onClick={async () => {
                    if (await confirm({ title: `End ${s.server_serial} for ${customer.email}?`, body: 'The server keeps running until you wipe it. Queue a wipe before reassigning it.', confirmLabel: 'End subscription', danger: true }))
                      await act(() => api.endSubscription(s.id), 'Subscription ended')
                  }}>End</button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {ended.length > 0 && (
        <details>
          <summary>{ended.length} past subscription{ended.length > 1 ? 's' : ''}</summary>
          <table className="compact" style={{ marginTop: 8 }}>
            <tbody>
              {ended.map((s) => (
                <tr key={s.id}>
                  <td className="mono subtle">{s.server_serial}</td>
                  <td className="subtle">{s.plan_name}</td>
                  <td className="subtle">{formatTime(s.started_at)} to {formatTime(s.ended_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </details>
      )}
    </Modal>
  )
}
