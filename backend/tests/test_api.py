"""API behaviour: authentication, ownership isolation, and the action endpoints."""

from __future__ import annotations

from app.enums import JobType, ServerState


class TestAuth:
    def test_login_and_me(self, client, make_customer, auth_header):
        customer = make_customer("alice@example.com")
        response = client.get("/api/v1/auth/me", headers=auth_header(customer))
        assert response.status_code == 200
        assert response.json()["email"] == "alice@example.com"

    def test_wrong_password_is_rejected(self, client, make_customer):
        make_customer("alice@example.com")
        response = client.post(
            "/api/v1/auth/login",
            json={"email": "alice@example.com", "password": "wrong"},
        )
        assert response.status_code == 401

    def test_unknown_account_gives_the_same_error(self, client):
        response = client.post(
            "/api/v1/auth/login",
            json={"email": "nobody@example.com", "password": "whatever"},
        )
        assert response.status_code == 401
        assert response.json()["detail"] == "invalid credentials"

    def test_no_token_is_rejected(self, client):
        assert client.get("/api/v1/servers").status_code == 401

    def test_api_token_authenticates_and_is_shown_once(
        self, client, make_customer, auth_header
    ):
        customer = make_customer()
        headers = auth_header(customer)

        created = client.post(
            "/api/v1/auth/tokens", json={"name": "ci"}, headers=headers
        )
        assert created.status_code == 201
        raw = created.json()["token"]
        assert raw.startswith("doz_")

        # The token works as a credential...
        response = client.get("/api/v1/servers", headers={"Authorization": f"Bearer {raw}"})
        assert response.status_code == 200

        # ...and is never returned again.
        listed = client.get("/api/v1/auth/tokens", headers=headers).json()
        assert "token" not in listed[0]

    def test_revoked_token_stops_working(self, client, make_customer, auth_header):
        customer = make_customer()
        headers = auth_header(customer)
        created = client.post(
            "/api/v1/auth/tokens", json={"name": "ci"}, headers=headers
        ).json()

        client.delete(f"/api/v1/auth/tokens/{created['id']}", headers=headers)
        response = client.get(
            "/api/v1/servers", headers={"Authorization": f"Bearer {created['token']}"}
        )
        assert response.status_code == 401


class TestOwnership:
    def test_customer_sees_only_their_servers(
        self, client, make_customer, make_server, make_subscription, auth_header
    ):
        alice = make_customer("alice@example.com")
        bob = make_customer("bob@example.com")
        alice_server = make_server()
        bob_server = make_server()
        make_subscription(alice, alice_server)
        make_subscription(bob, bob_server)

        listed = client.get("/api/v1/servers", headers=auth_header(alice)).json()
        assert [s["serial"] for s in listed] == [alice_server.serial]

    def test_another_customers_server_is_404_not_403(
        self, client, make_customer, make_server, make_subscription, auth_header
    ):
        """Whether a serial exists in the fleet is not a customer's business."""
        alice = make_customer("alice@example.com")
        bob = make_customer("bob@example.com")
        bob_server = make_server()
        make_subscription(bob, bob_server)

        response = client.get(
            f"/api/v1/servers/{bob_server.id}", headers=auth_header(alice)
        )
        assert response.status_code == 404

    def test_customer_cannot_power_another_customers_server(
        self, client, make_customer, make_server, make_subscription, auth_header
    ):
        alice = make_customer("alice@example.com")
        bob = make_customer("bob@example.com")
        bob_server = make_server()
        make_subscription(bob, bob_server)

        response = client.post(
            f"/api/v1/servers/{bob_server.id}/power",
            json={"action": "off"},
            headers=auth_header(alice),
        )
        assert response.status_code == 404

    def test_admin_endpoints_reject_customers(self, client, make_customer, auth_header):
        customer = make_customer()
        response = client.get("/api/v1/admin/servers", headers=auth_header(customer))
        assert response.status_code == 403


class TestPower:
    def test_power_queues_a_job(
        self, client, make_customer, make_server, make_subscription, auth_header, dispatched
    ):
        customer = make_customer()
        server = make_server()
        make_subscription(customer, server)

        response = client.post(
            f"/api/v1/servers/{server.id}/power",
            json={"action": "cycle"},
            headers=auth_header(customer),
        )
        assert response.status_code == 202
        body = response.json()
        assert body["type"] == JobType.POWER_CYCLE.value
        assert body["state"] == "queued"
        assert dispatched == [body["id"]]

    def test_unknown_action_is_rejected(
        self, client, make_customer, make_server, make_subscription, auth_header
    ):
        customer = make_customer()
        server = make_server()
        make_subscription(customer, server)

        response = client.post(
            f"/api/v1/servers/{server.id}/power",
            json={"action": "explode"},
            headers=auth_header(customer),
        )
        assert response.status_code == 422

    def test_suspended_server_refuses_customer_power(
        self, client, make_customer, make_server, make_subscription, auth_header
    ):
        customer = make_customer()
        server = make_server(state=ServerState.SUSPENDED)
        make_subscription(customer, server)

        response = client.post(
            f"/api/v1/servers/{server.id}/power",
            json={"action": "on"},
            headers=auth_header(customer),
        )
        assert response.status_code == 409

    def test_second_power_job_conflicts(
        self, client, make_customer, make_server, make_subscription, auth_header
    ):
        customer = make_customer()
        server = make_server()
        make_subscription(customer, server)
        headers = auth_header(customer)

        first = client.post(
            f"/api/v1/servers/{server.id}/power", json={"action": "off"}, headers=headers
        )
        assert first.status_code == 202
        second = client.post(
            f"/api/v1/servers/{server.id}/power", json={"action": "on"}, headers=headers
        )
        assert second.status_code == 409


class TestReinstall:
    def test_requires_explicit_confirmation(
        self,
        client,
        make_customer,
        make_server,
        make_subscription,
        make_template,
        make_ssh_key,
        auth_header,
    ):
        customer = make_customer()
        server = make_server()
        make_subscription(customer, server)
        make_ssh_key(customer)
        template = make_template()

        response = client.post(
            f"/api/v1/servers/{server.id}/reinstall",
            json={"os_template_id": str(template.id), "confirm_data_loss": False},
            headers=auth_header(customer),
        )
        assert response.status_code == 400
        assert "confirm_data_loss" in response.json()["detail"]

    def test_refuses_an_unreachable_install(
        self,
        client,
        make_customer,
        make_server,
        make_subscription,
        make_template,
        auth_header,
    ):
        """No SSH key and no root password means a server nobody can log into."""
        customer = make_customer()
        server = make_server()
        make_subscription(customer, server)
        template = make_template()

        response = client.post(
            f"/api/v1/servers/{server.id}/reinstall",
            json={"os_template_id": str(template.id), "confirm_data_loss": True},
            headers=auth_header(customer),
        )
        assert response.status_code == 400
        assert "SSH key" in response.json()["detail"]

    def test_happy_path_queues_an_install(
        self,
        client,
        db,
        make_customer,
        make_server,
        make_subscription,
        make_template,
        make_ssh_key,
        auth_header,
    ):
        from app.models import Job

        customer = make_customer()
        server = make_server()
        make_subscription(customer, server)
        make_ssh_key(customer)
        template = make_template()

        response = client.post(
            f"/api/v1/servers/{server.id}/reinstall",
            json={
                "os_template_id": str(template.id),
                "hostname": "web01",
                "raid_level": "raid1",
                "confirm_data_loss": True,
            },
            headers=auth_header(customer),
        )
        assert response.status_code == 202, response.text

        job = db.get(Job, response.json()["id"])
        assert job.payload["hostname"] == "web01"
        assert len(job.payload["ssh_keys"]) == 1
        assert job.callback_token_hash is not None

    def test_root_password_is_stored_hashed(
        self,
        client,
        db,
        make_customer,
        make_server,
        make_subscription,
        make_template,
        auth_header,
    ):
        from app.models import Job

        customer = make_customer()
        server = make_server()
        make_subscription(customer, server)
        template = make_template()
        secret = "a-very-long-root-password"

        response = client.post(
            f"/api/v1/servers/{server.id}/reinstall",
            json={
                "os_template_id": str(template.id),
                "root_password": secret,
                "confirm_data_loss": True,
            },
            headers=auth_header(customer),
        )
        assert response.status_code == 202, response.text

        job = db.get(Job, response.json()["id"])
        assert "root_password" not in job.payload
        assert secret not in str(job.payload)
        assert job.payload["root_password_hash"].startswith("$2")

    def test_server_without_a_mac_cannot_be_installed(
        self,
        client,
        make_customer,
        make_server,
        make_subscription,
        make_template,
        make_ssh_key,
        auth_header,
    ):
        customer = make_customer()
        server = make_server(provisioning_mac=None)
        make_subscription(customer, server)
        make_ssh_key(customer)
        template = make_template()

        response = client.post(
            f"/api/v1/servers/{server.id}/reinstall",
            json={"os_template_id": str(template.id), "confirm_data_loss": True},
            headers=auth_header(customer),
        )
        assert response.status_code == 400
        assert "inventory sync" in response.json()["detail"]

    def test_hidden_templates_are_not_offered_or_installable(
        self,
        client,
        make_customer,
        make_server,
        make_subscription,
        make_template,
        make_ssh_key,
        auth_header,
    ):
        customer = make_customer()
        server = make_server()
        make_subscription(customer, server)
        make_ssh_key(customer)
        make_template(slug="public-one")
        hidden = make_template(slug="_rescue-image", is_public=False)
        headers = auth_header(customer)

        listed = client.get("/api/v1/os-templates", headers=headers).json()
        assert [t["slug"] for t in listed] == ["public-one"]

        response = client.post(
            f"/api/v1/servers/{server.id}/reinstall",
            json={"os_template_id": str(hidden.id), "confirm_data_loss": True},
            headers=headers,
        )
        assert response.status_code == 400


class TestJobVisibility:
    def test_customers_do_not_see_raw_bmc_traffic(
        self, client, db, make_customer, make_server, make_subscription, auth_header
    ):
        from app.services import jobs as job_service

        customer = make_customer()
        server = make_server()
        make_subscription(customer, server)

        job, _ = job_service.create_job(
            db, job_type=JobType.POWER_ON, server_id=server.id
        )
        job_service.log(db, job, "rebooting", customer_visible=True)
        job_service.log(
            db,
            job,
            "POST /redfish/v1/Systems/1/Actions/ComputerSystem.Reset -> 204",
            request={"url": "https://10.10.99.1/redfish/v1/..."},
        )
        db.commit()

        body = client.get(f"/api/v1/jobs/{job.id}", headers=auth_header(customer)).json()
        messages = [e["message"] for e in body["log"]]
        assert "rebooting" in messages
        assert not any("redfish" in m for m in messages)

    def test_admin_sees_the_full_exchange(
        self, client, db, make_customer, make_server, auth_header
    ):
        from app.services import jobs as job_service

        admin = make_customer("admin@example.com", admin=True)
        server = make_server()
        job, _ = job_service.create_job(
            db, job_type=JobType.POWER_ON, server_id=server.id
        )
        job_service.log(
            db, job, "PATCH /redfish/v1/Systems/1 -> 200", request={"body": {"Boot": {}}}
        )
        db.commit()

        body = client.get(
            f"/api/v1/admin/jobs/{job.id}", headers=auth_header(admin)
        ).json()
        assert any(e["request"] for e in body["log"])


class TestSSHKeys:
    def test_add_list_delete(self, client, make_customer, auth_header):
        customer = make_customer()
        headers = auth_header(customer)
        key = (
            "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIB2jVQKmXPRzWZQmPTQrSVCPBTAkGDRr3ChJb9lQTKzE"
            " me@laptop"
        )

        created = client.post(
            "/api/v1/ssh-keys", json={"name": "laptop", "public_key": key}, headers=headers
        )
        assert created.status_code == 201
        assert created.json()["fingerprint"].startswith("SHA256:")

        assert len(client.get("/api/v1/ssh-keys", headers=headers).json()) == 1

        client.delete(f"/api/v1/ssh-keys/{created.json()['id']}", headers=headers)
        assert client.get("/api/v1/ssh-keys", headers=headers).json() == []

    def test_garbage_is_rejected(self, client, make_customer, auth_header):
        customer = make_customer()
        response = client.post(
            "/api/v1/ssh-keys",
            json={"name": "bad", "public_key": "-----BEGIN RSA PRIVATE KEY-----" + "x" * 40},
            headers=auth_header(customer),
        )
        assert response.status_code == 422

    def test_duplicate_key_is_rejected(self, client, make_customer, auth_header):
        customer = make_customer()
        headers = auth_header(customer)
        key = (
            "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIB2jVQKmXPRzWZQmPTQrSVCPBTAkGDRr3ChJb9lQTKzE"
            " me@laptop"
        )
        client.post(
            "/api/v1/ssh-keys", json={"name": "a", "public_key": key}, headers=headers
        )
        again = client.post(
            "/api/v1/ssh-keys", json={"name": "b", "public_key": key}, headers=headers
        )
        assert again.status_code == 409


class TestAdminGuards:
    def test_stock_requires_a_wipe(
        self, client, make_customer, make_server, auth_header
    ):
        admin = make_customer("admin@example.com", admin=True)
        server = make_server(state=ServerState.WIPING, last_wiped_at=None)

        response = client.post(
            f"/api/v1/admin/servers/{server.id}/state",
            json={"state": "in_stock"},
            headers=auth_header(admin),
        )
        assert response.status_code == 409
        assert "never been wiped" in response.json()["detail"]

    def test_illegal_transition_is_refused(
        self, client, make_customer, make_server, auth_header
    ):
        admin = make_customer("admin@example.com", admin=True)
        server = make_server(state=ServerState.RETIRED)

        response = client.post(
            f"/api/v1/admin/servers/{server.id}/state",
            json={"state": "active"},
            headers=auth_header(admin),
        )
        assert response.status_code == 409

    def test_registering_a_server_validates_the_credential_ref(
        self, client, make_customer, auth_header, monkeypatch
    ):
        from app.secrets import SecretNotFoundError

        admin = make_customer("admin@example.com", admin=True)

        class Broken:
            def get_bmc_credential(self, ref):  # noqa: ANN001
                raise SecretNotFoundError("nope")

        monkeypatch.setattr("app.api.admin.get_secrets_backend", lambda: Broken())

        response = client.post(
            "/api/v1/admin/servers",
            json={
                "serial": "FCH9999",
                "cimc_ip": "10.10.50.1",
                "cimc_credential_ref": "cimc/missing",
            },
            headers=auth_header(admin),
        )
        assert response.status_code == 400
        assert "does not resolve" in response.json()["detail"]
