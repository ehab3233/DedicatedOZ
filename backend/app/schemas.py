"""Request and response models.

Customer-facing shapes deliberately omit CIMC addressing, credential refs and
raw BMC output. Admin shapes carry them.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, BeforeValidator, ConfigDict, EmailStr, Field, field_validator

from app.enums import ActorType, InstallMethod, JobState, JobType, RaidLevel, ServerState


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


def _ip_to_str(value: object) -> object:
    """psycopg returns INET columns as `ipaddress` objects; the API speaks strings."""
    return None if value is None else str(value)


#: A string field that accepts what the database hands back for INET columns.
IPStr = Annotated[str, BeforeValidator(_ip_to_str)]
OptionalIPStr = Annotated[str | None, BeforeValidator(_ip_to_str)]


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=1024)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    is_admin: bool


class CustomerOut(ORMModel):
    id: uuid.UUID
    email: str
    company_name: str | None
    contact_name: str | None
    is_admin: bool
    created_at: datetime


class CustomerCreate(BaseModel):
    email: EmailStr
    password: str = Field(min_length=12, max_length=1024)
    company_name: str | None = Field(default=None, max_length=255)
    contact_name: str | None = Field(default=None, max_length=255)
    phone: str | None = Field(default=None, max_length=64)
    billing_ref: str | None = Field(default=None, max_length=128)
    is_admin: bool = False


class CustomerUpdate(BaseModel):
    company_name: str | None = None
    contact_name: str | None = None
    phone: str | None = None
    billing_ref: str | None = None
    is_active: bool | None = None
    password: str | None = Field(default=None, min_length=12, max_length=1024)


class AdminCustomerOut(CustomerOut):
    phone: str | None
    billing_ref: str | None
    is_active: bool
    active_servers: int = 0


class SubscriptionCreate(BaseModel):
    customer_id: uuid.UUID
    server_id: uuid.UUID
    plan_name: str = Field(min_length=1, max_length=128)
    monthly_price: float | None = None
    currency: str = Field(default="USD", min_length=3, max_length=3)
    billing_ref: str | None = None
    bandwidth_quota_tb: int | None = None


class SubscriptionOut(ORMModel):
    id: uuid.UUID
    customer_id: uuid.UUID
    server_id: uuid.UUID
    plan_name: str
    monthly_price: float | None
    currency: str
    billing_ref: str | None
    bandwidth_quota_tb: int | None
    started_at: datetime
    ended_at: datetime | None
    customer_email: str | None = None
    server_serial: str | None = None


class CredentialCreate(BaseModel):
    """Store a BMC credential in the secrets backend under `ref`."""

    ref: str = Field(min_length=1, max_length=255, pattern=r"^[A-Za-z0-9_./-]+$")
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class APITokenCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    expires_in_days: int | None = Field(default=None, ge=1, le=3650)


class APITokenOut(ORMModel):
    id: uuid.UUID
    name: str
    token_prefix: str
    created_at: datetime
    last_used_at: datetime | None
    expires_at: datetime | None


class APITokenCreated(APITokenOut):
    #: Returned once, at creation. Never retrievable again.
    token: str


# ---------------------------------------------------------------------------
# SSH keys
# ---------------------------------------------------------------------------


class SSHKeyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    public_key: str = Field(min_length=32, max_length=16384)

    @field_validator("public_key")
    @classmethod
    def _looks_like_a_key(cls, value: str) -> str:
        value = value.strip()
        valid_prefixes = (
            "ssh-rsa",
            "ssh-ed25519",
            "ecdsa-sha2-nistp256",
            "ecdsa-sha2-nistp384",
            "ecdsa-sha2-nistp521",
            "sk-ssh-ed25519@openssh.com",
            "sk-ecdsa-sha2-nistp256@openssh.com",
        )
        if not value.startswith(valid_prefixes):
            raise ValueError("not an OpenSSH public key")
        if "\n" in value:
            raise ValueError("public key must be a single line")
        return value


class SSHKeyOut(ORMModel):
    id: uuid.UUID
    name: str
    public_key: str
    fingerprint: str
    created_at: datetime


# ---------------------------------------------------------------------------
# Servers
# ---------------------------------------------------------------------------


class ServerHealthOut(BaseModel):
    status: str | None
    checked_at: datetime | None
    subsystems: dict = Field(default_factory=dict)
    #: Polls in a row the BMC has not answered; the status above is the last
    #: verdict until a few are missed.
    missed_polls: int = 0
    last_error: str | None = None


class ServerOut(ORMModel):
    """Customer view. No CIMC address, no credential ref."""

    id: uuid.UUID
    serial: str
    hostname: str | None
    model: str
    state: ServerState
    cpu_model: str | None
    cpu_count: int | None
    cpu_cores_total: int | None
    ram_gb: int | None
    datacenter: str | None
    last_power_state: str | None
    health_status: str | None
    health_checked_at: datetime | None
    created_at: datetime


class ServerDetailOut(ServerOut):
    drives: list[dict] = Field(default_factory=list)
    nics: list[dict] = Field(default_factory=list)
    ip_addresses: list[IPAssignmentOut] = Field(default_factory=list)
    health: ServerHealthOut | None = None


class AdminServerOut(ServerDetailOut):
    cimc_ip: IPStr
    cimc_credential_ref: str
    bmc_protocol: str | None = None
    ipmi_port: int | None = None
    redfish_port: int | None = None
    ipmi_cipher_suite: str | None = None
    cimc_firmware: str | None
    bios_version: str | None
    rack: str | None
    rack_unit: int | None
    switch_name: str | None
    switch_port: str | None
    customer_vlan: int | None
    provisioning_mac: str | None
    last_wiped_at: datetime | None
    state_changed_at: datetime | None = None
    bmc_prepared_at: datetime | None = None
    notes: str | None
    customer_email: str | None = None


BMCProtocol = Field(default=None, pattern="^(auto|redfish|ipmi)$")


class ServerCreate(BaseModel):
    serial: str = Field(min_length=1, max_length=64)
    cimc_ip: str
    cimc_credential_ref: str = Field(min_length=1, max_length=255)
    bmc_protocol: str | None = BMCProtocol
    ipmi_port: int | None = Field(default=None, ge=1, le=65535)
    redfish_port: int | None = Field(default=None, ge=1, le=65535)
    model: str = "UCSC-C220-M4S"
    datacenter: str | None = None
    rack: str | None = None
    rack_unit: int | None = Field(default=None, ge=1, le=60)
    switch_name: str | None = None
    switch_port: str | None = None
    customer_vlan: int | None = Field(default=None, ge=1, le=4094)
    provisioning_mac: str | None = None
    notes: str | None = None
    #: Queue Prepare BMC straight away, ahead of the inventory sync.
    prepare_bmc: bool = True


class ServerUpdate(BaseModel):
    hostname: str | None = None
    cimc_ip: str | None = None
    bmc_protocol: str | None = BMCProtocol
    ipmi_port: int | None = Field(default=None, ge=1, le=65535)
    redfish_port: int | None = Field(default=None, ge=1, le=65535)
    ipmi_cipher_suite: str | None = Field(default=None, pattern=r"^(auto|\d{1,2})$")
    datacenter: str | None = None
    rack: str | None = None
    rack_unit: int | None = Field(default=None, ge=1, le=60)
    switch_name: str | None = None
    switch_port: str | None = None
    customer_vlan: int | None = Field(default=None, ge=1, le=4094)
    provisioning_mac: str | None = None
    cimc_credential_ref: str | None = None
    notes: str | None = None


class ServerStateChange(BaseModel):
    state: ServerState
    reason: str | None = None


# ---------------------------------------------------------------------------
# Power / provisioning requests
# ---------------------------------------------------------------------------


POWER_ACTIONS = ("on", "off", "force_off", "reset", "cycle")


class PowerRequest(BaseModel):
    #: on | off (graceful, ACPI) | force_off | reset | cycle
    action: str
    #: Admin only: run even though another job holds the server -- for a
    #: machine stuck mid-install. Ignored for customers.
    force: bool = False

    @field_validator("action")
    @classmethod
    def _known_action(cls, value: str) -> str:
        if value not in POWER_ACTIONS:
            raise ValueError("action must be one of: " + ", ".join(POWER_ACTIONS))
        return value


class PowerStateOut(BaseModel):
    state: str
    via: str | None = None
    checked_at: str
    error: str | None = None
    cached: bool = False


BOOT_DEVICES = ("pxe", "disk", "cdrom", "bios")
BOOT_FOLLOW_UPS = ("none", "reset", "cycle", "on")


class BootOverrideRequest(BaseModel):
    """One-time boot device, and whether to restart now so it takes effect."""

    device: str
    #: none: just set the flag (BMCs drop it after ~60 s with no restart).
    #: reset: hard reset now (power on if off). cycle: off, then on. on: power on.
    then: str = "reset"

    @field_validator("device")
    @classmethod
    def _known_device(cls, value: str) -> str:
        if value not in BOOT_DEVICES:
            raise ValueError("device must be one of: " + ", ".join(BOOT_DEVICES))
        return value

    @field_validator("then")
    @classmethod
    def _known_follow_up(cls, value: str) -> str:
        if value not in BOOT_FOLLOW_UPS:
            raise ValueError("then must be one of: " + ", ".join(BOOT_FOLLOW_UPS))
        return value


class IdentifyRequest(BaseModel):
    #: Seconds to blink the locator LED; 0 switches it off.
    seconds: int = Field(default=300, ge=0, le=255 * 60)
    #: Leave it on until switched off.
    force: bool = False


class PowerPolicyRequest(BaseModel):
    policy: str = Field(pattern="^(always-on|always-off|previous)$")


class BmcPasswordRequest(BaseModel):
    #: Omit to have one generated. 1-16 characters (the IPMI 1.5 limit every
    #: BMC honours), and CIMC strong-password rules want mixed case, a digit
    #: and a symbol.
    password: str | None = Field(default=None, min_length=8, max_length=16)


class VmediaBootRequest(BaseModel):
    image_id: uuid.UUID
    #: Set a one-time CD boot and power cycle after mounting. Off = mount only.
    boot: bool = True


class ImageFetchRequest(BaseModel):
    url: str = Field(min_length=8, max_length=2048)
    name: str | None = Field(default=None, max_length=255)
    notes: str | None = None


class ImageUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    notes: str | None = None


class ImageOut(ORMModel):
    id: uuid.UUID
    name: str
    filename: str
    size_bytes: int | None
    sha256: str | None
    source_url: str | None
    status: str
    error: str | None
    uploaded_by: str | None
    notes: str | None
    created_at: datetime
    #: Where the BMC fetches it from.
    url: str = ""


class ReinstallRequest(BaseModel):
    os_template_id: uuid.UUID
    hostname: str | None = Field(default=None, max_length=255)
    raid_level: RaidLevel = RaidLevel.RAID1
    ssh_key_ids: list[uuid.UUID] = Field(default_factory=list)
    #: Optional; if unset the machine is key-only, which is the safer default.
    root_password: str | None = Field(default=None, min_length=12, max_length=256)
    #: Must be sent explicitly. Reinstall destroys everything on the array.
    confirm_data_loss: bool = False


class RescueRequest(BaseModel):
    #: Rescue always boots to RAM; nothing on disk is touched.
    ssh_key_ids: list[uuid.UUID] = Field(default_factory=list)


class WipeRequest(BaseModel):
    confirm_data_loss: bool = False
    #: "secure" uses ATA secure erase / nvme format; "zero" overwrites.
    method: str = "secure"


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------


class JobLogEntryOut(ORMModel):
    sequence: int
    timestamp: datetime
    level: str
    message: str


class AdminJobLogEntryOut(JobLogEntryOut):
    request: dict | None
    response: dict | None
    customer_visible: bool


class JobOut(ORMModel):
    id: uuid.UUID
    type: JobType
    state: JobState
    server_id: uuid.UUID | None
    progress: int
    stage: str | None
    error: str | None
    requested_by_type: ActorType
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class JobDetailOut(JobOut):
    log: list[JobLogEntryOut] = Field(default_factory=list)
    result: dict = Field(default_factory=dict)


class AdminJobDetailOut(JobOut):
    log: list[AdminJobLogEntryOut] = Field(default_factory=list)
    payload: dict = Field(default_factory=dict)
    result: dict = Field(default_factory=dict)
    celery_task_id: str | None
    attempts: int


# ---------------------------------------------------------------------------
# OS templates
# ---------------------------------------------------------------------------


class OSTemplateOut(ORMModel):
    id: uuid.UUID
    slug: str
    name: str
    family: str
    version: str
    install_method: InstallMethod
    default_raid_level: RaidLevel


class NetbootFileOut(BaseModel):
    role: str
    path: str
    url: str
    present: bool
    size_bytes: int | None
    modified_at: datetime | None


class NetbootTemplateOut(BaseModel):
    id: uuid.UUID
    slug: str
    name: str
    version: str
    install_method: InstallMethod
    is_public: bool
    files: list[NetbootFileOut]
    ready: bool


class NetbootRamdiskOut(BaseModel):
    files: list[NetbootFileOut]
    ready: bool


class NetbootReportOut(BaseModel):
    """What the PXE rails need on disk, and what is there."""

    asset_dir: str
    base_url: str
    ramdisk: NetbootRamdiskOut
    templates: list[NetbootTemplateOut]


class OSTemplateCreate(BaseModel):
    slug: str = Field(min_length=1, max_length=64)
    name: str
    family: str
    version: str
    install_method: InstallMethod
    config_template: str
    kernel_path: str | None = None
    initrd_path: str | None = None
    kernel_args: str | None = None
    iso_path: str | None = None
    default_raid_level: RaidLevel = RaidLevel.RAID1
    is_public: bool = True
    sort_order: int = 100


# ---------------------------------------------------------------------------
# IPAM
# ---------------------------------------------------------------------------


class IPAssignmentOut(ORMModel):
    id: uuid.UUID
    address: IPStr
    prefix_len: int
    gateway: OptionalIPStr = None
    is_primary: bool
    rdns: str | None


class RDNSUpdate(BaseModel):
    rdns: str | None = Field(default=None, max_length=255)


class IPBlockCreate(BaseModel):
    cidr: str
    gateway: str | None = None
    routing_mode: str = Field(default="bridged", pattern="^(bridged|routed)$")
    vlan: int | None = Field(default=None, ge=1, le=4094)
    datacenter: str | None = None
    source: str | None = None
    notes: str | None = None


class IPAssignCreate(BaseModel):
    block_id: uuid.UUID
    address: str
    is_primary: bool = False


class IPBlockOut(ORMModel):
    id: uuid.UUID
    cidr: str
    version: int
    gateway: OptionalIPStr
    routing_mode: str
    vlan: int | None
    datacenter: str | None
    source: str | None = None
    is_assignable: bool
    total_hosts: int = 0
    assigned: int = 0


# ---------------------------------------------------------------------------
# Installer callbacks
# ---------------------------------------------------------------------------


class InstallerProgress(BaseModel):
    stage: str = Field(max_length=64)
    progress: int = Field(ge=0, le=100)
    message: str | None = Field(default=None, max_length=4000)


class InstallerComplete(BaseModel):
    success: bool
    message: str | None = Field(default=None, max_length=8000)
    #: Reported by the wipe rail; a wipe with an empty list does not count.
    drives_wiped: list[str] = Field(default_factory=list)
    #: Host key fingerprints, so customers can verify their first SSH login.
    host_keys: list[str] = Field(default_factory=list)
    detail: dict = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Bandwidth
# ---------------------------------------------------------------------------


class BandwidthPoint(BaseModel):
    timestamp: datetime
    rx_bps: float
    tx_bps: float


class BandwidthSeries(BaseModel):
    server_id: uuid.UUID
    period: str
    points: list[BandwidthPoint]
    total_rx_bytes: int
    total_tx_bytes: int


ServerDetailOut.model_rebuild()
