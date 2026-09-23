import { MonitorPlay } from 'lucide-react'
import { Card } from '../../../components'
import SerialConsole from '../../../console/SerialConsole'
import { KvmButton } from './kvm'
import { useServer } from './ServerPage'

export default function ConsoleTab() {
  const { server } = useServer()
  return (
    <>
      <SerialConsole serverId={server.id} popoutTo={`/servers/${server.id}/console`} />
      <div className="grid cols-2" style={{ marginTop: 16 }}>
        <Card title="Graphical console (vKVM)" icon={<MonitorPlay />}>
          <p className="subtle" style={{ marginBottom: 12 }}>
            The CIMC's own viewer, opened with one-time tokens: full keyboard and mouse passthrough,
            video from POST onwards, and its own virtual media for mounting an ISO from your machine.
            Your browser connects to <span className="mono">{server.cimc_ip}</span> directly.
          </p>
          <KvmButton serverId={server.id} cimcIp={server.cimc_ip} port={server.redfish_port} />
          <p className="faint small" style={{ marginTop: 10 }}>
            First time? Open the CIMC web UI once and accept its certificate, or the KVM window is blocked.
          </p>
        </Card>
        <Card title="Serial console tips">
          <ul style={{ margin: 0, paddingLeft: 18, color: 'var(--text-2)' }}>
            <li>Nothing on screen? Press Enter. A BIOS or GRUB menu redraws on the next keystroke.</li>
            <li>Still nothing after a reset? Console redirection is off in the BIOS: run <strong>Prepare BMC</strong> from the BMC tab, then power-cycle.</li>
            <li>The BMC allows one serial viewer at a time. <strong>Take over</strong> drops the other session.</li>
            <li>Function keys are caught by the browser; use the toolbar. F2 opens BIOS setup, F6 the boot menu, F12 PXE.</li>
          </ul>
        </Card>
      </div>
    </>
  )
}
