import { useState } from 'react'
import { ApiError, api, setToken, type Me } from '../api'
import { Banner } from '../components'
import { BrandMark } from '../layout/Shell'

export default function Login({ onAuthenticated }: { onAuthenticated: (me: Me) => void }) {
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  async function submit(event: React.FormEvent) {
    event.preventDefault()
    setBusy(true)
    setError(null)
    try {
      const { access_token } = await api.login(email, password)
      setToken(access_token)
      onAuthenticated(await api.me())
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) {
        setError('Email or password is incorrect. The installer printed the admin password once; '
          + 'reset it with: sudo /opt/doz/doz.sh reset-admin <email>')
      } else {
        setError(e instanceof Error ? e.message : 'Sign in failed')
      }
      setToken(null)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="login-wrap">
      <div className="login-card">
        <div className="login-brand">
          <BrandMark />
          <div>
            <div className="brand-name">DedicatedOZ</div>
            <div className="brand-role">Server management</div>
          </div>
        </div>
        <div className="card">
          <div className="card-body">
            <form onSubmit={submit}>
              {error && <Banner kind="error">{error}</Banner>}
              <div className="field">
                <label htmlFor="email">Email</label>
                <input id="email" type="email" autoComplete="username" value={email} onChange={(e) => setEmail(e.target.value)} required autoFocus />
              </div>
              <div className="field">
                <label htmlFor="password">Password</label>
                <input id="password" type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} required />
              </div>
              <button className="primary" type="submit" disabled={busy} style={{ width: '100%', marginTop: 4 }}>
                {busy ? 'Signing in…' : 'Sign in'}
              </button>
            </form>
          </div>
        </div>
      </div>
    </div>
  )
}
