import { Plus } from 'lucide-react'
import { useState } from 'react'
import { api, type IPBlock } from '../../api'
import { Banner, Card, Empty, Modal, PageHeader, Progress } from '../../components'
import { useAsync } from '../../hooks'

export default function IPAM() {
  const blocks = useAsync(() => api.ipBlocks())
  const [creating, setCreating] = useState(false)
  const [inspecting, setInspecting] = useState<IPBlock | null>(null)

  return (
    <main className="page">
      <PageHeader
        title="IP space"
        sub="Blocks you can assign from. Provenance matters: record where each one came from."
        actions={<button className="primary" onClick={() => setCreating(true)}><Plus />Add block</button>}
      />

      {blocks.error && <Banner kind="error">{blocks.error}</Banner>}

      <Card flush>
        {!blocks.data?.length ? (
          <Empty>No IP blocks yet. Add the subnet your servers are reachable on, including its gateway.</Empty>
        ) : (
          <div className="table-scroll">
            <table>
              <thead><tr><th>Block</th><th>Gateway</th><th>Mode</th><th>VLAN</th><th style={{ width: 220 }}>Utilisation</th><th>Source</th><th className="actions" /></tr></thead>
              <tbody>
                {blocks.data.map((b) => {
                  const pct = b.total_hosts ? Math.round((b.assigned / b.total_hosts) * 100) : 0
                  return (
                    <tr key={b.id}>
                      <td className="mono strong">{b.cidr}</td>
                      <td className="mono subtle">{b.gateway ?? '—'}</td>
                      <td className="subtle">{b.routing_mode}</td>
                      <td className="subtle">{b.vlan ?? '—'}</td>
                      <td>
                        <Progress value={pct} />
                        <div className="cell-sub num">{b.assigned} / {b.total_hosts} ({pct}%)</div>
                      </td>
                      <td className="subtle">{b.source ?? '—'}</td>
                      <td className="actions"><button className="sm" onClick={() => setInspecting(b)}>Free addresses</button></td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      {creating && <CreateBlockModal onClose={() => setCreating(false)} onCreated={async () => { setCreating(false); await blocks.reload() }} />}
      {inspecting && <FreeAddressesModal block={inspecting} onClose={() => setInspecting(null)} />}
    </main>
  )
}

function CreateBlockModal({ onClose, onCreated }: { onClose: () => void; onCreated: () => void }) {
  const [cidr, setCidr] = useState('')
  const [gateway, setGateway] = useState('')
  const [mode, setMode] = useState('bridged')
  const [vlan, setVlan] = useState('')
  const [datacenter, setDatacenter] = useState('')
  const [source, setSource] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  async function submit(event: React.FormEvent) {
    event.preventDefault()
    setBusy(true)
    setError(null)
    try {
      await api.createIpBlock({ cidr: cidr.trim(), gateway: gateway.trim() || null, routing_mode: mode, vlan: vlan ? Number(vlan) : null, datacenter: datacenter || null, source: source || null })
      onCreated()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal
      title="Add an IP block"
      onClose={onClose}
      footer={<><button onClick={onClose}>Cancel</button><button type="submit" form="new-block" className="primary" disabled={busy}>{busy ? 'Adding…' : 'Add block'}</button></>}
    >
      <form id="new-block" onSubmit={submit}>
        {error && <Banner kind="error">{error}</Banner>}
        <div className="grid cols-2">
          <div className="field"><label htmlFor="cidr">CIDR</label><input id="cidr" className="mono" value={cidr} onChange={(e) => setCidr(e.target.value)} placeholder="203.0.113.0/24" required autoFocus /></div>
          <div className="field"><label htmlFor="gw">Gateway</label><input id="gw" className="mono" value={gateway} onChange={(e) => setGateway(e.target.value)} placeholder="203.0.113.1" /></div>
          <div className="field">
            <label htmlFor="mode">Mode</label>
            <select id="mode" value={mode} onChange={(e) => setMode(e.target.value)}>
              <option value="bridged">Bridged: one shared subnet, per-host addresses</option>
              <option value="routed">Routed: whole block handed to one server</option>
            </select>
          </div>
          <div className="field"><label htmlFor="vlan">VLAN (blank on a flat network)</label><input id="vlan" type="number" min={1} max={4094} value={vlan} onChange={(e) => setVlan(e.target.value)} /></div>
          <div className="field"><label htmlFor="dc">Datacenter</label><input id="dc" value={datacenter} onChange={(e) => setDatacenter(e.target.value)} /></div>
          <div className="field"><label htmlFor="src">Source (RIR allocation, lease, upstream)</label><input id="src" value={source} onChange={(e) => setSource(e.target.value)} placeholder="leased from …" /></div>
        </div>
      </form>
    </Modal>
  )
}

function FreeAddressesModal({ block, onClose }: { block: IPBlock; onClose: () => void }) {
  const free = useAsync(() => api.freeAddresses(block.id, 64), [block.id])
  return (
    <Modal title={`Free in ${block.cidr}`} onClose={onClose} footer={<button onClick={onClose}>Close</button>}>
      {free.error && <Banner kind="error">{free.error}</Banner>}
      {free.data && (
        <>
          <p className="subtle" style={{ marginBottom: 10 }}>{free.data.assigned} assigned of {free.data.total_hosts}. First {free.data.free_sample.length} free:</p>
          <div className="mono" style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(140px, 1fr))', gap: 4, fontSize: 12.5 }}>
            {free.data.free_sample.map((a) => <span key={a}>{a}</span>)}
          </div>
        </>
      )}
    </Modal>
  )
}
