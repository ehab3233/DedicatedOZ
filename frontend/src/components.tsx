import { AlertTriangle, ChevronRight, Info, X } from 'lucide-react'
import type { ReactNode } from 'react'
import { createContext, useCallback, useContext, useEffect, useRef, useState } from 'react'
import { Link, NavLink } from 'react-router-dom'

// ---------------------------------------------------------------------------
// Status
// ---------------------------------------------------------------------------

export function Pill({ value, className = '' }: { value: string | null | undefined; className?: string }) {
  if (!value) return <span className={`pill ${className}`}>unknown</span>
  return <span className={`pill ${value} ${className}`}>{value.replace(/_/g, ' ')}</span>
}

export function Dot({ state, pulse = false }: { state: string | null | undefined; pulse?: boolean }) {
  return <span className={`dot ${state ?? 'unknown'} ${pulse ? 'pulse' : ''}`} />
}

export function Banner({ kind, children }: { kind: 'error' | 'info' | 'warning'; children: ReactNode }) {
  return (
    <div className={`banner ${kind}`}>
      {kind === 'info' ? <Info /> : <AlertTriangle />}
      <div className="banner-body">{children}</div>
    </div>
  )
}

export function Progress({ value, active = false }: { value: number; active?: boolean }) {
  return (
    <div className={`progress ${active ? 'striped' : ''}`}>
      <div style={{ width: `${Math.max(0, Math.min(100, value))}%` }} />
    </div>
  )
}

export function Spinner() {
  return <span className="spinner" />
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="empty">{children}</div>
}

export function Skeleton({ rows = 3 }: { rows?: number }) {
  return (
    <div className="stack" style={{ padding: 16 }}>
      {Array.from({ length: rows }, (_, i) => (
        <div key={i} className="skeleton" style={{ width: `${90 - i * 12}%` }} />
      ))}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Layout primitives
// ---------------------------------------------------------------------------

export function Card({
  title,
  icon,
  actions,
  children,
  flush = false,
  note,
  className = '',
  style,
}: {
  title?: ReactNode
  icon?: ReactNode
  actions?: ReactNode
  children: ReactNode
  flush?: boolean
  note?: ReactNode
  className?: string
  style?: React.CSSProperties
}) {
  return (
    <section className={`card ${className}`} style={style}>
      {(title || actions) && (
        <header className="card-header">
          <div className="card-title">{icon}{title}</div>
          {actions && <div className="card-actions">{actions}</div>}
        </header>
      )}
      {flush ? children : <div className="card-body">{children}</div>}
      {note && <div className="card-note">{note}</div>}
    </section>
  )
}

export function Stat({
  label,
  value,
  sub,
  tone,
  to,
}: {
  label: string
  value: ReactNode
  sub?: ReactNode
  tone?: 'ok' | 'warn' | 'crit'
  to?: string
}) {
  const body = (
    <div className={`card stat ${tone ?? ''}`}>
      <div className="stat-label">{label}</div>
      <div className="stat-value">{value}</div>
      {sub && <div className="stat-sub">{sub}</div>}
    </div>
  )
  return to ? <Link to={to} style={{ color: 'inherit', textDecoration: 'none' }}>{body}</Link> : body
}

export function KV({ items, wide = false }: { items: Array<[ReactNode, ReactNode]>; wide?: boolean }) {
  return (
    <dl className={`kv ${wide ? 'wide' : ''}`}>
      {items.map(([k, v], i) => (
        <div key={i} style={{ display: 'contents' }}>
          <dt>{k}</dt>
          <dd>{v ?? <span className="faint">—</span>}</dd>
        </div>
      ))}
    </dl>
  )
}

export function PageHeader({
  title,
  sub,
  crumbs,
  actions,
}: {
  title: ReactNode
  sub?: ReactNode
  crumbs?: Array<{ label: string; to?: string }>
  actions?: ReactNode
}) {
  return (
    <div className="page-header">
      <div style={{ minWidth: 0 }}>
        {crumbs && (
          <div className="crumbs">
            {crumbs.map((c, i) => (
              <span key={i} className="row" style={{ gap: 6 }}>
                {i > 0 && <ChevronRight />}
                {c.to ? <Link to={c.to}>{c.label}</Link> : <span>{c.label}</span>}
              </span>
            ))}
          </div>
        )}
        <h1 className="page-title">{title}</h1>
        {sub && <div className="page-sub">{sub}</div>}
      </div>
      {actions && <div className="page-actions">{actions}</div>}
    </div>
  )
}

export function Tabs({ items }: { items: Array<{ to: string; label: string; icon?: ReactNode; end?: boolean; count?: number }> }) {
  return (
    <nav className="tabs">
      {items.map((t) => (
        <NavLink key={t.to} to={t.to} end={t.end} className={({ isActive }) => `tab ${isActive ? 'active' : ''}`}>
          {t.icon}
          {t.label}
          {t.count != null && t.count > 0 && <span className="count">{t.count}</span>}
        </NavLink>
      ))}
    </nav>
  )
}

// ---------------------------------------------------------------------------
// Modal and confirm
// ---------------------------------------------------------------------------

export function Modal({
  title,
  onClose,
  children,
  footer,
  wide = false,
}: {
  title: ReactNode
  onClose: () => void
  children: ReactNode
  footer?: ReactNode
  wide?: boolean
}) {
  // Escape closes. A dialog you cannot dismiss is how people end up
  // clicking the destructive button by accident.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [onClose])

  return (
    <div className="modal-backdrop" onMouseDown={onClose}>
      <div className={`modal ${wide ? 'wide' : ''}`} role="dialog" onMouseDown={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <h3>{title}</h3>
          <button className="ghost icon sm" onClick={onClose} aria-label="Close"><X /></button>
        </div>
        <div className="modal-body">{children}</div>
        {footer && <div className="modal-footer">{footer}</div>}
      </div>
    </div>
  )
}

interface ConfirmOptions {
  title: string
  body?: ReactNode
  confirmLabel?: string
  danger?: boolean
  /** Require this exact text to be typed before the button enables. */
  typed?: string
}

const ConfirmContext = createContext<((opts: ConfirmOptions) => Promise<boolean>) | null>(null)

export function ConfirmProvider({ children }: { children: ReactNode }) {
  const [pending, setPending] = useState<(ConfirmOptions & { resolve: (ok: boolean) => void }) | null>(null)
  const [typed, setTyped] = useState('')

  const confirm = useCallback((opts: ConfirmOptions) => {
    setTyped('')
    return new Promise<boolean>((resolve) => setPending({ ...opts, resolve }))
  }, [])

  const close = (ok: boolean) => {
    pending?.resolve(ok)
    setPending(null)
  }

  const ready = !pending?.typed || typed.trim() === pending.typed

  return (
    <ConfirmContext.Provider value={confirm}>
      {children}
      {pending && (
        <Modal
          title={pending.title}
          onClose={() => close(false)}
          footer={
            <>
              <button onClick={() => close(false)}>Cancel</button>
              <button className={`primary ${pending.danger ? 'danger' : ''}`} disabled={!ready} onClick={() => close(true)} autoFocus={!pending.typed}>
                {pending.confirmLabel ?? 'Confirm'}
              </button>
            </>
          }
        >
          {pending.body && <div style={{ marginBottom: pending.typed ? 14 : 0 }}>{pending.body}</div>}
          {pending.typed && (
            <div className="field" style={{ marginBottom: 0 }}>
              <label>Type <code>{pending.typed}</code> to confirm</label>
              <input className="mono" value={typed} onChange={(e) => setTyped(e.target.value)} autoFocus autoComplete="off" />
            </div>
          )}
        </Modal>
      )}
    </ConfirmContext.Provider>
  )
}

export function useConfirm() {
  const confirm = useContext(ConfirmContext)
  if (!confirm) throw new Error('useConfirm outside ConfirmProvider')
  return confirm
}

// ---------------------------------------------------------------------------
// Hooks
// ---------------------------------------------------------------------------

/** Ticks every second so relative times ("12s ago") stay honest. */
export function useNow(intervalMs = 1000): number {
  const [now, setNow] = useState(Date.now())
  useEffect(() => {
    const t = window.setInterval(() => setNow(Date.now()), intervalMs)
    return () => window.clearInterval(t)
  }, [intervalMs])
  return now
}

export function useDebounced<T>(value: T, ms = 200): T {
  const [v, setV] = useState(value)
  const timer = useRef<number>()
  useEffect(() => {
    window.clearTimeout(timer.current)
    timer.current = window.setTimeout(() => setV(value), ms)
    return () => window.clearTimeout(timer.current)
  }, [value, ms])
  return v
}

// ---------------------------------------------------------------------------
// Formatting
// ---------------------------------------------------------------------------

export function formatBytes(bytes: number | null | undefined): string {
  if (!bytes) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB', 'TB', 'PB']
  const i = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1)
  return `${(bytes / 1024 ** i).toFixed(i === 0 ? 0 : 1)} ${units[i]}`
}

export function formatBits(bps: number): string {
  if (!bps) return '0 bps'
  const units = ['bps', 'Kbps', 'Mbps', 'Gbps']
  const i = Math.min(Math.floor(Math.log(bps) / Math.log(1000)), units.length - 1)
  return `${(bps / 1000 ** i).toFixed(i === 0 ? 0 : 1)} ${units[i]}`
}

export function formatTime(iso: string | null | undefined): string {
  if (!iso) return '—'
  return new Date(iso).toLocaleString(undefined, {
    year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit',
  })
}

export function relativeTime(iso: string | null | undefined, now = Date.now()): string {
  if (!iso) return '—'
  const seconds = Math.max(0, (now - new Date(iso).getTime()) / 1000)
  if (seconds < 5) return 'just now'
  if (seconds < 60) return `${Math.floor(seconds)}s ago`
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`
  return `${Math.floor(seconds / 86400)}d ago`
}

export function label(value: string | null | undefined): string {
  return (value ?? '').replace(/_/g, ' ')
}
