import { useEffect, useRef, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { consoleUrl } from '../api'
import { Banner } from '../components'

/**
 * Serial console.
 *
 * A deliberately plain terminal: it renders the byte stream and sends
 * keystrokes, nothing more. A full terminal emulator is worth adding when
 * customers start running curses programs over SOL, and not before.
 */
export default function Console() {
  const { id = '' } = useParams()
  const [status, setStatus] = useState<'connecting' | 'open' | 'closed'>('connecting')
  const [error, setError] = useState<string | null>(null)
  const [output, setOutput] = useState('')
  const socketRef = useRef<WebSocket | null>(null)
  const screenRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const socket = new WebSocket(consoleUrl(id))
    socket.binaryType = 'arraybuffer'
    socketRef.current = socket

    socket.onopen = () => setStatus('open')
    socket.onmessage = (event) => {
      const text =
        typeof event.data === 'string'
          ? event.data
          : new TextDecoder().decode(new Uint8Array(event.data))
      // Cap the scrollback. An install streams a lot of output, and an
      // unbounded string eventually makes the tab unusable.
      setOutput((current) => (current + text).slice(-200_000))
    }
    socket.onerror = () => setError('Console connection failed.')
    socket.onclose = (event) => {
      setStatus('closed')
      if (event.code === 1008) {
        setError('Not authorised for this console.')
      }
    }

    return () => socket.close()
  }, [id])

  // Follow the tail as output arrives.
  useEffect(() => {
    screenRef.current?.scrollTo(0, screenRef.current.scrollHeight)
  }, [output])

  function onKeyDown(event: React.KeyboardEvent) {
    const socket = socketRef.current
    if (!socket || socket.readyState !== WebSocket.OPEN) return

    let data: string | null = null
    if (event.key === 'Enter') data = '\r'
    else if (event.key === 'Backspace') data = '\x7f'
    else if (event.key === 'Tab') data = '\t'
    else if (event.key === 'Escape') data = '\x1b'
    else if (event.key.length === 1) {
      // Ctrl-C, Ctrl-D and friends are the whole reason anyone opens a serial
      // console, so they have to reach the far end rather than the browser.
      data = event.ctrlKey
        ? String.fromCharCode(event.key.toUpperCase().charCodeAt(0) - 64)
        : event.key
    }

    if (data !== null) {
      event.preventDefault()
      socket.send(data)
    }
  }

  return (
    <main className="page">
      <div className="spread">
        <div>
          <h1>Serial console</h1>
          <p className="subtle">
            Serial-over-LAN, proxied through the platform. The BMC allows one session at a
            time — close this tab when you are done.
          </p>
        </div>
        <div className="row">
          <span className={`pill ${status === 'open' ? 'ok' : status === 'closed' ? 'failed' : 'queued'}`}>
            {status}
          </span>
          <Link className="button" to={`/servers/${id}`}>
            Back to server
          </Link>
        </div>
      </div>

      {error && <Banner kind="error">{error}</Banner>}

      <div
        className="log"
        ref={screenRef}
        tabIndex={0}
        onKeyDown={onKeyDown}
        style={{ maxHeight: '65vh', minHeight: 380, outline: 'none', cursor: 'text' }}
      >
        {output || 'Waiting for output. Click here and press Enter if the console looks idle.'}
      </div>
      <p className="subtle" style={{ fontSize: 13 }}>
        Click the terminal to give it focus. Control characters are forwarded.
      </p>
    </main>
  )
}
