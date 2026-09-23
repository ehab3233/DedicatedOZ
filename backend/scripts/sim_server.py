#!/usr/bin/env python3
"""Register the BMC simulator (deploy/sim) as a server in the panel.

    python -m scripts.sim_server --port 9623 --user admin --password sim-password

Creates or updates server SIM-0001 pointing at 127.0.0.1 over IPMI, and makes
sure the platform can find the simulator's password:

* file / vault secrets backends: the credential is written there;
* env backend (development): the simulator should have been started with
  DOZ_CIMC_DEFAULT_USER / DOZ_CIMC_DEFAULT_PASS, which `doz.sh sim start`
  does -- this script checks they resolve.
"""

from __future__ import annotations

import argparse
import sys

from sqlalchemy import select

from app.db import SessionLocal
from app.enums import ServerState
from app.models import Server
from app.secrets import BMCCredential, SecretNotFoundError, get_secrets_backend

SERIAL = "SIM-0001"
REF = f"cimc/{SERIAL}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=9623)
    parser.add_argument("--user", default="admin")
    parser.add_argument("--password", default="sim-password")
    parser.add_argument("--remove", action="store_true", help="delete the simulated server")
    args = parser.parse_args()

    with SessionLocal() as db:
        server = db.execute(select(Server).where(Server.serial == SERIAL)).scalar_one_or_none()

        if args.remove:
            if server is not None:
                db.delete(server)
                db.commit()
                print(f"removed {SERIAL}")
            return 0

        backend = get_secrets_backend()
        try:
            backend.put_bmc_credential(REF, BMCCredential(args.user, args.password))
            print(f"credential stored under {REF}")
        except NotImplementedError:
            try:
                cred = backend.get_bmc_credential(REF)
            except SecretNotFoundError:
                print("the env secrets backend has no credential for the simulator; set "
                      "DOZ_CIMC_DEFAULT_USER / DOZ_CIMC_DEFAULT_PASS", file=sys.stderr)
                return 1
            if (cred.username, cred.password) != (args.user, args.password):
                print("warning: the env backend's credential differs from the simulator's; "
                      "start the simulator with the DOZ_CIMC_DEFAULT_* values", file=sys.stderr)

        if server is None:
            clash = db.execute(
                select(Server).where(Server.cimc_ip == "127.0.0.1")
            ).scalar_one_or_none()
            if clash is not None:
                print(f"another server ({clash.serial}) already uses 127.0.0.1; "
                      "remove it or change its CIMC address first", file=sys.stderr)
                return 1
            server = Server(serial=SERIAL, cimc_ip="127.0.0.1", cimc_credential_ref=REF)
            db.add(server)
        server.model = "Simulated C220 M4"
        server.hostname = server.hostname or "sim01"
        server.bmc_protocol = "ipmi"
        server.ipmi_port = args.port
        server.rack = server.rack or "SIM"
        server.cimc_credential_ref = REF
        if server.state not in (ServerState.ACTIVE, ServerState.ACTIVE.value):
            server.state = ServerState.ACTIVE
        server.notes = (
            "BMC simulator (deploy/sim): OpenIPMI ipmi_sim with a fake serial console. "
            "Power, reset and the serial console behave like a real IPMI BMC; there is no "
            "Redfish, KVM or real hardware behind it."
        )
        db.commit()
        print(f"{SERIAL} registered: IPMI 127.0.0.1:{args.port}, protocol ipmi")
    return 0


if __name__ == "__main__":
    sys.exit(main())
