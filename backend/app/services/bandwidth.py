"""Bandwidth series from raw interface counters.

Samples are cumulative switch counters. Turning them into a rate means
differencing consecutive samples, which has two traps: a switch reboot resets
the counter to zero (producing a large negative delta), and a missed poll
leaves a gap that would otherwise show as a spike. Both are handled here so
callers only ever see rates.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import BandwidthSample
from app.schemas import BandwidthPoint, BandwidthSeries

PERIODS: dict[str, timedelta] = {
    "1h": timedelta(hours=1),
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
}

#: A gap longer than this is treated as missing data, not as traffic.
MAX_GAP = timedelta(minutes=30)


def series_for_server(db: Session, server_id: uuid.UUID, period: str) -> BandwidthSeries:
    window = PERIODS.get(period, PERIODS["24h"])
    since = datetime.now(UTC) - window

    samples = (
        db.execute(
            select(BandwidthSample)
            .where(BandwidthSample.server_id == server_id, BandwidthSample.sampled_at >= since)
            .order_by(BandwidthSample.sampled_at)
        )
        .scalars()
        .all()
    )

    points: list[BandwidthPoint] = []
    total_rx = 0
    total_tx = 0

    for previous, current in zip(samples, samples[1:], strict=False):
        elapsed = (current.sampled_at - previous.sampled_at).total_seconds()
        if elapsed <= 0 or timedelta(seconds=elapsed) > MAX_GAP:
            continue

        rx_delta = current.rx_bytes - previous.rx_bytes
        tx_delta = current.tx_bytes - previous.tx_bytes
        if rx_delta < 0 or tx_delta < 0:
            # Counter wrapped or the switch rebooted. There is no way to know
            # how much traffic was lost, so the interval is dropped rather than
            # guessed at.
            continue

        total_rx += rx_delta
        total_tx += tx_delta
        points.append(
            BandwidthPoint(
                timestamp=current.sampled_at,
                rx_bps=rx_delta * 8 / elapsed,
                tx_bps=tx_delta * 8 / elapsed,
            )
        )

    return BandwidthSeries(
        server_id=server_id,
        period=period,
        points=points,
        total_rx_bytes=total_rx,
        total_tx_bytes=total_tx,
    )


def record_sample(
    db: Session,
    server_id: uuid.UUID,
    *,
    rx_bytes: int,
    tx_bytes: int,
    rx_errors: int = 0,
    tx_errors: int = 0,
) -> BandwidthSample:
    sample = BandwidthSample(
        server_id=server_id,
        rx_bytes=rx_bytes,
        tx_bytes=tx_bytes,
        rx_errors=rx_errors,
        tx_errors=tx_errors,
    )
    db.add(sample)
    return sample
