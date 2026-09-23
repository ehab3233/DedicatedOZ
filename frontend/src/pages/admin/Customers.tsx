import { useState } from 'react'
import { Link } from 'react-router-dom'
import { api, type AdminCustomer, type Subscription } from '../../api'
import { Banner, Empty, Modal, formatTime } from '../../components'
import { useAsync } from '../../hooks'

export default function Customers() {
  const customers = useAsync(() => api.customers())
  const [creating, setCreating] = useState(false)
  const [selected, setSelected] = useState<AdminCustomer | null>(null)

  return (
    <main className="page">
      <div className="spread">
        <div>
          <h1>Customers</h1>
          <p className="subtle">Accounts, and which servers each one holds.</p>
        </div>
        <button className="primary" onClick={() => setCreating(true)}>New customer</button>
      </div>

      {customers.error && <Banner kind="error">{customers.error}</Banner>}

      <div className="card table-scroll" style={{ padding: 0 }}>
        {!customers.data?.length ? (
          <Empty>No customers yet.</Empty>
        ) : (
          <table>
            <thead>
              <tr>
                <th>Email</th>
                <th>Company</th>
                <th>Contact</th>
                <th>Servers</th>
                <th>Billing ref</th>
                <th>Status</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {customers.data.map((c) => (
                <tr key={c.id}>
                  <td>
                    <strong>{c.email}</strong>
                    {c.is_admin && <span className="pill" style={{ marginLeft: 8 }}>admin</span>}
                  </td>
                  <td className="subtle">{c.company_name ?? '—'}</td>
                  <td className="subtle">{c.contact_name ?? '—'}</td>
                  <td>{c.active_servers}</td>
                  <td className="mono subtle">{c.billing_ref ?? '—'}</td>
                  <td>
                    <span className={`pill ${c.is_active ? 'ok' : 'critical'}`}>
                      {c.is_active ? 'active' : 'disabled'}
                    </span>
                  </td>
                  <td>
                    <button onClick={() => setSelected(c)}>Manage</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {creating && (
        <CreateCustomerModal
          onClose={() => setCreating(false)}
          onCreated={async () => {
            setCreating(false)
            await customers.reload()
          }}
        />
      )}

      {selected && (
        <CustomerModal
          customer={selected}
          onClose={() => setSelected(null)}
          onChanged={async () => {
            await customers.reload()
          }}
        />
      )}
    </main>
  )
}

// ---------------------------------------------------------------------------

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

  function generatePassword() {
    const bytes = new Uint8Array(18)
    crypto.getRandomValues(bytes)
    setPassword(btoa(String.fromCharCode(...bytes)).replace(/[+/=]/g, '').slice(0, 20))
  }

  async function submit(event: React.FormEvent) {
    event.preventDefault()
    setBusy(true)
    setError(null)
    try {
      await api.createCustomer({
        email: email.trim(),
        password,
        company_name: company || null,
        contact_name: contact || null,
        phone: phone || null,
        billing_ref: billingRef || null,
        is_admin: isAdmin,
      })
      onCreated()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal title="New customer" onClose={onClose}>
      <form onSubmit={submit}>
        {error && <Banner kind="error">{error}</Banner>}
        <div className="field">
          <label htmlFor="email">Email (their login)</label>
          <input id="email" type="email" value={email} onChange={(e) => setEmail(e.target.value)} required />
        </div>
        <div className="field">
          <label htmlFor="pw">Initial password (12+ characters)</label>
          <div className="row">
            <input id="pw" value={password} onChange={(e) => setPassword(e.target.value)} minLength={12} required style={{ flex: 1 }} className="mono" />
            <button type="button" onClick={generatePassword}>Generate</button>
          </div>
          <div className="subtle" style={{ fontSize: 12, marginTop: 4 }}>
            Shown here only. Send it to the customer and have them change it.
          </div>
        </div>
        <div className="grid cols-2">
          <div className="field">
            <label htmlFor="company">Company</label>
            <input id="company" value={company} onChange={(e) => setCompany(e.target.value)} />
          </div>
          <div className="field">
            <label htmlFor="contact">Contact name</label>
            <input id="contact" value={contact} onChange={(e) => setContact(e.target.value)} />
          </div>
          <div className="field">
            <label htmlFor="phone">Phone</label>
            <input id="phone" value={phone} onChange={(e) => setPhone(e.target.value)} />
          </div>
          <div className="field">
            <label htmlFor="billing">Billing ref (WHMCS / HostBill id)</label>
            <input id="billing" value={billingRef} onChange={(e) => setBillingRef(e.target.value)} />
          </div>
        </div>
        <label className="row" style={{ marginBottom: 14 }}>
          <input type="checkbox" style={{ width: 'auto' }} checked={isAdmin} onChange={(e) => setIsAdmin(e.target.checked)} />
          <span>Staff account (full admin access)</span>
        </label>
        <div className="row" style={{ justifyContent: 'flex-end' }}>
          <button type="button" onClick={onClose}>Cancel</button>
          <button type="submit" className="primary" disabled={busy}>{busy ? 'Creating…' : 'Create'}</button>
        </div>
      </form>
    </Modal>
  )
}

// ---------------------------------------------------------------------------

function CustomerModal({
  customer,
  onClose,
  onChanged,
}: {
  customer: AdminCustomer
  onClose: () => void
  onChanged: () => void
}) {
  const subs = useAsync<Subscription[]>(
    () => api.subscriptions({ customer_id: customer.id, include_ended: true }),
    [customer.id],
  )
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)

  async function act(fn: () => Promise<unknown>, message: string) {
    setError(null)
    setNotice(null)
    try {
      await fn()
      setNotice(message)
      await subs.reload()
      onChanged()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }

  function resetPassword() {
    const bytes = new Uint8Array(18)
    crypto.getRandomValues(bytes)
    const password = btoa(String.fromCharCode(...bytes)).replace(/[+/=]/g, '').slice(0, 20)
    void act(() => api.updateCustomer(customer.id, { password }), `Password reset. New password: ${password}`)
  }

  const active = (subs.data ?? []).filter((s) => !s.ended_at)
  const ended = (subs.data ?? []).filter((s) => s.ended_at)

  return (
    <Modal title={customer.email} onClose={onClose}>
      {error && <Banner kind="error">{error}</Banner>}
      {notice && <Banner kind="info"><span className="mono">{notice}</span></Banner>}

      <div className="row" style={{ marginBottom: 16 }}>
        <button onClick={resetPassword}>Reset password</button>
        {customer.is_active ? (
          <button className="danger" onClick={() => act(() => api.updateCustomer(customer.id, { is_active: false }), 'Account disabled. Their servers keep running; they cannot log in.')}>
            Disable login
          </button>
        ) : (
          <button onClick={() => act(() => api.updateCustomer(customer.id, { is_active: true }), 'Account re-enabled.')}>
            Enable login
          </button>
        )}
      </div>

      <h2 style={{ marginTop: 0 }}>Servers</h2>
      {!active.length ? (
        <div className="subtle" style={{ marginBottom: 12 }}>
          No active servers. Assign one from the server's admin page.
        </div>
      ) : (
        <table style={{ marginBottom: 12 }}>
          <thead>
            <tr><th>Server</th><th>Plan</th><th>Since</th><th /></tr>
          </thead>
          <tbody>
            {active.map((s) => (
              <tr key={s.id}>
                <td><Link to={`/admin/servers/${s.server_id}`} className="mono">{s.server_serial}</Link></td>
                <td className="subtle">{s.plan_name}{s.monthly_price != null && ` · ${s.currency} ${s.monthly_price}/mo`}</td>
                <td className="subtle">{formatTime(s.started_at)}</td>
                <td>
                  <button className="danger" onClick={() => {
                    if (confirm(`End ${s.server_serial} for ${customer.email}? The server keeps running until you wipe it.`))
                      void act(() => api.endSubscription(s.id), 'Subscription ended. Queue a wipe before reassigning the server.')
                  }}>
                    End
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {ended.length > 0 && (
        <details>
          <summary className="subtle" style={{ cursor: 'pointer' }}>{ended.length} past subscription{ended.length > 1 ? 's' : ''}</summary>
          <table style={{ marginTop: 8 }}>
            <tbody>
              {ended.map((s) => (
                <tr key={s.id}>
                  <td className="mono subtle">{s.server_serial}</td>
                  <td className="subtle">{s.plan_name}</td>
                  <td className="subtle">{formatTime(s.started_at)} → {formatTime(s.ended_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </details>
      )}

      <div className="row" style={{ justifyContent: 'flex-end', marginTop: 16 }}>
        <button onClick={onClose}>Close</button>
      </div>
    </Modal>
  )
}
