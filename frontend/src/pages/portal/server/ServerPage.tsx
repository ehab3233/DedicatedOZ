import { Activity, Gauge, HardDriveDownload, ListChecks, Network, Terminal } from 'lucide-react'
import { createContext, useCallback, useContext } from 'react'
import { Outlet, useLocation, useParams } from 'react-router-dom'
import { api, type Job, type ServerDetail } from '../../../api'
import { Banner, Card, Empty, PageHeader, Pill, Progress, Tabs, label } from '../../../components'
import { useAsync, usePolling } from '../../../hooks'
import { PowerControls } from '../../../power'
import ActivityTab from './ActivityTab'
import NetworkTab from './NetworkTab'
import OSTab from './OSTab'
import Overview from './Overview'
import TrafficTab from './TrafficTab'

const ACTIVE_JOB_STATES = ['queued', 'running']

export interface PortalServerContext {
  server: ServerDetail
  jobs: Job[]
  activeJob: Job | null
  refresh: () => Promise<void>
}

const Ctx = createContext<PortalServerContext | null>(null)

export function usePortalServer(): PortalServerContext {
  const ctx = useContext(Ctx)
  if (!ctx) throw new Error('usePortalServer outside ServerPage')
  return ctx
}

/** One server, in tabs: what it is doing, its console, OS, network, traffic and history. */
export default function PortalServerPage() {
  const { id = '', tab } = useParams()
  const location = useLocation()
  const server = useAsync(() => api.server(id), [id])
  const jobs = useAsync(() => api.serverJobs(id), [id])
  const activeJob = (jobs.data ?? []).find((j) => ACTIVE_JOB_STATES.includes(j.state)) ?? null

  const refresh = useCallback(async () => {
    await Promise.all([server.reload(), jobs.reload()])
  }, [server.reload, jobs.reload]) // eslint-disable-line react-hooks/exhaustive-deps

  // While a job is in flight the page is a progress display, so poll fast.
  usePolling(refresh, 5000, Boolean(activeJob))

  if (server.error) return <main className="page"><Banner kind="error">{server.error}</Banner></main>
  if (!server.data) return <main className="page"><Empty>Loading…</Empty></main>

  const s = server.data
  const base = `/servers/${id}`
  const isConsole = location.pathname.endsWith('/console')
  const ctx: PortalServerContext = { server: s, jobs: jobs.data ?? [], activeJob, refresh }

  return (
    <Ctx.Provider value={ctx}>
      <main className="page wide">
        <PageHeader
          crumbs={[{ label: 'Servers', to: '/servers' }, { label: s.hostname ?? s.serial }]}
          title={<span className="row" style={{ gap: 10 }}>{s.hostname ?? s.serial}<Pill value={s.state} /><Pill value={s.health_status} /></span>}
          sub={[s.serial, s.model, s.datacenter, s.plan?.plan_name].filter(Boolean).join(' · ')}
        />

        {activeJob && (
          <Card>
            <div className="spread" style={{ marginBottom: 8 }}>
              <div><strong>{label(activeJob.type)}</strong> <span className="subtle">— {activeJob.stage ?? 'starting…'}</span></div>
              <a href={`/jobs/${activeJob.id}`}>Follow progress</a>
            </div>
            <Progress value={activeJob.progress} active />
          </Card>
        )}

        <Card><div className="card-body tight"><PowerControls serverId={id} activeJob={activeJob} onChanged={refresh} /></div></Card>

        <Tabs
          items={[
            { to: base, label: 'Overview', end: true, icon: <Gauge /> },
            { to: `${base}/console`, label: 'Console', icon: <Terminal /> },
            { to: `${base}/os`, label: 'Operating system', icon: <HardDriveDownload /> },
            { to: `${base}/network`, label: 'Network', icon: <Network /> },
            { to: `${base}/traffic`, label: 'Traffic', icon: <Activity /> },
            { to: `${base}/activity`, label: 'Activity', icon: <ListChecks />, count: activeJob ? 1 : 0 },
          ]}
        />

        {isConsole ? <Outlet /> : (
          {
            undefined: <Overview />,
            os: <OSTab />,
            network: <NetworkTab />,
            traffic: <TrafficTab />,
            activity: <ActivityTab />,
          }[tab ?? 'undefined'] ?? <Overview />
        )}
      </main>
    </Ctx.Provider>
  )
}
