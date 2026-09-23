import { Link, useParams } from 'react-router-dom'
import { PageHeader } from '../components'
import SerialConsole from '../console/SerialConsole'
import { PowerControls } from '../power'

/** The serial console on a page of its own, sized to the window. */
export default function Console({ isAdmin = false }: { isAdmin?: boolean }) {
  const { id = '' } = useParams()
  const back = isAdmin ? `/admin/servers/${id}` : `/servers/${id}`

  return (
    <main className="page wide">
      <PageHeader
        title="Serial console"
        sub="Serial-over-LAN through the platform: BIOS, boot loader, kernel and login, from power-on. The BMC allows one viewer at a time."
        actions={<Link className="button" to={back}>Back to server</Link>}
      />
      <div className="card"><div className="card-body tight"><PowerControls serverId={id} isAdmin={isAdmin} compact /></div></div>
      <SerialConsole serverId={id} fill />
      <p className="faint small" style={{ marginTop: 10 }}>
        Nothing on screen? Press Enter. Still nothing after a reset? Console redirection may be
        off in the BIOS: run <strong>Prepare BMC</strong>, then power-cycle. Function keys are often
        caught by the browser; use the toolbar buttons.
      </p>
    </main>
  )
}
