import { ExternalLink, Maximize2, MonitorPlay, RefreshCw } from 'lucide-react'
import { useCallback, useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../../../api'
import { Banner, Spinner } from '../../../components'
import { useToast } from '../../../toast'

export type KvmLaunch = Awaited<ReturnType<typeof api.launchKvm>>

/**
 * The CIMC's own vKVM, opened with single-use launch tokens. Video, keyboard
 * and mouse, and its own virtual media. The browser talks to the CIMC
 * directly for that, so it must be able to reach the CIMC's address.
 */
export function useKvmLauncher(serverId: string) {
  const toast = useToast()
  const [busy, setBusy] = useState(false)
  const [java, setJava] = useState<string | null>(null)

  /** Tokens and links, for showing the viewer inside the page. */
  const connect = useCallback(async (): Promise<KvmLaunch> => {
    setBusy(true)
    try {
      return await api.launchKvm(serverId)
    } finally {
      setBusy(false)
    }
  }, [serverId])

  /** The viewer in a tab of its own. */
  async function launch() {
    setBusy(true)
    // Open the window synchronously, inside the click, or popup blockers
    // swallow it; point it at the viewer once the tokens arrive.
    const win = window.open('about:blank', '_blank')
    try {
      const result = await api.launchKvm(serverId)
      if (result.tokens_unsupported) {
        // This CIMC build cannot issue launch tokens, so the next best thing
        // is its own web UI: land on it, log in, click Launch KVM there.
        if (win) win.location.href = result.cimc
        setJava(null)
        toast.info(
          `CIMC ${result.firmware ?? ''} does not issue KVM tokens, so the CIMC web UI opened instead: `
          + 'log in and use its Launch KVM.',
        )
      } else if (result.html5) {
        if (win) win.location.href = result.html5
        setJava(null)
      } else {
        win?.close()
        setJava(result.java)
        toast.info(noViewerMessage(result))
      }
    } catch (e) {
      win?.close()
      toast.error(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }
  return { launch, connect, busy, java }
}

function noViewerMessage(result: KvmLaunch): string {
  const probe = (result.probe ?? []).map((p) => `${p.path} → ${p.status ?? p.error ?? '?'}`).join(', ')
  return (
    `CIMC ${result.firmware ?? ''} serves no HTML5 viewer at the paths the panel knows${probe ? ` (${probe})` : ''}. `
    + 'Use the Java launcher, or open the CIMC, use its own Launch KVM → HTML based, copy the link its popup shows, '
    + 'and set DOZ_KVM_URL_TEMPLATE to that link with {host}, {tkn1} and {tkn2} in place of the address and tokens.'
  )
}

/** Header button: the console tab with the viewer in it. */
export function KvmButton({ serverId, cimcIp, port }: { serverId: string; cimcIp: string; port: number | null }) {
  const cimcUrl = `https://${cimcIp}${port ? `:${port}` : ''}/`
  return (
    <span className="row" style={{ gap: 6 }}>
      <Link className="button primary" to={`/admin/servers/${serverId}/console?view=kvm`} title="The CIMC's graphical console, inside the Console tab: video, keyboard, mouse, virtual media">
        <MonitorPlay />KVM
      </Link>
      <a className="button ghost" href={cimcUrl} target="_blank" rel="noreferrer" title="The CIMC's own web interface"><ExternalLink />CIMC</a>
    </span>
  )
}

/**
 * The viewer inside the panel. Tokens are single-use and expire in about a
 * minute, so the frame is pointed at the viewer the moment they arrive, and
 * Reconnect fetches new ones. A CIMC that forbids framing gets a tab instead.
 */
export function KvmViewer({ serverId, cimcIp, port, autoConnect = false }: {
  serverId: string; cimcIp: string; port: number | null; autoConnect?: boolean
}) {
  const { launch, connect, busy, java } = useKvmLauncher(serverId)
  const frame = useRef<HTMLIFrameElement>(null)
  const started = useRef(false)
  const [result, setResult] = useState<KvmLaunch | null>(null)
  const [url, setUrl] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const cimcUrl = `https://${cimcIp}${port ? `:${port}` : ''}/`

  const open = useCallback(async () => {
    setError(null)
    try {
      const r = await connect()
      setResult(r)
      setUrl(r.html5 && r.embeddable !== false ? r.html5 : null)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }, [connect])

  useEffect(() => {
    if (autoConnect && !started.current) {
      started.current = true
      void open()
    }
  }, [autoConnect, open])

  const framed = Boolean(url)
  return (
    <div className="stack" style={{ gap: 10 }}>
      <div className="toolbar" style={{ marginBottom: 0 }}>
        <span className="subtle small">
          The CIMC's own viewer: video from POST onwards, keyboard and mouse passthrough, and its virtual media for an ISO on your machine.
          Your browser connects to <span className="mono">{cimcIp}</span> directly.
        </span>
        <span className="spacer" />
        <button className="sm primary" onClick={open} disabled={busy}>{busy ? <Spinner /> : <RefreshCw />}{framed ? 'Reconnect' : 'Connect'}</button>
        <button className="sm" onClick={() => frame.current?.requestFullscreen?.()} disabled={!framed} title="The viewer alone, full screen (Esc to leave)"><Maximize2 />Full screen</button>
        <button className="sm" onClick={launch} disabled={busy} title="The viewer in a tab of its own, with fresh tokens"><ExternalLink />Open in a tab</button>
        {java && <a className="button sm" href={java}>Java KVM (.jnlp)</a>}
      </div>

      {error && <Banner kind="error">{error}</Banner>}
      {result?.tokens_unsupported && (
        <Banner kind="info">CIMC {result.firmware ?? ''} does not issue KVM launch tokens. <a href={result.cimc} target="_blank" rel="noreferrer">Open the CIMC</a>, log in and use its own Launch KVM.</Banner>
      )}
      {result && !result.tokens_unsupported && !result.html5 && (
        <Banner kind="warning">{noViewerMessage(result)}</Banner>
      )}
      {result?.html5 && result.embeddable === false && (
        <Banner kind="info">This CIMC does not allow its viewer inside another page, so it opens in a tab of its own. Use <strong>Open in a tab</strong>.</Banner>
      )}

      {framed ? (
        <iframe ref={frame} src={url ?? undefined} title="KVM" className="kvm-frame" allow="fullscreen; clipboard-read; clipboard-write" />
      ) : (
        <div className="kvm-frame kvm-placeholder">
          {busy ? <><Spinner /> Getting tokens from the CIMC…</> : <><MonitorPlay /> Press Connect to open the graphical console here.</>}
        </div>
      )}
      <p className="faint small" style={{ margin: 0 }}>
        First time in this browser? Open <a href={cimcUrl} target="_blank" rel="noreferrer">the CIMC</a> once and accept its certificate, or the frame stays blank.
        Launch tokens last about a minute: if the viewer asks you to log in, press Reconnect.
      </p>
    </div>
  )
}
