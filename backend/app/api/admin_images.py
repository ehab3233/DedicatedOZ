"""Admin API for the ISO image store.

Images live on the management server and are served to BMCs over plain HTTP
from the boot-asset port. They get there three ways: uploaded from the
browser (streamed straight to disk, any size), downloaded by the management
server from a URL (a job, with progress), or copied into the directory by
hand and picked up by a scan.
"""

from __future__ import annotations

import hashlib
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import client_ip, current_admin
from app.enums import ActorType, JobType
from app.models import Customer, Image
from app.schemas import ImageFetchRequest, ImageOut, ImageUpdate, JobOut, NetbootReportOut
from app.services import boot_assets, images
from app.services import jobs as job_service
from app.services.audit import record_audit
from app.services.dispatch import enqueue

router = APIRouter(
    prefix="/api/v1/admin/images", tags=["admin"], dependencies=[Depends(current_admin)]
)


def _out(image: Image) -> ImageOut:
    out = ImageOut.model_validate(image)
    out.url = images.public_url(image.filename)
    return out


def _audit(db: Session, request: Request, admin: Customer, action: str, image: Image,
           detail: dict | None = None) -> None:
    record_audit(
        db,
        action=action,
        actor_type=ActorType.ADMIN,
        actor_id=admin.id,
        actor_label=admin.email,
        target_type="image",
        target_id=str(image.id),
        source_ip=client_ip(request),
        detail={"filename": image.filename, **(detail or {})},
    )


@router.get("", response_model=list[ImageOut])
def list_images(db: Session = Depends(get_db)) -> list[ImageOut]:
    rows = db.execute(select(Image).order_by(Image.name, Image.created_at)).scalars().all()
    return [_out(image) for image in rows]


@router.get("/netboot", response_model=NetbootReportOut)
def netboot_assets(db: Session = Depends(get_db)) -> dict:
    """The files PXE reinstalls boot (ramdisk, kernels, initrds, the Ubuntu
    ISO) and whether each is on disk. fetch-os-images.sh and `doz.sh ramdisk`
    put them there; this is how the panel shows what they did."""
    return boot_assets.report(db)


@router.put("/upload", response_model=ImageOut, status_code=status.HTTP_201_CREATED)
async def upload_image(
    request: Request,
    filename: str = Query(min_length=1, max_length=255),
    name: str | None = Query(default=None, max_length=255),
    db: Session = Depends(get_db),
    admin: Customer = Depends(current_admin),
) -> ImageOut:
    """Upload an ISO. The body is the file, streamed to disk as it arrives.

    A raw body rather than a multipart form so a multi-gigabyte image is
    never buffered in memory or spooled through a temp file first.
    """
    safe = images.unique_filename(db, images.safe_filename(filename))
    dest = images.image_dir() / safe
    part = dest.with_name(f"{safe}.part")
    digest = hashlib.sha256()
    size = 0
    try:
        with part.open("wb") as fh:
            async for chunk in request.stream():
                fh.write(chunk)
                digest.update(chunk)
                size += len(chunk)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    if size == 0:
        part.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="the upload was empty")
    part.replace(dest)

    image = Image(
        name=name or dest.stem,
        filename=safe,
        size_bytes=size,
        sha256=digest.hexdigest(),
        status="ready",
        uploaded_by=admin.email,
    )
    db.add(image)
    db.flush()
    _audit(db, request, admin, "image.uploaded", image, {"size_bytes": size})
    db.commit()
    db.refresh(image)
    return _out(image)


@router.post("/fetch", status_code=status.HTTP_202_ACCEPTED)
def fetch_image(
    payload: ImageFetchRequest,
    request: Request,
    db: Session = Depends(get_db),
    admin: Customer = Depends(current_admin),
) -> dict:
    """Have the management server download an ISO from a URL. Returns the
    catalogue entry (status `fetching`) and the job to watch."""
    url = payload.url.strip()
    if not url.lower().startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="the URL must start with http:// or https://")
    filename = images.unique_filename(db, images.filename_from_url(url))
    image = Image(
        name=payload.name or filename.rsplit(".", 1)[0],
        filename=filename,
        source_url=url,
        status="fetching",
        uploaded_by=admin.email,
        notes=payload.notes,
    )
    db.add(image)
    db.flush()
    job, _ = job_service.create_job(
        db,
        job_type=JobType.IMAGE_FETCH,
        server_id=None,
        payload={"image_id": str(image.id), "url": url},
        requested_by_id=admin.id,
        requested_by_type=ActorType.ADMIN,
    )
    _audit(db, request, admin, "image.fetch", image, {"url": url, "job_id": str(job.id)})
    db.commit()
    db.refresh(job)
    db.refresh(image)
    job = enqueue(db, job)
    return {"image": _out(image), "job": JobOut.model_validate(job)}


@router.post("/scan", response_model=list[ImageOut])
def scan_images(
    request: Request,
    db: Session = Depends(get_db),
    admin: Customer = Depends(current_admin),
) -> list[ImageOut]:
    """Catalogue ISOs copied into the image directory by hand."""
    found = images.scan(db)
    for image in found:
        _audit(db, request, admin, "image.scanned", image)
    db.commit()
    return [_out(image) for image in found]


@router.patch("/{image_id}", response_model=ImageOut)
def update_image(
    image_id: uuid.UUID, payload: ImageUpdate, db: Session = Depends(get_db)
) -> ImageOut:
    image = db.get(Image, image_id)
    if image is None:
        raise HTTPException(status_code=404, detail="image not found")
    if payload.name is not None:
        image.name = payload.name
    if payload.notes is not None:
        image.notes = payload.notes or None
    db.add(image)
    db.commit()
    db.refresh(image)
    return _out(image)


@router.delete("/{image_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_image(
    image_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    admin: Customer = Depends(current_admin),
) -> None:
    """Remove the file and the catalogue entry. A server that still has it
    mounted keeps whatever the BMC already read; the next mount fails."""
    image = db.get(Image, image_id)
    if image is None:
        raise HTTPException(status_code=404, detail="image not found")
    if image.status == "fetching":
        raise HTTPException(status_code=409, detail="cancel the download job first")
    images.remove_file(image)
    _audit(db, request, admin, "image.deleted", image)
    db.delete(image)
    db.commit()
