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

  if (response.status === 401 && !path.endsWith('/auth/login')) {
    // The session is gone. Clear it so the router bounces to login rather
    // than every subsequent call failing in a different place. A 401 from
    // the login form itself is a wrong password, and says so below.
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
  volumes: Array<Record<string, unknown>>
  nics: Array<Record<string, unknown>>
  ip_addresses: IPAssignment[]
  health: {
    status: string | null
    checked_at: string | null
    subsystems: Record<string, { status?: string; detail?: unknown }>
    /** Polls in a row the BMC has not answered; `status` is the last verdict. */
    missed_polls?: number
    last_error?: string | null
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

export interface BmcTestCheck {
  name: string
  ok: boolean
  summary: string
  raw: string
  hint: string | null
}

export interface BmcTestReport {
  ok: boolean
  checks: BmcTestCheck[]
  facts: Record<string, string | boolean | null>
  configured_cipher: string
  working_cipher: string | null
  cipher_saved: boolean
  verdict: string
  hint: string | null
}

export interface NetbootFile {
  role: string
  path: string
  url: string
  present: boolean
  size_bytes: number | null
  modified_at: string | null
}

export interface NetbootTemplate {
  id: string
  slug: string
  name: string
  version: string
  install_method: string
  is_public: boolean
  files: NetbootFile[]
  ready: boolean
}

export interface NetbootReport {
  asset_dir: string
  base_url: string
  ramdisk: { files: NetbootFile[]; ready: boolean }
  loaders: { files: NetbootFile[]; present: boolean; embedded_url: string | null; expected_url: string; ready: boolean }
  templates: NetbootTemplate[]
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

export interface AdminServer extends ServerDetail {
  cimc_ip: string
  cimc_credential_ref: string
  bmc_protocol: string | null
  ipmi_port: number | null
  redfish_port: number | null
  ipmi_cipher_suite: string | null
  cimc_firmware: string | null
  bios_version: string | null
  rack: string | null
  rack_unit: number | null
  switch_name: string | null
  switch_port: string | null
  customer_vlan: number | null
  provisioning_mac: string | null
  last_wiped_at: string | null
  state_changed_at: string | null
  bmc_prepared_at: string | null
  notes: string | null
  customer_email: string | null
}

export interface AdminCustomer extends Me {
  phone: string | null
  billing_ref: string | null
  is_active: boolean
  active_servers: number
  created_at: string
}

export interface Subscription {
  id: string
  customer_id: string
  server_id: string
  plan_name: string
  monthly_price: number | null
  currency: string
  billing_ref: string | null
  bandwidth_quota_tb: number | null
  started_at: string
  ended_at: string | null
  customer_email: string | null
  server_serial: string | null
}

export interface IPBlock {
  id: string
  cidr: string
  version: number
  gateway: string | null
  routing_mode: string
  vlan: number | null
  datacenter: string | null
  source: string | null
  is_assignable: boolean
  total_hosts: number
  assigned: number
}

export interface AdminJobLogEntry extends JobLogEntry {
  request: Record<string, unknown> | null
  response: Record<string, unknown> | null
  customer_visible: boolean
}

export interface AdminJobDetail extends JobDetail {
  log: AdminJobLogEntry[]
  payload: Record<string, unknown>
  celery_task_id: string | null
  attempts: number
}

export interface FleetSummary {
  servers_by_state: Record<string, number>
  total_servers: number
  active_jobs: number
  failed_jobs_24h: number
  unhealthy_servers: number
  open_abuse_reports: number
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

  power: (id: string, action: PowerAction, force = false) =>
    request<Job>(`/api/v1/servers/${id}/power`, {
      method: 'POST',
      body: JSON.stringify({ action, force }),
    }),

  powerState: (id: string, fresh = false) =>
    request<PowerState>(`/api/v1/servers/${id}/power${fresh ? '?fresh=1' : ''}`),

  consoleTicket: (id: string) =>
    request<{ ticket: string; expires_in: number }>(`/api/v1/console/${id}/ticket`, {
      method: 'POST',
    }),

  reinstall: (
    id: string,
    body: {
      os_template_id: string
      hostname?: string
      raid_level: string
      ssh_key_ids: string[]
      confirm_data_loss: boolean
      root_password?: string
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
  forceCancelJob: (id: string, reason = 'stuck job') =>
    request<Job>(`/api/v1/admin/jobs/${id}/force-cancel`, { method: 'POST', body: JSON.stringify({ reason }) }),

  osTemplates: () => request<OSTemplate[]>('/api/v1/os-templates'),
  netboot: () => request<NetbootReport>('/api/v1/admin/images/netboot'),

  sshKeys: () => request<SSHKey[]>('/api/v1/ssh-keys'),
  addSshKey: (name: string, publicKey: string) =>
    request<SSHKey>('/api/v1/ssh-keys', {
      method: 'POST',
      body: JSON.stringify({ name, public_key: publicKey }),
    }),
  deleteSshKey: (id: string) =>
    request<void>(`/api/v1/ssh-keys/${id}`, { method: 'DELETE' }),

  // --- admin: fleet ---
  fleet: () => request<AdminServer[]>('/api/v1/admin/servers'),
  fleetSummary: () => request<FleetSummary>('/api/v1/admin/summary'),
  adminServer: (id: string) => request<AdminServer>(`/api/v1/admin/servers/${id}`),
  createServer: (body: {
    serial: string
    cimc_ip: string
    cimc_credential_ref: string
    model?: string
    datacenter?: string | null
    rack?: string | null
    rack_unit?: number | null
    switch_name?: string | null
    switch_port?: string | null
    customer_vlan?: number | null
    provisioning_mac?: string | null
    notes?: string | null
    prepare_bmc?: boolean
  }) => request<AdminServer>('/api/v1/admin/servers', { method: 'POST', body: JSON.stringify(body) }),
  updateServer: (id: string, body: Record<string, unknown>) =>
    request<AdminServer>(`/api/v1/admin/servers/${id}`, { method: 'PATCH', body: JSON.stringify(body) }),
  changeServerState: (id: string, state: ServerState, reason?: string) =>
    request<AdminServer>(`/api/v1/admin/servers/${id}/state`, {
      method: 'POST',
      body: JSON.stringify({ state, reason }),
    }),
  syncInventory: (id: string) =>
    request<Job>(`/api/v1/admin/servers/${id}/inventory-sync`, { method: 'POST' }),
  healthCheck: (id: string) =>
    request<Job>(`/api/v1/admin/servers/${id}/health-check`, { method: 'POST' }),
  wipe: (id: string, method: 'secure' | 'zero') =>
    request<Job>(`/api/v1/admin/servers/${id}/wipe`, {
      method: 'POST',
      body: JSON.stringify({ confirm_data_loss: true, method }),
    }),
  suspend: (id: string, reason: string) =>
    request<AdminServer>(`/api/v1/admin/servers/${id}/suspend?reason=${encodeURIComponent(reason)}`, {
      method: 'POST',
    }),
  unsuspend: (id: string) =>
    request<AdminServer>(`/api/v1/admin/servers/${id}/unsuspend`, { method: 'POST' }),
  assignIp: (serverId: string, body: { block_id: string; address: string; is_primary: boolean }) =>
    request<IPAssignment>(`/api/v1/admin/servers/${serverId}/ips`, {
      method: 'POST',
      body: JSON.stringify(body),
    }),
  releaseIp: (assignmentId: string) =>
    request<void>(`/api/v1/admin/ips/${assignmentId}`, { method: 'DELETE' }),

  // --- admin: BMC ---
  prepareBmc: (id: string) =>
    request<Job>(`/api/v1/admin/servers/${id}/prepare-bmc`, { method: 'POST' }),
  testBmc: (id: string) =>
    request<BmcTestReport>(`/api/v1/admin/servers/${id}/bmc/test`, { method: 'POST' }),
  launchKvm: (id: string) =>
    request<{
      html5: string | null
      java: string | null
      cimc: string
      firmware: string | null
      tokens_unsupported?: boolean
      reason?: string | null
      /** What each known HTML5 viewer path answered, for when none was found. */
      probe?: Array<{ path: string; status: number | null; viewer?: boolean; error?: string; location?: string | null }>
      /** Whether the CIMC lets the viewer be shown inside the panel; null when unknown. */
      embeddable?: boolean | null
    }>(
      `/api/v1/admin/servers/${id}/kvm`,
      { method: 'POST' },
    ),
  bmcSettings: (id: string) =>
    request<{
      protocol: string
      ipmi_port: number
      redfish_port: number
      ipmi_cipher_suite: string
      cipher_source: string
      credential_ref: string
      credential_resolves: boolean
      username: string | null
    }>(`/api/v1/admin/servers/${id}/bmc`),
  system: () => request<SystemStatus>('/api/v1/admin/system'),
  fleetPower: (fresh = false) =>
    request<{ servers: Record<string, PowerState>; checked_at: string }>(
      `/api/v1/admin/power${fresh ? '?fresh=1' : ''}`,
    ),
  sensors: (id: string, fresh = false) =>
    request<SensorReport>(`/api/v1/admin/servers/${id}/sensors${fresh ? '?fresh=1' : ''}`),
  eventLog: (id: string, fresh = false) =>
    request<SelReport>(`/api/v1/admin/servers/${id}/sel${fresh ? '?fresh=1' : ''}`),
  clearEventLog: (id: string) =>
    request<{ cleared: boolean; entries_removed: number }>(`/api/v1/admin/servers/${id}/sel`, {
      method: 'DELETE',
    }),
  bmcInfo: (id: string, fresh = false) =>
    request<BmcInfo>(`/api/v1/admin/servers/${id}/bmc/info${fresh ? '?fresh=1' : ''}`),
  identify: (id: string, body: { seconds?: number; force?: boolean }) =>
    request<{ led: string }>(`/api/v1/admin/servers/${id}/identify`, {
      method: 'POST',
      body: JSON.stringify(body),
    }),
  bmcReset: (id: string) =>
    request<{ reset: string; note: string }>(`/api/v1/admin/servers/${id}/bmc/reset`, {
      method: 'POST',
    }),
  setPowerPolicy: (id: string, policy: string) =>
    request<{ policy: string; chassis: Record<string, string> }>(
      `/api/v1/admin/servers/${id}/bmc/power-policy`,
      { method: 'POST', body: JSON.stringify({ policy }) },
    ),
  rotateBmcPassword: (id: string, password?: string) =>
    request<{ username: string; password: string; verified: boolean; stored: boolean; error: string | null }>(
      `/api/v1/admin/servers/${id}/bmc/password`,
      { method: 'POST', body: JSON.stringify(password ? { password } : {}) },
    ),
  configureRaid: (id: string, level: string) =>
    request<Job>(`/api/v1/admin/servers/${id}/raid`, {
      method: 'POST',
      body: JSON.stringify({ level, confirm_data_loss: true }),
    }),
  bootOverride: (id: string, device: BootDevice, then: BootFollowUp) =>
    request<Job>(`/api/v1/admin/servers/${id}/boot`, {
      method: 'POST',
      body: JSON.stringify({ device, then }),
    }),
  vmedia: (id: string) => request<VmediaStatus>(`/api/v1/admin/servers/${id}/vmedia`),
  vmediaBoot: (id: string, imageId: string, boot = true) =>
    request<Job>(`/api/v1/admin/servers/${id}/vmedia/boot`, {
      method: 'POST',
      body: JSON.stringify({ image_id: imageId, boot }),
    }),
  vmediaEject: (id: string) =>
    request<Job>(`/api/v1/admin/servers/${id}/vmedia/eject`, { method: 'POST' }),

  // --- admin: images ---
  images: () => request<Image[]>('/api/v1/admin/images'),
  fetchImage: (url: string, name?: string) =>
    request<{ image: Image; job: Job }>('/api/v1/admin/images/fetch', {
      method: 'POST',
      body: JSON.stringify({ url, name: name || null }),
    }),
  scanImages: () => request<Image[]>('/api/v1/admin/images/scan', { method: 'POST' }),
  updateImage: (id: string, body: { name?: string; notes?: string }) =>
    request<Image>(`/api/v1/admin/images/${id}`, { method: 'PATCH', body: JSON.stringify(body) }),
  deleteImage: (id: string) => request<void>(`/api/v1/admin/images/${id}`, { method: 'DELETE' }),

  // --- admin: jobs ---
  adminJobs: (params: { state?: string; server_id?: string } = {}) => {
    const query = new URLSearchParams(
      Object.entries(params).filter(([, v]) => v) as [string, string][],
    ).toString()
    return request<Job[]>(`/api/v1/admin/jobs${query ? `?${query}` : ''}`)
  },
  adminJob: (id: string) => request<AdminJobDetail>(`/api/v1/admin/jobs/${id}`),

  // --- admin: customers & subscriptions ---
  customers: () => request<AdminCustomer[]>('/api/v1/admin/customers'),
  createCustomer: (body: {
    email: string
    password: string
    company_name?: string | null
    contact_name?: string | null
    phone?: string | null
    billing_ref?: string | null
    is_admin?: boolean
  }) => request<AdminCustomer>('/api/v1/admin/customers', { method: 'POST', body: JSON.stringify(body) }),
  updateCustomer: (id: string, body: Record<string, unknown>) =>
    request<AdminCustomer>(`/api/v1/admin/customers/${id}`, { method: 'PATCH', body: JSON.stringify(body) }),
  subscriptions: (params: { customer_id?: string; server_id?: string; include_ended?: boolean } = {}) => {
    const query = new URLSearchParams(
      Object.entries(params)
        .filter(([, v]) => v !== undefined && v !== false)
        .map(([k, v]) => [k, String(v)]),
    ).toString()
    return request<Subscription[]>(`/api/v1/admin/subscriptions${query ? `?${query}` : ''}`)
  },
  createSubscription: (body: {
    customer_id: string
    server_id: string
    plan_name: string
    monthly_price?: number | null
    currency?: string
    bandwidth_quota_tb?: number | null
  }) => request<Subscription>('/api/v1/admin/subscriptions', { method: 'POST', body: JSON.stringify(body) }),
  endSubscription: (id: string) =>
    request<Subscription>(`/api/v1/admin/subscriptions/${id}/end`, { method: 'POST' }),

  // --- admin: IPAM ---
  ipBlocks: () => request<IPBlock[]>('/api/v1/admin/ip-blocks'),
  createIpBlock: (body: {
    cidr: string
    gateway?: string | null
    routing_mode?: string
    vlan?: number | null
    datacenter?: string | null
    source?: string | null
  }) => request<IPBlock>('/api/v1/admin/ip-blocks', { method: 'POST', body: JSON.stringify(body) }),
  freeAddresses: (blockId: string, limit = 32) =>
    request<{ cidr: string; total_hosts: number; assigned: number; free_sample: string[] }>(
      `/api/v1/admin/ip-blocks/${blockId}/free?limit=${limit}`,
    ),

  // --- admin: credentials ---
  credentialBackend: () => request<{ backend: string; writable: boolean }>('/api/v1/admin/credentials/backend'),
  checkCredential: (ref: string) =>
    request<{ ref: string; resolves: boolean; username?: string }>(
      `/api/v1/admin/credentials/check?ref=${encodeURIComponent(ref)}`,
    ),
  storeCredential: (body: { ref: string; username: string; password: string }) =>
    request<{ ref: string; stored: boolean }>('/api/v1/admin/credentials', {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  // --- admin: audit ---
  audit: (params: { action?: string; target_id?: string; limit?: number } = {}) => {
    const query = new URLSearchParams(
      Object.entries(params).filter(([, v]) => v !== undefined).map(([k, v]) => [k, String(v)]),
    ).toString()
    return request<AuditEntry[]>(`/api/v1/admin/audit${query ? `?${query}` : ''}`)
  },
}

export interface AuditEntry {
  timestamp: string
  actor_type: string
  actor_label: string | null
  action: string
  target_type: string | null
  target_id: string | null
  source_ip: string | null
  detail: Record<string, unknown>
}

/**
 * URL for the SOL console websocket.
 *
 * Browsers cannot set headers on a websocket handshake, so authentication
 * rides in the query string -- as a one-minute, single-use ticket fetched with
 * the normal bearer token, never the session token itself, which would end up
 * in the reverse proxy's access log.
 */
export async function consoleUrl(serverId: string, force = false): Promise<string> {
  const { ticket } = await api.consoleTicket(serverId)
  const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:'
  const params = new URLSearchParams({ ticket })
  if (force) params.set('force', '1')
  return `${protocol}//${location.host}/api/v1/console/${serverId}/sol?${params}`
}

export type PowerAction = 'on' | 'off' | 'force_off' | 'reset' | 'cycle'

export interface PowerState {
  state: 'on' | 'off' | 'unknown'
  via: string | null
  checked_at: string
  error: string | null
  cached: boolean
  /** The BMC missed the last poll; `state` is the last good reading, from `last_seen`. */
  stale?: boolean
  last_seen?: string | null
}

export interface SystemStatus {
  database: string
  redis: string
  workers: Array<{ name: string; queues: string[] }>
  queues: Record<string, { workers: string[]; handles: string }>
  ipmitool: { path: string; version: string } | null
  bmc_protocol: string
  ipmi_cipher_suite: string
  secrets_backend: string
  workers_error?: string
}

// ---------------------------------------------------------------------------
// IPMI management types
// ---------------------------------------------------------------------------

export type SensorKind = 'temperature' | 'fan' | 'voltage' | 'power' | 'current' | 'discrete'

export interface Sensor {
  name: string
  number: number | null
  status: 'ok' | 'warning' | 'critical' | 'no_reading' | 'unknown'
  raw_status: string
  entity: string
  reading: string
  value: number | null
  unit: string | null
  kind: SensorKind
}

export type PowerSource = 'dcmi' | 'redfish' | 'psu_output' | 'psu_input' | 'sensors'

export interface Utilization {
  overall: number | null
  cpu: number | null
  memory: number | null
  io: number | null
}

export interface SensorReport {
  sensors: Sensor[]
  power: { watts: number; minimum: number | null; maximum: number | null; average: number | null; source?: PowerSource } | null
  /** The CIMC's own CPU / memory / IO figures in percent; null when the BMC has none. */
  utilization: Utilization | null
  utilization_error?: string | null
  checked_at: string
  /** 'ipmi', or 'redfish' when IPMI was not answering (fallback_reason says why). */
  via: string
  fallback_reason?: string | null
  /** The BMC missed the last poll; these are the last good readings. */
  stale?: boolean
  error?: string
}

export interface SelEntry {
  id: number
  timestamp: string | null
  raw_time: string
  sensor: string
  event: string
  direction: string
  detail: string | null
  severity: 'info' | 'warning' | 'critical'
}

export interface SelReport {
  info: {
    entries: number
    free_bytes: number | null
    percent_used: number | null
    last_add: string | null
    last_clear: string | null
    overflow: boolean
  }
  entries: SelEntry[]
  checked_at: string
}

export interface BmcInfo {
  mc: {
    firmware: string | null
    ipmi_version: string | null
    manufacturer: string | null
    product: string | null
    device_id: string | null
    available: boolean
  }
  lan: {
    channel: number
    ip_address: string | null
    subnet_mask: string | null
    gateway: string | null
    mac_address: string | null
    source: string | null
    vlan: string | null
  }
  chassis: Record<string, string>
  users: Array<{ id: number; name: string; privilege: string; ipmi_messaging: boolean }>
  checked_at: string
}

export type BootDevice = 'pxe' | 'disk' | 'cdrom' | 'bios'
export type BootFollowUp = 'none' | 'reset' | 'cycle' | 'on'

export interface VmediaStatus {
  supported: boolean
  media: Array<{
    id: string | null
    name: string | null
    media_types: string[]
    inserted: boolean
    image: string | null
    image_name: string | null
  }>
  error: string | null
  checked_at: string
}

export interface Image {
  id: string
  name: string
  filename: string
  size_bytes: number | null
  sha256: string | null
  source_url: string | null
  status: 'ready' | 'fetching' | 'failed'
  error: string | null
  uploaded_by: string | null
  notes: string | null
  created_at: string
  url: string
}

/**
 * Upload an ISO with progress. XMLHttpRequest rather than fetch because only
 * XHR reports upload progress, and a 4 GB file without a progress bar looks
 * like a hang.
 */
export function uploadImage(
  file: File,
  name: string,
  onProgress: (fraction: number) => void,
): { promise: Promise<Image>; abort: () => void } {
  const xhr = new XMLHttpRequest()
  const params = new URLSearchParams({ filename: file.name, name: name || file.name })
  const promise = new Promise<Image>((resolve, reject) => {
    xhr.open('PUT', `/api/v1/admin/images/upload?${params}`)
    const token = getToken()
    if (token) xhr.setRequestHeader('Authorization', `Bearer ${token}`)
    xhr.setRequestHeader('Content-Type', 'application/octet-stream')
    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable) onProgress(event.loaded / event.total)
    }
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        resolve(JSON.parse(xhr.responseText) as Image)
        return
      }
      let detail = `${xhr.status} ${xhr.statusText}`
      try {
        const body = JSON.parse(xhr.responseText)
        if (typeof body.detail === 'string') detail = body.detail
      } catch {
        /* keep the status line */
      }
      reject(new ApiError(xhr.status, detail))
    }
    xhr.onerror = () => reject(new ApiError(0, 'upload failed: connection lost'))
    xhr.onabort = () => reject(new ApiError(0, 'upload cancelled'))
    xhr.send(file)
  })
  return { promise, abort: () => xhr.abort() }
}
