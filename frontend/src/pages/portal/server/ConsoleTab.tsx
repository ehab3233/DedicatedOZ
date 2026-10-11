import { Copy, ExternalLink, MonitorPlay, XCircle } from 'lucide-react'
import { useState } from 'react'
import { api, type KvmGrant, type KvmStatus } from '../../../api'
import { Banner, Card, KV, Spinner, relativeTime, useConfirm, useNow } from '../../../components'
import SerialConsole from '../../../console/SerialConsole'
import { useAsync } from '../../../hooks'
import { useToast } from '../../../toast'
import { usePortalServer } from './ServerPage'

export default function PortalConsoleTab() {
  const { server } = usePortalServer()
  return (
    <>
      <SerialConsole serverId={server.id} popoutTo={`/servers/${server.id}/console/full`} />
      <div className="grid cols-2" style={{ marginTop: 16 }}>
        <GraphicalConsole serverId={server.id} />
        <Card title="Using the serial console">
          <ul style={{ margin: 0, paddingLeft: 18, color: 'var(--text-2)' }}>
            <li>It shows the server from power-on: BIOS, boot loader, kernel messages and the login prompt.</li>
            <li>Nothing on screen? Press Enter. A boot menu redraws on the next keystroke.</li>
            <li>One viewer at a time. If a session is already open elsewhere you are offered to take it over.</li>
            <li>Function keys are caught by the browser; use the toolbar buttons for F2, F6 and F12.</li>
          </ul>
        </Card>
      </div>
    </>
  )
}

/**
 * The CIMC's own graphical console, reached through a hostname the platform
 * opens for this customer for a few hours, with a one-off login on the CIMC.
 */
function GraphicalConsole({ serverId }: { serverId: string }) {
  const status = useAsync<KvmStatus>(() => api.kvmStatus(serverId), [serverId])
  const toast = useToast()
  const confirm = useConfirm()
  const now = useNow()
  const [fresh, setFresh] = useState<KvmGrant | null>(null)
  const [busy, setBusy] = useState(false)

  async function open() {
    setBusy(true)
    try {
      const grant = await api.openKvm(serverId)
      setFresh(grant)
      await status.reload()
    } catch (e) {
      toast.error(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  async function close() {
    if (!(await confirm({ title: 'End console access?', body: 'The temporary login is removed from the server and the console address stops working. You can open it again any time.', confirmLabel: 'End access' }))) return
    await toast.run(async () => { await api.closeKvm(serverId); setFresh(null); await status.reload() }, 'Console access ended')
  }

  async function copy(text: string) {
    try { await navigator.clipboard.writeText(text); toast.ok('Copied') } catch { toast.error('Could not copy; select it and copy by hand') }
  }

  const grant = fresh ?? status.data?.grant ?? null
  const title = <span className="row" style={{ gap: 8 }}><MonitorPlay size={16} />Graphical console</span>

  if (status.error) return <Card title={title}><Banner kind="error">{status.error}</Banner></Card>
  if (!status.data) return <Card title={title}><div className="subtle"><Spinner /> Checking…</div></Card>
  if (!status.data.available) {
    return (
      <Card title={title}>
        <p className="subtle" style={{ margin: 0 }}>
          The graphical console is not available on this platform yet. The serial console covers BIOS, boot and login, and rescue mode covers repairs;
          for anything that needs the graphical console, contact support.
        </p>
      </Card>
    )
  }

  return (
    <Card
      title={title}
      actions={grant ? (
        <span className="row" style={{ gap: 6 }}>
          <a className="button sm primary" href={grant.url} target="_blank" rel="noreferrer"><ExternalLink />Open the console</a>
          <button className="sm" onClick={open} disabled={busy}>{busy ? 'Renewing…' : 'New password'}</button>
          <button className="sm danger" onClick={close}><XCircle />End access</button>
        </span>
      ) : (
        <button className="primary" onClick={open} disabled={busy}>{busy ? 'Opening…' : 'Open the graphical console'}</button>
      )}
    >
      {!grant ? (
        <p className="subtle" style={{ margin: 0 }}>
          Video from power-on, keyboard and mouse, and virtual media to boot your own ISO. Opening it gives you a private address and a one-off login
          to the server's management controller for {status.data.grant?.hours ?? 4} hours; both are removed afterwards, and you can end them sooner.
        </p>
      ) : (
        <>
          <KV items={[
            ['Address', <a href={grant.url} target="_blank" rel="noreferrer" className="mono">{grant.hostname}</a>],
            ['Username', <span className="row" style={{ gap: 6 }}><span className="mono">{grant.username}</span><button className="sm ghost" onClick={() => copy(grant.username)}><Copy /></button></span>],
            ['Password', grant.password ? (
              <span className="row" style={{ gap: 6 }}><code className="mono">{grant.password}</code><button className="sm ghost" onClick={() => copy(grant.password!)}><Copy /></button></span>
            ) : <span className="subtle">shown when opened; press New password for another</span>],
            ['Valid', `until ${new Date(grant.expires_at).toLocaleString()} (${relativeTime(grant.expires_at, now)})`],
          ]} />
          <ol className="subtle small" style={{ margin: '12px 0 0', paddingLeft: 18 }}>
            <li>Open the console address in a new tab; it shows the server's management controller.</li>
            <li>Sign in with the username and password above.</li>
            <li>Use its <strong>Launch KVM → HTML based</strong>. Virtual media is in the viewer's own menu.</li>
          </ol>
          {grant.password && <div className="faint small" style={{ marginTop: 8 }}>Copy the password now: it is not shown again, though you can set a new one at any time.</div>}
        </>
      )}
    </Card>
  )
}
