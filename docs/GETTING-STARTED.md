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

You cannot run two authoritative DHCP servers on one network. Pick one.

## 2. Install the management server

On the fresh Ubuntu 22.04 or 24.04 VM, with a static IP already configured:

```sh
sudo apt install -y git
git clone https://github.com/ehab3233/DedicatedOZ.git
cd DedicatedOZ
sudo ./doz.sh install --ip 10.0.0.5 --dhcp-range 10.0.0.200,10.0.0.249
# or, with your router still doing DHCP:
sudo ./doz.sh install --ip 10.0.0.5 --proxy-dhcp
# or, no netboot yet -- just the panel, power and consoles:
sudo ./doz.sh install --ip 10.0.0.5 --no-pxe
```

About ten minutes. It ends with the portal URL and a one-time admin password.
Log in at `http://10.0.0.5` straight away and confirm you get the Manage tab.

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

To update later: `git pull && sudo ./doz.sh update`. The settings from the
first install are remembered in `/etc/doz/install.conf`; secrets in
`/etc/doz/doz.env` are never overwritten.

### No hardware on the bench yet?

Try power control and the serial console against a simulated BMC first:

```sh
sudo apt install --no-install-recommends openipmi
sudo /opt/doz/doz.sh sim start
```

That starts OpenIPMI's `ipmi_sim` -- a real IPMI-over-LAN implementation --
with a pretend server behind it, and registers it as **SIM-0001**. Open it in
the panel: Power on / Shut down / Force off / Reset / Power cycle work, and
**Open serial console** shows a POST screen and a login prompt when you reset
it. `sudo /opt/doz/deploy/smoke-test.sh` runs the whole check automatically.
Remove it with `sudo /opt/doz/doz.sh sim remove`.

Then fetch the OS images and build the installer ramdisk. These take a while
and a couple of gigabytes, which is why they are separate:

```sh
cd /opt/doz
sudo -u doz ./deploy/fetch-os-images.sh          # Ubuntu 22.04, Debian 12, Rocky 9
sudo apt install -y docker.io && sudo ./doz.sh ramdisk    # the installer image
```

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
You do not need to fix that by hand -- step 5's **Prepare BMC** switches both
on. (If you would rather: CIMC web UI → Admin → Communication Services → IPMI
over LAN, and Server → Remote Presence → Serial over LAN, 115200.)

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

On save, an **inventory sync** job runs. Open the server (click its serial)
and watch it: within a minute the CPU, RAM, firmware, drives and NICs appear,
and one MAC is marked **PXE**. If more than one NIC came back, pick the one you
cabled with **Use for PXE**.

Then click **Prepare BMC**. Over the CIMC's XML API it switches on IPMI over
LAN, switches on Serial-over-LAN at 115200 on COM0, and points the BIOS
console redirection at the same port (that last one takes effect at the next
boot). It then proves IPMI works by reading the power state, and reads back
the SOL settings. The job log shows each step. If the job fails, the message
says which of the usual three it is: wrong password, IPMI still off, or UDP
623 blocked between the VM and the CIMC.

If the sync fails, the job's raw log shows exactly which Redfish call the CIMC
refused and what it said. The usual causes: wrong password, IPMI/Redfish not
enabled, or the VM cannot reach the CIMC IP.

## 6. Give it an address

**Manage → IP space → Add block.** Enter the server subnet from step 1 —
`10.0.0.0/24`, gateway `10.0.0.1`. (Yes, the same subnet the VM is on. It is a
flat network. The block just tells the installer what to configure.)

Back on the server page → **Assign address** → pick the block, take the first
free suggestion, tick **Primary**. The installer will configure this address
statically, so the server comes up exactly where you expect.

## 7. Test power control and the consoles

The **Power** card on the server page reads the power state live from the BMC
(the badge says which protocol answered and when). The buttons:

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

**Serial console.** Click **Open serial console**. The power buttons are on the
same page, so press **Reset** and watch POST, the BIOS, the boot loader and the
kernel scroll past, then log in. Keys a browser swallows (F2 setup, F6 boot
menu, F12 network boot, BREAK) are buttons above the terminal. The BMC allows
one console viewer; if someone else has it, you are offered **Take over**.
Idle consoles close after 30 minutes to free the slot.

**KVM.** **Launch KVM** gets one-time tokens from the CIMC and opens its HTML5
viewer in a new tab. Your browser talks to the CIMC directly, which works on
the flat network. Open **CIMC web UI** once first and accept its self-signed
certificate, or the viewer tab is blocked. If your firmware keeps the viewer
somewhere unexpected, the panel falls back to the Java launcher and the web UI
(and `DOZ_KVM_URL_TEMPLATE` pins the path once you know it).

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
| running OS installer | 55% | kexec into Ubuntu. Serial console shows subiquity from here |
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
- **Bandwidth graphs are empty.** The storage and API exist; the poller that
  reads switch counters does not, for the same reason.
- **No email.** New-customer passwords are shown to you once in the panel;
  you send them. Health warnings show in the fleet table; nobody is paged.
- **Billing is elsewhere.** The panel records plan and price so the portal can
  show them. Invoicing is WHMCS/HostBill's job.
