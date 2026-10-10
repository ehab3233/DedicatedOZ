import { Activity, Cpu, Gauge, ListChecks, Network, Pencil, ScrollText, Settings2, Terminal } from 'lucide-react'
import { createContext, useCallback, useContext, useState } from 'react'
import { Outlet, useLocation, useParams } from 'react-router-dom'
import { api, type AdminServer, type Job, type Subscription } from '../../../api'
import { Banner, Card, Empty, PageHeader, Pill, Progress, Tabs, label } from '../../../components'
import { useAsync, usePolling } from '../../../hooks'
import { PowerControls } from '../../../power'
import Bmc from './Bmc'
import Events from './Events'
import Hardware from './Hardware'
import JobsTab from './JobsTab'
import NetworkTab from './NetworkTab'
import Overview from './Overview'
import Sensors from './Sensors'
import { KvmButton } from './kvm'
import { EditModal } from './modals'

const ACTIVE_JOB_STATES = ['queued', 'running']

export interface ServerContext {
  server: AdminServer
  jobs: Job[]
  activeJob: Job | null
  subscription: Subscription | null
  refresh: () => Promise<void>
}

const Ctx = createContext<ServerContext | null>(null)

export function useServer(): ServerContext {
  const ctx = useContext(Ctx)
  if (!ctx) throw new Error('useServer outside ServerPage')
  return ctx
}

export default function ServerPage() {
  const { id = '', tab } = useParams()
  const location = useLocation()
  const [editing, setEditing] = useState(false)

  const server = useAsync(() => api.adminServer(id), [id])
  const jobs = useAsync(() => api.adminJobs({ server_id: id }), [id])
  const subs = useAsync(() => api.subscriptions({ server_id: id }), [id])

  const activeJob = (jobs.data ?? []).find((j) => ACTIVE_JOB_STATES.includes(j.state)) ?? null
  const subscription = (subs.data ?? [])[0] ?? null

  const refresh = useCallback(async () => {
    await Promise.all([server.reload(), jobs.reload(), subs.reload()])
  }, [server.reload, jobs.reload, subs.reload]) // eslint-disable-line react-hooks/exhaustive-deps

  // Fast while a job runs (it is a progress display), slow otherwise so
  // health and inventory from the sweeps still show up without a reload.
  usePolling(refresh, activeJob ? 4000 : 30000, true)

  if (server.error) return <main className="page"><Banner kind="error">{server.error}</Banner></main>
  if (!server.data) return <main className="page"><Empty>Loading…</Empty></main>

  const s = server.data
  const base = `/admin/servers/${id}`
  const isConsole = location.pathname.endsWith('/console')
  const ctx: ServerContext = { server: s, jobs: jobs.data ?? [], activeJob, subscription, refresh }

  return (
    <Ctx.Provider value={ctx}>
      <main className="page wide">
        <PageHeader
          crumbs={[{ label: 'Servers', to: '/admin/servers' }, { label: s.serial }]}
          title={
            <span className="row" style={{ gap: 10 }}>
              <span className="mono">{s.serial}</span>
              {s.hostname && <span className="subtle" style={{ fontWeight: 400 }}>{s.hostname}</span>}
              <Pill value={s.state} />
              <Pill value={s.health_status} />
            </span>
          }
          sub={[s.model, [s.datacenter, s.rack, s.rack_unit && `U${s.rack_unit}`].filter(Boolean).join(' '), `CIMC ${s.cimc_ip}`, s.customer_email && `customer ${s.customer_email}`].filter(Boolean).join(' · ')}
          actions={
            <>
              <KvmButton serverId={id} cimcIp={s.cimc_ip} port={s.redfish_port} />
              <button onClick={() => setEditing(true)}><Pencil />Edit</button>
            </>
          }
        />

        {activeJob && (
          <Card>
            <div className="spread" style={{ marginBottom: 8 }}>
              <div>
                <strong>{label(activeJob.type)}</strong> <span className="subtle">— {activeJob.stage ?? 'starting…'}</span>
              </div>
              <a href={`/jobs/${activeJob.id}`}>Job log</a>
            </div>
            <Progress value={activeJob.progress} active />
          </Card>
        )}

        {s.role === 'management' ? (
          <Banner kind="info">
            <strong>Management server.</strong> The panel reads it (health, readings, event log) but does not power, provision or configure it from here.
            {s.serial.startsWith('SIM-') && ' Its BMC is the built-in simulator, so the readings are simulated; the VM\'s own services are on the Dashboard.'}
          </Banner>
        ) : (
          <Card><div className="card-body tight"><PowerControls serverId={id} isAdmin activeJob={activeJob} onChanged={refresh} /></div></Card>
        )}

        <Tabs
          items={[
            { to: base, label: 'Overview', end: true, icon: <Gauge /> },
            { to: `${base}/console`, label: 'Console', icon: <Terminal /> },
            { to: `${base}/hardware`, label: 'Hardware', icon: <Cpu /> },
            { to: `${base}/sensors`, label: 'Sensors', icon: <Activity /> },
            { to: `${base}/events`, label: 'Event log', icon: <ScrollText /> },
            { to: `${base}/network`, label: 'Network', icon: <Network /> },
            { to: `${base}/jobs`, label: 'Jobs', icon: <ListChecks />, count: activeJob ? 1 : 0 },
            { to: `${base}/bmc`, label: 'BMC', icon: <Settings2 /> },
          ]}
        />

        {isConsole ? <Outlet /> : (
          {
            undefined: <Overview />,
            hardware: <Hardware />,
            sensors: <Sensors />,
            events: <Events />,
            network: <NetworkTab />,
            jobs: <JobsTab />,
            bmc: <Bmc />,
          } as Record<string, React.ReactNode>
        )[tab ?? 'undefined'] ?? <Overview />}

        {editing && (
          <EditModal server={s} onClose={() => setEditing(false)} onDone={async () => { setEditing(false); await refresh() }} />
        )}
      </main>
    </Ctx.Provider>
  )
}
