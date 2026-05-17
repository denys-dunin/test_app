# weather-alerts

A tiny Python script that runs three times a day and warns you about:

1. **High or low temperature**
2. **Geomagnetic activity** (planetary Kp index)
3. **Rain / snow / drizzle / thunderstorms**
4. **Strong wind**

Pure Python standard library at runtime — no API keys, no accounts. The only
optional dependency is [`plyer`](https://pypi.org/project/plyer/), which lets
the script raise a native desktop notification on macOS, Linux and Windows.

## Data sources

| Signal       | Source                                                              |
| ------------ | ------------------------------------------------------------------- |
| Location     | [ipapi.co](https://ipapi.co) (auto) or Open-Meteo geocoding (city)  |
| Weather      | [Open-Meteo](https://open-meteo.com) forecast API                   |
| Geomagnetic  | [NOAA SWPC](https://services.swpc.noaa.gov) planetary K-index feed  |

## Install

```bash
git clone <your-fork-url> weather-alerts
cd weather-alerts

# Optional: desktop notifications
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Copy the example config and edit if you like
cp config.example.json config.json
```

`config.json` is gitignored so you can keep personal settings out of source
control. If you skip this step, the defaults from `config.example.json` are
used.

## Run it once

```bash
python3 weather_alerts.py
```

Sample output:

```
Weather check for Kyiv, Ukraine (Europe/Kiev) at 2026-05-17 13:00
Now: 8.4°C, light rain, wind 11.2 m/s
ALERTS:
  • [precipitation] Rain in next 6h: up to 80% / 2.4 mm total (now: light rain)
  • [wind] Strong wind in next 6h: up to 12.3 m/s (~44 km/h)
```

Useful flags:

- `--config PATH` — point at a non-default config file
- `--quiet-when-no-alerts` — print nothing & skip the notification when the
  weather is uneventful (handy in cron)
- `--verbose` — debug logging

## Run it 3× per day

### macOS / Linux (cron)

```cron
# m  h    *  *  *  command
  0  8,13,19  *  *  *  cd /path/to/weather-alerts && /path/to/.venv/bin/python weather_alerts.py --quiet-when-no-alerts >> alerts.log 2>&1
```

### macOS (`launchd`)

Drop a plist into `~/Library/LaunchAgents/com.you.weather-alerts.plist` with a
`StartCalendarInterval` array for the three times, then `launchctl load` it.

### Windows (Task Scheduler)

Create a daily task with three triggers (08:00, 13:00, 19:00) that runs
`pythonw.exe weather_alerts.py --quiet-when-no-alerts` in the repo directory.

## Configuration

Everything lives in `config.json` and shadows the defaults in
`config.example.json`.

| Key                     | Meaning                                                                          |
| ----------------------- | -------------------------------------------------------------------------------- |
| `location`              | `"auto"` for IP-based lookup, a city name like `"Kyiv"`, or `{"lat":..,"lon":..}` |
| `thresholds.temp_low_c` | Cold alert when current temp ≤ this (°C)                                         |
| `thresholds.temp_high_c`| Hot alert when current temp ≥ this (°C)                                          |
| `thresholds.kp_index`   | Geomagnetic alert when forecast Kp ≥ this within 24h                             |
| `thresholds.precip_prob_pct` | Rain/snow alert when max probability ≥ this (%) within 6h                   |
| `thresholds.precip_mm`  | …or total precipitation ≥ this (mm) within 6h                                    |
| `thresholds.wind_ms`    | Wind alert when max forecast wind ≥ this (m/s) within 6h                         |
| `precip_lookahead_h`    | Lookahead window for precipitation (hours)                                       |
| `wind_lookahead_h`      | Lookahead window for wind (hours)                                                |
| `kp_lookahead_h`        | Lookahead window for geomagnetic activity (hours)                                |
| `timezone`              | Override the location's tz (e.g. `"Europe/Kiev"`); `null` = auto                 |
| `desktop_notification`  | Set false to disable native notifications                                        |

## Tests

```bash
python3 -m unittest test_weather_alerts.py
```

Tests cover the alert evaluation logic with mocked data — they don't hit the
network.

## License

MIT — do whatever you want.
