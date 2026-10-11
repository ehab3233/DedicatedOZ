import { MonitorPlay } from 'lucide-react'
import { Card } from '../../../components'
import SerialConsole from '../../../console/SerialConsole'
import { usePortalServer } from './ServerPage'

export default function PortalConsoleTab() {
  const { server } = usePortalServer()
  return (
    <>
      <SerialConsole serverId={server.id} popoutTo={`/servers/${server.id}/console/full`} />
      <div className="grid cols-2" style={{ marginTop: 16 }}>
        <Card title="Using the serial console">
          <ul style={{ margin: 0, paddingLeft: 18, color: 'var(--text-2)' }}>
            <li>It shows the server from power-on: BIOS, boot loader, kernel messages and the login prompt.</li>
            <li>Nothing on screen? Press Enter. A boot menu redraws on the next keystroke.</li>
            <li>One viewer at a time. If a session is already open elsewhere you are offered to take it over.</li>
            <li>Function keys are caught by the browser; use the toolbar buttons for F2, F6 and F12.</li>
          </ul>
        </Card>
        <Card title="Graphical console" icon={<MonitorPlay />}>
          <p className="subtle" style={{ margin: 0 }}>
            A full graphical console with virtual media is on its way to the portal. Until then the serial console covers
            BIOS, boot and login, and rescue mode covers repairs; if you need the graphical console for something specific, contact support.
          </p>
        </Card>
      </div>
    </>
  )
}
