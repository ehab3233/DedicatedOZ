import { Plus } from 'lucide-react'
import { useState } from 'react'
import { api, type BmcInfo } from '../../../api'
import { Card, Empty, KV, useConfirm } from '../../../components'
import { useAsync } from '../../../hooks'
import { useToast } from '../../../toast'
import { AssignIpModal } from './modals'
import { useServer } from './ServerPage'

export default function NetworkTab() {
  const { server: s, refresh } = useServer()
  const toast = useToast()
  const confirm = useConfirm()
  const [assigning, setAssigning] = useState(false)
  const bmc = useAsync<BmcInfo>(() => api.bmcInfo(s.id), [s.id])

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

      {assigning && (
        <AssignIpModal serverId={s.id} hasPrimary={s.ip_addresses.some((a) => a.is_primary)} onClose={() => setAssigning(false)} onDone={async (m) => { setAssigning(false); toast.ok(m); await refresh() }} />
      )}
    </>
  )
}
