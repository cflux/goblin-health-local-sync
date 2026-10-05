from google_health_local_sync.storage import GoogleHealthStore

import copy
import json


def test_store_roundtrips_token_and_raw_datapoint(tmp_path):
    store = GoogleHealthStore(tmp_path)
    store.save_token({"access_token": "a", "refresh_token": "r"})
    assert store.load_token()["refresh_token"] == "r"

    store.init_db()
    inserted = store.upsert_datapoint(
        data_type="sleep",
        data_point={"name": "users/me/dataTypes/sleep/dataPoints/1", "sleep": {"type": "CLASSIC"}},
    )
    inserted_again = store.upsert_datapoint(
        data_type="sleep",
        data_point={"name": "users/me/dataTypes/sleep/dataPoints/1", "sleep": {"type": "CLASSIC"}},
    )

    assert inserted is True
    assert inserted_again is False
    rows = store.list_datapoints("sleep")
    assert len(rows) == 1
    assert rows[0]["record_id"] == "users/me/dataTypes/sleep/dataPoints/1"


def test_recomputed_daily_rollup_supersedes_instead_of_accumulating(tmp_path):
    """A daily rollup carries no `name`, so its identity is a hash of its payload --
    and Fitbit finalises the value AFTER the first fetch. When the number moves the
    hash moves, so INSERT OR IGNORE wrote a second row and the first was never
    touched again: the store held two versions of one day with nothing marking
    which was current. Two rows covering the SAME span are two versions of one
    fact, and the later fetch is the correction.
    """
    store = GoogleHealthStore(tmp_path)
    store.init_db()

    first = {"dailyHeartRateVariability": {
        "averageHeartRateVariabilityMilliseconds": 84.35,
        "interval": {"startTime": "2026-10-04T07:00:00Z",
                     "endTime": "2026-10-05T07:00:00Z"}}}
    second = copy.deepcopy(first)
    second["dailyHeartRateVariability"]["averageHeartRateVariabilityMilliseconds"] = 84.5

    store.upsert_datapoint(data_type="daily-heart-rate-variability", data_point=first)
    store.upsert_datapoint(data_type="daily-heart-rate-variability", data_point=second)

    rows = store.list_datapoints("daily-heart-rate-variability")
    assert len(rows) == 1, "a recomputed value must supersede, not accumulate"
    kept = json.loads(rows[0]["raw_json"])["dailyHeartRateVariability"]
    assert kept["averageHeartRateVariabilityMilliseconds"] == 84.5


def test_distinct_intervals_are_never_collapsed(tmp_path):
    """The inverse, and the more dangerous direction. Minute-level series share a
    data_type and a day but never an interval: time-in-heart-rate-zone holds over
    1400 rows a day, and collapsing by day instead of by span would delete real
    observations. This asserts the supersede rule cannot do that.
    """
    store = GoogleHealthStore(tmp_path)
    store.init_db()

    for minute in range(3):
        store.upsert_datapoint(
            data_type="time-in-heart-rate-zone",
            data_point={"timeInHeartRateZone": {
                "heartRateZoneType": "LIGHT",
                "interval": {"startTime": f"2026-10-04T18:0{minute}:00Z",
                             "endTime": f"2026-10-04T18:0{minute + 1}:00Z"}}},
        )

    assert len(store.list_datapoints("time-in-heart-rate-zone")) == 3


def test_daily_rollups_for_different_days_both_survive(tmp_path):
    """The supersede is keyed on the civil day, not on the type. Two days of the
    same one-per-day rollup are two facts, and the gate must not eat one. Payload
    shape copied from the live store."""
    store = GoogleHealthStore(tmp_path)
    store.init_db()

    for day, bpm in ((3, "57"), (4, "56")):
        store.upsert_datapoint(
            data_type="daily-resting-heart-rate",
            data_point={"dailyRestingHeartRate": {
                "beatsPerMinute": bpm,
                "dailyRestingHeartRateMetadata": {"calculationMethod": "WITH_SLEEP"},
                "date": {"day": day, "month": 10, "year": 2026}}},
        )

    assert len(store.list_datapoints("daily-resting-heart-rate")) == 2


def test_civil_day_prefers_the_start_over_the_end(tmp_path):
    """A rollup carries civilStartTime AND civilEndTime. Payloads are stored with
    sort_keys=True, so 'civilEndTime' sorts first and a naive first-match walk takes
    the END date, putting the record a day late. Shape copied from the live store.
    """
    store = GoogleHealthStore(tmp_path)
    store.init_db()
    store.upsert_datapoint(
        data_type="calories-in-heart-rate-zone:daily-rollup",
        data_point={"calories-in-heart-rate-zone:daily-rollup": {
            "caloriesInHeartRateZone": {"caloriesInHeartRateZones": []},
            "civilEndTime": {"date": {"day": 26, "month": 9, "year": 2026}, "time": {}},
            "civilStartTime": {"date": {"day": 25, "month": 9, "year": 2026}, "time": {}},
        }},
    )
    row = store.list_datapoints("calories-in-heart-rate-zone:daily-rollup")[0]
    assert row["start_time"].startswith("2026-09-25"), (
        f"expected the civil START day, got {row['start_time']}")


def test_nameless_rows_without_an_interval_are_not_collapsed(tmp_path):
    """No name and no interval means no stable identity AND no observed span, so
    there is nothing to supersede on. These must keep accumulating rather than
    silently overwrite one another.
    """
    store = GoogleHealthStore(tmp_path)
    store.init_db()

    for value in (1, 2):
        store.upsert_datapoint(
            data_type="mystery-metric",
            data_point={"mysteryMetric": {"value": value}},
        )

    assert len(store.list_datapoints("mystery-metric")) == 2


def test_point_sample_uses_its_own_instant(tmp_path):
    """A point sample stores its instant as sampleTime.physicalTime, NOT startTime.

    This payload is copied VERBATIM from the live store. An earlier draft of this
    test invented a payload carrying an explicit endTime -- a shape that does not
    occur for these types -- and so it passed green while 3,046 real rows were
    written with local midnight instead of their own timestamps.
    """
    store = GoogleHealthStore(tmp_path)
    store.init_db()
    store.upsert_datapoint(
        data_type="oxygen-saturation",
        data_point={
            "dataSource": {"device": {"displayName": "Google Fitbit Air"},
                           "platform": "FITBIT",
                           "recordingMethod": "PASSIVELY_MEASURED"},
            "oxygenSaturation": {
                "percentage": 90.4,
                "sampleTime": {
                    "civilTime": {"date": {"day": 26, "month": 9, "year": 2026},
                                  "time": {"hours": 1, "minutes": 51, "seconds": 4}},
                    "physicalTime": "2026-09-26T08:51:04Z",
                    "utcOffset": "-25200s",
                },
            },
        },
    )
    row = store.list_datapoints("oxygen-saturation")[0]
    assert row["start_time"] == "2026-09-26T08:51:04Z", (
        "the payload's own instant must beat the civil-day fallback")
