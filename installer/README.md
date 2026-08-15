# Installer rail

Everything a machine touches between "power on" and "the OS is installed".

## How a reinstall actually runs

```
worker                       machine                    control plane
  │                             │                             │
  ├─ Redfish: boot once = PXE   │                             │
  ├─ Redfish: power cycle ─────▶│                             │
  │                             ├─ DHCP, iPXE ───────────────▶│  GET /boot/ipxe?mac=…
  │                             │◀───────────────────────────┤  per-MAC script
  │                             ├─ loads doz-installer ramdisk│
  │                             ├─ GET /boot/provision/{mac} ▶│
  │                             │◀───────────────────────────┤  provision.sh
  │                             ├─ wipe → StorCLI RAID        │
  │                             ├─ POST progress ────────────▶│  (job stage/percent)
  │                             ├─ kexec into distro installer│
  │                             ├─ GET /boot/answer/{mac} ───▶│  kickstart/autoinstall
  │                             ├─ installs                   │
  │                             ├─ POST complete ────────────▶│
  │◀────────────────────────────┴─────────────────────────────┤  worker sees result
  ├─ Redfish: clear boot override
  └─ server → active
```

The ramdisk builds the array before the distribution installer ever runs. That
split is deliberate: the OS installer is much better than we would be at
bootloaders and driver selection, and it is much worse than StorCLI at talking
to a Cisco 12G SAS controller. Each does the part it is good at.

Rescue and wipe are the same rail with `doz_mode` set differently. Rescue stops
after bringing up SSH; wipe erases every drive and reports which ones.

## Layout

| Path | What it is |
|---|---|
| `templates/ipxe/*.j2` | Per-MAC boot scripts, one per rail |
| `templates/ramdisk/provision.sh.j2` | The script the ramdisk runs — wipe, RAID, install |
| `templates/answers/*.j2` | Kickstart / autoinstall / preseed, rendered per job |
| `build-ramdisk.sh` | Builds the Alpine-based installer image |
| `assets/` | Kernels, initrds and ISOs, served over plain HTTP (not in git) |

## Building the ramdisk

```sh
sudo ./build-ramdisk.sh --out ./assets/doz-installer --storcli /path/to/storcli.deb
```

StorCLI has to be supplied separately — Broadcom does not permit
redistribution. Without it the ramdisk still installs, but onto bare disks with
no array.

## Boot assets

`assets/` is served by the `boot-assets` container on port 8080 and needs:

```
assets/
  doz-installer/vmlinuz            # built above
  doz-installer/initrd.img
  os/ubuntu-22.04/casper/{vmlinuz,initrd}
  os/debian-12/{linux,initrd.gz}
  os/rocky-9/images/pxeboot/{vmlinuz,initrd.img}
  iso/…                            # for the vMedia fallback path
```

Paths are whatever `os_templates.kernel_path` / `initrd_path` say; the ones
above match `scripts/init_db.py`.

Plain HTTP is not an oversight. CIMC virtual media will not follow redirects
and cannot validate a certificate from a CA it has never heard of, so the asset
server has to be trivially simple and reachable only from the provisioning
VLAN.

## DHCP

One option serves the whole fleet — iPXE substitutes the MAC itself:

```
# dnsmasq
dhcp-match=set:ipxe,175
dhcp-boot=tag:!ipxe,undionly.kpxe
dhcp-boot=tag:ipxe,http://10.10.0.5:8000/boot/ipxe?mac=${net0/mac}
```

A machine with no active job gets a script that boots from local disk, so
leaving this in place permanently is fine and is what you want: every reboot
re-confirms the machine can netboot, which is how you find a dead PXE ROM
before a customer does.

## Editing the templates

They are Jinja2, rendered by `app/services/boot.py`, and covered by
`tests/test_boot_rail.py`. Two things to keep in mind:

- `provision.sh.j2` is POSIX `sh`, not bash — the ramdisk is BusyBox.
- Jinja treats `{#` as a comment opener, so avoid shell constructs like
  `${#array[@]}`.

A change here that renders wrong does not raise an error. It produces a boot
script that is quietly incorrect, and the failure surfaces twenty minutes later
as a machine that never came back. Run the tests.
