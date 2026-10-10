"""The router programmed from the panel's record: VLAN on the switch port,
PXE lease for the MAC."""

from __future__ import annotations

import json

import pytest
import responses
from responses import matchers

from app.config import settings
from app.enums import JobType
from app.models import IPAssignment, IPBlock
from app.services import network

ROUTER = "https://10.20.0.1/rest"


@pytest.fixture
def router(monkeypatch):
    monkeypatch.setattr(settings, "routeros_url", ROUTER)
    monkeypatch.setattr(settings, "routeros_username", "doz")
    monkeypatch.setattr(settings, "routeros_password", "secret")
    monkeypatch.setattr(settings, "routeros_next_server", "203.0.113.5")


@pytest.fixture
def wired_server(db, make_server):
    """A server with a block, a primary address, a PXE MAC and a switch port."""
    server = make_server(switch_port="ether3", provisioning_mac="aa:bb:cc:dd:ee:01")
    block = IPBlock(cidr="103.167.10.0/28", gateway="103.167.10.1", vlan=1000,
                    routing_mode="bridged")
    db.add(block)
    db.flush()
    db.add(IPAssignment(block_id=block.id, address="103.167.10.10", prefix_len=28,
                        server_id=server.id, is_primary=True))
    db.commit()
    return server


class TestPlan:
    def test_everything_the_router_needs_comes_from_the_record(self, db, wired_server, router):
        plan = network.plan_for(db, wired_server)
        assert plan.vlan == 1000 and plan.port == "ether3"
        assert plan.mac == "AA:BB:CC:DD:EE:01"
        assert plan.address == "103.167.10.10" and plan.prefix_len == 28
        assert plan.gateway == "103.167.10.1" and plan.network == "103.167.10.0/28"
        assert plan.next_server == "203.0.113.5" and plan.boot_file == "undionly.kpxe"
        assert plan.vlan_interface == "vlan1000" and plan.dhcp_server == "dhcp-vlan1000"

    def test_next_server_defaults_to_the_control_plane(self, db, wired_server, monkeypatch):
        monkeypatch.setattr(settings, "routeros_next_server", "")
        assert network.plan_for(db, wired_server).next_server == "10.10.0.5"

    def test_what_is_missing_is_named(self, db, make_server):
        server = make_server()
        with pytest.raises(network.PlanError, match="no primary address"):
            network.plan_for(db, server)
        block = IPBlock(cidr="198.51.100.0/29", gateway="198.51.100.1", routing_mode="bridged")
        db.add(block)
        db.flush()
        db.add(IPAssignment(block_id=block.id, address="198.51.100.2", prefix_len=29,
                            server_id=server.id, is_primary=True))
        db.commit()
        with pytest.raises(network.PlanError, match="no VLAN"):
            network.plan_for(db, server)
        block.vlan = 20
        db.commit()
        with pytest.raises(network.PlanError, match="no switch port"):
            network.plan_for(db, server)

    def test_the_script_is_the_same_configuration_by_hand(self, db, wired_server, router):
        script = network.routeros_script(network.plan_for(db, wired_server))
        assert "/interface vlan add name=vlan1000 interface=bridge vlan-id=1000" in script
        assert "/ip address add address=103.167.10.1/28 interface=vlan1000" in script
        assert ("/ip dhcp-server add name=dhcp-vlan1000 interface=vlan1000 "
                "address-pool=static-only lease-time=1d") in script
        assert ("/ip dhcp-server network add address=103.167.10.0/28 gateway=103.167.10.1 "
                "dns-server=1.1.1.1 next-server=203.0.113.5 boot-file-name=undionly.kpxe") in script
        assert "/interface bridge port set [find interface=ether3] pvid=1000" in script
        assert ("/ip dhcp-server lease add server=dhcp-vlan1000 address=103.167.10.10 "
                'mac-address=AA:BB:CC:DD:EE:01 comment="doz FCH') in script


def _q(**params):
    return [matchers.query_param_matcher(params)]


class TestRouterOS:
    """Against the REST API, with `responses`: what is read, what is written."""

    @responses.activate
    def test_a_bare_router_gets_everything_created(self, db, wired_server, router):
        plan = network.plan_for(db, wired_server)
        responses.add(responses.GET, f"{ROUTER}/interface/vlan", json=[],
                      match=_q(**{"vlan-id": "1000", "interface": "bridge"}))
        responses.add(responses.PUT, f"{ROUTER}/interface/vlan",
                      json={".id": "*A", "name": "vlan1000"})
        responses.add(responses.GET, f"{ROUTER}/ip/address", json=[],
                      match=_q(address="103.167.10.1/28", interface="vlan1000"))
        responses.add(responses.PUT, f"{ROUTER}/ip/address", json={".id": "*B"})
        responses.add(responses.GET, f"{ROUTER}/interface/bridge/vlan", json=[],
                      match=_q(bridge="bridge", **{"vlan-ids": "1000"}))
        responses.add(responses.PUT, f"{ROUTER}/interface/bridge/vlan", json={".id": "*C"})
        responses.add(responses.GET, f"{ROUTER}/ip/dhcp-server", json=[],
                      match=_q(interface="vlan1000"))
        responses.add(responses.PUT, f"{ROUTER}/ip/dhcp-server",
                      json={".id": "*D", "name": "dhcp-vlan1000"})
        responses.add(responses.GET, f"{ROUTER}/ip/dhcp-server/network", json=[],
                      match=_q(address="103.167.10.0/28"))
        responses.add(responses.PUT, f"{ROUTER}/ip/dhcp-server/network", json={".id": "*E"})
        responses.add(responses.GET, f"{ROUTER}/interface/bridge/port",
                      json=[{".id": "*F", "interface": "ether3", "pvid": "1"}],
                      match=_q(interface="ether3"))
        responses.add(responses.PATCH, f"{ROUTER}/interface/bridge/port/*F", json={})
        responses.add(responses.GET, f"{ROUTER}/ip/dhcp-server/lease", json=[],
                      match=_q(address="103.167.10.10"))
        responses.add(responses.GET, f"{ROUTER}/ip/dhcp-server/lease", json=[],
                      match=_q(**{"mac-address": "AA:BB:CC:DD:EE:01"}))
        responses.add(responses.PUT, f"{ROUTER}/ip/dhcp-server/lease", json={".id": "*G"})

        summary = network.apply(plan)

        assert summary["changes"] == [
            "VLAN interface vlan1000", "gateway 103.167.10.1/28 on vlan1000",
            "bridge VLAN 1000", "DHCP server dhcp-vlan1000",
            "DHCP network 103.167.10.0/28 with the PXE options", "ether3 pvid 1 -> 1000",
            "lease 103.167.10.10 for AA:BB:CC:DD:EE:01",
        ]
        bodies = {(c.request.method, c.request.url.split("?")[0]): c.request.body
                  for c in responses.calls if c.request.body}
        assert json.loads(bodies[("PUT", f"{ROUTER}/ip/dhcp-server")]) == {
            "name": "dhcp-vlan1000", "interface": "vlan1000",
            "address-pool": "static-only", "lease-time": "1d",
        }
        assert json.loads(bodies[("PUT", f"{ROUTER}/ip/dhcp-server/network")]) == {
            "address": "103.167.10.0/28", "gateway": "103.167.10.1", "dns-server": "1.1.1.1",
            "next-server": "203.0.113.5", "boot-file-name": "undionly.kpxe",
        }
        assert json.loads(bodies[("PATCH", f"{ROUTER}/interface/bridge/port/*F")]) == {
            "pvid": "1000"}
        assert json.loads(bodies[("PUT", f"{ROUTER}/ip/dhcp-server/lease")]) == {
            "mac-address": "AA:BB:CC:DD:EE:01", "address": "103.167.10.10",
            "server": "dhcp-vlan1000", "comment": "doz " + wired_server.serial,
        }
        assert "changed: VLAN interface vlan1000" in summary["description"]

    @responses.activate
    def test_a_router_already_right_is_left_alone(self, db, wired_server, router):
        plan = network.plan_for(db, wired_server)
        responses.add(responses.GET, f"{ROUTER}/interface/vlan",
                      json=[{".id": "*A", "name": "cust1", "vlan-id": "1000"}])
        responses.add(responses.GET, f"{ROUTER}/ip/address",
                      json=[{".id": "*B", "address": "103.167.10.1/28", "interface": "cust1"}])
        responses.add(responses.GET, f"{ROUTER}/interface/bridge/vlan",
                      json=[{".id": "*C", "vlan-ids": "1000", "tagged": "bridge"}])
        responses.add(responses.GET, f"{ROUTER}/ip/dhcp-server",
                      json=[{".id": "*D", "name": "dhcp-cust1", "interface": "cust1",
                             "address-pool": "static-only"}])
        responses.add(responses.GET, f"{ROUTER}/ip/dhcp-server/network", json=[{
            ".id": "*E", "address": "103.167.10.0/28", "gateway": "103.167.10.1",
            "dns-server": "1.1.1.1", "next-server": "203.0.113.5",
            "boot-file-name": "undionly.kpxe",
        }])
        responses.add(responses.GET, f"{ROUTER}/interface/bridge/port",
                      json=[{".id": "*F", "interface": "ether3", "pvid": "1000"}])
        responses.add(responses.GET, f"{ROUTER}/ip/dhcp-server/lease", json=[{
            ".id": "*G", "address": "103.167.10.10", "mac-address": "AA:BB:CC:DD:EE:01",
            "server": "dhcp-cust1", "comment": "doz " + wired_server.serial,
        }])
        summary = network.apply(plan)
        assert summary["changes"] == [] and "nothing to change" in summary["description"]
        assert all(c.request.method == "GET" for c in responses.calls)

    @responses.activate
    def test_an_address_leased_to_another_mac_is_refused(self, db, wired_server, router):
        plan = network.plan_for(db, wired_server)
        responses.add(responses.GET, f"{ROUTER}/interface/vlan",
                      json=[{".id": "*A", "name": "vlan1000"}])
        responses.add(responses.GET, f"{ROUTER}/ip/address", json=[{".id": "*B"}])
        responses.add(responses.GET, f"{ROUTER}/interface/bridge/vlan",
                      json=[{".id": "*C", "tagged": "bridge"}])
        responses.add(responses.GET, f"{ROUTER}/ip/dhcp-server",
                      json=[{".id": "*D", "name": "dhcp-vlan1000", "address-pool": "static-only"}])
        responses.add(responses.GET, f"{ROUTER}/ip/dhcp-server/network", json=[{
            ".id": "*E", "gateway": "103.167.10.1", "dns-server": "1.1.1.1",
            "next-server": "203.0.113.5", "boot-file-name": "undionly.kpxe",
        }])
        responses.add(responses.GET, f"{ROUTER}/interface/bridge/port",
                      json=[{".id": "*F", "pvid": "1000"}])
        responses.add(responses.GET, f"{ROUTER}/ip/dhcp-server/lease", json=[{
            ".id": "*G", "address": "103.167.10.10", "mac-address": "00:11:22:33:44:55",
            "comment": "someone else",
        }])
        with pytest.raises(network.NetworkError, match="leased to 00:11:22:33:44:55"):
            network.apply(plan)

    @responses.activate
    def test_an_unknown_switch_port_is_a_clear_error(self, db, wired_server, router):
        plan = network.plan_for(db, wired_server)
        responses.add(responses.GET, f"{ROUTER}/interface/vlan",
                      json=[{".id": "*A", "name": "vlan1000"}])
        responses.add(responses.GET, f"{ROUTER}/ip/address", json=[{".id": "*B"}])
        responses.add(responses.GET, f"{ROUTER}/interface/bridge/vlan",
                      json=[{".id": "*C", "tagged": "bridge"}])
        responses.add(responses.GET, f"{ROUTER}/ip/dhcp-server",
                      json=[{".id": "*D", "name": "dhcp-vlan1000", "address-pool": "static-only"}])
        responses.add(responses.GET, f"{ROUTER}/ip/dhcp-server/network", json=[{
            ".id": "*E", "gateway": "103.167.10.1", "dns-server": "1.1.1.1",
            "next-server": "203.0.113.5", "boot-file-name": "undionly.kpxe",
        }])
        responses.add(responses.GET, f"{ROUTER}/interface/bridge/port", json=[])
        with pytest.raises(network.NetworkError, match="ether3 is not a port of bridge"):
            network.apply(plan)

    @responses.activate
    def test_bad_credentials_and_router_errors_are_named(self, router):
        responses.add(responses.GET, f"{ROUTER}/ip/dhcp-server/lease", status=401)
        with pytest.raises(network.NetworkError, match="refused the credentials"):
            network.remove_lease("aa:bb:cc:dd:ee:01")
        responses.reset()
        responses.add(responses.GET, f"{ROUTER}/ip/dhcp-server/lease",
                      json=[{".id": "*G", "address": "103.167.10.10"}])
        responses.add(responses.DELETE, f"{ROUTER}/ip/dhcp-server/lease/*G", status=400,
                      json={"error": 400, "message": "Bad Request", "detail": "no such item"})
        with pytest.raises(network.NetworkError, match="no such item"):
            network.remove_lease("aa:bb:cc:dd:ee:01")


class TestWiring:
    """Where the router gets programmed from: the install, the job, assignment."""

    @pytest.fixture
    def applied(self, monkeypatch):
        calls: list = []

        def fake_apply(plan, *, log=None):  # noqa: ANN001
            calls.append(plan)
            return {"vlan": plan.vlan, "port": plan.port, "address": plan.address,
                    "mac": plan.mac, "changes": [], "description": "router: programmed"}

        monkeypatch.setattr(network, "apply", fake_apply)
        return calls

    def test_the_network_job_programs_the_router(self, db, wired_server, router, applied,
                                                 make_customer):
        from app.enums import ActorType, JobState
        from app.services import jobs as job_service
        from app.workers.tasks import network_apply_task

        admin = make_customer("admin@example.com", admin=True)
        job, _ = job_service.create_job(
            db, job_type=JobType.NETWORK_APPLY, server_id=wired_server.id, payload={},
            requested_by_id=admin.id, requested_by_type=ActorType.ADMIN,
        )
        db.commit()
        network_apply_task.run(str(job.id))
        db.refresh(job)
        assert job.state == JobState.SUCCEEDED, job.error
        assert [p.vlan for p in applied] == [1000]
        assert job.result["description"] == "router: programmed"

    def test_assigning_a_primary_address_queues_the_job(
        self, client, db, make_server, make_customer, auth_header, router, dispatched
    ):
        from sqlalchemy import select

        from app.models import Job

        admin = make_customer("admin@example.com", admin=True)
        server = make_server(switch_port="ether4")
        block = IPBlock(cidr="103.167.10.16/28", gateway="103.167.10.17", vlan=1001,
                        routing_mode="bridged")
        db.add(block)
        db.commit()
        response = client.post(
            f"/api/v1/admin/servers/{server.id}/ips", headers=auth_header(admin),
            json={"block_id": str(block.id), "address": "103.167.10.18", "is_primary": True},
        )
        assert response.status_code == 201, response.text
        jobs = db.execute(select(Job).where(Job.server_id == server.id)).scalars().all()
        assert [j.type for j in jobs] == [JobType.NETWORK_APPLY.value]
        assert dispatched == [str(jobs[0].id)]

    def test_without_a_router_nothing_is_queued_and_the_commands_are_shown(
        self, client, db, wired_server, make_customer, auth_header, monkeypatch, dispatched
    ):
        monkeypatch.setattr(settings, "routeros_url", "")
        admin = make_customer("admin@example.com", admin=True)
        body = client.get(f"/api/v1/admin/servers/{wired_server.id}/network",
                          headers=auth_header(admin)).json()
        assert body["configured"] is False and body["plan"]["vlan"] == 1000
        assert "/interface bridge port set [find interface=ether3] pvid=1000" in body["script"]
        response = client.post(f"/api/v1/admin/servers/{wired_server.id}/network/apply",
                               headers=auth_header(admin))
        assert response.status_code == 400 and "DOZ_ROUTEROS_URL" in response.json()["detail"]
        assert dispatched == []

    def test_the_plan_endpoint_says_what_is_missing(self, client, db, make_server,
                                                   make_customer, auth_header):
        admin = make_customer("admin@example.com", admin=True)
        server = make_server()
        body = client.get(f"/api/v1/admin/servers/{server.id}/network",
                          headers=auth_header(admin)).json()
        assert body["plan"] is None and "no primary address" in body["reason"]

    @responses.activate
    def test_releasing_the_primary_address_removes_the_lease(
        self, client, db, wired_server, make_customer, auth_header, router
    ):
        from sqlalchemy import select

        admin = make_customer("admin@example.com", admin=True)
        responses.add(responses.GET, f"{ROUTER}/ip/dhcp-server/lease",
                      json=[{".id": "*G", "address": "103.167.10.10"}],
                      match=_q(**{"mac-address": "AA:BB:CC:DD:EE:01"}))
        responses.add(responses.DELETE, f"{ROUTER}/ip/dhcp-server/lease/*G", json={})
        assignment = db.execute(
            select(IPAssignment).where(IPAssignment.server_id == wired_server.id)
        ).scalar_one()
        response = client.delete(f"/api/v1/admin/ips/{assignment.id}", headers=auth_header(admin))
        assert response.status_code == 204, response.text
        assert [c.request.method for c in responses.calls] == ["GET", "DELETE"]

    def test_an_install_programs_the_router_first(self, db, wired_server, router, applied,
                                                  make_template):
        from app.enums import ActorType
        from app.services import jobs as job_service
        from app.workers.tasks import _apply_network_for_install

        job, _ = job_service.create_job(
            db, job_type=JobType.INSTALL, server_id=wired_server.id,
            payload={"os_template_id": str(make_template().id)},
            requested_by_type=ActorType.ADMIN,
        )
        db.commit()
        _apply_network_for_install(db, job, wired_server, job_service.make_log_sink(db, job))
        db.refresh(job)
        assert [p.port for p in applied] == ["ether3"]
        assert any("router: programmed" in e.message for e in job.log_entries)

    def test_an_install_on_a_flat_network_server_just_notes_it(self, db, make_server, router,
                                                               applied):
        from app.enums import ActorType
        from app.services import jobs as job_service
        from app.workers.tasks import _apply_network_for_install

        server = make_server()
        job, _ = job_service.create_job(db, job_type=JobType.INSTALL, server_id=server.id,
                                        payload={}, requested_by_type=ActorType.ADMIN)
        db.commit()
        _apply_network_for_install(db, job, server, job_service.make_log_sink(db, job))
        assert applied == []
        db.refresh(job)
        assert any("router not programmed: no primary address" in e.message
                   for e in job.log_entries)
