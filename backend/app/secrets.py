"""Credential storage for BMCs.

CIMC passwords never enter Postgres. `Server.cimc_credential_ref` is a lookup
key; this module resolves it against whichever backend is configured. The env
and file backends exist so the bench and docker-compose work without standing
up Vault first — production should run `secrets_backend=vault`.
"""

from __future__ import annotations

import json
import os
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import requests

from app.config import settings

_SAFE_REF = re.compile(r"^[A-Za-z0-9_./-]{1,255}$")


@dataclass(frozen=True)
class BMCCredential:
    username: str
    password: str

    def __repr__(self) -> str:  # never let a password reach a traceback
        return f"BMCCredential(username={self.username!r}, password='***')"


class SecretNotFoundError(KeyError):
    pass


class SecretsBackend(ABC):
    @abstractmethod
    def get_bmc_credential(self, ref: str) -> BMCCredential: ...

    @abstractmethod
    def put_bmc_credential(self, ref: str, cred: BMCCredential) -> None: ...

    @staticmethod
    def validate_ref(ref: str) -> str:
        # Refs come from the database but are used to build paths and env var
        # names, so they get validated at the boundary regardless.
        if not _SAFE_REF.match(ref or ""):
            raise ValueError(f"invalid credential ref: {ref!r}")
        if ".." in ref:
            raise ValueError(f"invalid credential ref: {ref!r}")
        return ref


class EnvSecretsBackend(SecretsBackend):
    """Reads `DOZ_CIMC_<REF>_USER` / `_PASS`, with a fleet-wide fallback.

    Suitable for the bench, where the whole fleet shares one password and the
    point is to test hardware, not secret management.
    """

    def _env_key(self, ref: str) -> str:
        return re.sub(r"[^A-Z0-9]", "_", ref.upper())

    def get_bmc_credential(self, ref: str) -> BMCCredential:
        self.validate_ref(ref)
        key = self._env_key(ref)
        user = os.environ.get(f"DOZ_CIMC_{key}_USER") or os.environ.get("DOZ_CIMC_DEFAULT_USER")
        password = os.environ.get(f"DOZ_CIMC_{key}_PASS") or os.environ.get("DOZ_CIMC_DEFAULT_PASS")
        if not user or not password:
            raise SecretNotFoundError(f"no credential for ref {ref!r} in environment")
        return BMCCredential(username=user, password=password)

    def put_bmc_credential(self, ref: str, cred: BMCCredential) -> None:
        raise NotImplementedError("env backend is read-only; set the variables instead")


class FileSecretsBackend(SecretsBackend):
    """One JSON file per ref under `secrets_file_dir`. Mount it read-only."""

    def _path(self, ref: str) -> Path:
        self.validate_ref(ref)
        base = Path(settings.secrets_file_dir).resolve()
        path = (base / f"{ref}.json").resolve()
        if not str(path).startswith(str(base) + os.sep):
            raise ValueError(f"credential ref escapes secrets dir: {ref!r}")
        return path

    def get_bmc_credential(self, ref: str) -> BMCCredential:
        path = self._path(ref)
        if not path.is_file():
            raise SecretNotFoundError(f"no credential file for ref {ref!r}")
        data = json.loads(path.read_text())
        return BMCCredential(username=data["username"], password=data["password"])

    def put_bmc_credential(self, ref: str, cred: BMCCredential) -> None:
        path = self._path(ref)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"username": cred.username, "password": cred.password}))
        path.chmod(0o600)


class VaultSecretsBackend(SecretsBackend):
    """HashiCorp Vault KV v2."""

    def __init__(self) -> None:
        if not settings.vault_addr or not settings.vault_token:
            raise RuntimeError("vault backend selected but vault_addr/vault_token are unset")
        self._session = requests.Session()
        self._session.headers["X-Vault-Token"] = settings.vault_token

    def _url(self, ref: str) -> str:
        self.validate_ref(ref)
        return f"{settings.vault_addr.rstrip('/')}/v1/{settings.vault_mount}/data/{ref}"

    def get_bmc_credential(self, ref: str) -> BMCCredential:
        resp = self._session.get(self._url(ref), timeout=10)
        if resp.status_code == 404:
            raise SecretNotFoundError(f"no credential at {ref!r}")
        resp.raise_for_status()
        data = resp.json()["data"]["data"]
        return BMCCredential(username=data["username"], password=data["password"])

    def put_bmc_credential(self, ref: str, cred: BMCCredential) -> None:
        resp = self._session.post(
            self._url(ref),
            json={"data": {"username": cred.username, "password": cred.password}},
            timeout=10,
        )
        resp.raise_for_status()


@lru_cache
def get_secrets_backend() -> SecretsBackend:
    backend = settings.secrets_backend.lower()
    if backend == "env":
        return EnvSecretsBackend()
    if backend == "file":
        return FileSecretsBackend()
    if backend == "vault":
        return VaultSecretsBackend()
    raise ValueError(f"unknown secrets backend {settings.secrets_backend!r}")
