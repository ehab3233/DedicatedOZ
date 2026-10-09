"""Runtime configuration.

Everything is environment-driven so the same image runs as the API, as a Celery
worker on the OOB plane, or on a laptop against docker-compose.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="DOZ_", extra="ignore")

    # --- core services -----------------------------------------------------
    database_url: str = "postgresql+psycopg://doz:doz@localhost:5432/doz"
    redis_url: str = "redis://localhost:6379/0"

    # --- auth --------------------------------------------------------------
    jwt_secret: str = "dev-only-change-me"
    jwt_algorithm: str = "HS256"
    access_token_ttl_seconds: int = 60 * 60 * 8

    # --- control plane addressing -----------------------------------------
    # The address the installer ramdisk and iPXE use to reach us. Must be
    # reachable from the provisioning VLAN, not from the OOB plane.
    control_plane_url: str = "http://10.10.0.5:8000"

    # Where boot artifacts (kernel, initrd, ISOs) are served from.
    boot_asset_base_url: str = "http://10.10.0.5:8080"

    # --- secrets -----------------------------------------------------------
    # "env" reads CIMC credentials from environment variables (dev/bench),
    # "file" from a directory of JSON files, "vault" from HashiCorp Vault.
    secrets_backend: str = "env"
    secrets_file_dir: str = "/run/secrets/cimc"
    vault_addr: str = ""
    vault_token: str = ""
    vault_mount: str = "secret"

    # --- BMC transport -----------------------------------------------------
    # M4 CIMC ships a self-signed cert and will never get a new one. The OOB
    # plane is isolated and unroutable, which is what actually provides the
    # security boundary here. Set to true if you pin a CA per server.
    redfish_verify_tls: bool = False
    redfish_timeout_seconds: int = 30
    redfish_max_retries: int = 3

    # Which protocol drives power and boot. "auto" uses IPMI first (tens of
    # milliseconds per call) and falls back to Redfish when IPMI fails, then
    # stays on Redfish for the rest of that job. "ipmi" and "redfish" force
    # one. Can be overridden per server. Inventory and virtual media always
    # use Redfish; the serial console is always IPMI.
    bmc_protocol: str = "auto"

    # IPMI over LAN (ipmitool lanplus). Cipher suite 3 is pinned by default:
    # ipmitool 1.8.19 otherwise probes for the best suite, and a BMC that does
    # not answer that probe costs ten seconds on every single call -- long
    # enough to blow the IPMI 60-second boot-flag window during a reinstall.
    # Empty string lets ipmitool choose.
    ipmi_cipher_suite: str = "3"
    ipmi_port: int = 623
    ipmi_timeout_seconds: int = 20
    # Add options=efiboot to IPMI boot device overrides. Only for servers
    # booting in UEFI mode; the guide sets the M4s to legacy.
    ipmi_boot_efi: bool = False

    # Synchronous power-state reads for the panel. Short, because a person is
    # waiting on them; cached briefly so a busy page cannot hammer the BMC.
    bmc_status_timeout_seconds: int = 10
    bmc_status_cache_seconds: int = 10

    # vKVM launch URL. Empty means probe the CIMC for the HTML5 viewer and
    # fall back to the Java launcher. Placeholders: {host} {tkn1} {tkn2}.
    kvm_url_template: str = ""

    # What Prepare BMC sets as the persistent legacy boot order: "disk,pxe"
    # (boot the installed OS; installs use a one-time PXE override),
    # "pxe,disk" (every boot asks the management server first), or empty to
    # leave the boot order alone.
    bmc_prepare_boot_order: str = "disk,pxe"
    # NTP servers Prepare BMC points each CIMC at (comma-separated), so the
    # event log carries real timestamps. Empty leaves the CIMC's clock alone.
    ntp_servers: str = ""

    # Live sensor, event-log and BMC-info reads for the panel are cached this
    # long, so several open tabs share one BMC round trip instead of each
    # starting their own IPMI session.
    bmc_live_cache_seconds: int = 3

    # --- images ------------------------------------------------------------
    # The directory nginx serves at boot_asset_base_url: the installer
    # ramdisk, the OS templates' kernels and initrds, the ISO store. Empty
    # means <repo>/installer/assets.
    boot_asset_dir: str = ""
    # Where uploaded ISO images live. Empty means <boot_asset_dir>/iso, which
    # nginx serves at {boot_asset_base_url}/iso/ -- the URL the BMC fetches
    # virtual media from.
    image_dir: str = ""

    # --- provisioning ------------------------------------------------------
    # How long a graceful shutdown may take before the job reports that the OS
    # ignored it. It is never escalated to a forced power-off automatically:
    # that is the operator's call, and it is one click away.
    graceful_shutdown_timeout_seconds: int = 300

    # How long a provisioning job waits for the installer to phone home.
    install_timeout_seconds: int = 60 * 45
    # How long an installer callback token stays valid.
    callback_token_ttl_seconds: int = 60 * 60 * 4
    # Wipe passes before a server may return to in_stock.
    require_wipe_before_stock: bool = True
    # Bind a job's boot material to the first client address that fetches it.
    # Requires a DHCP server that hands the same address to a MAC regardless of
    # client-id (dnsmasq: `dhcp-ignore-clid`). Turn off when using someone
    # else's DHCP in proxy mode, or the OS installer may be refused mid-install.
    boot_pin_client_ip: bool = True

    # --- console -----------------------------------------------------------
    ipmitool_path: str = "ipmitool"
    # A console with no traffic either way for this long is closed, which
    # frees the BMC's single SOL slot for the next person.
    sol_idle_timeout_seconds: int = 1800
    # Hard cap regardless of activity.
    sol_max_session_seconds: int = 8 * 3600
    # Console websocket tickets: single use, this short.
    console_ticket_ttl_seconds: int = 60

    # --- misc --------------------------------------------------------------
    # Only honour X-Forwarded-For when a reverse proxy we control sets it. On a
    # flat network with the API exposed directly, trusting it would let any
    # host forge its address and defeat boot-script client pinning.
    trust_proxy_headers: bool = False
    cors_origins: str = "http://localhost:5173"
    log_level: str = "INFO"
    environment: str = "development"

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
