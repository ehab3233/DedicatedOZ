# Getting servers into DedicatedOZ — flat network edition

This walks from "a rack of C220 M4s and an Ubuntu VM" to "a customer reinstalls
their server from the portal". It assumes one flat network to start with: the
management VM, every CIMC, and every server's data NIC are all on the same
subnet. That is fine for getting going and for a small beta. The last section
covers what to change when you outgrow it, and why.

---

## 0. What you need on the bench

- The management VM: Ubuntu 22.04 or 24.04, 2 vCPU, 4 GB RAM, 40 GB disk plus
  room for OS images (another ~5 GB). One NIC on the flat network.
- A switch everything plugs into. Unmanaged is fine for now.
- For each C220 M4: power, the CIMC/management port cabled, and at least one
  of the onboard i350 1G ports cabled (that is what PXE boots).
- A monitor and USB keyboard for first-time CIMC setup. You need them once
  per server.
- Somewhere to write down serials, CIMC IPs and MACs. The panel records them,
  but you will want them on paper while cabling.

## 1. Plan the addresses

Pick a private subnet. This guide uses `10.0.0.0/24`. Decide three things:

| What | Example | Notes |
|---|---|---|
| Management VM | `10.0.0.5` | Static. Everything else points at it. |
| CIMC addresses | `10.0.0.101` – `10.0.0.199` | Static, one per server. Last octet = rack unit is a habit worth forming. |
| DHCP range for PXE | `10.0.0.200` – `10.0.0.249` | Only used while a server is being installed. |
| Server addresses | `10.0.0.20` – `10.0.0.99` | What the installed OS ends up on. Later, this becomes your public block. |
| Gateway | `10.0.0.1` | Your router. |

Write these in the panel later; for now they only need to be decided.

**Do you already have a DHCP server on this network?** (A home/office router
almost certainly is one.) You then have two options:

- **Turn it off for this subnet** and let the management VM be the DHCP server
  (`--dhcp-range`). Cleanest. Recommended if this is a dedicated lab network.
- **Leave it running** and have the management VM only add PXE information to
  its replies (`--proxy-dhcp`). Works, but the installed OS may get a different
  address from the one iPXE got, so client pinning is turned off in this mode.
- **Your DHCP server is on the servers' VLAN and this VM is not** (a routed
  network). Keep it, and point its PXE options at this VM (`--external-dhcp`):
  the VM then only serves TFTP and the boot scripts. Section 2 has the exact
  options to set.

You cannot run two authoritative DHCP servers on one network. Pick one.

## 2. Install the management server

On a fresh Ubuntu VM (22.04, 24.04 or 26.04) with a static IP already
configured, one command does everything:

```sh
curl -fsSL https://raw.githubusercontent.com/ehab3233/DedicatedOZ/HEAD/install.sh | sudo bash
```

It fetches the code, installs the whole management server as the `doz`
service, starts a simulated BMC (server SIM-0001) so there is something to
try, and ends with the panel address and a one-time admin password. About
ten minutes. Run the same command again later to update.

By default it sets up **no DHCP or PXE**. That is the right choice when the
servers' CIMCs sit on a different VLAN with routing in between: power,
consoles, sensors, the event log and ISO installs over virtual media all
work across routing, and nothing on the network is touched. If this VM is on
the same flat network as the servers and should netboot them, choose one of
these instead:

```sh
# this VM becomes the DHCP server for the flat network:
DOZ_PXE=range DOZ_DHCP_RANGE=10.0.0.200,10.0.0.249 \
  curl -fsSL https://raw.githubusercontent.com/ehab3233/DedicatedOZ/HEAD/install.sh | sudo bash
# your router keeps doing DHCP; this only adds the PXE options:
DOZ_PXE=proxy curl -fsSL https://raw.githubusercontent.com/ehab3233/DedicatedOZ/HEAD/install.sh | sudo bash
```

**Servers on another VLAN, with their own DHCP server** (a MikroTik, a
firewall, Windows DHCP): keep that DHCP server and tell it where to send PXE
clients. Install with `DOZ_PXE=external` (or, on an existing install,
`sudo /opt/doz-src/doz.sh install --external-dhcp`); the VM then serves TFTP
and the boot scripts and does no DHCP of its own. On the DHCP server, for
the servers' subnet, set two things:

| Option | Value |
|---|---|
| next-server (66) | the VM's address |
| filename (67) | `undionly.kpxe` (`ipxe.efi` for UEFI servers) |

On a MikroTik (RouterOS 7):

```
/ip dhcp-server network set [find] next-server=172.16.100.193 boot-file-name=undionly.kpxe
```

That is enough because `sudo /opt/doz/doz.sh ramdisk` also builds the iPXE
loaders the VM serves (`doz.sh ipxe` builds only those), with the VM's
address compiled in. Stock iPXE, once loaded, asks DHCP again and boots
whatever filename it gets, which on most DHCP servers is `undionly.kpxe`
again, forever. A user-class rule is the usual workaround and does not work
on a MikroTik: its `boot-file-name` fills the BOOTP file field, which iPXE
reads before option 67. These loaders ignore the second filename and go
straight to `http://<VM>/boot/ipxe`. The **Images** page shows whether the
VM is serving them.

The servers' VLAN must reach the VM on UDP 69 and TCP 80 and 8080, and the
VM must reach the CIMCs. `PXE-E53: No boot filename received` on the
server's console means the two options are missing; the console stuck at
`TFTP...` means nothing on the VM answered (PXE is off, or UDP 69 is
blocked); iPXE fetching `undionly.kpxe` over and over means the VM is still
serving stock loaders, so run `sudo /opt/doz/doz.sh ipxe`. A NAT between
the VLANs is fine: the boot rail does not pin clients to an address in this
mode.

The long form, from a checkout, is the same installer with flags:

```sh
sudo ./doz.sh install --ip 10.0.0.5 --no-pxe        # or --dhcp-range A,B, or --proxy-dhcp
```

Log in at `http://<the VM's address>` straight away and confirm you land on
the dashboard.

Everything runs as one Ubuntu service, `doz`, enabled at boot:

```sh
sudo systemctl status doz        # the umbrella service
sudo systemctl restart doz       # restarts every part
/opt/doz/doz.sh status           # each part, and whether every job queue has a worker
/opt/doz/doz.sh logs             # follow all the logs (journalctl -u 'doz*')
```

The parts are the API, three workers (power, provisioning, polling -- separate
so a pile of reinstalls can never sit in front of a power button), the
scheduler, and PXE. **Manage → Fleet** shows the same thing in its System
card; if a queue has no worker, it says so in red.

To update later, run the one-liner again (or `git pull && sudo ./doz.sh
update` from a checkout). The settings from the first install are remembered
in `/etc/doz/install.conf`; secrets in `/etc/doz/doz.env` are never
overwritten.

### No hardware on the bench yet?

Try power control and the serial console against a simulated BMC first:

```sh
sudo apt install --no-install-recommends openipmi
sudo /opt/doz/doz.sh sim start
```

That starts OpenIPMI's `ipmi_sim` -- a real IPMI-over-LAN implementation --
with a pretend server behind it, and registers it as **SIM-0001**. (The
one-line installer already did this.) Open it in the panel: every power
button works, the **Console** tab shows a POST screen and a login prompt
when you reset it, **Sensors** shows twelve readings, and the **BMC** tab's
tools all answer. `sudo /opt/doz/doz.sh sim events` puts a few entries in
its event log. `sudo /opt/doz/deploy/smoke-test.sh` runs the whole check
automatically. Remove it with `sudo /opt/doz/doz.sh sim remove`.

The simulator's readings come from files under `/var/lib/doz/sim/sensors/`,
one per sensor, in the sensor's own unit. Write a number into one to watch
the panel react:

```sh
echo 96 | sudo tee /var/lib/doz/sim/sensors/30    # CPU1 Temp to 96 C: critical within a second
echo 0  | sudo tee /var/lib/doz/sim/sensors/40    # FAN1 stops
echo 47 | sudo tee /var/lib/doz/sim/sensors/30    # back to normal
```

Then fetch the OS images and build the installer ramdisk. These take a while
and a couple of gigabytes, which is why they are separate:

```sh
cd /opt/doz
sudo -u doz ./deploy/fetch-os-images.sh          # Ubuntu 22.04, Debian 12, Rocky 9
sudo ./doz.sh ramdisk                            # the installer ramdisk (installs docker)
```

Neither shows up as an "image" you click on: they are the files a PXE
reinstall boots. The **Images** page lists them under **Netboot images**,
per OS template, with anything missing marked, and a reinstall refuses to
start while a file it needs is missing. The Ubuntu ISO the first script
downloads is also linked into the ISO store; **Scan directory** on the same
page catalogues it for virtual-media installs.

StorCLI is not included in the ramdisk unless you supply it — Broadcom does not
allow redistribution. Download `storcli` for Linux from Broadcom's site and
pass it: `sudo ./installer/build-ramdisk.sh --out installer/assets/doz-installer --storcli /path/to/storcli_*.deb`.
Without it, installs land on bare disks with no RAID.

Check the assets are being served: `http://10.0.0.5:8080/` should list
`doz-installer/` and `os/`.

## 3. Set up one CIMC by hand

Do this for one server first, run the bench test, and only then do the rest.

Plug in the monitor and keyboard, power on, and press **F8** during POST for
CIMC configuration:

- **NIC mode:** Dedicated (the separate management port), NIC redundancy None.
- **IPv4:** DHCP disabled, IP `10.0.0.101`, mask `255.255.255.0`, gateway `10.0.0.1`.
- **Default user:** set the `admin` password now. Write it down.
- Save (F10) and let it reset.

Then press **F2** for BIOS setup:

- **Boot options → Boot mode:** Legacy. UEFI works too; Legacy is the path
  that has been tested most on the M4.
- **LOM ports → PXE / Option ROM:** enabled on port 1 at least.
- **Server management → Console redirection:** enabled, COM0, 115200 8N1,
  terminal type VT100+. Without this the serial console shows nothing.
- Save and exit.

From the management VM, confirm the CIMC answers:

```sh
curl -sk https://10.0.0.101/redfish/v1/ | head -c 300
IPMI_PASSWORD='the-password' ipmitool -I lanplus -H 10.0.0.101 -U admin -E -C 3 chassis status
```

The Redfish call should return JSON. The IPMI one may fail at this point:
IPMI over LAN and Serial-over-LAN are **off by default** on many CIMC builds.
You do not need to fix that by hand -- **Prepare BMC** runs when you add the
server in step 5 and switches both on, with the rest of what the platform
needs. (If you would rather: CIMC web UI → Admin → Communication Services →
IPMI over LAN, and Server → Remote Presence → Serial over LAN, 115200.)

`-C 3` pins the IPMI cipher suite. Without it, ipmitool 1.8.19 probes for the
best one on every call, and a BMC that does not answer the probe costs ten
seconds each time. The platform pins it for the same reason.

**Firmware.** Note the CIMC version on the web UI's summary page. The fleet
target is **4.1(2f)**, the last M4 release. If this server is lower, upgrade
it now with the Host Upgrade Utility ISO from Cisco (mount it as virtual
media, boot it, tick everything, go for coffee). Do every server before it is
racked. A mixed fleet turns every later failure into a guessing game.

## 4. Run the bench test

This is section 2 of the spec and it is not optional. From the VM:

```sh
cd /opt/doz
sudo -u doz ./doz.sh bench 10.0.0.101 --user admin --power-cycle \
    --iso-url http://10.0.0.5:8080/os/ubuntu-22.04/ubuntu-22.04-live-server-amd64.iso
```

It walks through all five checks and reboots the machine twice. Watch the
serial console in another terminal while it runs:

```sh
ipmitool -I lanplus -H 10.0.0.101 -U admin -E sol activate    # IPMI_PASSWORD=… in the env
```

You are looking for: the PXE ROM firing, DHCP, `iPXE` chainloading, and the
banner `=== DedicatedOZ provisioning ===` — or, for the vMedia test, the
Ubuntu ISO's boot menu.

Step 5 (StorCLI) is done from a live Linux image on the server itself and the
test tells you what to run. It is the awkward one. Do not skip it: it decides
whether reinstall builds a RAID or not.

All five green → the design holds and you can register the fleet. Anything red
→ fix it now, because every server will have the same problem.

## 5. Register the server in the panel

Portal → **Manage** → **Add server**.

- Serial: from the pull-out tab on the front of the chassis.
- CIMC IP: `10.0.0.101`.
- Credential ref: leave the default `cimc/<serial>`; enter the CIMC username
  and password below it. They are written to `/etc/doz/cimc/`, never the
  database.
- Rack / unit / switch port: fill in what you know. It is what the abuse
  queue and remote hands will need at 3am.
- Leave the PXE MAC blank.

On save, a **Prepare BMC** job runs (a tickbox on the form; leave it on).
Over the CIMC's XML API it switches on IPMI over LAN, Serial-over-LAN at
115200 on COM0, BIOS console redirection to the same port, virtual media,
KVM and Redfish, enables the PXE option ROMs on the LAN ports, sets the boot
order (disk first, PXE available, legacy mode) and, if `DOZ_NTP_SERVERS` is
set, NTP. BIOS changes take effect at the next boot. It then proves IPMI
works by reading the power state, reads back the SOL settings, and queues
the **inventory sync**. The job log shows each step and the CIMC's reply;
anything this firmware rejects is listed at the end, and those few you set
in the CIMC web UI by hand. If the job fails, the message says which of the
usual three it is: wrong password, IPMI still off, or UDP 623 blocked
between the VM and the CIMC.

This is a one-time job: the settings live in the CIMC. The BMC tab shows when
it last ran, and a re-run reads each setting first and only writes the ones
that have drifted, so on a prepared server it changes nothing. Expect the
panel to lose the server for a few seconds the first time: switching IPMI
over LAN on restarts the CIMC's IPMI service.

Then open the server (click its serial) and watch the sync: within a minute
the CPU, RAM, firmware, drives and NICs appear, and one MAC is marked
**PXE**. If more than one NIC came back, pick the one you cabled with **Use
for PXE**. If the sync fails, the job's raw log shows exactly which Redfish
call the CIMC refused and what it said. The usual causes: wrong password,
Redfish not enabled, or the VM cannot reach the CIMC IP.

## 6. Give it an address

**Manage → IP space → Add block.** Enter the server subnet from step 1 —
`10.0.0.0/24`, gateway `10.0.0.1`. (Yes, the same subnet the VM is on. It is a
flat network. The block just tells the installer what to configure.)

Back on the server page → **Assign address** → pick the block, take the first
free suggestion, tick **Primary**. The installer will configure this address
statically, so the server comes up exactly where you expect.

## 7. Test power control, the consoles, and the readings

Open the server from **Servers**. The strip under its name reads the power
state live from the BMC every ten seconds (the badge says which protocol
answered and when). The buttons:

| Button | What the BMC is asked to do |
|---|---|
| Power on | Power on. |
| Shut down | Press the power button (ACPI). The OS decides; the job reports if it has not shut down after five minutes. Never escalated automatically. |
| Force off | Cut power immediately. |
| Reset | Hard reset. If the server is off, it is powered on instead. |
| Power cycle | Off, confirm off, wait, on. |

Each click queues a job, and the card follows it to the end and re-reads the
BMC, so what it shows is what happened. Power goes over IPMI first (tens of
milliseconds per command) and falls back to Redfish if IPMI is not answering;
the job log records which one did the work. Per server, **Edit** can pin a
server to IPMI only or Redfish only.

If a job is holding the server -- a reinstall that hung -- admins get **reset
anyway** / **force off anyway** under the buttons. It is audited as a forced
action.

**Serial console.** The **Console** tab. The power buttons stay above it, so
press **Reset** and watch POST, the BIOS, the boot loader and the kernel
scroll past, then log in. Keys a browser swallows (F2 setup, F6 boot menu,
F12 network boot, BREAK) are buttons in the toolbar. The BMC allows one
console viewer; if someone else has it, you are offered **Take over**. Idle
consoles close after 30 minutes to free the slot. **Full page** opens the
same console on a page of its own.

**KVM.** The **KVM** button gets one-time tokens from the CIMC and opens its
HTML5 viewer in a new tab: video from POST onwards, keyboard and mouse
passthrough, and the viewer's own virtual media for an ISO on your machine.
Your browser talks to the CIMC directly, which works on the flat network.
Open **CIMC** once first and accept its self-signed certificate, or the
viewer tab is blocked. If your firmware keeps the viewer somewhere
unexpected, the panel falls back to the Java launcher and the web UI (and
`DOZ_KVM_URL_TEMPLATE` pins the path once you know it).

**Sensors.** The **Sensors** tab reads every sensor the BMC has over IPMI:
temperatures, fans, voltages, PSU output and, where the BMC supports DCMI,
the power draw. It re-reads every ten seconds while the tab is open, and
the **Overview** tab shows the hottest sensor, inlet temperature, fan
average and power draw at the same rate. Nothing here is stored or
estimated: it is what the BMC answered, with the time it answered.

**Event log.** The **Event log** tab is the BMC's System Event Log: fan
stalls, thermal trips, PSU events, with the sensor name resolved and the
reading that triggered it. It refreshes every 30 seconds. **Clear log**
empties it on the BMC (audited) once the cause is dealt with, so the next
fault stands out.

**Hardware.** Inventory (CPUs, memory, BIOS, NICs, drives) and health (PSU,
fans, temperatures, drive predicted failure) come from the BMC on a
schedule: inventory every 30 minutes, health every 5, both with their age
shown. **Sync inventory** and **Check health** read them now.

**Boot once from.** On the Overview tab, pick a device (network, first disk,
CD/virtual media, BIOS setup) and whether to reset, power cycle, power on, or
only set the flag. BMCs drop the flag about a minute after it is set with no
restart, so "then reset now" is the default.

**BMC tab.** What the controller says about itself (firmware, IPMI version,
its own network settings, faults, the power-restore policy, which you can
change), and the tools: **Test connection** (tries HTTPS, the CIMC XML API
and IPMI with each cipher suite, shows the raw answer from each, and
remembers a cipher suite that works where the default does not: the first
thing to click when a server shows "IPMI session failed"), **Prepare BMC**
(run for you when the server was added; the tab shows when), the locator
LED (blink it for a minute so remote hands find the box), **Reset BMC** for
a CIMC that has
stopped answering (the host keeps running), and **Rotate** for the IPMI
password. Rotation sets a new 16-character password on the BMC for the user
the platform logs in as, proves a fresh session works with it, then stores
it; the new password is shown once. It needs a writable secrets backend
(`file`, which the installer sets up, or `vault`).

**Install from an ISO.** Upload an ISO on the **Images** page (any size; it
streams to disk), have the management server fetch one from a URL, or copy
files into `installer/assets/iso/` and click **Scan directory**. Then on the
server: **Install from image** mounts it on the BMC as a virtual CD over
HTTP, sets a one-time CD boot and power-cycles. Open the **KVM** and drive
the installer with keyboard and mouse. Virtual media is Redfish, so this
needs the server on `auto` or `redfish`, not pinned to IPMI. The ISO is
served from `http://<management-ip>:8080/iso/`; the CIMC must be able to
reach that address.

With no job waiting, a reset also shows the netboot rail at work on the
console: PXE fires, iPXE chains to the management server, gets "No
provisioning job for this host. Booting from local disk", and falls through to
whatever is on disk.

That fall-through is the whole architecture working end to end: DHCP, TFTP,
iPXE, the control plane, and the BMC. If it does not happen, nothing else
will, so stop and check `journalctl -u dnsmasq` on the VM.

## 8. Create a customer and hand them the server

**Manage → Customers → New customer.** Give them an email and generate a
password. Send it to them; they can change it after logging in. Have them add
an SSH key under **SSH keys** — or add one for them from their account.

Server page → **Assign to customer**. Pick them, name the plan, set the price
you will bill in WHMCS. The server appears in their portal immediately.

## 9. The first reinstall

From the server page (or from the customer's own portal): **Reinstall OS** →
Ubuntu 22.04 → RAID 1 → type `REINSTALL`.

What you should see, in order, over about fifteen to twenty minutes:

| Stage | Where it shows | If it stalls here |
|---|---|---|
| setting one-time PXE boot | job log | Redfish rejected the boot override — bench test 2 |
| power cycling into installer | job log | BMC did not power the box — check the raw log |
| installer fetched boot script | job log, 20% | Never arrives → DHCP/TFTP/iPXE. `journalctl -u dnsmasq`, serial console |
| installer booted | ramdisk callback, 25% | Ramdisk loaded but cannot reach `http://10.0.0.5` — is the data NIC on the same network as the VM? |
| preparing storage / building array | 25–45% | StorCLI — bench test 5 |
| rebooting into the OS installer | 50% | The ramdisk has prepared the disks and reboots; the next PXE boot loads Ubuntu's own installer. If the box comes back into the ramdisk instead, the one-time PXE flag was not set (job log) |
| the OS installer boots | serial and KVM | Ubuntu's installer runs with the answer file; its own messages show on the console from here |
| Ubuntu 22.04 installed | callback | The installer's late-command reported back |
| clearing boot override → install complete | 95–100% | Done. `ssh root@10.0.0.20` |

Then try **Rescue mode**: the server reboots into the ramdisk with the
customer's key and stays there, disks untouched, `ssh root@<dhcp address>`.
This is the feature that prevents the most tickets — make sure it works.

## 10. Repeat for the fleet

Per server: CIMC setup (step 3), register (5), assign address (6), power test
(7). Firmware to 4.1(2f) before racking. Then assign to customers as they sign.

Deprovisioning a customer: **End subscription**, then **Secure wipe**. The wipe
job erases every drive (ATA secure erase, `nvme format`, or an overwrite, in
that order of preference), reports which ones, and only then returns the
server to stock. The state machine will not let a server skip the wipe and go
straight back on sale.

---

## What "flat network" means for security, and when to change it

Right now the CIMCs sit on the same network as customers' servers. A customer
with a shell on their own box can reach every CIMC's login page. The M4's
firmware will never be patched again. On day one with three trusted beta
customers that is a known, accepted risk. It is not one to carry into public
sale.

The move, when you make it, is the three planes from the spec:

1. Put the CIMC ports on their own VLAN (or a cheap separate 1G switch) with no
   route to anywhere except the management VM's second NIC.
2. Put the PXE/provisioning traffic on a second VLAN, and move dnsmasq to it.
3. Customer data ports on per-customer VLANs with the public block.

Nothing in the software changes. `DOZ_CONTROL_PLANE_URL` becomes the VM's
address on the provisioning VLAN, `cimc_ip` values become OOB-VLAN addresses,
and the IP blocks become public space. The job workers are already the only
thing that talks to a BMC, which is what makes the separation a config change
rather than a rewrite.

## Things that are still manual

Honest list, so nobody is surprised:

- **Suspend does not shut the switch port.** It flips the state, locks the
  customer's power controls, and writes an audit line saying the port still
  needs downing. Do it by hand until the switch model is picked and the hook
  is written.
- **KVM is for admins on the flat network.** The browser has to reach the
  CIMC. Customers get the serial console, which goes through the platform;
  giving them KVM needs a proxy in front of the CIMC, which comes with the
  move off the flat network.
- **Nothing inside the OS is monitored.** CPU load, memory and disk use need
  an agent in the customer's OS; a BMC cannot see them. What the panel shows
  is what the BMC can: temperatures, fans, voltages, power draw, PSU and
  drive health, NIC link.
- **Bandwidth graphs are empty.** The storage and API exist; the poller that
  reads switch counters does not, for the same reason.
- **No email.** New-customer passwords are shown to you once in the panel;
  you send them. Health warnings show in the fleet table; nobody is paged.
- **Billing is elsewhere.** The panel records plan and price so the portal can
  show them. Invoicing is WHMCS/HostBill's job.
