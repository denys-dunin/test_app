"""
Unit tests for the pure logic in weather_alerts.py. No network required.

Run with:
    python -m unittest test_weather_alerts.py
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

import weather_alerts as wa


def _hourly_times(n: int) -> list[str]:
    base = datetime.now().replace(minute=0, second=0, microsecond=0)
    return [(base + timedelta(hours=i)).isoformat(timespec="minutes") for i in range(n)]


class EvaluateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = {
            "thresholds": {
                "temp_low_c": 0.0,
                "temp_high_c": 28.0,
                "kp_index": 5.0,
                "precip_prob_pct": 50,
                "precip_mm": 0.2,
                "wind_ms": 10.0,
            },
            "precip_lookahead_h": 6,
            "wind_lookahead_h": 6,
            "kp_lookahead_h": 24,
        }
        self.kp_quiet = wa.KpForecast(rows=[
            (datetime.now(timezone.utc) + timedelta(hours=3), 2.0),
            (datetime.now(timezone.utc) + timedelta(hours=12), 3.0),
        ])

    def _snap(self, **overrides):
        times = _hourly_times(8)
        base = dict(
            timezone="UTC",
            current_temp_c=15.0,
            current_wind_ms=2.0,
            current_precip_mm=0.0,
            weather_code=2,
            hourly_time=times,
            hourly_temp_c=[15.0] * 8,
            hourly_precip_mm=[0.0] * 8,
            hourly_precip_prob=[10.0] * 8,
            hourly_wind_ms=[2.0] * 8,
        )
        base.update(overrides)
        return wa.WeatherSnapshot(**base)

    def test_no_alerts_on_calm_day(self) -> None:
        alerts = wa.evaluate(self._snap(), self.kp_quiet, self.cfg)
        self.assertEqual(alerts, [])

    def test_hot_day_fires_temperature_alert(self) -> None:
        alerts = wa.evaluate(self._snap(current_temp_c=30.0), self.kp_quiet, self.cfg)
        self.assertEqual([a.category for a in alerts], ["temperature"])
        self.assertIn("Hot", alerts[0].message)

    def test_cold_day_fires_temperature_alert(self) -> None:
        alerts = wa.evaluate(self._snap(current_temp_c=-5.0), self.kp_quiet, self.cfg)
        self.assertEqual([a.category for a in alerts], ["temperature"])
        self.assertIn("Cold", alerts[0].message)

    def test_kp_storm_fires_geomagnetic_alert(self) -> None:
        kp = wa.KpForecast(rows=[
            (datetime.now(timezone.utc) + timedelta(hours=6), 6.0),
        ])
        alerts = wa.evaluate(self._snap(), kp, self.cfg)
        self.assertIn("geomagnetic", [a.category for a in alerts])

    def test_kp_storm_outside_window_ignored(self) -> None:
        kp = wa.KpForecast(rows=[
            (datetime.now(timezone.utc) + timedelta(hours=48), 7.0),  # past 24h window
        ])
        alerts = wa.evaluate(self._snap(), kp, self.cfg)
        self.assertNotIn("geomagnetic", [a.category for a in alerts])

    def test_precip_by_probability(self) -> None:
        snap = self._snap(hourly_precip_prob=[80, 20, 10, 5, 0, 0, 0, 0], weather_code=61)
        alerts = wa.evaluate(snap, self.kp_quiet, self.cfg)
        cats = [a.category for a in alerts]
        self.assertIn("precipitation", cats)
        msg = next(a.message for a in alerts if a.category == "precipitation")
        self.assertIn("Rain", msg)

    def test_precip_snow_label(self) -> None:
        snap = self._snap(hourly_precip_mm=[1.0, 0, 0, 0, 0, 0, 0, 0], weather_code=73)
        alerts = wa.evaluate(snap, self.kp_quiet, self.cfg)
        msg = next(a.message for a in alerts if a.category == "precipitation")
        self.assertIn("Snow", msg)

    def test_wind_alert(self) -> None:
        snap = self._snap(hourly_wind_ms=[3, 5, 7, 11, 12, 9, 4, 3])
        alerts = wa.evaluate(snap, self.kp_quiet, self.cfg)
        self.assertIn("wind", [a.category for a in alerts])


class SliceWindowTests(unittest.TestCase):
    def test_keeps_only_next_n_hours(self) -> None:
        times = _hourly_times(10)
        values = list(range(10))
        out = wa._slice_window(times, values, 4)
        # Should include indices 0..3 (next 4 hours from now)
        self.assertEqual(out, [0.0, 1.0, 2.0, 3.0])


class KpPayloadParseTests(unittest.TestCase):
    """Regression tests for the Kp parser — the bug that produced 'failed to fetch data: 0'."""

    def test_parses_list_of_lists(self) -> None:
        payload = [
            ["time_tag", "Kp", "a_running", "station_count"],
            ["2026-05-17 00:00:00", "3.33", "12", "8"],
            ["2026-05-17 03:00:00", "5.0", "15", "8"],
        ]
        rows = wa._parse_kp_payload(payload)
        self.assertEqual(len(rows), 2)
        self.assertAlmostEqual(rows[1][1], 5.0)

    def test_parses_list_of_dicts_object_shape(self) -> None:
        # This is the shape that triggered KeyError(0) in the original parser.
        payload = [
            {"time_tag": "2026-05-17T00:00:00", "kp": "3.33"},
            {"time_tag": "2026-05-17T03:00:00", "kp": "6.0"},
        ]
        rows = wa._parse_kp_payload(payload)
        self.assertEqual(len(rows), 2)
        self.assertAlmostEqual(rows[1][1], 6.0)

    def test_parses_list_of_dicts_with_predicted_kp(self) -> None:
        payload = [
            {"model_prediction_time": "2026-05-17 12:00:00", "predicted_kp": 4.7},
            {"model_prediction_time": "2026-05-17 15:00:00", "predicted_kp": 7.0},
        ]
        rows = wa._parse_kp_payload(payload)
        self.assertEqual(len(rows), 2)
        self.assertAlmostEqual(rows[1][1], 7.0)

    def test_empty_or_garbage_payload_returns_empty(self) -> None:
        self.assertEqual(wa._parse_kp_payload([]), [])
        self.assertEqual(wa._parse_kp_payload(None), [])  # type: ignore[arg-type]
        self.assertEqual(wa._parse_kp_payload({"oops": True}), [])  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
