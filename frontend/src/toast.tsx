import { AlertCircle, CheckCircle2, Info, X } from 'lucide-react'
import { createContext, useCallback, useContext, useMemo, useRef, useState, type ReactNode } from 'react'

type Kind = 'ok' | 'error' | 'info'
interface Toast { id: number; kind: Kind; text: string }

interface ToastApi {
  push: (kind: Kind, text: string, ttlMs?: number) => void
  ok: (text: string) => void
  error: (text: string) => void
  info: (text: string) => void
  /** Run an async action, reporting its failure as a toast. Returns undefined on failure. */
  run: <T>(fn: () => Promise<T>, success?: string) => Promise<T | undefined>
}

const ToastContext = createContext<ToastApi | null>(null)

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([])
  const seq = useRef(0)

  const dismiss = useCallback((id: number) => {
    setToasts((current) => current.filter((t) => t.id !== id))
  }, [])

  const push = useCallback(
    (kind: Kind, text: string, ttlMs?: number) => {
      const id = ++seq.current
      setToasts((current) => [...current.slice(-4), { id, kind, text }])
      const ttl = ttlMs ?? (kind === 'error' ? 9000 : 4500)
      window.setTimeout(() => dismiss(id), ttl)
    },
    [dismiss],
  )

  const api = useMemo<ToastApi>(
    () => ({
      push,
      ok: (text) => push('ok', text),
      error: (text) => push('error', text),
      info: (text) => push('info', text),
      run: async (fn, success) => {
        try {
          const result = await fn()
          if (success) push('ok', success)
          return result
        } catch (e) {
          push('error', e instanceof Error ? e.message : String(e))
          return undefined
        }
      },
    }),
    [push],
  )

  return (
    <ToastContext.Provider value={api}>
      {children}
      <div className="toasts" aria-live="polite">
        {toasts.map((t) => (
          <div key={t.id} className={`toast ${t.kind}`}>
            {t.kind === 'ok' ? <CheckCircle2 /> : t.kind === 'error' ? <AlertCircle /> : <Info />}
            <div className="toast-body">{t.text}</div>
            <button onClick={() => dismiss(t.id)} aria-label="Dismiss"><X /></button>
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  )
}

export function useToast(): ToastApi {
  const api = useContext(ToastContext)
  if (!api) throw new Error('useToast outside ToastProvider')
  return api
}
