"""Vendor-neutral BMC driver interface.

Everything above this layer speaks in these verbs. Nothing above this layer
knows what a CIMC is. Swapping the M4s for M5s or Dell R640s should mean
adding a driver, not editing the provisioning code.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.enums import PowerAction


class LogSink(Protocol):
    """Where a driver writes its raw exchange.

    Job workers pass a sink that appends to `job_log_entries`; the bench script
    passes one that prints. Drivers never touch the database directly.
    """

    def __call__(
        self,
        message: str,
        *,
        level: str = "info",
        request: dict | None = None,
        response: dict | None = None,
    ) -> None: ...


def null_sink(
    message: str,
    *,
    level: str = "info",
    request: dict | None = None,
    response: dict | None = None,
) -> None:
    return None


@dataclass
class PowerStatus:
    state: str  # "on" | "off" | "unknown"
    raw: dict = field(default_factory=dict)


@dataclass
class HealthStatus:
    status: str  # "ok" | "warning" | "critical" | "unknown"
    #: Subsystem -> {status, detail}. Populated as far as the BMC supports it.
    subsystems: dict[str, Any] = field(default_factory=dict)
    raw: dict = field(default_factory=dict)


@dataclass
class HardwareInventory:
    manufacturer: str | None = None
    model: str | None = None
    serial: str | None = None
    bios_version: str | None = None
    bmc_firmware: str | None = None
    cpu_model: str | None = None
    cpu_count: int | None = None
    cpu_cores_total: int | None = None
    ram_gb: int | None = None
    #: [{"mac": ..., "name": ..., "speed_mbps": ...}]
    nics: list[dict] = field(default_factory=list)
    #: [{"name": ..., "capacity_gb": ..., "media": ..., "serial": ...}]
    drives: list[dict] = field(default_factory=list)
    raw: dict = field(default_factory=dict)


class BMCError(RuntimeError):
    """Any BMC-side failure. Carries the raw exchange for the job log."""

    def __init__(
        self,
        message: str,
        *,
        request: dict | None = None,
        response: dict | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.request = request
        self.response = response
        self.retryable = retryable


class BMCDriver(ABC):
    """The whole surface the platform needs from a baseboard controller."""

    @abstractmethod
    def power_status(self) -> PowerStatus: ...

    @abstractmethod
    def power(self, action: PowerAction) -> None: ...

    def wait_for_power_state(self, want: str, timeout: int = 180, interval: int = 5) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.power_status().state == want:
                return True
            time.sleep(interval)
        return False

    def power_cycle(self, *, settle_seconds: int = 5) -> None:
        """Off, confirm, on.

        A ForceRestart on a powered-off machine is a no-op on most BMCs, and
        provisioning depends on the box actually coming up, so the transition
        is driven explicitly instead.
        """
        if self.power_status().state == "on":
            self.power(PowerAction.FORCE_OFF)
            if not self.wait_for_power_state("off", timeout=120):
                self.log("server did not report power off within 120s", level="warning")
        # The power supply needs a moment before it will accept an On.
        time.sleep(settle_seconds)
        self.power(PowerAction.ON)

    def log(
        self,
        message: str,
        *,
        level: str = "info",
        request: dict | None = None,
        response: dict | None = None,
    ) -> None:
        """Implementations override to route to their sink."""
        return None

    @abstractmethod
    def set_boot_once(self, target: str) -> None:
        """Set a one-time boot override. `target` is "pxe", "hdd" or "cd"."""

    @abstractmethod
    def clear_boot_override(self) -> None: ...

    @abstractmethod
    def inventory(self) -> HardwareInventory: ...

    @abstractmethod
    def health(self) -> HealthStatus: ...

    @abstractmethod
    def insert_virtual_media(self, image_url: str) -> None: ...

    @abstractmethod
    def eject_virtual_media(self) -> None: ...

    def virtual_media(self) -> list[dict]:
        """Virtual media slots and what is in them. Redfish only.

        Each item: {id, name, media_types, inserted, image, image_name}.
        """
        raise BMCError("virtual media status is not available over this protocol")

    def close(self) -> None:
        return None

    def __enter__(self) -> BMCDriver:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
