import { Plus, Router } from 'lucide-react'
import { useState } from 'react'
import { api, type BmcInfo, type NetworkPlanReport } from '../../../api'
import { Banner, Card, Empty, KV, useConfirm } from '../../../components'
import { useAsync, waitForJob } from '../../../hooks'
import { useToast } from '../../../toast'
import { AssignIpModal } from './modals'
import { useServer } from './ServerPage'

export default function NetworkTab() {
  const { server: s, refresh } = useServer()
  const toast = useToast()
  const confirm = useConfirm()
  const [assigning, setAssigning] = useState(false)
  const bmc = useAsync<BmcInfo>(() => api.bmcInfo(s.id), [s.id])
  const plan = useAsync<NetworkPlanReport>(() => api.networkPlan(s.id), [s.id, s.ip_addresses.length, s.switch_port, s.customer_vlan])
  const [applying, setApplying] = useState(false)
  const [showScript, setShowScript] = useState(false)

  async function applyToRouter() {
    setApplying(true)
    try {
      await toast.run(async () => {
        const job = await api.applyNetwork(s.id)
        const done = await waitForJob(job.id)
        toast.ok(done.stage ?? 'Router programmed')
      })
    } finally {
      setApplying(false)
    }
  }

  async function release(id: string, address: string) {
    if (!(await confirm({ title: `Release ${address}?`, body: 'The address returns to the pool. A running server keeps using it until it is reconfigured.', confirmLabel: 'Release', danger: true }))) return
    await toast.run(async () => { await api.releaseIp(id); await refresh() }, `${address} released`)
  }

  return (
    <>
      <Card
        title="Addresses"
        actions={<button className="sm" onClick={() => setAssigning(true)}><Plus />Assign address</button>}
        flush
        note="The installer configures the primary address statically. Assign one before reinstalling."
      >
        {!s.ip_addresses.length ? (
          <Empty>No addresses assigned.</Empty>
        ) : (
          <table className="compact">
            <thead><tr><th>Address</th><th>Gateway</th><th>Reverse DNS</th><th className="actions" /></tr></thead>
            <tbody>
              {s.ip_addresses.map((ip) => (
                <tr key={ip.id}>
                  <td className="mono">
                    {ip.address}/{ip.prefix_len}
                    {ip.is_primary && <span className="pill info" style={{ marginLeft: 8 }}>primary</span>}
                  </td>
                  <td className="mono subtle">{ip.gateway ?? '—'}</td>
                  <td className="mono subtle">{ip.rdns ?? '—'}</td>
                  <td className="actions"><button className="sm danger" onClick={() => release(ip.id, ip.address)}>Release</button></td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>

      <div className="grid cols-2">
        <Card title="Switch and VLAN">
          <KV items={[
            ['Switch', s.switch_name ? <span className="mono">{s.switch_name}</span> : null],
            ['Port', s.switch_port ? <span className="mono">{s.switch_port}</span> : null],
            ['Customer VLAN', s.customer_vlan],
            ['PXE MAC', s.provisioning_mac ? <span className="mono">{s.provisioning_mac}</span> : <span className="pill warning">not set</span>],
          ]} />
        </Card>
        <Card title="BMC network (from the CIMC)" note={bmc.error ? `Could not read: ${bmc.error}` : bmc.data ? undefined : 'Reading…'}>
          <KV items={[
            ['Address', <span className="mono">{s.cimc_ip}</span>],
            ['Reports itself as', bmc.data?.lan.ip_address ? <span className="mono">{bmc.data.lan.ip_address}/{bmc.data.lan.subnet_mask}</span> : null],
            ['Gateway', bmc.data?.lan.gateway ? <span className="mono">{bmc.data.lan.gateway}</span> : null],
            ['MAC', bmc.data?.lan.mac_address ? <span className="mono">{bmc.data.lan.mac_address}</span> : null],
            ['Source', bmc.data?.lan.source],
            ['VLAN', bmc.data?.lan.vlan],
            ['Ports', <span className="mono">IPMI {s.ipmi_port ?? 623} · HTTPS {s.redfish_port ?? 443}</span>],
          ]} />
        </Card>
      </div>

      <Card
        title="Router: switch port and PXE lease"
        icon={<Router />}
        note={plan.data?.configured ? `Programmed by the panel on ${plan.data.router} when a primary address is assigned and at every install.` : 'What the router must hold for this server. Set DOZ_ROUTEROS_URL on the management server and the panel programs it; until then, the commands are here to paste.'}
        actions={plan.data?.plan ? (
          <span className="row" style={{ gap: 6 }}>
            <button className="sm" onClick={() => setShowScript((v) => !v)}>{showScript ? 'Hide commands' : 'RouterOS commands'}</button>
            {plan.data.configured && <button className="sm primary" onClick={applyToRouter} disabled={applying}>{applying ? 'Applying…' : 'Apply to router'}</button>}
          </span>
        ) : null}
      >
        {plan.error ? (
          <Banner kind="error">{plan.error}</Banner>
        ) : !plan.data ? (
          <div className="subtle">Working it out…</div>
        ) : !plan.data.plan ? (
          <Empty>Not enough to program a port yet: {plan.data.reason}.</Empty>
        ) : (
          <>
            <KV items={[
              ['Switch port', <span className="mono">{plan.data.plan.port}</span>],
              ['VLAN', <span className="mono">{plan.data.plan.vlan}</span>],
              ['Gateway on the router', <span className="mono">{plan.data.plan.gateway}/{plan.data.plan.prefix_len}</span>],
              ['DHCP server', <span className="mono">{plan.data.plan.dhcp_server}</span>],
              ['Static lease', <span className="mono">{plan.data.plan.address} → {plan.data.plan.mac}</span>],
              ['PXE options', <span className="mono">next-server {plan.data.plan.next_server} · {plan.data.plan.boot_file}</span>],
            ]} />
            {showScript && plan.data.script && (
              <pre className="mono small" style={{ marginTop: 12, padding: 12, background: 'var(--surface-2)', borderRadius: 8, overflowX: 'auto', whiteSpace: 'pre' }}>{plan.data.script}</pre>
            )}
          </>
        )}
      </Card>

      {assigning && (
        <AssignIpModal serverId={s.id} hasPrimary={s.ip_addresses.some((a) => a.is_primary)} onClose={() => setAssigning(false)} onDone={async (m) => { setAssigning(false); toast.ok(m); await refresh() }} />
      )}
    </>
  )
}
