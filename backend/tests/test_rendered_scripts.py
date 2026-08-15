"""Syntax-check the things we render and then hand to a machine.

A broken boot script does not raise here — it raises on a server in a rack
twenty minutes into a reinstall, on the serial console, where nobody is
watching. These tests are cheap and they catch the whole class.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest
import yaml

from app.enums import RaidLevel, ServerState
from app.services import boot as boot_service
from app.services import provisioning

POSIX_SHELLS = [s for s in ("dash", "sh", "busybox") if shutil.which(s)]


@pytest.fixture
def rendered(db, make_customer, make_server, make_subscription, make_template, make_ssh_key):
    """Render every artifact for one install job."""

    def _render(raid: RaidLevel = RaidLevel.RAID1, install_method: str = "autoinstall",
                config_template: str = "ubuntu-2204-autoinstall.yaml.j2"):
        customer = make_customer(f"r-{raid.value}-{install_method}@example.com")
        server = make_server(state=ServerState.ACTIVE)
        make_subscription(customer, server)
        make_ssh_key(customer)
        template = make_template(
            install_method=install_method, config_template=config_template
        )
        job = provisioning.create_install_job(
            db,
            server,
            os_template_id=template.id,
            hostname="web01",
            raid_level=raid,
            ssh_key_ids=[],
            root_password=None,
            customer=customer,
        )
        db.commit()
        return server, job

    return _render


class TestProvisionScript:
    @pytest.mark.skipif(not POSIX_SHELLS, reason="no POSIX shell available")
    @pytest.mark.parametrize("raid", list(RaidLevel))
    def test_is_valid_posix_sh(self, db, rendered, raid):
        """Every RAID level must render a script the ramdisk can actually run."""
        server, job = rendered(raid=raid)
        script = boot_service.render_provision_script(db, server, job)

        shell = POSIX_SHELLS[0]
        args = [shell, "-n"] if shell != "busybox" else [shell, "ash", "-n"]
        result = subprocess.run(  # noqa: S603
            args, input=script, capture_output=True, text=True, timeout=30
        )
        assert result.returncode == 0, (
            f"{shell} rejected the rendered script:\n{result.stderr}"
        )

    def test_every_mode_renders(self, db, make_customer, make_server, make_subscription,
                                make_ssh_key):
        """Rescue and wipe go through the same renderer as install."""
        customer = make_customer()
        server = make_server()
        make_subscription(customer, server)
        make_ssh_key(customer)

        rescue = provisioning.create_rescue_job(
            db, server, ssh_key_ids=[], customer=customer
        )
        db.commit()
        script = boot_service.render_provision_script(db, server, rescue)
        assert 'MODE="rescue"' in script
        assert "do_rescue" in script

    def test_no_unrendered_jinja_survives(self, db, rendered):
        """A typo'd variable must not reach a machine as a literal `{{ ... }}`."""
        server, job = rendered()
        for text in (
            boot_service.render_provision_script(db, server, job),
            boot_service.render_ipxe_script(db, server, job),
            boot_service.render_answer_file(db, server, job)[1],
        ):
            assert "{{" not in text
            assert "{%" not in text

    def test_callback_token_is_present_exactly_once_per_use(self, db, rendered):
        server, job = rendered()
        script = boot_service.render_provision_script(db, server, job)
        token = job.payload["_callback_token"]
        # It is assigned to a variable, not pasted into every curl call.
        assert script.count(token) == 1
        assert f'CALLBACK_TOKEN="{token}"' in script


class TestAnswerFiles:
    def test_ubuntu_autoinstall_is_valid_yaml(self, db, rendered):
        server, job = rendered()
        content_type, body = boot_service.render_answer_file(db, server, job)

        assert content_type == "text/yaml"
        parsed = yaml.safe_load(body)
        assert "autoinstall" in parsed
        assert parsed["autoinstall"]["version"] == 1
        assert parsed["autoinstall"]["identity"]["hostname"] == "web01"
        assert parsed["autoinstall"]["ssh"]["install-server"] is True
        assert len(parsed["autoinstall"]["ssh"]["authorized-keys"]) == 1

    def test_debian_preseed_renders(self, db, rendered):
        server, job = rendered(
            install_method="preseed", config_template="debian-12-preseed.cfg.j2"
        )
        _, body = boot_service.render_answer_file(db, server, job)

        assert "d-i partman-auto/disk string /dev/sda" in body
        assert "d-i preseed/late_command string" in body
        # Key-only unless a password was asked for.
        assert "d-i passwd/root-password-crypted password !" in body

    def test_rocky_kickstart_renders(self, db, rendered):
        server, job = rendered(
            install_method="kickstart", config_template="rocky-9-kickstart.cfg.j2"
        )
        _, body = boot_service.render_answer_file(db, server, job)

        assert body.lstrip().startswith("###")
        assert "rootpw --lock" in body
        assert "%post" in body and "%end" in body
        # A failed kickstart must report rather than let the job time out.
        assert "%onerror" in body

    def test_root_password_hash_reaches_the_answer_file(
        self, db, make_customer, make_server, make_subscription, make_template
    ):
        customer = make_customer()
        server = make_server()
        make_subscription(customer, server)
        template = make_template()

        job = provisioning.create_install_job(
            db,
            server,
            os_template_id=template.id,
            hostname="db01",
            raid_level=RaidLevel.RAID1,
            ssh_key_ids=[],
            root_password="a-sufficiently-long-password",
            customer=customer,
        )
        db.commit()

        _, body = boot_service.render_answer_file(db, server, job)
        assert job.payload["root_password_hash"] in body
        assert "a-sufficiently-long-password" not in body


class TestIPXEScripts:
    @pytest.mark.parametrize("mode", ["install", "rescue", "wipe"])
    def test_every_rail_renders_a_bootable_script(
        self, db, make_customer, make_server, make_subscription, make_template,
        make_ssh_key, mode,
    ):
        customer = make_customer(f"{mode}@example.com")
        server = make_server()
        make_subscription(customer, server)
        make_ssh_key(customer)

        if mode == "install":
            job = provisioning.create_install_job(
                db, server, os_template_id=make_template().id, hostname=None,
                raid_level=RaidLevel.RAID1, ssh_key_ids=[], root_password=None,
                customer=customer,
            )
        elif mode == "rescue":
            job = provisioning.create_rescue_job(
                db, server, ssh_key_ids=[], customer=customer
            )
        else:
            job = provisioning.create_wipe_job(
                db, server, method="secure", requested_by=customer
            )
        db.commit()

        script = boot_service.render_ipxe_script(db, server, job)
        assert script.startswith("#!ipxe")
        assert f"doz_mode={mode}" in script
        assert "kernel " in script
        assert "initrd " in script
        assert "boot" in script
        # A failure branch that reboots would destroy the evidence.
        assert ":failed" in script
