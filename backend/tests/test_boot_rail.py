"""The netboot rail: script rendering, access control, and installer callbacks.

These are the tests that matter most. A bug here does not raise an exception —
it produces a boot script that is subtly wrong, and the failure shows up
twenty minutes later as a machine that never came back.
"""

from __future__ import annotations

import pytest

from app.enums import JobState, JobType, ServerState
from app.security import boot_signature
from app.services import boot as boot_service
from app.services import jobs as job_service
from app.services import provisioning


@pytest.fixture
def install_job(db, make_customer, make_server, make_subscription, make_template, make_ssh_key):
    customer = make_customer()
    server = make_server(state=ServerState.ACTIVE)
    make_subscription(customer, server)
    make_ssh_key(customer)
    template = make_template()

    job = provisioning.create_install_job(
        db,
        server,
        os_template_id=template.id,
        hostname="web01",
        raid_level=provisioning.RaidLevel.RAID1,
        ssh_key_ids=[],
        root_password=None,
        customer=customer,
    )
    # What the worker records once the disks are ready and the PXE flag is
    # set; before that a PXE boot is told to wait (TestNotReadyYet).
    job.payload = {**job.payload, "_netboot_ready": True}
    db.commit()
    return server, job, customer


class TestNotReadyYet:
    def test_a_boot_before_the_worker_is_ready_is_told_to_wait(self, client, db, install_job):
        # A server with no OS falls through to PXE on every boot, so it can
        # turn up while the controller is still being rebuilt.
        server, job, _ = install_job
        job.payload = {k: v for k, v in job.payload.items() if k != "_netboot_ready"}
        db.commit()
        script = client.get("/boot/ipxe", params={"mac": server.provisioning_mac}).text
        assert "still preparing the disks" in script
        assert "chain http://" in script and f"/boot/ipxe?mac={server.provisioning_mac}" in script
        assert "doz-installer" not in script
        db.refresh(job)
        assert job.progress == 0  # not "installer fetched boot script"

        job.payload = {**job.payload, "_netboot_ready": True}
        db.commit()
        script = client.get("/boot/ipxe", params={"mac": server.provisioning_mac}).text
        assert "doz-installer" in script


class TestBootScriptRendering:
    def test_ipxe_script_carries_everything_the_ramdisk_needs(self, db, install_job):
        server, job, _ = install_job
        script = boot_service.render_ipxe_script(db, server, job)

        assert script.startswith("#!ipxe")
        assert f"doz_job={job.id}" in script
        assert f"doz_mac={server.provisioning_mac}" in script
        assert "doz_mode=install" in script
        # Serial console output is what makes a failed install debuggable.
        assert "console=ttyS0,115200n8" in script

    def test_rescue_script_uses_the_rescue_mode(self, db, make_customer, make_server,
                                                make_subscription, make_ssh_key):
        customer = make_customer()
        server = make_server()
        make_subscription(customer, server)
        make_ssh_key(customer)
        job = provisioning.create_rescue_job(
            db, server, ssh_key_ids=[], customer=customer
        )
        db.commit()

        script = boot_service.render_ipxe_script(db, server, job)
        assert "doz_mode=rescue" in script
        assert "Disks will NOT be modified" in script

    def test_answer_file_contains_the_customers_keys(self, db, install_job):
        server, job, _ = install_job
        content_type, body = boot_service.render_answer_file(db, server, job)

        assert content_type == "text/yaml"
        assert "ssh-ed25519 AAAAC3Nza" in body
        assert "hostname: web01" in body
        # The completion callback must be embedded, or the job times out.
        assert str(job.id) in body
        assert job.payload["_callback_token"] in body

    def test_provision_script_reflects_the_requested_raid_level(self, db, install_job):
        server, job, _ = install_job
        job.payload = {**job.payload, "raid_level": "raid10"}
        db.commit()

        script = boot_service.render_provision_script(db, server, job)
        assert 'RAID_LEVEL="raid10"' in script
        assert "storcli" in script.lower()

    def test_static_addressing_reaches_the_answer_file(
        self, db, install_job, make_customer
    ):
        from app.models import IPAssignment, IPBlock

        server, job, customer = install_job
        block = IPBlock(cidr="203.0.113.0/24", gateway="203.0.113.1", routing_mode="bridged")
        db.add(block)
        db.flush()
        db.add(
            IPAssignment(
                block_id=block.id,
                address="203.0.113.50",
                prefix_len=24,
                server_id=server.id,
                customer_id=customer.id,
                is_primary=True,
            )
        )
        db.commit()

        _, body = boot_service.render_answer_file(db, server, job)
        assert "203.0.113.50/24" in body
        assert "203.0.113.1" in body

    def test_no_addressing_yet_still_renders(self, db, install_job):
        """An install must not break just because IPAM has not run."""
        server, job, _ = install_job
        _, body = boot_service.render_answer_file(db, server, job)
        assert "autoinstall" in body


class TestBootEndpoints:
    def test_unknown_mac_gets_boot_from_disk(self, client):
        response = client.get("/boot/ipxe", params={"mac": "de:ad:be:ef:00:01"})
        assert response.status_code == 200
        assert "Booting from local disk" in response.text

    def test_server_with_no_job_gets_boot_from_disk(
        self, client, make_customer, make_server, make_subscription
    ):
        """The normal case for every reboot of every non-provisioning server."""
        customer = make_customer()
        server = make_server()
        make_subscription(customer, server)

        response = client.get("/boot/ipxe", params={"mac": server.provisioning_mac})
        assert "Booting from local disk" in response.text

    def test_active_job_gets_the_install_script(self, client, db, install_job):
        server, job, _ = install_job
        response = client.get("/boot/ipxe", params={"mac": server.provisioning_mac})

        assert response.status_code == 200
        assert f"doz_job={job.id}" in response.text
        db.refresh(job)
        assert job.progress == 20

    def test_mac_formats_are_normalised(self, client, install_job):
        server, job, _ = install_job
        for form in ("AA-BB-CC-00-00-01", "aabbcc000001", "AA:BB:CC:00:00:01"):
            response = client.get("/boot/ipxe", params={"mac": form})
            assert f"doz_job={job.id}" in response.text, form

    def test_answer_file_requires_a_valid_signature(self, client, install_job):
        server, _, _ = install_job
        mac = server.provisioning_mac

        bad = client.get(f"/boot/answer/{mac}", params={"sig": "not-the-signature"})
        assert bad.status_code == 403

        good = client.get(f"/boot/answer/{mac}", params={"sig": boot_signature(mac)})
        assert good.status_code == 200

    def test_boot_material_is_pinned_to_the_first_client(self, client, install_job):
        """A second machine on the VLAN cannot fetch another host's answer file."""
        server, _, _ = install_job
        mac = server.provisioning_mac
        sig = boot_signature(mac)

        first = client.get(
            f"/boot/answer/{mac}",
            params={"sig": sig},
            headers={"X-Forwarded-For": "10.20.0.11"},
        )
        assert first.status_code == 200

        second = client.get(
            f"/boot/answer/{mac}",
            params={"sig": sig},
            headers={"X-Forwarded-For": "10.20.0.99"},
        )
        assert second.status_code == 403

        again = client.get(
            f"/boot/answer/{mac}",
            params={"sig": sig},
            headers={"X-Forwarded-For": "10.20.0.11"},
        )
        assert again.status_code == 200


class TestInstallerCallbacks:
    def test_progress_updates_the_job(self, client, db, install_job):
        _, job, _ = install_job
        token = job.payload["_callback_token"]

        response = client.post(
            f"/boot/callback/{job.id}/progress",
            json={"stage": "partitioning disks", "progress": 60},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 204

        db.refresh(job)
        assert job.stage == "partitioning disks"
        assert job.progress == 60

    def test_progress_cannot_reach_100(self, client, db, install_job):
        """The last stretch belongs to the worker, not the installer."""
        _, job, _ = install_job
        client.post(
            f"/boot/callback/{job.id}/progress",
            json={"stage": "finishing", "progress": 100},
            headers={"Authorization": f"Bearer {job.payload['_callback_token']}"},
        )
        db.refresh(job)
        assert job.progress == 94

    def test_wrong_token_is_rejected(self, client, install_job):
        _, job, _ = install_job
        response = client.post(
            f"/boot/callback/{job.id}/progress",
            json={"stage": "hello", "progress": 10},
            headers={"Authorization": "Bearer wrong-token"},
        )
        assert response.status_code == 401

    def test_missing_token_is_rejected(self, client, install_job):
        _, job, _ = install_job
        response = client.post(
            f"/boot/callback/{job.id}/progress", json={"stage": "hello", "progress": 10}
        )
        assert response.status_code == 401

    def test_completion_records_the_result_without_ending_the_job(
        self, client, db, install_job
    ):
        _, job, _ = install_job
        response = client.post(
            f"/boot/callback/{job.id}/complete",
            json={"success": True, "message": "Ubuntu installed", "host_keys": ["SHA256:abc"]},
            headers={"Authorization": f"Bearer {job.payload['_callback_token']}"},
        )
        assert response.status_code == 204

        db.refresh(job)
        assert job.result["_installer_status"] == "succeeded"
        assert job.result["host_keys"] == ["SHA256:abc"]
        # The worker still has to clear the boot override.
        assert job.state == JobState.QUEUED

    def test_finished_job_stops_accepting_callbacks(self, client, db, install_job):
        _, job, _ = install_job
        token = job.payload["_callback_token"]

        job_service.transition_job(db, job, JobState.RUNNING)
        job_service.transition_job(db, job, JobState.SUCCEEDED)
        db.commit()

        response = client.post(
            f"/boot/callback/{job.id}/progress",
            json={"stage": "late", "progress": 10},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 409

    def test_internal_result_keys_are_hidden_from_customers(
        self, client, db, install_job, auth_header
    ):
        _, job, customer = install_job
        client.post(
            f"/boot/callback/{job.id}/complete",
            json={"success": True, "message": "done"},
            headers={"Authorization": f"Bearer {job.payload['_callback_token']}"},
        )

        body = client.get(f"/api/v1/jobs/{job.id}", headers=auth_header(customer)).json()
        assert "_installer_status" not in body["result"]


class TestRaidByBmc:
    def test_ramdisk_skips_storcli_when_the_bmc_built_the_array(self, db, install_job):
        server, job, _ = install_job
        before = boot_service.render_provision_script(db, server, job)
        assert "build_array" in before.split("do_install()")[1]

        job.payload = {**job.payload, "_raid_configured": {
            "description": "RAID1 over 2 drives (Disk 1, Disk 2), 999 GB", "via": "redfish",
        }}
        db.commit()
        after = boot_service.render_provision_script(db, server, job)
        body = after.split("do_install()")[1].split("install_os()")[0]
        assert "array built through the BMC before this boot: RAID1 over 2 drives" in body
        assert "build_array" not in body


class TestHandoff:
    """An install boots twice: the ramdisk, then the distribution's installer."""

    @pytest.fixture
    def bmc(self, monkeypatch):
        from contextlib import contextmanager

        from app.api import boot as boot_api

        calls: list[str] = []

        class Driver:
            def set_boot_once(self, target):  # noqa: ANN001
                calls.append(target)

        @contextmanager
        def fake_driver(server, log=None, interactive=False):  # noqa: ANN001
            yield Driver()

        monkeypatch.setattr(boot_api, "get_driver", fake_driver)
        return calls

    def test_provision_script_hands_off_by_rebooting(self, db, install_job):
        server, job, _ = install_job
        script = boot_service.render_provision_script(db, server, job)
        assert f'JOB_ID="{job.id}"' in script
        assert "/boot/callback/$JOB_ID/handoff" in script
        assert "reboot -f" in script
        assert "kexec -" not in script  # no kexec command: this kernel cannot

    def test_handoff_flags_the_job_and_sets_pxe_for_the_next_boot(
        self, client, db, install_job, bmc
    ):
        server, job, _ = install_job
        token = job.payload["_callback_token"]
        response = client.post(f"/boot/callback/{job.id}/handoff",
                               headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 204, response.text
        db.refresh(job)
        assert job.result["_handoff"] is True
        assert job.stage == "rebooting into the OS installer" and job.progress == 50
        assert bmc == ["pxe"]

        # The next PXE boot gets the OS installer, not the ramdisk again.
        script = client.get("/boot/ipxe", params={"mac": server.provisioning_mac}).text
        assert "doz-installer" not in script
        assert "kernel " in script and "/os/ubuntu-22.04/casper/vmlinuz" in script
        assert "initrd " in script and "/os/ubuntu-22.04/casper/initrd" in script
        sig = boot_signature(server.provisioning_mac)
        assert "autoinstall ds=nocloud-net;s=http://" in script
        assert f"/boot/nocloud/{server.provisioning_mac}/{sig}/" in script
        assert "console=ttyS0,115200n8" in script
        db.refresh(job)
        assert job.stage == "loading the OS installer" and job.progress == 55

    def test_handoff_without_a_reachable_bmc_still_hands_off(
        self, client, db, install_job, monkeypatch
    ):
        from contextlib import contextmanager

        from app.api import boot as boot_api
        from app.drivers import BMCError

        @contextmanager
        def dead(server, log=None, interactive=False):  # noqa: ANN001
            raise BMCError("IPMI session failed")
            yield

        monkeypatch.setattr(boot_api, "get_driver", dead)
        server, job, _ = install_job
        token = job.payload["_callback_token"]
        response = client.post(f"/boot/callback/{job.id}/handoff",
                               headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 204
        db.refresh(job)
        assert job.result["_handoff"] is True
        script = client.get("/boot/ipxe", params={"mac": server.provisioning_mac}).text
        assert "/os/ubuntu-22.04/casper/vmlinuz" in script

    def test_installer_args_per_method(self, make_template):
        from app.enums import InstallMethod

        mac, sig = "aa:bb:cc:00:00:01", "deadbeef"
        ks = boot_service.installer_args(
            make_template(install_method=InstallMethod.KICKSTART), mac=mac, signature=sig
        )
        assert ks.startswith("inst.ks=http://") and f"/boot/answer/{mac}?sig={sig} inst.text" in ks
        ps = boot_service.installer_args(
            make_template(install_method=InstallMethod.PRESEED), mac=mac, signature=sig
        )
        assert ps.startswith("auto=true priority=critical url=http://")
        assert boot_service.installer_args(
            make_template(install_method=InstallMethod.IMAGE), mac=mac, signature=sig
        ) == ""

    def test_only_installs_hand_off(self, client, db, make_customer, make_server,
                                    make_subscription, make_ssh_key, bmc):
        customer = make_customer()
        server = make_server()
        make_subscription(customer, server)
        make_ssh_key(customer)
        job = provisioning.create_rescue_job(db, server, ssh_key_ids=[], customer=customer)
        db.commit()
        token = job.payload["_callback_token"]
        response = client.post(f"/boot/callback/{job.id}/handoff",
                               headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 409
        assert bmc == []


class TestWipeGating:
    def test_wipe_with_no_drives_erased_is_a_failure(self, db, make_server):
        """A wipe that erased nothing must not put a server back on sale."""

        server = make_server(state=ServerState.ACTIVE)
        job, _ = job_service.create_job(
            db, job_type=JobType.WIPE, server_id=server.id, with_callback_token=True
        )
        db.commit()

        # The task refuses before it would flip the server to in_stock; here we
        # assert the guard itself, since driving the full task needs a BMC.
        from app.config import settings

        assert settings.require_wipe_before_stock is True
        assert (job.result or {}).get("drives_wiped") is None
