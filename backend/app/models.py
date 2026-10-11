"""SQLAlchemy models.

Note on credentials: `Server.cimc_credential_ref` is an opaque pointer into the
secrets backend. Passwords never land in this database.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import INET, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.enums import (
    ActorType,
    InstallMethod,
    JobState,
    JobType,
    RaidLevel,
    ServerState,
)


class Base(DeclarativeBase):
    pass


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------


class Customer(Base, TimestampMixin):
    __tablename__ = "customers"

    id: Mapped[uuid.UUID] = _uuid_pk()
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    company_name: Mapped[str | None] = mapped_column(String(255))
    contact_name: Mapped[str | None] = mapped_column(String(255))
    phone: Mapped[str | None] = mapped_column(String(64))
    # Pointer into WHMCS/HostBill/Blesta. We deliberately do not build billing.
    billing_ref: Mapped[str | None] = mapped_column(String(128), index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    #: Mail when a reinstall, rescue boot or wipe on their server finishes.
    notify_jobs: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("true"), nullable=False
    )

    subscriptions: Mapped[list[Subscription]] = relationship(back_populates="customer")
    ssh_keys: Mapped[list[SSHKey]] = relationship(
        back_populates="customer", cascade="all, delete-orphan"
    )


class APIToken(Base, TimestampMixin):
    """Customer-scoped API tokens. Only the hash is stored."""

    __tablename__ = "api_tokens"

    id: Mapped[uuid.UUID] = _uuid_pk()
    customer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("customers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    token_prefix: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    token_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SSHKey(Base, TimestampMixin):
    __tablename__ = "ssh_keys"

    id: Mapped[uuid.UUID] = _uuid_pk()
    customer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("customers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    public_key: Mapped[str] = mapped_column(Text, nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(128), nullable=False)

    customer: Mapped[Customer] = relationship(back_populates="ssh_keys")

    __table_args__ = (UniqueConstraint("customer_id", "fingerprint", name="uq_ssh_key_per_cust"),)


# ---------------------------------------------------------------------------
# Inventory
# ---------------------------------------------------------------------------


class Server(Base, TimestampMixin):
    __tablename__ = "servers"

    id: Mapped[uuid.UUID] = _uuid_pk()
    serial: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    hostname: Mapped[str | None] = mapped_column(String(255))
    model: Mapped[str] = mapped_column(String(128), default="UCSC-C220-M4S", nullable=False)

    # --- out-of-band -------------------------------------------------------
    cimc_ip: Mapped[str] = mapped_column(INET, nullable=False, unique=True)
    #: Opaque key into the secrets backend, e.g. "cimc/SN12345".
    cimc_credential_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    cimc_firmware: Mapped[str | None] = mapped_column(String(64))
    #: "customer": a server the panel provisions and hands out. "management":
    #: the platform's own machine, read (health, readings, events) but never
    #: changed from here.
    role: Mapped[str] = mapped_column(
        String(16), default="customer", server_default=text("'customer'"), nullable=False
    )
    bios_version: Mapped[str | None] = mapped_column(String(64))
    #: Redfish system path, discovered once and cached.
    redfish_system_path: Mapped[str | None] = mapped_column(String(255))
    #: auto | redfish | ipmi. Null means the global DOZ_BMC_PROTOCOL.
    bmc_protocol: Mapped[str | None] = mapped_column(String(16))
    #: Non-standard BMC ports, for BMCs behind a port forward (and the
    #: simulator). Null means 623 and 443.
    ipmi_port: Mapped[int | None] = mapped_column(Integer)
    redfish_port: Mapped[int | None] = mapped_column(Integer)
    #: IPMI cipher suite for this BMC: a number, "auto" to let ipmitool
    #: probe, or null for the platform default. Test connection sets it when
    #: the default does not work.
    ipmi_cipher_suite: Mapped[str | None] = mapped_column(String(8))

    # --- physical ----------------------------------------------------------
    datacenter: Mapped[str | None] = mapped_column(String(64))
    rack: Mapped[str | None] = mapped_column(String(64))
    rack_unit: Mapped[int | None] = mapped_column(Integer)
    switch_name: Mapped[str | None] = mapped_column(String(128))
    switch_port: Mapped[str | None] = mapped_column(String(64))
    #: VLAN the customer-facing NIC sits on.
    customer_vlan: Mapped[int | None] = mapped_column(Integer)
    #: MAC of the NIC we PXE from. The netboot rail is keyed on this.
    provisioning_mac: Mapped[str | None] = mapped_column(String(32), unique=True, index=True)

    # --- spec (denormalised for listing/filtering) ------------------------
    cpu_model: Mapped[str | None] = mapped_column(String(128))
    cpu_count: Mapped[int | None] = mapped_column(Integer)
    cpu_cores_total: Mapped[int | None] = mapped_column(Integer)
    ram_gb: Mapped[int | None] = mapped_column(Integer)
    #: Full discovered inventory as returned by Redfish.
    hardware_spec: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)

    # --- lifecycle ---------------------------------------------------------
    state: Mapped[ServerState] = mapped_column(
        String(32), default=ServerState.IN_STOCK, nullable=False, index=True
    )
    state_changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    #: Set when a wipe completes; cleared on assignment. Guards resale.
    last_wiped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str | None] = mapped_column(Text)

    # --- health ------------------------------------------------------------
    health_status: Mapped[str | None] = mapped_column(String(32))
    health_detail: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    health_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_power_state: Mapped[str | None] = mapped_column(String(16))
    #: When Prepare BMC last proved IPMI works on this CIMC. Null: never run.
    bmc_prepared_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    subscriptions: Mapped[list[Subscription]] = relationship(back_populates="server")
    jobs: Mapped[list[Job]] = relationship(back_populates="server")
    ip_assignments: Mapped[list[IPAssignment]] = relationship(back_populates="server")

    __table_args__ = (
        Index("ix_servers_state_dc", "state", "datacenter"),
        CheckConstraint("rack_unit IS NULL OR rack_unit BETWEEN 1 AND 60", name="ck_rack_unit"),
    )

    @property
    def active_subscription(self) -> Subscription | None:
        for sub in self.subscriptions:
            if sub.ended_at is None:
                return sub
        return None


class Subscription(Base, TimestampMixin):
    __tablename__ = "subscriptions"

    id: Mapped[uuid.UUID] = _uuid_pk()
    customer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("customers.id"), nullable=False, index=True
    )
    server_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("servers.id"), nullable=False, index=True
    )
    plan_name: Mapped[str] = mapped_column(String(128), nullable=False)
    monthly_price: Mapped[float | None] = mapped_column(Numeric(10, 2))
    currency: Mapped[str] = mapped_column(String(3), default="USD", nullable=False)
    #: Line item in the external billing system.
    billing_ref: Mapped[str | None] = mapped_column(String(128))
    bandwidth_quota_tb: Mapped[int | None] = mapped_column(Integer)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    customer: Mapped[Customer] = relationship(back_populates="subscriptions")
    server: Mapped[Server] = relationship(back_populates="subscriptions")

    __table_args__ = (
        Index(
            "uq_one_active_sub_per_server",
            "server_id",
            unique=True,
            postgresql_where=(ended_at.is_(None)),
        ),
    )


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------


class Job(Base, TimestampMixin):
    __tablename__ = "jobs"

    id: Mapped[uuid.UUID] = _uuid_pk()
    type: Mapped[JobType] = mapped_column(String(32), nullable=False, index=True)
    state: Mapped[JobState] = mapped_column(
        String(16), default=JobState.QUEUED, nullable=False, index=True
    )
    server_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("servers.id"), index=True, nullable=True
    )
    requested_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("customers.id"))
    requested_by_type: Mapped[ActorType] = mapped_column(
        String(16), default=ActorType.CUSTOMER, nullable=False
    )
    payload: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    result: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    progress: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: Stage name shown to the customer, e.g. "wiping disks".
    stage: Mapped[str | None] = mapped_column(String(64))
    celery_task_id: Mapped[str | None] = mapped_column(String(64), index=True)
    #: Bearer token the installer ramdisk uses to phone home. Hashed.
    callback_token_hash: Mapped[str | None] = mapped_column(String(255))
    callback_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    server: Mapped[Server | None] = relationship(back_populates="jobs")
    log_entries: Mapped[list[JobLogEntry]] = relationship(
        back_populates="job", cascade="all, delete-orphan", order_by="JobLogEntry.sequence"
    )

    __table_args__ = (Index("ix_jobs_server_created", "server_id", "created_at"),)


class JobLogEntry(Base):
    """One line of job history.

    `request` / `response` carry the raw BMC exchange. This is the thing that
    makes a vMedia failure at 2am debuggable, so it is stored verbatim (minus
    credentials, which the driver redacts before handing them over).
    """

    __tablename__ = "job_log_entries"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    job_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    level: Mapped[str] = mapped_column(String(16), default="info", nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    request: Mapped[dict | None] = mapped_column(JSONB)
    response: Mapped[dict | None] = mapped_column(JSONB)
    #: True for lines the customer may see. Raw BMC traffic is admin-only.
    customer_visible: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    job: Mapped[Job] = relationship(back_populates="log_entries")

    __table_args__ = (UniqueConstraint("job_id", "sequence", name="uq_job_log_seq"),)


# ---------------------------------------------------------------------------
# OS templates
# ---------------------------------------------------------------------------


class OSTemplate(Base, TimestampMixin):
    __tablename__ = "os_templates"

    id: Mapped[uuid.UUID] = _uuid_pk()
    slug: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    family: Mapped[str] = mapped_column(String(32), nullable=False)  # debian/rhel/windows
    version: Mapped[str] = mapped_column(String(32), nullable=False)
    install_method: Mapped[InstallMethod] = mapped_column(String(32), nullable=False)

    #: Netboot artifacts, resolved against `boot_asset_base_url`.
    kernel_path: Mapped[str | None] = mapped_column(String(512))
    initrd_path: Mapped[str | None] = mapped_column(String(512))
    #: Extra kernel command line, templated with Jinja.
    kernel_args: Mapped[str | None] = mapped_column(Text)
    #: ISO for the vMedia install path.
    iso_path: Mapped[str | None] = mapped_column(String(512))
    #: Jinja template name under installer/templates/ for the answer file.
    config_template: Mapped[str] = mapped_column(String(128), nullable=False)

    default_raid_level: Mapped[RaidLevel] = mapped_column(
        String(16), default=RaidLevel.RAID1, nullable=False
    )
    #: Hidden templates back rescue/wipe rails; customers never see them.
    is_public: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=100, nullable=False)


# ---------------------------------------------------------------------------
# Image store
# ---------------------------------------------------------------------------


class Image(Base, TimestampMixin):
    """An ISO on the management server, for virtual-media installs.

    The file lives under `settings.image_dir`, which nginx serves on the boot
    asset port; `filename` is the name there. Nothing else about the file is
    trusted: size and checksum are measured after the bytes arrive.
    """

    __tablename__ = "images"

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    filename: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    sha256: Mapped[str | None] = mapped_column(String(64))
    #: Where it was downloaded from, when it was.
    source_url: Mapped[str | None] = mapped_column(Text)
    #: ready | fetching | failed
    status: Mapped[str] = mapped_column(String(16), default="ready", nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    uploaded_by: Mapped[str | None] = mapped_column(String(255))
    notes: Mapped[str | None] = mapped_column(Text)


# ---------------------------------------------------------------------------
# IPAM
# ---------------------------------------------------------------------------


class IPBlock(Base, TimestampMixin):
    __tablename__ = "ip_blocks"

    id: Mapped[uuid.UUID] = _uuid_pk()
    cidr: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=4, nullable=False)
    gateway: Mapped[str | None] = mapped_column(INET)
    #: "routed" blocks are handed to a server whole; "bridged" are per-host.
    routing_mode: Mapped[str] = mapped_column(String(16), default="bridged", nullable=False)
    vlan: Mapped[int | None] = mapped_column(Integer)
    datacenter: Mapped[str | None] = mapped_column(String(64))
    #: Where the space came from — RIR, lease, or upstream allocation.
    source: Mapped[str | None] = mapped_column(String(128))
    is_assignable: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)

    assignments: Mapped[list[IPAssignment]] = relationship(back_populates="block")


class IPAssignment(Base, TimestampMixin):
    __tablename__ = "ip_assignments"

    id: Mapped[uuid.UUID] = _uuid_pk()
    block_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ip_blocks.id"), nullable=False)
    address: Mapped[str] = mapped_column(INET, nullable=False, index=True)
    prefix_len: Mapped[int] = mapped_column(Integer, nullable=False)
    server_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("servers.id"), index=True)
    customer_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("customers.id"), index=True)
    #: True for the address the OS installer configures as primary.
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    rdns: Mapped[str | None] = mapped_column(String(255))
    assigned_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    block: Mapped[IPBlock] = relationship(back_populates="assignments")
    server: Mapped[Server | None] = relationship(back_populates="ip_assignments")

    __table_args__ = (
        Index(
            "uq_active_ip_assignment",
            "address",
            unique=True,
            postgresql_where=(released_at.is_(None)),
        ),
    )


# ---------------------------------------------------------------------------
# Telemetry, audit, abuse
# ---------------------------------------------------------------------------


class BandwidthSample(Base):
    """Raw switch counters. Deltas are computed at query time.

    Counters are cumulative and reset when a switch reboots, so the reader has
    to discard negative deltas rather than trusting monotonicity.
    """

    __tablename__ = "bandwidth_samples"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    server_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("servers.id", ondelete="CASCADE"), nullable=False
    )
    sampled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    rx_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    tx_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    rx_errors: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    tx_errors: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)

    __table_args__ = (Index("ix_bw_server_time", "server_id", "sampled_at"),)


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    actor_type: Mapped[ActorType] = mapped_column(String(16), nullable=False)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
    actor_label: Mapped[str | None] = mapped_column(String(320))
    action: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    target_type: Mapped[str | None] = mapped_column(String(64))
    target_id: Mapped[str | None] = mapped_column(String(64), index=True)
    source_ip: Mapped[str | None] = mapped_column(INET)
    user_agent: Mapped[str | None] = mapped_column(String(512))
    detail: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)


class AbuseReport(Base, TimestampMixin):
    __tablename__ = "abuse_reports"

    id: Mapped[uuid.UUID] = _uuid_pk()
    server_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("servers.id"), index=True)
    customer_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("customers.id"), index=True)
    reported_ip: Mapped[str | None] = mapped_column(INET)
    source: Mapped[str] = mapped_column(String(255), nullable=False)  # reporter / upstream
    category: Mapped[str | None] = mapped_column(String(64))  # spam, ddos, phishing, copyright
    #: open -> acknowledged -> customer_notified -> suspended -> resolved
    status: Mapped[str] = mapped_column(String(32), default="open", nullable=False, index=True)
    #: Clock the transit provider is actually watching.
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    body: Mapped[str | None] = mapped_column(Text)
    resolution: Mapped[str | None] = mapped_column(Text)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
