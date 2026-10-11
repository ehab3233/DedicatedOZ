"""Customer KVM access: temporary CIMC users, grants, and nginx's question."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import jwt
import pytest
import responses

from app.config import settings
from app.drivers.base import BMCError
from app.drivers.cimc import CimcXmlApi
from app.secrets import BMCCredential
from app.security import create_kvm_ticket, decode_kvm_ticket
from app.services import kvm_access

HOST = "10.10.99.7"
NUOVA = f"https://{HOST}/nuova"
CRED = BMCCredential(username="admin", password="bench-password")


def _login_ok(rsps):
    rsps.add(rsps.POST, NUOVA,
             body='<aaaLogin response="yes" outCookie="cookie-1" outVersion="4.1(2g)"/>')


def _logout_ok(rsps):
    rsps.add(rsps.POST, NUOVA, body='<aaaLogout response="yes" outStatus="success"/>')


def _users(rsps, *slots):
    rows = "".join(
        f'<aaaUser dn="sys/user-ext/user-{i}" id="{i}" name="{name}" priv="{priv}" '
        f'accountStatus="{status}"/>'
        for i, name, priv, status in slots
    )
    rsps.add(rsps.POST, NUOVA, body=(
        f'<configResolveClass response="yes" classId="aaaUser"><outConfigs>{rows}'
        '</outConfigs></configResolveClass>'
    ))


class TestCimcUsers:
    @responses.activate
    def test_a_user_goes_into_the_first_free_slot_never_slot_one(self):
        _login_ok(responses)
        _users(responses, (1, "admin", "admin", "active"), (2, "ops", "user", "active"),
               (3, "", "read-only", "inactive"))
        _users(responses, (1, "admin", "admin", "active"), (2, "ops", "user", "active"),
               (3, "", "read-only", "inactive"))
        responses.add(responses.POST, NUOVA, body=(
            '<configConfMo response="yes"><outConfig><aaaUser dn="sys/user-ext/user-3" '
            'id="3" name="doz1234abcd" priv="user" accountStatus="active"/></outConfig>'
            '</configConfMo>'
        ))
        _logout_ok(responses)
        with CimcXmlApi(HOST, CRED) as api:
            created = api.create_user("doz1234abcd", "Secret1234abcd56")
        assert created["id"] == "3"
        body = responses.calls[3].request.body.decode()
        assert ('<aaaUser dn="sys/user-ext/user-3" name="doz1234abcd" pwd="Secret1234abcd56" '
                'priv="user" accountStatus="active" />') in body

    @responses.activate
    def test_a_full_cimc_is_an_error_not_an_overwrite(self):
        _login_ok(responses)
        full = tuple((i, f"u{i}", "user", "active") for i in range(1, 16))
        _users(responses, *full)
        _users(responses, *full)
        _logout_ok(responses)
        with pytest.raises(BMCError, match="no free user slot"):
            with CimcXmlApi(HOST, CRED) as api:
                api.create_user("doznew", "Secret1234abcd56")

    @responses.activate
    def test_removing_frees_the_slot_and_a_missing_user_is_not_an_error(self):
        _login_ok(responses)
        _users(responses, (1, "admin", "admin", "active"), (4, "doz1234abcd", "user", "active"))
        responses.add(responses.POST, NUOVA, body=(
            '<configConfMo response="yes"><outConfig><aaaUser dn="sys/user-ext/user-4" '
            'id="4" name="" accountStatus="inactive"/></outConfig></configConfMo>'
        ))
        _users(responses, (1, "admin", "admin", "active"))
        _logout_ok(responses)
        with CimcXmlApi(HOST, CRED) as api:
            assert api.remove_user("doz1234abcd") is True
            assert api.remove_user("doz1234abcd") is False
        body = responses.calls[2].request.body.decode()
        assert 'accountStatus="inactive"' in body and 'pwd="***"' not in body

    def test_passwords_never_reach_the_log(self):
        from app.drivers.cimc import _redact_xml

        assert 'pwd="***"' in _redact_xml('<aaaUser name="x" pwd="Secret1234abcd56"/>')


class TestTicket:
    def test_round_trip_and_type_check(self, make_customer):
        customer = make_customer("alice@example.com")
        ticket = create_kvm_ticket(customer.id, hours=1)
        assert decode_kvm_ticket(ticket)["sub"] == str(customer.id)
        from app.security import create_access_token

        session, _ = create_access_token(customer.id, False)
        with pytest.raises(jwt.InvalidTokenError):
            decode_kvm_ticket(session)


@pytest.fixture
def portal_domain(monkeypatch):
    monkeypatch.setattr(settings, "portal_domain", "portal.example.com")
    monkeypatch.setattr(settings, "kvm_grant_hours", 2)


class FakeCimc:
    """The XML API as the grant service uses it: users come and go."""

    users: dict[str, str] = {}
    calls: list[tuple] = []
    fail = False

    def __init__(self, *a, **k) -> None:
        pass

    def __enter__(self):
        if FakeCimc.fail:
            raise BMCError("CIMC XML API unreachable")
        return self

    def __exit__(self, *exc):
        return False

    def create_user(self, name, password, *, priv="user"):
        FakeCimc.users[name] = password
        FakeCimc.calls.append(("create", name, priv))
        return {"name": name}

    def set_user_password(self, name, password):
        if name not in FakeCimc.users:
            raise BMCError("no such user")
        FakeCimc.users[name] = password
        FakeCimc.calls.append(("password", name))
        return {"name": name}

    def remove_user(self, name):
        FakeCimc.calls.append(("remove", name))
        return FakeCimc.users.pop(name, None) is not None


@pytest.fixture
def fake_cimc(monkeypatch):
    FakeCimc.users, FakeCimc.calls, FakeCimc.fail = {}, [], False
    monkeypatch.setattr(kvm_access, "CimcXmlApi", FakeCimc)
    monkeypatch.setattr(kvm_access, "credential_for", lambda server: CRED)
    return FakeCimc


@pytest.fixture
def owned(client, db, make_customer, make_server, make_subscription, auth_header):
    customer = make_customer("alice@example.com")
    server = make_server()
    make_subscription(customer, server)
    return customer, server, auth_header(customer)


class TestGrants:
    def test_not_set_up_is_said_plainly(self, client, owned, monkeypatch):
        monkeypatch.setattr(settings, "portal_domain", "")
        customer, server, headers = owned
        body = client.get(f"/api/v1/servers/{server.id}/kvm", headers=headers).json()
        assert body["available"] is False and "not set up" in body["reason"]
        response = client.post(f"/api/v1/servers/{server.id}/kvm", headers=headers)
        assert response.status_code == 400

    def test_opening_creates_a_cimc_user_a_hostname_and_the_cookie(
        self, client, db, owned, portal_domain, fake_cimc
    ):
        customer, server, headers = owned
        response = client.post(f"/api/v1/servers/{server.id}/kvm", headers=headers)
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["hostname"].startswith("kvm-")
        assert body["hostname"].endswith(".portal.example.com")
        assert body["url"] == f"https://{body['hostname']}/"
        assert body["username"].startswith("doz") and len(body["password"]) == 16
        assert fake_cimc.users[body["username"]] == body["password"]
        assert fake_cimc.calls == [("create", body["username"], "user")]
        cookie = response.headers["set-cookie"]
        assert cookie.startswith("doz_kvm=") and "Domain=.portal.example.com" in cookie
        assert "HttpOnly" in cookie and "Secure" in cookie and "Max-Age=7200" in cookie

        # Opening again renews rather than making a second user.
        again = client.post(f"/api/v1/servers/{server.id}/kvm", headers=headers).json()
        assert again["hostname"] == body["hostname"] and again["username"] == body["username"]
        assert again["password"] != body["password"]
        assert fake_cimc.calls[-1] == ("password", body["username"])
        status = client.get(f"/api/v1/servers/{server.id}/kvm", headers=headers).json()
        assert status["grant"]["hostname"] == body["hostname"]
        assert status["grant"]["password"] is None  # never stored, never shown again

    def test_a_bmc_that_refuses_is_a_clear_error(self, client, owned, portal_domain, fake_cimc):
        customer, server, headers = owned
        fake_cimc.fail = True
        response = client.post(f"/api/v1/servers/{server.id}/kvm", headers=headers)
        assert response.status_code == 400
        assert "did not accept a console user" in response.json()["detail"]

    def test_closing_removes_the_user_and_the_sweep_cleans_expired_ones(
        self, client, db, owned, portal_domain, fake_cimc
    ):
        from sqlalchemy import select

        from app.models import KvmGrant

        customer, server, headers = owned
        opened = client.post(f"/api/v1/servers/{server.id}/kvm", headers=headers).json()
        closed = client.delete(f"/api/v1/servers/{server.id}/kvm", headers=headers)
        assert closed.status_code == 204
        assert ("remove", opened["username"]) in fake_cimc.calls
        assert opened["username"] not in fake_cimc.users
        status = client.get(f"/api/v1/servers/{server.id}/kvm", headers=headers).json()
        assert status["grant"] is None

        reopened = client.post(f"/api/v1/servers/{server.id}/kvm", headers=headers).json()
        label = reopened["hostname"].split(".")[0]
        record = db.execute(select(KvmGrant).where(KvmGrant.label == label)).scalar_one()
        record.expires_at = datetime.now(UTC) - timedelta(minutes=1)
        db.commit()
        assert kvm_access.expire_due(db) == 1
        db.commit()
        assert reopened["username"] not in fake_cimc.users
        db.refresh(record)
        assert record.cleaned_at is not None


class TestAuthorize:
    """What nginx asks on every request to kvm-<label>.<domain>."""

    def _open(self, client, server, headers):
        response = client.post(f"/api/v1/servers/{server.id}/kvm", headers=headers)
        body = response.json()
        ticket = response.headers["set-cookie"].split(";")[0].split("=", 1)[1]
        return body, ticket

    def test_the_right_browser_is_sent_to_the_right_cimc(self, client, owned, portal_domain,
                                                        fake_cimc, make_server):
        customer, server, headers = owned
        body, ticket = self._open(client, server, headers)
        response = client.get("/api/v1/kvm/authorize", headers={
            "X-KVM-Host": body["hostname"], "Cookie": f"other=1; doz_kvm={ticket}",
        })
        assert response.status_code == 204, response.text
        assert response.headers["X-Upstream"] == f"https://{server.cimc_ip}"
        assert response.headers["X-Upstream-Host"] == str(server.cimc_ip)

    def test_everything_else_is_refused(self, client, db, owned, portal_domain, fake_cimc,
                                        make_customer, auth_header, make_server, make_subscription):
        customer, server, headers = owned
        body, ticket = self._open(client, server, headers)
        host = body["hostname"]
        # No cookie, wrong host, another customer's ticket, an expired grant.
        assert client.get("/api/v1/kvm/authorize", headers={"X-KVM-Host": host}).status_code == 401
        assert client.get("/api/v1/kvm/authorize", headers={
            "X-KVM-Host": "kvm-nope.portal.example.com", "Cookie": f"doz_kvm={ticket}",
        }).status_code == 401
        other = make_customer("mallory@example.com")
        other_ticket = create_kvm_ticket(other.id, hours=1)
        assert client.get("/api/v1/kvm/authorize", headers={
            "X-KVM-Host": host, "Cookie": f"doz_kvm={other_ticket}",
        }).status_code == 401
        assert client.get("/api/v1/kvm/authorize", headers={
            "X-KVM-Host": host, "Cookie": "doz_kvm=not-a-ticket",
        }).status_code == 401
        client.delete(f"/api/v1/servers/{server.id}/kvm", headers=headers)
        assert client.get("/api/v1/kvm/authorize", headers={
            "X-KVM-Host": host, "Cookie": f"doz_kvm={ticket}",
        }).status_code == 401
