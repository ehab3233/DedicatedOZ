"""BMC drivers.

`get_driver()` is the only thing the rest of the platform imports.
"""

from __future__ import annotations

from app.drivers.base import (
    BMCDriver,
    BMCError,
    HardwareInventory,
    HealthStatus,
    LogSink,
    PowerStatus,
    null_sink,
)
from app.drivers.factory import get_driver, get_ipmi_driver

__all__ = [
    "BMCDriver",
    "BMCError",
    "HardwareInventory",
    "HealthStatus",
    "LogSink",
    "PowerStatus",
    "get_driver",
    "get_ipmi_driver",
    "null_sink",
]
