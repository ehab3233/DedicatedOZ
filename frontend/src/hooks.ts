import { useCallback, useEffect, useRef, useState } from 'react'

/** Load data once, with a manual `reload`. */
export function useAsync<T>(loader: () => Promise<T>, deps: unknown[] = []) {
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)

  const run = useCallback(async () => {
    setLoading(true)
    try {
      setData(await loader())
      setError(null)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }, deps) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    void run()
  }, [run])

  return { data, error, loading, reload: run, setData }
}

/**
 * Poll while `active` is true.
 *
 * Provisioning takes about twenty minutes and the progress bar is the only
 * thing telling the customer it is still alive, so this keeps running until
 * the caller says the job has finished. It stops itself when the tab is
 * hidden — nobody is watching a background tab, and a rack of them polling
 * every two seconds is real load on the API.
 */
export function usePolling(fn: () => void | Promise<void>, intervalMs: number, active: boolean) {
  const saved = useRef(fn)
  useEffect(() => {
    saved.current = fn
  }, [fn])

  useEffect(() => {
    if (!active) return
    let timer: number | undefined

    const tick = () => {
      if (document.visibilityState === 'visible') void saved.current()
    }
    timer = window.setInterval(tick, intervalMs)

    const onVisible = () => {
      if (document.visibilityState === 'visible') void saved.current()
    }
    document.addEventListener('visibilitychange', onVisible)

    return () => {
      if (timer) window.clearInterval(timer)
      document.removeEventListener('visibilitychange', onVisible)
    }
  }, [intervalMs, active])
}

/** Poll a job until it finishes. Resolves with the final row; rejects on failure. */
export async function waitForJob(
  jobId: string,
  onUpdate?: (job: import('./api').Job) => void,
  intervalMs = 1500,
): Promise<import('./api').Job> {
  const { api } = await import('./api')
  for (;;) {
    await new Promise((r) => setTimeout(r, intervalMs))
    let job: import('./api').Job
    try {
      job = await api.job(jobId)
    } catch {
      continue
    }
    onUpdate?.(job)
    if (job.state === 'succeeded') return job
    if (job.state === 'failed' || job.state === 'cancelled') {
      throw new Error(job.error ?? `job ${job.state}`)
    }
  }
}
