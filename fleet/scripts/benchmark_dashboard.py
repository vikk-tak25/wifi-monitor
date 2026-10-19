"""Benchmark the dashboard queries (#5): 50 units x 7 days of synthetic samples.

Usage (needs the development database from docker-compose.yml):
    .venv/bin/python fleet/scripts/benchmark_dashboard.py [--units 50] [--days 7]

Creates and drops a separate database `fleet_bench`; development data is not touched.
"""

import argparse
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from fleet import analytics
from fleet import units as unit_repo
from fleet.cli import migrate
from fleet.config import Settings
from fleet.db import make_engine
from fleet.demo import seed_demo

BASE = "postgresql+psycopg://fleet:fleet-dev@127.0.0.1:5433/fleet"


def timed(label, fn, budget_s=2.0):
    start = time.perf_counter()
    result = fn()
    elapsed = time.perf_counter() - start
    print(f"  {label:<44} {elapsed * 1000:8.0f} ms  {'OK' if elapsed < budget_s else 'SLOW'}")
    return result, elapsed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--units", type=int, default=50)
    parser.add_argument("--days", type=float, default=7)
    args = parser.parse_args()

    admin = create_engine(BASE, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text("DROP DATABASE IF EXISTS fleet_bench WITH (FORCE)"))
        conn.execute(text("CREATE DATABASE fleet_bench"))
    url = make_url(BASE).set(database="fleet_bench").render_as_string(hide_password=False)
    migrate(url)
    engine = make_engine(url)
    settings = Settings(database_url=url)
    now = datetime.now(timezone.utc)
    try:
        start = time.perf_counter()
        with engine.begin() as conn:
            ids = seed_demo(conn, settings, count=args.units, days=args.days, now=now)
        with engine.begin() as conn:
            conn.execute(text("ANALYZE"))
            rows = conn.execute(text("SELECT count(*) FROM link_samples")).scalar_one()
        print(f"seeded {args.units} units x {args.days:g} days = {rows:,} samples in {time.perf_counter() - start:.0f} s")

        worst = 0.0
        with engine.connect() as conn:
            units = [unit_repo.public_unit(u, settings) for u in unit_repo.list_units(conn)]
            for window in ("1h", "24h"):
                _, t = timed(f"room comparison, {window} (uncached)", lambda: analytics.rooms(conn, units, window, now))
                worst = max(worst, t)
            # The 7-day statistics are recomputed in the background every 4 minutes; requests reuse them.
            computed, _ = timed("7-day statistics (background refresh)", lambda: analytics.room_stats(conn, "7d", now),
                                budget_s=10)
            _, t = timed("room comparison, 7d (what a request waits)",
                         lambda: analytics.rooms(conn, units, "7d", now, computed))
            worst = max(worst, t)
            for bucket, span in (("5m", timedelta(days=1)), ("1h", timedelta(days=7)), ("1d", timedelta(days=7))):
                for metric in ("rssi", "gw_ms", "srv_ms"):
                    _, t = timed(f"candles {metric} {bucket} over {span.days or 1} d",
                                 lambda: analytics.candles(conn, ids[-1], metric, bucket, now - span, now, "Europe/Tallinn"))
                    worst = max(worst, t)
            _, t = timed("room summary, 7d", lambda: analytics.unit_summary(conn, ids[-1], "7d", now))
            worst = max(worst, t)
        print(f"slowest query: {worst * 1000:.0f} ms ({'within' if worst < 2 else 'OVER'} the 2 s budget)")
        return 0 if worst < 2 else 1
    finally:
        engine.dispose()
        with admin.connect() as conn:
            conn.execute(text("DROP DATABASE IF EXISTS fleet_bench WITH (FORCE)"))
        admin.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
