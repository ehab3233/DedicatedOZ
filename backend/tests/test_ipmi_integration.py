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
