import { useState } from 'react'
import { Link } from 'react-router-dom'
import { api, type AdminCustomer, type AdminServer, type IPBlock, type Image, type OSTemplate, type Subscription } from '../../../api'
import { Banner, Modal, formatBytes } from '../../../components'
import { useAsync, waitForJob } from '../../../hooks'

type Done = (message: string) => void

function useSubmit(onDone: Done) {
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  async function submit(fn: () => Promise<string>) {
    setBusy(true)
    setError(null)
    try {
      onDone(await fn())
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }
  return { error, busy, submit }
}

// ---------------------------------------------------------------------------

export function AssignModal({ serverId, onClose, onDone }: { serverId: string; onClose: () => void; onDone: Done }) {
  const customers = useAsync<AdminCustomer[]>(() => api.customers())
  const [customerId, setCustomerId] = useState('')
  const [plan, setPlan] = useState('C220-M4')
  const [price, setPrice] = useState('')
  const [currency, setCurrency] = useState('USD')
  const [quota, setQuota] = useState('')
  const { error, busy, submit } = useSubmit(onDone)
  const eligible = (customers.data ?? []).filter((c) => !c.is_admin)

  return (
    <Modal
      title="Assign to a customer"
      onClose={onClose}
      footer={<><button onClick={onClose}>Cancel</button><button className="primary" disabled={busy || !customerId} onClick={() => submit(async () => {
        const sub: Subscription = await api.createSubscription({ customer_id: customerId, server_id: serverId, plan_name: plan, monthly_price: price ? Number(price) : null, currency, bandwidth_quota_tb: quota ? Number(quota) : null })
        return `Assigned to ${sub.customer_email}. They can see it in their portal now.`
      })}>{busy ? 'Assigning…' : 'Assign'}</button></>}
    >
      {error && <Banner kind="error">{error}</Banner>}
      <div className="field">
        <label htmlFor="cust">Customer</label>
        <select id="cust" value={customerId} onChange={(e) => setCustomerId(e.target.value)} required>
          <option value="">Choose…</option>
          {eligible.map((c) => <option key={c.id} value={c.id}>{c.email}{c.company_name ? ` — ${c.company_name}` : ''}</option>)}
        </select>
        {customers.data && eligible.length === 0 && <div className="hint">No customer accounts yet: <Link to="/admin/customers">create one</Link> first.</div>}
      </div>
      <div className="grid cols-2">
        <div className="field"><label htmlFor="plan">Plan name</label><input id="plan" value={plan} onChange={(e) => setPlan(e.target.value)} required /></div>
        <div className="field">
          <label htmlFor="price">Monthly price</label>
          <div className="row">
            <input id="price" type="number" step="0.01" value={price} onChange={(e) => setPrice(e.target.value)} style={{ flex: 1 }} />
            <input value={currency} onChange={(e) => setCurrency(e.target.value.toUpperCase())} maxLength={3} style={{ width: 70 }} />
          </div>
        </div>
        <div className="field"><label htmlFor="quota">Bandwidth quota (TB/mo)</label><input id="quota" type="number" value={quota} onChange={(e) => setQuota(e.target.value)} /></div>
      </div>
      <p className="faint small">Billing stays in WHMCS / HostBill; the plan is recorded here so the portal can show it.</p>
    </Modal>
  )
}

export function AdminReinstallModal({ serverId, customerEmail, onClose, onDone }: { serverId: string; customerEmail: string | null; onClose: () => void; onDone: Done }) {
  const templates = useAsync<OSTemplate[]>(() => api.osTemplates())
  const netboot = useAsync(() => api.netboot())
  const notReady = new Set((netboot.data?.templates ?? []).filter((t) => !t.ready).map((t) => t.id))
  const [templateId, setTemplateId] = useState('')
  const [hostname, setHostname] = useState('')
  const [raid, setRaid] = useState('raid1')
  const [rootPassword, setRootPassword] = useState('')
  const [confirmText, setConfirmText] = useState('')
  const { error, busy, submit } = useSubmit(onDone)
  const ready = Boolean(templateId) && confirmText.trim().toUpperCase() === 'REINSTALL'

  return (
    <Modal
      title="Reinstall operating system"
      onClose={onClose}
      footer={<><button onClick={onClose}>Cancel</button><button className="primary danger" disabled={busy || !ready} onClick={() => submit(async () => {
        await api.reinstall(serverId, { os_template_id: templateId, hostname: hostname || undefined, raid_level: raid, ssh_key_ids: [], confirm_data_loss: true, ...(rootPassword ? { root_password: rootPassword } : {}) })
        return 'Reinstall queued. About 20 minutes; watch the progress bar.'
      })}>{busy ? 'Queueing…' : 'Reinstall'}</button></>}
    >
      <Banner kind="error">Erases every disk on the server.</Banner>
      {error && <Banner kind="error">{error}</Banner>}
      {netboot.data && !netboot.data.ramdisk.ready && (
        <Banner kind="warning">The installer ramdisk is not built, so nothing can be reinstalled yet: run <code>sudo /opt/doz/doz.sh ramdisk</code> on the management server. The <Link to="/admin/images">Images</Link> page lists what is missing.</Banner>
      )}
      <Banner kind="info">
        {customerEmail ? <>SSH keys from <strong>{customerEmail}</strong>'s account are installed.</> : <>Unassigned server: no customer keys. Set a root password below or the install is unreachable.</>}
      </Banner>
      <div className="field">
        <label htmlFor="os">Operating system</label>
        <select id="os" value={templateId} onChange={(e) => setTemplateId(e.target.value)}>
          <option value="">Choose…</option>
          {(templates.data ?? []).map((t) => <option key={t.id} value={t.id} disabled={notReady.has(t.id)}>{t.name} {t.version}{notReady.has(t.id) ? ' (files missing on the management server)' : ''}</option>)}
        </select>
      </div>
      <div className="grid cols-2">
        <div className="field"><label htmlFor="host">Hostname</label><input id="host" value={hostname} onChange={(e) => setHostname(e.target.value)} /></div>
        <div className="field">
          <label htmlFor="raid">RAID</label>
          <select id="raid" value={raid} onChange={(e) => setRaid(e.target.value)}>
            <option value="raid1">RAID 1</option><option value="raid0">RAID 0</option><option value="raid5">RAID 5</option><option value="raid10">RAID 10</option><option value="none">None (JBOD)</option>
          </select>
        </div>
      </div>
      <div className="field"><label htmlFor="rootpw">Root password (optional, 12+ characters; stored hashed)</label><input id="rootpw" type="password" value={rootPassword} onChange={(e) => setRootPassword(e.target.value)} autoComplete="new-password" /></div>
      <div className="field"><label htmlFor="confirm">Type REINSTALL to confirm</label><input id="confirm" className="mono" value={confirmText} onChange={(e) => setConfirmText(e.target.value)} autoComplete="off" /></div>
    </Modal>
  )
}

export function WipeModal({ serverId, serial, onClose, onDone }: { serverId: string; serial: string; onClose: () => void; onDone: Done }) {
  const [method, setMethod] = useState<'secure' | 'zero'>('secure')
  const [confirmText, setConfirmText] = useState('')
  const { error, busy, submit } = useSubmit(onDone)

  return (
    <Modal
      title="Secure wipe"
      onClose={onClose}
      footer={<><button onClick={onClose}>Cancel</button><button className="primary danger" disabled={busy || confirmText.trim() !== serial} onClick={() => submit(async () => {
        await api.wipe(serverId, method)
        return 'Wipe queued. The server powers off when it finishes and returns to stock.'
      })}>{busy ? 'Queueing…' : 'Wipe'}</button></>}
    >
      <Banner kind="error">Destroys all data on every drive in {serial}. Required before resale.</Banner>
      {error && <Banner kind="error">{error}</Banner>}
      <div className="field">
        <label htmlFor="method">Method</label>
        <select id="method" value={method} onChange={(e) => setMethod(e.target.value as 'secure' | 'zero')}>
          <option value="secure">Secure erase (ATA / NVMe / SCSI format, falls back to overwrite)</option>
          <option value="zero">Zero overwrite only</option>
        </select>
      </div>
      <div className="field"><label htmlFor="confirm">Type the serial <code>{serial}</code> to confirm</label><input id="confirm" className="mono" value={confirmText} onChange={(e) => setConfirmText(e.target.value)} autoComplete="off" /></div>
    </Modal>
  )
}

export function AssignIpModal({ serverId, hasPrimary, onClose, onDone }: { serverId: string; hasPrimary: boolean; onClose: () => void; onDone: Done }) {
  const blocks = useAsync<IPBlock[]>(() => api.ipBlocks())
  const [blockId, setBlockId] = useState('')
  const [address, setAddress] = useState('')
  const [isPrimary, setIsPrimary] = useState(!hasPrimary)
  const [suggestions, setSuggestions] = useState<string[]>([])
  const { error, busy, submit } = useSubmit(onDone)
  const [pickError, setPickError] = useState<string | null>(null)

  async function pickBlock(id: string) {
    setBlockId(id)
    setSuggestions([])
    if (!id) return
    try {
      const free = await api.freeAddresses(id, 12)
      setSuggestions(free.free_sample)
      if (!address && free.free_sample[0]) setAddress(free.free_sample[0])
    } catch (e) {
      setPickError(e instanceof Error ? e.message : String(e))
    }
  }

  return (
    <Modal
      title="Assign an address"
      onClose={onClose}
      footer={<><button onClick={onClose}>Cancel</button><button className="primary" disabled={busy || !blockId || !address} onClick={() => submit(async () => {
        await api.assignIp(serverId, { block_id: blockId, address: address.trim(), is_primary: isPrimary })
        return `${address} assigned${isPrimary ? ' as primary' : ''}`
      })}>{busy ? 'Assigning…' : 'Assign'}</button></>}
    >
      {(error || pickError) && <Banner kind="error">{error ?? pickError}</Banner>}
      <div className="field">
        <label htmlFor="block">Block</label>
        <select id="block" value={blockId} onChange={(e) => pickBlock(e.target.value)}>
          <option value="">Choose…</option>
          {(blocks.data ?? []).map((b) => <option key={b.id} value={b.id}>{b.cidr} — {b.total_hosts - b.assigned} free{b.gateway ? ` · gw ${b.gateway}` : ''}</option>)}
        </select>
        {blocks.data && blocks.data.length === 0 && <div className="hint">No blocks yet: <Link to="/admin/ipam">add one</Link> first.</div>}
      </div>
      <div className="field">
        <label htmlFor="addr">Address</label>
        <input id="addr" className="mono" value={address} onChange={(e) => setAddress(e.target.value)} />
        {suggestions.length > 0 && (
          <div className="key-list" style={{ marginTop: 6 }}>
            {suggestions.map((a) => <button type="button" key={a} className="sm mono" onClick={() => setAddress(a)}>{a}</button>)}
          </div>
        )}
      </div>
      <label className="check"><input type="checkbox" checked={isPrimary} onChange={(e) => setIsPrimary(e.target.checked)} /> Primary: the installer configures this one statically</label>
    </Modal>
  )
}

export function EditModal({ server, onClose, onDone }: { server: AdminServer; onClose: () => void; onDone: Done }) {
  const [form, setForm] = useState({
    hostname: server.hostname ?? '',
    datacenter: server.datacenter ?? '',
    rack: server.rack ?? '',
    rack_unit: server.rack_unit?.toString() ?? '',
    switch_name: server.switch_name ?? '',
    switch_port: server.switch_port ?? '',
    customer_vlan: server.customer_vlan?.toString() ?? '',
    role: server.role ?? 'customer',
    provisioning_mac: server.provisioning_mac ?? '',
    cimc_credential_ref: server.cimc_credential_ref,
    cimc_ip: server.cimc_ip,
    bmc_protocol: server.bmc_protocol ?? '',
    ipmi_cipher_suite: server.ipmi_cipher_suite ?? '',
    ipmi_port: server.ipmi_port?.toString() ?? '',
    redfish_port: server.redfish_port?.toString() ?? '',
    notes: server.notes ?? '',
  })
  const { error, busy, submit } = useSubmit(onDone)
  const set = (k: keyof typeof form) => (e: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement>) => setForm((f) => ({ ...f, [k]: e.target.value }))

  return (
    <Modal
      title={`Edit ${server.serial}`}
      onClose={onClose}
      wide
      footer={<><button onClick={onClose}>Cancel</button><button className="primary" disabled={busy} onClick={() => submit(async () => {
        await api.updateServer(server.id, {
          hostname: form.hostname || null, datacenter: form.datacenter || null, rack: form.rack || null,
          rack_unit: form.rack_unit ? Number(form.rack_unit) : null, switch_name: form.switch_name || null,
          switch_port: form.switch_port || null, customer_vlan: form.customer_vlan ? Number(form.customer_vlan) : null, role: form.role,
          provisioning_mac: form.provisioning_mac || null, cimc_credential_ref: form.cimc_credential_ref, cimc_ip: form.cimc_ip,
          bmc_protocol: form.bmc_protocol || null, ipmi_cipher_suite: form.ipmi_cipher_suite || null, ipmi_port: form.ipmi_port ? Number(form.ipmi_port) : null,
          redfish_port: form.redfish_port ? Number(form.redfish_port) : null, notes: form.notes || null,
        })
        return 'Saved'
      })}>{busy ? 'Saving…' : 'Save'}</button></>}
    >
      {error && <Banner kind="error">{error}</Banner>}
      <div className="grid cols-3">
        <div className="field"><label>Hostname</label><input value={form.hostname} onChange={set('hostname')} /></div>
        <div className="field"><label>CIMC IP</label><input className="mono" value={form.cimc_ip} onChange={set('cimc_ip')} /></div>
        <div className="field"><label>Credential ref</label><input className="mono" value={form.cimc_credential_ref} onChange={set('cimc_credential_ref')} /></div>
        <div className="field">
          <label>Power/boot control</label>
          <select value={form.bmc_protocol} onChange={set('bmc_protocol')}>
            <option value="">Platform default</option>
            <option value="auto">Auto — IPMI, then Redfish</option>
            <option value="ipmi">IPMI only</option>
            <option value="redfish">Redfish only</option>
          </select>
        </div>
        <div className="field">
          <label>IPMI cipher suite</label>
          <select value={form.ipmi_cipher_suite} onChange={set('ipmi_cipher_suite')}>
            <option value="">Platform default</option>
            <option value="3">3 (SHA1, AES)</option>
            <option value="17">17 (SHA256, AES)</option>
            <option value="auto">Let ipmitool probe (slow)</option>
          </select>
        </div>
        <div className="field"><label>IPMI port (blank = 623)</label><input type="number" min={1} max={65535} value={form.ipmi_port} onChange={set('ipmi_port')} /></div>
        <div className="field"><label>HTTPS port (blank = 443)</label><input type="number" min={1} max={65535} value={form.redfish_port} onChange={set('redfish_port')} /></div>
        <div className="field"><label>Datacenter</label><input value={form.datacenter} onChange={set('datacenter')} /></div>
        <div className="field"><label>Rack</label><input value={form.rack} onChange={set('rack')} /></div>
        <div className="field"><label>Rack unit</label><input type="number" min={1} max={60} value={form.rack_unit} onChange={set('rack_unit')} /></div>
        <div className="field"><label>PXE MAC</label><input className="mono" value={form.provisioning_mac} onChange={set('provisioning_mac')} /></div>
        <div className="field"><label>Switch</label><input value={form.switch_name} onChange={set('switch_name')} /></div>
        <div className="field"><label>Switch port</label><input value={form.switch_port} onChange={set('switch_port')} /></div>
        <div className="field"><label>Role</label>
          <select value={form.role} onChange={(e) => setForm({ ...form, role: e.target.value as 'customer' | 'management' })}>
            <option value="customer">Customer server: provisioned and handed out</option>
            <option value="management">Management server: read only, never changed from the panel</option>
          </select>
        </div>
        <div className="field"><label>Customer VLAN</label><input type="number" min={1} max={4094} value={form.customer_vlan} onChange={set('customer_vlan')} /></div>
      </div>
      <div className="field"><label>Notes</label><textarea value={form.notes} onChange={set('notes')} /></div>
    </Modal>
  )
}

/** Mount an ISO from the image store on the BMC and boot from it. */
export function InstallFromImageModal({ serverId, onClose, onDone }: { serverId: string; onClose: () => void; onDone: Done }) {
  const images = useAsync<Image[]>(() => api.images())
  const [imageId, setImageId] = useState('')
  const [boot, setBoot] = useState(true)
  const [stage, setStage] = useState<string | null>(null)
  const { error, busy, submit } = useSubmit(onDone)
  const ready = (images.data ?? []).filter((i) => i.status === 'ready')
  const chosen = ready.find((i) => i.id === imageId)

  return (
    <Modal
      title="Install from image"
      onClose={onClose}
      footer={<><button onClick={onClose} disabled={busy}>Cancel</button><button className="primary" disabled={busy || !imageId} onClick={() => submit(async () => {
        const job = await api.vmediaBoot(serverId, imageId, boot)
        setStage('queued')
        const done = await waitForJob(job.id, (j) => setStage(j.stage ?? j.state))
        return done.stage ?? (boot ? 'Booting from the image; open the KVM to drive the installer.' : 'Image mounted.')
      })}>{busy ? 'Working…' : boot ? 'Mount and boot' : 'Mount'}</button></>}
    >
      {error && <Banner kind="error">{error}</Banner>}
      <p className="subtle" style={{ marginBottom: 12 }}>
        The BMC mounts the ISO over HTTP from the management server as a virtual CD. With <em>boot</em> on, the server is set to boot from it once and power-cycled; then open the <strong>KVM</strong> and drive the installer with keyboard and mouse.
      </p>
      <div className="field">
        <label htmlFor="img">Image</label>
        <select id="img" value={imageId} onChange={(e) => setImageId(e.target.value)} disabled={busy}>
          <option value="">Choose…</option>
          {ready.map((i) => <option key={i.id} value={i.id}>{i.name} — {formatBytes(i.size_bytes)}</option>)}
        </select>
        {images.data && ready.length === 0 && <div className="hint">No images yet: <Link to="/admin/images">upload or fetch one</Link> first.</div>}
        {chosen && <div className="hint mono">{chosen.url}</div>}
      </div>
      <label className="check"><input type="checkbox" checked={boot} onChange={(e) => setBoot(e.target.checked)} disabled={busy} /> Boot from it now (one-time CD boot, then power cycle)</label>
      {stage && <p className="subtle" style={{ marginTop: 12 }}><span className="spinner" /> {stage}</p>}
    </Modal>
  )
}
