import { MonitorPlay, Terminal } from 'lucide-react'
import { useSearchParams } from 'react-router-dom'
import { Card } from '../../../components'
import SerialConsole from '../../../console/SerialConsole'
import { KvmViewer } from './kvm'
import { useServer } from './ServerPage'

/**
 * Two consoles, one at a time: the serial console the platform proxies over
 * SOL, and the CIMC's graphical vKVM shown inside the page. `?view=kvm` picks
 * the latter, which is what the header's KVM button links to.
 */
export default function ConsoleTab() {
  const { server } = useServer()
  const [params, setParams] = useSearchParams()
  const view = params.get('view') === 'kvm' ? 'kvm' : 'serial'

  return (
    <>
      <div className="toolbar">
        <div className="row" style={{ gap: 4 }}>
          <button className={`sm ${view === 'serial' ? 'primary' : ''}`} onClick={() => setParams({})}><Terminal />Serial console</button>
          <button className={`sm ${view === 'kvm' ? 'primary' : ''}`} onClick={() => setParams({ view: 'kvm' })}><MonitorPlay />Graphical KVM</button>
        </div>
      </div>

      {view === 'serial' ? (
        <>
          <SerialConsole serverId={server.id} popoutTo={`/servers/${server.id}/console/full`} />
          <Card title="Serial console tips" style={{ marginTop: 16 }}>
            <ul style={{ margin: 0, paddingLeft: 18, color: 'var(--text-2)' }}>
              <li>Nothing on screen? Press Enter. A BIOS or GRUB menu redraws on the next keystroke.</li>
              <li>Still nothing after a reset? Console redirection is off in the BIOS: run <strong>Prepare BMC</strong> from the BMC tab, then power-cycle.</li>
              <li>The BMC allows one serial viewer at a time. <strong>Take over</strong> drops the other session.</li>
              <li>Function keys are caught by the browser; use the toolbar. F2 opens BIOS setup, F6 the boot menu, F12 PXE.</li>
            </ul>
          </Card>
        </>
      ) : (
        <Card title="Graphical console (vKVM)" icon={<MonitorPlay />}>
          <KvmViewer serverId={server.id} cimcIp={server.cimc_ip} port={server.redfish_port} autoConnect />
        </Card>
      )}
    </>
  )
}
