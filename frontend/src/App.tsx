import { Suspense, lazy, useEffect, useState } from 'react'
import { NavLink, Navigate, Route, Routes, useNavigate } from 'react-router-dom'
import { api, getToken, setToken, type Me } from './api'
import Login from './pages/Login'
import Servers from './pages/Servers'
import ServerDetail from './pages/ServerDetail'
import JobDetail from './pages/JobDetail'
import SSHKeys from './pages/SSHKeys'
import AdminLayout from './pages/admin/AdminLayout'
import Fleet from './pages/admin/Fleet'
import ServerAdmin from './pages/admin/ServerAdmin'
import Customers from './pages/admin/Customers'
import IPAM from './pages/admin/IPAM'
import Jobs from './pages/admin/Jobs'
import Audit from './pages/admin/Audit'

// xterm.js is most of the bundle; only the console page needs it.
const Console = lazy(() => import('./pages/Console'))

export default function App() {
  const [me, setMe] = useState<Me | null>(null)
  const [checked, setChecked] = useState(false)
  const navigate = useNavigate()

  useEffect(() => {
    if (!getToken()) {
      setChecked(true)
      return
    }
    api
      .me()
      .then(setMe)
      .catch(() => setToken(null))
      .finally(() => setChecked(true))
  }, [])

  // Wait for the session check before rendering routes; otherwise a reload on
  // a deep link bounces to login and back, losing the page you were on.
  if (!checked) return null

  if (!me) {
    return (
      <Routes>
        <Route path="/login" element={<Login onAuthenticated={setMe} />} />
        <Route path="*" element={<Navigate to="/login" replace />} />
      </Routes>
    )
  }

  const signOut = () => {
    setToken(null)
    setMe(null)
    navigate('/login')
  }

  const link = ({ isActive }: { isActive: boolean }) => (isActive ? 'active' : '')

  return (
    <>
      <header className="topbar">
        <span className="brand">DedicatedOZ</span>
        <nav>
          <NavLink to="/servers" className={link}>Servers</NavLink>
          <NavLink to="/ssh-keys" className={link}>SSH keys</NavLink>
          {me.is_admin && <NavLink to="/admin" className={link}>Manage</NavLink>}
        </nav>
        <span className="subtle">{me.email}</span>
        <button onClick={signOut}>Sign out</button>
      </header>

      <Routes>
        <Route path="/" element={<Navigate to={me.is_admin ? '/admin' : '/servers'} replace />} />
        <Route path="/login" element={<Navigate to={me.is_admin ? '/admin' : '/servers'} replace />} />
        <Route path="/servers" element={<Servers />} />
        <Route path="/servers/:id" element={<ServerDetail isAdmin={me.is_admin} />} />
        <Route
          path="/servers/:id/console"
          element={
            <Suspense fallback={<main className="page subtle">Loading console…</main>}>
              <Console isAdmin={me.is_admin} />
            </Suspense>
          }
        />
        <Route path="/jobs/:id" element={<JobDetail isAdmin={me.is_admin} />} />
        <Route path="/ssh-keys" element={<SSHKeys />} />
        {me.is_admin && (
          <Route path="/admin" element={<AdminLayout />}>
            <Route index element={<Fleet />} />
            <Route path="servers/:id" element={<ServerAdmin />} />
            <Route path="customers" element={<Customers />} />
            <Route path="ipam" element={<IPAM />} />
            <Route path="jobs" element={<Jobs />} />
            <Route path="audit" element={<Audit />} />
          </Route>
        )}
        <Route path="*" element={<Navigate to="/servers" replace />} />
      </Routes>
    </>
  )
}
