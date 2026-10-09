"""The ISO image store.

Images are plain files on the management server, under `settings.image_dir`,
which nginx serves on the boot-asset port. A BMC mounting one as virtual
media fetches it from there over HTTP -- the same path the netboot rail
already uses for kernels and initrds, so nothing new has to be reachable.

The database row is a catalogue entry: the file is the truth. Size and
checksum are measured from the bytes on disk, never taken from an upload
header or a download's claim.
"""

from __future__ import annotations

import hashlib
import os
import re
import uuid
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlparse

import requests
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Image
from app.services import boot_assets

CHUNK = 1024 * 1024
IMAGE_SUFFIXES = (".iso", ".img")

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def image_dir() -> Path:
    """Where the files live. Created on first use."""
    if settings.image_dir:
        path = Path(settings.image_dir)
    else:
        path = boot_assets.asset_dir() / "iso"
    path.mkdir(parents=True, exist_ok=True)
    return path


def public_url(filename: str) -> str:
    """Where a BMC fetches the image from."""
    return f"{settings.boot_asset_base_url.rstrip('/')}/iso/{filename}"


def safe_filename(name: str) -> str:
    """A basename with nothing a path or a shell could misread, ending in .iso/.img."""
    base = os.path.basename(name.strip().replace("\\", "/"))
    base = _UNSAFE.sub("-", base).strip("-.")
    if not base:
        base = f"image-{uuid.uuid4().hex[:8]}"
    if not base.lower().endswith(IMAGE_SUFFIXES):
        base += ".iso"
    return base[:200]


def unique_filename(db: Session, wanted: str) -> str:
    """`wanted`, or `wanted-2`, `-3`... if a row or a file already has that name."""
    stem, suffix = os.path.splitext(wanted)
    candidate = wanted
    n = 1
    while _taken(db, candidate):
        n += 1
        candidate = f"{stem}-{n}{suffix}"
    return candidate


def _taken(db: Session, filename: str) -> bool:
    if (image_dir() / filename).exists() or (image_dir() / f"{filename}.part").exists():
        return True
    return db.execute(select(Image.id).where(Image.filename == filename)).first() is not None


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def register_file(
    db: Session,
    path: Path,
    *,
    name: str | None = None,
    uploaded_by: str | None = None,
    source_url: str | None = None,
    notes: str | None = None,
) -> Image:
    """Catalogue a file that is already complete on disk."""
    image = Image(
        name=name or path.stem,
        filename=path.name,
        size_bytes=path.stat().st_size,
        sha256=sha256_of(path),
        source_url=source_url,
        status="ready",
        uploaded_by=uploaded_by,
        notes=notes,
    )
    db.add(image)
    db.flush()
    return image


def scan(db: Session) -> list[Image]:
    """Catalogue ISOs someone copied into the directory by hand."""
    known = set(db.execute(select(Image.filename)).scalars().all())
    found: list[Image] = []
    for path in sorted(image_dir().iterdir()):
        if not path.is_file() or path.name in known:
            continue
        if not path.name.lower().endswith(IMAGE_SUFFIXES):
            continue
        found.append(register_file(db, path))
    return found


def remove_file(image: Image) -> None:
    for candidate in (image_dir() / image.filename, image_dir() / f"{image.filename}.part"):
        candidate.unlink(missing_ok=True)


def filename_from_url(url: str) -> str:
    return safe_filename(os.path.basename(urlparse(url).path) or "download.iso")


def download(
    url: str,
    dest: Path,
    *,
    progress: Callable[[int, int | None], None] | None = None,
    session: requests.Session | None = None,
) -> tuple[int, str]:
    """Stream `url` into `dest`. Returns (bytes written, sha256).

    `progress(done, total)` is called every 16 MiB and at the end; `total` is
    None when the server sends no Content-Length.
    """
    http = session or requests.Session()
    http.trust_env = False
    digest = hashlib.sha256()
    done = 0
    with http.get(url, stream=True, timeout=(15, 120)) as response:
        response.raise_for_status()
        length = response.headers.get("Content-Length")
        total = int(length) if length and length.isdigit() else None
        next_report = 16 * CHUNK
        with dest.open("wb") as fh:
            for chunk in response.iter_content(chunk_size=CHUNK):
                if not chunk:
                    continue
                fh.write(chunk)
                digest.update(chunk)
                done += len(chunk)
                if progress and done >= next_report:
                    progress(done, total)
                    next_report = done + 16 * CHUNK
    if progress:
        progress(done, total)
    return done, digest.hexdigest()
