import {
  Activity,
  Disc3,
  KeyRound,
  LayoutDashboard,
  ListChecks,
  LogOut,
  Menu,
  Moon,
  Network,
  ScrollText,
  Server,
  Sun,
  UserRound,
  Users,
  X,
} from 'lucide-react'
import { useEffect, useState, type ReactNode } from 'react'
import { NavLink, useLocation } from 'react-router-dom'
import { api, type Me } from '../api'
import { useAsync, usePolling } from '../hooks'
import { effectiveTheme, setTheme } from '../theme'

function BrandMark() {
  return (
    <span className="brand-mark" aria-hidden>
      <svg width="14" height="14" viewBox="0 0 14 14" fill="currentColor">
        <rect x="1" y="2" width="12" height="3" rx="1" />
        <rect x="1" y="6.5" width="12" height="3" rx="1" opacity="0.75" />
        <rect x="1" y="11" width="12" height="2" rx="1" opacity="0.45" />
      </svg>
    </span>
  )
}

export { BrandMark }

function NavItem({ to, icon, children, end = false, count, crit = false }: {
  to: string
  icon: ReactNode
  children: ReactNode
  end?: boolean
  count?: number
  crit?: boolean
}) {
  return (
    <NavLink to={to} end={end} className={({ isActive }) => `nav-link ${isActive ? 'active' : ''}`}>
      {icon}
      <span>{children}</span>
      {count != null && count > 0 && <span className={`count ${crit ? 'crit' : ''}`}>{count}</span>}
    </NavLink>
  )
}

/** Sidebar status: is everything the panel depends on running? */
function SystemIndicator() {
  const system = useAsync(() => api.system())
  usePolling(system.reload, 60000, true)
  const d = system.data
  if (!d) {
    return (
      <span className="sys-pill">
        <span className={`dot ${system.error ? 'critical' : 'unknown'}`} />
        {system.error ? 'status unavailable' : 'checking services…'}
      </span>
    )
  }
  const problems =
    (d.database !== 'ok' ? 1 : 0) +
    (d.redis !== 'ok' ? 1 : 0) +
    Object.values(d.queues).filter((q) => !q.workers.length).length +
    (d.ipmitool ? 0 : 1)
  return (
    <NavLink to="/admin" className="sys-pill" title="System status on the dashboard">
      <span className={`dot ${problems ? 'critical' : 'ok'}`} />
      {problems ? `${problems} problem${problems > 1 ? 's' : ''}` : 'all services running'}
    </NavLink>
  )
}

function ThemeToggle() {
  const [current, setCurrent] = useState(effectiveTheme())
  const toggle = () => {
    const next = current === 'dark' ? 'light' : 'dark'
    setTheme(next)
    setCurrent(next)
  }
  return (
    <button className="ghost sm" onClick={toggle} title="Switch theme">
      {current === 'dark' ? <Sun /> : <Moon />}
      {current === 'dark' ? 'Light' : 'Dark'}
    </button>
  )
}

export default function Shell({
  me,
  onSignOut,
  children,
}: {
  me: Me
  onSignOut: () => void
  children: ReactNode
}) {
  const [open, setOpen] = useState(false)
  const location = useLocation()
  useEffect(() => setOpen(false), [location.pathname])

  const admin = me.is_admin

  return (
    <div className="app">
      {open && <div className="sidebar-scrim" onClick={() => setOpen(false)} />}
      <aside className={`sidebar ${open ? 'open' : ''}`}>
        <div className="sidebar-brand">
          <BrandMark />
          <div>
            <div className="brand-name">DedicatedOZ</div>
            <div className="brand-role">{admin ? 'Admin panel' : 'Customer portal'}</div>
          </div>
          <button className="ghost icon sm close" style={{ marginLeft: 'auto' }} onClick={() => setOpen(false)} aria-label="Close menu">
            <X />
          </button>
        </div>

        <nav className="sidebar-nav">
          {admin ? (
            <>
              <div className="nav-section">Infrastructure</div>
              <NavItem to="/admin" end icon={<LayoutDashboard />}>Dashboard</NavItem>
              <NavItem to="/admin/servers" icon={<Server />}>Servers</NavItem>
              <NavItem to="/admin/images" icon={<Disc3 />}>Images</NavItem>
              <NavItem to="/admin/ipam" icon={<Network />}>IP space</NavItem>
              <div className="nav-section">Operations</div>
              <NavItem to="/admin/jobs" icon={<ListChecks />}>Jobs</NavItem>
              <NavItem to="/admin/audit" icon={<ScrollText />}>Audit</NavItem>
              <div className="nav-section">Accounts</div>
              <NavItem to="/admin/customers" icon={<Users />}>Customers</NavItem>
            </>
          ) : (
            <>
              <div className="nav-section">Your servers</div>
              <NavItem to="/dashboard" icon={<LayoutDashboard />}>Overview</NavItem>
              <NavItem to="/servers" icon={<Server />}>Servers</NavItem>
              <div className="nav-section">Your account</div>
              <NavItem to="/ssh-keys" icon={<KeyRound />}>SSH keys</NavItem>
              <NavItem to="/account" icon={<UserRound />}>Account</NavItem>
            </>
          )}
        </nav>

        <div className="sidebar-footer">
          {admin && <SystemIndicator />}
          <div className="user-row">
            <Activity size={14} style={{ color: 'var(--text-3)' }} />
            <span className="email" title={me.email}>{me.email}</span>
          </div>
          <div className="row" style={{ justifyContent: 'space-between' }}>
            <ThemeToggle />
            <button className="ghost sm" onClick={onSignOut}><LogOut />Sign out</button>
          </div>
        </div>
      </aside>

      <div className="content">
        <div className="topbar-mobile">
          <button className="ghost icon" onClick={() => setOpen(true)} aria-label="Menu"><Menu /></button>
          <BrandMark />
          <span className="brand-name">DedicatedOZ</span>
        </div>
        {children}
      </div>
    </div>
  )
}
