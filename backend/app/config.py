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

    # --- provisioning ------------------------------------------------------
    # How long a provisioning job waits for the installer to phone home.
    install_timeout_seconds: int = 60 * 45
    # How long an installer callback token stays valid.
    callback_token_ttl_seconds: int = 60 * 60 * 4
    # Wipe passes before a server may return to in_stock.
    require_wipe_before_stock: bool = True

    # --- console -----------------------------------------------------------
    ipmitool_path: str = "ipmitool"
    sol_idle_timeout_seconds: int = 900

    # --- misc --------------------------------------------------------------
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
