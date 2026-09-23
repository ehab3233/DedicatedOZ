"""IPMI management features that do not need a simulator.

Parsers against real ipmitool output, the admin endpoints against a stand-in
driver, the image store against a temporary directory, and the new job
bodies against a recording driver.
"""

from __future__ import annotations

import hashlib
import uuid
from contextlib import contextmanager

import pytest
import responses

from app.drivers.base import BMCError
from app.drivers.ipmi import parse_sdr_elist, parse_sel_elist, parse_user_list
from app.enums import JobState, JobType, PowerAction
from app.services import bmc_status

SDR = """\
CPU1 Temp        | 30h | ok  |  3.1 | 47 degrees C
Inlet Temp       | 32h | nc  |  7.1 | 51 degrees C
FAN1 Tach        | 40h | cr  | 29.1 | 0 RPM
P12V             | 50h | ok  |  7.1 | 12.10 Volts
PSU1 Power       | 60h | ok  | 10.1 | 185 Watts
PSU2 Status      | 70h | ok  | 10.2 | Presence detected
Chassis Intru    | 42h | ok  |  7.1 | 0x0180
DIMM Temp        | 33h | ns  |  8.1 | Disabled
"""

SEL = (
    "   1 | 09/23/2026 | 14:03:11 | Temperature Inlet Temp | Upper Non-critical going high"
    " | Asserted | Reading 51 > Threshold 45 degrees C\n"
    "   2 | 09/23/2026 | 14:03:40 | Fan FAN1 Tach | Lower Critical going low  | Asserted"
    " | Reading 0 < Threshold 1000 RPM\n"
) + """\
   3 |  Pre-Init  |0000005906| Fan FAN1 Tach | Lower Critical going low  | Deasserted
   a | 09/23/2026 | 14:05:00 | Power Supply PSU2 Status | Power Supply AC lost | Asserted
"""

USERS = """\
ID  Name\t     Callin  Link Auth\tIPMI Msg   Channel Priv Limit
1                    true    false      true       USER
2   admin            true    false      true       ADMINISTRATOR
3   remote hands     true    true       true       OPERATOR
4                    true    false      false      Unknown (0x00)
"""


class TestParsers:
    def test_sdr_readings_units_kinds_and_levels(self):
        by_name = {s["name"]: s for s in parse_sdr_elist(SDR)}
        assert by_name["CPU1 Temp"] == {
            "name": "CPU1 Temp", "number": 0x30, "status": "ok", "raw_status": "ok",
            "entity": "3.1", "reading": "47 degrees C", "value": 47.0, "unit": "°C",
            "kind": "temperature",
        }
        assert by_name["Inlet Temp"]["status"] == "warning"
        assert by_name["FAN1 Tach"]["status"] == "critical"
        assert by_name["FAN1 Tach"]["kind"] == "fan" and by_name["FAN1 Tach"]["unit"] == "RPM"
        assert by_name["P12V"]["value"] == 12.1 and by_name["P12V"]["unit"] == "V"
        assert by_name["PSU1 Power"]["kind"] == "power" and by_name["PSU1 Power"]["unit"] == "W"
        # Discrete: no number to plot, the text is the reading; grouped by name.
        psu = by_name["PSU2 Status"]
        assert psu["value"] is None and psu["kind"] == "power"
        assert psu["reading"] == "Presence detected"
        intrusion = by_name["Chassis Intru"]
        assert intrusion["value"] is None and intrusion["unit"] is None
        assert intrusion["kind"] == "discrete" and intrusion["reading"] == "0x0180"
        assert by_name["DIMM Temp"]["status"] == "no_reading"

    def test_sel_dates_severity_and_hex_ids(self):
        entries = parse_sel_elist(SEL)
        assert [e["id"] for e in entries] == [1, 2, 3, 10]
        assert entries[0]["timestamp"] == "2026-09-23T14:03:11"
        assert entries[0]["severity"] == "warning"
        assert entries[0]["detail"] == "Reading 51 > Threshold 45 degrees C"
        assert entries[1]["severity"] == "critical"
        # A BMC with no clock prints Pre-Init: kept as text, not a fake date.
        assert entries[2]["timestamp"] is None and "Pre-Init" in entries[2]["raw_time"]
        assert entries[2]["severity"] == "info"  # deasserted = recovered
        assert entries[3]["severity"] == "info"

    def test_empty_sel_is_empty(self):
        assert parse_sel_elist("SEL has no entries\n") == []

    def test_user_list_keeps_free_slots_and_spaced_names(self):
        users = parse_user_list(USERS)
        assert [u["id"] for u in users] == [1, 2, 3, 4]
        assert users[1] == {
            "id": 2, "name": "admin", "callin": True, "link_auth": False,
            "ipmi_messaging": True, "privilege": "ADMINISTRATOR",
        }
        assert users[2]["name"] == "remote hands" and users[2]["privilege"] == "OPERATOR"
        assert users[0]["name"] == "" and users[3]["ipmi_messaging"] is False


# ---------------------------------------------------------------------------
# Endpoints against a stand-in driver
# ---------------------------------------------------------------------------


class FakeIpmi:
    """Records every call; answers like a healthy BMC unless told to fail."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.fail: BMCError | None = None
        self.policy = "always-off"
        self._cred = type("C", (), {"username": "admin"})()

    def _hit(self, *call):
        self.calls.append(call)
        if self.fail:
            raise self.fail

    def sensors(self):
        self._hit("sensors")
        return parse_sdr_elist(SDR)

    def power_reading(self):
        self._hit("power_reading")
        return {"watts": 185, "minimum": 120, "maximum": 240, "average": 180}

    def sel_entries(self):
        self._hit("sel_entries")
        return parse_sel_elist(SEL)

    def sel_info(self):
        self._hit("sel_info")
        return {"entries": 4, "free_bytes": 15900, "percent_used": 1,
                "last_add": "09/23/2026 14:05:00", "last_clear": "Not Available",
                "overflow": False}

    def sel_clear(self):
        self._hit("sel_clear")

    def chassis_status(self):
        self._hit("chassis_status")
        return {"system_power": "on", "power_restore_policy": self.policy}

    def identify(self, seconds=15, *, force=False):
        self._hit("identify", seconds, force)

    def set_power_restore_policy(self, policy):
        self._hit("set_power_restore_policy", policy)
        self.policy = policy

    def mc_info(self):
        self._hit("mc_info")
        return {"firmware": "4.1(2f)", "ipmi_version": "2.0", "manufacturer": "Cisco",
                "product": "C220 M4", "device_id": "0", "available": True}

    def lan_config(self, channel=1):
        self._hit("lan_config", channel)
        return {"channel": 1, "ip_address": "10.0.0.101", "subnet_mask": "255.255.255.0",
                "gateway": "10.0.0.1", "mac_address": "00:11:22:33:44:55",
                "source": "Static Address", "vlan": "Disabled"}

    def users(self, channel=1):
        self._hit("users", channel)
        return parse_user_list(USERS)

    def user_id_for(self, username):
        self._hit("user_id_for", username)
        return 2 if username == "admin" else None

    def set_user_password(self, user_id, password):
        self._hit("set_user_password", user_id, password)

    def bmc_reset(self, kind="cold"):
        self._hit("bmc_reset", kind)

    def power_status(self):
        self._hit("power_status")
        return type("P", (), {"state": "on"})()


@pytest.fixture
def fake_ipmi(monkeypatch):
    fake = FakeIpmi()
    monkeypatch.setattr("app.api.admin_bmc._ipmi", lambda server, timeout=None: fake)
    bmc_status._live.clear()
    return fake


@pytest.fixture
def admin_headers(client, make_customer, auth_header):
    return auth_header(make_customer("admin@example.com", admin=True))


class TestLiveReadings:
    def test_sensors_are_returned_and_briefly_cached(self, client, make_server, admin_headers,
                                                     fake_ipmi):
        server = make_server()
        url = f"/api/v1/admin/servers/{server.id}/sensors"
        body = client.get(url, headers=admin_headers).json()
        assert body["via"] == "ipmi" and body["power"]["watts"] == 185
        assert {s["name"] for s in body["sensors"]} >= {"CPU1 Temp", "FAN1 Tach", "P12V"}
        client.get(url, headers=admin_headers)
        assert fake_ipmi.calls.count(("sensors",)) == 1  # second read served from cache
        client.get(url + "?fresh=1", headers=admin_headers)
        assert fake_ipmi.calls.count(("sensors",)) == 2

    def test_event_log_is_newest_first_and_clearing_is_audited(
        self, client, db, make_server, admin_headers, fake_ipmi
    ):
        from sqlalchemy import select

        from app.models import AuditLog

        server = make_server()
        body = client.get(f"/api/v1/admin/servers/{server.id}/sel", headers=admin_headers).json()
        assert [e["id"] for e in body["entries"]] == [10, 3, 2, 1]
        assert body["info"]["entries"] == 4

        cleared = client.delete(f"/api/v1/admin/servers/{server.id}/sel", headers=admin_headers)
        assert cleared.status_code == 200 and cleared.json()["entries_removed"] == 4
        assert ("sel_clear",) in fake_ipmi.calls
        audit = db.execute(
            select(AuditLog).where(AuditLog.action == "server.sel_cleared")
        ).scalar_one()
        assert audit.target_id == str(server.id) and audit.detail == {"entries": 4}

    def test_bmc_info_lists_only_named_users(self, client, make_server, admin_headers, fake_ipmi):
        server = make_server()
        body = client.get(f"/api/v1/admin/servers/{server.id}/bmc/info",
                          headers=admin_headers).json()
        assert body["mc"]["firmware"] == "4.1(2f)"
        assert body["lan"]["mac_address"] == "00:11:22:33:44:55"
        assert body["chassis"]["power_restore_policy"] == "always-off"
        assert [u["name"] for u in body["users"]] == ["admin", "remote hands"]

    def test_bmc_failures_are_502_with_the_reason(self, client, make_server, admin_headers,
                                                  fake_ipmi):
        fake_ipmi.fail = BMCError("ipmitool sdr elist: IPMI session failed")
        server = make_server()
        response = client.get(f"/api/v1/admin/servers/{server.id}/sensors", headers=admin_headers)
        assert response.status_code == 502
        assert "IPMI session failed" in response.json()["detail"]

    def test_customers_cannot_read_sensors(self, client, make_customer, make_server,
                                           make_subscription, auth_header, fake_ipmi):
        customer = make_customer()
        server = make_server()
        make_subscription(customer, server)
        response = client.get(f"/api/v1/admin/servers/{server.id}/sensors",
                              headers=auth_header(customer))
        assert response.status_code == 403


class TestBmcActions:
    def test_identify_blinks_for_a_while_and_can_be_switched_off(
        self, client, make_server, admin_headers, fake_ipmi
    ):
        server = make_server()
        url = f"/api/v1/admin/servers/{server.id}/identify"
        post = lambda body: client.post(url, json=body, headers=admin_headers).json()  # noqa: E731
        assert post({"seconds": 30}) == {"led": "30s"}
        assert post({"force": True}) == {"led": "on"}
        assert post({"seconds": 0}) == {"led": "off"}
        assert fake_ipmi.calls == [("identify", 30, False), ("identify", 255, True),
                                   ("identify", 0, False)]

    def test_power_restore_policy_is_validated_and_applied(self, client, make_server,
                                                           admin_headers, fake_ipmi):
        server = make_server()
        url = f"/api/v1/admin/servers/{server.id}/bmc/power-policy"
        bad = client.post(url, json={"policy": "sometimes"}, headers=admin_headers)
        assert bad.status_code == 422
        body = client.post(url, json={"policy": "previous"}, headers=admin_headers).json()
        assert body["policy"] == "previous"
        assert ("set_power_restore_policy", "previous") in fake_ipmi.calls

    def test_bmc_reset_is_audited(self, client, db, make_server, admin_headers, fake_ipmi):
        from sqlalchemy import select

        from app.models import AuditLog

        server = make_server()
        body = client.post(f"/api/v1/admin/servers/{server.id}/bmc/reset",
                           headers=admin_headers).json()
        assert body["reset"] == "cold"
        assert ("bmc_reset", "cold") in fake_ipmi.calls
        audit = db.execute(select(AuditLog).where(AuditLog.action == "server.bmc_reset"))
        assert audit.scalar_one()

    def test_password_rotation_refuses_when_it_could_not_store(self, client, make_server,
                                                               admin_headers, fake_ipmi):
        server = make_server()
        response = client.post(f"/api/v1/admin/servers/{server.id}/bmc/password", json={},
                               headers=admin_headers)
        assert response.status_code == 409
        assert "read-only" in response.json()["detail"]
        assert not any(c[0] == "set_user_password" for c in fake_ipmi.calls)

    def test_password_rotation_changes_verifies_stores_and_never_logs_it(
        self, client, db, make_server, admin_headers, fake_ipmi, monkeypatch
    ):
        from sqlalchemy import select

        from app.api import admin_bmc
        from app.models import AuditLog

        stored: dict = {}

        class Backend:
            def put_bmc_credential(self, ref, cred):
                stored[ref] = cred

        class VerifyDriver:
            def __init__(self, **kw):
                self.kw = kw

            def power_status(self):
                return type("P", (), {"state": "on"})()

        monkeypatch.setattr(admin_bmc.settings, "secrets_backend", "file")
        monkeypatch.setattr(admin_bmc, "get_secrets_backend", lambda: Backend())
        monkeypatch.setattr(admin_bmc, "IpmiDriver", VerifyDriver)
        server = make_server()

        body = client.post(f"/api/v1/admin/servers/{server.id}/bmc/password", json={},
                           headers=admin_headers).json()
        assert body["verified"] and body["stored"] and body["error"] is None
        assert body["username"] == "admin" and len(body["password"]) == 16
        assert ("set_user_password", 2, body["password"]) in fake_ipmi.calls
        assert stored[server.cimc_credential_ref].password == body["password"]
        audit = db.execute(
            select(AuditLog).where(AuditLog.action == "server.bmc_password_rotated")
        ).scalar_one()
        assert body["password"] not in str(audit.detail)
        assert audit.detail["stored"] is True

    def test_generated_passwords_satisfy_cimc_rules(self):
        from app.api.admin_bmc import generate_bmc_password

        for _ in range(20):
            pw = generate_bmc_password()
            assert len(pw) == 16
            assert any(c.islower() for c in pw) and any(c.isupper() for c in pw)
            assert any(c.isdigit() for c in pw) and any(c in "-_." for c in pw)


class TestBootAndVirtualMedia:
    def test_boot_override_queues_a_job_with_its_payload(self, client, make_server,
                                                         admin_headers, dispatched):
        server = make_server()
        url = f"/api/v1/admin/servers/{server.id}/boot"
        bad = client.post(url, json={"device": "floppy"}, headers=admin_headers)
        assert bad.status_code == 422
        response = client.post(url, json={"device": "bios", "then": "cycle"}, headers=admin_headers)
        assert response.status_code == 202
        job = response.json()
        assert job["type"] == JobType.BOOT_OVERRIDE.value and dispatched == [job["id"]]

    def test_boot_override_respects_the_server_lock(self, client, db, make_server, admin_headers):
        from app.services import jobs as job_service

        server = make_server()
        job_service.create_job(db, job_type=JobType.INSTALL, server_id=server.id)
        db.commit()
        response = client.post(f"/api/v1/admin/servers/{server.id}/boot", json={"device": "pxe"},
                               headers=admin_headers)
        assert response.status_code == 409

    def test_vmedia_boot_needs_a_ready_image_on_disk(self, client, db, make_server,
                                                     admin_headers, dispatched, image_dir):
        from app.models import Image

        server = make_server()
        url = f"/api/v1/admin/servers/{server.id}/vmedia/boot"
        missing = client.post(url, json={"image_id": str(uuid.uuid4())}, headers=admin_headers)
        assert missing.status_code == 404

        fetching = Image(name="dl", filename="dl.iso", status="fetching")
        ready = Image(name="Ubuntu", filename="ubuntu.iso", status="ready")
        db.add_all([fetching, ready])
        db.commit()
        assert client.post(url, json={"image_id": str(fetching.id)},
                           headers=admin_headers).status_code == 409
        # Ready in the catalogue but gone from disk: refuse rather than queue a job that fails.
        assert client.post(url, json={"image_id": str(ready.id)},
                           headers=admin_headers).status_code == 409
        (image_dir / "ubuntu.iso").write_bytes(b"iso")
        response = client.post(url, json={"image_id": str(ready.id), "boot": True},
                               headers=admin_headers)
        assert response.status_code == 202
        from app.models import Job

        job = db.get(Job, uuid.UUID(response.json()["id"]))
        assert job.payload["image_url"].endswith("/iso/ubuntu.iso") and job.payload["boot"] is True

    def test_vmedia_eject_queues(self, client, make_server, admin_headers, dispatched):
        server = make_server()
        response = client.post(f"/api/v1/admin/servers/{server.id}/vmedia/eject",
                               headers=admin_headers)
        assert response.status_code == 202
        assert response.json()["type"] == JobType.VMEDIA_EJECT.value

    def test_vmedia_status_says_so_when_the_protocol_cannot(self, client, make_server,
                                                            admin_headers):
        server = make_server(bmc_protocol="ipmi")
        body = client.get(f"/api/v1/admin/servers/{server.id}/vmedia", headers=admin_headers).json()
        assert body["supported"] is False and "Redfish" in body["error"]


# ---------------------------------------------------------------------------
# Image store
# ---------------------------------------------------------------------------


@pytest.fixture
def image_dir(tmp_path, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "image_dir", str(tmp_path / "iso"))
    return tmp_path / "iso"


class TestImageStore:
    def test_upload_streams_to_disk_and_catalogues(self, client, db, admin_headers, image_dir):
        payload = b"\x00ISO" * 100_000
        response = client.put(
            "/api/v1/admin/images/upload?filename=Ubuntu%2024.04.iso&name=Ubuntu%2024.04",
            content=payload, headers=admin_headers,
        )
        assert response.status_code == 201, response.text
        image = response.json()
        assert image["filename"] == "Ubuntu-24.04.iso" and image["name"] == "Ubuntu 24.04"
        assert image["size_bytes"] == len(payload)
        assert image["sha256"] == hashlib.sha256(payload).hexdigest()
        assert image["url"].endswith("/iso/Ubuntu-24.04.iso")
        assert (image_dir / "Ubuntu-24.04.iso").read_bytes() == payload
        assert not list(image_dir.glob("*.part"))

        listed = client.get("/api/v1/admin/images", headers=admin_headers).json()
        assert [i["id"] for i in listed] == [image["id"]]

    def test_upload_names_are_sanitised_and_deduplicated(self, client, admin_headers, image_dir):
        first = client.put("/api/v1/admin/images/upload?filename=../../etc/passwd",
                           content=b"x", headers=admin_headers).json()
        assert first["filename"] == "passwd.iso"
        second = client.put("/api/v1/admin/images/upload?filename=passwd.iso",
                            content=b"y", headers=admin_headers).json()
        assert second["filename"] == "passwd-2.iso"
        assert sorted(p.name for p in image_dir.iterdir()) == ["passwd-2.iso", "passwd.iso"]

    def test_empty_upload_is_rejected_and_leaves_nothing(self, client, admin_headers, image_dir):
        response = client.put("/api/v1/admin/images/upload?filename=empty.iso", content=b"",
                              headers=admin_headers)
        assert response.status_code == 400
        assert not list(image_dir.iterdir())

    def test_scan_catalogues_files_copied_by_hand(self, client, admin_headers, image_dir):
        image_dir.mkdir(parents=True, exist_ok=True)
        (image_dir / "rescue.iso").write_bytes(b"rescue")
        (image_dir / "notes.txt").write_text("not an image")
        found = client.post("/api/v1/admin/images/scan", headers=admin_headers).json()
        assert [i["filename"] for i in found] == ["rescue.iso"]
        assert found[0]["sha256"] == hashlib.sha256(b"rescue").hexdigest()
        assert client.post("/api/v1/admin/images/scan", headers=admin_headers).json() == []

    def test_delete_removes_the_file_too(self, client, admin_headers, image_dir):
        image = client.put("/api/v1/admin/images/upload?filename=gone.iso", content=b"bye",
                           headers=admin_headers).json()
        assert client.delete(f"/api/v1/admin/images/{image['id']}",
                             headers=admin_headers).status_code == 204
        assert not (image_dir / "gone.iso").exists()
        assert client.get("/api/v1/admin/images", headers=admin_headers).json() == []

    def test_fetch_creates_a_catalogue_entry_and_a_job(self, client, admin_headers, image_dir,
                                                       dispatched):
        response = client.post("/api/v1/admin/images/fetch",
                               json={"url": "http://mirror.example/ubuntu-24.04.iso"},
                               headers=admin_headers)
        assert response.status_code == 202, response.text
        body = response.json()
        assert body["image"]["status"] == "fetching"
        assert body["image"]["filename"] == "ubuntu-24.04.iso"
        assert body["job"]["type"] == JobType.IMAGE_FETCH.value
        assert dispatched == [body["job"]["id"]]
        assert client.post("/api/v1/admin/images/fetch", json={"url": "ftp://x/y.iso"},
                           headers=admin_headers).status_code == 400

    @responses.activate
    def test_fetch_job_downloads_with_progress_and_checksum(self, db, image_dir, dispatched):
        from app.models import Image, Job
        from app.services import jobs as job_service
        from app.workers.tasks import image_fetch_task

        payload = b"\x01" * (3 * 1024 * 1024)
        responses.add(responses.GET, "http://mirror.example/big.iso", body=payload,
                      headers={"Content-Length": str(len(payload))})
        image = Image(name="big", filename="big.iso", source_url="http://mirror.example/big.iso",
                      status="fetching")
        db.add(image)
        db.flush()
        job, _ = job_service.create_job(db, job_type=JobType.IMAGE_FETCH, server_id=None,
                                        payload={"image_id": str(image.id)})
        db.commit()

        image_fetch_task(str(job.id))

        db.expire_all()
        job = db.get(Job, job.id)
        image = db.get(Image, image.id)
        assert job.state == JobState.SUCCEEDED, job.error
        assert job.progress == 100 and job.stage == "downloaded, 3 MB"
        assert image.status == "ready" and image.size_bytes == len(payload)
        assert image.sha256 == hashlib.sha256(payload).hexdigest()
        assert (image_dir / "big.iso").stat().st_size == len(payload)
        assert not (image_dir / "big.iso.part").exists()

    @responses.activate
    def test_fetch_job_failure_is_recorded_on_the_image(self, db, image_dir, dispatched):
        from app.models import Image, Job
        from app.services import jobs as job_service
        from app.workers.tasks import image_fetch_task

        responses.add(responses.GET, "http://mirror.example/missing.iso", status=404)
        image = Image(name="missing", filename="missing.iso",
                      source_url="http://mirror.example/missing.iso", status="fetching")
        db.add(image)
        db.flush()
        job, _ = job_service.create_job(db, job_type=JobType.IMAGE_FETCH, server_id=None,
                                        payload={"image_id": str(image.id)})
        db.commit()

        with pytest.raises(Exception, match="404"):
            image_fetch_task(str(job.id))

        db.expire_all()
        assert db.get(Job, job.id).state == JobState.FAILED
        image = db.get(Image, image.id)
        assert image.status == "failed" and "404" in image.error
        assert not list(image_dir.glob("missing*"))


# ---------------------------------------------------------------------------
# Job bodies against a recording driver
# ---------------------------------------------------------------------------


class RecordingDriver:
    def __init__(self, state="on"):
        self.state = state
        self.calls: list[tuple] = []
        self.media: list[dict] = []

    def power_status(self):
        self.calls.append(("power_status",))
        return type("P", (), {"state": self.state})()

    def power(self, action):
        self.calls.append(("power", action))
        self.state = "off" if action is PowerAction.FORCE_OFF else "on"

    def power_cycle(self, *, settle_seconds=5):
        self.calls.append(("power_cycle",))
        self.state = "on"

    def wait_for_power_state(self, want, timeout=180, interval=5):
        return self.state == want

    def set_boot_once(self, target):
        self.calls.append(("set_boot_once", target))

    def insert_virtual_media(self, url):
        self.calls.append(("insert_virtual_media", url))

    def eject_virtual_media(self):
        self.calls.append(("eject_virtual_media",))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None


@pytest.fixture
def recording_driver(monkeypatch):
    driver = RecordingDriver()

    @contextmanager
    def fake_get_driver(server, *, log=None, interactive=False):
        yield driver

    monkeypatch.setattr("app.workers.tasks.get_driver", fake_get_driver)
    monkeypatch.setattr("app.workers.tasks.protocol_label", lambda d: "fake")
    return driver


def _run(db, task, job_type, server, payload):
    from app.models import Job
    from app.services import jobs as job_service

    job, _ = job_service.create_job(db, job_type=job_type, server_id=server.id, payload=payload)
    db.commit()
    task(str(job.id))
    db.expire_all()
    return db.get(Job, job.id)


class TestJobBodies:
    def test_boot_override_resets_a_running_server(self, db, make_server, recording_driver,
                                                   dispatched):
        from app.workers.tasks import boot_override_task

        job = _run(db, boot_override_task, JobType.BOOT_OVERRIDE, make_server(),
                   {"device": "disk", "then": "reset"})
        assert job.state == JobState.SUCCEEDED, job.error
        assert recording_driver.calls[0] == ("set_boot_once", "hdd")
        assert ("power", PowerAction.FORCE_RESTART) in recording_driver.calls
        assert job.result["power_state"] == "on" and job.result["reached_target"]

    def test_boot_override_powers_on_a_server_that_is_off(self, db, make_server,
                                                          recording_driver, dispatched):
        from app.workers.tasks import boot_override_task

        recording_driver.state = "off"
        job = _run(db, boot_override_task, JobType.BOOT_OVERRIDE, make_server(),
                   {"device": "pxe", "then": "reset"})
        assert job.state == JobState.SUCCEEDED, job.error
        assert ("power", PowerAction.ON) in recording_driver.calls
        assert not any(c == ("power", PowerAction.FORCE_RESTART) for c in recording_driver.calls)

    def test_boot_override_can_just_set_the_flag(self, db, make_server, recording_driver,
                                                 dispatched):
        from app.workers.tasks import boot_override_task

        job = _run(db, boot_override_task, JobType.BOOT_OVERRIDE, make_server(),
                   {"device": "bios", "then": "none"})
        assert job.state == JobState.SUCCEEDED
        assert recording_driver.calls == [("set_boot_once", "bios")]
        assert "restart within a minute" in job.stage

    def test_vmedia_boot_mounts_sets_cd_and_cycles(self, db, make_server, recording_driver,
                                                   dispatched):
        from app.workers.tasks import vmedia_task

        job = _run(db, vmedia_task, JobType.VMEDIA_BOOT, make_server(),
                   {"image_url": "http://10.0.0.5:8080/iso/u.iso", "image_name": "u", "boot": True})
        assert job.state == JobState.SUCCEEDED, job.error
        assert recording_driver.calls[:1] == [("insert_virtual_media", "http://10.0.0.5:8080/iso/u.iso")]
        assert ("set_boot_once", "cd") in recording_driver.calls
        assert ("power_cycle",) in recording_driver.calls
        assert job.result["booted"] is True and "open the KVM" in job.stage

    def test_vmedia_mount_only_and_eject(self, db, make_server, recording_driver, dispatched):
        from app.workers.tasks import vmedia_task

        server = make_server()
        job = _run(db, vmedia_task, JobType.VMEDIA_BOOT, server,
                   {"image_url": "http://x/u.iso", "boot": False})
        assert job.state == JobState.SUCCEEDED and job.result["booted"] is False
        assert ("power_cycle",) not in recording_driver.calls
        job = _run(db, vmedia_task, JobType.VMEDIA_EJECT, server, {})
        assert job.state == JobState.SUCCEEDED
        assert ("eject_virtual_media",) in recording_driver.calls


class TestFleetPower:
    def test_reads_every_server_and_reports_unreachable_ones(self, client, make_server,
                                                              admin_headers):
        # Nothing listens on port 1, so both come back unknown -- quickly, and
        # without one dead BMC hiding the other.
        a = make_server(cimc_ip="127.0.0.1", ipmi_port=1, bmc_protocol="ipmi")
        b = make_server(cimc_ip="127.0.0.2", ipmi_port=1, bmc_protocol="ipmi")
        bmc_status.forget(a.id)
        bmc_status.forget(b.id)
        body = client.get("/api/v1/admin/power", headers=admin_headers).json()
        assert set(body["servers"]) == {str(a.id), str(b.id)}
        assert all(v["state"] == "unknown" and v["error"] for v in body["servers"].values())
