# Redfish mockup servers

Runs 10 fake Redfish BMCs (ports 8001–8010) plus 6 standalone temperature sensors with their own non-Redfish API (ports 9001–9006). Every request returns new, randomly drifting sensor readings, and requests can fail at random. Uses only the Python standard library.

```bash
python3 redfish_mockup.py
```

```bash
python3 poll_temps.py --watch 5
```

## Servers

| Port | Personality  | Behaviour |
|------|--------------|-----------|
| 8001, 8002, 8004, 8007, 8009 | normal | typical readings, 5% error rate |
| 8003 | hot          | CPU/exhaust ~18 °C hotter, more spikes into Warning/Critical |
| 8005 | noisy        | 3× sensor noise, frequent spikes |
| 8006 | flaky        | 20% request error rate, more sensor dropouts |
| 8008 | failing-fan  | one fan around 1400 RPM (Warning/Critical), often drops out |
| 8010 | stable       | no errors, low noise, no spikes (good as a control) |

Hardware differs per server: vendor, CPU count, fans, PSUs, GPU, serial and UUID. These come from `--seed`, so they stay the same between runs.

## Endpoints

- `/redfish/v1`, `/Systems/1`, `/Chassis/1`, `/Managers/1`, `/SessionService/Sessions`
- `/redfish/v1/Chassis/1/Thermal`: temperatures and fans (legacy schema)
- `/redfish/v1/Chassis/1/Power`: power use, PSUs and voltages
- `/redfish/v1/Chassis/1/Sensors` and `/Sensors/{id}`: the newer Sensor schema
- `POST /redfish/v1/Systems/1/Actions/ComputerSystem.Reset` with `{"ResetType": "ForceOff"}` or `"On"`. When a server is off, its temperatures fall toward ambient and its fans and power drop toward 0.

## Random failures

Each request has a chance (`--error-rate`, scaled per server) of failing with one of these:
`http_500`, `http_503` (with Retry-After), `http_504`, `http_404`, `slow` (2–6 s delay), `timeout` (hangs `--timeout-seconds`), `drop` (connection closed), `malformed` (truncated JSON).

Individual sensors can also go offline (`Reading: null`, `State: UnavailableOffline`) or spike past their thresholds.

Testing helpers:
- Header `X-Mockup-Error: <kind>` forces an error. `X-Mockup-Error: none` turns errors off for that request.
- `GET /mockup` shows a server's profile and error stats.
- `POST /mockup/config` with `{"error_rate": 0.5}` or `{"power_state": "Off"}` changes settings while it runs.

## Options

```
--count 10  --base-port 8001  --host 127.0.0.1  --error-rate 0.05
--timeout-seconds 30  --seed 42  --auth admin:password  --quiet
```

`--auth` turns on Basic auth and session-token checks (`X-Auth-Token` from a `POST /redfish/v1/SessionService/Sessions`).

`--control-port 8100` sets the scenario control API port (`0` = off).

`--sensors 6` sets how many standalone temperature sensors start alongside the servers (`0` = none). `--sensor-base-port 9001` sets the first sensor port.

# Standalone temperature sensors (not Redfish)

Six network temperature probes (ports 9001–9006), each with its own simple JSON API. `redfish_mockup.py` starts them automatically. To run only the sensors:

```bash
python3 temp_sensors.py
```

Unlike the Redfish servers, each probe samples in the background every `--interval` seconds (default 2) and keeps the last 720 readings.

| Port | Sensor | Location | Behaviour |
|------|--------|----------|-----------|
| 9001 | TS-ROOM-A   | Server room A, ambient | normal, ~21 °C |
| 9002 | TS-COLD-01  | Cold aisle 1           | normal, ~18 °C |
| 9003 | TS-HOT-01   | Hot aisle 1            | hot (~36 °C), noisy, frequent spikes |
| 9004 | TS-RACK-07T | Rack 07, top           | calibration drift: climbs slowly into warning/alarm; `CALIBRATION_DUE` alarm |
| 9005 | TS-UPS-01   | UPS / battery room     | value sometimes freezes ("stuck") for 10–40 samples |
| 9006 | TS-ROOF-01  | Rooftop chiller intake | battery-powered, goes offline often, `BATTERY_LOW`, 3× error rate |

## API

Successful responses are `{"ok": true, "data": {...}}`. Errors are `{"ok": false, "error": {"code": "...", "message": "...", "request_id": "..."}}`.

| Method | Path | Notes |
|--------|------|-------|
| GET | `/api/v1/info` | id, location, model, serial, MAC, firmware |
| GET | `/api/v1/temperature?unit=C\|F\|K` | latest reading plus humidity, `level` (normal/warning/alarm). Returns 502 `PROBE_OFFLINE` while the probe is down |
| GET | `/api/v1/history?limit=60&unit=C` | newest first. Offline samples have `value: null` |
| GET | `/api/v1/status` | `health` (healthy/degraded/critical), battery, signal, active alarms |
| GET / PUT | `/api/v1/thresholds` | `low_alarm`, `low_warn`, `high_warn`, `high_alarm` in °C. The order is checked (422 `BAD_ORDER`) |
| GET / POST | `/api/v1/mockup` | stats. Change the error rate with `{"error_rate": 0.3}` |

Random request errors: `server_error` (500), `busy` (503 + Retry-After), `rate_limited` (429 + Retry-After), `slow`, `timeout` (504), `drop`, `malformed`, `stale` (200 with a reading that is minutes old). Force one with the header `X-Mockup-Error: <kind>`, or turn errors off for one request with `X-Mockup-Error: none`.

## Scenarios (control API, port 8100)

`redfish_mockup.py` also starts a control API at `http://127.0.0.1:8100` (`--control-port`, `0` = off). A scenario adds heat on top of the normal random readings, so noise, spikes and errors keep happening.

```bash
curl -X POST http://127.0.0.1:8100/api/scenarios/rising-temperature/start
```

```bash
curl http://127.0.0.1:8100/api/scenarios/rising-temperature
```

```bash
curl -X POST http://127.0.0.1:8100/api/scenarios/rising-temperature/stop
```

| Method | Path | Notes |
|--------|------|-------|
| GET  | `/api/scenarios` | all scenarios and their phase |
| GET  | `/api/scenarios/<name>` | phase, current rise, time left, live readings of each target |
| POST | `/api/scenarios/<name>/start` | optional JSON body overrides the defaults below |
| POST | `/api/scenarios/<name>/stop` | cools back down at `cooldown_c_per_min` |
| POST | `/api/scenarios/reset` | turns all scenario heat off at once |

**`rising-temperature`**: Redfish servers 1–5 (ports 8001–8005) and sensors 1–3 (ports 9001–9003) heat up at **1.5 °C per minute**, up to **+35 °C**, then hold. The phases are `rising`, then `holding`, then `cooling` after stop, then `idle`. On the servers every temperature rises, fans spin up about 100 RPM per °C, and power draw grows about 3 W per °C. Rough timeline at the defaults:

| After | What you should see |
|-------|---------------------|
| ~3 min | hot-aisle sensor (9003) warning, room sensor (9001) warning ~4 min |
| ~5 min | server 3 ("hot") CPU caution (85 °C) |
| ~6–9 min | sensor alarms (9003, 9001, then 9002) |
| ~9–12 min | server inlets caution (38 °C), then critical (42 °C) |
| ~20 min | CPU caution on the normal servers |
| ~23 min | max rise reached, then holds |

Override any default, for example a faster run on different targets:

```bash
curl -X POST http://127.0.0.1:8100/api/scenarios/rising-temperature/start -d '{"rate_c_per_min": 3, "max_rise_c": 20, "servers": [1,2], "sensors": [1]}'
```

## Listing error types

Each route returns every error a device can produce in three groups: `random_errors` (with HTTP status, error code, share, and the chance per request on that device), `request_errors` (returned for bad input) and `data_faults` (problems inside a normal 200 response).

| Route | Scope |
|-------|-------|
| `GET http://127.0.0.1:8100/api/errors` | every API (Redfish, sensors, control) plus each device's error rate |
| `GET http://127.0.0.1:8001/mockup/errors` | one Redfish server |
| `GET http://127.0.0.1:9001/api/v1/mockup/errors` | one standalone sensor |

These routes never get random errors.

`poll_temps.py` polls both the Redfish servers and the sensors. Use `--sensors 0` or `--count 0` to skip either group.
