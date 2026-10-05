from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

DEFAULT_DATA_DIR = Path.home() / ".hermes" / "google_health"

# Timezone used to turn a DAILY record's civil date into an instant. A civil date
# is local by definition, so the machine's local zone is the honest choice -- and
# raw_json keeps the date itself, so nothing is lost if this ever changes.
CIVIL_TZ = "America/Los_Angeles"


def _find_mapping_with(node: Any, key: str, depth: int = 0) -> dict[str, Any]:
    """Return the first mapping in the document that carries <key> as a string.

    WHY THIS SEARCHES INSTEAD OF INDEXING
    -------------------------------------
    The original code guessed the payload key by transforming the data_type:

        payload = data_point.get(data_type.replace("-", "_")) or data_point.get(data_type)

    Google's payload keys are camelCase -- the key for 'active-energy-burned' is
    'activeEnergyBurned', never 'active_energy_burned'. So for every multi-word
    data_type the lookup missed, payload became {}, and start_time, end_time and
    update_time were written as NULL. The row landed; the dates did not; nothing
    raised. 44,316 of 49,504 rows were undated because of it, and a date-filtered
    query returned a tenth of the store while looking perfectly healthy.

    A third naming guess would fail the same way the first two did, so this walks
    the document. It returns the containing MAPPING rather than the bare value on
    purpose: pulling endTime from the whole document would find a sleep STAGE's
    endTime out of `stages` instead of the session's own.
    """
    if depth > 6:
        return {}
    if isinstance(node, dict):
        if isinstance(node.get(key), str):
            return node
        for child_key, child in node.items():
            if child_key == "dataSource":
                continue
            found = _find_mapping_with(child, key, depth + 1)
            if found:
                return found
    elif isinstance(node, list):
        for child in node:
            found = _find_mapping_with(child, key, depth + 1)
            if found:
                return found
    return {}


def _civil_day_start(node: Any, depth: int = 0) -> str | None:
    """The UTC instant of local midnight for a DAILY record's civil date.

    Daily rollups carry `date: {year, month, day}` and no interval at all -- they
    describe a calendar day, not a moment. 18,877 rows in the local store are of
    this kind (mostly Apple Health imports: platform HEALTH_KIT, no Fitbit
    interval), so the interval search cannot reach them no matter how it is
    written. Without this they stay invisible to every date query forever.

    Local midnight is the only honest instant to give such a row. It makes day
    filters work uniformly, and raw_json still holds the exact civil date -- so
    nothing is lost, and a consumer that needs day-granularity can read it there.
    """
    if depth > 6:
        return None
    if isinstance(node, dict):
        d = node.get("date")
        if isinstance(d, dict) and {"year", "month", "day"} <= set(d):
            try:
                local = datetime(int(d["year"]), int(d["month"]), int(d["day"]),
                                 tzinfo=ZoneInfo(CIVIL_TZ))
            except (ValueError, TypeError):
                return None
            return local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        for child_key, child in node.items():
            if child_key == "dataSource":
                continue
            got = _civil_day_start(child, depth + 1)
            if got:
                return got
    elif isinstance(node, list):
        for child in node:
            got = _civil_day_start(child, depth + 1)
            if got:
                return got
    return None


class GoogleHealthStore:
    def __init__(self, data_dir: str | Path = DEFAULT_DATA_DIR):
        self.data_dir = Path(data_dir).expanduser()
        self.token_path = self.data_dir / "token.json"
        self.db_path = self.data_dir / "google_health.sqlite"

    def save_token(self, token: dict[str, Any]) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.token_path.write_text(json.dumps(token, indent=2, sort_keys=True))
        try:
            self.token_path.chmod(0o600)
        except OSError:
            pass

    def load_token(self) -> dict[str, Any]:
        return json.loads(self.token_path.read_text())

    def init_db(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as con:
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS data_points (
                    data_type TEXT NOT NULL,
                    record_id TEXT NOT NULL,
                    start_time TEXT,
                    end_time TEXT,
                    update_time TEXT,
                    platform TEXT,
                    recording_method TEXT,
                    raw_json TEXT NOT NULL,
                    PRIMARY KEY (data_type, record_id)
                )
                """
            )
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS sync_state (
                    key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

    def upsert_datapoint(self, *, data_type: str, data_point: dict[str, Any]) -> bool:
        self.init_db()
        record_id = data_point.get("name") or json.dumps(data_point, sort_keys=True)
        data_source = data_point.get("dataSource") or {}
        # Was a guessed payload key -- see _find_mapping_with. It missed for every
        # multi-word data_type, so start/end/update were written as NULL for 90% of
        # the store without raising anything. The walk () gives the mapping
        # that actually holds the times, and takes endTime from THAT mapping so a
        # sleep stage's endTime cannot be mistaken for the session's.
        interval = _find_mapping_with(data_point, "startTime")
        if not interval:
            # A POINT sample has no interval -- it has an instant, and Google calls
            # it sampleTime.physicalTime. Six data types store it that way
            # (oxygen-saturation, heart-rate-variability, weight, height, vo2-max,
            # respiratory-rate-sleep-summary), and searching only for startTime
            # missed all of them, fell through to the civil-day fallback below, and
            # stamped local midnight on 3,046 rows whose real times -- down to the
            # second -- were in the payload the whole time. See _civil_day_start.
            interval = _find_mapping_with(data_point, "physicalTime")
        start = interval.get("startTime") or interval.get("physicalTime")
        end = interval.get("endTime") or interval.get("sessionEndTime")
        update_time = interval.get("updateTime")
        if not update_time:
            update_time = _find_mapping_with(data_point, "updateTime").get("updateTime")
        if not start:
            # No interval anywhere means a DAILY record, which is dated by its
            # civil date rather than by a moment.
            start = _civil_day_start(data_point)
        raw_json = json.dumps(data_point, ensure_ascii=False, sort_keys=True)
        with sqlite3.connect(self.db_path) as con:
            cur = con.execute(
                """
                INSERT OR IGNORE INTO data_points
                  (data_type, record_id, start_time, end_time, update_time, platform, recording_method, raw_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (data_type, record_id, start, end, update_time, data_source.get("platform"), data_source.get("recordingMethod"), raw_json),
            )
            if cur.rowcount == 0:
                con.execute(
                    """
                    UPDATE data_points
                    SET start_time=?, end_time=?, update_time=?, platform=?, recording_method=?, raw_json=?
                    WHERE data_type=? AND record_id=?
                    """,
                    (start, end, update_time, data_source.get("platform"), data_source.get("recordingMethod"), raw_json, data_type, record_id),
                )
                return False
            return True

    def list_datapoints(self, data_type: str, limit: int = 100) -> list[dict[str, Any]]:
        self.init_db()
        with sqlite3.connect(self.db_path) as con:
            con.row_factory = sqlite3.Row
            rows = con.execute(
                "SELECT * FROM data_points WHERE data_type=? ORDER BY COALESCE(end_time, update_time, record_id) DESC LIMIT ?",
                (data_type, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def counts_by_data_type(self) -> dict[str, int]:
        self.init_db()
        with sqlite3.connect(self.db_path) as con:
            rows = con.execute("SELECT data_type, COUNT(*) FROM data_points GROUP BY data_type ORDER BY data_type").fetchall()
        return {str(k): int(v) for k, v in rows}

    def get_sync_state(self, key: str) -> dict[str, Any] | None:
        self.init_db()
        with sqlite3.connect(self.db_path) as con:
            row = con.execute("SELECT value_json FROM sync_state WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def set_sync_state(self, key: str, value: dict[str, Any]) -> None:
        self.init_db()
        with sqlite3.connect(self.db_path) as con:
            con.execute(
                """
                INSERT INTO sync_state(key, value_json, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, updated_at=CURRENT_TIMESTAMP
                """,
                (key, json.dumps(value, sort_keys=True)),
            )

    def clear_sync_state(self, key: str) -> None:
        self.init_db()
        with sqlite3.connect(self.db_path) as con:
            con.execute("DELETE FROM sync_state WHERE key=?", (key,))

    @staticmethod
    def _civil_date(civil_dt: dict[str, Any] | None) -> str | None:
        if not civil_dt:
            return None
        d = civil_dt.get("date") or {}
        if not all(k in d for k in ("year", "month", "day")):
            return None
        return f"{int(d['year']):04d}-{int(d['month']):02d}-{int(d['day']):02d}"

    def upsert_rollup(self, *, data_type: str, rollup_point: dict[str, Any]) -> bool:
        start = self._civil_date(rollup_point.get("civilStartTime"))
        end = self._civil_date(rollup_point.get("civilEndTime"))
        rollup_type = f"{data_type}:daily-rollup"
        record_id = f"users/me/dataTypes/{data_type}/dataPoints:dailyRollUp/{start or 'unknown'}_{end or 'unknown'}"
        wrapper = {"name": record_id, rollup_type: rollup_point}
        inserted = self.upsert_datapoint(data_type=rollup_type, data_point=wrapper)
        if start or end:
            with sqlite3.connect(self.db_path) as con:
                con.execute(
                    "UPDATE data_points SET start_time=?, end_time=? WHERE data_type=? AND record_id=?",
                    (start, end, rollup_type, record_id),
                )
        return inserted
