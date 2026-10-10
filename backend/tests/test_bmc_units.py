"""BMC plumbing that does not need a simulator: CIMC XML API, endpoints, schema."""

from __future__ import annotations

import uuid

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



def _kvm_enabled(rsps, state: str = "enabled") -> None:
    """What `kvm_launch` reads first: the vKVM service object."""
    rsps.add(rsps.POST, NUOVA, body=(
        '<configResolveDn response="yes"><outConfig><commKvm dn="sys/svc-ext/kvm-svc" '
        f'adminState="{state}" port="2068"/></outConfig></configResolveDn>'
    ))


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
        _kvm_enabled(responses)
        responses.add(
            responses.POST, NUOVA,
            body='<aaaGetComputeAuthTokens cookie="x" outTokens="1804289383,846930886" '
                 'response="yes"/>',
        )
        responses.add(responses.GET, f"https://{HOST}/html/kvmViewer.html", status=404)
        responses.add(responses.GET, f"https://{HOST}/html/kvm.html", status=200,
                      body="<html><title>KVM Console</title></html>")
        _logout_ok(responses)
        with CimcXmlApi(HOST, CRED) as api:
            links = api.kvm_launch()
        assert links["html5"] == (f"https://{HOST}/html/kvm.html?cimcAddr={HOST}&cimcName=KVM"
                                  "&tkn1=1804289383&tkn2=846930886")
        assert links["java"] == (f"https://{HOST}/kvm.jnlp?cimcAddr={HOST}&cimcName=KVM"
                                 "&tkn1=1804289383&tkn2=846930886")
        assert links["cimc"] == f"https://{HOST}/"
        assert [p["status"] for p in links["probe"]] == [404, 200]

    @responses.activate
    def test_reports_whether_the_viewer_may_be_shown_inside_the_panel(self):
        # The console tab frames the viewer when the CIMC lets it; a CIMC
        # that forbids framing gets a tab instead of a blank frame.
        for headers, expected in (({}, True),
                                  ({"X-Frame-Options": "SAMEORIGIN"}, False),
                                  ({"Content-Security-Policy": "frame-ancestors 'self'"}, False),
                                  ({"Content-Security-Policy": "frame-ancestors *"}, True)):
            responses.reset()
            _login_ok(responses)
            _kvm_enabled(responses)
            responses.add(responses.POST, NUOVA,
                          body='<aaaGetComputeAuthTokens outTokens="1,2" response="yes"/>')
            responses.add(responses.GET, f"https://{HOST}/html/kvmViewer.html", status=200,
                          body="<html><title>KVM</title></html>", headers=headers)
            _logout_ok(responses)
            with CimcXmlApi(HOST, CRED) as api:
                links = api.kvm_launch()
            assert links["embeddable"] is expected, headers

    @responses.activate
    def test_a_cimc_that_serves_its_login_page_for_any_path_is_not_a_viewer(self):
        # Some builds answer 200 with the login app for unknown paths; opening
        # that with tokens on the URL is what "KVM is not working" looks like.
        _login_ok(responses)
        _kvm_enabled(responses)
        responses.add(responses.POST, NUOVA,
                      body='<aaaGetComputeAuthTokens outTokens="1,2" response="yes"/>')
        for path in ("/html/kvmViewer.html", "/html/kvm.html", "/kvmViewer.html"):
            responses.add(responses.GET, f"https://{HOST}{path}", status=200,
                          body="<html><body>Cisco IMC login</body></html>")
        _logout_ok(responses)
        with CimcXmlApi(HOST, CRED) as api:
            links = api.kvm_launch()
        assert links["html5"] is None
        assert all(p["status"] == 200 and p["viewer"] is False for p in links["probe"])

    @responses.activate
    def test_no_viewer_found_still_offers_java_and_the_web_ui(self):
        _login_ok(responses)
        _kvm_enabled(responses)
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


class TestRaidOverXmlApi:
    @responses.activate
    def test_virtual_drive_create_sends_the_creator_object(self):
        _login_ok(responses)
        responses.add(responses.POST, NUOVA, body=(
            '<configConfMo response="yes"><outConfig>'
            '<storageVirtualDriveCreatorUsingUnusedPhysicalDrive '
            'dn="sys/rack-unit-1/board/storage-SAS-MRAID/virtual-drive-create" '
            'adminState="triggered"/></outConfig></configConfMo>'
        ))
        _logout_ok(responses)
        with CimcXmlApi(HOST, CRED) as api:
            api.create_virtual_drive(
                "sys/rack-unit-1/board/storage-SAS-MRAID", name="doz", raid_level=10,
                drive_groups=[[1, 2], [3, 4]], size="1830000 MB",
            )
        body = responses.calls[1].request.body.decode()
        assert ('<storageVirtualDriveCreatorUsingUnusedPhysicalDrive '
                'dn="sys/rack-unit-1/board/storage-SAS-MRAID/virtual-drive-create" '
                'virtualDriveName="doz" raidLevel="10" driveGroup="[1,2][3,4]" '
                'size="1830000 MB" adminState="trigger" />') in body

    @responses.activate
    def test_prepare_resets_a_custom_ipmi_encryption_key(self):
        # A custom key makes every IPMI session fail with the wrong-password
        # message; the platform never sends a key, so Prepare zeroes it.
        _login_ok(responses)
        responses.add(responses.POST, NUOVA, body=(
            '<configResolveDn response="yes"><outConfig><commIpmiLan '
            'dn="sys/svc-ext/ipmi-lan-svc" adminState="enabled" priv="admin" '
            'key="ab12ab12ab12ab12ab12ab12ab12ab12ab12ab12"/></outConfig></configResolveDn>'
        ))
        responses.add(responses.POST, NUOVA, body=(
            '<configConfMo response="yes"><outConfig><commIpmiLan '
            'dn="sys/svc-ext/ipmi-lan-svc" adminState="enabled" priv="admin" '
            'key="0000000000000000000000000000000000000000"/></outConfig></configConfMo>'
        ))
        _logout_ok(responses)
        with CimcXmlApi(HOST, CRED) as api:
            changed, attrs = api.enable_ipmi_over_lan()
        assert changed and attrs["key"] == "0" * 40
        assert 'key="0000000000000000000000000000000000000000"' in (
            responses.calls[2].request.body.decode()
        )


class TestRaidOverXmlApiFlow:
    @responses.activate
    def test_xml_path_builds_the_array_from_the_disks_the_cimc_lists(self):
        from app.services import raid

        ctrl = "sys/rack-unit-1/board/storage-SAS-SLOT-HBA"
        _login_ok(responses)
        responses.add(responses.POST, NUOVA, body=(
            '<configResolveClass response="yes"><outConfigs>'
            f'<storageController dn="{ctrl}" id="SLOT-HBA" type="SAS" '
            'model="Cisco 12G SAS Modular Raid Controller"/></outConfigs></configResolveClass>'
        ))
        responses.add(responses.POST, NUOVA, body=(  # the array someone built by hand
            '<configResolveClass response="yes"><outConfigs>'
            f'<storageVirtualDrive dn="{ctrl}/vd-0" id="0" name="RAID1_12" raidLevel="RAID 1" '
            'size="952720 MB" vdStatus="Optimal" bootDrive="false"/>'
            '</outConfigs></configResolveClass>'
        ))
        responses.add(responses.POST, NUOVA, body=(  # its deletion
            f'<configConfMo response="yes"><outConfig><storageVirtualDrive dn="{ctrl}/vd-0" '
            'status="deleted"/></outConfig></configConfMo>'
        ))
        # First listing: what the C220 M4 in the field reported, verbatim.
        responses.add(responses.POST, NUOVA, body=(
            '<configResolveClass response="yes"><outConfigs>'
            f'<storageLocalDisk dn="{ctrl}/pd-1" id="1" pdStatus="Foreign Configuration" '
            'pdState="unconfigured good" health="Moderate Fault" coercedSize="952720 MB" '
            'mediaType="HDD" driveState="unconfigured good"/>'
            f'<storageLocalDisk dn="{ctrl}/pd-2" id="2" pdStatus="Foreign Configuration" '
            'pdState="unconfigured good" health="Moderate Fault" coercedSize="952720 MB" '
            'mediaType="HDD" driveState="unconfigured good"/>'
            '</outConfigs></configResolveClass>'
        ))
        responses.add(responses.POST, NUOVA, body=(  # clear-foreign-config
            f'<configConfMo response="yes"><outConfig><storageController dn="{ctrl}" '
            'adminAction="no-op"/></outConfig></configConfMo>'
        ))
        responses.add(responses.POST, NUOVA, body=(  # listed again, now usable
            '<configResolveClass response="yes"><outConfigs>'
            f'<storageLocalDisk dn="{ctrl}/pd-1" id="1" pdStatus="JBOD" health="Good" '
            'coercedSize="952720 MB" mediaType="HDD"/>'
            f'<storageLocalDisk dn="{ctrl}/pd-2" id="2" pdStatus="Unconfigured Good" '
            'health="Good" coercedSize="952720 MB" mediaType="HDD"/>'
            '</outConfigs></configResolveClass>'
        ))
        responses.add(responses.POST, NUOVA, body=(  # make-unconfigured-good on pd-1
            f'<configConfMo response="yes"><outConfig><storageLocalDisk dn="{ctrl}/pd-1" '
            'pdStatus="Unconfigured Good"/></outConfig></configConfMo>'
        ))
        responses.add(responses.POST, NUOVA, body=(  # the creator
            '<configConfMo response="yes"><outConfig>'
            '<storageVirtualDriveCreatorUsingUnusedPhysicalDrive adminState="triggered"/>'
            '</outConfig></configConfMo>'
        ))
        responses.add(responses.POST, NUOVA, body=(  # virtual drives afterwards
            '<configResolveClass response="yes"><outConfigs>'
            f'<storageVirtualDrive dn="{ctrl}/vd-0" id="0" name="doz" raidLevel="RAID 1"/>'
            '</outConfigs></configResolveClass>'
        ))
        responses.add(responses.POST, NUOVA, body=(  # set-boot-drive
            f'<configConfMo response="yes"><outConfig><storageVirtualDrive dn="{ctrl}/vd-0" '
            'bootDrive="true"/></outConfig></configConfMo>'
        ))
        _logout_ok(responses)
        logged: list[str] = []
        with CimcXmlApi(HOST, CRED) as api:
            summary = raid._configure_xml(
                api, raid.RaidLevel.RAID1,
                lambda m, level="info", request=None, response=None: logged.append(m),
            )
        assert summary["via"] == "cimc-xml" and summary["drives"] == ["disk 1", "disk 2"]
        bodies = [c.request.body.decode() for c in responses.calls]
        assert any(f'<storageController dn="{ctrl}" adminAction="clear-foreign-config"' in b
                   for b in bodies)
        assert any(f'<storageLocalDisk dn="{ctrl}/pd-1" adminAction="make-unconfigured-good"'
                   in b for b in bodies)
        assert any(f'<storageVirtualDrive dn="{ctrl}/vd-0" status="deleted" />' in b
                   for b in bodies)
        creator = next(b for b in bodies if "VirtualDriveCreator" in b)
        assert 'raidLevel="1" driveGroup="[1,2]"' in creator
        assert 'size="952720 MB"' in creator  # the disks' coerced size, in full
        assert any(f'dn="{ctrl}/vd-0" adminAction="set-boot-drive"' in b for b in bodies)
        assert any("attributes: {" in m and "pdStatus" in m for m in logged)


class TestRaidOverXmlApiExistingArray:
    CTRL = "sys/rack-unit-1/board/storage-SAS-SLOT-HBA"

    def _controller_and_vd(self, raid_level: str, boot: str) -> None:
        _login_ok(responses)
        responses.add(responses.POST, NUOVA, body=(
            '<configResolveClass response="yes"><outConfigs>'
            f'<storageController dn="{self.CTRL}" id="SLOT-HBA" type="SAS" raidSupport="yes" '
            'model="Cisco 12G SAS Modular Raid Controller"/></outConfigs></configResolveClass>'
        ))
        responses.add(responses.POST, NUOVA, body=(
            '<configResolveClass response="yes"><outConfigs>'
            f'<storageVirtualDrive dn="{self.CTRL}/vd-0" id="0" name="RAID1_12" '
            f'raidLevel="{raid_level}" size="952720 MB" vdStatus="Optimal" health="Good" '
            f'bootDrive="{boot}" drivesPerSpan="2" spanDepth="1"/>'
            '</outConfigs></configResolveClass>'
        ))

    @responses.activate
    def test_a_matching_optimal_array_is_kept_and_made_bootable(self):
        from app.services import raid

        self._controller_and_vd("RAID 1", boot="false")
        responses.add(responses.POST, NUOVA, body=(  # set-boot-drive
            f'<configConfMo response="yes"><outConfig><storageVirtualDrive dn="{self.CTRL}/vd-0" '
            'bootDrive="true"/></outConfig></configConfMo>'
        ))
        _logout_ok(responses)
        with CimcXmlApi(HOST, CRED) as api:
            summary = raid._configure_xml(api, raid.RaidLevel.RAID1,
                                          lambda m, level="info", **kw: None)
        assert summary["kept"] is True and summary["volume"] == "RAID1_12"
        bodies = [c.request.body.decode() for c in responses.calls]
        assert any(f'dn="{self.CTRL}/vd-0" adminAction="set-boot-drive"' in b for b in bodies)
        assert not any('status="deleted"' in b or "VirtualDriveCreator" in b for b in bodies)

    @responses.activate
    def test_the_boot_drive_is_cleared_before_a_different_array_is_deleted(self):
        # "The Virtual Drive 0 is an OS Drive. This virtual drive cannot be
        # deleted": the controller's boot drive has to be cleared first.
        from app.drivers.base import BMCError as Err
        from app.services import raid

        self._controller_and_vd("RAID 1", boot="true")
        responses.add(responses.POST, NUOVA, body=(  # clear-boot-drive
            f'<configConfMo response="yes"><outConfig><storageController dn="{self.CTRL}" '
            'adminAction="no-op"/></outConfig></configConfMo>'
        ))
        responses.add(responses.POST, NUOVA, body=(  # delete
            f'<configConfMo response="yes"><outConfig><storageVirtualDrive dn="{self.CTRL}/vd-0" '
            'status="deleted"/></outConfig></configConfMo>'
        ))
        responses.add(responses.POST, NUOVA,  # disks: none left, to end the flow early
                      body='<configResolveClass response="yes"><outConfigs/></configResolveClass>')
        responses.add(responses.POST, NUOVA,
                      body='<configResolveClass response="yes"><outConfigs/></configResolveClass>')
        _logout_ok(responses)
        with pytest.raises((raid.RaidError, Err)):
            with CimcXmlApi(HOST, CRED) as api:
                raid._configure_xml(api, raid.RaidLevel.RAID0, lambda m, level="info", **kw: None)
        bodies = [c.request.body.decode() for c in responses.calls]
        clear = next(i for i, b in enumerate(bodies) if 'adminAction="clear-boot-drive"' in b)
        delete = next(i for i, b in enumerate(bodies) if 'status="deleted"' in b)
        assert clear < delete


class TestRaidNeedsTheHostOn:
    def test_a_powered_off_host_is_powered_on_and_the_controller_waited_for(self, monkeypatch):
        from app.services import raid

        monkeypatch.setattr(raid.time, "sleep", lambda s: None)
        calls: list[str] = []
        states = iter([
            [{"name": "MRAID", "model": "", "raid_types": [], "state": "Disabled"}],
            [{"name": "MRAID", "model": "", "raid_types": [], "state": "Enabled"}],
        ])

        class Redfish:
            def power_status(self):
                return type("P", (), {"state": "off"})()

            def power(self, action):  # noqa: ANN001
                calls.append(f"power {action.value}")

            def storage(self):
                calls.append("storage")
                return next(states)

        logged: list[str] = []
        raid.ensure_host_on(Redfish(), lambda m, level="info", **kw: logged.append(m))
        assert calls == ["power on", "storage", "storage"]
        assert any("RAID controller is up" in m for m in logged)

    def test_a_host_that_is_on_is_left_alone(self):
        from app.services import raid

        class Redfish:
            def power_status(self):
                return type("P", (), {"state": "on"})()

            def power(self, action):  # noqa: ANN001
                raise AssertionError("must not touch power")

        raid.ensure_host_on(Redfish(), lambda m, level="info", **kw: None)

    def test_not_ready_answers_are_retried_until_the_controller_is_up(self, monkeypatch):
        # "Operation failed. storage subsystem not ready yet (CIMC error 2003)"
        # right after power-on, while the controller is still initialising.
        from app.services import raid

        monkeypatch.setattr(raid.time, "sleep", lambda s: None)
        answers = iter([
            BMCError("configConfMo x: Operation failed. storage subsystem not ready yet "
                     "(CIMC error 2003)"),
            BMCError("configConfMo x: Operation failed. storage subsystem not ready yet "
                     "(CIMC error 2003)"),
            {"ok": "yes"},
        ])

        def attempt():
            answer = next(answers)
            if isinstance(answer, Exception):
                raise answer
            return answer

        assert raid._until_ready(attempt, log=lambda m, level="info", **kw: None,
                                 what="x") == {"ok": "yes"}

        def other_error():
            raise BMCError("configConfMo x: Invalid request (CIMC error 2999)")

        with pytest.raises(BMCError, match="2999"):
            raid._until_ready(other_error, log=lambda m, level="info", **kw: None, what="x")


class TestRaidPlanning:
    def _drive(self, name, cap, media="HDD", **extra):
        return {"name": name, "path": f"/d/{name}", "capacity_bytes": cap, "media": media,
                "health": "OK", "state": "Enabled", **extra}

    def test_raid1_picks_two_matching_drives_and_skips_the_sick_one(self):
        from app.services import raid

        drives = [
            self._drive("ssd", 480_000_000_000, "SSD"),
            self._drive("a", 1_000_000_000_000),
            self._drive("b", 1_000_000_000_000, failure_predicted=True),
            self._drive("c", 1_000_000_000_000),
        ]
        chosen = raid.choose_drives(raid.RaidLevel.RAID1, drives)
        assert [d["name"] for d in chosen] == ["a", "c"]
        assert raid.usable_capacity(raid.RaidLevel.RAID1, [1_000, 1_000]) == 1_000
        assert raid.usable_capacity(raid.RaidLevel.RAID5, [1_000, 1_000, 900]) == 1_800
        assert raid.usable_capacity(raid.RaidLevel.RAID10, [10, 10, 10, 10]) == 20

    def test_too_few_drives_is_a_clear_error_that_lists_every_drive(self):
        from app.services import raid

        with pytest.raises(raid.RaidError, match="raid5 needs 3 drives; 2 usable of 2") as err:
            raid.choose_drives(raid.RaidLevel.RAID5,
                               [self._drive("a", 10), self._drive("b", 10)])
        assert "a: 0 GB HDD, health OK, state Enabled" in str(err.value)

    def test_unknown_size_or_state_does_not_disqualify_a_drive(self):
        # The C220's Redfish reported two drives the first filter threw out
        # without saying why. Only states that cannot join an array exclude.
        from app.services import raid

        drives = [
            {"name": "PD-1", "path": "/d/1", "health": "OK", "state": "Enabled",
             "oem_state": "JBOD"},
            {"name": "PD-2", "path": "/d/2", "health": None, "state": None},
            {"name": "PD-3", "path": "/d/3", "health": "OK", "oem_state": "Unconfigured Bad"},
        ]
        chosen = raid.choose_drives(raid.RaidLevel.RAID1, drives)
        assert [d["name"] for d in chosen] == ["PD-1", "PD-2"]
        assert raid.usable_capacity(raid.RaidLevel.RAID1, [None, None]) is None
        assert raid.unusable_reason(drives[2]) == "state Unconfigured Bad"

    def test_raid10_takes_an_even_number(self):
        from app.services import raid

        drives = [self._drive(n, 10) for n in "abcde"]
        assert len(raid.choose_drives(raid.RaidLevel.RAID10, drives)) == 4

    def test_cimc_sizes_parse(self):
        from app.services.raid import _parse_size

        assert _parse_size("952720 MB") == 952720 * 1024 ** 2
        assert _parse_size("1.8 TB") == int(1.8 * 1024 ** 4)
        assert _parse_size("n/a") == 0


class TestRaidEndpoint:
    def test_queues_a_raid_job_only_with_confirmation(self, client, make_customer, make_server,
                                                       auth_header, dispatched):
        admin = make_customer("admin@example.com", admin=True)
        server = make_server()
        refused = client.post(f"/api/v1/admin/servers/{server.id}/raid",
                              json={"level": "raid1"}, headers=auth_header(admin))
        assert refused.status_code == 400
        queued = client.post(f"/api/v1/admin/servers/{server.id}/raid",
                             json={"level": "raid1", "confirm_data_loss": True},
                             headers=auth_header(admin))
        assert queued.status_code == 202, queued.text
        assert queued.json()["type"] == "raid_configure"
        assert dispatched == [queued.json()["id"]]


class TestServerUtilization:
    """The CIMC summary page's CPU / memory / IO chart, read the same way."""

    @responses.activate
    def test_reads_the_four_figures(self):
        _login_ok(responses)
        responses.add(responses.POST, NUOVA, body=(
            '<configResolveClass response="yes" classId="serverUtilization"><outConfigs>'
            '<serverUtilization dn="sys/rack-unit-1/utilization" overallUtilization="12" '
            'cpuUtilization="9" memoryUtilization="31" ioUtilization="2"/>'
            '</outConfigs></configResolveClass>'
        ))
        _logout_ok(responses)
        with CimcXmlApi(HOST, CRED) as api:
            assert api.server_utilization() == {"overall": 12, "cpu": 9, "memory": 31, "io": 2}
        assert 'classId="serverUtilization"' in responses.calls[1].request.body.decode()

    @responses.activate
    def test_figures_the_cimc_has_not_got_are_none(self):
        # "N/A" while the host is off; the object itself missing on a build
        # without it. Neither is an error for the sensors page.
        _login_ok(responses)
        responses.add(responses.POST, NUOVA, body=(
            '<configResolveClass response="yes" classId="serverUtilization"><outConfigs>'
            '<serverUtilization dn="sys/rack-unit-1/utilization" overallUtilization="N/A" '
            'cpuUtilization="N/A" memoryUtilization="N/A" ioUtilization="N/A"/>'
            '</outConfigs></configResolveClass>'
        ))
        responses.add(responses.POST, NUOVA, body=(
            '<configResolveClass response="yes" errorCode="103" '
            'errorDescr="unknown class serverUtilization"/>'
        ))
        _logout_ok(responses)
        with CimcXmlApi(HOST, CRED) as api:
            assert api.server_utilization() == {"overall": None, "cpu": None,
                                                "memory": None, "io": None}
            assert api.server_utilization() is None


class TestKvmTokensUnsupported:
    @responses.activate
    def test_old_firmware_without_the_token_method_is_not_an_error(self):
        _login_ok(responses)
        _kvm_enabled(responses)
        responses.add(
            responses.POST, NUOVA,
            body='<aaaGetComputeAuthTokens cookie="x" response="yes" errorCode="2009" '
                 'invocationResult="unidentified-fail" errorDescr="Method not supported."/>',
        )
        _logout_ok(responses)
        responses.add(responses.GET, f"https://{HOST}/html/kvmViewer.html", status=200,
                      body="<html><title>KVM</title></html>")
        with CimcXmlApi(HOST, CRED) as api:
            links = api.kvm_launch()
        assert links["tokens_unsupported"] is True
        assert links["html5"] is None and links["java"] is None
        assert links["cimc"] == f"https://{HOST}/"
        assert "Method not supported" in links["reason"]
        # The viewer is still there for a browser that has logged in to the CIMC.
        assert links["viewer"] == f"https://{HOST}/html/kvmViewer.html"
        assert links["kvm_service"]["adminState"] == "enabled"

    @responses.activate
    def test_a_vkvm_service_that_is_off_is_switched_on_before_asking(self):
        # Cisco: tokens cannot be obtained while vKVM is disabled. Prepare BMC
        # enables it, but a KVM click must not depend on that having run.
        _login_ok(responses)
        _kvm_enabled(responses, state="disabled")
        responses.add(responses.POST, NUOVA, body=(
            '<configConfMo response="yes"><outConfig><commKvm dn="sys/svc-ext/kvm-svc" '
            'adminState="enabled" port="2068"/></outConfig></configConfMo>'
        ))
        responses.add(responses.POST, NUOVA,
                      body='<aaaGetComputeAuthTokens outTokens="1,2" response="yes"/>')
        responses.add(responses.GET, f"https://{HOST}/html/kvmViewer.html", status=200,
                      body="<html><title>KVM</title></html>")
        _logout_ok(responses)
        with CimcXmlApi(HOST, CRED) as api:
            links = api.kvm_launch()
        assert links["html5"] and links["tokens_unsupported"] is False
        body = responses.calls[2].request.body.decode()
        assert 'commKvm dn="sys/svc-ext/kvm-svc" adminState="enabled" port="2068"' in body

    @responses.activate
    def test_other_token_failures_still_raise(self):
        _login_ok(responses)
        _kvm_enabled(responses)
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
            "ipmi_cipher_suite": "3", "cipher_source": "platform",
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


class TestConnectionTest:
    def test_a_working_cipher_is_remembered_for_the_server(
        self, client, db, make_customer, make_server, auth_header, monkeypatch
    ):
        from app.api import admin_bmc

        admin = make_customer("admin@example.com", admin=True)
        server = make_server()
        report = {
            "ok": True, "checks": [], "facts": {}, "configured_cipher": "3",
            "working_cipher": "17", "verdict": "IPMI works with cipher suite 17", "hint": None,
        }
        monkeypatch.setattr(admin_bmc.bmc_test, "run", lambda server, credential: dict(report))

        body = client.post(f"/api/v1/admin/servers/{server.id}/bmc/test",
                           headers=auth_header(admin)).json()
        assert body["cipher_saved"] is True and body["working_cipher"] == "17"
        db.refresh(server)
        assert server.ipmi_cipher_suite == "17"

        settings = client.get(f"/api/v1/admin/servers/{server.id}/bmc",
                              headers=auth_header(admin)).json()
        assert settings["ipmi_cipher_suite"] == "17" and settings["cipher_source"] == "server"

        # The configured suite working again is not a change.
        report["working_cipher"] = "17"
        report["configured_cipher"] = "17"
        body = client.post(f"/api/v1/admin/servers/{server.id}/bmc/test",
                           headers=auth_header(admin)).json()
        assert body["cipher_saved"] is False

    def test_the_probe_is_stored_as_auto_and_used_as_no_cipher_flag(self, make_server):
        from app.drivers.factory import cipher_for, get_ipmi_driver

        server = make_server(ipmi_cipher_suite="auto")
        assert cipher_for(server) == ""
        assert "-C" not in get_ipmi_driver(server).base_command()
        server.ipmi_cipher_suite = "17"
        cmd = get_ipmi_driver(server).base_command()
        assert cmd[cmd.index("-C") + 1] == "17"

    def test_bmc_reset_falls_back_to_redfish_when_ipmi_is_dead(
        self, client, make_customer, make_server, auth_header, monkeypatch
    ):
        from app.drivers.ipmi import IpmiDriver
        from app.drivers.redfish import RedfishDriver

        admin = make_customer("admin@example.com", admin=True)
        server = make_server()
        calls: list[str] = []

        def dead(self, kind="cold"):  # noqa: ANN001
            raise BMCError("ipmitool mc reset cold: IPMI session failed")

        monkeypatch.setattr(IpmiDriver, "bmc_reset", dead)
        monkeypatch.setattr(RedfishDriver, "manager_reset",
                            lambda self: calls.append("manager_reset") or "ForceRestart")
        body = client.post(f"/api/v1/admin/servers/{server.id}/bmc/reset",
                           headers=auth_header(admin)).json()
        assert body["via"] == "redfish" and calls == ["manager_reset"]


class TestLiveReadCache:
    def test_a_failed_read_is_retried_once_then_held(self, monkeypatch):
        from app.services import bmc_status

        server_id = uuid.uuid4()
        calls = {"n": 0}

        def read():
            calls["n"] += 1
            raise BMCError("IPMI session failed")

        for _ in range(3):
            with pytest.raises(BMCError, match="session failed"):
                bmc_status.cached("sensors", server_id, read)
        assert calls["n"] == 2  # first poll: one retry after the quick failure; then held

        # A forced refresh always asks the BMC again.
        with pytest.raises(BMCError):
            bmc_status.cached("sensors", server_id, read, fresh=True)
        assert calls["n"] == 4

        # Once the hold has passed, so does the next poll.
        monkeypatch.setattr(bmc_status, "ERROR_HOLD_SECONDS", 0.0)
        with pytest.raises(BMCError):
            bmc_status.cached("sensors", server_id, read)
        assert calls["n"] == 6
        bmc_status.forget(server_id)

    def test_a_missed_poll_shows_the_last_good_reading_as_stale(self, monkeypatch):
        from app.config import settings
        from app.services import bmc_status

        server_id = uuid.uuid4()
        answers = [{"sensors": [1, 2], "checked_at": "t0"}, BMCError("lost packet")]

        def read():
            answer = answers.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer

        assert bmc_status.cached("sensors", server_id, read)["sensors"] == [1, 2]
        monkeypatch.setattr(settings, "bmc_live_cache_seconds", 0)
        answers.append(BMCError("lost packet again"))  # the retry
        stale = bmc_status.cached("sensors", server_id, read)
        assert stale["sensors"] == [1, 2] and stale["stale"] is True
        assert "lost packet" in stale["error"]

        # Past the stale window the error is what the caller gets.
        monkeypatch.setattr(settings, "bmc_stale_after_seconds", 0)
        monkeypatch.setattr(bmc_status, "ERROR_HOLD_SECONDS", 0.0)
        answers.extend([BMCError("still lost"), BMCError("still lost")])
        with pytest.raises(BMCError, match="still lost"):
            bmc_status.cached("sensors", server_id, read)
        bmc_status.forget(server_id)

    def test_power_keeps_the_last_state_through_a_missed_poll(self, db, make_server, monkeypatch):
        from contextlib import contextmanager

        from app.config import settings
        from app.services import bmc_status

        server = make_server()
        outcomes = ["on", BMCError("lost"), BMCError("lost")]

        class Driver:
            def power_status(self):
                outcome = outcomes.pop(0)
                if isinstance(outcome, Exception):
                    raise outcome
                return type("P", (), {"state": outcome})()

        @contextmanager
        def fake_driver(server, interactive=False):  # noqa: ANN001
            yield Driver()

        monkeypatch.setattr(bmc_status, "get_driver", fake_driver)
        monkeypatch.setattr(bmc_status, "protocol_label", lambda d: "ipmi")
        bmc_status.forget(server.id)

        first = bmc_status.read_power(db, server)
        assert first["state"] == "on" and first["stale"] is False

        bmc_status._cache.pop(server.id, None)
        second = bmc_status.read_power(db, server)
        assert second["state"] == "on" and second["stale"] is True and "lost" in second["error"]

        monkeypatch.setattr(settings, "bmc_stale_after_seconds", 0)
        outcomes.extend([BMCError("lost"), BMCError("lost")])
        bmc_status._cache.pop(server.id, None)
        third = bmc_status.read_power(db, server)
        assert third["state"] == "unknown" and third["stale"] is False
        bmc_status.forget(server.id)

    def test_health_sweep_keeps_the_last_verdict_through_missed_polls(
        self, db, make_server, monkeypatch
    ):
        from contextlib import contextmanager

        from app.workers import tasks

        server = make_server(health_status="ok", health_detail={"psu": "ok"})

        @contextmanager
        def dead(server, log=None):  # noqa: ANN001
            raise BMCError("no answer")
            yield

        monkeypatch.setattr(tasks, "get_driver", dead)
        for expected_status, expected_missed in (("ok", 1), ("ok", 2), ("unknown", 3)):
            result = tasks._poll_health(db, server)
            assert result["status"] == expected_status
            assert result["missed_polls"] == expected_missed
            assert server.health_status == expected_status
        assert "psu" not in server.health_detail


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
