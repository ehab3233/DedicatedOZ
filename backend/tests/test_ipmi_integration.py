"""IPMI power and Serial-over-LAN against a real IPMI stack.

These start OpenIPMI's `ipmi_sim` (deploy/sim/) and talk to it with the real
ipmitool, so they exercise the actual RMCP+ protocol, the actual ipmitool
output, and -- for the console -- a real pty bridge through a live API server.
They are skipped where `ipmi_sim` is not installed (`apt install openipmi`).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest
import requests

from app.drivers.base import BMCError
from app.drivers.fallback import FallbackDriver, protocol_label
from app.drivers.ipmi import IpmiDriver
from app.enums import JobState, JobType, PowerAction, ServerState
from app.secrets import BMCCredential

pytestmark = pytest.mark.skipif(
    shutil.which("ipmi_sim") is None or shutil.which("ipmitool") is None,
    reason="needs ipmi_sim and ipmitool (apt install openipmi ipmitool)",
)

RUN_SIM = Path(__file__).resolve().parents[2] / "deploy" / "sim" / "run-sim.sh"
PASSWORD = "bench-password"  # matches DOZ_CIMC_DEFAULT_PASS in conftest


def _free_port(kind: int = socket.SOCK_STREAM) -> int:
    with socket.socket(socket.AF_INET, kind) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def sim(tmp_path_factory):
    state = tmp_path_factory.mktemp("sim")
    port = _free_port(socket.SOCK_DGRAM)
    env = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin"}
    started = subprocess.run(
        [str(RUN_SIM), "start", "--state", str(state), "--port", str(port),
         "--serial-port", str(_free_port()), "--password", PASSWORD],
        env=env, capture_output=True, text=True, timeout=60,
    )
    assert started.returncode == 0, started.stderr
    yield {"port": port, "state": state}
    subprocess.run([str(RUN_SIM), "stop", "--state", str(state)], env=env,
                   capture_output=True, timeout=30)


def sim_power(sim) -> str:
    return (sim["state"] / "power").read_text().strip()


def driver(sim, **kw) -> IpmiDriver:
    return IpmiDriver("127.0.0.1", BMCCredential("admin", PASSWORD), port=sim["port"], **kw)


class TestPower:
    def test_every_action_changes_real_state(self, sim):
        d = driver(sim)
        d.power(PowerAction.FORCE_OFF)
        assert d.power_status().state == "off" and sim_power(sim) == "0"
        d.power(PowerAction.ON)
        assert d.power_status().state == "on" and sim_power(sim) == "1"
        before = (sim["state"] / "booted").read_text()
        time.sleep(1.1)
        d.power(PowerAction.FORCE_RESTART)
        assert (sim["state"] / "booted").read_text() != before
        d.power_cycle(settle_seconds=0)
        assert d.power_status().state == "on"

    def test_graceful_shutdown_is_acpi_not_a_power_cut(self, sim):
        d = driver(sim)
        d.power(PowerAction.ON)
        d.power(PowerAction.OFF)
        # The simulated OS takes a few seconds to go down, like a real one.
        assert d.wait_for_power_state("off", timeout=15, interval=1)
        assert "shutdown" in (sim["state"] / "chassis.log").read_text()

    def test_there_is_no_graceful_restart_over_ipmi(self, sim):
        with pytest.raises(BMCError, match="not available over IPMI"):
            driver(sim).power(PowerAction.RESTART)

    def test_calls_are_fast_with_the_pinned_cipher_suite(self, sim):
        """Without -C 3, ipmitool 1.8.19 spends ~10s probing on every call."""
        d = driver(sim)
        started = time.monotonic()
        for _ in range(5):
            d.power_status()
        assert time.monotonic() - started < 3

    def test_wrong_password_is_explained(self, sim):
        bad = IpmiDriver("127.0.0.1", BMCCredential("admin", "wrong"), port=sim["port"],
                         retransmit=(1, 1), timeout=15)
        with pytest.raises(BMCError, match="username or password is wrong"):
            bad.power_status()


class TestBootDevice:
    def test_pxe_is_set_and_read_back(self, sim):
        d = driver(sim)
        d.set_boot_once("pxe")
        assert (sim["state"] / "boot").read_text().strip() == "pxe"
        d.clear_boot_override()
        assert (sim["state"] / "boot").read_text().strip() == "none"


class TestSol:
    def test_sol_info_and_enable(self, sim):
        d = driver(sim)
        assert d.sol_enable().get("Enabled") == "true"

    def test_deactivate_is_harmless_when_idle(self, sim):
        driver(sim).sol_deactivate()


class TestFallback:
    def test_ipmi_serves_power_when_it_works(self, sim):
        broken_redfish = _Unreachable()
        fb = FallbackDriver(driver(sim), broken_redfish, rich=broken_redfish)
        assert fb.power_status().state in {"on", "off"}
        assert protocol_label(fb) == "ipmi"

    def test_dead_ipmi_falls_back_and_sticks(self, sim):
        dead = IpmiDriver("127.0.0.1", BMCCredential("admin", PASSWORD), port=_free_port(),
                          retransmit=(1, 1), timeout=10)
        other = driver(sim)  # stands in for Redfish: a second working path
        fb = FallbackDriver(dead, other, rich=other)
        started = time.monotonic()
        fb.power_status()
        first = time.monotonic() - started
        started = time.monotonic()
        fb.power_status()
        second = time.monotonic() - started
        assert fb._stick_to_second
        assert second < first  # no second attempt on the dead path


class _Unreachable:
    def power_status(self):
        raise BMCError("unreachable")

    def close(self):
        pass


# ---------------------------------------------------------------------------
# Through the job engine and the live API
# ---------------------------------------------------------------------------


@pytest.fixture
def sim_server(db, sim, make_server):
    return make_server(
        cimc_ip="127.0.0.1", ipmi_port=sim["port"], bmc_protocol="ipmi",
        state=ServerState.ACTIVE,
    )


class TestPowerJobs:
    @pytest.mark.parametrize(
        "job_type,expect",
        [
            (JobType.POWER_FORCE_OFF, "0"),
            (JobType.POWER_ON, "1"),
            (JobType.POWER_RESET, "1"),
            (JobType.POWER_CYCLE, "1"),
        ],
    )
    def test_job_drives_the_bmc(self, db, sim, sim_server, job_type, expect):
        from app.services import jobs as job_service
        from app.workers.tasks import power_task

        job, _ = job_service.create_job(db, job_type=job_type, server_id=sim_server.id)
        db.commit()
        power_task(str(job.id))  # the task body, run inline

        db.expire_all()
        job = db.get(type(job), job.id)
        assert job.state == JobState.SUCCEEDED, job.error
        assert job.result["via"] == "ipmi"
        assert sim_power(sim) == expect

    def test_reset_of_a_powered_off_server_powers_it_on(self, db, sim, sim_server):
        from app.services import jobs as job_service
        from app.workers.tasks import power_task

        driver(sim).power(PowerAction.FORCE_OFF)
        job, _ = job_service.create_job(db, job_type=JobType.POWER_RESET, server_id=sim_server.id)
        db.commit()
        power_task(str(job.id))
        assert sim_power(sim) == "1"


@pytest.fixture
def live_api():
    """The real app on a real port, so websockets and ptys behave as deployed."""
    import uvicorn

    from app.main import app

    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        try:
            requests.get(f"http://127.0.0.1:{port}/health", timeout=0.5)
            break
        except requests.RequestException:
            time.sleep(0.1)
    yield f"127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=10)


class TestConsoleBridge:
    def _login(self, base: str, customer) -> dict:
        token = requests.post(
            f"http://{base}/api/v1/auth/login",
            json={"email": customer.email, "password": "correct-horse-battery-staple"},
        ).json()["access_token"]
        return {"Authorization": f"Bearer {token}"}

    async def _session(self, base, server_id, headers, *, force=False, send=(), wait=3.0,
                       after_connect=None):
        import websockets

        ticket = requests.post(
            f"http://{base}/api/v1/console/{server_id}/ticket", headers=headers
        ).json()["ticket"]
        url = f"ws://{base}/api/v1/console/{server_id}/sol?ticket={ticket}"
        if force:
            url += "&force=1"
        output, states = b"", []
        connected = asyncio.Event()
        async with websockets.connect(url) as ws:
            async def read():
                nonlocal output
                async for message in ws:
                    if isinstance(message, bytes):
                        output += message
                    else:
                        states.append(json.loads(message)["state"])
                        if states[-1] in {"connected", "busy"}:
                            connected.set()

            reader = asyncio.create_task(read())
            # Only act once the SOL session is live: serial output produced
            # before then goes nowhere, exactly as on real hardware.
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(connected.wait(), timeout=10)
            if after_connect:
                await asyncio.to_thread(after_connect)
                await asyncio.sleep(1.5)
            for chunk in send:
                await ws.send(chunk)
                await asyncio.sleep(0.4)
            await asyncio.sleep(wait)
            reader.cancel()
        return output.decode(errors="replace"), states

    def test_boot_output_and_typing_round_trip(
        self, db, sim, sim_server, make_customer, make_subscription, live_api
    ):
        customer = make_customer("console@example.com")
        make_subscription(customer, sim_server)
        headers = self._login(live_api, customer)
        driver(sim).power(PowerAction.ON)

        text, states = asyncio.run(
            self._session(
                live_api, sim_server.id, headers,
                send=[b"\r", b"uname -a\r", b"reboot\r"], wait=2,
            )
        )
        assert "connected" in states
        assert "Linux web01" in text            # keystrokes reached the host and back
        assert "UCSC-C220-M4S" in text          # a full POST screen streamed through
        assert "\x1b[1;37;44m" in text          # ANSI colour arrives byte-for-byte
        assert "\x1b[2J\x1b[H" in text          # and cursor addressing, for BIOS screens

    def test_second_viewer_is_told_busy_then_can_take_over(
        self, db, sim, sim_server, make_customer, make_subscription, live_api
    ):
        customer = make_customer("busy@example.com")
        make_subscription(customer, sim_server)
        headers = self._login(live_api, customer)

        async def scenario():
            holder = asyncio.create_task(
                self._session(live_api, sim_server.id, headers, wait=6)
            )
            await asyncio.sleep(2)
            _, second = await self._session(live_api, sim_server.id, headers, wait=1)
            _, forced = await self._session(live_api, sim_server.id, headers, force=True, wait=1)
            await holder
            return second, forced

        second, forced = asyncio.run(scenario())
        assert "busy" in second
        assert "connected" in forced

    def test_tickets_are_single_use_and_server_bound(
        self, db, sim, sim_server, make_customer, make_subscription, make_server, live_api
    ):
        import websockets

        customer = make_customer("ticket@example.com")
        make_subscription(customer, sim_server)
        other = make_server()
        headers = self._login(live_api, customer)
        ticket = requests.post(
            f"http://{live_api}/api/v1/console/{sim_server.id}/ticket", headers=headers
        ).json()["ticket"]

        async def use(server_id):
            url = f"ws://{live_api}/api/v1/console/{server_id}/sol?ticket={ticket}"
            try:
                async with websockets.connect(url) as ws:
                    await asyncio.wait_for(ws.recv(), timeout=5)
                    return "accepted"
            except (websockets.exceptions.InvalidStatus,
                    websockets.exceptions.ConnectionClosed):
                return "refused"

        assert asyncio.run(use(other.id)) == "refused"      # wrong server
        assert asyncio.run(use(sim_server.id)) == "accepted"
        assert asyncio.run(use(sim_server.id)) == "refused"  # replay


# ---------------------------------------------------------------------------
# Sensors, event log, chassis and BMC administration against the simulator
# ---------------------------------------------------------------------------


def _sim_events(sim) -> None:
    subprocess.run(
        [str(RUN_SIM), "events", "--state", str(sim["state"]), "--port", str(sim["port"]),
         "--password", PASSWORD],
        env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin"}, check=True, capture_output=True,
        timeout=60,
    )


class TestSensorsAndEvents:
    def test_sensors_come_back_with_values_units_and_state(self, sim):
        sensors = {s["name"]: s for s in driver(sim).sensors()}
        assert len(sensors) == 12
        cpu = sensors["CPU1 Temp"]
        assert cpu["value"] == 47.0 and cpu["unit"] == "°C" and cpu["kind"] == "temperature"
        assert cpu["status"] == "ok" and cpu["number"] == 0x30
        assert sensors["FAN1 Tach"]["value"] == 4200.0 and sensors["FAN1 Tach"]["unit"] == "RPM"
        assert sensors["P12V"]["value"] == 12.1 and sensors["P12V"]["kind"] == "voltage"
        assert sensors["PSU1 Power"]["unit"] == "W" and sensors["PSU1 Power"]["kind"] == "power"

    def test_no_dcmi_means_no_power_reading_not_a_crash(self, sim):
        assert driver(sim).power_reading() is None

    def test_event_log_lists_and_clears(self, sim):
        d = driver(sim)
        d.sel_clear()
        _sim_events(sim)
        entries = d.sel_entries()
        assert len(entries) == 3
        assert entries[0]["sensor"] == "Temperature Inlet Temp"
        assert entries[0]["severity"] == "warning"
        assert entries[1]["sensor"] == "Fan FAN1 Tach" and entries[1]["severity"] == "critical"
        assert entries[2]["direction"] == "Deasserted" and entries[2]["severity"] == "info"
        assert d.sel_info()["entries"] == 3
        d.sel_clear()
        assert d.sel_entries() == [] and d.sel_info()["entries"] == 0


class TestBmcAdministration:
    def test_chassis_lan_and_controller_info(self, sim):
        d = driver(sim)
        chassis = d.chassis_status()
        assert chassis["system_power"] in {"on", "off"}
        assert chassis["power_restore_policy"] == "always-off"
        assert d.lan_config()["mac_address"]
        mc = d.mc_info()
        assert mc["ipmi_version"] == "2.0" and mc["firmware"] and mc["available"]

    def test_identify_led(self, sim):
        d = driver(sim)
        d.identify(5)
        d.identify(0)

    def test_unsupported_power_policy_is_reported_not_faked(self, sim):
        with pytest.raises(BMCError, match="Invalid command"):
            driver(sim).set_power_restore_policy("previous")

    def test_password_rotation_round_trip(self, sim):
        """Change the admin password over IPMI, prove the new one works and the
        old one does not, then put it back for the rest of the module."""
        d = driver(sim)
        users = {u["name"]: u for u in d.users()}
        assert users["admin"]["privilege"] == "ADMINISTRATOR"
        user_id = d.user_id_for("admin")
        assert user_id == 2

        new_password = "Rot4ted-Passw0rd"
        d.set_user_password(user_id, new_password)
        rotated = IpmiDriver("127.0.0.1", BMCCredential("admin", new_password),
                             port=sim["port"], retransmit=(1, 1), timeout=15)
        try:
            assert rotated.power_status().state in {"on", "off"}
            with pytest.raises(BMCError):
                IpmiDriver("127.0.0.1", BMCCredential("admin", PASSWORD), port=sim["port"],
                           retransmit=(1, 1), timeout=15).power_status()
        finally:
            rotated.set_user_password(user_id, PASSWORD)
        assert driver(sim).power_status().state in {"on", "off"}

    def test_password_length_is_checked_before_touching_the_bmc(self, sim):
        with pytest.raises(ValueError, match="1 to 16"):
            driver(sim).set_user_password(2, "x" * 17)


class TestBootOverrideJobs:
    def test_pxe_then_reset_reboots_into_the_network(self, db, sim, sim_server):
        from app.services import jobs as job_service
        from app.workers.tasks import boot_override_task

        driver(sim).power(PowerAction.ON)
        before = (sim["state"] / "booted").read_text()
        time.sleep(1.1)
        job, _ = job_service.create_job(db, job_type=JobType.BOOT_OVERRIDE, server_id=sim_server.id,
                                        payload={"device": "pxe", "then": "reset"})
        db.commit()
        boot_override_task(str(job.id))

        db.expire_all()
        job = db.get(type(job), job.id)
        assert job.state == JobState.SUCCEEDED, job.error
        assert job.result == {"device": "pxe", "then": "reset", "via": "ipmi",
                              "power_state": "on", "reached_target": True}
        assert (sim["state"] / "boot").read_text().strip() == "pxe"
        assert (sim["state"] / "booted").read_text() != before

    def test_bios_setup_flag_only(self, db, sim, sim_server):
        from app.services import jobs as job_service
        from app.workers.tasks import boot_override_task

        job, _ = job_service.create_job(db, job_type=JobType.BOOT_OVERRIDE, server_id=sim_server.id,
                                        payload={"device": "bios", "then": "none"})
        db.commit()
        boot_override_task(str(job.id))
        db.expire_all()
        job = db.get(type(job), job.id)
        assert job.state == JobState.SUCCEEDED, job.error
        assert (sim["state"] / "boot").read_text().strip() == "bios"
        driver(sim).clear_boot_override()


class TestLiveAdminApi:
    """The endpoints the panel polls, end to end: API -> ipmitool -> simulator."""

    @pytest.fixture
    def admin_headers(self, client, make_customer, auth_header):
        return auth_header(make_customer("admin@example.com", admin=True))

    def test_connection_test_against_the_simulator(self, client, sim_server, admin_headers):
        body = client.post(f"/api/v1/admin/servers/{sim_server.id}/bmc/test",
                           headers=admin_headers).json()
        by = {c["name"]: c for c in body["checks"]}
        assert body["ok"] is True and body["working_cipher"] == "3"
        assert body["cipher_saved"] is False
        assert by["ipmi:3"]["ok"] and "Chassis Power is" in by["ipmi:3"]["summary"]
        # Nothing listens on HTTPS here, and the report says so rather than guessing.
        assert by["redfish"]["ok"] is False and by["xml_api"]["ok"] is False
        assert "IPMI works with cipher suite 3" in body["verdict"]

    def test_sensors_endpoint(self, client, sim_server, admin_headers):
        from app.services import bmc_status

        bmc_status._live.clear()
        body = client.get(f"/api/v1/admin/servers/{sim_server.id}/sensors",
                          headers=admin_headers).json()
        assert body["via"] == "ipmi" and len(body["sensors"]) == 12 and body["power"] is None
        assert {s["name"] for s in body["sensors"] if s["kind"] == "temperature"} == {
            "CPU1 Temp", "CPU2 Temp", "Inlet Temp", "DIMM Temp"}

    def test_event_log_endpoint(self, client, sim, sim_server, admin_headers):
        from app.services import bmc_status

        driver(sim).sel_clear()
        _sim_events(sim)
        bmc_status._live.clear()
        body = client.get(f"/api/v1/admin/servers/{sim_server.id}/sel",
                          headers=admin_headers).json()
        assert body["info"]["entries"] == 3 and body["entries"][0]["direction"] == "Deasserted"
        assert client.delete(f"/api/v1/admin/servers/{sim_server.id}/sel",
                             headers=admin_headers).json()["entries_removed"] == 3

    def test_bmc_info_and_identify_endpoints(self, client, sim_server, admin_headers):
        from app.services import bmc_status

        bmc_status._live.clear()
        info = client.get(f"/api/v1/admin/servers/{sim_server.id}/bmc/info",
                          headers=admin_headers).json()
        assert info["mc"]["ipmi_version"] == "2.0"
        assert [u["name"] for u in info["users"]] == ["admin"]
        led = client.post(f"/api/v1/admin/servers/{sim_server.id}/identify", json={"seconds": 3},
                          headers=admin_headers)
        assert led.status_code == 200 and led.json() == {"led": "3s"}

    def test_fleet_power_endpoint(self, client, sim_server, admin_headers):
        from app.services import bmc_status

        bmc_status.forget(sim_server.id)
        body = client.get("/api/v1/admin/power", headers=admin_headers).json()
        mine = body["servers"][str(sim_server.id)]
        assert mine["state"] in {"on", "off"} and mine["via"] == "ipmi"

    def test_password_rotation_endpoint_with_a_writable_backend(
        self, client, sim, sim_server, admin_headers, tmp_path, monkeypatch
    ):
        from app import secrets as secrets_module
        from app.api import admin_bmc
        from app.services import bmc_status

        # Switch to the file backend for this test so the platform can store
        # the new password, seeding it with the current one.
        monkeypatch.setattr(secrets_module.settings, "secrets_backend", "file")
        monkeypatch.setattr(secrets_module.settings, "secrets_file_dir", str(tmp_path))
        secrets_module.get_secrets_backend.cache_clear()
        monkeypatch.setattr(admin_bmc.settings, "secrets_backend", "file")
        backend = secrets_module.get_secrets_backend()
        backend.put_bmc_credential(sim_server.cimc_credential_ref,
                                   BMCCredential("admin", PASSWORD))
        try:
            body = client.post(f"/api/v1/admin/servers/{sim_server.id}/bmc/password", json={},
                               headers=admin_headers).json()
            assert body["verified"] and body["stored"] and body["error"] is None, body
            stored = backend.get_bmc_credential(sim_server.cimc_credential_ref)
            assert stored.password == body["password"] != PASSWORD
            # The platform now authenticates with the stored password.
            bmc_status.forget(sim_server.id)
            power = client.get(f"/api/v1/servers/{sim_server.id}/power?fresh=1",
                               headers=admin_headers).json()
            assert power["state"] in {"on", "off"} and power["via"] == "ipmi"
        finally:
            IpmiDriver("127.0.0.1", BMCCredential("admin", body["password"]),
                       port=sim["port"]).set_user_password(2, PASSWORD)
            secrets_module.get_secrets_backend.cache_clear()
