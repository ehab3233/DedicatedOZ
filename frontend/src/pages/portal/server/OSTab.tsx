import { HardDriveDownload, LifeBuoy } from 'lucide-react'
import { useState } from 'react'
import { Card } from '../../../components'
import { useToast } from '../../../toast'
import { ReinstallModal, RescueModal } from './modals'
import { usePortalServer } from './ServerPage'

export default function OSTab() {
  const { server: s, activeJob, refresh } = usePortalServer()
  const toast = useToast()
  const [modal, setModal] = useState<null | 'reinstall' | 'rescue'>(null)
  const locked = Boolean(activeJob)

  return (
    <>
      <div className="grid cols-2">
        <Card title="Reinstall" icon={<HardDriveDownload />} actions={<button className="danger" disabled={locked} onClick={() => setModal('reinstall')}>Reinstall…</button>}>
          <p className="subtle" style={{ margin: 0 }}>
            A clean install of the operating system you choose, onto a freshly built array. Every disk is erased; there is no undo and we keep no backup.
            Takes fifteen to twenty minutes; you can follow it step by step, and the serial console shows the installer the whole way.
          </p>
          <ol className="subtle small" style={{ margin: '10px 0 0', paddingLeft: 18 }}>
            <li>The array is rebuilt at the level you pick (RAID 1 unless you say otherwise).</li>
            <li>The installer runs with your hostname, your SSH keys and the server's address.</li>
            <li>The server reboots into the new system; root logs in with your keys.</li>
          </ol>
        </Card>
        <Card title="Rescue mode" icon={<LifeBuoy />} actions={<button disabled={locked} onClick={() => setModal('rescue')}>Boot rescue…</button>}>
          <p className="subtle" style={{ margin: 0 }}>
            Reboots the server into a Linux system that runs entirely in memory. Your disks are left as they are and can be mounted to repair a boot problem,
            recover data or reset a password. Log in as root with your SSH keys; reboot from here to return to the installed system.
          </p>
        </Card>
      </div>

      {modal === 'reinstall' && (
        <ReinstallModal serverId={s.id} hostname={s.hostname} onClose={() => setModal(null)} onQueued={async () => { setModal(null); toast.ok('Reinstall queued. About twenty minutes; the Activity tab follows it.'); await refresh() }} />
      )}
      {modal === 'rescue' && (
        <RescueModal serverId={s.id} onClose={() => setModal(null)} onQueued={async () => { setModal(null); toast.ok('Rescue boot queued.'); await refresh() }} />
      )}
    </>
  )
}
