# Will it work? A review of the first build

An honest pass over the code before anyone racks hardware. Ordered by how much
each item would have hurt. Everything in the first two sections has been fixed
in the repository; the third section is what can only be settled on the bench.

## Would have broken on real hardware — fixed

**Basic auth on every Redfish call.** The driver authenticated each request
with HTTP basic auth. CIMC 4.x opens an internal session per basic-auth
request and caps them at four. A health sweep doing a dozen GETs per server
would have wedged every BMC in the rack with "max session limit reached"
within the first fifteen-minute cycle, and the sessions take minutes to time
out. The driver now logs in once per job with a Redfish session token and
deletes the session on close. Old firmware without a working SessionService
falls back to basic auth.

**Ubuntu autoinstall could never have completed.** Two separate problems:
cloud-init's NoCloud datasource is given a directory URL and appends
`user-data` and `meta-data` itself, so the single answer-file URL would have
404'd; and the casper netboot kernel needs `url=` pointing at the live ISO or
it boots to a prompt and waits forever. Both fixed — there is now a
`/boot/nocloud/{mac}/{sig}/` seed and the template's kernel arguments carry
the ISO URL. Subiquity also rejects `"!"` as an identity password, so a hash
of a random value is used and the account locked afterwards.

**`sleep infinity` in the ramdisk.** BusyBox `sleep` does not accept
`infinity`. Every rescue boot and every failed install would have exited the
provisioning script — and the exit trap would have reported failure. Replaced
with a loop.

**Rescue SSH would have refused root.** Alpine ships root locked (`!` in
`/etc/shadow`) and its sshd, having no PAM, honours that even for key auth.
The shadow entry is now set to `*` before sshd starts.

**INET columns crashed the customer server page.** psycopg returns
PostgreSQL `inet` values as `ipaddress` objects; the response schemas declared
plain strings. Any server with an address assigned would have 500'd on
`GET /servers/{id}`. The schemas now coerce.

**Admin reinstalls installed the admin's keys.** An admin reinstalling a
customer's server got the admin's SSH keys, not the customer's. Keys now come
from the subscription holder.

## Would have bitten on a flat network — fixed

**`X-Forwarded-For` was trusted unconditionally.** With the API reachable
directly, any host could forge its source address and defeat boot-script
client pinning. Now opt-in (`DOZ_TRUST_PROXY_HEADERS`), set true only by the
installer that puts nginx in front.

**Client pinning vs. DHCP client-ids.** iPXE, the ramdisk's udhcpc, and the
OS installer's DHCP client each present a different client-id. A DHCP server
that keys leases on client-id can hand the same machine three addresses during
one install, and the platform would have refused the OS installer's fetch of
its own answer file. The generated dnsmasq config sets `dhcp-ignore-clid`; in
proxy-DHCP mode, where we do not control the lease server, pinning is turned
off (`DOZ_BOOT_PIN_CLIENT_IP=false`).

**DHCP variable expansion.** The DHCP boot filename used to carry
`${net0/mac}` and rely on the DHCP server passing it through untouched. The
entry point now returns a one-line `chain` script and iPXE expands the MAC
itself.

**A broker outage 500'd requests** whose job row was already committed.
Fixed in the first build; noting it here because the same class of bug —
trusting the delivery mechanism over the database — is the one to watch for.

## Cannot be verified without the hardware

These are why the bench test exists. Run it on one server before anything
else.

1. **Does the M4 honour `BootSourceOverrideEnabled=Once` + `Pxe` over
   Redfish?** The driver reads the override back and refuses to continue if
   it did not stick, so a silent failure becomes a loud one — but whether it
   works at all on your firmware build is unknown until you try. If it does
   not, vMedia becomes the install path and the ramdisk is booted as an ISO.

2. **Does virtual media insert over plain HTTP from the CIMC?** Known to be
   fussy: no redirects, no HTTPS with an unknown CA, and older builds lack
   the `InsertMedia` action (the driver tries the PATCH form too).

3. **Does StorCLI drive the Cisco 12G SAS controller non-interactively, and
   does the controller support JBOD?** Without JBOD, secure erase cannot
   reach individual drives and wipe falls back to a full overwrite — which
   changes deprovisioning from minutes to hours per server.

4. **Does the Alpine ramdisk see the NICs and the RAID controller?** `igb`
   and `megaraid_sas` are in `linux-lts`, and udev coldplug should load them,
   but "should" is the operative word until one boots.

5. **Does `kexec` into the distribution installer work on this BIOS?** It
   nearly always does. When it does not, the fallback is to skip kexec and
   chainload the distro kernel directly from iPXE, with the ramdisk stage
   done as a separate rescue-style job first.

6. **Serial console.** Needs BIOS console redirection on COM0 at 115200, and
   IPMI over LAN enabled in the CIMC. Both are BIOS/CIMC settings, not
   software.

## Known limits that are design choices, not bugs

- **One worker slot per install for its whole duration.** The worker waits on
  the installer callback for up to 45 minutes. Four concurrent installs per
  worker process. Raise `--concurrency` or run more workers as the fleet grows;
  a proper fix would move the wait into a scheduled poll.
- **Suspend does not touch the switch.** Documented in three places. Needs
  a switch model.
- **No bandwidth poller.** Same reason.
- **Health is stored, not alerted.** No email, no pager.
- **Windows unattend is a template slot, not a template.** Nobody asked yet.

## What was tested

- 119 tests against a real PostgreSQL, including a simulated CIMC exercising
  session auth, boot-override read-back, reset-type fallbacks, both virtual
  media insert forms, credential redaction, and health rollup.
- Every rendered provisioning script (all six RAID levels) syntax-checked with
  `dash -n`; the Ubuntu answer file parsed as YAML.
- A real Celery worker driving a power job against an unreachable BMC:
  correct retries, correct failure, lifecycle untouched, next job accepted.
- The full boot rail, end to end, with a simulated ramdisk: entry script,
  provision script, progress callbacks, answer file, completion.
