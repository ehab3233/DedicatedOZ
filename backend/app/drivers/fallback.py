"""Two protocols, one driver.

Power, boot device and power state can be driven over either IPMI or Redfish.
This driver tries one, and on any failure repeats the operation over the
other, logging both. Inventory and virtual media only exist in Redfish and go
straight there.

The order is IPMI first. Measured against a real IPMI stack (OpenIPMI's
ipmi_sim) and ipmitool 1.8.19: a status read over IPMI takes ~60 ms, while
Redfish on a CIMC is a login, one or two GETs and a logout, each of them slow.
IPMI also sidesteps the CIMC's small Redfish session limit. Redfish remains
the fallback for when IPMI over LAN has been switched off.

Once a fallback has happened, the rest of the operation stays on the protocol
that worked. A power cycle polls power state every few seconds; paying for a
failed attempt on every poll would turn a thirty-second job into five minutes.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

from app.drivers.base import (
    BMCDriver,
    BMCError,
    HardwareInventory,
    HealthStatus,
    LogSink,
    PowerStatus,
    null_sink,
)
from app.enums import PowerAction

T = TypeVar("T")


class FallbackDriver(BMCDriver):
    def __init__(
        self,
        first: BMCDriver,
        second: BMCDriver,
        *,
        rich: BMCDriver,
        log: LogSink = null_sink,
    ) -> None:
        self.first = first
        self.second = second
        #: The one that can do inventory, health detail and virtual media.
        self.rich = rich
        self._log = log
        self._stick_to_second = False
        #: Which protocol served the last call ("ipmi" / "redfish").
        self.last_used: str | None = None

    def log(self, message: str, *, level: str = "info", request=None, response=None) -> None:  # noqa: ANN001
        self._log(message, level=level, request=request, response=response)

    def _call(self, name: str, fn: Callable[[BMCDriver], T]) -> T:
        if not self._stick_to_second:
            try:
                result = fn(self.first)
                self.last_used = protocol_label(self.first)
                return result
            except BMCError as first_error:
                self.log(
                    f"{name} over {protocol_label(self.first)} failed ({first_error}); "
                    f"retrying over {protocol_label(self.second)}",
                    level="warning",
                )
                try:
                    result = fn(self.second)
                except BMCError as second_error:
                    raise BMCError(
                        f"{name} failed over {protocol_label(self.first)} ({first_error}) "
                        f"and over {protocol_label(self.second)} ({second_error})",
                        request=second_error.request,
                        response=second_error.response,
                    ) from second_error
                self._stick_to_second = True
                self.last_used = protocol_label(self.second)
                return result
        result = fn(self.second)
        self.last_used = protocol_label(self.second)
        return result

    # -- either protocol ---------------------------------------------------

    def power_status(self) -> PowerStatus:
        return self._call("power status", lambda d: d.power_status())

    def power(self, action: PowerAction) -> None:
        self._call(f"power {action.value}", lambda d: d.power(action))

    def set_boot_once(self, target: str) -> None:
        self._call(f"boot once {target}", lambda d: d.set_boot_once(target))

    def clear_boot_override(self) -> None:
        self._call("clear boot override", lambda d: d.clear_boot_override())

    def health(self) -> HealthStatus:
        # Redfish knows PSUs, fans and DIMMs by name; the IPMI SDR is the
        # fallback when Redfish is down.
        other = self.first if self.rich is self.second else self.second
        try:
            return self.rich.health()
        except BMCError as exc:
            self.log(f"health over redfish failed ({exc}); reading the IPMI SDR", level="warning")
            return other.health()

    # -- Redfish only ------------------------------------------------------

    def inventory(self) -> HardwareInventory:
        return self.rich.inventory()

    def insert_virtual_media(self, image_url: str) -> None:
        self.rich.insert_virtual_media(image_url)

    def eject_virtual_media(self) -> None:
        self.rich.eject_virtual_media()

    def virtual_media(self) -> list[dict]:
        return self.rich.virtual_media()

    def close(self) -> None:
        for driver in (self.first, self.second):
            try:
                driver.close()
            except Exception:  # noqa: BLE001 - closing must not mask the real error
                pass


def protocol_label(driver: BMCDriver) -> str:
    """"ipmi" / "redfish" for a concrete driver, or what a fallback last used."""
    if isinstance(driver, FallbackDriver):
        return driver.last_used or "auto"
    name = type(driver).__name__.lower()
    return "redfish" if "redfish" in name else "ipmi" if "ipmi" in name else name
