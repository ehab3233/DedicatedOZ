# DedicatedOZ

Control plane for a bare-metal hosting business running on Cisco UCS C220 M4s.

This is the portal and the provisioning rail: inventory, an async job engine,
power control over Redfish, a netboot installer, rescue mode, secure wipe, a
serial console, and the customer and admin UIs over all of it.

It deliberately does **not** include billing. Integrate WHMCS, HostBill or
Blesta against the API instead — billing is a tar pit of tax rules and payment
gateways, and none of it is a differentiator.

---

## What works today

| | |
|---|---|
| Inventory | Register servers, sync hardware over Redfish, track rack/switch/VLAN position |
| Jobs | Every action is queued, with an explicit state machine and a full log of raw BMC traffic |
| Power | On, graceful off, cycle, hard reset — with read-back confirmation |
| Reinstall | iPXE → ramdisk → StorCLI RAID → kickstart/autoinstall/preseed → phone home |
| Rescue | Same rail, boots to RAM, disks untouched |
| Wipe | ATA secure erase / `nvme format` / `sg_format`, gated so an unwiped server cannot return to stock |
| Console | Serial-over-LAN bridged to a browser websocket |
| IPAM | Blocks, assignments, free-pool view, customer-editable rDNS |
| Health | PSU, fan, temperature and drive pre-fail, polled every 15 minutes |
| Portal | Servers, power, reinstall, rescue, jobs, bandwidth, SSH keys, console |
| Admin | Fleet map, lifecycle control, suspend, raw job log viewer, audit trail |
| API | Customer tokens, same authorisation path as the portal |

Not built, on purpose: billing, custom ISO upload, BIOS tuning, advanced RAID
options, vKVM proxying. Wait until someone asks.

---

## Architecture

Three network planes, and the separation is the security model:

- **Control plane** — API, workers, Postgres, Redis. The only thing customers reach.
- **OOB plane** — an isolated VLAN carrying CIMC traffic only. Reachable *only*
  from job workers. No route to the internet, ever.
- **Customer plane** — switch ports, per-customer VLANs, public addresses.

The M4 is past Cisco's last day of support. There will never be another CIMC
firmware release, so the BMCs will never be patched again. Isolation is not a
best practice here; it is the entire defence. The API process has no route to
the OOB VLAN and no BMC credentials — only workers do.

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

### Redfish, not Cisco

`app/drivers/` speaks generic DMTF Redfish. Cisco-specific behaviour is
confined to clearly marked fallbacks. Swapping in M5s or Dell R640s later means
adding a driver, not a rewrite — and you should assume that happens, because
the M4s are a cheap way to learn operations, not a permanent fleet.

The driver assumes nothing works. It reads back every boot override it sets,
falls back when the BMC refuses a graceful reset type, tries both the action
and the PATCH form of virtual media insert, and redacts credentials before
anything reaches the job log.

---

## Repository layout

```
backend/
  app/
    drivers/         Redfish driver + the vendor-neutral interface
    services/        state machines, job engine, boot rendering, IPAM, bandwidth
    api/             HTTP surface: customer, admin, and the netboot rail
    workers/         Celery tasks
  scripts/
    bench_validate.py   the five pre-build hardware tests
    init_db.py          schema + seed
  tests/
installer/
  templates/         iPXE scripts, provision.sh, OS answer files (Jinja2)
  build-ramdisk.sh   builds the installer image
frontend/            React portal
```

---

## Before you write any more code: validate the hardware

Section 2 of the spec is not optional. Run this against one server on the
bench, before racking anything:

```sh
cd backend
python -m scripts.bench_validate --host 10.0.0.10 --user admin \
    --iso-url http://10.10.0.5:8080/iso/ubuntu-22.04.iso \
    --power-cycle
```

It checks firmware and Redfish reachability, one-time PXE override, vMedia ISO
boot, SOL and vKVM, and walks you through driving StorCLI non-interactively.
Whichever of PXE and vMedia proves more reliable becomes your primary install
path. Test 5 is the awkward one — budget time for it.

Standardise the whole fleet on CIMC **4.1(2f)**, the final M4 release, via HUU
**on the bench, before racking**. A mixed-firmware fleet makes every later
failure ambiguous.

---

## Running it

```sh
cp .env.example .env       # then edit DOZ_JWT_SECRET and the CIMC credentials
docker compose up -d postgres redis
cd backend && python -m scripts.init_db --admin-email you@example.com
uvicorn app.main:app --reload
celery -A app.workers.celery_app.celery_app worker -Q provision,power,poll
cd ../frontend && npm install && npm run dev
```

Portal at http://localhost:5173, API docs at http://localhost:8000/docs.

Or the whole stack: `docker compose up --build`.

### Tests

```sh
cd backend && python -m pytest
```

They run against a real PostgreSQL (JSONB, INET, partial unique indexes), so
they test what actually ships. The Redfish driver is tested against a simulated
CIMC including its known misbehaviours — a boot override that reports success
without taking effect, reset types the BMC will not accept, and virtual media
missing its InsertMedia action.

---

## Configuration

All of it is environment-driven; see `.env.example`. The ones that matter:

- `DOZ_JWT_SECRET` — also derives the per-MAC boot signature, so rotating it
  invalidates in-flight provisioning jobs. Rotate between installs.
- `DOZ_CONTROL_PLANE_URL` — where the ramdisk reaches the API. Must resolve
  from the provisioning VLAN.
- `DOZ_SECRETS_BACKEND` — `env` for the bench, `vault` in production. CIMC
  passwords never enter Postgres; `servers.cimc_credential_ref` is a pointer.
- `DOZ_REQUIRE_WIPE_BEFORE_STOCK` — leave it on. It is what stops a server
  going back on sale with the last customer's data still on it.

---

## Build order

Following section 11 of the spec, with the current state marked:

1. ✅ Bench firmware standardisation + the five validation tests (`bench_validate.py`)
2. ✅ Data model, inventory, CIMC credential storage
3. ✅ Job queue and state machine with audit logging
4. ✅ Power control — the simplest end-to-end proof of the architecture
5. ✅ iPXE + installer ramdisk + OS templates
6. ✅ Rescue mode and disk wipe
7. ✅ Serial console
8. ⚠️ Bandwidth — storage, API and graphs are built; the switch poller is a
   stub, because it depends on a switch model that has not been chosen
9. ✅ Customer UI
10. ⚠️ Admin UI and IPAM are built; health *alerting* stores status but does not
    yet notify anyone
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
   switch-port shutdown is not yet automated — it records intent and the port
   must currently be downed by hand. Wire that up before selling anything.

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
