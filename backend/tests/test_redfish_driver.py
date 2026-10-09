"""Redfish driver behaviour against a simulated CIMC.

The M4's quirks are the point of these tests: a boot override that reports
success but does not take effect, graceful reset types the BMC will not accept,
and a virtual media resource missing its InsertMedia action. All three have
been observed on real 4.x CIMC builds and all three fail silently in the worst
possible way if the driver trusts what it is told.
"""

from __future__ import annotations

import json

import pytest
import responses

from app.drivers.base import BMCError
from app.drivers.redfish import RedfishDriver
from app.enums import PowerAction
from app.secrets import BMCCredential

HOST = "10.10.99.1"
BASE = f"https://{HOST}"
SYSTEM = "/redfish/v1/Systems/SYS1"
MANAGER = "/redfish/v1/Managers/CIMC"

CREDENTIAL = BMCCredential(username="admin", password="bench-password")


def driver(**kwargs) -> RedfishDriver:
    return RedfishDriver(host=HOST, credential=CREDENTIAL, verify_tls=False, **kwargs)


def system_body(**overrides) -> dict:
    body = {
        "@odata.id": SYSTEM,
        "PowerState": "On",
        "Manufacturer": "Cisco Systems Inc.",
        "Model": "UCSC-C220-M4S",
        "SerialNumber": "FCH1234V5X6",
        "BiosVersion": "C220M4.4.1.2f.0",
        "Status": {"Health": "OK", "HealthRollup": "OK", "State": "Enabled"},
        "ProcessorSummary": {
            "Count": 2,
            "Model": "Intel(R) Xeon(R) CPU E5-2680 v4",
            "LogicalProcessorCount": 56,
            "Status": {"HealthRollup": "OK"},
        },
        "MemorySummary": {"TotalSystemMemoryGiB": 256, "Status": {"HealthRollup": "OK"}},
        "Boot": {
            "BootSourceOverrideEnabled": "Disabled",
            "BootSourceOverrideTarget": "None",
        },
        "Actions": {
            "#ComputerSystem.Reset": {
                "target": f"{SYSTEM}/Actions/ComputerSystem.Reset",
                "ResetType@Redfish.AllowableValues": [
                    "On",
                    "ForceOff",
                    "GracefulShutdown",
                    "ForceRestart",
                    "GracefulRestart",
                ],
            }
        },
    }
    body.update(overrides)
    return body


def register_common(rsps, system: dict | None = None) -> None:
    rsps.add(rsps.GET, f"{BASE}/redfish/v1/Systems", json={"Members": [{"@odata.id": SYSTEM}]})
    rsps.add(rsps.GET, f"{BASE}/redfish/v1/Managers", json={"Members": [{"@odata.id": MANAGER}]})
    rsps.add(rsps.GET, f"{BASE}{SYSTEM}", json=system or system_body())


class TestDiscovery:
    @responses.activate
    def test_system_path_is_discovered_and_cached(self):
        register_common(responses)
        d = driver()
        assert d.system_path == SYSTEM
        assert d.system_path == SYSTEM
        # One collection fetch, not two.
        collection_calls = [
            c for c in responses.calls if c.request.url.endswith("/redfish/v1/Systems")
        ]
        assert len(collection_calls) == 1

    @responses.activate
    def test_no_systems_is_an_error(self):
        responses.add(responses.GET, f"{BASE}/redfish/v1/Systems", json={"Members": []})
        with pytest.raises(BMCError, match="no ComputerSystem"):
            driver().power_status()


class TestPower:
    @responses.activate
    def test_power_status(self):
        register_common(responses)
        assert driver().power_status().state == "on"

    @responses.activate
    def test_unknown_power_state_is_reported_as_unknown(self):
        register_common(responses, system_body(PowerState="PoweringOn"))
        assert driver().power_status().state == "unknown"

    @responses.activate
    def test_reset_uses_the_action_target(self):
        register_common(responses)
        responses.add(
            responses.POST, f"{BASE}{SYSTEM}/Actions/ComputerSystem.Reset", status=204
        )
        driver().power(PowerAction.FORCE_OFF)

        post = responses.calls[-1].request
        assert b'"ResetType": "ForceOff"' in post.body

    @responses.activate
    def test_graceful_falls_back_when_unsupported(self):
        """A machine with no OS ignores ACPI; the job must not hang on it."""
        system = system_body()
        system["Actions"]["#ComputerSystem.Reset"][
            "ResetType@Redfish.AllowableValues"
        ] = ["On", "ForceOff", "ForceRestart"]
        register_common(responses, system)
        responses.add(
            responses.POST, f"{BASE}{SYSTEM}/Actions/ComputerSystem.Reset", status=204
        )

        driver().power(PowerAction.OFF)
        assert b'"ResetType": "ForceOff"' in responses.calls[-1].request.body

    @responses.activate
    def test_impossible_reset_type_raises(self):
        system = system_body()
        system["Actions"]["#ComputerSystem.Reset"][
            "ResetType@Redfish.AllowableValues"
        ] = ["ForceOff"]
        register_common(responses, system)

        with pytest.raises(BMCError, match="does not support ResetType"):
            driver().power(PowerAction.ON)


class TestBootOverride:
    @responses.activate
    def test_override_is_read_back_after_setting(self):
        register_common(responses)
        responses.add(responses.PATCH, f"{BASE}{SYSTEM}", status=204)
        responses.add(
            responses.GET,
            f"{BASE}{SYSTEM}",
            json=system_body(
                Boot={
                    "BootSourceOverrideEnabled": "Once",
                    "BootSourceOverrideTarget": "Pxe",
                }
            ),
        )
        driver().set_boot_once("pxe")

    @responses.activate
    def test_silently_ignored_override_is_caught(self):
        """The failure that turns a reinstall into a twenty-minute mystery."""
        register_common(responses)
        responses.add(responses.PATCH, f"{BASE}{SYSTEM}", status=204)
        # The BMC accepts the PATCH and changes nothing.
        responses.add(responses.GET, f"{BASE}{SYSTEM}", json=system_body())

        with pytest.raises(BMCError, match="did not take effect"):
            driver().set_boot_once("pxe")

    @responses.activate
    def test_target_set_but_override_disabled_is_caught(self):
        register_common(responses)
        responses.add(responses.PATCH, f"{BASE}{SYSTEM}", status=204)
        responses.add(
            responses.GET,
            f"{BASE}{SYSTEM}",
            json=system_body(
                Boot={
                    "BootSourceOverrideEnabled": "Disabled",
                    "BootSourceOverrideTarget": "Pxe",
                }
            ),
        )
        with pytest.raises(BMCError, match="override is disabled"):
            driver().set_boot_once("pxe")

    def test_unknown_target_is_rejected_before_any_request(self):
        with pytest.raises(ValueError, match="unknown boot target"):
            driver().set_boot_once("floppy")


class TestVirtualMedia:
    VM_COLLECTION = f"{MANAGER}/VirtualMedia"
    VM_CD = f"{MANAGER}/VirtualMedia/CD"

    def _register(self, resource: dict) -> None:
        responses.add(
            responses.GET,
            f"{BASE}/redfish/v1/Managers",
            json={"Members": [{"@odata.id": MANAGER}]},
        )
        # Redfish 1.0 (the M4's CIMC): the Manager links to its VirtualMedia.
        responses.add(
            responses.GET,
            f"{BASE}{MANAGER}",
            json={"@odata.id": MANAGER, "VirtualMedia": {"@odata.id": self.VM_COLLECTION}},
        )
        responses.add(
            responses.GET,
            f"{BASE}{self.VM_COLLECTION}",
            json={"Members": [{"@odata.id": self.VM_CD}]},
        )
        responses.add(responses.GET, f"{BASE}{self.VM_CD}", json=resource)

    @responses.activate
    def test_insert_uses_the_action_when_present(self):
        self._register(
            {
                "@odata.id": self.VM_CD,
                "MediaTypes": ["CD", "DVD"],
                "Inserted": False,
                "Actions": {
                    "#VirtualMedia.InsertMedia": {
                        "target": f"{self.VM_CD}/Actions/VirtualMedia.InsertMedia"
                    }
                },
            }
        )
        responses.add(
            responses.POST,
            f"{BASE}{self.VM_CD}/Actions/VirtualMedia.InsertMedia",
            status=204,
        )
        responses.add(
            responses.GET,
            f"{BASE}{self.VM_CD}",
            json={"@odata.id": self.VM_CD, "MediaTypes": ["CD"], "Inserted": True},
        )

        driver().insert_virtual_media("http://10.10.0.5:8080/iso/rocky-9.iso")
        post = next(c for c in responses.calls if "InsertMedia" in c.request.url)
        body = json.loads(post.request.body)
        assert body["Image"] == "http://10.10.0.5:8080/iso/rocky-9.iso"
        assert body["TransferProtocolType"] == "HTTP"  # the CIMC refuses without it
        assert body["Inserted"] is True and body["WriteProtected"] is True

    @responses.activate
    def test_a_rejected_insert_says_what_the_bmc_objected_to(self):
        self._register(
            {
                "@odata.id": self.VM_CD,
                "MediaTypes": ["CD", "DVD"],
                "Inserted": False,
                "Actions": {
                    "#VirtualMedia.InsertMedia": {
                        "target": f"{self.VM_CD}/Actions/VirtualMedia.InsertMedia"
                    }
                },
            }
        )
        responses.add(
            responses.POST,
            f"{BASE}{self.VM_CD}/Actions/VirtualMedia.InsertMedia",
            status=400,
            json={"error": {
                "code": "Base.1.4.GeneralError",
                "message": "A general error has occurred. See ExtendedInfo for more information.",
                "@Message.ExtendedInfo": [{
                    "MessageId": "Base.1.4.PropertyValueFormatError",
                    "Message": "The value http://10.10.0.5:8080/iso/rocky-9.iso for the property "
                               "Image is of a different format than the property can accept.",
                }],
            }},
        )
        with pytest.raises(BMCError, match="returned 400: The value .* property Image"):
            driver().insert_virtual_media("http://10.10.0.5:8080/iso/rocky-9.iso")

    @responses.activate
    def test_insert_falls_back_to_patch_on_older_firmware(self):
        """Pre-4.1 CIMC omits the action and expects the Image property set."""
        self._register(
            {
                "@odata.id": self.VM_CD,
                "MediaTypes": ["CD", "DVD"],
                "Inserted": False,
                "Actions": {},
            }
        )
        responses.add(responses.PATCH, f"{BASE}{self.VM_CD}", status=200)
        responses.add(
            responses.GET,
            f"{BASE}{self.VM_CD}",
            json={"@odata.id": self.VM_CD, "MediaTypes": ["CD"], "Inserted": True},
        )

        driver().insert_virtual_media("http://10.10.0.5:8080/iso/rocky-9.iso")
        assert any(c.request.method == "PATCH" for c in responses.calls)

    @responses.activate
    def test_media_that_does_not_insert_is_an_error(self):
        self._register(
            {
                "@odata.id": self.VM_CD,
                "MediaTypes": ["CD"],
                "Inserted": False,
                "Actions": {
                    "#VirtualMedia.InsertMedia": {
                        "target": f"{self.VM_CD}/Actions/VirtualMedia.InsertMedia"
                    }
                },
            }
        )
        responses.add(
            responses.POST,
            f"{BASE}{self.VM_CD}/Actions/VirtualMedia.InsertMedia",
            status=204,
        )
        responses.add(
            responses.GET,
            f"{BASE}{self.VM_CD}",
            json={"@odata.id": self.VM_CD, "MediaTypes": ["CD"], "Inserted": False},
        )

        with pytest.raises(BMCError, match="did not report as inserted"):
            driver().insert_virtual_media("http://10.10.0.5:8080/iso/rocky-9.iso")

    @responses.activate
    def test_newer_schema_with_virtual_media_under_the_system(self):
        """Newer Redfish moved VirtualMedia to the ComputerSystem; follow the link."""
        system_vm = f"{SYSTEM}/VirtualMedia"
        responses.add(responses.GET, f"{BASE}/redfish/v1/Managers",
                      json={"Members": [{"@odata.id": MANAGER}]})
        responses.add(responses.GET, f"{BASE}{MANAGER}",
                      json={"@odata.id": MANAGER, "VirtualMedia": {"@odata.id": system_vm}})
        responses.add(responses.GET, f"{BASE}{system_vm}",
                      json={"Members": [{"@odata.id": f"{system_vm}/Cd"}]})
        cd = {"@odata.id": f"{system_vm}/Cd", "MediaTypes": ["CD"], "Inserted": False,
              "Actions": {"#VirtualMedia.InsertMedia": {
                  "target": f"{system_vm}/Cd/Actions/VirtualMedia.InsertMedia"}}}
        responses.add(responses.GET, f"{BASE}{system_vm}/Cd", json=cd)
        responses.add(responses.POST,
                      f"{BASE}{system_vm}/Cd/Actions/VirtualMedia.InsertMedia", status=204)
        responses.add(responses.GET, f"{BASE}{system_vm}/Cd", json={**cd, "Inserted": True})

        driver().insert_virtual_media("http://10.0.0.5:8080/os/rocky-9.iso")
        assert any("VirtualMedia.InsertMedia" in c.request.url for c in responses.calls)

    @responses.activate
    def test_no_cd_slot_is_an_error(self):
        self._register(
            {"@odata.id": self.VM_CD, "MediaTypes": ["Floppy", "USBStick"], "Inserted": False}
        )
        with pytest.raises(BMCError, match="no CD/DVD"):
            driver().insert_virtual_media("http://example/iso")


class TestRetryAndErrors:
    @responses.activate
    def test_transient_500s_are_retried(self, monkeypatch):
        monkeypatch.setattr("app.drivers.redfish.time.sleep", lambda _: None)
        responses.add(responses.GET, f"{BASE}/redfish/v1/Systems", status=503)
        responses.add(
            responses.GET,
            f"{BASE}/redfish/v1/Systems",
            json={"Members": [{"@odata.id": SYSTEM}]},
        )
        responses.add(responses.GET, f"{BASE}{SYSTEM}", json=system_body())

        assert driver().power_status().state == "on"

    @responses.activate
    def test_401_is_not_retried(self, monkeypatch):
        monkeypatch.setattr("app.drivers.redfish.time.sleep", lambda _: None)
        responses.add(responses.POST, f"{BASE}/redfish/v1/SessionService/Sessions", status=401)

        with pytest.raises(BMCError, match="rejected the credentials"):
            driver().power_status()
        # One login attempt, no retries, no fallback to basic auth.
        assert len(responses.calls) == 1


class TestSessionAuth:
    """The CIMC caps concurrent sessions; the driver must use exactly one."""

    @responses.activate
    def test_logs_in_once_and_uses_the_token(self):
        responses.add(
            responses.POST,
            f"{BASE}/redfish/v1/SessionService/Sessions",
            status=201,
            headers={
                "X-Auth-Token": "tok-123",
                "Location": "/redfish/v1/SessionService/Sessions/1",
            },
            json={"@odata.id": "/redfish/v1/SessionService/Sessions/1"},
        )
        register_common(responses)

        d = driver()
        d.power_status()
        d.power_status()

        logins = [c for c in responses.calls if c.request.method == "POST"]
        assert len(logins) == 1
        gets = [c for c in responses.calls if c.request.method == "GET"]
        assert all(c.request.headers.get("X-Auth-Token") == "tok-123" for c in gets)
        assert all("Authorization" not in c.request.headers for c in gets)

    @responses.activate
    def test_close_deletes_the_session(self):
        responses.add(
            responses.POST,
            f"{BASE}/redfish/v1/SessionService/Sessions",
            status=201,
            headers={
                "X-Auth-Token": "tok-123",
                "Location": "/redfish/v1/SessionService/Sessions/1",
            },
            json={},
        )
        register_common(responses)
        responses.add(responses.DELETE, f"{BASE}/redfish/v1/SessionService/Sessions/1", status=204)

        with driver() as d:
            d.power_status()

        deletes = [c for c in responses.calls if c.request.method == "DELETE"]
        assert len(deletes) == 1

    @responses.activate
    def test_falls_back_to_basic_auth_without_a_session_service(self):
        """Old builds: the session endpoint 404s but basic auth still works."""
        responses.add(responses.POST, f"{BASE}/redfish/v1/SessionService/Sessions", status=404)
        register_common(responses)

        driver().power_status()
        gets = [c for c in responses.calls if c.request.method == "GET"]
        assert all(c.request.headers.get("Authorization", "").startswith("Basic ") for c in gets)


class TestRedaction:
    @responses.activate
    def test_credentials_never_reach_the_job_log(self):
        """The job log is shown to admins and stored forever."""
        captured: list[dict] = []

        def sink(message, *, level="info", request=None, response=None):  # noqa: ANN001
            captured.append({"message": message, "request": request, "response": response})

        register_common(responses)
        responses.add(responses.PATCH, f"{BASE}{SYSTEM}", status=204)
        responses.add(
            responses.GET,
            f"{BASE}{SYSTEM}",
            json=system_body(
                Boot={
                    "BootSourceOverrideEnabled": "Once",
                    "BootSourceOverrideTarget": "Pxe",
                }
            ),
        )

        d = RedfishDriver(host=HOST, credential=CREDENTIAL, verify_tls=False, log=sink)
        d.set_boot_once("pxe")

        assert captured
        assert CREDENTIAL.password not in str(captured)

    def test_credential_repr_hides_the_password(self):
        assert "bench-password" not in repr(CREDENTIAL)


class TestHealth:
    @responses.activate
    def test_worst_subsystem_wins(self):
        """A failed PSU on an otherwise-fine system is not 'ok'."""
        register_common(responses)
        responses.add(
            responses.GET, f"{BASE}{MANAGER}", json={"FirmwareVersion": "4.1(2f)"}
        )
        responses.add(
            responses.GET,
            f"{BASE}/redfish/v1/Chassis",
            json={"Members": [{"@odata.id": "/redfish/v1/Chassis/1"}]},
        )
        responses.add(
            responses.GET,
            f"{BASE}/redfish/v1/Chassis/1",
            json={"Power": {"@odata.id": "/redfish/v1/Chassis/1/Power"}},
        )
        responses.add(
            responses.GET,
            f"{BASE}/redfish/v1/Chassis/1/Power",
            json={
                "PowerSupplies": [
                    {"Name": "PSU1", "Status": {"Health": "OK", "State": "Enabled"}},
                    {"Name": "PSU2", "Status": {"Health": "Critical", "State": "Enabled"}},
                ]
            },
        )

        health = driver().health()
        assert health.status == "critical"
        assert health.subsystems["power_supplies"]["status"] == "critical"


class TestRaid:
    """Building a virtual drive through the CIMC's Redfish storage model."""

    STORAGE = f"{SYSTEM}/Storage"
    MRAID = f"{SYSTEM}/Storage/MRAID"

    def _controller(self, volumes: list[str]) -> None:
        # No SessionService on this build: the driver falls back to basic auth.
        responses.add(responses.POST, f"{BASE}/redfish/v1/SessionService/Sessions", status=404)
        register_common(responses, system={
            **system_body(), "Storage": {"@odata.id": self.STORAGE},
        })
        responses.add(responses.GET, f"{BASE}{self.STORAGE}",
                      json={"Members": [{"@odata.id": self.MRAID}]})
        responses.add(responses.GET, f"{BASE}{self.MRAID}", json={
            "@odata.id": self.MRAID, "Id": "MRAID", "Name": "MRAID",
            "StorageControllers": [{
                "Name": "Cisco 12G SAS Modular Raid Controller",
                "Model": "UCSC-MRAID12G", "SupportedRAIDTypes": ["RAID0", "RAID1", "RAID5"],
            }],
            "Drives": [{"@odata.id": f"{self.MRAID}/Drives/{i}"} for i in (1, 2, 3)],
            "Volumes": {"@odata.id": f"{self.MRAID}/Volumes"},
        })
        for i, (cap, media) in enumerate(((1_000_204_886_016, "HDD"), (1_000_204_886_016, "HDD"),
                                          (480_103_981_056, "SSD")), start=1):
            responses.add(responses.GET, f"{BASE}{self.MRAID}/Drives/{i}", json={
                "@odata.id": f"{self.MRAID}/Drives/{i}", "Id": str(i), "Name": f"Disk {i}",
                "CapacityBytes": cap, "MediaType": media, "Protocol": "SAS",
                "SerialNumber": f"SN{i}", "Model": "ST1000", "FailurePredicted": False,
                "Status": {"Health": "OK", "State": "Enabled"},
                "Oem": {"Cisco": {"DriveState": "UnconfiguredGood"}},
            })
        members = [{"@odata.id": f"{self.MRAID}/Volumes/{v}"} for v in volumes]
        responses.add(responses.GET, f"{BASE}{self.MRAID}/Volumes", json={"Members": members})
        for v in volumes:
            responses.add(responses.GET, f"{BASE}{self.MRAID}/Volumes/{v}", json={
                "@odata.id": f"{self.MRAID}/Volumes/{v}", "Id": v, "Name": v,
                "VolumeType": "Mirrored", "CapacityBytes": 999_000_000_000,
                "Status": {"Health": "OK"},
                "Links": {"Drives": [{"@odata.id": f"{self.MRAID}/Drives/1"},
                                     {"@odata.id": f"{self.MRAID}/Drives/2"}]},
            })

    @responses.activate
    def test_storage_lists_controllers_drives_and_volumes(self):
        self._controller(volumes=["old"])
        controllers = driver().storage()
        assert len(controllers) == 1
        c = controllers[0]
        assert c["name"] == "Cisco 12G SAS Modular Raid Controller"
        assert c["raid_types"] == ["RAID0", "RAID1", "RAID5"]
        assert [d["name"] for d in c["drives"]] == ["Disk 1", "Disk 2", "Disk 3"]
        assert c["drives"][0]["oem_state"] == "UnconfiguredGood"
        assert c["volumes"][0]["name"] == "old" and c["volumes"][0]["raid_type"] == "Mirrored"
        assert c["volumes"][0]["drive_count"] == 2

    @responses.activate
    def test_create_volume_posts_the_drives_and_waits_for_the_task(self):
        self._controller(volumes=[])
        responses.add(
            responses.POST, f"{BASE}{self.MRAID}/Volumes", status=202,
            headers={"Location": "/redfish/v1/TaskService/Tasks/7"},
        )
        responses.add(responses.GET, f"{BASE}/redfish/v1/TaskService/Tasks/7",
                      json={"TaskState": "Running"})
        responses.add(responses.GET, f"{BASE}/redfish/v1/TaskService/Tasks/7",
                      json={"TaskState": "Completed"})
        d = driver()
        controller = d.storage()[0]
        d.create_volume(
            controller, raid_type="RAID1", name="doz",
            drive_paths=[f"{self.MRAID}/Drives/1", f"{self.MRAID}/Drives/2"],
        )
        post = next(c for c in responses.calls if c.request.method == "POST"
                    and c.request.url.endswith("/Volumes"))
        body = json.loads(post.request.body)
        assert body == {
            "Name": "doz", "RAIDType": "RAID1",
            "Drives": [{"@odata.id": f"{self.MRAID}/Drives/1"},
                       {"@odata.id": f"{self.MRAID}/Drives/2"}],
        }
        assert sum(1 for c in responses.calls if "Tasks/7" in c.request.url) == 2

    @responses.activate
    def test_a_failed_task_is_an_error_with_the_bmcs_reason(self):
        self._controller(volumes=[])
        responses.add(
            responses.POST, f"{BASE}{self.MRAID}/Volumes", status=202,
            headers={"Location": "/redfish/v1/TaskService/Tasks/8"},
        )
        responses.add(responses.GET, f"{BASE}/redfish/v1/TaskService/Tasks/8", json={
            "TaskState": "Exception",
            "Messages": [], "@Message.ExtendedInfo": [{"Message": "Drive 2 is not available"}],
        })
        d = driver()
        with pytest.raises(BMCError, match="Exception: Drive 2 is not available"):
            d.create_volume(d.storage()[0], raid_type="RAID1", name="doz",
                            drive_paths=[f"{self.MRAID}/Drives/1", f"{self.MRAID}/Drives/2"])

    @responses.activate
    def test_delete_volume(self):
        self._controller(volumes=["old"])
        responses.add(responses.DELETE, f"{BASE}{self.MRAID}/Volumes/old", status=204)
        d = driver()
        d.delete_volume(d.storage()[0]["volumes"][0]["path"])
        assert any(c.request.method == "DELETE" for c in responses.calls)

    @responses.activate
    def test_an_array_that_already_matches_is_kept(self):
        from app.enums import RaidLevel
        from app.services import raid

        self._controller(volumes=["old"])
        summary = raid._configure_redfish(driver(), RaidLevel.RAID1,
                                          lambda m, level="info", **kw: None)
        assert summary["kept"] is True and summary["volume"] == "old"
        assert not any(c.request.method in ("DELETE", "POST") and "Volumes" in c.request.url
                       for c in responses.calls)
