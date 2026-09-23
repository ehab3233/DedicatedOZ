"""Test fixtures.

Runs against a real PostgreSQL database: the models use JSONB, INET and
partial unique indexes, so SQLite would test something other than what ships.
Celery dispatch is stubbed — worker behaviour is tested by calling the task
bodies directly, not by standing up a broker.
"""

from __future__ import annotations

import os
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("DOZ_JWT_SECRET", "test-secret-not-for-production")
os.environ.setdefault("DOZ_SECRETS_BACKEND", "env")
os.environ.setdefault("DOZ_CIMC_DEFAULT_USER", "admin")
os.environ.setdefault("DOZ_CIMC_DEFAULT_PASS", "bench-password")
os.environ.setdefault("DOZ_CONTROL_PLANE_URL", "http://10.10.0.5:8000")
os.environ.setdefault("DOZ_BOOT_ASSET_BASE_URL", "http://10.10.0.5:8080")
os.environ.setdefault("DOZ_TRUST_PROXY_HEADERS", "true")

ADMIN_URL = os.environ.get(
    "DOZ_TEST_ADMIN_DATABASE_URL", "postgresql+psycopg://doz:doz@localhost:5432/postgres"
)
TEST_DB_NAME = f"doz_test_{uuid.uuid4().hex[:8]}"
TEST_DB_URL = os.environ.get(
    "DOZ_TEST_DATABASE_URL", f"postgresql+psycopg://doz:doz@localhost:5432/{TEST_DB_NAME}"
)
os.environ["DOZ_DATABASE_URL"] = TEST_DB_URL


@pytest.fixture(scope="session", autouse=True)
def _database() -> None:
    admin_engine = create_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")
    with admin_engine.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{TEST_DB_NAME}"'))
    admin_engine.dispose()

    from app.models import Base

    engine = create_engine(TEST_DB_URL)
    Base.metadata.create_all(engine)
    engine.dispose()

    yield

    admin_engine = create_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")
    with admin_engine.connect() as conn:
        conn.execute(
            text(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                f"WHERE datname = '{TEST_DB_NAME}'"
            )
        )
        conn.execute(text(f'DROP DATABASE IF EXISTS "{TEST_DB_NAME}"'))
    admin_engine.dispose()


@pytest.fixture
def db(_database):
    """A session against the test database, truncated between tests."""
    from app.models import Base

    engine = create_engine(TEST_DB_URL)
    tables = ", ".join(f'"{t.name}"' for t in reversed(Base.metadata.sorted_tables))
    with engine.connect() as conn:
        conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
        conn.commit()

    Session = sessionmaker(bind=engine, expire_on_commit=False)
    session = Session()
    yield session
    session.close()
    engine.dispose()


@pytest.fixture
def dispatched(monkeypatch):
    """Capture Celery dispatches instead of enqueuing them."""
    calls: list[str] = []

    def fake_dispatch(job) -> str:  # noqa: ANN001
        calls.append(str(job.id))
        return f"task-{job.id}"

    import app.workers.tasks as tasks

    monkeypatch.setattr(tasks, "dispatch", fake_dispatch)
    return calls


@pytest.fixture
def client(db, dispatched):
    from app.db import get_db
    from app.main import app

    def override_get_db():
        yield db

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Object factories
# ---------------------------------------------------------------------------


@pytest.fixture
def make_customer(db):
    from app.models import Customer
    from app.security import hash_password

    def _make(email: str = "customer@example.com", *, admin: bool = False) -> Customer:
        customer = Customer(
            email=email,
            password_hash=hash_password("correct-horse-battery-staple"),
            is_admin=admin,
        )
        db.add(customer)
        db.commit()
        db.refresh(customer)
        return customer

    return _make


@pytest.fixture
def make_server(db):
    from app.enums import ServerState
    from app.models import Server

    counter = {"n": 0}

    def _make(**overrides):
        counter["n"] += 1
        n = counter["n"]
        defaults = {
            "serial": f"FCH000000{n:02d}",
            "cimc_ip": f"10.10.99.{n}",
            "cimc_credential_ref": f"cimc/FCH000000{n:02d}",
            "provisioning_mac": f"aa:bb:cc:00:00:{n:02x}",
            "state": ServerState.ACTIVE,
            "rack": "R1",
            "rack_unit": n,
        }
        server = Server(**{**defaults, **overrides})
        db.add(server)
        db.commit()
        db.refresh(server)
        return server

    return _make


@pytest.fixture
def make_subscription(db):
    from app.models import Subscription

    def _make(customer, server, **overrides):  # noqa: ANN001
        subscription = Subscription(
            customer_id=customer.id,
            server_id=server.id,
            plan_name="C220-M4-BASIC",
            **overrides,
        )
        db.add(subscription)
        db.commit()
        db.refresh(subscription)
        return subscription

    return _make


@pytest.fixture
def make_template(db):
    from app.enums import InstallMethod, RaidLevel
    from app.models import OSTemplate

    counter = {"n": 0}

    def _make(**overrides):
        counter["n"] += 1
        defaults = {
            "slug": f"ubuntu-22.04-{counter['n']}",
            "name": "Ubuntu Server",
            "family": "debian",
            "version": "22.04 LTS",
            "install_method": InstallMethod.AUTOINSTALL,
            "config_template": "ubuntu-2204-autoinstall.yaml.j2",
            "kernel_path": "/os/ubuntu-22.04/casper/vmlinuz",
            "initrd_path": "/os/ubuntu-22.04/casper/initrd",
            "default_raid_level": RaidLevel.RAID1,
        }
        template = OSTemplate(**{**defaults, **overrides})
        db.add(template)
        db.commit()
        db.refresh(template)
        return template

    return _make


@pytest.fixture
def make_ssh_key(db):
    from app.api.ssh_keys import fingerprint
    from app.models import SSHKey

    # A real, syntactically valid ed25519 public key.
    PUBLIC_KEY = (
        "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIB2jVQKmXPRzWZQmPTQrSVCPBTAkGDRr3ChJb9lQTKzE "
        "bench@dedicatedoz"
    )

    def _make(customer, name: str = "laptop") -> SSHKey:
        key = SSHKey(
            customer_id=customer.id,
            name=name,
            public_key=PUBLIC_KEY,
            fingerprint=fingerprint(PUBLIC_KEY),
        )
        db.add(key)
        db.commit()
        db.refresh(key)
        return key

    return _make


@pytest.fixture
def auth_header(client):
    def _header(customer) -> dict:  # noqa: ANN001
        response = client.post(
            "/api/v1/auth/login",
            json={"email": customer.email, "password": "correct-horse-battery-staple"},
        )
        assert response.status_code == 200, response.text
        return {"Authorization": f"Bearer {response.json()['access_token']}"}

    return _header
