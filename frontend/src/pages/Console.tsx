import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { Terminal } from '@xterm/xterm'
import { FitAddon } from '@xterm/addon-fit'
import '@xterm/xterm/css/xterm.css'
import { consoleUrl } from '../api'
import { Banner } from '../components'
import { PowerControls } from '../power'

type ConsoleState =
  | 'idle'
  | 'connecting'
  | 'connected'
  | 'busy'
  | 'sol_disabled'
  | 'ipmi_unreachable'
  | 'closed'
  | 'error'

const STATE_LABEL: Record<ConsoleState, string> = {
  idle: 'not connected',
  connecting: 'connecting',
  connected: 'connected',
  busy: 'in use elsewhere',
  sol_disabled: 'SOL disabled',
  ipmi_unreachable: 'IPMI unreachable',
  closed: 'closed',
  error: 'error',
}

const ESC = '\x1b'
/** ipmitool escape character on the server side (see app/api/console.py). */
const SOL_ESCAPE = '\x1d'

/**
 * Keys a browser will not let through, or that BIOS console redirection
 * expects in a different form. In VT100+ mode (what Prepare BMC sets) the
 * BIOS takes Esc followed by a digit as a function key: Esc-2 is F2.
 */
const KEYS: Array<{ label: string; title: string; data: string }> = [
  { label: 'F2 Setup', title: 'BIOS setup (sent as Esc-2, the VT100+ form)', data: `${ESC}2` },
  { label: 'F6 Boot menu', title: 'One-time boot menu (Esc-6)', data: `${ESC}6` },
  { label: 'F8 CIMC', title: 'CIMC configuration utility (Esc-8)', data: `${ESC}8` },
  { label: 'F10 Save', title: 'Save and exit BIOS setup (Esc-0)', data: `${ESC}0` },
  { label: 'F12 PXE', title: 'Network boot (Esc-@)', data: `${ESC}@` },
  { label: 'Esc', title: 'Escape', data: ESC },
  { label: 'Enter', title: 'Carriage return', data: '\r' },
  { label: 'Ctrl-C', title: 'Interrupt', data: '\x03' },
  { label: 'Ctrl-D', title: 'End of input', data: '\x04' },
  {
    label: 'BREAK',
    title: 'Serial BREAK (for Linux Magic SysRq: send BREAK, then the SysRq letter)',
    data: `\r${SOL_ESCAPE}B`,
  },
]

export default function Console({ isAdmin = false }: { isAdmin?: boolean }) {
  const { id = '' } = useParams()
  const screenRef = useRef<HTMLDivElement>(null)
  const termRef = useRef<Terminal | null>(null)
  const fitRef = useRef<FitAddon | null>(null)
  const wsRef = useRef<WebSocket | null>(null)
  /** Bumped on every connect and on unmount; a connect that finds it changed
   *  after its await has been superseded and must not open a socket. Without
   *  this, a double-clicked Reconnect (or React's development double-mount)
   *  opens two SOL sessions: one holds the BMC's only slot, the other gets
   *  the keystrokes. */
  const generation = useRef(0)
  const [state, setState] = useState<ConsoleState>('idle')
  const [detail, setDetail] = useState<string>('')
  const encoder = useRef(new TextEncoder())

  // One terminal for the life of the page; connections come and go.
  useEffect(() => {
    const term = new Terminal({
      cursorBlink: true,
      fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace',
      fontSize: 14,
      scrollback: 5000,
      theme: { background: '#0c0f13', foreground: '#d7dce3' },
    })
    const fit = new FitAddon()
    term.loadAddon(fit)
    term.open(screenRef.current!)
    fit.fit()
    termRef.current = term
    fitRef.current = fit

    const send = (data: string) => {
      const ws = wsRef.current
      if (ws && ws.readyState === WebSocket.OPEN) ws.send(encoder.current.encode(data))
    }
    const onData = term.onData(send)
    const onBinary = term.onBinary((data) => {
      const ws = wsRef.current
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(Uint8Array.from(data, (c) => c.charCodeAt(0)))
      }
    })
    const onResize = () => fit.fit()
    window.addEventListener('resize', onResize)
    term.focus()

    return () => {
      window.removeEventListener('resize', onResize)
      onData.dispose()
      onBinary.dispose()
      term.dispose()
    }
  }, [])

  const connect = useCallback(
    async (force = false) => {
      const mine = ++generation.current
      const previous = wsRef.current
      wsRef.current = null
      previous?.close()
      setState('connecting')
      setDetail(force ? 'Releasing the other session…' : '')
      let url: string
      try {
        url = await consoleUrl(id, force)
      } catch (e) {
        if (mine !== generation.current) return
        setState('error')
        setDetail(e instanceof Error ? e.message : String(e))
        return
      }
      if (mine !== generation.current) return // superseded while fetching the ticket
      const ws = new WebSocket(url)
      ws.binaryType = 'arraybuffer'
      wsRef.current = ws

      ws.onmessage = (event) => {
        if (wsRef.current !== ws) return
        const term = termRef.current
        if (!term) return
        if (typeof event.data === 'string') {
          try {
            const msg = JSON.parse(event.data) as { type: string; state: ConsoleState; message: string }
            if (msg.type === 'status') {
              setState(msg.state)
              setDetail(msg.message ?? '')
              if (msg.state === 'connected') term.focus()
            }
          } catch {
            /* not a status message */
          }
          return
        }
        term.write(new Uint8Array(event.data as ArrayBuffer))
      }
      ws.onclose = (event) => {
        if (wsRef.current !== ws) return
        setState((current) =>
          ['busy', 'sol_disabled', 'ipmi_unreachable', 'error'].includes(current)
            ? current
            : event.code === 1008
              ? 'error'
              : 'closed',
        )
        if (event.code === 1008) setDetail('Not authorised for this console, or the link expired.')
      }
      ws.onerror = () => {
        if (wsRef.current !== ws) return
        setState('error')
        setDetail('Could not reach the console service.')
      }
    },
    [id],
  )

  useEffect(() => {
    void connect()
    return () => {
      generation.current++ // cancels a connect still waiting for its ticket
      const ws = wsRef.current
      wsRef.current = null
      ws?.close()
    }
  }, [connect])

  function sendKey(data: string) {
    const ws = wsRef.current
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(encoder.current.encode(data))
    termRef.current?.focus()
  }

  const stateClass =
    state === 'connected' ? 'ok' : state === 'connecting' ? 'queued' : state === 'idle' ? '' : 'failed'
  const live = state === 'connected'

  return (
    <main className="page" style={{ maxWidth: 1180 }}>
      <div className="spread">
        <div>
          <h1>Serial console</h1>
          <p className="subtle">
            Serial-over-LAN through the platform. Works from power-on: BIOS, boot loader, kernel,
            login. The BMC allows one viewer at a time.
          </p>
        </div>
        <div className="row">
          <span className={`pill ${stateClass}`}>{STATE_LABEL[state]}</span>
          <Link className="button" to={isAdmin ? `/admin/servers/${id}` : `/servers/${id}`}>
            Back to server
          </Link>
        </div>
      </div>

      <div className="card">
        <PowerControls serverId={id} isAdmin={isAdmin} />
      </div>

      {state === 'busy' && (
        <Banner kind="error">
          {detail || 'Someone else has this console open.'}{' '}
          <button onClick={() => connect(true)}>Take over</button>
        </Banner>
      )}
      {(state === 'sol_disabled' || state === 'ipmi_unreachable' || state === 'error') && (
        <Banner kind="error">
          {detail} <button onClick={() => connect()}>Retry</button>
        </Banner>
      )}
      {state === 'closed' && (
        <Banner kind="info">
          {detail || 'The console session ended.'} <button onClick={() => connect()}>Reconnect</button>
        </Banner>
      )}

      <div className="console-bar">
        {KEYS.map((k) => (
          <button key={k.label} title={k.title} disabled={!live} onClick={() => sendKey(k.data)}>
            {k.label}
          </button>
        ))}
        <span className="sep" />
        <button onClick={() => termRef.current?.clear()} title="Clear the scrollback">
          Clear
        </button>
        <button onClick={() => connect(true)} title="Drop any other session and reconnect">
          Reconnect
        </button>
      </div>

      <div className="console-shell" onClick={() => termRef.current?.focus()}>
        <div className="console-screen" ref={screenRef} />
      </div>

      <p className="subtle" style={{ fontSize: 13, marginTop: 10 }}>
        Nothing on screen? Press Enter. Still nothing after a reset? Console redirection may be
        off in the BIOS: an admin can run <strong>Prepare BMC</strong>, then power-cycle.
        Function keys on your keyboard may be caught by the browser; use the buttons above.
      </p>
    </main>
  )
}
