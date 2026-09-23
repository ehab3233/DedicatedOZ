"""Lifecycle and job enumerations, plus the transition tables that govern them.

The state machines live here rather than being implied by scattered `if`
statements, so that an illegal transition is a loud error instead of a server
silently ending up in a state nothing knows how to recover from.
"""

from __future__ import annotations

import enum


class ServerState(enum.StrEnum):
    IN_STOCK = "in_stock"
    PROVISIONING = "provisioning"
    ACTIVE = "active"
    SUSPENDED = "suspended"
    WIPING = "wiping"
    RESCUE = "rescue"
    RMA = "rma"
    RETIRED = "retired"


#: Allowed server lifecycle transitions. Anything not listed is rejected.
SERVER_TRANSITIONS: dict[ServerState, set[ServerState]] = {
    ServerState.IN_STOCK: {ServerState.PROVISIONING, ServerState.RMA, ServerState.RETIRED},
    ServerState.PROVISIONING: {
        ServerState.ACTIVE,
        ServerState.IN_STOCK,  # install failed, back to the pool
        ServerState.RMA,
    },
    ServerState.ACTIVE: {
        ServerState.SUSPENDED,
        ServerState.PROVISIONING,  # reinstall
        ServerState.RESCUE,
        ServerState.WIPING,  # deprovision
        ServerState.RMA,
    },
    ServerState.SUSPENDED: {ServerState.ACTIVE, ServerState.WIPING, ServerState.RMA},
    ServerState.RESCUE: {ServerState.ACTIVE, ServerState.PROVISIONING, ServerState.WIPING},
    ServerState.WIPING: {ServerState.IN_STOCK, ServerState.RMA},
    ServerState.RMA: {ServerState.IN_STOCK, ServerState.RETIRED},
    ServerState.RETIRED: set(),
}


class JobState(enum.StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


JOB_TRANSITIONS: dict[JobState, set[JobState]] = {
    JobState.QUEUED: {JobState.RUNNING, JobState.CANCELLED},
    JobState.RUNNING: {JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED},
    JobState.SUCCEEDED: set(),
    JobState.FAILED: set(),
    JobState.CANCELLED: set(),
}

TERMINAL_JOB_STATES = {JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED}


class JobType(enum.StrEnum):
    POWER_ON = "power_on"
    POWER_OFF = "power_off"  # graceful: ACPI, the OS decides
    POWER_FORCE_OFF = "power_force_off"
    POWER_CYCLE = "power_cycle"
    POWER_RESET = "power_reset"
    BMC_SETUP = "bmc_setup"
    #: One-time boot device (PXE, disk, CD, BIOS setup), optionally followed
    #: by a reset or power cycle so it takes effect now.
    BOOT_OVERRIDE = "boot_override"
    #: Mount an ISO from the image store as virtual media and boot from it.
    VMEDIA_BOOT = "vmedia_boot"
    VMEDIA_EJECT = "vmedia_eject"
    #: Download an ISO into the image store on the management server.
    IMAGE_FETCH = "image_fetch"
    INSTALL = "install"
    RESCUE = "rescue"
    WIPE = "wipe"
    INVENTORY_SYNC = "inventory_sync"
    HEALTH_POLL = "health_poll"
    BANDWIDTH_POLL = "bandwidth_poll"


class PowerAction(enum.StrEnum):
    """Generic power verbs, mapped to Redfish ResetType by the driver."""

    ON = "on"
    OFF = "off"  # graceful
    FORCE_OFF = "force_off"
    RESTART = "restart"  # graceful
    FORCE_RESTART = "force_restart"


class InstallMethod(enum.StrEnum):
    KICKSTART = "kickstart"  # RHEL / Rocky / Alma
    AUTOINSTALL = "autoinstall"  # Ubuntu 20.04+
    PRESEED = "preseed"  # Debian
    UNATTEND = "unattend"  # Windows
    IMAGE = "image"  # dd a raw image


class RaidLevel(enum.StrEnum):
    NONE = "none"  # passthrough / JBOD
    RAID0 = "raid0"
    RAID1 = "raid1"
    RAID5 = "raid5"
    RAID6 = "raid6"
    RAID10 = "raid10"


class ActorType(enum.StrEnum):
    CUSTOMER = "customer"
    ADMIN = "admin"
    SYSTEM = "system"
    INSTALLER = "installer"
