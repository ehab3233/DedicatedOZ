import { useEffect, useState } from 'react'
import { NavLink, Navigate, Route, Routes, useNavigate } from 'react-router-dom'
import { api, getToken, setToken, type Me } from './api'
import Login from './pages/Login'
import Servers from './pages/Servers'
import ServerDetail from './pages/ServerDetail'
import JobDetail from './pages/JobDetail'
import SSHKeys from './pages/SSHKeys'
import Console from './pages/Console'
import Admin from './pages/Admin'

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

  return (
    <>
      <header className="topbar">
        <span className="brand">DedicatedOZ</span>
        <nav>
          <NavLink to="/servers" className={({ isActive }) => (isActive ? 'active' : '')}>
            Servers
          </NavLink>
          <NavLink to="/ssh-keys" className={({ isActive }) => (isActive ? 'active' : '')}>
            SSH keys
          </NavLink>
          {me.is_admin && (
            <NavLink to="/admin" className={({ isActive }) => (isActive ? 'active' : '')}>
              Admin
            </NavLink>
          )}
        </nav>
        <span className="subtle">{me.email}</span>
        <button onClick={signOut}>Sign out</button>
      </header>

      <Routes>
        <Route path="/" element={<Navigate to="/servers" replace />} />
        <Route path="/login" element={<Navigate to="/servers" replace />} />
        <Route path="/servers" element={<Servers />} />
        <Route path="/servers/:id" element={<ServerDetail isAdmin={me.is_admin} />} />
        <Route path="/servers/:id/console" element={<Console />} />
        <Route path="/jobs/:id" element={<JobDetail isAdmin={me.is_admin} />} />
        <Route path="/ssh-keys" element={<SSHKeys />} />
        {me.is_admin && <Route path="/admin" element={<Admin />} />}
        <Route path="*" element={<Navigate to="/servers" replace />} />
      </Routes>
    </>
  )
}
