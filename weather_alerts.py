"""
weather_alerts.py — produce concise alerts about temperature, geomagnetic activity,
precipitation, and wind for your current (or a configured) location.

Designed to be run on a schedule (e.g. cron) three times per day. Prints a human-
readable summary to stdout and, if `plyer` is installed, also sends a desktop
notification. Exits 0 if it ran successfully even when no alerts fired.

Data sources (all free, no API key):
  * IP geolocation : https://ipapi.co/json/
  * Weather        : https://api.open-meteo.com/v1/forecast   (Open-Meteo)
  * Geocoding      : https://geocoding-api.open-meteo.com/v1/search  (Open-Meteo)
  * Geomagnetic Kp : https://services.swpc.noaa.gov/products/noaa-planetary-k-index.json
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import platform
import ssl
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------- #
# Config                                                                      #
# --------------------------------------------------------------------------- #

DEFAULT_CONFIG: dict[str, Any] = {
    # "auto" = look up location by IP. Otherwise put a city name string here
    # (e.g. "Kyiv", "San Francisco, US") or an explicit {"lat": .., "lon": ..}.
    "location": "auto",

    # Thresholds. Anything that meets or exceeds these fires an alert.
    "thresholds": {
        "temp_low_c": 0.0,        # current temp at or below this -> alert
        "temp_high_c": 28.0,      # current temp at or above this -> alert
        "kp_index": 5.0,          # forecast Kp >= this in next 24h -> alert
        "precip_prob_pct": 50,    # max precip probability % in next 6h
        "precip_mm": 0.2,         # OR total precip mm in next 6h
        "wind_ms": 10.0,          # max wind speed m/s in next 6h
    },

    # Lookahead windows (in hours)
    "precip_lookahead_h": 6,
    "wind_lookahead_h": 6,
    "kp_lookahead_h": 24,

    # Set a string here to override the timezone returned by Open-Meteo.
    # Leave null to use the location's local timezone (recommended).
    "timezone": None,

    # If true, also send a desktop notification via `plyer` when available.
    "desktop_notification": True,
}

CONFIG_PATH_DEFAULT = Path(__file__).with_name("config.json")
USER_AGENT = "weather-alerts/1.0 (+https://example.invalid)"
HTTP_TIMEOUT_S = 12

log = logging.getLogger("weather_alerts")


# --------------------------------------------------------------------------- #
# HTTP                                                                        #
# --------------------------------------------------------------------------- #

# Common CA bundle locations across Linux distros + macOS.
_SYSTEM_CA_CANDIDATES: tuple[str, ...] = (
    "/etc/ssl/cert.pem",                                  # macOS, FreeBSD, Alpine
    "/etc/ssl/certs/ca-certificates.crt",                 # Debian, Ubuntu
    "/etc/pki/tls/certs/ca-bundle.crt",                   # RHEL, CentOS, Fedora
    "/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem",  # RHEL 8+
    "/etc/ssl/ca-bundle.pem",                             # openSUSE
)


def _macos_keychain_bundle() -> str | None:
    """
    Last-resort CA discovery on macOS: ask the system keychain for every
    trusted root cert and write it to a temp PEM. This is what
    `Install Certificates.command` does under the hood for python.org Python.
    """
    if platform.system() != "Darwin":
        return None
    keychains = (
        "/System/Library/Keychains/SystemRootCertificates.keychain",
        "/Library/Keychains/System.keychain",
    )
    parts: list[str] = []
    for kc in keychains:
        if not os.path.exists(kc):
            continue
        try:
            out = subprocess.run(
                ["security", "find-certificate", "-a", "-p", kc],
                capture_output=True, text=True, timeout=10, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if out.returncode == 0 and out.stdout:
            parts.append(out.stdout)
    if not parts:
        return None
    fd, path = tempfile.mkstemp(prefix="weather-alerts-ca-", suffix=".pem")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("\n".join(parts))
    except OSError:
        return None
    return path


def _build_ssl_context() -> ssl.SSLContext:
    """
    Build an SSL context that actually verifies certs across environments.

    On macOS, Python installed via the python.org installer ships without
    OS-level trust roots, so the stdlib's default context can't verify any
    HTTPS server out of the box — that produces:

        SSL: CERTIFICATE_VERIFY_FAILED: unable to get local issuer certificate

    Resolution order:
      1. SSL_CERT_FILE env var (if set and exists)
      2. The `certifi` package's bundle (optional dep)
      3. A well-known system CA bundle path
      4. (macOS only) certs extracted from the system keychain
      5. Stdlib default — usually works on properly configured Linux
    """
    cafile = os.environ.get("SSL_CERT_FILE")
    if cafile and os.path.isfile(cafile):
        log.debug("ssl: using SSL_CERT_FILE=%s", cafile)
        return ssl.create_default_context(cafile=cafile)

    try:
        import certifi  # type: ignore
        path = certifi.where()
        log.debug("ssl: using certifi bundle at %s", path)
        return ssl.create_default_context(cafile=path)
    except Exception:
        pass

    for path in _SYSTEM_CA_CANDIDATES:
        if os.path.isfile(path):
            log.debug("ssl: using system CA bundle at %s", path)
            return ssl.create_default_context(cafile=path)

    keychain_bundle = _macos_keychain_bundle()
    if keychain_bundle:
        log.debug("ssl: using macOS keychain export at %s", keychain_bundle)
        return ssl.create_default_context(cafile=keychain_bundle)

    log.debug("ssl: using stdlib default context (no explicit CA bundle found)")
    return ssl.create_default_context()


_SSL_CONTEXT: ssl.SSLContext | None = None


def http_get_json(url: str) -> Any:
    """GET a URL and return parsed JSON. Raises on non-2xx."""
    global _SSL_CONTEXT
    if _SSL_CONTEXT is None:
        _SSL_CONTEXT = _build_ssl_context()
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S, context=_SSL_CONTEXT) as resp:
        data = resp.read()
    return json.loads(data.decode("utf-8"))


# --------------------------------------------------------------------------- #
# Location                                                                    #
# --------------------------------------------------------------------------- #

@dataclass
class Location:
    lat: float
    lon: float
    label: str  # e.g. "Kyiv, Ukraine"


def resolve_location(spec: Any) -> Location:
    """Turn a config 'location' value into a concrete Location."""
    # Already explicit lat/lon
    if isinstance(spec, dict) and "lat" in spec and "lon" in spec:
        return Location(
            lat=float(spec["lat"]),
            lon=float(spec["lon"]),
            label=str(spec.get("label", f"{spec['lat']:.3f},{spec['lon']:.3f}")),
        )

    if isinstance(spec, str) and spec.strip().lower() == "auto":
        data = http_get_json("https://ipapi.co/json/")
        if not isinstance(data, dict) or data.get("error"):
            reason = data.get("reason") if isinstance(data, dict) else "no response"
            raise RuntimeError(f"IP geolocation failed: {reason}")
        lat = data.get("latitude")
        lon = data.get("longitude")
        if lat is None or lon is None:
            raise RuntimeError("IP geolocation response missing latitude/longitude")
        city = data.get("city") or ""
        country = data.get("country_name") or data.get("country") or ""
        label = ", ".join(p for p in (city, country) if p) or "your location"
        return Location(lat=float(lat), lon=float(lon), label=label)

    if isinstance(spec, str):
        # Geocode a city name via Open-Meteo
        q = urllib.parse.quote(spec.strip())
        url = f"https://geocoding-api.open-meteo.com/v1/search?name={q}&count=1&language=en&format=json"
        data = http_get_json(url)
        results = data.get("results") or []
        if not results:
            raise ValueError(f"Could not geocode location: {spec!r}")
        r = results[0]
        bits = [r.get("name"), r.get("admin1"), r.get("country")]
        label = ", ".join(b for b in bits if b)
        return Location(lat=float(r["latitude"]), lon=float(r["longitude"]), label=label)

    raise ValueError(f"Unrecognised location config: {spec!r}")


# --------------------------------------------------------------------------- #
# Weather                                                                     #
# --------------------------------------------------------------------------- #

@dataclass
class WeatherSnapshot:
    timezone: str
    current_temp_c: float | None
    current_wind_ms: float | None
    current_precip_mm: float | None
    weather_code: int | None
    hourly_time: list[str] = field(default_factory=list)
    hourly_precip_mm: list[float] = field(default_factory=list)
    hourly_precip_prob: list[float] = field(default_factory=list)
    hourly_wind_ms: list[float] = field(default_factory=list)
    hourly_temp_c: list[float] = field(default_factory=list)


def fetch_weather(loc: Location, tz: str | None) -> WeatherSnapshot:
    params = {
        "latitude": f"{loc.lat}",
        "longitude": f"{loc.lon}",
        "current": "temperature_2m,precipitation,wind_speed_10m,weather_code",
        "hourly": "temperature_2m,precipitation,precipitation_probability,wind_speed_10m",
        "wind_speed_unit": "ms",
        "timezone": tz or "auto",
        "forecast_days": 2,  # plenty of hourly data for our 6-24h lookaheads
    }
    url = "https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode(params)
    data = http_get_json(url)
    current = data.get("current") or {}
    hourly = data.get("hourly") or {}
    return WeatherSnapshot(
        timezone=data.get("timezone") or "UTC",
        current_temp_c=_get_float(current, "temperature_2m"),
        current_wind_ms=_get_float(current, "wind_speed_10m"),
        current_precip_mm=_get_float(current, "precipitation"),
        weather_code=_get_int(current, "weather_code"),
        hourly_time=hourly.get("time") or [],
        hourly_temp_c=hourly.get("temperature_2m") or [],
        hourly_precip_mm=hourly.get("precipitation") or [],
        hourly_precip_prob=hourly.get("precipitation_probability") or [],
        hourly_wind_ms=hourly.get("wind_speed_10m") or [],
    )


def _get_float(d: dict[str, Any], key: str) -> float | None:
    v = d.get(key)
    return float(v) if isinstance(v, (int, float)) else None


def _get_int(d: dict[str, Any], key: str) -> int | None:
    v = d.get(key)
    return int(v) if isinstance(v, (int, float)) else None


# WMO weather code -> short label. https://open-meteo.com/en/docs (Weather variable)
WMO_CODES: dict[int, str] = {
    0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "depositing rime fog",
    51: "light drizzle", 53: "drizzle", 55: "dense drizzle",
    56: "light freezing drizzle", 57: "freezing drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain",
    66: "light freezing rain", 67: "freezing rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains",
    80: "rain showers", 81: "heavy rain showers", 82: "violent rain showers",
    85: "snow showers", 86: "heavy snow showers",
    95: "thunderstorm", 96: "thunderstorm w/ hail", 99: "severe thunderstorm w/ hail",
}

PRECIP_CODES = set(range(51, 100))  # everything drizzle/rain/snow/thunderstorm


# --------------------------------------------------------------------------- #
# Geomagnetic Kp                                                              #
# --------------------------------------------------------------------------- #

@dataclass
class KpForecast:
    """Upcoming planetary-K-index values."""
    rows: list[tuple[datetime, float]] = field(default_factory=list)

    def max_within(self, hours: int) -> tuple[datetime, float] | None:
        if not self.rows:
            return None
        now = datetime.now(timezone.utc)
        upcoming = [(t, k) for (t, k) in self.rows if 0 <= (t - now).total_seconds() <= hours * 3600]
        if not upcoming:
            return None
        return max(upcoming, key=lambda r: r[1])


def fetch_kp() -> KpForecast:
    """
    Fetch upcoming planetary Kp values from NOAA SWPC.

    SWPC has changed the response shape of this feed at least once in the
    wild: it has been served as both a list-of-lists (first row a header)
    and a list-of-objects. We tolerate either. We also try the forecast
    endpoint first and fall back to the observed-Kp feed so we still get
    *some* signal if the forecast feed is unavailable.
    """
    urls = (
        "https://services.swpc.noaa.gov/products/noaa-planetary-k-index-forecast.json",
        "https://services.swpc.noaa.gov/products/noaa-planetary-k-index.json",
    )
    last_err: Exception | None = None
    for url in urls:
        try:
            data = http_get_json(url)
        except Exception as exc:
            last_err = exc
            continue
        rows = _parse_kp_payload(data)
        if rows:
            return KpForecast(rows=rows)
    if last_err is not None:
        log.warning("Kp fetch failed: %s", last_err)
    return KpForecast()


_KP_TIME_KEYS: tuple[str, ...] = (
    "time_tag", "model_prediction_time", "valid_time", "prediction_time", "time"
)
_KP_VALUE_KEYS: tuple[str, ...] = (
    "kp", "predicted_kp", "kp_index", "estimated_kp"
)


def _parse_kp_payload(data: Any) -> list[tuple[datetime, float]]:
    """Parse NOAA's Kp feed. Tolerates both list-of-lists and list-of-dicts."""
    if not isinstance(data, list) or not data:
        return []

    rows: list[tuple[datetime, float]] = []
    first = data[0]

    if isinstance(first, dict):
        # List-of-objects shape.
        for row in data:
            if not isinstance(row, dict):
                continue
            ts = _first_present(row, _KP_TIME_KEYS)
            kp = _first_present(row, _KP_VALUE_KEYS)
            dt = _parse_kp_timestamp(ts)
            try:
                kpv = float(kp) if kp is not None else None
            except (ValueError, TypeError):
                kpv = None
            if dt is not None and kpv is not None:
                rows.append((dt, kpv))
    else:
        # 2-D array shape: first row is a header.
        header = [str(h).lower() for h in first] if hasattr(first, "__iter__") else []
        time_idx = header.index("time_tag") if "time_tag" in header else 0
        # The "kp" column may also appear as "kp_index" depending on the feed.
        if "kp" in header:
            kp_idx = header.index("kp")
        elif "kp_index" in header:
            kp_idx = header.index("kp_index")
        else:
            kp_idx = 1

        for row in data[1:]:
            if isinstance(row, dict) or not hasattr(row, "__getitem__"):
                continue
            try:
                ts = row[time_idx]
                kp = float(row[kp_idx])
            except (IndexError, ValueError, TypeError):
                continue
            dt = _parse_kp_timestamp(ts)
            if dt is not None:
                rows.append((dt, kp))

    rows.sort(key=lambda r: r[0])
    return rows


def _first_present(d: dict[str, Any], keys: tuple[str, ...]) -> Any:
    """Return d[k] for the first key in `keys` that's present and non-empty."""
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return None


def _parse_kp_timestamp(ts: Any) -> datetime | None:
    """Parse the assortment of timestamp shapes SWPC has served over the years."""
    if not isinstance(ts, str) or not ts:
        return None
    s = ts.strip()
    # Try the most common literal formats first.
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    # ISO 8601 with optional Z or offset.
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)
    except (ValueError, AttributeError):
        return None


# --------------------------------------------------------------------------- #
# Alert logic                                                                 #
# --------------------------------------------------------------------------- #

@dataclass
class Alert:
    category: str   # "temperature" | "geomagnetic" | "precipitation" | "wind"
    message: str    # short, human-readable


def _slice_window(times: list[str], values: list[float], hours: int) -> list[float]:
    """Return the next `hours` hourly values starting from now in the series' local tz."""
    if not times or not values:
        return []
    # Open-Meteo hourly times are ISO without tz when timezone=auto (they are local).
    # We just take the next N hours from "now in the same wall-clock".
    now_local_naive = datetime.now().replace(minute=0, second=0, microsecond=0)
    out: list[float] = []
    for ts, v in zip(times, values):
        try:
            t = datetime.fromisoformat(ts)
        except ValueError:
            continue
        delta_h = (t - now_local_naive).total_seconds() / 3600.0
        if 0 <= delta_h < hours and v is not None:
            out.append(float(v))
    return out


def evaluate(snap: WeatherSnapshot, kp: KpForecast, cfg: dict[str, Any]) -> list[Alert]:
    th = cfg["thresholds"]
    alerts: list[Alert] = []

    # 1. Temperature
    t = snap.current_temp_c
    if t is not None:
        if t <= th["temp_low_c"]:
            alerts.append(Alert("temperature", f"Cold: {t:.1f}°C (≤ {th['temp_low_c']}°C)"))
        elif t >= th["temp_high_c"]:
            alerts.append(Alert("temperature", f"Hot: {t:.1f}°C (≥ {th['temp_high_c']}°C)"))

    # 2. Geomagnetic
    hit = kp.max_within(int(cfg["kp_lookahead_h"]))
    if hit and hit[1] >= float(th["kp_index"]):
        when = hit[0].strftime("%Y-%m-%d %H:%MZ")
        alerts.append(Alert("geomagnetic", f"Geomagnetic storm: Kp {hit[1]:.1f} at {when}"))

    # 3. Precipitation in the next N hours
    n_precip = int(cfg["precip_lookahead_h"])
    precip_mm = _slice_window(snap.hourly_time, snap.hourly_precip_mm, n_precip)
    precip_prob = _slice_window(snap.hourly_time, snap.hourly_precip_prob, n_precip)
    total_mm = sum(precip_mm) if precip_mm else 0.0
    max_prob = max(precip_prob) if precip_prob else 0.0
    code_label = WMO_CODES.get(snap.weather_code or -1, "")
    if total_mm >= float(th["precip_mm"]) or max_prob >= float(th["precip_prob_pct"]):
        kind = ""
        if any(c in code_label for c in ("snow",)):
            kind = "Snow"
        elif any(c in code_label for c in ("rain", "drizzle", "thunder")):
            kind = "Rain"
        elif (snap.weather_code or -1) in PRECIP_CODES:
            kind = "Precipitation"
        else:
            kind = "Precipitation"
        alerts.append(Alert(
            "precipitation",
            f"{kind} in next {n_precip}h: up to {max_prob:.0f}% / {total_mm:.1f} mm total"
            + (f" (now: {code_label})" if code_label else "")
        ))

    # 4. Wind in the next N hours
    n_wind = int(cfg["wind_lookahead_h"])
    winds = _slice_window(snap.hourly_time, snap.hourly_wind_ms, n_wind)
    max_wind = max(winds) if winds else (snap.current_wind_ms or 0.0)
    if max_wind >= float(th["wind_ms"]):
        kmh = max_wind * 3.6
        alerts.append(Alert("wind", f"Strong wind in next {n_wind}h: up to {max_wind:.1f} m/s (~{kmh:.0f} km/h)"))

    return alerts


# --------------------------------------------------------------------------- #
# Output                                                                      #
# --------------------------------------------------------------------------- #

def format_report(loc: Location, snap: WeatherSnapshot, alerts: list[Alert]) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [f"Weather check for {loc.label} ({snap.timezone}) at {now}"]
    if snap.current_temp_c is not None:
        cond = WMO_CODES.get(snap.weather_code or -1, "")
        cond_str = f", {cond}" if cond else ""
        wind = f", wind {snap.current_wind_ms:.1f} m/s" if snap.current_wind_ms is not None else ""
        lines.append(f"Now: {snap.current_temp_c:.1f}°C{cond_str}{wind}")
    if not alerts:
        lines.append("No alerts.")
    else:
        lines.append("ALERTS:")
        for a in alerts:
            lines.append(f"  • [{a.category}] {a.message}")
    return "\n".join(lines)


def send_desktop_notification(title: str, body: str) -> None:
    """Best-effort desktop notification. Silently skipped if plyer isn't installed."""
    try:
        from plyer import notification  # type: ignore
    except Exception:
        log.debug("plyer not available; skipping desktop notification")
        return
    try:
        notification.notify(title=title, message=body, app_name="Weather Alerts", timeout=10)
    except Exception as exc:  # pragma: no cover - platform-specific
        log.warning("desktop notification failed: %s", exc)


# --------------------------------------------------------------------------- #
# Entry point                                                                 #
# --------------------------------------------------------------------------- #

def load_config(path: Path) -> dict[str, Any]:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy
    if path.exists():
        with path.open("r", encoding="utf-8") as f:
            user = json.load(f)
        # shallow merge, with nested merge for "thresholds"
        for k, v in user.items():
            if k == "thresholds" and isinstance(v, dict):
                cfg["thresholds"].update(v)
            else:
                cfg[k] = v
    return cfg


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Send 3x/day weather + geomagnetic alerts.")
    parser.add_argument("--config", type=Path, default=CONFIG_PATH_DEFAULT,
                        help=f"Path to config.json (default: {CONFIG_PATH_DEFAULT.name})")
    parser.add_argument("--quiet-when-no-alerts", action="store_true",
                        help="Exit silently (no stdout, no notification) when no alerts fired.")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose logging.")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    cfg = load_config(args.config)

    try:
        loc = resolve_location(cfg["location"])
        snap = fetch_weather(loc, cfg.get("timezone"))
        kp = fetch_kp()
    except urllib.error.URLError as exc:
        # Surface a much more actionable message for the common SSL failure
        # rather than just dumping the raw OpenSSL error.
        reason = getattr(exc, "reason", exc)
        msg = str(reason)
        if "CERTIFICATE_VERIFY_FAILED" in msg or isinstance(reason, ssl.SSLCertVerificationError):
            log.error(
                "TLS certificate verification failed.\n"
                "  Your Python install lacks trusted root certificates.\n"
                "  Quick fixes (pick one):\n"
                "    1.  pip install certifi\n"
                "    2.  /Applications/Python\\ 3.x/Install\\ Certificates.command   "
                "(python.org Python on macOS — adjust 3.x to your version)\n"
                "    3.  SSL_CERT_FILE=/etc/ssl/cert.pem python3 weather_alerts.py\n"
                "  Underlying error: %s",
                msg,
            )
        else:
            log.error("network error: %s", msg)
        return 2
    except Exception as exc:
        log.error("failed to fetch data: %s", exc)
        return 2

    alerts = evaluate(snap, kp, cfg)
    report = format_report(loc, snap, alerts)

    if alerts or not args.quiet_when_no_alerts:
        print(report)
    if alerts and cfg.get("desktop_notification", True):
        body = "\n".join(f"• {a.message}" for a in alerts)
        send_desktop_notification(f"Weather alerts — {loc.label}", body)

    return 0


if __name__ == "__main__":
    sys.exit(main())
