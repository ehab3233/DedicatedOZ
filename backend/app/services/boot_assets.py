"""What the netboot rails need on disk, and whether it is there.

The iPXE scripts fetch the installer ramdisk and each OS template's kernel
and initrd (and, for Ubuntu, the live ISO its initrd pulls the squashfs
from) from `boot_asset_base_url`, which nginx serves out of `asset_dir()`.
Nothing in the database says whether those files exist: fetch-os-images.sh
and build-ramdisk.sh put them there, and this is how the panel and the
provisioning jobs find out what they did.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import OSTemplate

#: What every rail boots first, built by `doz.sh ramdisk`.
RAMDISK_FILES = ("doz-installer/vmlinuz", "doz-installer/initrd.img")
#: `{{ boot_asset_base_url }}/some/path` inside a template's kernel arguments.
_ASSET_REF = re.compile(r"\{\{\s*boot_asset_base_url\s*\}\}/([^\s\"']+)")


def asset_dir() -> Path:
    """The directory nginx serves at `boot_asset_base_url`."""
    if settings.boot_asset_dir:
        return Path(settings.boot_asset_dir)
    return Path(__file__).resolve().parents[3] / "installer" / "assets"


def asset_url(rel: str) -> str:
    return f"{settings.boot_asset_base_url.rstrip('/')}/{rel.lstrip('/')}"


def file_status(rel: str, role: str) -> dict:
    """One file under the asset directory: is it there, how big, how old."""
    rel = rel.lstrip("/")
    entry: dict = {
        "role": role, "path": rel, "url": asset_url(rel),
        "present": False, "size_bytes": None, "modified_at": None,
    }
    try:
        stat = (asset_dir() / rel).stat()
    except OSError:
        return entry
    entry.update(
        present=stat.st_size > 0,
        size_bytes=stat.st_size,
        modified_at=datetime.fromtimestamp(stat.st_mtime, UTC),
    )
    return entry


def _is_local(path: str | None) -> bool:
    return bool(path) and not str(path).startswith(("http://", "https://", "tftp://"))


def template_files(template: OSTemplate) -> list[dict]:
    """The files a template's iPXE script and kernel fetch from this server."""
    files: list[dict] = []
    if _is_local(template.kernel_path):
        files.append(file_status(template.kernel_path or "", "kernel"))
    if _is_local(template.initrd_path):
        files.append(file_status(template.initrd_path or "", "initrd"))
    if _is_local(template.iso_path):
        files.append(file_status(template.iso_path or "", "iso"))
    for ref in _ASSET_REF.findall(template.kernel_args or ""):
        files.append(file_status(ref, "iso" if ref.lower().endswith(".iso") else "file"))
    return files


#: The iPXE loaders dnsmasq serves over TFTP.
LOADER_FILES = (("undionly.kpxe", "BIOS"), ("ipxe.efi", "UEFI"))


def tftp_dir() -> Path:
    """Where the installer puts the TFTP root: next to the asset directory."""
    return asset_dir().parent / "tftp"


def loaders() -> dict:
    """The iPXE loaders a PXE ROM fetches first, and what they chain to.

    Stock loaders boot whatever filename DHCP gives them next, which loops
    on most DHCP servers; ones built by build-ipxe.sh chain to the control
    plane themselves, and record where in `.embedded-url`.
    """
    root = tftp_dir()
    files = []
    for name, role in LOADER_FILES:
        entry = {"role": role, "path": f"tftp/{name}", "url": f"tftp://{name}",
                 "present": False, "size_bytes": None, "modified_at": None}
        try:
            stat = (root / name).stat()
            entry.update(present=stat.st_size > 0, size_bytes=stat.st_size,
                         modified_at=datetime.fromtimestamp(stat.st_mtime, UTC))
        except OSError:
            pass
        files.append(entry)
    try:
        embedded = (root / ".embedded-url").read_text().strip() or None
    except OSError:
        embedded = None
    expected = settings.control_plane_url.rstrip("/")
    return {
        "files": files,
        "present": any(f["present"] for f in files),
        "embedded_url": embedded,
        "expected_url": expected,
        "ready": files[0]["present"] and embedded == expected,
    }


def ramdisk_files() -> list[dict]:
    return [
        file_status(rel, "kernel" if rel.endswith("vmlinuz") else "initrd")
        for rel in RAMDISK_FILES
    ]


def missing_for(template: OSTemplate | None) -> list[str]:
    """Paths a netboot of `template` (or of the bare ramdisk) would 404 on."""
    files = ramdisk_files() + (template_files(template) if template else [])
    return [f["path"] for f in files if not f["present"]]


def report(db: Session) -> dict:
    ramdisk = ramdisk_files()
    ramdisk_ready = all(f["present"] for f in ramdisk)
    templates = []
    rows = db.execute(
        select(OSTemplate)
        .where(OSTemplate.is_enabled.is_(True))
        .order_by(OSTemplate.sort_order, OSTemplate.name)
    ).scalars()
    for template in rows:
        files = template_files(template)
        templates.append({
            "id": template.id,
            "slug": template.slug,
            "name": template.name,
            "version": template.version,
            "install_method": template.install_method,
            "is_public": template.is_public,
            "files": files,
            "ready": ramdisk_ready and all(f["present"] for f in files),
        })
    return {
        "asset_dir": str(asset_dir()),
        "base_url": settings.boot_asset_base_url.rstrip("/"),
        "ramdisk": {"files": ramdisk, "ready": ramdisk_ready},
        "loaders": loaders(),
        "templates": templates,
    }
