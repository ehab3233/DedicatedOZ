import { Download, FolderSearch, Trash2, Upload } from 'lucide-react'
import { useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { api, uploadImage, type Image } from '../../api'
import { Banner, Card, Empty, Modal, PageHeader, Pill, Progress, formatBytes, formatTime, relativeTime, useConfirm, useNow } from '../../components'
import { useAsync, usePolling, waitForJob } from '../../hooks'
import { useToast } from '../../toast'

export default function Images() {
  const images = useAsync(() => api.images())
  const toast = useToast()
  const confirm = useConfirm()
  const now = useNow()
  const [fetching, setFetching] = useState(false)
  const fetchingAny = (images.data ?? []).some((i) => i.status === 'fetching')
  usePolling(images.reload, 5000, fetchingAny)

  async function scan() {
    const found = await toast.run(() => api.scanImages())
    if (found) {
      toast.ok(found.length ? `Catalogued ${found.length} image${found.length > 1 ? 's' : ''}` : 'Nothing new in the image directory')
      await images.reload()
    }
  }

  async function remove(image: Image) {
    if (!(await confirm({ title: `Delete ${image.name}?`, body: `${image.filename} (${formatBytes(image.size_bytes)}) is removed from the management server. A server that still has it mounted keeps whatever the BMC already read.`, confirmLabel: 'Delete', danger: true }))) return
    await toast.run(async () => { await api.deleteImage(image.id); await images.reload() }, 'Image deleted')
  }

  return (
    <main className="page">
      <PageHeader
        title="Images"
        sub="ISO images on the management server. A server installs from one by mounting it as virtual media on its BMC and booting from it."
        actions={
          <>
            <button onClick={scan} title="Catalogue ISOs copied into the image directory by hand"><FolderSearch />Scan directory</button>
            <button onClick={() => setFetching(true)}><Download />Fetch from URL</button>
          </>
        }
      />

      {images.error && <Banner kind="error">{images.error}</Banner>}

      <UploadCard onUploaded={images.reload} />

      <Card flush>
        {!images.data ? (
          <Empty>Loading…</Empty>
        ) : images.data.length === 0 ? (
          <Empty>No images yet. Upload an ISO above, fetch one from a URL, or copy files into the image directory and scan.</Empty>
        ) : (
          <div className="table-scroll">
            <table>
              <thead><tr><th>Image</th><th>File</th><th className="right">Size</th><th>SHA-256</th><th>Status</th><th>Added</th><th className="actions" /></tr></thead>
              <tbody>
                {images.data.map((image) => (
                  <tr key={image.id}>
                    <td>
                      <strong>{image.name}</strong>
                      {image.notes && <div className="cell-sub">{image.notes}</div>}
                      {image.source_url && <div className="cell-sub mono truncate" style={{ maxWidth: 360 }} title={image.source_url}>{image.source_url}</div>}
                    </td>
                    <td className="mono subtle"><a href={image.url} target="_blank" rel="noreferrer" title={image.url}>{image.filename}</a></td>
                    <td className="right num">{image.size_bytes ? formatBytes(image.size_bytes) : '—'}</td>
                    <td className="mono faint" title={image.sha256 ?? ''}>{image.sha256 ? `${image.sha256.slice(0, 12)}…` : '—'}</td>
                    <td>
                      <Pill value={image.status} />
                      {image.error && <div className="cell-sub" style={{ color: 'var(--crit)' }}>{image.error}</div>}
                    </td>
                    <td className="subtle nowrap" title={formatTime(image.created_at)}>{relativeTime(image.created_at, now)}{image.uploaded_by && <div className="cell-sub">{image.uploaded_by}</div>}</td>
                    <td className="actions"><button className="sm danger" onClick={() => remove(image)} disabled={image.status === 'fetching'}><Trash2 />Delete</button></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      <p className="faint small">
        Images are served to BMCs over plain HTTP from the boot-asset port. To use one, open a server and choose <strong>Install from image</strong>. <Link to="/admin/servers">Servers</Link>
      </p>

      {fetching && (
        <FetchModal onClose={() => setFetching(false)} onQueued={async () => { setFetching(false); await images.reload() }} />
      )}
    </main>
  )
}

function UploadCard({ onUploaded }: { onUploaded: () => Promise<void> }) {
  const toast = useToast()
  const inputRef = useRef<HTMLInputElement>(null)
  const [file, setFile] = useState<File | null>(null)
  const [name, setName] = useState('')
  const [progress, setProgress] = useState<number | null>(null)
  const [dragging, setDragging] = useState(false)
  const abortRef = useRef<(() => void) | null>(null)

  function choose(f: File | null) {
    setFile(f)
    if (f && !name) setName(f.name.replace(/\.(iso|img)$/i, ''))
  }

  async function start() {
    if (!file) return
    setProgress(0)
    const { promise, abort } = uploadImage(file, name.trim() || file.name, setProgress)
    abortRef.current = abort
    try {
      const image = await promise
      toast.ok(`${image.name} uploaded (${formatBytes(image.size_bytes)})`)
      setFile(null)
      setName('')
      if (inputRef.current) inputRef.current.value = ''
      await onUploaded()
    } catch (e) {
      toast.error(e instanceof Error ? e.message : String(e))
    } finally {
      setProgress(null)
      abortRef.current = null
    }
  }

  return (
    <Card title="Upload an ISO" icon={<Upload />}>
      <div
        className={`upload-zone ${dragging ? 'active' : ''}`}
        onDragOver={(e) => { e.preventDefault(); setDragging(true) }}
        onDragLeave={() => setDragging(false)}
        onDrop={(e) => { e.preventDefault(); setDragging(false); choose(e.dataTransfer.files[0] ?? null) }}
        onClick={() => inputRef.current?.click()}
        role="button"
      >
        <input ref={inputRef} type="file" accept=".iso,.img,application/x-iso9660-image" onChange={(e) => choose(e.target.files?.[0] ?? null)} />
        {file ? (
          <span><strong>{file.name}</strong> · {formatBytes(file.size)}</span>
        ) : (
          <span>Drop an ISO here, or click to choose one. Any size: it streams straight to disk.</span>
        )}
      </div>
      <div className="row" style={{ marginTop: 12 }}>
        <input placeholder="Display name" value={name} onChange={(e) => setName(e.target.value)} style={{ maxWidth: 320 }} disabled={progress != null} />
        {progress == null ? (
          <button className="primary" disabled={!file} onClick={start}><Upload />Upload</button>
        ) : (
          <button onClick={() => abortRef.current?.()}>Cancel</button>
        )}
      </div>
      {progress != null && (
        <div style={{ marginTop: 12 }}>
          <div className="spread small subtle" style={{ marginBottom: 4 }}>
            <span>{progress >= 1 ? 'Finishing: checksum and catalogue…' : 'Uploading…'}</span>
            <span className="num">{Math.round(progress * 100)}%</span>
          </div>
          <Progress value={progress * 100} active={progress >= 1} />
        </div>
      )}
    </Card>
  )
}

function FetchModal({ onClose, onQueued }: { onClose: () => void; onQueued: () => Promise<void> }) {
  const toast = useToast()
  const [url, setUrl] = useState('')
  const [name, setName] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function submit() {
    setBusy(true)
    setError(null)
    try {
      const { image, job } = await api.fetchImage(url.trim(), name.trim() || undefined)
      toast.info(`Downloading ${image.filename} on the management server…`)
      await onQueued()
      void waitForJob(job.id).then(
        (done) => toast.ok(done.stage ?? `${image.name} downloaded`),
        (e) => toast.error(`${image.name}: ${e instanceof Error ? e.message : e}`),
      )
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
      setBusy(false)
    }
  }

  return (
    <Modal
      title="Fetch an image from a URL"
      onClose={onClose}
      footer={<><button onClick={onClose}>Cancel</button><button className="primary" disabled={busy || !url.trim()} onClick={submit}>{busy ? 'Queueing…' : 'Fetch'}</button></>}
    >
      {error && <Banner kind="error">{error}</Banner>}
      <p className="subtle" style={{ marginBottom: 12 }}>The management server downloads it directly, which is far faster than uploading through a browser. Progress shows on the Jobs page and in the list here.</p>
      <div className="field"><label htmlFor="url">URL</label><input id="url" className="mono" value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://releases.ubuntu.com/24.04/ubuntu-24.04.2-live-server-amd64.iso" autoFocus /></div>
      <div className="field"><label htmlFor="name">Display name (optional)</label><input id="name" value={name} onChange={(e) => setName(e.target.value)} placeholder="Ubuntu 24.04 Server" /></div>
    </Modal>
  )
}
