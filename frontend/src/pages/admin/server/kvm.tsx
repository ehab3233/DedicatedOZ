import { ExternalLink, MonitorPlay } from 'lucide-react'
import { useState } from 'react'
import { api } from '../../../api'
import { useToast } from '../../../toast'

/**
 * One-click vKVM. Asks the CIMC for single-use launch tokens and opens its
 * own HTML5 viewer, which carries video, keyboard and mouse, and its own
 * virtual media. The browser talks to the CIMC directly for that, so it
 * must be able to reach the CIMC's address.
 */
export function useKvmLauncher(serverId: string) {
  const toast = useToast()
  const [busy, setBusy] = useState(false)
  const [java, setJava] = useState<string | null>(null)

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
        const probe = (result.probe ?? []).map((p) => `${p.path} → ${p.status ?? p.error ?? '?'}`).join(', ')
        toast.info(
          `CIMC ${result.firmware ?? ''} serves no HTML5 viewer at the paths the panel knows${probe ? ` (${probe})` : ''}. `
          + 'Use the Java launcher, or open the CIMC, use its own Launch KVM → HTML based, copy the link its popup shows, '
          + 'and set DOZ_KVM_URL_TEMPLATE to that link with {host}, {tkn1} and {tkn2} in place of the address and tokens.',
        )
      }
    } catch (e) {
      win?.close()
      toast.error(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }
  return { launch, busy, java }
}

export function KvmButton({ serverId, cimcIp, port }: { serverId: string; cimcIp: string; port: number | null }) {
  const { launch, busy, java } = useKvmLauncher(serverId)
  const cimcUrl = `https://${cimcIp}${port ? `:${port}` : ''}/`
  return (
    <span className="row" style={{ gap: 6 }}>
      <button className="primary" onClick={launch} disabled={busy} title="Open the CIMC's graphical console: video, keyboard, mouse, virtual media">
        <MonitorPlay />{busy ? 'Getting tokens…' : 'KVM'}
      </button>
      {java && <a className="button" href={java}>Java KVM (.jnlp)</a>}
      <a className="button ghost" href={cimcUrl} target="_blank" rel="noreferrer" title="The CIMC's own web interface"><ExternalLink />CIMC</a>
    </span>
  )
}
