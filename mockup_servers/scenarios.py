"""
Scenario engine and control API for the mockup servers.

A scenario adds a time-varying temperature offset ("extra heat") to chosen
Redfish servers and standalone sensors. The devices keep their normal noise,
spikes and errors on top of it.

Control API (default http://127.0.0.1:8100):
    GET  /api/scenarios                          list scenarios + state
    GET  /api/scenarios/<name>                   state of one scenario, live readings
    POST /api/scenarios/<name>/start             start (optional JSON overrides)
    POST /api/scenarios/<name>/stop              stop; temperatures cool back down
    POST /api/scenarios/reset                    stop everything immediately, no cool-down
    GET  /api/errors                             every error type, per API, with per-device rates
"""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

# Scenario definitions. Server / sensor numbers are 1-based positions
# (server 1 = first Redfish port, sensor 1 = first sensor port).
SCENARIOS = {
    "rising-temperature": {
        "description": "Cooling degrades: temperatures on servers 1-5 and sensors 1-3 "
                       "climb steadily, fans spin up, then hold at the maximum rise.",
        "servers": [1, 2, 3, 4, 5],
        "sensors": [1, 2, 3],
        # 1.5 C/min: warnings after a few minutes, criticals after ~10-20 min.
        "rate_c_per_min": 1.5,
        "max_rise_c": 35.0,
        "cooldown_c_per_min": 3.0,
    },
}


class Scenario:
    def __init__(self, name, spec):
        self.name = name
        self.spec = dict(spec)
        self.config = dict(spec)
        self.phase = "idle"          # idle | rising | holding | cooling
        self.started_at = None
        self.stopped_at = None
        self.offset_at_stop = 0.0

    def _rise(self, now):
        rate = self.config["rate_c_per_min"]
        return min(self.config["max_rise_c"], rate * (now - self.started_at) / 60.0)

    def offset(self, now=None):
        now = time.time() if now is None else now
        if self.phase in ("rising", "holding"):
            off = self._rise(now)
            self.phase = "holding" if off >= self.config["max_rise_c"] else "rising"
            return off
        if self.phase == "cooling":
            off = self.offset_at_stop - self.config["cooldown_c_per_min"] * (now - self.stopped_at) / 60.0
            if off <= 0:
                self.phase = "idle"
                return 0.0
            return off
        return 0.0

    def start(self, overrides):
        cfg = dict(self.spec)
        cfg.pop("description", None)
        for k, v in (overrides or {}).items():
            if k not in cfg:
                raise ValueError("unknown field '%s' (allowed: %s)" % (k, ", ".join(sorted(cfg))))
            if k in ("servers", "sensors"):
                if not isinstance(v, list) or not all(isinstance(i, int) and i >= 1 for i in v):
                    raise ValueError("'%s' must be a list of 1-based numbers" % k)
            elif not isinstance(v, (int, float)) or v <= 0:
                raise ValueError("'%s' must be a positive number" % k)
            cfg[k] = v
        # Restarting while cooling continues from the current offset.
        current = self.offset()
        self.config = cfg
        rate = cfg["rate_c_per_min"]
        self.started_at = time.time() - (current / rate) * 60.0
        self.phase = "rising"
        self.stopped_at = None

    def stop(self):
        if self.phase in ("rising", "holding"):
            self.offset_at_stop = self.offset()
            self.stopped_at = time.time()
            self.phase = "cooling" if self.offset_at_stop > 0 else "idle"

    def reset(self):
        self.phase = "idle"
        self.started_at = self.stopped_at = None
        self.offset_at_stop = 0.0


class ScenarioManager:
    def __init__(self, servers, probes):
        self.servers = servers      # list of MockServer
        self.probes = probes        # list of temp_sensors.Probe
        self.lock = threading.Lock()
        self.scenarios = {n: Scenario(n, s) for n, s in SCENARIOS.items()}
        for i, srv in enumerate(servers):
            srv.extra_heat = (lambda n=i + 1: self.heat_for("servers", n))
        for i, pr in enumerate(probes):
            pr.extra_heat = (lambda n=i + 1: self.heat_for("sensors", n))

    def heat_for(self, group, number):
        with self.lock:
            now = time.time()
            return sum(sc.offset(now) for sc in self.scenarios.values()
                       if number in sc.config[group])

    def state(self, sc, detail=False):
        off = sc.offset()
        cfg = sc.config
        out = {
            "name": sc.name,
            "description": sc.spec["description"],
            "phase": sc.phase,
            "current_rise_c": round(off, 2),
            "config": {k: v for k, v in cfg.items() if k != "description"},
        }
        if sc.started_at and sc.phase in ("rising", "holding"):
            out["elapsed_s"] = int(time.time() - sc.started_at)
            remaining = (cfg["max_rise_c"] - off) / cfg["rate_c_per_min"] * 60.0
            out["seconds_to_max_rise"] = max(0, int(remaining))
        if sc.phase == "cooling":
            out["seconds_to_normal"] = int(off / cfg["cooldown_c_per_min"] * 60.0)
        if detail:
            out["targets"] = self.live_targets(cfg)
        return out

    def live_targets(self, cfg):
        servers = []
        for n in cfg["servers"]:
            if n > len(self.servers):
                continue
            srv = self.servers[n - 1]
            temps = [s for s in srv.sensors if s.kind == "temp"]
            servers.append({
                "server": n, "name": srv.name, "port": srv.port,
                "extra_heat_c": round(srv.extra_heat(), 2),
                "last_readings_c": {s.name: s.value for s in temps},
                "health": srv.health_rollup(),
            })
        sensors = []
        for n in cfg["sensors"]:
            if n > len(self.probes):
                continue
            pr = self.probes[n - 1]
            sensors.append({
                "sensor": n, "sensor_id": pr.id, "port": pr.port,
                "extra_heat_c": round(pr.extra_heat(), 2),
                "last_reading_c": pr.latest["celsius"] if pr.latest else None,
                "level": pr.latest["level"] if pr.latest else None,
            })
        return {"servers": servers, "sensors": sensors}


def make_control_handler(mgr):
    class Handler(BaseHTTPRequestHandler):
        server_version = "MockupControl/1.0"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            print("[control :%d] %s - %s" % (self.server.server_address[1],
                                             self.address_string(), fmt % args), flush=True)

        def send_json(self, status, body):
            data = json.dumps(body, indent=2).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def fail(self, status, message):
            self.send_json(status, {"ok": False, "error": message})

        def parts(self):
            return [p for p in urlparse(self.path).path.split("/") if p]

        def do_GET(self):
            p = self.parts()
            if p == ["api", "errors"]:
                return self.send_json(200, {"ok": True, "apis": [
                    dict(mgr.catalogs["redfish"](), devices=[
                        {"name": s.name, "port": s.port, "personality": s.personality,
                         "error_rate": s.error_rate} for s in mgr.servers]),
                    dict(mgr.catalogs["sensor"](), devices=[
                        {"sensor_id": pr.id, "port": pr.port, "personality": pr.personality,
                         "error_rate": pr.error_rate} for pr in mgr.probes]),
                    {"api": "control", "request_errors": [
                        {"http_status": 400, "when": "bad JSON body, unknown field or bad value"},
                        {"http_status": 404, "when": "unknown scenario or path"}]},
                ]})
            if p in ([], ["api"], ["api", "scenarios"]):
                return self.send_json(200, {"ok": True, "scenarios": [
                    mgr.state(sc) for sc in mgr.scenarios.values()]})
            if len(p) == 3 and p[:2] == ["api", "scenarios"]:
                sc = mgr.scenarios.get(p[2])
                if not sc:
                    return self.fail(404, "no scenario named '%s'" % p[2])
                return self.send_json(200, {"ok": True, "scenario": mgr.state(sc, detail=True)})
            self.fail(404, "not found")

        def do_POST(self):
            p = self.parts()
            n = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(n) or b"{}") if n else {}
            except ValueError:
                return self.fail(400, "body is not valid JSON")
            if not isinstance(body, dict):
                return self.fail(400, "body must be a JSON object")

            if p == ["api", "scenarios", "reset"]:
                with mgr.lock:
                    for sc in mgr.scenarios.values():
                        sc.reset()
                return self.send_json(200, {"ok": True, "scenarios": [
                    mgr.state(sc) for sc in mgr.scenarios.values()]})

            if len(p) == 4 and p[:2] == ["api", "scenarios"] and p[3] in ("start", "stop"):
                sc = mgr.scenarios.get(p[2])
                if not sc:
                    return self.fail(404, "no scenario named '%s'" % p[2])
                with mgr.lock:
                    try:
                        sc.start(body) if p[3] == "start" else sc.stop()
                    except ValueError as e:
                        return self.fail(400, str(e))
                print("[control] scenario '%s' %s -> phase %s" % (sc.name, p[3], sc.phase), flush=True)
                return self.send_json(200, {"ok": True, "scenario": mgr.state(sc, detail=True)})
            self.fail(404, "not found")

    return Handler


def start_control(servers, probes, host, port, catalogs):
    """catalogs: {"redfish": fn, "sensor": fn} returning each API's error list."""
    mgr = ScenarioManager(servers, probes)
    mgr.catalogs = catalogs
    httpd = ThreadingHTTPServer((host, port), make_control_handler(mgr))
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return mgr, httpd
