import { useState } from 'react'
import { api, setToken, type Me } from '../api'
import { Banner } from '../components'

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
      setError(e instanceof Error ? e.message : 'Sign in failed')
      setToken(null)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="login-wrap">
      <div className="login-card">
        <h1 style={{ marginBottom: 18 }}>DedicatedOZ</h1>
        <div className="card">
          <form onSubmit={submit}>
            {error && <Banner kind="error">{error}</Banner>}
            <div className="field">
              <label htmlFor="email">Email</label>
              <input
                id="email"
                type="email"
                autoComplete="username"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                required
              />
            </div>
            <div className="field">
              <label htmlFor="password">Password</label>
              <input
                id="password"
                type="password"
                autoComplete="current-password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                required
              />
            </div>
            <button className="primary" type="submit" disabled={busy} style={{ width: '100%' }}>
              {busy ? 'Signing in…' : 'Sign in'}
            </button>
          </form>
        </div>
      </div>
    </div>
  )
}
