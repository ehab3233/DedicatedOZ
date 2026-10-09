import { Activity, CheckCircle2, Copy, Disc3, KeyRound, Lightbulb, RefreshCw, RotateCcw, Wrench, XCircle } from 'lucide-react'
import { useState } from 'react'
import { api, type BmcInfo, type BmcTestReport, type VmediaStatus } from '../../../api'
import { Banner, Card, Empty, KV, Modal, Pill, Spinner, formatBytes, label, relativeTime, useConfirm, useNow } from '../../../components'
import { useAsync, usePolling, waitForJob } from '../../../hooks'
import { useToast } from '../../../toast'
import { InstallFromImageModal } from './modals'
import { useServer } from './ServerPage'

export default function Bmc() {
  const { server: s, activeJob, refresh } = useServer()
  const toast = useToast()
  const confirm = useConfirm()
  const now = useNow()
  const settings = useAsync(() => api.bmcSettings(s.id), [s.id])
  const info = useAsync<BmcInfo>(() => api.bmcInfo(s.id), [s.id])
  const vmedia = useAsync<VmediaStatus>(() => api.vmedia(s.id), [s.id])
  usePolling(info.reload, 30000, true)
  const [busy, setBusy] = useState<string | null>(null)
  const [rotated, setRotated] = useState<{ username: string; password: string; verified: boolean; stored: boolean; error: string | null } | null>(null)
  const [installing, setInstalling] = useState(false)
  const [test, setTest] = useState<BmcTestReport | null>(null)

  const chassis = info.data?.chassis ?? {}
  const policy = chassis.power_restore_policy

  async function act(key: string, fn: () => Promise<unknown>, success?: string) {
    setBusy(key)
    await toast.run(fn, success)
    setBusy(null)
  }

  async function identify(seconds: number, force = false) {
    const led = await toast.run(() => api.identify(s.id, force ? { force: true } : { seconds }))
    if (led) toast.ok(led.led === 'off' ? 'Locator LED off' : led.led === 'on' ? 'Locator LED on until switched off' : `Locator LED blinking for ${led.led}`)
  }

  async function resetBmc() {
    if (!(await confirm({ title: 'Reset the BMC?', body: 'The CIMC reboots. The server keeps running, but IPMI, Redfish, the consoles and the web UI drop for one to two minutes. Use this when the BMC has stopped answering or the KVM is stuck.', confirmLabel: 'Reset BMC', danger: true }))) return
    await act('reset', () => api.bmcReset(s.id), 'BMC reset sent; give it a minute or two')
  }

  async function testConnection() {
    setTest(null)
    await act('test', async () => {
      const report = await api.testBmc(s.id)
      setTest(report)
      if (report.cipher_saved) {
        await settings.reload()
        await refresh()
      }
      if (report.ok) toast.ok(report.verdict)
      else toast.error(report.verdict)
    })
  }

  async function rotatePassword() {
    if (!(await confirm({
      title: 'Rotate the IPMI password?',
      body: 'A new 16-character password is set on the BMC for the user the platform logs in as, proven with a fresh session, then stored. Anything else that uses the old password (a monitoring system, a saved KVM login) stops working.',
      confirmLabel: 'Rotate password',
      danger: true,
    }))) return
    setBusy('password')
    const result = await toast.run(() => api.rotateBmcPassword(s.id))
    setBusy(null)
    if (result) {
      setRotated(result)
      await settings.reload()
    }
  }

  async function setPolicy(value: string) {
    await act('policy', () => api.setPowerPolicy(s.id, value), `Power restore policy: ${value}`)
    await info.reload()
  }

  async function prepare() {
    await act('prepare', async () => {
      const job = await api.prepareBmc(s.id)
      await refresh()
      const done = await waitForJob(job.id)
      await refresh()
      await info.reload()
      toast.ok(done.stage ?? 'BMC prepared')
    })
  }

  async function eject() {
    await act('eject', async () => {
      const job = await api.vmediaEject(s.id)
      await refresh()
      await waitForJob(job.id)
      await refresh()
      await vmedia.reload()
    }, 'Virtual media ejected')
  }

  const mounted = vmedia.data?.media.find((m) => m.inserted)

  return (
    <>
      <div className="grid cols-2">
        <Card
          title="How the platform reaches this BMC"
          actions={<button className="sm" disabled={busy === 'test'} onClick={testConnection} title="Try HTTPS, the XML API and IPMI with each cipher suite, and show what each said">{busy === 'test' ? <Spinner /> : <Activity />}Test connection</button>}
          note={settings.data && !settings.data.credential_resolves ? undefined : 'Edit the protocol, ports, cipher suite and credential ref with the Edit button at the top of the page.'}
        >
          {settings.data && !settings.data.credential_resolves && (
            <Banner kind="error">The credential ref <code>{settings.data.credential_ref}</code> does not resolve. Nothing on this page works until it does.</Banner>
          )}
          <KV items={[
            ['Address', <span className="mono">{s.cimc_ip}</span>],
            ['Protocol', settings.data ? <span>{settings.data.protocol.toUpperCase()} <span className="faint small">{settings.data.protocol === 'auto' ? '(IPMI first, Redfish if IPMI fails)' : ''}</span></span> : null],
            ['Ports', settings.data ? <span className="mono">IPMI {settings.data.ipmi_port} · HTTPS {settings.data.redfish_port}</span> : null],
            ['Cipher suite', settings.data ? <span>{settings.data.ipmi_cipher_suite} <span className="faint small">· {settings.data.cipher_source === 'server' ? 'set for this server' : 'platform default'}</span></span> : null],
            ['Credential ref', settings.data ? <span className="row" style={{ gap: 6 }}><span className="mono">{settings.data.credential_ref}</span><Pill value={settings.data.credential_resolves ? 'ok' : 'critical'} /></span> : null],
            ['IPMI user', settings.data?.username ? <span className="mono">{settings.data.username}</span> : null],
          ]} />
          {test && (
            <div style={{ marginTop: 12 }}>
              <Banner kind={test.ok ? 'info' : 'error'}>
                <div>{test.verdict}</div>
                {test.hint && <div className="small" style={{ marginTop: 4 }}>{test.hint}</div>}
              </Banner>
              <div className="stack" style={{ gap: 6, marginTop: 8 }}>
                {test.checks.map((c) => (
                  <details key={c.name} className="small">
                    <summary style={{ display: 'flex', gap: 8, cursor: 'pointer', alignItems: 'flex-start', listStyle: 'none' }}>
                      {c.ok ? <CheckCircle2 style={{ color: 'var(--ok, #2e9e5b)', flex: 'none' }} /> : <XCircle style={{ color: 'var(--crit)', flex: 'none' }} />}
                      <span>{c.summary}</span>
                    </summary>
                    {c.raw && <pre className="mono small" style={{ whiteSpace: 'pre-wrap', margin: '6px 0 0 24px' }}>{c.raw}</pre>}
                    {c.hint && <div className="subtle" style={{ margin: '4px 0 0 24px' }}>{c.hint}</div>}
                  </details>
                ))}
              </div>
            </div>
          )}
        </Card>

        <Card
          title="Controller"
          actions={<button className="ghost icon sm" onClick={() => info.reload()} title="Re-read from the BMC"><RefreshCw /></button>}
          note={info.error ? `Could not read: ${info.error}` : info.data ? `Read ${relativeTime(info.data.checked_at, now)} · refreshes every 30 s` : 'Reading…'}
        >
          <KV items={[
            ['Firmware', info.data?.mc.firmware ? <span className="mono">{info.data.mc.firmware}</span> : null],
            ['IPMI version', info.data?.mc.ipmi_version],
            ['Manufacturer', info.data?.mc.manufacturer],
            ['Product', info.data?.mc.product],
            ['System power', chassis.system_power ? <Pill value={chassis.system_power} /> : null],
            ['Last power event', chassis.last_power_event || null],
            ['Faults', info.data ? (faults(chassis).length ? <span style={{ color: 'var(--crit)' }}>{faults(chassis).join(', ')}</span> : <span className="subtle">none reported</span>) : null],
            ['Power restore policy', info.data ? (
              <span className="row" style={{ gap: 6 }}>
                <select value={policy ?? ''} disabled={busy === 'policy'} onChange={(e) => setPolicy(e.target.value)} style={{ width: 'auto' }}>
                  {!['always-on', 'always-off', 'previous'].includes(policy ?? '') && <option value={policy ?? ''}>{policy || 'unknown'}</option>}
                  <option value="always-on">always on</option>
                  <option value="always-off">always off</option>
                  <option value="previous">previous state</option>
                </select>
                <span className="faint small">after a mains outage</span>
              </span>
            ) : null],
          ]} />
        </Card>
      </div>

      <div className="grid cols-2">
        <Card title="Tools" icon={<Wrench />}>
          <div className="stack" style={{ gap: 12 }}>
            <div className="spread">
              <div>
                <div className="strong">Prepare BMC</div>
                <div className="subtle small">One click sets everything the platform needs in the CIMC: IPMI over LAN, Serial-over-LAN, BIOS console redirection, virtual media, KVM, Redfish, PXE on the LAN ports, the boot order (disk first, PXE available) and NTP. Then it proves IPMI and SOL work. Anything the firmware rejects is listed in the job log; the CIMC's own network settings are never touched. It runs when a server is added; re-running it only writes settings that have drifted.</div>
                <div className="small" style={{ marginTop: 4 }}>{s.bmc_prepared_at ? <span className="subtle">Last run {relativeTime(s.bmc_prepared_at, now)}</span> : <span className="pill warning">never run</span>}</div>
              </div>
              <button disabled={Boolean(activeJob) || busy === 'prepare'} onClick={prepare}>{busy === 'prepare' ? <Spinner /> : <Wrench />}Prepare BMC</button>
            </div>
            <hr className="divider" style={{ margin: 0 }} />
            <div className="spread">
              <div>
                <div className="strong">Locator LED</div>
                <div className="subtle small">Blink the chassis identify LED so remote hands can find this box in the rack.</div>
              </div>
              <div className="btn-group">
                <button onClick={() => identify(60)}><Lightbulb />1 min</button>
                <button onClick={() => identify(255)}>4 min</button>
                <button onClick={() => identify(0, true)}>On</button>
                <button onClick={() => identify(0)}>Off</button>
              </div>
            </div>
            <hr className="divider" style={{ margin: 0 }} />
            <div className="spread">
              <div>
                <div className="strong">Reset BMC</div>
                <div className="subtle small">Cold-reset the CIMC when it stops answering. The host keeps running.</div>
              </div>
              <button className="danger" disabled={busy === 'reset'} onClick={resetBmc}><RotateCcw />Reset BMC</button>
            </div>
            <hr className="divider" style={{ margin: 0 }} />
            <div className="spread">
              <div>
                <div className="strong">Rotate IPMI password</div>
                <div className="subtle small">Set a new password on the BMC for the platform's user, verify it, store it. Do this after a customer leaves.</div>
              </div>
              <button className="danger" disabled={busy === 'password'} onClick={rotatePassword}>{busy === 'password' ? <Spinner /> : <KeyRound />}Rotate</button>
            </div>
          </div>
        </Card>

        <Card
          title="Virtual media"
          icon={<Disc3 />}
          actions={<button className="ghost icon sm" onClick={() => vmedia.reload()} title="Re-read"><RefreshCw /></button>}
          note={vmedia.data && !vmedia.data.supported ? `Not available: ${vmedia.data.error}` : 'Images come from the image store on the management server, served over HTTP to the BMC.'}
        >
          {vmedia.data?.supported ? (
            mounted ? (
              <KV items={[
                ['Mounted', <span className="mono">{mounted.image_name ?? mounted.image ?? mounted.name}</span>],
                ['Slot', mounted.name ?? mounted.id],
                ['Source', mounted.image ? <span className="mono small">{mounted.image}</span> : null],
              ]} />
            ) : (
              <p className="subtle">Nothing mounted. {vmedia.data.media.length} slot{vmedia.data.media.length === 1 ? '' : 's'}: {vmedia.data.media.map((m) => `${m.name ?? m.id} (${m.media_types.join('/')})`).join(', ') || 'none exposed'}.</p>
            )
          ) : vmedia.loading ? (
            <p className="subtle"><Spinner /> Asking the BMC…</p>
          ) : (
            <p className="subtle">Virtual media needs Redfish. {s.bmc_protocol === 'ipmi' ? 'This server is pinned to IPMI; set it to auto in Edit.' : ''}</p>
          )}
          <div className="row" style={{ marginTop: 12 }}>
            <button className="primary" disabled={Boolean(activeJob)} onClick={() => setInstalling(true)}><Disc3 />Install from image</button>
            <button disabled={Boolean(activeJob) || busy === 'eject' || !vmedia.data?.supported} onClick={eject}>{busy === 'eject' ? <Spinner /> : null}Eject</button>
          </div>
        </Card>
      </div>

      <Card title="IPMI users on the BMC" flush note="Users the BMC knows about, from its channel 1 user table. The platform logs in as the one in the credential.">
        {!info.data ? (
          <Empty>{info.error ? 'Not available.' : 'Reading…'}</Empty>
        ) : info.data.users.length === 0 ? (
          <Empty>No named users reported.</Empty>
        ) : (
          <table className="compact">
            <thead><tr><th>ID</th><th>Name</th><th>Privilege</th><th>IPMI messaging</th></tr></thead>
            <tbody>
              {info.data.users.map((u) => (
                <tr key={u.id}>
                  <td className="num faint">{u.id}</td>
                  <td className="mono">{u.name}{u.name === settings.data?.username && <span className="pill info" style={{ marginLeft: 8 }}>platform</span>}</td>
                  <td>{label(u.privilege.toLowerCase())}</td>
                  <td><Pill value={u.ipmi_messaging ? 'ok' : 'off'} className="" /></td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>

      {rotated && (
        <Modal
          title="IPMI password rotated"
          onClose={() => setRotated(null)}
          footer={<button className="primary" onClick={() => setRotated(null)}>Done</button>}
        >
          {rotated.stored ? (
            <Banner kind="info">Changed on the BMC, verified with a fresh session, and stored. The platform is already using it.</Banner>
          ) : (
            <Banner kind="error">{rotated.error ?? 'The new password could not be stored.'} Record it now: it is not shown again.</Banner>
          )}
          <KV wide items={[
            ['User', <span className="mono">{rotated.username}</span>],
            ['New password', (
              <span className="row" style={{ gap: 6 }}>
                <code style={{ fontSize: 14 }}>{rotated.password}</code>
                <button className="ghost icon sm" title="Copy" onClick={() => { void navigator.clipboard?.writeText(rotated.password); toast.ok('Copied') }}><Copy /></button>
              </span>
            )],
            ['Verified', rotated.verified ? 'yes, a new session logged in with it' : `no: ${rotated.error}`],
          ]} />
        </Modal>
      )}

      {installing && (
        <InstallFromImageModal serverId={s.id} onClose={() => setInstalling(false)} onDone={async (m) => { setInstalling(false); toast.ok(m); await refresh(); await vmedia.reload() }} />
      )}
    </>
  )
}

function faults(chassis: Record<string, string>): string[] {
  const out: string[] = []
  for (const [key, value] of Object.entries(chassis)) {
    if (/fault|overload|intrusion/.test(key) && !['false', 'inactive', 'none', ''].includes(value.toLowerCase())) out.push(label(key))
  }
  return out
}

export { formatBytes }
