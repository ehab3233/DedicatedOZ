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

## Second pass: power, consoles, and running as a service — fixed

This round tested against real protocol implementations instead of mocks:
OpenIPMI's `ipmi_sim` (a genuine IPMI-over-LAN / RMCP+ stack, driven by the
real ipmitool 1.8.19), OpenStack's `sushy-emulator` (a Redfish service), a
live API with real Celery workers, a browser, and the installer on a fresh
Ubuntu 24.04 with systemd as PID 1. Every item below was found that way; none
of them showed up against the mocks.

**BMC HTTP traffic obeyed the environment.** `requests` lets
`REQUESTS_CA_BUNDLE` silently override `verify=False`, so every Redfish call
to the CIMC's self-signed certificate failed on any host with that variable
set, and any `HTTPS_PROXY` would have received the BMC traffic. BMC sessions
now ignore the environment entirely.

**Every ipmitool call could cost ten seconds.** ipmitool 1.8.19 probes for
the best cipher suite before each command; a BMC that does not answer the
probe costs ten seconds a call (measured: 10.09 s against 0.05 s). Beyond
slowness, that put installs at risk: IPMI boot flags expire sixty seconds
after they are set. Cipher suite 3 is now pinned.

**A dead BMC cost twenty seconds per attempt.** ipmitool's default
retransmits. Interactive reads now use one retransmit and fail in two seconds.

**Power went Redfish-first.** A power-state read over Redfish on a CIMC is a
login, one or two GETs and a logout, and each counts against the CIMC's small
session limit. Over IPMI it is ~60 ms. Power, boot device and live state now
go over IPMI first and fall back to Redfish; inventory and virtual media stay
on Redfish.

**Shut down reported success when it had not happened.** If the OS ignored
ACPI, the job still "succeeded". It now fails, saying the OS ignored the
request and pointing at Force off — which did not exist, and now does.
A reset of a powered-off server was a silent no-op; it now powers the server
on.

**Power could queue behind reinstalls.** One Celery pool served every queue,
and each reinstall holds a slot for up to 45 minutes; four reinstalls and the
power buttons stopped working. There is now a worker per queue.

**The serial console.**
- It authenticated with the session JWT in the websocket URL, which nginx
  writes to its access log. It now uses a one-minute, single-use ticket.
- It ran ipmitool on pipes; ipmitool wants a terminal. It now runs on a pty,
  which is also what BIOS screens with cursor addressing need.
- The "idle timeout" was a hard 15-minute cap regardless of activity. It is
  now a real idle timeout (30 min) plus an 8-hour ceiling.
- It held a pooled database connection for the whole session.
- The browser rendered raw text, so BIOS output was garbage. It is now
  xterm.js.
- A double-clicked Reconnect (or React's development double-mount) opened two
  SOL sessions: one held the BMC's only slot, the other got the keystrokes.

**Redfish details.** Virtual media was looked up at a hard-coded path;
newer schemas moved it, and it now follows the links the BMC publishes.
Clearing a boot override with `Enabled: Disabled` alone is rejected by
stricter implementations; the target is now sent too.

**The installer.** It needed `sudo` (absent from minimal images); installed
the full `dnsmasq` package, whose own service fights systemd-resolved for
port 53 and can register itself as the host's resolver; bound dnsmasq with
`bind-interfaces`, which fails if the NIC comes up after it at boot; and
validated a dnsmasq config file that dnsmasq was never going to read. Now:
`runuser`, `dnsmasq-base` under our own `doz-pxe` unit, `bind-dynamic`, and
validation of the file actually used. The whole platform is one service,
`doz`, with every part `PartOf` it.

**Schema upgrades.** `create_all` never adds columns to an existing table, so
an install from an earlier version would have broken on the first new column.
An additive upgrade runs on every install and update.

## Third pass: IPMI management, the image store, and the panel — fixed

This round added what a dedicated-server company's panel needs beyond power
and consoles, and rebuilt the panel around it. Everything was verified
against the simulator, which now carries a sensor repository and an event
log, and in a browser driving the real API.

**A job failing with an HTTP client's exception crashed the failure
recorder.** `requests` exceptions carry `.request` and `.response` objects
under the same attribute names a driver's `BMCError` uses for its JSON-able
exchange. Recording one blew up on the JSONB column, so the job never
reached `failed`. Only dicts are recorded now.

**Job stage labels are 64 characters.** New stage texts were longer and the
database refused them. Stages are short labels again, with detail in the
log, and `set_stage` truncates rather than fails.

**A discrete sensor's state bits parsed as the number zero.** `sdr elist`
prints them as `0x0180`; the reading parser took the leading `0`. Hex
readings are now text.

**The README said the API held no BMC credentials.** It has since the
console; the live power state, sensors, event log and KVM tokens are read
by the API too. It now says so, and says what moving the API off the OOB
network would require.

**The simulator.** OpenIPMI's `ipmi_sim` drops sensor records added after a
sensor's event support is configured, and leaves every sensor's scanning
off on a clean start; both are worked around in the generated config and
the run script, so `sdr elist` returns readings from the first start. The
2.0.37 release that Ubuntu 26.04 ships only marks a sensor readable through
its file-polling path, so values set in the config never show there; the
sensors now poll their values from files in the state directory, which
works on both releases and doubles as a way to simulate a fault.

**Ubuntu 26.04.** The installer refused anything but 22.04 and 24.04. It now
accepts 26.04 and newer (older than 22.04 is still refused), and was run on
26.04 end to end: Python 3.14 with every dependency from a wheel,
PostgreSQL 18, Redis 8, the smoke test green. Where a release has dropped
Redis for Valkey, the installer uses that instead. A one-line installer,
`install.sh`, now fetches the code, installs, starts the simulator and
prints the login.

What was added, and how it was verified:

- Sensors (`sdr elist`), the SEL (`sel elist` / `sel info` / `sel clear`),
  chassis status, LAN config, `mc info`, the identify LED, BMC cold reset,
  the power-restore policy, the IPMI user table and `user set password`
  (typed into a pty, never on the command line): each against the
  simulator, including a password rotation that proves the new password
  with a fresh session, proves the old one is rejected, and rotates back.
- Boot-device override as a job (set, read back, then reset / cycle / on /
  nothing): against the simulator, which records the device it was told.
- Virtual media mount / boot / eject as jobs: against a recording driver;
  the IPMI-only case fails with the reason rather than pretending.
- The image store: upload (streamed, sanitised names, deduplicated),
  fetch-by-URL as a job with progress and a checksum, scan, delete: against
  a temporary directory and a mocked HTTP server, then in the browser.
- Bulk live power for the fleet page, read in parallel with a short cache.
- The panel: every page and tab in a browser against the simulator, light
  and dark, desktop and phone width, with no page errors and only the
  expected failures (no CIMC for the KVM, no power-policy command on the
  simulator, a read-only credential backend).

## Cannot be verified without the hardware

These are why the bench test exists. Run it on one server before anything
else.

1. **Does the M4 honour a one-time PXE boot override?** Now set over IPMI
   first (`chassis bootdev pxe`), Redfish second. Both paths read the
   override back and refuse to continue if it did not stick, so a silent
   failure becomes a loud one — but whether it works on your firmware build
   is unknown until you try. If neither does, vMedia becomes the install path.

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

6. **Serial console and the rest of the CIMC setup.** Needs IPMI over LAN
   and SOL enabled on the CIMC, and BIOS console redirection on COM0 at
   115200. **Prepare BMC** (run when a server is added) sets those, plus
   virtual media, KVM, Redfish, the LAN-port PXE option ROMs, the boot order
   and NTP, over the CIMC XML API using the object names Cisco's own SDK
   uses, then proves IPMI and SOL work. Each setting is read before it is
   written: on a real C220 M4, rewriting IPMI over LAN restarted the CIMC's
   IPMI service and the panel lost the server for a while after a re-run, so
   a re-run now writes nothing on a prepared CIMC. The XML API calls are
   tested against recorded request shapes, not a real CIMC: the first three
   settings have run on a real M4, the rest have not yet. If one is rejected,
   the job log shows the CIMC's error and lists it at the end; the web UI can
   do the same by hand.

7. **The HTML5 KVM viewer's URL.** The one-time token call
   (`aaaGetComputeAuthTokens`) is documented; the path of the HTML5 viewer
   has moved between CIMC releases. The panel probes the known paths and
   falls back to the Java launcher and the CIMC web UI, and
   `DOZ_KVM_URL_TEMPLATE` pins it once the bench test shows which one yours
   uses. One C220 M4 in the field answers the token call itself with "Method
   not supported" (CIMC error 2009); on that firmware the KVM button opens
   the CIMC web UI, where Launch KVM works after a login. A launch that skips
   the login needs that firmware's version and what its Launch KVM link does.

8. **What the CIMC's sensor list looks like.** The parser handles ipmitool's
   formats (numeric readings with units, discrete state text, hex state
   bits, `ns` for no reading); sensor names and which ones exist are the
   firmware's. DCMI power reading is optional in the spec and shown only if
   the BMC answers it.

9. **`user set password` on a CIMC with strong-password policy.** Generated
   passwords have upper, lower, digit and a symbol and are 16 characters,
   which is the IPMI 1.5 length every BMC accepts. If the CIMC rejects one,
   the job says so before anything changes; if it accepts the change but a
   fresh session then fails, the new password is still shown once so nobody
   is locked out.

10. **The power-restore policy command.** Standard IPMI, and the simulator
    does not implement it (the panel reports that honestly). CIMC does.

11. **Virtual media boot from the image store.** The same Redfish path as
    item 2, now driven from the panel: the CIMC fetches the ISO over plain
    HTTP from the management server's boot-asset port, which the flat
    network makes reachable.

## Known limits that are design choices, not bugs

- **One provisioning worker slot per install for its whole duration.** The
  worker waits on the installer callback for up to 45 minutes; eight
  concurrent installs by default. Power, console and polling have their own
  workers, so this no longer blocks anything else. A proper fix would move
  the wait into a scheduled poll.
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
