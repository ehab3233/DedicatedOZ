import { Power, PowerOff, RefreshCw, RotateCcw, Zap } from 'lucide-react'
import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react'
import { api, type Job, type PowerAction, type PowerState } from './api'
import { Dot, Spinner, relativeTime, useConfirm, useNow } from './components'
import { useToast } from './toast'

const TERMINAL = ['succeeded', 'failed', 'cancelled']

const ACTIONS: Array<{
  action: PowerAction
  label: string
  title: string
  icon: ReactNode
  danger?: boolean
  confirm?: { title: string; body: string }
}> = [
  { action: 'on', label: 'Power on', title: 'Switch the server on', icon: <Power /> },
  {
    action: 'off',
    label: 'Shut down',
    title: 'Ask the operating system to shut down cleanly (ACPI power button)',
    icon: <PowerOff />,
  },
  {
    action: 'reset',
    label: 'Reset',
    title: 'Hard reset: reboot immediately without shutting down',
    icon: <RotateCcw />,
    danger: true,
    confirm: { title: 'Hard-reset the server?', body: 'It reboots immediately without shutting the OS down. Unsaved data is lost.' },
  },
  {
    action: 'cycle',
    label: 'Power cycle',
    title: 'Power off, wait, power on',
    icon: <RefreshCw />,
    danger: true,
    confirm: { title: 'Power-cycle the server?', body: 'Power is cut, then restored after a few seconds. Like pulling the plug and putting it back.' },
  },
  {
    action: 'force_off',
    label: 'Force off',
    title: 'Cut power immediately, like pulling the plug',
    icon: <Zap />,
    danger: true,
    confirm: { title: 'Cut power now?', body: 'The server switches off immediately, without shutting down. Unsaved data on it is lost.' },
  },
]

/** Live power state straight from the BMC, refreshed on a timer. */
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
  const now = useNow()
  const s = state?.state ?? 'unknown'
  return (
    <span className="status-line">
      <Dot state={s} pulse={loading && !state} />
      <span className="strong" style={{ textTransform: 'capitalize' }}>
        {loading && !state ? 'Checking…' : s === 'unknown' ? 'Unknown' : `Power ${s}`}
      </span>
      {state?.via && (
        <span className="faint small">
          {state.via.toUpperCase()} · {relativeTime(state.checked_at, now)}
        </span>
      )}
      <button className="ghost icon sm" onClick={onRefresh} disabled={loading} title="Read from the BMC now">
        {loading ? <Spinner /> : <RefreshCw />}
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
  pollMs = 10000,
  compact = false,
}: {
  serverId: string
  isAdmin?: boolean
  activeJob?: Job | null
  disabled?: boolean
  onChanged?: () => void
  pollMs?: number
  compact?: boolean
}) {
  const power = usePowerState(serverId, pollMs)
  const toast = useToast()
  const confirm = useConfirm()
  const [running, setRunning] = useState<{ action: PowerAction; job: Job } | null>(null)
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
        if (current.state === 'succeeded') toast.ok(`${label}: ${current.stage ?? 'done'}`)
        else toast.error(`${label} ${current.state}: ${current.error ?? 'see the job log'}`)
        await power.refresh(true)
        onChanged?.()
        return
      }
    }
  }

  async function run(action: PowerAction, force = false) {
    const spec = ACTIONS.find((a) => a.action === action)
    if (spec?.confirm) {
      const ok = await confirm({
        title: spec.confirm.title,
        body: force ? `${spec.confirm.body} This overrides the job that is running.` : spec.confirm.body,
        confirmLabel: spec.label,
        danger: true,
      })
      if (!ok) return
    }
    try {
      const job = await api.power(serverId, action, force)
      setRunning({ action, job })
      onChanged?.()
      void follow(job, action)
    } catch (e) {
      toast.error(e instanceof Error ? e.message : String(e))
    }
  }

  useEffect(() => () => { followRef.current++ }, [])

  const runningLabel = running ? ACTIONS.find((a) => a.action === running.action)?.label : null
  const state = power.state?.state

  return (
    <div className="stack" style={{ gap: 10 }}>
      <div className="spread">
        <PowerBadge state={power.state} loading={power.loading} onRefresh={() => power.refresh(true)} />
        <div className="row" style={{ gap: 6 }}>
          {ACTIONS.filter((a) => (compact ? a.action !== 'cycle' : true)).map((a) => {
            const pointless = (a.action === 'on' && state === 'on') || (a.action !== 'on' && state === 'off')
            return (
              <button
                key={a.action}
                className={`sm ${a.danger ? 'danger' : ''}`}
                title={a.title}
                disabled={locked || pointless}
                onClick={() => run(a.action)}
              >
                {a.icon}{a.label}
              </button>
            )
          })}
        </div>
      </div>

      {power.state?.error && (
        <div className="small" style={{ color: 'var(--warn)' }}>Could not read power state: {power.state.error}</div>
      )}

      {running && (
        <div className="small subtle row">
          <Spinner /> {runningLabel}: {running.job.stage ?? running.job.state}…
        </div>
      )}

      {busyElsewhere && (
        <div className="small subtle">
          Locked while a {activeJob!.type.replace(/_/g, ' ')} job runs.
          {isAdmin && (
            <>
              {' '}If the machine is stuck:{' '}
              <button className="link-button danger" onClick={() => run('reset', true)}>reset anyway</button>
              {' · '}
              <button className="link-button danger" onClick={() => run('force_off', true)}>force off anyway</button>
            </>
          )}
        </div>
      )}
    </div>
  )
}
