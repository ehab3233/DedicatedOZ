"""The management panel's API: customers, subscriptions, IPAM, credentials."""

from __future__ import annotations

from app.enums import ServerState


class TestCustomers:
    def test_create_list_update(self, client, make_customer, auth_header):
        admin = make_customer("admin@example.com", admin=True)
        headers = auth_header(admin)

        created = client.post(
            "/api/v1/admin/customers",
            json={
                "email": "Buyer@Example.com",
                "password": "a-strong-initial-password",
                "company_name": "Buyer Ltd",
            },
            headers=headers,
        )
        assert created.status_code == 201, created.text
        assert created.json()["email"] == "buyer@example.com"

        listed = client.get("/api/v1/admin/customers", headers=headers).json()
        assert {c["email"] for c in listed} == {"admin@example.com", "buyer@example.com"}

        # The new customer can log in with what we set...
        login = client.post(
            "/api/v1/auth/login",
            json={"email": "buyer@example.com", "password": "a-strong-initial-password"},
        )
        assert login.status_code == 200

        # ...and cannot after we disable them.
        client.patch(
            f"/api/v1/admin/customers/{created.json()['id']}",
            json={"is_active": False},
            headers=headers,
        )
        login = client.post(
            "/api/v1/auth/login",
            json={"email": "buyer@example.com", "password": "a-strong-initial-password"},
        )
        assert login.status_code == 401

    def test_duplicate_email_is_rejected(self, client, make_customer, auth_header):
        admin = make_customer("admin@example.com", admin=True)
        make_customer("buyer@example.com")
        response = client.post(
            "/api/v1/admin/customers",
            json={"email": "buyer@example.com", "password": "a-strong-initial-password"},
            headers=auth_header(admin),
        )
        assert response.status_code == 409

    def test_short_password_is_rejected(self, client, make_customer, auth_header):
        admin = make_customer("admin@example.com", admin=True)
        response = client.post(
            "/api/v1/admin/customers",
            json={"email": "buyer@example.com", "password": "short"},
            headers=auth_header(admin),
        )
        assert response.status_code == 422


class TestSubscriptions:
    def test_assigning_a_server_makes_it_visible_to_the_customer(
        self, client, make_customer, make_server, auth_header
    ):
        admin = make_customer("admin@example.com", admin=True)
        buyer = make_customer("buyer@example.com")
        server = make_server(state=ServerState.IN_STOCK)

        before = client.get("/api/v1/servers", headers=auth_header(buyer)).json()
        assert before == []

        response = client.post(
            "/api/v1/admin/subscriptions",
            json={
                "customer_id": str(buyer.id),
                "server_id": str(server.id),
                "plan_name": "C220-BASIC",
                "monthly_price": 79.0,
            },
            headers=auth_header(admin),
        )
        assert response.status_code == 201, response.text
        assert response.json()["customer_email"] == "buyer@example.com"

        after = client.get("/api/v1/servers", headers=auth_header(buyer)).json()
        assert [s["serial"] for s in after] == [server.serial]

    def test_one_active_subscription_per_server(
        self, client, make_customer, make_server, make_subscription, auth_header
    ):
        admin = make_customer("admin@example.com", admin=True)
        alice = make_customer("alice@example.com")
        bob = make_customer("bob@example.com")
        server = make_server()
        make_subscription(alice, server)

        response = client.post(
            "/api/v1/admin/subscriptions",
            json={"customer_id": str(bob.id), "server_id": str(server.id), "plan_name": "X"},
            headers=auth_header(admin),
        )
        assert response.status_code == 409

    def test_cannot_assign_a_server_mid_wipe(
        self, client, make_customer, make_server, auth_header
    ):
        admin = make_customer("admin@example.com", admin=True)
        buyer = make_customer("buyer@example.com")
        server = make_server(state=ServerState.WIPING)

        response = client.post(
            "/api/v1/admin/subscriptions",
            json={"customer_id": str(buyer.id), "server_id": str(server.id), "plan_name": "X"},
            headers=auth_header(admin),
        )
        assert response.status_code == 409

    def test_ending_removes_customer_access(
        self, client, make_customer, make_server, make_subscription, auth_header
    ):
        admin = make_customer("admin@example.com", admin=True)
        buyer = make_customer("buyer@example.com")
        server = make_server()
        sub = make_subscription(buyer, server)

        ended = client.post(
            f"/api/v1/admin/subscriptions/{sub.id}/end", headers=auth_header(admin)
        )
        assert ended.status_code == 200
        assert ended.json()["ended_at"] is not None

        response = client.get(f"/api/v1/servers/{server.id}", headers=auth_header(buyer))
        assert response.status_code == 404

    def test_admin_reinstall_uses_the_customers_keys(
        self, client, db, make_customer, make_server, make_subscription, make_template,
        make_ssh_key, auth_header,
    ):
        """An admin reinstalling a customer's box must not install their own keys."""
        from app.models import Job, SSHKey

        admin = make_customer("admin@example.com", admin=True)
        buyer = make_customer("buyer@example.com")
        server = make_server()
        make_subscription(buyer, server)
        make_ssh_key(buyer, name="buyer-laptop")
        # Give the admin a distinguishable key.
        admin_key = SSHKey(
            customer_id=admin.id,
            name="admin-key",
            public_key=(
                "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAdminAdminAdminAdminAdminAdminAdmin admin"
            ),
            fingerprint="SHA256:admin",
        )
        db.add(admin_key)
        db.commit()
        template = make_template()

        response = client.post(
            f"/api/v1/servers/{server.id}/reinstall",
            json={"os_template_id": str(template.id), "confirm_data_loss": True},
            headers=auth_header(admin),
        )
        assert response.status_code == 202, response.text

        job = db.get(Job, response.json()["id"])
        assert len(job.payload["ssh_keys"]) == 1
        assert "Admin" not in job.payload["ssh_keys"][0]


class TestIPAM:
    def test_create_block_and_assign(self, client, make_customer, make_server, auth_header):
        admin = make_customer("admin@example.com", admin=True)
        server = make_server()
        headers = auth_header(admin)

        block = client.post(
            "/api/v1/admin/ip-blocks",
            json={"cidr": "203.0.113.0/28", "gateway": "203.0.113.1", "source": "test"},
            headers=headers,
        )
        assert block.status_code == 201, block.text
        assert block.json()["total_hosts"] == 14

        free = client.get(
            f"/api/v1/admin/ip-blocks/{block.json()['id']}/free", headers=headers
        ).json()
        assert "203.0.113.1" not in free["free_sample"]  # gateway excluded
        assert free["free_sample"][0] == "203.0.113.2"

        assigned = client.post(
            f"/api/v1/admin/servers/{server.id}/ips",
            json={"block_id": block.json()["id"], "address": "203.0.113.2", "is_primary": True},
            headers=headers,
        )
        assert assigned.status_code == 201, assigned.text
        assert assigned.json()["gateway"] == "203.0.113.1"

        listed = client.get("/api/v1/admin/ip-blocks", headers=headers).json()
        assert listed[0]["assigned"] == 1

    def test_bad_cidr_and_foreign_gateway_are_rejected(self, client, make_customer, auth_header):
        admin = make_customer("admin@example.com", admin=True)
        headers = auth_header(admin)
        assert client.post(
            "/api/v1/admin/ip-blocks", json={"cidr": "not-a-cidr"}, headers=headers
        ).status_code == 400
        assert client.post(
            "/api/v1/admin/ip-blocks",
            json={"cidr": "203.0.113.0/24", "gateway": "198.51.100.1"},
            headers=headers,
        ).status_code == 400

    def test_address_outside_block_is_rejected(
        self, client, make_customer, make_server, auth_header
    ):
        admin = make_customer("admin@example.com", admin=True)
        server = make_server()
        headers = auth_header(admin)
        block = client.post(
            "/api/v1/admin/ip-blocks", json={"cidr": "203.0.113.0/24"}, headers=headers
        ).json()
        response = client.post(
            f"/api/v1/admin/servers/{server.id}/ips",
            json={"block_id": block["id"], "address": "198.51.100.7", "is_primary": False},
            headers=headers,
        )
        assert response.status_code == 400


class TestCustomerIPView:
    def test_customer_can_read_their_addresses(
        self, client, make_customer, make_server, make_subscription, auth_header
    ):
        """Regression: INET columns come back as ipaddress objects, not str."""
        admin = make_customer("admin@example.com", admin=True)
        buyer = make_customer("buyer@example.com")
        server = make_server()
        make_subscription(buyer, server)
        block = client.post(
            "/api/v1/admin/ip-blocks",
            json={"cidr": "203.0.113.0/24", "gateway": "203.0.113.1"},
            headers=auth_header(admin),
        ).json()
        client.post(
            f"/api/v1/admin/servers/{server.id}/ips",
            json={"block_id": block["id"], "address": "203.0.113.10", "is_primary": True},
            headers=auth_header(admin),
        )

        detail = client.get(f"/api/v1/servers/{server.id}", headers=auth_header(buyer))
        assert detail.status_code == 200, detail.text
        assert detail.json()["ip_addresses"][0]["address"] == "203.0.113.10"
        assert detail.json()["ip_addresses"][0]["gateway"] == "203.0.113.1"

        ips = client.get(f"/api/v1/servers/{server.id}/ips", headers=auth_header(buyer))
        assert ips.status_code == 200
        assert ips.json()[0]["address"] == "203.0.113.10"


class TestCredentials:
    def test_env_backend_reports_read_only(self, client, make_customer, auth_header):
        admin = make_customer("admin@example.com", admin=True)
        headers = auth_header(admin)
        info = client.get("/api/v1/admin/credentials/backend", headers=headers).json()
        assert info == {"backend": "env", "writable": False}

        response = client.post(
            "/api/v1/admin/credentials",
            json={"ref": "cimc/X", "username": "admin", "password": "pw"},
            headers=headers,
        )
        assert response.status_code == 409

    def test_check_never_returns_the_password(self, client, make_customer, auth_header):
        admin = make_customer("admin@example.com", admin=True)
        response = client.get(
            "/api/v1/admin/credentials/check",
            params={"ref": "cimc/anything"},
            headers=auth_header(admin),
        ).json()
        # The env backend has a fleet-wide default in the test environment.
        assert response["resolves"] is True
        assert "password" not in response
        assert "bench-password" not in str(response)

    def test_file_backend_round_trip(self, tmp_path, monkeypatch):
        from app.secrets import BMCCredential, FileSecretsBackend

        monkeypatch.setattr("app.secrets.settings.secrets_file_dir", str(tmp_path))
        backend = FileSecretsBackend()
        backend.put_bmc_credential("cimc/FCH1", BMCCredential("admin", "s3cret"))
        assert backend.get_bmc_credential("cimc/FCH1").password == "s3cret"
        assert oct((tmp_path / "cimc" / "FCH1.json").stat().st_mode & 0o777) == "0o600"

    def test_file_backend_refuses_path_escape(self, tmp_path, monkeypatch):
        import pytest

        from app.secrets import FileSecretsBackend

        monkeypatch.setattr("app.secrets.settings.secrets_file_dir", str(tmp_path))
        with pytest.raises(ValueError):
            FileSecretsBackend().get_bmc_credential("../etc/passwd")


class TestSuspendCycle:
    def test_suspend_then_unsuspend(self, client, make_customer, make_server, auth_header):
        admin = make_customer("admin@example.com", admin=True)
        server = make_server(state=ServerState.ACTIVE)
        headers = auth_header(admin)

        suspended = client.post(
            f"/api/v1/admin/servers/{server.id}/suspend",
            params={"reason": "abuse ticket 42"},
            headers=headers,
        )
        assert suspended.status_code == 200
        assert suspended.json()["state"] == "suspended"

        restored = client.post(
            f"/api/v1/admin/servers/{server.id}/unsuspend", headers=headers
        )
        assert restored.status_code == 200
        assert restored.json()["state"] == "active"

        audit = client.get(
            "/api/v1/admin/audit", params={"target_id": str(server.id)}, headers=headers
        ).json()
        actions = [e["action"] for e in audit]
        assert "server.suspended" in actions and "server.unsuspended" in actions


class TestBootEntry:
    def test_bare_entry_hands_out_a_chain_script(self, client):
        response = client.get("/boot/ipxe")
        assert response.status_code == 200
        assert "chain http://10.10.0.5:8000/boot/ipxe?mac=${net0/mac}" in response.text

    def test_nocloud_seed_serves_user_and_meta_data(
        self, client, db, make_customer, make_server, make_subscription, make_template,
        make_ssh_key,
    ):
        from app.enums import RaidLevel
        from app.security import boot_signature
        from app.services import provisioning

        customer = make_customer()
        server = make_server()
        make_subscription(customer, server)
        make_ssh_key(customer)
        template = make_template()
        provisioning.create_install_job(
            db, server, os_template_id=template.id, hostname="web01",
            raid_level=RaidLevel.RAID1, ssh_key_ids=[], root_password=None, customer=customer,
        )
        db.commit()

        mac = server.provisioning_mac
        sig = boot_signature(mac)
        user_data = client.get(f"/boot/nocloud/{mac}/{sig}/user-data")
        assert user_data.status_code == 200
        assert user_data.text.startswith("#cloud-config")

        meta = client.get(f"/boot/nocloud/{mac}/{sig}/meta-data")
        assert meta.status_code == 200
        assert "local-hostname: web01" in meta.text

        assert client.get(f"/boot/nocloud/{mac}/wrong-sig/user-data").status_code == 403
