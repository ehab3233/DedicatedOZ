import { HardDrive, Network, RefreshCw } from 'lucide-react'
import { useState } from 'react'
import { api } from '../../../api'
import { Card, Empty, KV, Pill, Spinner, formatTime, label, relativeTime, useConfirm, useNow } from '../../../components'
import { TARGET_FIRMWARE, firmwareBelowTarget } from '../../../firmware'
import { waitForJob } from '../../../hooks'
import { useToast } from '../../../toast'
import { useServer } from './ServerPage'


export default function Hardware() {
  const { server: s, activeJob, refresh } = useServer()
  const toast = useToast()
  const confirm = useConfirm()
  const now = useNow()
  const [syncing, setSyncing] = useState<string | null>(null)
  const [raidLevel, setRaidLevel] = useState('raid1')
  const volumes = ((s as unknown as { volumes?: Array<Record<string, string | number | null>> }).volumes) ?? []

  async function configureRaid() {
    const clearing = raidLevel === 'none'
    if (!(await confirm({
      title: clearing ? 'Delete every virtual drive?' : `Build ${label(raidLevel)} on the RAID controller?`,
      body: clearing
        ? 'The BMC deletes every virtual drive on the controller. Everything on them is lost; the disks are left unconfigured.'
        : `The BMC deletes every existing virtual drive and builds one ${label(raidLevel)} array from the healthy drives it finds. Everything on those disks is lost. Takes a minute; the drives list refreshes afterwards.`,
      confirmLabel: clearing ? 'Delete virtual drives' : 'Build array',
      danger: true,
    }))) return
    setSyncing('raid')
    await toast.run(async () => {
      const job = await api.configureRaid(s.id, raidLevel)
      await refresh()
      const done = await waitForJob(job.id)
      await refresh()
      toast.ok(done.stage ?? 'RAID configured')
    })
    setSyncing(null)
  }

  const spec = (s as unknown as { hardware_spec?: Record<string, unknown> }).hardware_spec
  const syncedAt = typeof spec?.synced_at === 'string' ? spec.synced_at : null

  async function runJob(kind: 'inventory' | 'health') {
    setSyncing(kind)
    await toast.run(async () => {
      const job = kind === 'inventory' ? await api.syncInventory(s.id) : await api.healthCheck(s.id)
      await refresh()
      await waitForJob(job.id)
      await refresh()
    }, kind === 'inventory' ? 'Inventory read from the BMC' : 'Health read from the BMC')
    setSyncing(null)
  }

  const subsystems = Object.entries(s.health?.subsystems ?? {}) as Array<[string, { status?: string; detail?: unknown; reading?: string; state?: string }]>
  const nics = s.nics as Array<Record<string, string | number | null>>
  const drives = s.drives as Array<Record<string, string | number | boolean | null>>

  return (
    <>
      <div className="toolbar">
        <span className="subtle small">
          Inventory is re-read from the BMC every 30 minutes and health every 5. Sensors on the next tab are live.
        </span>
        <span className="spacer" />
        <button className="sm" disabled={Boolean(activeJob) || Boolean(syncing)} onClick={() => runJob('inventory')}>
          {syncing === 'inventory' ? <Spinner /> : <RefreshCw />}Sync inventory
        </button>
        <button className="sm" disabled={Boolean(activeJob) || Boolean(syncing)} onClick={() => runJob('health')}>
          {syncing === 'health' ? <Spinner /> : <RefreshCw />}Check health
        </button>
      </div>

      <div className="grid cols-2">
        <Card title="System" note={syncedAt ? `Inventory read ${relativeTime(syncedAt, now)} (${formatTime(syncedAt)})` : 'Inventory has not been read yet: run Sync inventory.'}>
          <KV
            items={[
              ['Model', s.model],
              ['CPU', s.cpu_count ? `${s.cpu_count} × ${s.cpu_model}` : null],
              ['Threads', s.cpu_cores_total],
              ['Memory', s.ram_gb ? `${s.ram_gb} GB` : null],
              ['BIOS', s.bios_version ? <span className="mono">{s.bios_version}</span> : null],
              ['CIMC firmware', s.cimc_firmware ? (
                <span className="row" style={{ gap: 6 }}>
                  <span className="mono">{s.cimc_firmware}</span>
                  {firmwareBelowTarget(s.cimc_firmware) && <span className="pill warning">below {TARGET_FIRMWARE}</span>}
                </span>
              ) : null],
              ['Serial', <span className="mono">{s.serial}</span>],
            ]}
          />
        </Card>

        <Card title="Health" actions={<Pill value={s.health_status} />} note={s.health_checked_at ? `Checked ${relativeTime(s.health_checked_at, now)} (${formatTime(s.health_checked_at)})${s.health?.missed_polls ? ` · the BMC missed the last ${s.health.missed_polls} poll${s.health.missed_polls > 1 ? 's' : ''}; this is the last verdict` : ''}` : 'Not checked yet.'} flush>
          {subsystems.length === 0 ? (
            <Empty>Nothing collected yet.</Empty>
          ) : (
            <table className="compact">
              <tbody>
                {subsystems.map(([name, info]) => (
                  <tr key={name}>
                    <td style={{ textTransform: 'capitalize' }}>{label(name)}</td>
                    <td><Pill value={info?.status} /></td>
                    <td className="subtle small">{describeSubsystem(info)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Card>
      </div>

      <Card title="Network interfaces" icon={<Network />} flush note="The PXE NIC is the one the netboot rail keys on. Pick the interface cabled to the provisioning network.">
        {nics.length === 0 ? (
          <Empty>No NICs recorded. Run <strong>Sync inventory</strong>.</Empty>
        ) : (
          <table className="compact">
            <thead><tr><th>Interface</th><th>MAC</th><th>Speed</th><th>Link</th><th className="actions">PXE</th></tr></thead>
            <tbody>
              {nics.map((nic, i) => {
                const isPxe = nic.mac === s.provisioning_mac
                return (
                  <tr key={i}>
                    <td className="mono">{String(nic.name ?? '—')}</td>
                    <td className="mono subtle">{String(nic.mac ?? '—')}</td>
                    <td className="subtle">{nic.speed_mbps ? `${nic.speed_mbps} Mb/s` : '—'}</td>
                    <td><LinkState value={nic.link_status} /></td>
                    <td className="actions">
                      {isPxe ? (
                        <span className="pill ok">PXE</span>
                      ) : (
                        <button className="sm" onClick={() => toast.run(async () => { await api.updateServer(s.id, { provisioning_mac: nic.mac }); await refresh() }, `PXE MAC set to ${nic.mac}`)}>
                          Use for PXE
                        </button>
                      )}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        )}
      </Card>

      <Card
        title="Drives"
        icon={<HardDrive />}
        flush
        note={volumes.length ? `Virtual drives: ${volumes.map((v) => `${v.name ?? '?'} (${String(v.raid_type ?? '?').replace(/^RAID/i, 'RAID ')}${v.capacity_gb ? `, ${v.capacity_gb} GB` : ''})`).join(' · ')}` : drives.length ? 'No virtual drive: the RAID controller has nothing to boot from until one is built.' : undefined}
        actions={
          <span className="row" style={{ gap: 6 }}>
            <select value={raidLevel} onChange={(e) => setRaidLevel(e.target.value)} disabled={Boolean(activeJob) || syncing === 'raid'} style={{ width: 'auto' }}>
              <option value="raid1">RAID 1</option>
              <option value="raid0">RAID 0</option>
              <option value="raid5">RAID 5</option>
              <option value="raid6">RAID 6</option>
              <option value="raid10">RAID 10</option>
              <option value="none">No array (delete)</option>
            </select>
            <button className="danger sm" disabled={Boolean(activeJob) || syncing === 'raid'} onClick={configureRaid} title="Rebuild the array through the BMC. Destroys the data on the disks.">{syncing === 'raid' ? <Spinner /> : <HardDrive />}Configure RAID</button>
          </span>
        }
      >
        {drives.length === 0 ? (
          <Empty>No drives recorded. Run <strong>Sync inventory</strong>. The M4's Redfish only lists drives behind a RAID controller it knows.</Empty>
        ) : (
          <table className="compact">
            <thead><tr><th>Drive</th><th>Model</th><th>Serial</th><th>Media</th><th className="right">Capacity</th><th>State</th><th>Health</th></tr></thead>
            <tbody>
              {drives.map((d, i) => (
                <tr key={i}>
                  <td className="mono">{String(d.name ?? '—')}</td>
                  <td className="subtle">{String(d.model ?? '—')}</td>
                  <td className="mono subtle">{String(d.serial ?? '—')}</td>
                  <td className="subtle">{String(d.media ?? '—')}</td>
                  <td className="right num">{d.capacity_gb ? `${d.capacity_gb} GB` : '—'}</td>
                  <td className="subtle">{String(d.state ?? '—')}</td>
                  <td>
                    <Pill value={d.failure_predicted ? 'critical' : String(d.health ?? 'unknown').toLowerCase()} />
                    {d.failure_predicted ? <span className="small" style={{ color: 'var(--crit)', marginLeft: 6 }}>failure predicted</span> : null}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>
    </>
  )
}

function LinkState({ value }: { value: string | number | null }) {
  const text = String(value ?? '').toLowerCase()
  if (!text) return <span className="faint">—</span>
  const up = text.includes('up') || text === 'linkup'
  return <span className="status-line"><span className={`dot ${up ? 'ok' : 'off'}`} />{String(value)}</span>
}

function describeSubsystem(info: { detail?: unknown; reading?: string; state?: string } | undefined): string {
  if (!info) return ''
  const detail = info.detail
  if (Array.isArray(detail)) {
    return detail
      .map((d: Record<string, unknown>) => {
        const name = String(d.name ?? '')
        const value = d.celsius != null ? `${d.celsius}°C` : d.reading != null ? `${d.reading}` : d.state ? String(d.state) : ''
        const bad = d.status && d.status !== 'ok' ? ` (${d.status})` : ''
        return `${name}${value ? ` ${value}` : ''}${bad}`
      })
      .filter(Boolean)
      .join(' · ')
  }
  if (detail && typeof detail === 'object') {
    const entries = Object.entries(detail as Record<string, { status?: string; reading?: string }>)
    const bad = entries.filter(([, v]) => v?.status && v.status !== 'ok')
    return bad.length ? bad.map(([k, v]) => `${k}: ${v.reading ?? v.status}`).join(' · ') : `${entries.length} sensors ok`
  }
  return info.reading ?? info.state ?? ''
}
