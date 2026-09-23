# DedicatedOZ

Control plane for a bare-metal hosting business running on Cisco UCS C220 M4s.

This is the portal and the provisioning rail: inventory, an async job engine,
power control over IPMI and Redfish, a netboot installer, rescue mode, secure
wipe, a browser serial console, vKVM launch, and the customer and admin UIs
over all of it. On the management server it runs as one Ubuntu service,
`doz`.

It deliberately does **not** include billing. Integrate WHMCS, HostBill or
Blesta against the API instead — billing is a tar pit of tax rules and payment
gateways, and none of it is a differentiator.

---

## What works today

| | |
|---|---|
| Service | `sudo ./doz.sh install` makes it a systemd service: `systemctl start\|stop\|restart doz`, starts at boot |
| Inventory | Register servers, sync hardware over Redfish, track rack/switch/VLAN position |
| Jobs | Every action is queued, with an explicit state machine and a full log of raw BMC traffic |
| Power | Live state, on, graceful shut down, force off, reset, power cycle — over IPMI with Redfish as fallback, each confirmed by reading the state back. Bulk actions from the fleet list |
| Sensors | Every sensor the BMC has, read live every 10 s while the page is open: temperatures, fans, voltages, PSU and power draw |
| Event log | The BMC's System Event Log with sensor names resolved, severity, and a clear button |
| Boot device | One-time boot from PXE, disk, CD/virtual media or BIOS setup, with the reset or power cycle that makes it take effect |
| Images | ISO store on the management server: upload from the browser, fetch from a URL, or scan a directory. **Install from image** mounts one as virtual media on the BMC and boots it |
| BMC tools | Locator LED, cold BMC reset, power-restore policy, IPMI user list, IPMI password rotation (set on the BMC, verified with a fresh session, stored) |
| Reinstall | iPXE → ramdisk → StorCLI RAID → kickstart/autoinstall/preseed → phone home |
| Rescue | Same rail, boots to RAM, disks untouched |
| Wipe | ATA secure erase / `nvme format` / `sg_format`, gated so an unwiped server cannot return to stock |
| Console | Serial-over-LAN in the browser (xterm.js over a websocket), embedded in the server page or full-page, with take-over when someone else holds the port |
| vKVM | One click asks the CIMC for launch tokens and opens its HTML5 viewer: video, keyboard and mouse passthrough, its own virtual media. Java launcher on older firmware |
| BMC setup | **Prepare BMC** turns on IPMI over LAN, SOL and BIOS console redirection through the CIMC XML API |
| IPAM | Blocks, assignments, free-pool view, customer-editable rDNS |
| Health | PSU, fan, temperature and drive pre-fail, polled every 15 minutes |
| Portal | Servers, power, reinstall, rescue, jobs, bandwidth, SSH keys, console |
| Admin | Fleet map, lifecycle control, suspend, raw job log viewer, audit trail |
| API | Customer tokens, same authorisation path as the portal |

Not built, on purpose: billing, BIOS tuning, advanced RAID options, and
proxying the vKVM's video through the platform (the CIMC's own viewer is
launched instead). Wait until someone asks.

Nothing in the panel is made up. Power state, sensors, the event log and the
BMC's details are read from the BMC when the page asks; hardware inventory
and health are re-read on a schedule (every 30 and 5 minutes) and shown with
their age. What a BMC cannot see — CPU load, memory use, disk space inside
the running OS — is not shown, because it would need an agent in the OS.

---

## Architecture

Three network planes, and the separation is the security model:

- **Control plane** — API, workers, Postgres, Redis. The only thing customers reach.
- **OOB plane** — an isolated VLAN carrying CIMC traffic only. Reachable *only*
  from job workers. No route to the internet, ever.
- **Customer plane** — switch ports, per-customer VLANs, public addresses.

The M4 is past Cisco's last day of support. There will never be another CIMC
firmware release, so the BMCs will never be patched again. Isolation is not a
best practice here; it is the entire defence. Customers never reach a BMC:
the management server talks to the CIMCs on their behalf. Jobs run in the
workers. The API process also reads CIMC credentials, because the serial
console, the live power state and the vKVM launch tokens are answered while
someone waits. Splitting the API onto a host without an OOB route would mean
moving those three behind a worker first.

### Everything is a job

No provisioning action is a synchronous call. A reinstall takes ~20 minutes and
fails partway through in interesting ways. So:

```
server:  in_stock → provisioning → active → suspended → wiping → in_stock
                                      ↓
                                 rma / retired

job:     queued → running → (succeeded | failed | cancelled)
```

Both machines are declared as transition tables in `app/enums.py` and enforced
in `app/services/`. An illegal transition raises rather than silently leaving a
server somewhere nothing knows how to recover from.

The database is the source of truth, not Celery. A lost broker message leaves a
job visibly `queued`; a dead worker leaves one `running` past its deadline,
which the reaper fails. Neither disappears quietly.

### IPMI first, Redfish behind it, not Cisco

`app/drivers/` has two vendor-neutral drivers behind one interface:

- **IPMI** (`ipmitool lanplus`) drives power and the one-time PXE override.
  A call takes tens of milliseconds, against seconds for the M4's Redfish,
  and it is what the serial console needs anyway. The password goes through
  the environment, never the command line, and cipher suite 3 is pinned
  because ipmitool's suite probe costs ten seconds per call on BMCs that
  ignore it.
- **Redfish** handles inventory, health and virtual media, and takes over
  power and boot when IPMI fails.

`DOZ_BMC_PROTOCOL=auto` (the default) does exactly that. Each server can be
pinned to `ipmi` or `redfish` in the panel. Cisco-specific code is confined
to `drivers/cimc.py`, the XML API used for BMC setup and vKVM tokens.
Swapping in M5s or Dell R640s later means adding a driver, not a rewrite.

Both drivers assume nothing works. They read back every boot override they
set, confirm every power change by reading the state back, fall back when the
BMC refuses a graceful reset type, try both forms of virtual media insert,
and redact credentials before anything reaches the job log.

---

## Repository layout

```
doz.sh               run script: install / update / up / down / status / logs / sim / ...
backend/
  app/
    drivers/         IPMI (power, boot, sensors, SEL, users) + Redfish (inventory, vmedia), CIMC XML API
    services/        state machines, job engine, boot rendering, IPAM, image store, live BMC reads
    api/             HTTP surface: customer, admin, and the netboot rail
    workers/         Celery tasks
  scripts/
    bench_validate.py   the five pre-build hardware tests
    init_db.py          schema create/upgrade + seed
    smoke_test.py       end-to-end check of an install: power + serial console
  tests/
installer/
  templates/         iPXE scripts, provision.sh, OS answer files (Jinja2)
  build-ramdisk.sh   builds the installer image
frontend/            React portal + management panel
deploy/
  install-management-server.sh   Ubuntu VM -> the `doz` service, from scratch
  smoke-test.sh                  check an install against the BMC simulator
  sim/                           simulated BMC (OpenIPMI ipmi_sim) for no-hardware testing
  fetch-os-images.sh             Ubuntu / Debian / Rocky netboot assets
docs/
  GETTING-STARTED.md   flat-network onboarding, CIMC setup, first reinstall
  REVIEW.md            what would have broken, what was fixed, what is unknown
```

---

## Before you write any more code: validate the hardware

Section 2 of the spec is not optional. Run this against one server on the
bench, before racking anything:

```sh
cd backend
python -m scripts.bench_validate --host 10.0.0.10 --user admin \
    --iso-url http://10.10.0.5:8080/iso/ubuntu-22.04.iso \
    --power-cycle --prepare
```

`--prepare` first turns on IPMI over LAN and SOL through the CIMC XML API.
The script then checks firmware and Redfish reachability, the one-time PXE
override over both IPMI and Redfish, vMedia ISO boot, a live SOL session and
vKVM launch, and walks you through driving StorCLI non-interactively.
Whichever of PXE and vMedia proves more reliable becomes your primary install
path. Test 5 is the awkward one — budget time for it.

Standardise the whole fleet on CIMC **4.1(2f)**, the final M4 release, via HUU
**on the bench, before racking**. A mixed-firmware fleet makes every later
failure ambiguous.

---

## Running it

### On the management server (production)

A fresh Ubuntu 22.04 / 24.04 VM becomes the whole control plane — API,
workers, nginx, PXE — in one command, installed as the `doz` service:

```sh
git clone https://github.com/ehab3233/DedicatedOZ.git && cd DedicatedOZ
sudo ./doz.sh install --ip 10.0.0.5 --dhcp-range 10.0.0.200,10.0.0.249
```

`doz` is one systemd unit over all the parts, so the usual commands work on
the whole stack and it starts at boot:

```sh
sudo systemctl status doz        # or: ./doz.sh status, which also shows each job queue's worker
sudo systemctl restart doz
journalctl -u 'doz*' -f          # or: ./doz.sh logs [api|power|provision|poll|beat|pxe]
sudo ./doz.sh update             # after git pull: redeploy, keep config and secrets
```

The parts are `doz-api`, one Celery worker per queue (`doz-worker-power`,
`doz-worker-provision`, `doz-worker-poll`), `doz-beat` and `doz-pxe`, so a
twenty-minute reinstall never holds up someone's power button.

To try power control and the serial console before any hardware arrives:

```sh
sudo apt install --no-install-recommends openipmi
sudo ./doz.sh sim start          # a simulated BMC, registered as server SIM-0001
sudo ./deploy/smoke-test.sh      # drives power and the console through nginx; resets the admin password
```

Then **[docs/GETTING-STARTED.md](docs/GETTING-STARTED.md)** walks through
getting the first server in: CIMC setup, the bench test, registering it in
the panel, addressing, the first reinstall. It assumes a flat network and
says what to change when you outgrow one.

### On a laptop (development)

```sh
./doz.sh up          # creates .env, venv, node_modules; inits the DB; starts everything
./doz.sh logs        # api, worker, beat, frontend
./doz.sh down
```

Needs a local PostgreSQL and Redis (`docker compose up -d postgres redis`
gives you both). Portal at http://localhost:5173, API docs at
http://localhost:8000/docs.

`./doz.sh` also wraps `test`, `lint`, `shell`, `bench <cimc-ip>`,
`reset-admin <email>`, `assets` (fetch OS images) and `ramdisk` (build the
installer image), and detects whether it is talking to a dev checkout, a
docker compose stack, or a systemd install.

### The management panel

Log in as an admin and open **Manage**:

| Page | What you do there |
|---|---|
| Dashboard | Fleet counts, how many servers are powered on right now, what needs attention (unhealthy servers, missing PXE MACs, firmware off baseline, failed jobs), recent jobs, and whether every service and worker is running. |
| Servers | Every server with live power, state, health, location, CIMC, firmware and customer. Search, filter by state, select several for bulk power actions. **Add server** registers one and syncs its hardware. |
| Server → Overview | Power controls, live readings (hottest sensor, inlet, fans, power draw), reinstall / install from image / rescue / wipe, boot once from a device, lifecycle, customer, location. |
| Server → Console | Serial console in the page or full-page; vKVM launch. |
| Server → Hardware | CPU, memory, BIOS, firmware, health subsystems, NICs (pick the PXE one), drives with predicted failure. Sync inventory and check health on demand. |
| Server → Sensors | Every sensor, grouped, refreshed every 10 s. |
| Server → Event log | The System Event Log, newest first, with clear. |
| Server → Network | Addresses, switch port and VLAN, what the BMC says about its own network. |
| Server → Jobs | Everything that has run on this server. |
| Server → BMC | How the platform reaches it, controller details, power-restore policy, Prepare BMC, locator LED, BMC reset, password rotation, virtual media, IPMI users. |
| Images | The ISO store: upload, fetch from URL, scan, delete. |
| Customers | Create accounts, generate initial passwords, disable logins, see and end subscriptions. |
| IP space | Add blocks with gateways and provenance, see utilisation, find free addresses. |
| Jobs | Everything that has run, filterable by state, with raw BMC exchanges. |
| Audit | Who did what, from where, when. |

### Tests

```sh
cd backend && python -m pytest
```

They run against a real PostgreSQL (JSONB, INET, partial unique indexes), so
they test what actually ships. The Redfish driver is tested against a simulated
CIMC including its known misbehaviours — session limits, a boot override that
reports success without taking effect, reset types the BMC will not accept, and
virtual media missing its InsertMedia action. The IPMI driver, the power jobs
and the browser console are tested against a real IPMI stack (OpenIPMI's
`ipmi_sim`, skipped if it is not installed), including SOL take-over. Every
rendered provisioning script is syntax-checked with a POSIX shell.

CI also installs the whole thing as a service on a clean Ubuntu 24.04 runner,
runs the smoke test, runs `doz.sh update` and the smoke test again, and
restarts the service.

**[docs/REVIEW.md](docs/REVIEW.md)** is an honest pass over what would have
broken on real hardware (fixed), what would have bitten on a flat network
(fixed), and what genuinely cannot be known until the bench test runs.

---

## Configuration

All of it is environment-driven; see `.env.example`. The ones that matter:

- `DOZ_JWT_SECRET` — also derives the per-MAC boot signature, so rotating it
  invalidates in-flight provisioning jobs. Rotate between installs.
- `DOZ_CONTROL_PLANE_URL` — where the ramdisk reaches the API. Must resolve
  from the provisioning VLAN.
- `DOZ_SECRETS_BACKEND` — `env` for the bench, `file` (what the installer
  sets up) or `vault` in production. CIMC passwords never enter Postgres;
  `servers.cimc_credential_ref` is a pointer.
- `DOZ_BMC_PROTOCOL` — `auto` (IPMI, then Redfish), `ipmi` or `redfish`.
- `DOZ_IPMI_CIPHER_SUITE` — leave it at `3` unless a BMC refuses it.
- `DOZ_KVM_URL_TEMPLATE` — only needed if your CIMC's HTML5 viewer lives at a
  path the launcher does not probe.
- `DOZ_IMAGE_DIR` — where uploaded ISOs live; served at
  `DOZ_BOOT_ASSET_BASE_URL/iso/`, which is the URL a BMC mounts them from.
- `DOZ_REQUIRE_WIPE_BEFORE_STOCK` — leave it on. It is what stops a server
  going back on sale with the last customer's data still on it.

---

## Build order

Following section 11 of the spec, with the current state marked:

1. ✅ Bench firmware standardisation + the five validation tests (`bench_validate.py`)
2. ✅ Data model, inventory, CIMC credential storage
3. ✅ Job queue and state machine with audit logging
4. ✅ Power control — IPMI with Redfish fallback, confirmed by read-back
5. ✅ iPXE + installer ramdisk + OS templates
6. ✅ Rescue mode and disk wipe
7. ✅ Serial console in the browser, vKVM launch, sensors, event log, BMC tools, ISO image store
8. ⚠️ Bandwidth — storage, API and graphs are built; the switch poller is a
   stub, because it depends on a switch model that has not been chosen
9. ✅ Customer UI
10. ⚠️ Admin UI and IPAM are built; health *alerting* stores status but does not
    yet notify anyone (email is deferred)
11. ⬜ Billing integration — deliberately not started
12. ⬜ Beta

---

## The things that actually decide whether this works

None of them are the software. From section 10 of the spec, restated because
they are easy to defer and fatal to defer:

1. **IPv4 space.** Lease or acquire it. This gates everything else. `ip_blocks`
   has a `source` column so you can prove provenance later; that is the easy part.
2. **DDoS mitigation.** One attack on one customer takes the whole rack off the
   air without upstream scrubbing. Confirm what your transit provider offers
   *before* launch, not after the first incident.
3. **Abuse handling.** Have a written policy and a working suspend path on day
   one. The admin suspend endpoint flips lifecycle state immediately, but the
   switch-port shutdown is not yet automated (deferred for now) — it records
   intent and the port must currently be downed by hand. Wire that up before
   selling anything.

Also worth confirming with the facility before you sell: a dedicated OOB port
or management VLAN per server, remote-hands pricing and response SLA (on EOL
hardware you *will* have failures, and one 3am callout can eat a month of
margin on a budget plan), how many addresses you get and whether rDNS can be
delegated, and power cost per amp — E5 v3/v4 in 1U is power-hungry per unit of
performance, so price against power, not against hardware you already own.

## One thing the spec is right about that this repository ignores

Section 7 suggests wrapping Tinkerbell or Ironic instead of building
provisioning. That advice is sound and this repository does not take it — you
asked for the rail, so here it is. If time to launch matters more than control,
the boot templates and the driver layer here are still useful, and
`app/workers/tasks.py` is the seam where a Tinkerbell workflow would slot in
behind the same job API.
