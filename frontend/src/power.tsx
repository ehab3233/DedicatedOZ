import { useCallback, useEffect, useRef, useState } from 'react'
import { api, type Job, type PowerAction, type PowerState } from './api'
import { relativeTime } from './components'

const TERMINAL = ['succeeded', 'failed', 'cancelled']

const ACTIONS: Array<{
  action: PowerAction
  label: string
  title: string
  danger?: boolean
  confirm?: string
}> = [
  { action: 'on', label: 'Power on', title: 'Switch the server on' },
  {
    action: 'off',
    label: 'Shut down',
    title: 'Ask the operating system to shut down cleanly (ACPI power button)',
  },
  {
    action: 'force_off',
    label: 'Force off',
    title: 'Cut power immediately, like pulling the plug',
    danger: true,
    confirm: 'Cut power immediately? Unsaved data on the server will be lost.',
  },
  {
    action: 'reset',
    label: 'Reset',
    title: 'Hard reset: reboot immediately without shutting down',
    danger: true,
    confirm: 'Hard-reset the server now? It reboots without shutting down.',
  },
  {
    action: 'cycle',
    label: 'Power cycle',
    title: 'Power off, wait, power on',
    danger: true,
    confirm: 'Power-cycle the server? It turns off hard, then back on.',
  },
]

/** Live power state straight from the BMC, refreshed on demand. */
export function usePowerState(serverId: string, pollMs = 0) {
  const [state, setState] = useState<PowerState | null>(null)
  const [loading, setLoading] = useState(false)

  const refresh = useCallback(
    async (fresh = false) => {
      setLoading(true)
      try {
        setState(await api.powerState(serverId, fresh))
      } catch (e) {
        setState({
          state: 'unknown',
          via: null,
          checked_at: new Date().toISOString(),
          error: e instanceof Error ? e.message : String(e),
          cached: false,
        })
      } finally {
        setLoading(false)
      }
    },
    [serverId],
  )

  useEffect(() => {
    void refresh()
    if (!pollMs) return
    const timer = window.setInterval(() => {
      if (document.visibilityState === 'visible') void refresh()
    }, pollMs)
    return () => window.clearInterval(timer)
  }, [refresh, pollMs])

  return { state, loading, refresh }
}

export function PowerBadge({ state, loading, onRefresh }: {
  state: PowerState | null
  loading: boolean
  onRefresh: () => void
}) {
  const s = state?.state ?? 'unknown'
  const cls = s === 'on' ? 'ok' : s === 'off' ? 'critical' : 'warning'
  return (
    <span className="row" style={{ gap: 8 }}>
      <span className={`pill ${cls}`} style={{ fontSize: 13 }}>
        {loading && !state ? 'checking…' : `power ${s}`}
      </span>
      {state?.via && (
        <span className="subtle" style={{ fontSize: 12 }}>
          via {state.via.toUpperCase()}, {relativeTime(state.checked_at)}
        </span>
      )}
      <button
        className="link-button"
        onClick={onRefresh}
        disabled={loading}
        title="Read power state from the BMC now"
      >
        {loading ? '…' : '↻'}
      </button>
    </span>
  )
}

/**
 * The power buttons, with live state and inline progress for the action you
 * just clicked. Each click queues a job; this component follows that job to
 * the end and then re-reads the BMC, so what it shows is what happened.
 */
export function PowerControls({
  serverId,
  isAdmin = false,
  activeJob = null,
  disabled = false,
  onChanged,
  pollMs = 0,
}: {
  serverId: string
  isAdmin?: boolean
  activeJob?: Job | null
  disabled?: boolean
  onChanged?: () => void
  pollMs?: number
}) {
  const power = usePowerState(serverId, pollMs)
  const [running, setRunning] = useState<{ action: PowerAction; job: Job } | null>(null)
  const [message, setMessage] = useState<{ kind: 'info' | 'error'; text: string } | null>(null)
  const followRef = useRef(0)

  const busyElsewhere = Boolean(activeJob) && !running
  const locked = disabled || Boolean(running) || busyElsewhere

  async function follow(job: Job, action: PowerAction) {
    const token = ++followRef.current
    const label = ACTIONS.find((a) => a.action === action)?.label ?? action
    for (;;) {
      await new Promise((r) => setTimeout(r, 1000))
      if (token !== followRef.current) return
      let current: Job
      try {
        current = await api.job(job.id)
      } catch {
        continue
      }
      setRunning({ action, job: current })
      if (TERMINAL.includes(current.state)) {
        setRunning(null)
        if (current.state === 'succeeded') {
          setMessage({ kind: 'info', text: `${label}: done — ${current.stage ?? 'ok'}.` })
        } else {
          setMessage({ kind: 'error', text: `${label} ${current.state}: ${current.error ?? 'see the job log'}` })
        }
        await power.refresh(true)
        onChanged?.()
        return
      }
    }
  }

  async function run(action: PowerAction, force = false) {
    const spec = ACTIONS.find((a) => a.action === action)
    if (spec?.confirm && !confirm(force ? `${spec.confirm}\n\nThis overrides the job that is running.` : spec.confirm)) return
    setMessage(null)
    try {
      const job = await api.power(serverId, action, force)
      setRunning({ action, job })
      onChanged?.()
      void follow(job, action)
    } catch (e) {
      setMessage({ kind: 'error', text: e instanceof Error ? e.message : String(e) })
    }
  }

  useEffect(() => () => { followRef.current++ }, [])

  const runningLabel = running ? ACTIONS.find((a) => a.action === running.action)?.label : null

  return (
    <div>
      <div className="spread" style={{ marginBottom: 10, flexWrap: 'wrap' }}>
        <PowerBadge state={power.state} loading={power.loading} onRefresh={() => power.refresh(true)} />
        <div className="row">
          {ACTIONS.map((a) => (
            <button
              key={a.action}
              className={a.danger ? 'danger' : ''}
              title={a.title}
              disabled={locked}
              onClick={() => run(a.action)}
            >
              {a.label}
            </button>
          ))}
        </div>
      </div>

      {power.state?.error && (
        <div className="subtle" style={{ color: 'var(--warn)', fontSize: 13, marginBottom: 6 }}>
          Could not read power state: {power.state.error}
        </div>
      )}

      {running && (
        <div className="subtle" style={{ fontSize: 13 }}>
          <span className="spinner" /> {runningLabel}: {running.job.stage ?? running.job.state}…
        </div>
      )}

      {busyElsewhere && (
        <div className="subtle" style={{ fontSize: 13 }}>
          Locked while a {activeJob!.type.replace(/_/g, ' ')} job runs.
          {isAdmin && (
            <>
              {' '}If the machine is stuck:{' '}
              <button className="link-button danger" onClick={() => run('reset', true)}>
                reset anyway
              </button>{' '}
              ·{' '}
              <button className="link-button danger" onClick={() => run('force_off', true)}>
                force off anyway
              </button>
            </>
          )}
        </div>
      )}

      {message && (
        <div
          style={{ fontSize: 13, marginTop: 6, color: message.kind === 'error' ? 'var(--crit)' : 'var(--ok)' }}
        >
          {message.text}
        </div>
      )}
    </div>
  )
}
