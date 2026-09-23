#!/usr/bin/env python3
"""Create the schema and seed the minimum a working install needs.

Idempotent: safe to run against an existing database. Intended for the bench
and for docker-compose. Schema changes after first deploy go through Alembic,
not through this script.

    python -m scripts.init_db --admin-email you@example.com --admin-password ...
"""

from __future__ import annotations

import argparse
import secrets
import sys

from sqlalchemy import select

from app.db import SessionLocal, engine
from app.enums import InstallMethod, RaidLevel
from app.models import Customer, OSTemplate
from app.schema import upgrade_schema
from app.security import hash_password

#: The three-option list the spec calls for, plus the two hidden templates the
#: rescue and wipe rails run on.
DEFAULT_TEMPLATES = [
    {
        "slug": "ubuntu-22.04",
        "name": "Ubuntu Server",
        "family": "debian",
        "version": "22.04 LTS",
        "install_method": InstallMethod.AUTOINSTALL,
        "kernel_path": "/os/ubuntu-22.04/casper/vmlinuz",
        "initrd_path": "/os/ubuntu-22.04/casper/initrd",
        # The casper initrd fetches the live squashfs from this ISO. Without
        # it the installer boots to a prompt and waits forever.
        "kernel_args": (
            "url={{ boot_asset_base_url }}/os/ubuntu-22.04/ubuntu-22.04-live-server-amd64.iso"
        ),
        "config_template": "ubuntu-2204-autoinstall.yaml.j2",
        "default_raid_level": RaidLevel.RAID1,
        "sort_order": 10,
    },
    {
        "slug": "debian-12",
        "name": "Debian",
        "family": "debian",
        "version": "12 (bookworm)",
        "install_method": InstallMethod.PRESEED,
        "kernel_path": "/os/debian-12/linux",
        "initrd_path": "/os/debian-12/initrd.gz",
        "kernel_args": "interface=auto netcfg/dhcp_timeout=60",
        "config_template": "debian-12-preseed.cfg.j2",
        "default_raid_level": RaidLevel.RAID1,
        "sort_order": 20,
    },
    {
        "slug": "rocky-9",
        "name": "Rocky Linux",
        "family": "rhel",
        "version": "9",
        "install_method": InstallMethod.KICKSTART,
        "kernel_path": "/os/rocky-9/images/pxeboot/vmlinuz",
        "initrd_path": "/os/rocky-9/images/pxeboot/initrd.img",
        "kernel_args": "inst.repo=http://mirror.rockylinux.org/rocky/9/BaseOS/x86_64/os/",
        "config_template": "rocky-9-kickstart.cfg.j2",
        "default_raid_level": RaidLevel.RAID1,
        "sort_order": 30,
    },
    {
        "slug": "_rescue",
        "name": "Rescue environment",
        "family": "rescue",
        "version": "1",
        "install_method": InstallMethod.IMAGE,
        "config_template": "ubuntu-2204-autoinstall.yaml.j2",
        "default_raid_level": RaidLevel.NONE,
        "is_public": False,
        "sort_order": 900,
    },
    {
        "slug": "_wipe",
        "name": "Secure wipe",
        "family": "wipe",
        "version": "1",
        "install_method": InstallMethod.IMAGE,
        "config_template": "ubuntu-2204-autoinstall.yaml.j2",
        "default_raid_level": RaidLevel.NONE,
        "is_public": False,
        "sort_order": 910,
    },
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--admin-email", default="admin@example.com")
    parser.add_argument(
        "--admin-password",
        default=None,
        help="omit to have one generated and printed once",
    )
    parser.add_argument(
        "--skip-templates", action="store_true", help="create the schema only"
    )
    args = parser.parse_args()

    print("creating / upgrading schema...")
    added = upgrade_schema(engine)
    for change in added:
        print(f"  added column {change}")

    with SessionLocal() as db:
        if not args.skip_templates:
            created = 0
            for spec in DEFAULT_TEMPLATES:
                exists = db.execute(
                    select(OSTemplate).where(OSTemplate.slug == spec["slug"])
                ).scalar_one_or_none()
                if exists:
                    continue
                db.add(OSTemplate(**spec))
                created += 1
            print(f"os templates: {created} created, {len(DEFAULT_TEMPLATES) - created} existing")

        email = args.admin_email.lower()
        admin = db.execute(
            select(Customer).where(Customer.email == email)
        ).scalar_one_or_none()

        if admin is None:
            password = args.admin_password or secrets.token_urlsafe(18)
            db.add(
                Customer(
                    email=email,
                    password_hash=hash_password(password),
                    contact_name="Platform admin",
                    is_admin=True,
                )
            )
            print(f"\nadmin account created: {email}")
            if not args.admin_password:
                # Printed once. There is no way to recover it afterwards.
                print(f"admin password:         {password}\n")
        else:
            print(f"admin account already exists: {email}")

        db.commit()

    print("done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
