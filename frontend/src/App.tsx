import { Suspense, lazy, useEffect, useState } from 'react'
import { Navigate, Route, Routes, useNavigate } from 'react-router-dom'
import { api, getToken, setToken, type Me } from './api'
import { ConfirmProvider } from './components'
import Shell from './layout/Shell'
import JobDetail from './pages/JobDetail'
import Login from './pages/Login'
import SSHKeys from './pages/SSHKeys'
import ServerDetail from './pages/ServerDetail'
import Servers from './pages/Servers'
import Audit from './pages/admin/Audit'
import Customers from './pages/admin/Customers'
import Dashboard from './pages/admin/Dashboard'
import IPAM from './pages/admin/IPAM'
import Images from './pages/admin/Images'
import Jobs from './pages/admin/Jobs'
import Fleet from './pages/admin/Servers'
import ServerPage from './pages/admin/server/ServerPage'
import { ToastProvider } from './toast'

// xterm.js is most of the bundle; only the console pages need it.
const Console = lazy(() => import('./pages/Console'))
const ConsoleTab = lazy(() => import('./pages/admin/server/ConsoleTab'))

function Loading() {
  return <main className="page subtle">Loading…</main>
}

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

  const home = me.is_admin ? '/admin' : '/servers'

  return (
    <ToastProvider>
      <ConfirmProvider>
        <Shell me={me} onSignOut={signOut}>
          <Routes>
            <Route path="/" element={<Navigate to={home} replace />} />
            <Route path="/login" element={<Navigate to={home} replace />} />
            <Route path="/servers" element={<Servers />} />
            <Route path="/servers/:id" element={<ServerDetail isAdmin={me.is_admin} />} />
            <Route
              path="/servers/:id/console"
              element={
                <Suspense fallback={<Loading />}>
                  <Console isAdmin={me.is_admin} />
                </Suspense>
              }
            />
            <Route path="/jobs/:id" element={<JobDetail isAdmin={me.is_admin} />} />
            <Route path="/ssh-keys" element={<SSHKeys />} />
            {me.is_admin && (
              <>
                <Route path="/admin" element={<Dashboard />} />
                <Route path="/admin/servers" element={<Fleet />} />
                <Route path="/admin/servers/:id" element={<ServerPage />}>
                  <Route
                    path="console"
                    element={
                      <Suspense fallback={<Loading />}>
                        <ConsoleTab />
                      </Suspense>
                    }
                  />
                </Route>
                <Route path="/admin/servers/:id/:tab" element={<ServerPage />} />
                <Route path="/admin/images" element={<Images />} />
                <Route path="/admin/customers" element={<Customers />} />
                <Route path="/admin/ipam" element={<IPAM />} />
                <Route path="/admin/jobs" element={<Jobs />} />
                <Route path="/admin/audit" element={<Audit />} />
              </>
            )}
            <Route path="*" element={<Navigate to={home} replace />} />
          </Routes>
        </Shell>
      </ConfirmProvider>
    </ToastProvider>
  )
}
