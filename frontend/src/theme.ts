/** Light / dark / follow-the-system, remembered per browser. */

export type Theme = 'system' | 'light' | 'dark'

const KEY = 'doz.theme'

export function getTheme(): Theme {
  try {
    const stored = localStorage.getItem(KEY)
    if (stored === 'light' || stored === 'dark') return stored
  } catch {
    /* storage blocked: follow the system */
  }
  return 'system'
}

export function applyTheme(theme: Theme): void {
  const root = document.documentElement
  if (theme === 'system') root.removeAttribute('data-theme')
  else root.setAttribute('data-theme', theme)
}

export function setTheme(theme: Theme): void {
  try {
    if (theme === 'system') localStorage.removeItem(KEY)
    else localStorage.setItem(KEY, theme)
  } catch {
    /* fine */
  }
  applyTheme(theme)
}

/** What is actually showing right now, resolving "system". */
export function effectiveTheme(): 'light' | 'dark' {
  const theme = getTheme()
  if (theme !== 'system') return theme
  return window.matchMedia?.('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'
}
