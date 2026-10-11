"""The customer portal's side of the API: account, plan, kept readings, mail."""

from __future__ import annotations

import pytest

from app.config import settings
from app.enums import JobState, JobType
from app.services import mail, notify


@pytest.fixture
def customer_session(client, make_customer, auth_header):
    customer = make_customer("alice@example.com")
    return customer, auth_header(customer)


class TestAccount:
    def test_profile_and_notification_switch(self, client, customer_session):
        customer, headers = customer_session
        body = client.get("/api/v1/auth/me", headers=headers).json()
        assert body["notify_jobs"] is True and body["phone"] is None
        response = client.patch("/api/v1/auth/me", headers=headers, json={
            "contact_name": "Alice", "phone": "+61 400 000 000", "notify_jobs": False,
        })
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["contact_name"] == "Alice" and body["phone"] == "+61 400 000 000"
        assert body["notify_jobs"] is False
        # Email is the login and the billing key: not changeable here.
        client.patch("/api/v1/auth/me", headers=headers, json={"email": "x@example.com"})
        me = client.get("/api/v1/auth/me", headers=headers).json()
        assert me["email"] == "alice@example.com"

    def test_password_change_needs_the_current_one(self, client, customer_session):
        customer, headers = customer_session
        refused = client.post("/api/v1/auth/password", headers=headers, json={
            "current_password": "not-it", "new_password": "a-much-longer-password",
        })
        assert refused.status_code == 400 and "current password" in refused.json()["detail"]
        same = client.post("/api/v1/auth/password", headers=headers, json={
            "current_password": "correct-horse-battery-staple",
            "new_password": "correct-horse-battery-staple",
        })
        assert same.status_code == 400
        changed = client.post("/api/v1/auth/password", headers=headers, json={
            "current_password": "correct-horse-battery-staple",
            "new_password": "a-much-longer-password",
        })
        assert changed.status_code == 204, changed.text
        login = client.post("/api/v1/auth/login", json={
            "email": "alice@example.com", "password": "a-much-longer-password",
        })
        assert login.status_code == 200
        old = client.post("/api/v1/auth/login", json={
            "email": "alice@example.com", "password": "correct-horse-battery-staple",
        })
        assert old.status_code == 401


class TestServerView:
    def test_plan_and_primary_address_reach_the_customer(
        self, client, db, customer_session, make_server, make_subscription
    ):
        from app.models import IPAssignment, IPBlock

        customer, headers = customer_session
        server = make_server()
        make_subscription(customer, server, bandwidth_quota_tb=20)
        block = IPBlock(cidr="203.0.113.0/28", gateway="203.0.113.1", routing_mode="bridged")
        db.add(block)
        db.flush()
        db.add(IPAssignment(block_id=block.id, address="203.0.113.10", prefix_len=28,
                            server_id=server.id, is_primary=True))
        db.commit()
        listing = client.get("/api/v1/servers", headers=headers).json()
        assert listing[0]["primary_ip"] == "203.0.113.10"
        detail = client.get(f"/api/v1/servers/{server.id}", headers=headers).json()
        assert detail["plan"]["plan_name"] == "C220-M4-BASIC"
        assert detail["plan"]["bandwidth_quota_tb"] == 20
        assert detail["ip_addresses"][0]["gateway"] == "203.0.113.1"

    def test_kept_readings_without_bmc_detail(
        self, client, customer_session, make_server, make_subscription, monkeypatch
    ):
        from app.services import sensors as sensors_service

        customer, headers = customer_session
        server = make_server()
        make_subscription(customer, server)
        empty = client.get(f"/api/v1/servers/{server.id}/sensors", headers=headers).json()
        assert empty["sensors"] == [] and empty["checked_at"] is None
        monkeypatch.setattr(sensors_service, "load", lambda server_id: {
            "sensors": [{"name": "CPU1 Temp", "value": 41.0, "unit": "°C", "kind": "temperature",
                         "status": "ok"}],
            "power": {"watts": 150, "source": "dcmi"},
            "utilization": {"overall": 5, "cpu": 3, "memory": 20, "io": 1},
            "checked_at": "2026-10-11T10:00:00+00:00",
            "via": "redfish",
            "fallback_reason": "IPMI: Unable to establish session",
            "utilization_error": None,
        })
        body = client.get(f"/api/v1/servers/{server.id}/sensors", headers=headers).json()
        assert body["sensors"][0]["name"] == "CPU1 Temp" and body["power"]["watts"] == 150
        assert "fallback_reason" not in body and "utilization_error" not in body

    def test_someone_elses_server_is_not_there(self, client, customer_session, make_server):
        customer, headers = customer_session
        server = make_server()
        response = client.get(f"/api/v1/servers/{server.id}/sensors", headers=headers)
        assert response.status_code == 404


class TestMail:
    def test_nothing_is_sent_without_smtp(self, monkeypatch):
        monkeypatch.setattr(settings, "smtp_host", "")
        assert mail.configured() is False
        assert mail.send("a@example.com", "s", "b") is False

    def test_a_finished_reinstall_mails_the_holder(
        self, db, make_customer, make_server, make_subscription, monkeypatch
    ):
        from app.enums import ActorType
        from app.services import jobs as job_service

        sent: list[tuple[str, str, str]] = []
        monkeypatch.setattr(settings, "smtp_host", "smtp.example.com")
        monkeypatch.setattr(settings, "mail_from", "noreply@example.com")
        monkeypatch.setattr(settings, "portal_url", "https://portal.example.com")
        monkeypatch.setattr(mail, "send",
                            lambda to, subject, body: sent.append((to, subject, body)) or True)

        holder = make_customer("holder@example.com")
        quiet = make_customer("quiet@example.com")
        quiet.notify_jobs = False
        server = make_server(hostname="web01")
        make_subscription(holder, server)
        db.commit()

        job, _ = job_service.create_job(
            db, job_type=JobType.INSTALL, server_id=server.id, payload={},
            requested_by_id=quiet.id, requested_by_type=ActorType.CUSTOMER,
        )
        job_service.transition_job(db, job, JobState.RUNNING)
        job_service.transition_job(db, job, JobState.FAILED, error="installer did not check in")
        db.commit()

        assert notify.job_finished(job.id) == 1
        to, subject, body = sent[0]
        assert to == "holder@example.com"
        assert subject == "web01: reinstall failed"
        assert "Reason: installer did not check in" in body
        assert f"https://portal.example.com/jobs/{job.id}" in body

    def test_admin_only_work_is_never_mailed(self, db, make_customer, make_server,
                                             make_subscription, monkeypatch):
        from app.enums import ActorType
        from app.services import jobs as job_service

        sent: list = []
        monkeypatch.setattr(settings, "smtp_host", "smtp.example.com")
        monkeypatch.setattr(settings, "mail_from", "noreply@example.com")
        monkeypatch.setattr(mail, "send", lambda *a: sent.append(a) or True)
        holder = make_customer("holder@example.com")
        server = make_server()
        make_subscription(holder, server)
        job, _ = job_service.create_job(db, job_type=JobType.HEALTH_POLL, server_id=server.id,
                                        payload={}, requested_by_type=ActorType.SYSTEM)
        job_service.transition_job(db, job, JobState.RUNNING)
        job_service.transition_job(db, job, JobState.SUCCEEDED)
        db.commit()
        assert notify.job_finished(job.id) == 0 and sent == []
