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
order (disk first, PXE available, legacy mode), sets the BIOS power profile
(`DOZ_BMC_PREPARE_POWER_PROFILE`, default `balanced`: CPU power technology
Energy Efficient with the energy/performance bias at Balanced Energy, so the
CPUs idle down between bursts; `performance` or `low_power` are the other
choices, empty leaves the BIOS alone) and, if `DOZ_NTP_SERVERS` is set, NTP.
BIOS changes take effect at the next boot. It then proves IPMI
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

**KVM.** The **KVM** button opens the Console tab with the CIMC's HTML5
viewer inside the page: video from POST onwards, keyboard and mouse
passthrough, and the viewer's own virtual media for an ISO on your machine.
**Full screen** gives it the whole display; **Open in a tab** is the same
viewer on its own. The panel gets one-time tokens from the CIMC for each
connection, and they last about a minute, so **Reconnect** if the viewer
asks you to log in. Your browser talks to the CIMC directly, which works on
the flat network. Open **CIMC** once first and accept its self-signed
certificate, or the frame stays blank. A CIMC that forbids being shown
inside another page is detected and gets a tab instead. If your firmware
keeps the viewer somewhere unexpected, the panel falls back to the Java
launcher and the web UI, and the message says what each path it tried
answered. To pin the right one:
open the CIMC, use its own **Launch KVM → HTML based**, copy the link its
pop-up shows, and set `DOZ_KVM_URL_TEMPLATE` to that link with `{host}`,
`{tkn1}` and `{tkn2}` in place of the address and the two tokens. The Java
launcher (`kvm.jnlp`) needs a Java Web Start such as OpenWebStart.

**Sensors.** The **Sensors** tab reads every sensor the BMC has over IPMI:
temperatures, fans, voltages, PSU output and, where the BMC supports DCMI,
the power draw. The worker reads every server once a minute and keeps the
report, so a page opens with readings at once and says how old they are;
**Read now** asks the BMC itself. The **Overview** tab shows the hottest
sensor, inlet temperature, the fan speed range and power draw from the same
report. Nothing here is estimated: it is what the BMC answered, with the
time it answered. When IPMI is not answering (off, or an IPMI encryption key the panel does not have) the same readings come from Redfish's Thermal and Power resources and the page says so. The power figure is DCMI where the BMC has it, otherwise the PSU output sensors; input and output are never added together. The overview's **Utilisation** card is the CIMC's own CPU, memory and IO percentages, the ones its summary page charts, which it measures through the management engine whatever OS is installed.

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
whatever is on disk. A server with nothing on disk stays at the boot loader
("No bootable disk. Waiting here for a job") and asks the management server
again every minute, rather than going round through POST and PXE forever.

That fall-through is the whole architecture working end to end: DHCP, TFTP,
iPXE, the control plane, and the BMC. If it does not happen, nothing else
will, so stop and check `journalctl -u dnsmasq` on the VM.

**The management server itself.** The server the installer registers as
`SIM-0001` (hostname `sim01`) has the role *management*: the panel shows its
health, readings and event log but never powers, provisions or configures
it, and every such request is refused with a clear message. Its BMC is the
built-in simulator, so those readings are simulated; the VM's own services
are on the Dashboard. The role is on the Edit form, so any server can be
marked the same way.

## 7b. HTTPS and the customers' graphical console

The portal can run on a domain over HTTPS, which also makes the CIMC's
graphical console available to customers. Create two DNS records pointing
at the management server, `portal.example.com` and `*.portal.example.com`,
make a Cloudflare API token with permission to edit that zone's DNS, and
run the installer with them (on an existing install, the same flags on
`sudo /opt/doz-src/doz.sh update`):

```sh
DOZ_DOMAIN=portal.example.com DOZ_CLOUDFLARE_TOKEN=... \
  curl -fsSL https://raw.githubusercontent.com/ehab3233/DedicatedOZ/HEAD/install.sh | sudo bash
```

The installer gets a Let's Encrypt certificate for the domain and the
wildcard through Cloudflare's DNS, renews it on certbot's timer, serves the
portal on `https://portal.example.com` (plain HTTP on the name redirects;
the address stays on plain HTTP because the netboot rail needs it), and sets
`DOZ_PORTAL_DOMAIN` and `DOZ_PORTAL_URL` for the API.

**How a customer opens the graphical console.** On the server's Console
tab, **Open the graphical console** creates a temporary user on that
server's CIMC (role *user*: power, console and virtual media; no BMC
settings) and a hostname `kvm-<label>.portal.example.com` that nginx
proxies to that CIMC for the customer's browser only. The browser carries a
signed ticket cookie the portal sets on its domain; nginx asks the API about
it on every request and proxies the CIMC's own viewer, websockets included,
so nothing about the viewer changes. The customer logs in on the CIMC page
with the one-time username and password the portal shows, then uses its
own **Launch KVM → HTML based**. The grant lasts `DOZ_KVM_GRANT_HOURS` (4);
when it runs out, or the customer ends it, the CIMC user is removed and the
hostname stops answering. Every grant and removal is in the audit log, with
the CIMC username.

What this does not do: it never shows a customer the CIMC's admin login,
never reaches a CIMC without a live grant, and does not let two customers'
grants see each other's servers (the hostname is per grant and tied to the
customer who holds it). The management server must be able to reach the
CIMC VLAN on TCP 443 and 2068, which it already needs for the admin KVM.

## 8. Create a customer and hand them the server

**Manage → Customers → New customer.** Give them an email and generate a
password. Send it to them; they can change it after logging in. Have them add
an SSH key under **SSH keys** — or add one for them from their account.

Server page → **Assign to customer**. Pick them, name the plan, set the price
you will bill in WHMCS. The server appears in their portal immediately.

**What the customer sees.** Signing in lands on an overview: every server
with its power state, health and primary address, and the recent activity
across them. A server opens in tabs: **Overview** (power buttons, live
temperatures, fan speeds, power draw and utilisation as the hardware reports
them, hardware, address, plan, health), **Console** (the serial console,
full-page if they like), **Operating system** (reinstall with their choice of
OS, hostname, disk layout and keys, behind a typed confirmation; rescue
mode), **Network** (addresses with reverse DNS they can edit, the server's
ports), **Traffic** (switch-port counters over a day, a week or a month, and
usage against the plan's included traffic), and **Activity** (every job with
its own log). **Account** holds their contact details, password, API tokens
and the notification switch. With `DOZ_SMTP_HOST`, `DOZ_MAIL_FROM` and
`DOZ_PORTAL_URL` set in `/etc/doz/doz.env`, they are mailed when a reinstall,
rescue boot or wipe on their server finishes, with a link to the job log.
The plan's price is shown to them exactly as you entered it; invoicing is
still WHMCS's job.

## 9. The first reinstall

From the server page (or from the customer's own portal): **Reinstall OS** →
Ubuntu 22.04 → RAID 1 → type `REINSTALL`.

What you should see, in order, over about fifteen to twenty minutes:

| Stage | Where it shows | If it stalls here |
|---|---|---|
| building raid1 through the BMC | 3% | The BMC could not build the array: the job log shows what the CIMC said. Hardware tab → Configure RAID runs the same step on its own. The host is powered on for this; with nothing on disk it PXE-boots meanwhile and the job notes it as *parked at the boot loader until the disks are ready* — that is normal, the server waits there |
| setting one-time PXE boot | job log | Redfish rejected the boot override — bench test 2 |
| waiting for the parked boot loader to fetch the script | job log | Only when the server was parked: it is given a minute to pick the script up itself, which saves a POST. If it does not, the job power cycles as below |
| power cycling into installer | job log | BMC did not power the box — check the raw log |
| installer fetched boot script | job log, 20% | Never arrives → DHCP/TFTP/iPXE. `journalctl -u dnsmasq`, serial console |
| installer booted | ramdisk callback, 25% | Ramdisk loaded but cannot reach `http://10.0.0.5` — is the data NIC on the same network as the VM? |
| preparing storage | 25–45% | The ramdisk wipes the array the BMC built (no StorCLI needed) |
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
   route to anywhere except the management VM.
2. Put the management VM on a services VLAN that every customer VLAN can reach
   on the three PXE ports and nothing else.
3. Customer data ports on per-customer VLANs with the public block.

Nothing in the software changes. `cimc_ip` values become OOB-VLAN addresses
and the IP blocks become public space. The job workers are already the only
thing that talks to a BMC, which is what makes the separation a config change
rather than a rewrite. The rest of this section is that config.

### Per-customer VLANs on a MikroTik (RouterOS 7)

**How a server is wired.** Its data port is an access port in the customer's
VLAN: untagged to the server, so the installed OS has no VLAN configuration
at all (the installer writes a plain static address, and does not tag). The
gateway for the customer's public addresses is the router's address on that
VLAN. The CIMC port is an access port in the CIMC VLAN.

**How PXE works inside a customer VLAN.** The server's PXE ROM asks for DHCP
in its own VLAN, so each customer VLAN has a small DHCP server on the router
whose only job is the install: it hands out a private address for the few
minutes the ramdisk and the OS installer run, with `next-server` pointing at
the management VM. The installed OS never uses DHCP; it comes up on the
public address the panel assigned. The ramdisk and the installer reach the
VM through the router, which is why the VM must be reachable from every
customer VLAN on UDP 69 and TCP 80 and 8080, and nothing else.

**The example below** uses: VLAN 10 for the management VM (`203.0.113.5`),
VLAN 20 for the CIMCs (`10.20.0.0/24`), and VLAN 101 for customer 1 with the
public block `203.0.113.8/29` (router `.9`, first server `.10`) and
`10.101.0.0/24` for PXE leases. Add a VLAN per customer the same way, with
the next /29 and the next 10.1xx.0.0/24. `ether1` is the uplink, `ether2` the
VM host, `ether3` customer 1's server data port, `ether5` the CIMC ports (or
a separate switch). Replace the addresses with your own.

```
# VLAN-aware bridge; keep filtering off until the VLAN table is complete.
/interface bridge add name=bridge vlan-filtering=no
/interface bridge port add bridge=bridge interface=ether2 pvid=10 frame-types=admit-only-untagged-and-priority-tagged
/interface bridge port add bridge=bridge interface=ether3 pvid=101 frame-types=admit-only-untagged-and-priority-tagged
/interface bridge port add bridge=bridge interface=ether5 pvid=20 frame-types=admit-only-untagged-and-priority-tagged
/interface bridge vlan add bridge=bridge vlan-ids=10 tagged=bridge untagged=ether2
/interface bridge vlan add bridge=bridge vlan-ids=20 tagged=bridge untagged=ether5
/interface bridge vlan add bridge=bridge vlan-ids=101 tagged=bridge untagged=ether3

# The router's leg in each VLAN.
/interface vlan add name=vlan10-mgmt interface=bridge vlan-id=10
/interface vlan add name=vlan20-cimc interface=bridge vlan-id=20
/interface vlan add name=vlan101-cust1 interface=bridge vlan-id=101
/ip address add address=203.0.113.1/29 interface=vlan10-mgmt
/ip address add address=10.20.0.1/24 interface=vlan20-cimc
/ip address add address=203.0.113.9/29 interface=vlan101-cust1
/ip address add address=10.101.0.1/24 interface=vlan101-cust1

# PXE-only DHCP in the customer VLAN: a private lease for the install, with
# the PXE options pointing at the VM.
/ip pool add name=pxe-cust1 ranges=10.101.0.100-10.101.0.199
/ip dhcp-server add name=dhcp-cust1 interface=vlan101-cust1 address-pool=pxe-cust1 lease-time=30m
/ip dhcp-server network add address=10.101.0.0/24 gateway=10.101.0.1 dns-server=10.101.0.1 \
    next-server=203.0.113.5 boot-file-name=undionly.kpxe

/interface list add name=customers
/interface list member add list=customers interface=vlan101-cust1

# What may cross between VLANs. Order matters; put these above any
# catch-all accept in your forward chain.
/ip firewall filter
add chain=forward connection-state=established,related action=accept
add chain=forward in-interface-list=customers out-interface-list=customers action=drop \
    comment="customers never see each other"
add chain=forward in-interface-list=customers out-interface=vlan20-cimc action=drop \
    comment="customers never reach a CIMC"
add chain=forward in-interface-list=customers dst-address=203.0.113.5 protocol=udp dst-port=69 action=accept \
    comment="PXE: the loaders"
add chain=forward in-interface-list=customers dst-address=203.0.113.5 protocol=tcp dst-port=80,8080 action=accept \
    comment="PXE: boot scripts, callbacks, ramdisk, images"
add chain=forward in-interface-list=customers dst-address=203.0.113.5 action=drop \
    comment="nothing else on the VM from a customer VLAN"
add chain=forward src-address=203.0.113.5 out-interface=vlan20-cimc protocol=udp dst-port=623 action=accept \
    comment="VM -> CIMCs: IPMI, SOL"
add chain=forward src-address=203.0.113.5 out-interface=vlan20-cimc protocol=tcp dst-port=443 action=accept \
    comment="VM -> CIMCs: Redfish, XML API, KVM tokens"
add chain=forward in-interface=vlan20-cimc dst-address=203.0.113.5 protocol=tcp dst-port=8080 action=accept \
    comment="a CIMC fetching an ISO for virtual media"
add chain=forward out-interface=vlan20-cimc action=drop \
    comment="CIMCs: only the VM (add your VPN above this line: tcp 443,2068 for the KVM)"

# Last: switch the VLAN table on.
/interface bridge set bridge vlan-filtering=yes
```

The router's own `input` chain must accept DHCP (UDP 67) from the customer
VLANs for the PXE lease; the default MikroTik firewall does for interfaces
in its LAN list, so add each customer VLAN to that list or add the rule.
Customers' public addresses are routed, not NATed; make sure no `srcnat`
masquerade rule matches them.

**In the panel, per customer:**

1. **Manage → IP space → Add block:** `203.0.113.8/29`, gateway
   `203.0.113.9`, VLAN `101`, bridged.
2. **Server → Edit:** Customer VLAN `101`, and the switch port, so the
   record says where the cable goes.
3. **Server → Assign address:** `203.0.113.10` from that block, Primary.
4. **Reinstall.** The ramdisk takes a `10.101.0.x` lease, installs, and the
   OS comes up on `203.0.113.10/29` via `203.0.113.9`.

The VLAN numbers on the block and the server are recorded for you and shown
on the Network tab; the panel does not program the switch, so moving a
server to another customer is: change the port's `pvid`, change the two
fields, assign an address from the new block, reinstall.

**Public addresses by static lease instead.** If you would rather not carry
a private PXE subnet per VLAN, the DHCP server in the customer VLAN can hand
each server its own public address, bound to its PXE MAC, with the same PXE
options, and no dynamic pool at all. The installed OS still gets that
address statically from the panel, so the lease and the assignment must
agree; the lease is only used by the PXE ROM, the ramdisk and the OS
installer, and by a manual install from an ISO, which then also comes up on
the right address. For customer 1 on VLAN 1000 with `103.167.10.0/28`:

```
/interface vlan add name=vlan1000-cust1 interface=bridge vlan-id=1000
/ip address add address=103.167.10.1/28 interface=vlan1000-cust1
/ip dhcp-server add name=dhcp-cust1 interface=vlan1000-cust1 address-pool=static-only lease-time=1d
/ip dhcp-server network add address=103.167.10.0/28 gateway=103.167.10.1 dns-server=1.1.1.1 \
    next-server=203.0.113.5 boot-file-name=undionly.kpxe
/ip dhcp-server lease add server=dhcp-cust1 address=103.167.10.10 mac-address=<the server's PXE MAC> comment="server 1"
```

`static-only` matters: a device a customer plugs in with an unknown MAC gets
nothing, rather than one of your public addresses. In the panel the block is
`103.167.10.0/28`, gateway `103.167.10.1`, VLAN `1000`, and the server's
primary address is `103.167.10.10`; the PXE MAC is on its Hardware tab.

**What the panel does with this.** The server's **Network** tab shows,
from the block's VLAN and gateway, the assigned address, the PXE MAC and
the switch port, exactly what the router must hold for that server: the
port's VLAN, the DHCP server for it, and the lease. It shows the RouterOS
commands for it, so the two can be checked against each other. With
`DOZ_ROUTEROS_URL`, `DOZ_ROUTEROS_USERNAME` and `DOZ_ROUTEROS_PASSWORD` set
in `/etc/doz/doz.env` (a RouterOS 7 user with the `read`, `write`, `api` and
`rest-api` policies, and the `www-ssl` service on), the panel applies it
itself: when a primary address is assigned, at the start of every install,
and from **Apply to router** on that tab. Releasing the address removes the
lease. Either way the installer writes the address, prefix and gateway into
the OS statically; nothing after the install depends on DHCP.

**On the VM** nothing changes between customers. If you move the VM onto
the services VLAN at this point, run
`sudo /opt/doz-src/doz.sh update --iface <nic> --ip 203.0.113.5` once so the
loaders and callbacks carry that address, and set each server's CIMC
address in the panel to its VLAN 20 address.

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
