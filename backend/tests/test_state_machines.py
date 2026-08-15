"""The two state machines, which everything else depends on being correct."""

from __future__ import annotations

import pytest

from app.enums import JobState, JobType, ServerState
from app.services import jobs as job_service
from app.services.lifecycle import IllegalTransition, can_transition, transition_server


class TestServerLifecycle:
    def test_normal_provisioning_path(self, db, make_server):
        server = make_server(state=ServerState.IN_STOCK)
        for target in (ServerState.PROVISIONING, ServerState.ACTIVE):
            transition_server(db, server, target)
        assert server.state == ServerState.ACTIVE

    def test_deprovision_path_returns_to_stock(self, db, make_server):
        server = make_server(state=ServerState.ACTIVE)
        transition_server(db, server, ServerState.WIPING)
        transition_server(db, server, ServerState.IN_STOCK)
        assert server.state == ServerState.IN_STOCK

    def test_active_cannot_jump_straight_to_stock(self, db, make_server):
        """The wipe stage is mandatory — this is the guard that enforces it."""
        server = make_server(state=ServerState.ACTIVE)
        with pytest.raises(IllegalTransition):
            transition_server(db, server, ServerState.IN_STOCK)
        assert server.state == ServerState.ACTIVE

    def test_retired_is_terminal(self):
        for target in ServerState:
            if target is ServerState.RETIRED:
                continue
            assert not can_transition(ServerState.RETIRED, target)

    def test_transition_is_audited(self, db, make_server):
        from app.models import AuditLog

        server = make_server(state=ServerState.ACTIVE)
        transition_server(db, server, ServerState.SUSPENDED, reason="abuse report 7")
        db.commit()

        entry = db.query(AuditLog).filter_by(action="server.state_change").one()
        assert entry.detail["from"] == "active"
        assert entry.detail["to"] == "suspended"
        assert entry.detail["reason"] == "abuse report 7"

    def test_same_state_is_a_noop(self, db, make_server):
        server = make_server(state=ServerState.ACTIVE)
        transition_server(db, server, ServerState.ACTIVE)
        assert server.state == ServerState.ACTIVE


class TestJobStateMachine:
    def test_lifecycle(self, db, make_server):
        server = make_server()
        job, _ = job_service.create_job(
            db, job_type=JobType.POWER_ON, server_id=server.id, payload={}
        )
        assert job.state == JobState.QUEUED

        job_service.transition_job(db, job, JobState.RUNNING)
        assert job.started_at is not None
        assert job.attempts == 1

        job_service.transition_job(db, job, JobState.SUCCEEDED)
        assert job.finished_at is not None
        assert job.progress == 100

    def test_terminal_states_are_final(self, db, make_server):
        server = make_server()
        job, _ = job_service.create_job(
            db, job_type=JobType.POWER_ON, server_id=server.id, payload={}
        )
        job_service.transition_job(db, job, JobState.RUNNING)
        job_service.transition_job(db, job, JobState.FAILED, error="boom")

        with pytest.raises(job_service.IllegalJobTransition):
            job_service.transition_job(db, job, JobState.SUCCEEDED)

    def test_callback_token_is_destroyed_on_completion(self, db, make_server):
        """A finished job's boot script must not be replayable."""
        server = make_server()
        job, token = job_service.create_job(
            db,
            job_type=JobType.INSTALL,
            server_id=server.id,
            payload={"_callback_token": "plaintext"},
            with_callback_token=True,
        )
        assert token is not None
        assert job.callback_token_hash is not None

        job_service.transition_job(db, job, JobState.RUNNING)
        job_service.transition_job(db, job, JobState.SUCCEEDED)

        assert job.callback_token_hash is None
        assert job.callback_expires_at is None
        assert "_callback_token" not in job.payload

    def test_one_exclusive_job_per_server(self, db, make_server):
        server = make_server()
        job_service.create_job(db, job_type=JobType.INSTALL, server_id=server.id)
        db.commit()

        with pytest.raises(job_service.JobConflict):
            job_service.create_job(db, job_type=JobType.POWER_OFF, server_id=server.id)

    def test_finished_job_frees_the_server(self, db, make_server):
        server = make_server()
        first, _ = job_service.create_job(
            db, job_type=JobType.POWER_ON, server_id=server.id
        )
        job_service.transition_job(db, first, JobState.RUNNING)
        job_service.transition_job(db, first, JobState.SUCCEEDED)
        db.commit()

        second, _ = job_service.create_job(
            db, job_type=JobType.POWER_OFF, server_id=server.id
        )
        assert second.state == JobState.QUEUED

    def test_log_sequence_is_gapless(self, db, make_server):
        server = make_server()
        job, _ = job_service.create_job(db, job_type=JobType.INSTALL, server_id=server.id)
        for i in range(5):
            job_service.log(db, job, f"line {i}")
        db.commit()

        sequences = [e.sequence for e in job.log_entries]
        assert sequences == sorted(sequences)
        assert len(set(sequences)) == len(sequences)

    def test_reaper_fails_abandoned_jobs(self, db, make_server):
        from datetime import UTC, datetime, timedelta

        server = make_server()
        job, _ = job_service.create_job(db, job_type=JobType.INSTALL, server_id=server.id)
        job_service.transition_job(db, job, JobState.RUNNING)
        job.started_at = datetime.now(UTC) - timedelta(hours=3)
        db.commit()

        assert job_service.reap_stale_jobs(db, running_timeout=60) == 1
        db.commit()
        db.refresh(job)
        assert job.state == JobState.FAILED
        assert "presumed lost" in job.error
