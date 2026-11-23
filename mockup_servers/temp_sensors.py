#!/usr/bin/env python3
"""
Standalone network temperature sensors (NOT Redfish).

Simulates 6 independent environmental probes (room / rack / aisle sensors),
each on its own port with its own small JSON REST API. Every probe samples
in the background, so readings change over time, and history is kept.

Each probe has a personality (hot aisle, drifting calibration, stuck value,
dropouts, low battery, ...) and requests can randomly fail with errors in
this API's own error format.

    python3 temp_sensors.py                   # 6 probes on ports 9001-9006
    python3 temp_sensors.py --error-rate 0    # no random errors

API (per probe), all under /api/v1:
    GET  /api/v1/info                 device identity and firmware
    GET  /api/v1/temperature[?unit=C|F|K]   latest reading
    GET  /api/v1/history[?limit=N&unit=..]  recent readings, newest first
    GET  /api/v1/status               health, battery, signal, alarms
    GET  /api/v1/thresholds           alarm thresholds
    PUT  /api/v1/thresholds           {"high_warn": 30, "high_alarm": 35, ...}
    GET  /api/v1/mockup               mockup stats (error counts, personality)
    POST /api/v1/mockup               {"error_rate": 0.3}
"""

import argparse
import collections
import json
import random
import socket
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

# (id, location, model, baseline °C, personality)
PROBES = [
    ("TS-ROOM-A",   "Server room A, ambient",    "ThermoProbe TP-100", 21.5, "normal"),
    ("TS-COLD-01",  "Cold aisle 1, row 3",       "ThermoProbe TP-100", 18.0, "normal"),
    ("TS-HOT-01",   "Hot aisle 1, row 3",        "ThermoProbe TP-200", 36.0, "hot"),
    ("TS-RACK-07T", "Rack 07, top U42",          "AirSense AS-3",      27.0, "drifting"),
    ("TS-UPS-01",   "UPS / battery room",        "AirSense AS-3",      23.0, "stuck"),
    ("TS-ROOF-01",  "Rooftop chiller intake",    "ThermoProbe TP-200", 12.0, "dropouts"),
]

# Personality tweaks.
#   error_mult : multiplier on request error rate
#   noise      : std-dev of reading noise (°C)
#   drift      : °C per sample added to the baseline (calibration drift)
#   stuck      : chance per sample of the value freezing for a while
#   dropout    : chance per sample of the sensor going offline for a while
#   battery    : starting battery % (None = mains powered)
#   spike      : chance per sample of a transient spike
PERSONALITIES = {
    "normal":   dict(error_mult=1.0, noise=0.15, drift=0.0,   stuck=0.0,  dropout=0.002, battery=None, spike=0.01),
    "hot":      dict(error_mult=1.0, noise=0.6,  drift=0.0,   stuck=0.0,  dropout=0.002, battery=None, spike=0.05),
    "drifting": dict(error_mult=1.0, noise=0.2,  drift=0.004, stuck=0.0,  dropout=0.002, battery=None, spike=0.01),
    "stuck":    dict(error_mult=1.5, noise=0.2,  drift=0.0,   stuck=0.02, dropout=0.002, battery=None, spike=0.01),
    "dropouts": dict(error_mult=3.0, noise=0.4,  drift=0.0,   stuck=0.0,  dropout=0.03,  battery=34,   spike=0.02),
}

DEFAULT_THRESHOLDS = {
    "normal":   dict(low_alarm=10, low_warn=15, high_warn=27, high_alarm=32),
    "hot":      dict(low_alarm=20, low_warn=25, high_warn=40, high_alarm=45),
    "drifting": dict(low_alarm=10, low_warn=15, high_warn=32, high_alarm=38),
    "stuck":    dict(low_alarm=10, low_warn=15, high_warn=28, high_alarm=33),
    "dropouts": dict(low_alarm=-10, low_warn=0, high_warn=30, high_alarm=38),
}

# Errors injected at random (weight = relative share when an error fires).
# Single source of truth for injection and for /api/v1/mockup/errors.
RANDOM_ERRORS = [
    dict(kind="server_error", weight=25, http_status=500, error_code="INTERNAL_ERROR",
         description="Unexpected firmware fault."),
    dict(kind="busy", weight=20, http_status=503, error_code="SENSOR_BUSY", retry_after="1-5 s",
         description="Probe busy sampling; Retry-After header is set."),
    dict(kind="rate_limited", weight=10, http_status=429, error_code="RATE_LIMITED",
         retry_after="2-15 s", description="Too many requests; Retry-After header is set."),
    dict(kind="slow", weight=15, http_status=200, error_code=None,
         description="Correct response, but delayed 1-5 s."),
    dict(kind="timeout", weight=7, http_status=504, error_code="PROBE_TIMEOUT",
         description="Request hangs for --timeout-seconds (default 30), then 504."),
    dict(kind="drop", weight=8, http_status=None, error_code=None,
         description="TCP connection closed without any HTTP response."),
    dict(kind="malformed", weight=7, http_status=200, error_code=None,
         description="200 OK with a truncated, unparseable JSON body."),
    dict(kind="stale", weight=8, http_status=200, error_code=None,
         description="200 OK on /temperature, but the reading is 5-30 minutes old "
                     "(check timestamp). Other endpoints answer normally."),
]
ERROR_KINDS = [(e["kind"], e["weight"]) for e in RANDOM_ERRORS]

# Errors returned deterministically (never random).
REQUEST_ERRORS = [
    dict(http_status=502, error_code="PROBE_OFFLINE",
         when="GET /temperature while the probe is offline (dropout)"),
    dict(http_status=400, error_code="BAD_UNIT", when="unit is not C, F or K"),
    dict(http_status=400, error_code="BAD_LIMIT", when="history limit is not an integer"),
    dict(http_status=400, error_code="BAD_JSON", when="PUT/POST body is not valid JSON"),
    dict(http_status=400, error_code="BAD_FIELD", when="PUT /thresholds with an unknown field"),
    dict(http_status=400, error_code="BAD_VALUE", when="PUT /thresholds with a non-number"),
    dict(http_status=422, error_code="BAD_ORDER",
         when="thresholds not ordered low_alarm <= low_warn < high_warn <= high_alarm"),
    dict(http_status=400, error_code="BAD_MOCKUP_ERROR", when="Unknown X-Mockup-Error kind"),
    dict(http_status=404, error_code="NOT_FOUND", when="Unknown endpoint"),
    dict(http_status=405, error_code="METHOD_NOT_ALLOWED", when="Wrong HTTP method for the endpoint"),
]

# Faults visible in successful responses (/status alarms and reading fields).
DATA_FAULTS = [
    dict(fault="PROBE_OFFLINE", where="/status alarms, /history value null",
         description="Probe offline for 3-15 samples; health critical."),
    dict(fault="TEMP_WARNING", where="/temperature level, /status alarms",
         description="Reading at or past low_warn / high_warn; health degraded."),
    dict(fault="TEMP_ALARM", where="/temperature level, /status alarms",
         description="Reading at or past low_alarm / high_alarm; health critical."),
    dict(fault="BATTERY_LOW", where="/status alarms", description="Battery below 20% (TS-ROOF-01)."),
    dict(fault="CALIBRATION_DUE", where="/status alarms",
         description="Drifting probe after ~300 samples (TS-RACK-07T)."),
    dict(fault="stuck_value", where="/temperature, /history",
         description="Value frozen for 10-40 samples; not reported as an alarm (TS-UPS-01)."),
]


def error_catalog(error_rate=None):
    """Error list for the standalone sensors; with error_rate, adds each kind's chance per request."""
    total = sum(e["weight"] for e in RANDOM_ERRORS)
    random_errors = []
    for e in RANDOM_ERRORS:
        e = dict(e, share_pct=round(100.0 * e["weight"] / total, 1))
        if error_rate is not None:
            e["chance_per_request_pct"] = round(error_rate * 100.0 * e["weight"] / total, 2)
        random_errors.append(e)
    return {
        "api": "standalone-sensor",
        "error_format": {"ok": False, "error": {"code": "<error_code>", "message": "...",
                                                "request_id": "..."}},
        "force_with_header": "X-Mockup-Error: <kind>  (or 'none' to disable for one request)",
        "random_errors": random_errors,
        "request_errors": REQUEST_ERRORS,
        "data_faults": DATA_FAULTS,
    }


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")[:-4] + "Z"


def convert(c, unit):
    if c is None:
        return None
    if unit == "F":
        return round(c * 9 / 5 + 32, 2)
    if unit == "K":
        return round(c + 273.15, 2)
    return round(c, 2)


class Probe:
    def __init__(self, index, host, port, base_error_rate, interval, timeout_seconds,
                 seed, quiet):
        sid, location, model, baseline, personality = PROBES[index % len(PROBES)]
        if index >= len(PROBES):
            sid = "%s-%d" % (sid, index // len(PROBES) + 1)
        prng = random.Random(seed * 7919 + index)
        self.p = PERSONALITIES[personality]

        self.id = sid
        self.location = location
        self.model = model
        self.personality = personality
        self.host, self.port = host, port
        self.quiet = quiet
        self.interval = interval
        self.timeout_seconds = timeout_seconds
        self.error_rate = round(min(1.0, base_error_rate * self.p["error_mult"]), 4)
        self.serial = "%s-%06d" % (model.split()[1], prng.randint(0, 999999))
        self.mac = "02:%02x:%02x:%02x:%02x:%02x" % tuple(prng.randint(0, 255) for _ in range(5))
        self.firmware = "%d.%d.%d" % (prng.randint(1, 4), prng.randint(0, 9), prng.randint(0, 30))
        self.uuid = str(uuid.UUID(int=prng.getrandbits(128)))
        self.thresholds = dict(DEFAULT_THRESHOLDS[personality])

        self.rng = random.Random()
        self.lock = threading.Lock()
        self.started = time.time()
        self.baseline = baseline + prng.uniform(-1, 1)
        self.current = self.baseline
        self.humidity = prng.uniform(30, 55)
        self.battery = self.p["battery"]
        self.stuck_for = 0
        self.offline_for = 0
        self.seq = 0
        self.latest = None
        self.history = collections.deque(maxlen=720)
        self.stats = {"requests": 0, "errors_injected": 0, "by_kind": {}}
        self.extra_heat = lambda: 0.0   # set by scenarios.ScenarioManager
        self.sample()

    # -- simulation -----------------------------------------------------------

    def sample(self):
        with self.lock:
            p, r = self.p, self.rng
            now = time.time()
            self.seq += 1
            self.baseline += p["drift"]
            # Slow daily-ish wave so values move even for calm probes.
            wave = 0.8 * __import__("math").sin((now - self.started) / 300.0)
            if self.battery is not None:
                self.battery = max(0.0, self.battery - 0.01)

            if self.offline_for > 0:
                self.offline_for -= 1
                value, state = None, "offline"
            elif r.random() < p["dropout"]:
                self.offline_for = r.randint(3, 15)
                value, state = None, "offline"
            elif self.stuck_for > 0:
                self.stuck_for -= 1
                value = self.latest["celsius"] if self.latest and self.latest["celsius"] is not None else self.current
                state = "ok"
            else:
                if r.random() < p["stuck"]:
                    self.stuck_for = r.randint(10, 40)
                target = self.baseline + wave + self.extra_heat()
                self.current += (target - self.current) * 0.2 + r.gauss(0, p["noise"])
                value = self.current
                if r.random() < p["spike"]:
                    value += r.uniform(3, 9) * r.choice([1, 1, -1])
                value = round(value, 2)
                state = "ok"

            self.humidity = max(10, min(90, self.humidity + r.gauss(0, 0.3)))
            rec = {
                "seq": self.seq,
                "timestamp": iso(now),
                "epoch_ms": int(now * 1000),
                "celsius": value,
                "humidity_pct": round(self.humidity, 1) if value is not None else None,
                "state": state,
                "level": self.level(value),
            }
            self.latest = rec
            self.history.appendleft(rec)

    def level(self, c):
        if c is None:
            return "unknown"
        t = self.thresholds
        if c >= t["high_alarm"] or c <= t["low_alarm"]:
            return "alarm"
        if c >= t["high_warn"] or c <= t["low_warn"]:
            return "warning"
        return "normal"

    def run_sampler(self, stop):
        while not stop.wait(self.interval):
            self.sample()

    def log(self, msg):
        if not self.quiet:
            print("[%s :%d] %s" % (self.id, self.port, msg), flush=True)

    # -- API payloads ---------------------------------------------------------

    def reading_payload(self, rec, unit):
        return {
            "sensor_id": self.id,
            "seq": rec["seq"],
            "timestamp": rec["timestamp"],
            "epoch_ms": rec["epoch_ms"],
            "value": convert(rec["celsius"], unit),
            "unit": unit,
            "humidity_pct": rec["humidity_pct"],
            "state": rec["state"],
            "level": rec["level"],
        }

    def info(self):
        return {
            "sensor_id": self.id,
            "location": self.location,
            "model": self.model,
            "serial": self.serial,
            "uuid": self.uuid,
            "mac": self.mac,
            "firmware": self.firmware,
            "sample_interval_s": self.interval,
            "power_source": "battery" if self.battery is not None else "poe",
            "uptime_s": int(time.time() - self.started),
        }

    def status(self):
        rec = self.latest
        alarms = []
        if rec["state"] == "offline":
            alarms.append({"code": "PROBE_OFFLINE", "message": "No response from probe element"})
        if rec["level"] in ("warning", "alarm"):
            alarms.append({"code": "TEMP_%s" % rec["level"].upper(),
                           "message": "Temperature %.2f C outside %s range"
                                      % (rec["celsius"], rec["level"])})
        if self.stuck_for > 0:
            # A real device wouldn't know; expose it only via the mockup endpoint.
            pass
        if self.battery is not None and self.battery < 20:
            alarms.append({"code": "BATTERY_LOW", "message": "Battery at %.0f%%" % self.battery})
        if self.personality == "drifting" and self.seq > 300:
            alarms.append({"code": "CALIBRATION_DUE", "message": "Probe calibration overdue"})
        health = "critical" if any(a["code"] in ("PROBE_OFFLINE", "TEMP_ALARM") for a in alarms) \
            else "degraded" if alarms else "healthy"
        return {
            "sensor_id": self.id,
            "health": health,
            "probe_state": rec["state"],
            "battery_pct": None if self.battery is None else round(self.battery, 1),
            "signal_dbm": self.rng.randint(-72, -48) if self.battery is not None else None,
            "last_sample": rec["timestamp"],
            "alarms": alarms,
        }

    def mockup_info(self):
        return {
            "sensor_id": self.id,
            "port": self.port,
            "personality": self.personality,
            "error_rate": self.error_rate,
            "baseline_c": round(self.baseline, 2),
            "scenario_heat_c": round(self.extra_heat(), 2),
            "stuck_samples_left": self.stuck_for,
            "offline_samples_left": self.offline_for,
            "stats": self.stats,
            "hint": "POST /api/v1/mockup {\"error_rate\": 0.5}; header 'X-Mockup-Error: <kind>' "
                    "forces an error (kinds: %s, none)" % ", ".join(k for k, _ in ERROR_KINDS),
        }


def api_error(status, code, message, request_id):
    return status, {"ok": False, "error": {"code": code, "message": message,
                                           "request_id": request_id}}


def make_handler(probe):
    class Handler(BaseHTTPRequestHandler):
        server_version = "ThermoProbeHTTP/%s" % probe.firmware
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            probe.log("%s - %s" % (self.address_string(), fmt % args))

        def send_json(self, status, body, headers=None):
            data = json.dumps(body, indent=2).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("X-Sensor-Id", probe.id)
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def ok(self, data):
            self.send_json(200, {"ok": True, "data": data})

        def fail(self, status, code, message, headers=None):
            _, body = api_error(status, code, message, uuid.uuid4().hex[:8])
            self.send_json(status, body, headers)

        def unit(self, q):
            u = (q.get("unit", ["C"])[0] or "C").upper()
            return u if u in ("C", "F", "K") else None

        def inject(self, path):
            """Returns 'done' if a response was sent, 'stale' for stale data, else None."""
            if path.startswith("/api/v1/mockup"):
                return None
            forced = self.headers.get("X-Mockup-Error")
            if forced == "none":
                return None
            if forced:
                kind = forced
            elif probe.rng.random() < probe.error_rate:
                kind = probe.rng.choices([k for k, _ in ERROR_KINDS],
                                         [w for _, w in ERROR_KINDS])[0]
            else:
                return None
            with probe.lock:
                probe.stats["errors_injected"] += 1
                probe.stats["by_kind"][kind] = probe.stats["by_kind"].get(kind, 0) + 1
            probe.log("injecting error '%s' on %s %s" % (kind, self.command, path))

            if kind == "server_error":
                self.fail(500, "INTERNAL_ERROR", "Unexpected firmware fault")
            elif kind == "busy":
                self.fail(503, "SENSOR_BUSY", "Probe is busy sampling, try again",
                          {"Retry-After": str(probe.rng.randint(1, 5))})
            elif kind == "rate_limited":
                self.fail(429, "RATE_LIMITED", "Too many requests",
                          {"Retry-After": str(probe.rng.randint(2, 15))})
            elif kind == "slow":
                time.sleep(probe.rng.uniform(1, 5))
                return None
            elif kind == "timeout":
                time.sleep(probe.timeout_seconds)
                try:
                    self.fail(504, "PROBE_TIMEOUT", "Probe element did not answer in time")
                except OSError:
                    pass
            elif kind == "drop":
                self.close_connection = True
                try:
                    self.connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            elif kind == "malformed":
                data = json.dumps({"ok": True, "data": probe.info()}).encode()
                data = data[: probe.rng.randint(5, len(data) // 2)]
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            elif kind == "stale":
                return "stale"
            else:
                self.fail(400, "BAD_MOCKUP_ERROR", "Unknown X-Mockup-Error kind '%s'" % kind)
            return "done"

        def count(self):
            with probe.lock:
                probe.stats["requests"] += 1

        def do_GET(self):
            self.count()
            url = urlparse(self.path)
            path, q = url.path.rstrip("/") or "/", parse_qs(url.query)
            inj = self.inject(path)
            if inj == "done":
                return
            unit = self.unit(q)
            if unit is None:
                return self.fail(400, "BAD_UNIT", "unit must be one of C, F, K")

            with probe.lock:
                if path in ("/", "/api", "/api/v1"):
                    return self.ok({"endpoints": ["/api/v1/info", "/api/v1/temperature",
                                                  "/api/v1/history", "/api/v1/status",
                                                  "/api/v1/thresholds", "/api/v1/mockup",
                                                  "/api/v1/mockup/errors"]})
                if path == "/api/v1/info":
                    return self.ok(probe.info())
                if path == "/api/v1/temperature":
                    rec = probe.latest
                    if inj == "stale" and len(probe.history) > 1:
                        rec = dict(probe.history[-1])
                        rec["timestamp"] = iso(time.time() - probe.rng.uniform(300, 1800))
                        rec["epoch_ms"] = int(time.time() * 1000) - 600000
                    if rec["state"] == "offline":
                        return self.fail(502, "PROBE_OFFLINE",
                                         "Probe element not responding (last good sample unavailable)")
                    return self.ok(probe.reading_payload(rec, unit))
                if path == "/api/v1/history":
                    try:
                        limit = max(1, min(720, int(q.get("limit", ["60"])[0])))
                    except ValueError:
                        return self.fail(400, "BAD_LIMIT", "limit must be an integer")
                    items = [probe.reading_payload(r, unit) for r in list(probe.history)[:limit]]
                    return self.ok({"sensor_id": probe.id, "count": len(items), "items": items})
                if path == "/api/v1/status":
                    return self.ok(probe.status())
                if path == "/api/v1/thresholds":
                    return self.ok(dict(probe.thresholds, unit="C"))
                if path == "/api/v1/mockup":
                    return self.ok(probe.mockup_info())
                if path == "/api/v1/mockup/errors":
                    return self.ok(dict(error_catalog(probe.error_rate), sensor_id=probe.id,
                                        port=probe.port, error_rate=probe.error_rate))
            self.fail(404, "NOT_FOUND", "No such endpoint: %s" % path)

        def read_json(self):
            n = int(self.headers.get("Content-Length") or 0)
            try:
                return json.loads(self.rfile.read(n) or b"{}") if n else {}
            except ValueError:
                return None

        def do_PUT(self):
            self.count()
            path = urlparse(self.path).path.rstrip("/")
            body = self.read_json()
            if body is None:
                return self.fail(400, "BAD_JSON", "Request body is not valid JSON")
            if self.inject(path) == "done":
                return
            if path != "/api/v1/thresholds":
                return self.fail(405, "METHOD_NOT_ALLOWED", "PUT not allowed on %s" % path)
            new = dict(probe.thresholds)
            for k, v in body.items():
                if k not in new:
                    return self.fail(400, "BAD_FIELD", "Unknown threshold '%s'" % k)
                if not isinstance(v, (int, float)):
                    return self.fail(400, "BAD_VALUE", "'%s' must be a number" % k)
                new[k] = v
            if not (new["low_alarm"] <= new["low_warn"] < new["high_warn"] <= new["high_alarm"]):
                return self.fail(422, "BAD_ORDER",
                                 "Require low_alarm <= low_warn < high_warn <= high_alarm")
            with probe.lock:
                probe.thresholds = new
            self.ok(dict(new, unit="C"))

        def do_POST(self):
            self.count()
            path = urlparse(self.path).path.rstrip("/")
            body = self.read_json()
            if body is None:
                return self.fail(400, "BAD_JSON", "Request body is not valid JSON")
            if path == "/api/v1/mockup":
                if "error_rate" in body:
                    probe.error_rate = max(0.0, min(1.0, float(body["error_rate"])))
                return self.ok(probe.mockup_info())
            self.fail(405, "METHOD_NOT_ALLOWED", "POST not allowed on %s" % path)

    return Handler


def start_probes(count=6, host="127.0.0.1", base_port=9001, error_rate=0.05, interval=2.0,
                 timeout_seconds=30, seed=42, quiet=False):
    """Start probes in background threads. Returns (probes, httpds, stop_event)."""
    stop = threading.Event()
    probes, httpds = [], []
    for i in range(count):
        probe = Probe(i, host, base_port + i, error_rate, interval, timeout_seconds, seed, quiet)
        httpd = ThreadingHTTPServer((host, probe.port), make_handler(probe))
        httpd.daemon_threads = True
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        threading.Thread(target=probe.run_sampler, args=(stop,), daemon=True).start()
        probes.append(probe)
        httpds.append(httpd)
    return probes, httpds, stop


def print_probe_table(probes, host):
    print("  %-12s %-34s %-27s %-10s %5s" % ("SENSOR", "URL", "LOCATION", "PERSONALITY", "ERR%"))
    for p in probes:
        print("  %-12s %-34s %-27s %-10s %5.1f" % (
            p.id, "http://%s:%d/api/v1" % (host, p.port), p.location[:27], p.personality,
            p.error_rate * 100))


def stop_probes(httpds, stop):
    stop.set()
    for h in httpds:
        h.shutdown()
        h.server_close()


def main():
    ap = argparse.ArgumentParser(description="Run standalone (non-Redfish) temperature sensors.")
    ap.add_argument("--count", type=int, default=6)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--base-port", type=int, default=9001)
    ap.add_argument("--error-rate", type=float, default=0.05)
    ap.add_argument("--interval", type=float, default=2.0, help="seconds between samples")
    ap.add_argument("--timeout-seconds", type=float, default=30)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()

    probes, httpds, stop = start_probes(a.count, a.host, a.base_port, a.error_rate, a.interval,
                                        a.timeout_seconds, a.seed, a.quiet)
    print("Standalone temperature sensors: %d running\n" % len(probes))
    print_probe_table(probes, a.host)
    print("\nTry: curl -s http://%s:%d/api/v1/temperature" % (a.host, a.base_port))
    print("Press Ctrl+C to stop.\n", flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        print("\nShutting down...")
        stop_probes(httpds, stop)


if __name__ == "__main__":
    main()
