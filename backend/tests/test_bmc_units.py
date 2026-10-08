"""BMC plumbing that does not need a simulator: CIMC XML API, endpoints, schema."""

from __future__ import annotations

import pytest
import responses

from app.drivers.base import BMCError
from app.drivers.cimc import CimcXmlApi
from app.enums import JobType, ServerState
from app.secrets import BMCCredential

HOST = "10.10.99.1"
NUOVA = f"https://{HOST}/nuova"
CRED = BMCCredential("admin", 'p&ss"word<')


def _login_ok(rsps):
    rsps.add(
        rsps.POST, NUOVA,
        body='<aaaLogin cookie="" response="yes" outCookie="1690000000/abc-123" '
             'outRefreshPeriod="600" outPriv="admin" outVersion="4.1(2f)"> </aaaLogin>',
    )


def _logout_ok(rsps):
    rsps.add(rsps.POST, NUOVA, body='<aaaLogout cookie="" response="yes" outStatus="success"/>')


class TestCimcXmlApi:
    @responses.activate
    def test_login_escapes_credentials_and_logs_out(self):
        _login_ok(responses)
        _logout_ok(responses)
        with CimcXmlApi(HOST, CRED) as api:
            assert api.version == "4.1(2f)"
        login_body = responses.calls[0].request.body.decode()
        # The password is quoted as an XML attribute, not pasted raw.
        assert 'inPassword="p&amp;ss&quot;word&lt;"' in login_body
        assert "aaaLogout" in responses.calls[1].request.body.decode()
        assert responses.calls[0].request.headers["Content-Type"] == (
            "application/x-www-form-urlencoded"
        )

    @responses.activate
    def test_failed_login_raises_with_the_cimc_reason(self):
        responses.add(
            responses.POST, NUOVA,
            body='<aaaLogin cookie="" response="yes" errorCode="551" '
                 'invocationResult="unidentified-fail" errorDescr="Authorization required"/>',
        )
        with pytest.raises(BMCError, match="Authorization required"):
            with CimcXmlApi(HOST, CRED):
                pass

    @responses.activate
    def test_enable_sol_sends_the_right_object(self):
        _login_ok(responses)
        responses.add(
            responses.POST, NUOVA,
            body='<configResolveDn dn="sys/rack-unit-1/sol-if" cookie="x" response="yes">'
                 '<outConfig><solIf dn="sys/rack-unit-1/sol-if" adminState="disable" '
                 'speed="9600" comport="com0"/></outConfig></configResolveDn>',
        )
        responses.add(
            responses.POST, NUOVA,
            body='<configConfMo dn="sys/rack-unit-1/sol-if" cookie="x" response="yes">'
                 '<outConfig><solIf dn="sys/rack-unit-1/sol-if" adminState="enable" '
                 'speed="115200" comport="com0"/></outConfig></configConfMo>',
        )
        _logout_ok(responses)
        with CimcXmlApi(HOST, CRED) as api:
            changed, out = api.enable_sol()
        read, body = (responses.calls[i].request.body.decode() for i in (1, 2))
        assert '<configResolveDn' in read and 'dn="sys/rack-unit-1/sol-if"' in read
        assert '<solIf dn="sys/rack-unit-1/sol-if" adminState="enable"' in body
        assert 'speed="115200"' in body and 'comport="com0"' in body
        assert changed and out["adminState"] == "enable"

    @responses.activate
    def test_ipmi_over_lan_and_console_redirection_objects(self):
        _login_ok(responses)
        # An empty read (the CIMC has nothing at the DN) means write.
        for _ in range(4):
            responses.add(
                responses.POST, NUOVA,
                body='<configConfMo response="yes"><outConfig/></configConfMo>',
            )
        _logout_ok(responses)
        with CimcXmlApi(HOST, CRED) as api:
            assert api.enable_ipmi_over_lan()[0]
            assert api.set_console_redirection()[0]
        ipmi = responses.calls[2].request.body.decode()
        bios = responses.calls[4].request.body.decode()
        assert (
            '<commIpmiLan dn="sys/svc-ext/ipmi-lan-svc" adminState="enabled" priv="admin"'
            in ipmi
        )
        assert "biosVfConsoleRedirection" in bios and 'vpConsoleRedirection="com-0"' in bios
        assert 'vpTerminalType="vt100-plus"' in bios

    @responses.activate
    def test_kvm_launch_probes_for_the_html5_viewer(self):
        _login_ok(responses)
        responses.add(
            responses.POST, NUOVA,
            body='<aaaGetComputeAuthTokens cookie="x" outTokens="1804289383,846930886" '
                 'response="yes"/>',
        )
        responses.add(responses.GET, f"https://{HOST}/html/kvmViewer.html", status=404)
        responses.add(responses.GET, f"https://{HOST}/html/kvm.html", status=200, body="<html>")
        _logout_ok(responses)
        with CimcXmlApi(HOST, CRED) as api:
            links = api.kvm_launch()
        assert links["html5"] == f"https://{HOST}/html/kvm.html?tkn1=1804289383&tkn2=846930886"
        assert links["java"].startswith(f"https://{HOST}/kvm.jnlp?cimcAddr={HOST}&tkn1=")
        assert links["cimc"] == f"https://{HOST}/"

    @responses.activate
    def test_no_viewer_found_still_offers_java_and_the_web_ui(self):
        _login_ok(responses)
        responses.add(responses.POST, NUOVA,
                      body='<aaaGetComputeAuthTokens outTokens="1,2" response="yes"/>')
        for path in ("/html/kvmViewer.html", "/html/kvm.html", "/kvmViewer.html"):
            responses.add(responses.GET, f"https://{HOST}{path}", status=404)
        _logout_ok(responses)
        with CimcXmlApi(HOST, CRED) as api:
            links = api.kvm_launch()
        assert links["html5"] is None and links["java"] and links["cimc"]

    @responses.activate
    def test_passwords_and_cookies_never_reach_the_log(self):
        captured = []
        _login_ok(responses)
        _logout_ok(responses)
        with CimcXmlApi(HOST, CRED,
                        log=lambda m, level="info", request=None, response=None:
                        captured.append((request, response))):
            pass
        assert 'p&amp;ss' not in str(captured) and "abc-123" not in str(captured)


class TestPrepareObjects:
    """The XML sent for each Prepare BMC step, against the names Cisco's SDK uses,
    and the read-before-write that makes a re-run a no-op."""

    def _confmo_ok(self, dn: str, cls: str, **attrs: str) -> None:
        attributes = " ".join(f'{k}="{v}"' for k, v in attrs.items())
        responses.add(
            responses.POST, NUOVA,
            body=f'<configConfMo dn="{dn}" cookie="x" response="yes"><outConfig>'
                 f'<{cls} dn="{dn}" {attributes}/></outConfig></configConfMo>',
        )

    def _resolve_ok(self, dn: str, cls: str, **attrs: str) -> None:
        attributes = " ".join(f'{k}="{v}"' for k, v in attrs.items())
        responses.add(
            responses.POST, NUOVA,
            body=f'<configResolveDn dn="{dn}" cookie="x" response="yes"><outConfig>'
                 f'<{cls} dn="{dn}" {attributes}/></outConfig></configResolveDn>',
        )

    @responses.activate
    def test_services_vmedia_kvm_redfish_ntp(self):
        _login_ok(responses)
        self._resolve_ok("sys/svc-ext/vmedia-svc", "commVMedia", adminState="disabled")
        self._confmo_ok("sys/svc-ext/vmedia-svc", "commVMedia", adminState="enabled")
        self._resolve_ok("sys/svc-ext/kvm-svc", "commKvm", adminState="disabled", port="2068")
        self._confmo_ok("sys/svc-ext/kvm-svc", "commKvm", adminState="enabled")
        self._resolve_ok("sys/svc-ext/redfish-svc", "commRedfish", adminState="disabled")
        self._confmo_ok("sys/svc-ext/redfish-svc", "commRedfish", adminState="enabled")
        self._resolve_ok("sys/svc-ext/ntp-svc", "commNtpProvider", ntpEnable="no")
        self._confmo_ok("sys/svc-ext/ntp-svc", "commNtpProvider", ntpEnable="yes")
        _logout_ok(responses)
        with CimcXmlApi(HOST, CRED) as api:
            changed, attrs = api.enable_vmedia()
            assert changed and attrs["adminState"] == "enabled"
            assert api.enable_kvm()[0]
            assert api.enable_redfish()[0]
            assert api.set_ntp(["10.0.0.5", "pool.ntp.org"])[0]
        bodies = [c.request.body.decode() for c in responses.calls[1:9]]
        assert '<configResolveDn' in bodies[0] and 'dn="sys/svc-ext/vmedia-svc"' in bodies[0]
        assert '<commVMedia dn="sys/svc-ext/vmedia-svc" adminState="enabled"' in bodies[1]
        assert '<commKvm dn="sys/svc-ext/kvm-svc" adminState="enabled" port="2068"' in bodies[3]
        assert '<commRedfish dn="sys/svc-ext/redfish-svc" adminState="enabled"' in bodies[5]
        assert 'ntpEnable="yes" ntpServer1="10.0.0.5" ntpServer2="pool.ntp.org"' in bodies[7]

    @responses.activate
    def test_a_setting_the_cimc_already_has_is_not_rewritten(self):
        # Rewriting IPMI over LAN restarts the CIMC's IPMI service, which is
        # what made the panel lose a server after a second Prepare BMC.
        _login_ok(responses)
        self._resolve_ok("sys/svc-ext/ipmi-lan-svc", "commIpmiLan",
                         adminState="enabled", priv="admin", key="0" * 40)
        _logout_ok(responses)
        with CimcXmlApi(HOST, CRED) as api:
            changed, attrs = api.enable_ipmi_over_lan()
        assert changed is False and attrs["priv"] == "admin"
        assert len(responses.calls) == 3  # login, read, logout: no configConfMo
        assert "configConfMo" not in responses.calls[1].request.body.decode()

    @responses.activate
    def test_comparison_ignores_the_cimcs_capitalisation(self):
        _login_ok(responses)
        self._resolve_ok("sys/rack-unit-1/bios/bios-settings/LOMPort-OptionROM",
                         "biosVfLOMPortOptionROM", vpLOMPortsAllState="enabled")
        _logout_ok(responses)
        with CimcXmlApi(HOST, CRED) as api:
            assert api.enable_lom_pxe()[0] is False

    @responses.activate
    def test_a_setting_that_cannot_be_read_is_written_anyway(self):
        _login_ok(responses)
        responses.add(
            responses.POST, NUOVA,
            body='<configResolveDn cookie="x" response="yes" errorCode="170" '
                 'invocationResult="unidentified-fail" errorDescr="DN does not exist"/>',
        )
        self._confmo_ok("sys/svc-ext/redfish-svc", "commRedfish", adminState="enabled")
        _logout_ok(responses)
        with CimcXmlApi(HOST, CRED) as api:
            changed, attrs = api.enable_redfish()
        assert changed and attrs["adminState"] == "enabled"
        assert "configConfMo" in responses.calls[2].request.body.decode()

    @responses.activate
    def test_boot_order_is_a_precision_tree_without_a_reboot(self):
        _login_ok(responses)
        self._confmo_ok("sys/rack-unit-1/boot-precision", "lsbootDevPrecision",
                        configuredBootMode="Legacy")
        _logout_ok(responses)
        with CimcXmlApi(HOST, CRED) as api:
            changed, _ = api.set_boot_order("disk,pxe")
        assert changed
        body = responses.calls[1].request.body.decode()
        assert 'inHierarchical="true"' in body
        assert ('<lsbootDevPrecision dn="sys/rack-unit-1/boot-precision" '
                'configuredBootMode="Legacy" rebootOnUpdate="no">') in body
        assert '<lsbootHdd rn="hdd-local" name="local" order="1" state="Enabled" />' in body
        assert ('<lsbootPxe rn="pxe-net" name="net" order="2" state="Enabled" slot="L" '
                'port="0" />') in body

    def test_boot_order_rejects_unknown_devices(self):
        api = CimcXmlApi(HOST, CRED)
        with pytest.raises(ValueError, match="disk and pxe"):
            api.set_boot_order("floppy,disk")

    @responses.activate
    def test_lom_option_rom(self):
        _login_ok(responses)
        self._resolve_ok("sys/rack-unit-1/bios/bios-settings/LOMPort-OptionROM",
                         "biosVfLOMPortOptionROM", vpLOMPortsAllState="Disabled")
        self._confmo_ok("sys/rack-unit-1/bios/bios-settings/LOMPort-OptionROM",
                        "biosVfLOMPortOptionROM", vpLOMPortsAllState="Enabled")
        _logout_ok(responses)
        with CimcXmlApi(HOST, CRED) as api:
            changed, attrs = api.enable_lom_pxe()
        assert changed and attrs["vpLOMPortsAllState"] == "Enabled"


class TestKvmTokensUnsupported:
    @responses.activate
    def test_old_firmware_without_the_token_method_is_not_an_error(self):
        _login_ok(responses)
        responses.add(
            responses.POST, NUOVA,
            body='<aaaGetComputeAuthTokens cookie="x" response="yes" errorCode="2009" '
                 'invocationResult="unidentified-fail" errorDescr="Method not supported."/>',
        )
        _logout_ok(responses)
        with CimcXmlApi(HOST, CRED) as api:
            links = api.kvm_launch()
        assert links["tokens_unsupported"] is True
        assert links["html5"] is None and links["java"] is None
        assert links["cimc"] == f"https://{HOST}/"
        assert "Method not supported" in links["reason"]

    @responses.activate
    def test_other_token_failures_still_raise(self):
        _login_ok(responses)
        responses.add(
            responses.POST, NUOVA,
            body='<aaaGetComputeAuthTokens cookie="x" response="yes" errorCode="552" '
                 'errorDescr="Authorization required"/>',
        )
        _logout_ok(responses)
        with pytest.raises(BMCError, match="Authorization required"):
            with CimcXmlApi(HOST, CRED) as api:
                api.kvm_launch()


class TestPowerEndpoints:
    def test_force_off_queues_its_own_job_type(
        self, client, make_customer, make_server, make_subscription, auth_header, dispatched
    ):
        customer = make_customer()
        server = make_server()
        make_subscription(customer, server)
        response = client.post(
            f"/api/v1/servers/{server.id}/power",
            json={"action": "force_off"},
            headers=auth_header(customer),
        )
        assert response.status_code == 202
        assert response.json()["type"] == JobType.POWER_FORCE_OFF.value

    def test_customers_cannot_override_the_job_lock(
        self, client, make_customer, make_server, make_subscription, auth_header
    ):
        customer = make_customer()
        server = make_server()
        make_subscription(customer, server)
        response = client.post(
            f"/api/v1/servers/{server.id}/power",
            json={"action": "reset", "force": True},
            headers=auth_header(customer),
        )
        assert response.status_code == 403

    def test_admin_can_reset_a_server_stuck_mid_install(
        self, client, db, make_customer, make_server, auth_header
    ):
        from app.services import jobs as job_service

        admin = make_customer("admin@example.com", admin=True)
        server = make_server()
        job_service.create_job(db, job_type=JobType.INSTALL, server_id=server.id)
        db.commit()
        headers = auth_header(admin)

        blocked = client.post(f"/api/v1/servers/{server.id}/power",
                              json={"action": "reset"}, headers=headers)
        assert blocked.status_code == 409
        forced = client.post(f"/api/v1/servers/{server.id}/power",
                             json={"action": "reset", "force": True}, headers=headers)
        assert forced.status_code == 202

    def test_live_power_state_reports_bmc_errors_instead_of_crashing(
        self, client, make_customer, make_server, make_subscription, auth_header
    ):
        from app.services import bmc_status

        customer = make_customer()
        # Nothing listens here, so the read fails -- and must say so politely.
        server = make_server(cimc_ip="127.0.0.1", ipmi_port=1, bmc_protocol="ipmi")
        make_subscription(customer, server)
        bmc_status.forget(server.id)
        body = client.get(f"/api/v1/servers/{server.id}/power",
                          headers=auth_header(customer)).json()
        assert body["state"] == "unknown"
        assert body["error"]


class TestAdminBmcEndpoints:
    def test_prepare_bmc_queues_a_job(self, client, make_customer, make_server, auth_header,
                                      dispatched):
        admin = make_customer("admin@example.com", admin=True)
        server = make_server()
        response = client.post(f"/api/v1/admin/servers/{server.id}/prepare-bmc",
                               headers=auth_header(admin))
        assert response.status_code == 202
        assert response.json()["type"] == JobType.BMC_SETUP.value
        assert dispatched == [response.json()["id"]]

    def test_kvm_launch_explains_an_unreachable_cimc(self, client, make_customer, make_server,
                                                     auth_header):
        admin = make_customer("admin@example.com", admin=True)
        server = make_server(cimc_ip="127.0.0.1", redfish_port=1)
        response = client.post(f"/api/v1/admin/servers/{server.id}/kvm",
                               headers=auth_header(admin))
        assert response.status_code == 502
        assert "CIMC web UI" in response.json()["detail"]

    def test_customers_cannot_launch_kvm(self, client, make_customer, make_server,
                                         make_subscription, auth_header):
        customer = make_customer()
        server = make_server()
        make_subscription(customer, server)
        response = client.post(f"/api/v1/admin/servers/{server.id}/kvm",
                               headers=auth_header(customer))
        assert response.status_code == 403

    def test_system_status_names_queues_with_no_worker(self, client, make_customer,
                                                       auth_header):
        admin = make_customer("admin@example.com", admin=True)
        body = client.get("/api/v1/admin/system", headers=auth_header(admin)).json()
        assert body["database"] == "ok"
        assert set(body["queues"]) == {"power", "provision", "poll"}

    def test_bmc_settings_resolve(self, client, make_customer, make_server, auth_header):
        admin = make_customer("admin@example.com", admin=True)
        server = make_server(bmc_protocol="ipmi", ipmi_port=6230)
        body = client.get(f"/api/v1/admin/servers/{server.id}/bmc",
                          headers=auth_header(admin)).json()
        assert body == {
            "protocol": "ipmi", "ipmi_port": 6230, "redfish_port": 443,
            "credential_ref": server.cimc_credential_ref, "credential_resolves": True,
            "username": "admin",
        }

    def test_bmc_protocol_is_validated(self, client, make_customer, make_server, auth_header):
        admin = make_customer("admin@example.com", admin=True)
        server = make_server()
        response = client.patch(f"/api/v1/admin/servers/{server.id}",
                                json={"bmc_protocol": "telnet"}, headers=auth_header(admin))
        assert response.status_code == 422


class TestConsoleTickets:
    def test_ticket_for_one_server_does_not_open_another(self, make_customer, make_server):
        import uuid

        from app.api.console import issue_ticket, redeem_ticket

        ticket, _ = issue_ticket(uuid.uuid4(), uuid.uuid4())
        assert redeem_ticket(ticket, uuid.uuid4()) is None

    def test_ticket_works_once(self):
        import uuid

        from app.api.console import issue_ticket, redeem_ticket

        customer, server = uuid.uuid4(), uuid.uuid4()
        ticket, _ = issue_ticket(customer, server)
        assert redeem_ticket(ticket, server) == customer
        assert redeem_ticket(ticket, server) is None

    def test_a_session_token_is_not_a_ticket(self):
        import uuid

        from app.api.console import redeem_ticket
        from app.security import create_access_token

        session, _ = create_access_token(uuid.uuid4(), is_admin=True)
        assert redeem_ticket(session, uuid.uuid4()) is None

    def test_suspended_customers_get_no_ticket(
        self, client, make_customer, make_server, make_subscription, auth_header
    ):
        customer = make_customer()
        server = make_server(state=ServerState.SUSPENDED)
        make_subscription(customer, server)
        response = client.post(f"/api/v1/console/{server.id}/ticket",
                               headers=auth_header(customer))
        assert response.status_code == 409


class TestSchemaUpgrade:
    def test_missing_columns_are_added_to_an_old_database(self, db):
        from sqlalchemy import inspect, text

        from app.db import engine
        from app.schema import upgrade_schema

        with engine.begin() as conn:
            conn.execute(text('ALTER TABLE servers DROP COLUMN IF EXISTS "ipmi_port"'))
        assert "ipmi_port" not in {c["name"] for c in inspect(engine).get_columns("servers")}

        added = upgrade_schema(engine)
        assert "servers.ipmi_port" in added
        assert upgrade_schema(engine) == []  # idempotent
