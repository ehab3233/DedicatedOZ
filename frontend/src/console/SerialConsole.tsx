import { Terminal } from '@xterm/xterm'
import { FitAddon } from '@xterm/addon-fit'
import '@xterm/xterm/css/xterm.css'
import { Eraser, Maximize2, RefreshCw } from 'lucide-react'
import { useCallback, useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { consoleUrl } from '../api'
import { Dot } from '../components'

export type ConsoleState =
  | 'idle'
  | 'connecting'
  | 'connected'
  | 'busy'
  | 'sol_disabled'
  | 'ipmi_unreachable'
  | 'closed'
  | 'error'

const STATE_LABEL: Record<ConsoleState, string> = {
  idle: 'Not connected',
  connecting: 'Connecting',
  connected: 'Connected',
  busy: 'In use elsewhere',
  sol_disabled: 'SOL disabled',
  ipmi_unreachable: 'IPMI unreachable',
  closed: 'Closed',
  error: 'Error',
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
  { label: 'F2', title: 'BIOS setup (sent as Esc-2, the VT100+ form)', data: `${ESC}2` },
  { label: 'F6', title: 'One-time boot menu (Esc-6)', data: `${ESC}6` },
  { label: 'F8', title: 'CIMC configuration utility (Esc-8)', data: `${ESC}8` },
  { label: 'F10', title: 'Save and exit BIOS setup (Esc-0)', data: `${ESC}0` },
  { label: 'F12', title: 'Network boot (Esc-@)', data: `${ESC}@` },
  { label: 'Esc', title: 'Escape', data: ESC },
  { label: 'Enter', title: 'Carriage return', data: '\r' },
  { label: '^C', title: 'Ctrl-C: interrupt', data: '\x03' },
  { label: '^D', title: 'Ctrl-D: end of input', data: '\x04' },
  {
    label: 'BREAK',
    title: 'Serial BREAK (for Linux Magic SysRq: send BREAK, then the SysRq letter)',
    data: `\r${SOL_ESCAPE}B`,
  },
]

/**
 * The Serial-over-LAN terminal. Used embedded in the server page and on the
 * full-page console. One xterm for the life of the component; websocket
 * connections come and go.
 */
export default function SerialConsole({
  serverId,
  fill = false,
  popoutTo,
}: {
  serverId: string
  /** Fill the viewport height rather than a card-sized box. */
  fill?: boolean
  /** Where the pop-out button goes; omitted on the full page. */
  popoutTo?: string
}) {
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

  useEffect(() => {
    const term = new Terminal({
      cursorBlink: true,
      fontFamily: "'JetBrains Mono Variable', ui-monospace, SFMono-Regular, Menlo, Consolas, monospace",
      fontSize: 13,
      scrollback: 5000,
      theme: { background: '#0b0e12', foreground: '#d9dee6', cursor: '#d9dee6' },
    })
    const fit = new FitAddon()
    term.loadAddon(fit)
    term.open(screenRef.current!)
    try {
      fit.fit()
    } catch {
      /* not laid out yet; the observer below fits it when it is */
    }
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
    // Refit whenever the box changes size: a sidebar closing, a tab
    // switching, a window resize.
    const observer = new ResizeObserver(() => {
      try {
        fit.fit()
      } catch {
        /* not laid out yet */
      }
    })
    observer.observe(screenRef.current!)
    term.focus()

    return () => {
      observer.disconnect()
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
        url = await consoleUrl(serverId, force)
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
    [serverId],
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

  const dotState = state === 'connected' ? 'ok' : state === 'connecting' ? 'warning' : state === 'idle' ? 'unknown' : 'critical'
  const live = state === 'connected'

  return (
    <div className={`console ${fill ? 'fill' : ''}`}>
      <div className="console-toolbar">
        <span className="state">
          <Dot state={dotState} pulse={state === 'connecting'} />
          {STATE_LABEL[state]}
        </span>
        <span className="sep" />
        {KEYS.map((k) => (
          <button key={k.label} className="ghost" title={k.title} disabled={!live} onClick={() => sendKey(k.data)}>
            {k.label}
          </button>
        ))}
        <span className="sep" />
        <button className="ghost" onClick={() => termRef.current?.clear()} title="Clear the scrollback"><Eraser />Clear</button>
        <button className="ghost" onClick={() => connect(true)} title="Drop any other session and reconnect"><RefreshCw />Reconnect</button>
        {popoutTo && (
          <Link className="button ghost" to={popoutTo} title="Open the console on its own page" style={{ marginLeft: 'auto', height: 26, fontSize: 12 }}>
            <Maximize2 />Full page
          </Link>
        )}
      </div>

      <div className="console-screen" ref={screenRef} onClick={() => termRef.current?.focus()} />

      {state === 'busy' && (
        <div className="console-overlay error">
          {detail || 'Someone else has this console open.'}
          <button className="sm" onClick={() => connect(true)}>Take over</button>
        </div>
      )}
      {(state === 'sol_disabled' || state === 'ipmi_unreachable' || state === 'error') && (
        <div className="console-overlay error">
          {detail}
          <button className="sm" onClick={() => connect()}>Retry</button>
        </div>
      )}
      {state === 'closed' && (
        <div className="console-overlay">
          {detail || 'The console session ended.'}
          <button className="sm" onClick={() => connect()}>Reconnect</button>
        </div>
      )}
    </div>
  )
}
