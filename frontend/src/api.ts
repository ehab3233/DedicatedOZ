/**
 * API client.
 *
 * Every provisioning action returns a job rather than a result, so the calling
 * component's job is to start polling, not to wait.
 */

const TOKEN_KEY = 'doz.token'

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY)
}

export function setToken(token: string | null): void {
  if (token) localStorage.setItem(TOKEN_KEY, token)
  else localStorage.removeItem(TOKEN_KEY)
}

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message)
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const token = getToken()
  const response = await fetch(path, {
    ...init,
    headers: {
      'Content-Type': 'application/json',
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...init.headers,
    },
  })

  if (response.status === 401) {
    // The session is gone. Clear it so the router bounces to login rather
    // than every subsequent call failing in a different place.
    setToken(null)
    throw new ApiError(401, 'Your session has expired. Please sign in again.')
  }

  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`
    try {
      const body = await response.json()
      if (typeof body.detail === 'string') detail = body.detail
      else if (Array.isArray(body.detail)) detail = body.detail[0]?.msg ?? detail
    } catch {
      /* a non-JSON error body is not worth a second failure */
    }
    throw new ApiError(response.status, detail)
  }

  if (response.status === 204) return undefined as T
  return response.json() as Promise<T>
}

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export type ServerState =
  | 'in_stock' | 'provisioning' | 'active' | 'suspended'
  | 'wiping' | 'rescue' | 'rma' | 'retired'

export type JobState = 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled'

export interface Server {
  id: string
  serial: string
  hostname: string | null
  model: string
  state: ServerState
  cpu_model: string | null
  cpu_count: number | null
  cpu_cores_total: number | null
  ram_gb: number | null
  datacenter: string | null
  last_power_state: string | null
  health_status: string | null
  health_checked_at: string | null
  created_at: string
}

export interface IPAssignment {
  id: string
  address: string
  prefix_len: number
  gateway: string | null
  is_primary: boolean
  rdns: string | null
}

export interface ServerDetail extends Server {
  drives: Array<Record<string, unknown>>
  nics: Array<Record<string, unknown>>
  ip_addresses: IPAssignment[]
  health: {
    status: string | null
    checked_at: string | null
    subsystems: Record<string, { status?: string; detail?: unknown }>
  } | null
}

export interface Job {
  id: string
  type: string
  state: JobState
  server_id: string | null
  progress: number
  stage: string | null
  error: string | null
  created_at: string
  started_at: string | null
  finished_at: string | null
}

export interface JobLogEntry {
  sequence: number
  timestamp: string
  level: string
  message: string
  request?: Record<string, unknown> | null
  response?: Record<string, unknown> | null
}

export interface JobDetail extends Job {
  log: JobLogEntry[]
  result: Record<string, unknown>
}

export interface OSTemplate {
  id: string
  slug: string
  name: string
  family: string
  version: string
  install_method: string
  default_raid_level: string
}

export interface SSHKey {
  id: string
  name: string
  public_key: string
  fingerprint: string
  created_at: string
}

export interface BandwidthSeries {
  server_id: string
  period: string
  points: Array<{ timestamp: string; rx_bps: number; tx_bps: number }>
  total_rx_bytes: number
  total_tx_bytes: number
}

export interface Me {
  id: string
  email: string
  company_name: string | null
  contact_name: string | null
  is_admin: boolean
}

// ---------------------------------------------------------------------------
// Endpoints
// ---------------------------------------------------------------------------

export const api = {
  login: (email: string, password: string) =>
    request<{ access_token: string; expires_in: number; is_admin: boolean }>(
      '/api/v1/auth/login',
      { method: 'POST', body: JSON.stringify({ email, password }) },
    ),

  me: () => request<Me>('/api/v1/auth/me'),

  servers: () => request<Server[]>('/api/v1/servers'),
  server: (id: string) => request<ServerDetail>(`/api/v1/servers/${id}`),
  serverJobs: (id: string) => request<Job[]>(`/api/v1/servers/${id}/jobs`),

  power: (id: string, action: 'on' | 'off' | 'cycle' | 'reset') =>
    request<Job>(`/api/v1/servers/${id}/power`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    }),

  reinstall: (
    id: string,
    body: {
      os_template_id: string
      hostname?: string
      raid_level: string
      ssh_key_ids: string[]
      confirm_data_loss: boolean
    },
  ) =>
    request<Job>(`/api/v1/servers/${id}/reinstall`, {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  rescue: (id: string, sshKeyIds: string[]) =>
    request<Job>(`/api/v1/servers/${id}/rescue`, {
      method: 'POST',
      body: JSON.stringify({ ssh_key_ids: sshKeyIds }),
    }),

  bandwidth: (id: string, period: string) =>
    request<BandwidthSeries>(`/api/v1/servers/${id}/bandwidth?period=${period}`),

  setRdns: (serverId: string, assignmentId: string, rdns: string | null) =>
    request<IPAssignment>(`/api/v1/servers/${serverId}/ips/${assignmentId}/rdns`, {
      method: 'PATCH',
      body: JSON.stringify({ rdns }),
    }),

  job: (id: string) => request<JobDetail>(`/api/v1/jobs/${id}`),
  cancelJob: (id: string) => request<Job>(`/api/v1/jobs/${id}/cancel`, { method: 'POST' }),

  osTemplates: () => request<OSTemplate[]>('/api/v1/os-templates'),

  sshKeys: () => request<SSHKey[]>('/api/v1/ssh-keys'),
  addSshKey: (name: string, publicKey: string) =>
    request<SSHKey>('/api/v1/ssh-keys', {
      method: 'POST',
      body: JSON.stringify({ name, public_key: publicKey }),
    }),
  deleteSshKey: (id: string) =>
    request<void>(`/api/v1/ssh-keys/${id}`, { method: 'DELETE' }),

  // --- admin ---
  fleet: () => request<Record<string, unknown>[]>('/api/v1/admin/servers'),
  fleetSummary: () => request<Record<string, unknown>>('/api/v1/admin/summary'),
  adminJobs: () => request<Job[]>('/api/v1/admin/jobs'),
  adminJob: (id: string) => request<JobDetail>(`/api/v1/admin/jobs/${id}`),
}

/** URL for the SOL console websocket. The token rides in the query string
 *  because browsers cannot set headers on a websocket handshake. */
export function consoleUrl(serverId: string): string {
  const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${protocol}//${location.host}/api/v1/console/${serverId}/sol?token=${getToken()}`
}
