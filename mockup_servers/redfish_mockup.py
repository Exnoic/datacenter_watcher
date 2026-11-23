#!/usr/bin/env python3
"""
Redfish mockup servers.

Starts N (default 10) independent Redfish-style BMC endpoints on consecutive
ports. Each server has its own hardware profile (vendor, CPU count, fans,
PSUs, ...) and a "personality" that shapes its readings: some run hot, one
is flaky, one has a dying fan, etc.

Every request returns fresh, randomly drifting sensor values (temperatures,
fan RPM, voltages, power), and requests can randomly fail with HTTP errors,
timeouts, dropped connections, slow responses or malformed JSON.

Only the Python standard library is used (Python 3.7+).

    python3 redfish_mockup.py                 # 10 servers on ports 8001-8010
    python3 redfish_mockup.py --error-rate 0  # no random errors
    python3 redfish_mockup.py --count 3 --base-port 9000 --host 0.0.0.0
"""

import argparse
import base64
import json
import math
import random
import re
import socket
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import scenarios
import temp_sensors

# ---------------------------------------------------------------------------
# Static data used to build server profiles
# ---------------------------------------------------------------------------

VENDORS = [
    ("Acme Compute", "AC-R650", "Acme iBMC"),
    ("Northwind Systems", "NW-2U Gen5", "Northwind NMC"),
    ("Fabrikam", "FX-1200", "Fabrikam Lights-Out"),
    ("Contoso", "CT-Blade 7", "Contoso XCC"),
    ("Initech", "IT-Rack 4400", "Initech BMC"),
]

CPU_MODELS = [
    "Mock Xeon Gold 6338 @ 2.00GHz",
    "Mock EPYC 7543 32-Core",
    "Mock Xeon Silver 4314 @ 2.40GHz",
    "Mock EPYC 9354 32-Core",
]

# Personality -> tweaks applied on top of the randomly generated profile.
#   error_mult : multiplier on the base error rate
#   heat       : degrees added to CPU / exhaust baseline temperatures
#   noise_mult : multiplier on sensor noise
#   spike_mult : multiplier on spike probability
#   fail_mult  : multiplier on sensor dropout probability
PERSONALITIES = {
    "normal":       dict(error_mult=1.0, heat=0,  noise_mult=1.0, spike_mult=1.0, fail_mult=1.0),
    "hot":          dict(error_mult=1.0, heat=18, noise_mult=1.3, spike_mult=2.0, fail_mult=1.0),
    "flaky":        dict(error_mult=4.0, heat=0,  noise_mult=1.0, spike_mult=1.0, fail_mult=3.0),
    "failing-fan":  dict(error_mult=1.5, heat=8,  noise_mult=1.2, spike_mult=1.5, fail_mult=1.5),
    "noisy":        dict(error_mult=1.0, heat=3,  noise_mult=3.0, spike_mult=4.0, fail_mult=1.0),
    "stable":       dict(error_mult=0.0, heat=-5, noise_mult=0.3, spike_mult=0.0, fail_mult=0.0),
}

# Personality assignment for the first 10 servers (cycled if --count > 10).
PERSONALITY_ORDER = [
    "normal", "normal", "hot", "normal", "noisy",
    "flaky", "normal", "failing-fan", "normal", "stable",
]

# Weighted error types that can be injected into a request.
# Errors injected at random (weight = relative share when an error fires).
# This list is the single source of truth: injection picks from it and the
# /mockup/errors route publishes it.
RANDOM_ERRORS = [
    dict(kind="http_500", weight=25, http_status=500, error_code="Base.1.16.InternalError",
         description="Internal server error with a Redfish error body."),
    dict(kind="http_503", weight=20, http_status=503,
         error_code="Base.1.16.ServiceTemporarilyUnavailable", retry_after="1-10 s",
         description="Service temporarily unavailable; Retry-After header is set."),
    dict(kind="http_504", weight=8, http_status=504, error_code="Base.1.16.OperationTimeout",
         description="Gateway timeout returned immediately (sensor bus timed out)."),
    dict(kind="http_404", weight=5, http_status=404, error_code="Base.1.16.ResourceNotFound",
         description="A valid resource is reported as not found."),
    dict(kind="slow", weight=20, http_status=200, error_code=None,
         description="Correct response, but delayed 2-6 s."),
    dict(kind="timeout", weight=7, http_status=504, error_code="Base.1.16.OperationTimeout",
         description="Request hangs for --timeout-seconds (default 30), then 504 if the client is still there."),
    dict(kind="drop", weight=8, http_status=None, error_code=None,
         description="TCP connection closed without any HTTP response."),
    dict(kind="malformed", weight=7, http_status=200, error_code=None,
         description="200 OK with a truncated, unparseable JSON body."),
]
ERROR_KINDS = [(e["kind"], e["weight"]) for e in RANDOM_ERRORS]

# Errors returned deterministically for bad requests (never random).
REQUEST_ERRORS = [
    dict(http_status=400, error_code="Base.1.16.MalformedJSON", when="POST body is not valid JSON"),
    dict(http_status=400, error_code="Base.1.16.ActionParameterValueNotInList",
         when="ComputerSystem.Reset with an unsupported ResetType"),
    dict(http_status=400, error_code="Base.1.16.GeneralError", when="Unknown X-Mockup-Error kind"),
    dict(http_status=401, error_code="Base.1.16.NoValidSession",
         when="--auth is set and credentials / X-Auth-Token are missing or wrong"),
    dict(http_status=404, error_code="Base.1.16.ResourceNotFound", when="Unknown path or session"),
    dict(http_status=405, error_code="Base.1.16.ActionNotSupported", when="POST to a path that has no action"),
]

# Faults that show up inside otherwise successful (200) responses.
DATA_FAULTS = [
    dict(fault="sensor_offline", where="Thermal / Power / Sensors",
         description="Reading is null, Status.State UnavailableOffline, Health Critical; "
                     "recovers after a few reads."),
    dict(fault="threshold_warning", where="Thermal / Power / Sensors",
         description="Reading at or past UpperCaution / LowerCaution: Health Warning."),
    dict(fault="threshold_critical", where="Thermal / Power / Sensors",
         description="Reading at or past UpperCritical / LowerCritical: Health Critical "
                     "(spikes, the hot server, the failing fan, or a scenario)."),
    dict(fault="health_rollup", where="Systems/1, Chassis/1, Thermal",
         description="Status.HealthRollup reflects the worst sensor."),
]


def error_catalog(error_rate=None):
    """Error list for the Redfish servers; with error_rate, adds each kind's chance per request."""
    total = sum(e["weight"] for e in RANDOM_ERRORS)
    random_errors = []
    for e in RANDOM_ERRORS:
        e = dict(e, share_pct=round(100.0 * e["weight"] / total, 1))
        if error_rate is not None:
            e["chance_per_request_pct"] = round(error_rate * 100.0 * e["weight"] / total, 2)
        random_errors.append(e)
    return {
        "api": "redfish",
        "error_format": {"error": {"code": "<error_code>", "message": "...",
                                   "@Message.ExtendedInfo": ["..."]}},
        "force_with_header": "X-Mockup-Error: <kind>  (or 'none' to disable for one request)",
        "random_errors": random_errors,
        "request_errors": REQUEST_ERRORS,
        "data_faults": DATA_FAULTS,
    }


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Sensors
# ---------------------------------------------------------------------------

class Sensor:
    """A single sensor with a mean-reverting random walk, spikes and dropouts."""

    def __init__(self, sid, name, kind, context, units, base, noise,
                 thresholds, rng_range, off_base=None, spike_chance=0.0,
                 spike=(0, 0), fail_chance=0.0, decimals=1, number=0):
        self.id = sid
        self.name = name
        self.kind = kind            # temp | fan | voltage | power
        self.context = context      # Redfish PhysicalContext
        self.units = units
        self.base = base
        self.off_base = base if off_base is None else off_base
        self.noise = noise
        self.thresholds = thresholds  # dict: UpperCaution, UpperCritical, ...
        self.range = rng_range        # (min, max)
        self.spike_chance = spike_chance
        self.spike = spike
        self.fail_chance = fail_chance
        self.decimals = decimals
        self.number = number
        self.current = None
        self.failed = False
        self.value = None
        self.last_sample = None

    def sample(self, rng, powered, heat=0.0):
        target = self.base if powered else self.off_base
        if heat and powered:
            # Scenario heat: temps rise with it, fans spin up, power draw grows.
            target += {"temp": heat, "fan": heat * 100, "power": heat * 3}.get(self.kind, 0)
        now = time.time()
        dt = 2.0 if self.last_sample is None else now - self.last_sample
        self.last_sample = now
        # Converge faster after long gaps so rarely polled values aren't stale.
        alpha = max(0.25, 1 - math.exp(-dt / 8.0))
        if self.failed:
            # A failed sensor recovers after a few reads.
            if rng.random() < 0.25:
                self.failed = False
            else:
                self.value = None
                return
        elif rng.random() < self.fail_chance:
            self.failed = True
            self.value = None
            return

        if self.current is None:
            self.current = target
        self.current += (target - self.current) * alpha + rng.gauss(0, self.noise)
        v = self.current
        if rng.random() < self.spike_chance:
            v += rng.uniform(*self.spike)   # transient spike, not persisted
        lo, hi = self.range
        v = max(lo, min(hi, v))
        self.value = round(v, self.decimals) if self.decimals else int(round(v))

    def state_health(self):
        if self.value is None:
            return "UnavailableOffline", "Critical"
        t, v = self.thresholds, self.value
        if ("UpperCritical" in t and v >= t["UpperCritical"]) or \
           ("LowerCritical" in t and v <= t["LowerCritical"]):
            return "Enabled", "Critical"
        if ("UpperCaution" in t and v >= t["UpperCaution"]) or \
           ("LowerCaution" in t and v <= t["LowerCaution"]):
            return "Enabled", "Warning"
        return "Enabled", "OK"

    def status(self):
        state, health = self.state_health()
        return {"State": state, "Health": health}

    def reading_type(self):
        return {"temp": "Temperature", "fan": "Rotational",
                "voltage": "Voltage", "power": "Power"}[self.kind]


HEALTH_ORDER = {"OK": 0, "Warning": 1, "Critical": 2}


def worst_health(healths):
    worst = "OK"
    for h in healths:
        if HEALTH_ORDER.get(h, 0) > HEALTH_ORDER[worst]:
            worst = h
    return worst


# ---------------------------------------------------------------------------
# A single mock BMC
# ---------------------------------------------------------------------------

class MockServer:
    def __init__(self, index, host, port, base_error_rate, seed, timeout_seconds,
                 require_auth, quiet):
        self.index = index
        self.host = host
        self.port = port
        self.quiet = quiet
        self.require_auth = require_auth
        self.timeout_seconds = timeout_seconds
        self.lock = threading.Lock()
        self.rng = random.Random()                 # live randomness
        prng = random.Random(seed * 1000 + index)  # deterministic profile

        self.personality = PERSONALITY_ORDER[index % len(PERSONALITY_ORDER)]
        p = PERSONALITIES[self.personality]
        self.error_rate = round(min(1.0, base_error_rate * p["error_mult"]), 4)

        vendor, model, bmc = VENDORS[index % len(VENDORS)]
        self.manufacturer = vendor
        self.model = model
        self.bmc_model = bmc
        self.name = "mock-srv-%02d" % (index + 1)
        self.serial = "MCK%07d" % prng.randint(0, 9_999_999)
        self.uuid = str(uuid.UUID(int=prng.getrandbits(128)))
        self.bios_version = "%d.%d.%d" % (prng.randint(1, 3), prng.randint(0, 9), prng.randint(0, 20))
        self.bmc_firmware = "%d.%02d" % (prng.randint(2, 7), prng.randint(0, 99))
        self.cpu_count = prng.choice([1, 2, 2, 2])
        self.cpu_model = prng.choice(CPU_MODELS)
        self.memory_gib = self.cpu_count * prng.choice([64, 128, 256])
        self.fan_count = prng.choice([4, 6, 6, 8])
        self.psu_count = prng.choice([1, 2, 2])
        self.has_gpu = prng.random() < 0.3
        self.power_state = "On"
        self.boot_time = time.time()
        self.sessions = {}
        self.stats = {"requests": 0, "errors_injected": 0, "by_kind": {}}

        self.ambient = prng.uniform(18, 28)
        self.extra_heat = lambda: 0.0   # set by scenarios.ScenarioManager
        self.sensors = self._build_sensors(prng, p)

    # -- profile ------------------------------------------------------------

    def _build_sensors(self, prng, p):
        nm, sm, heat = p["noise_mult"], p["spike_mult"], p["heat"]
        amb = self.ambient
        sensors = []
        n = [0]

        def add(**kw):
            n[0] += 1
            kw["number"] = n[0]
            kw["fail_chance"] = kw.get("fail_chance", 0) * p["fail_mult"]
            s = Sensor(**kw)
            sensors.append(s)
            return s

        temp_thr = lambda c, cr, f: {"UpperCaution": c, "UpperCritical": cr, "UpperFatal": f}

        add(sid="InletTemp", name="System Inlet Temp", kind="temp", context="Intake",
            units="Cel", base=amb, off_base=amb, noise=0.3 * nm,
            thresholds=temp_thr(38, 42, 47), rng_range=(-10, 70),
            spike_chance=0.01 * sm, spike=(4, 10), fail_chance=0.003)
        add(sid="ExhaustTemp", name="System Exhaust Temp", kind="temp", context="Exhaust",
            units="Cel", base=amb + 14 + heat * 0.6, off_base=amb + 1, noise=0.6 * nm,
            thresholds=temp_thr(60, 70, 75), rng_range=(-10, 100),
            spike_chance=0.02 * sm, spike=(5, 15), fail_chance=0.003)
        for c in range(1, self.cpu_count + 1):
            add(sid="CPU%dTemp" % c, name="CPU%d Temp" % c, kind="temp", context="CPU",
                units="Cel", base=prng.uniform(45, 62) + heat, off_base=amb + 3,
                noise=1.2 * nm, thresholds=temp_thr(85, 95, 100), rng_range=(0, 125),
                spike_chance=0.03 * sm, spike=(8, 25), fail_chance=0.005)
            add(sid="DIMMZone%dTemp" % c, name="DIMM Zone %d Temp" % c, kind="temp",
                context="Memory", units="Cel", base=prng.uniform(35, 48) + heat * 0.5,
                off_base=amb + 2, noise=0.5 * nm, thresholds=temp_thr(80, 85, 95),
                rng_range=(0, 110), spike_chance=0.01 * sm, spike=(5, 12), fail_chance=0.003)
        add(sid="PCHTemp", name="PCH Temp", kind="temp", context="SystemBoard",
            units="Cel", base=prng.uniform(44, 55) + heat * 0.4, off_base=amb + 2,
            noise=0.5 * nm, thresholds=temp_thr(80, 90, 95), rng_range=(0, 110),
            spike_chance=0.01 * sm, spike=(5, 10), fail_chance=0.003)
        add(sid="NVMeBackplaneTemp", name="NVMe Backplane Temp", kind="temp",
            context="Backplane", units="Cel", base=amb + prng.uniform(10, 18) + heat * 0.3,
            off_base=amb + 1, noise=0.4 * nm, thresholds=temp_thr(65, 72, 80),
            rng_range=(0, 100), spike_chance=0.01 * sm, spike=(4, 10), fail_chance=0.004)
        if self.has_gpu:
            add(sid="GPU1Temp", name="GPU1 Temp", kind="temp", context="GPU",
                units="Cel", base=prng.uniform(55, 72) + heat, off_base=amb + 3,
                noise=2.0 * nm, thresholds=temp_thr(83, 90, 95), rng_range=(0, 120),
                spike_chance=0.04 * sm, spike=(5, 20), fail_chance=0.005)
        for ps in range(1, self.psu_count + 1):
            add(sid="PSU%dTemp" % ps, name="PSU%d Temp" % ps, kind="temp",
                context="PowerSupply", units="Cel", base=amb + prng.uniform(12, 20),
                off_base=amb + 1, noise=0.5 * nm, thresholds=temp_thr(70, 80, 90),
                rng_range=(0, 110), spike_chance=0.01 * sm, spike=(5, 10), fail_chance=0.003)

        # Fans
        fan_base = prng.choice([6000, 7200, 8400]) + heat * 120
        bad_fan = prng.randint(1, self.fan_count) if self.personality == "failing-fan" else None
        for f in range(1, self.fan_count + 1):
            base, noise, fail = fan_base + prng.uniform(-300, 300), 150 * nm, 0.003
            if f == bad_fan:
                base, noise, fail = 1400, 350, 0.05
            add(sid="Fan%d" % f, name="System Fan %d" % f, kind="fan", context="Fan",
                units="RPM", base=base, off_base=0, noise=noise,
                thresholds={"LowerCaution": 1500, "LowerCritical": 900, "LowerFatal": 500},
                rng_range=(0, 18000), spike_chance=0.02 * sm, spike=(-2500, 2500),
                fail_chance=fail, decimals=0)

        # Voltages
        for sid, nominal in (("Volt12V", 12.0), ("Volt5V", 5.0), ("Volt3V3", 3.3)):
            add(sid=sid, name="System Board %sV" % ("%g" % nominal), kind="voltage",
                context="VoltageRegulator", units="V", base=nominal * prng.uniform(0.99, 1.01),
                off_base=0 if nominal != 3.3 else 3.3, noise=nominal * 0.004 * nm,
                thresholds={"UpperCaution": nominal * 1.05, "UpperCritical": nominal * 1.1,
                            "LowerCaution": nominal * 0.95, "LowerCritical": nominal * 0.9},
                rng_range=(0, nominal * 1.5), spike_chance=0.005 * sm,
                spike=(-nominal * 0.08, nominal * 0.08), fail_chance=0.002, decimals=3)
        for c in range(1, self.cpu_count + 1):
            add(sid="CPU%dVCore" % c, name="CPU%d VCORE" % c, kind="voltage",
                context="CPU", units="V", base=prng.uniform(1.75, 1.85), off_base=0,
                noise=0.01 * nm,
                thresholds={"UpperCaution": 1.95, "UpperCritical": 2.05,
                            "LowerCaution": 1.6, "LowerCritical": 1.5},
                rng_range=(0, 2.5), spike_chance=0.005 * sm, spike=(-0.2, 0.2),
                fail_chance=0.002, decimals=3)

        # Power
        self.psu_capacity = prng.choice([800, 1100, 1600])
        idle = 120 + self.cpu_count * 60 + (150 if self.has_gpu else 0) + heat * 3
        add(sid="TotalPower", name="Total Power Consumption", kind="power",
            context="Chassis", units="W", base=idle + prng.uniform(0, 120), off_base=12,
            noise=15 * nm,
            thresholds={"UpperCaution": self.psu_capacity * self.psu_count * 0.8,
                        "UpperCritical": self.psu_capacity * self.psu_count * 0.95},
            rng_range=(0, self.psu_capacity * self.psu_count * 1.1),
            spike_chance=0.05 * sm, spike=(50, 250), fail_chance=0.002, decimals=0)
        return sensors

    def sensor(self, sid):
        for s in self.sensors:
            if s.id == sid:
                return s
        return None

    def of_kind(self, kind):
        return [s for s in self.sensors if s.kind == kind]

    def sample_all(self):
        with self.lock:
            powered = self.power_state == "On"
            heat = self.extra_heat()
            for s in self.sensors:
                s.sample(self.rng, powered, heat)

    def health_rollup(self, kinds=None):
        return worst_health(s.state_health()[1] for s in self.sensors
                            if kinds is None or s.kind in kinds)

    def log(self, msg):
        if not self.quiet:
            print("[%s :%d] %s" % (self.name, self.port, msg), flush=True)

    # -- Redfish resources ----------------------------------------------------

    def service_root(self):
        return {
            "@odata.id": "/redfish/v1",
            "@odata.type": "#ServiceRoot.v1_11_0.ServiceRoot",
            "Id": "RootService",
            "Name": "Root Service",
            "RedfishVersion": "1.15.0",
            "UUID": self.uuid,
            "Product": self.bmc_model,
            "Vendor": self.manufacturer,
            "Systems": {"@odata.id": "/redfish/v1/Systems"},
            "Chassis": {"@odata.id": "/redfish/v1/Chassis"},
            "Managers": {"@odata.id": "/redfish/v1/Managers"},
            "SessionService": {"@odata.id": "/redfish/v1/SessionService"},
            "Links": {"Sessions": {"@odata.id": "/redfish/v1/SessionService/Sessions"}},
        }

    @staticmethod
    def collection(odata_id, odata_type, name, members):
        return {
            "@odata.id": odata_id,
            "@odata.type": odata_type,
            "Name": name,
            "Members@odata.count": len(members),
            "Members": [{"@odata.id": m} for m in members],
        }

    def system(self):
        return {
            "@odata.id": "/redfish/v1/Systems/1",
            "@odata.type": "#ComputerSystem.v1_20_0.ComputerSystem",
            "Id": "1",
            "Name": self.name,
            "HostName": self.name,
            "SystemType": "Physical",
            "Manufacturer": self.manufacturer,
            "Model": self.model,
            "SerialNumber": self.serial,
            "UUID": self.uuid,
            "BiosVersion": self.bios_version,
            "PowerState": self.power_state,
            "Status": {"State": "Enabled", "Health": self.health_rollup({"temp", "voltage"}),
                       "HealthRollup": self.health_rollup()},
            "ProcessorSummary": {"Count": self.cpu_count, "Model": self.cpu_model,
                                 "Status": {"State": "Enabled",
                                            "Health": self.health_rollup({"temp"})}},
            "MemorySummary": {"TotalSystemMemoryGiB": self.memory_gib,
                              "Status": {"State": "Enabled", "Health": "OK"}},
            "Actions": {"#ComputerSystem.Reset": {
                "target": "/redfish/v1/Systems/1/Actions/ComputerSystem.Reset",
                "ResetType@Redfish.AllowableValues": ["On", "ForceOff", "GracefulShutdown",
                                                       "ForceRestart", "GracefulRestart"]}},
            "Links": {"Chassis": [{"@odata.id": "/redfish/v1/Chassis/1"}],
                      "ManagedBy": [{"@odata.id": "/redfish/v1/Managers/1"}]},
        }

    def chassis(self):
        return {
            "@odata.id": "/redfish/v1/Chassis/1",
            "@odata.type": "#Chassis.v1_23_0.Chassis",
            "Id": "1",
            "Name": "%s Chassis" % self.name,
            "ChassisType": "RackMount",
            "Manufacturer": self.manufacturer,
            "Model": self.model,
            "SerialNumber": self.serial,
            "PowerState": self.power_state,
            "Status": {"State": "Enabled", "Health": self.health_rollup(),
                       "HealthRollup": self.health_rollup()},
            "Thermal": {"@odata.id": "/redfish/v1/Chassis/1/Thermal"},
            "Power": {"@odata.id": "/redfish/v1/Chassis/1/Power"},
            "Sensors": {"@odata.id": "/redfish/v1/Chassis/1/Sensors"},
            "Links": {"ComputerSystems": [{"@odata.id": "/redfish/v1/Systems/1"}],
                      "ManagedBy": [{"@odata.id": "/redfish/v1/Managers/1"}]},
        }

    def thermal(self):
        temps = []
        for i, s in enumerate(self.of_kind("temp")):
            t = s.thresholds
            temps.append({
                "@odata.id": "/redfish/v1/Chassis/1/Thermal#/Temperatures/%d" % i,
                "MemberId": str(i),
                "Name": s.name,
                "SensorNumber": s.number,
                "ReadingCelsius": s.value,
                "UpperThresholdNonCritical": t.get("UpperCaution"),
                "UpperThresholdCritical": t.get("UpperCritical"),
                "UpperThresholdFatal": t.get("UpperFatal"),
                "MinReadingRangeTemp": s.range[0],
                "MaxReadingRangeTemp": s.range[1],
                "PhysicalContext": s.context,
                "Status": s.status(),
            })
        fans = []
        for i, s in enumerate(self.of_kind("fan")):
            t = s.thresholds
            fans.append({
                "@odata.id": "/redfish/v1/Chassis/1/Thermal#/Fans/%d" % i,
                "MemberId": str(i),
                "Name": s.name,
                "SensorNumber": s.number,
                "Reading": s.value,
                "ReadingUnits": "RPM",
                "LowerThresholdNonCritical": t.get("LowerCaution"),
                "LowerThresholdCritical": t.get("LowerCritical"),
                "LowerThresholdFatal": t.get("LowerFatal"),
                "MinReadingRange": s.range[0],
                "MaxReadingRange": s.range[1],
                "PhysicalContext": "Fan",
                "Status": s.status(),
            })
        return {
            "@odata.id": "/redfish/v1/Chassis/1/Thermal",
            "@odata.type": "#Thermal.v1_7_1.Thermal",
            "Id": "Thermal",
            "Name": "Thermal",
            "Status": {"State": "Enabled", "HealthRollup": self.health_rollup({"temp", "fan"})},
            "Temperatures": temps,
            "Fans": fans,
        }

    def power(self):
        total = self.sensor("TotalPower")
        psus = []
        share = (total.value or 0) / max(1, self.psu_count)
        for i in range(self.psu_count):
            psu_temp = self.sensor("PSU%dTemp" % (i + 1))
            psus.append({
                "@odata.id": "/redfish/v1/Chassis/1/Power#/PowerSupplies/%d" % i,
                "MemberId": str(i),
                "Name": "PSU%d" % (i + 1),
                "Manufacturer": self.manufacturer,
                "PowerSupplyType": "AC",
                "LineInputVoltage": round(self.rng.gauss(230, 1.5), 1),
                "PowerCapacityWatts": self.psu_capacity,
                "PowerOutputWatts": round(share + self.rng.gauss(0, 3), 0),
                "Status": psu_temp.status() if psu_temp else {"State": "Enabled", "Health": "OK"},
            })
        volts = []
        for i, s in enumerate(self.of_kind("voltage")):
            t = s.thresholds
            volts.append({
                "@odata.id": "/redfish/v1/Chassis/1/Power#/Voltages/%d" % i,
                "MemberId": str(i),
                "Name": s.name,
                "SensorNumber": s.number,
                "ReadingVolts": s.value,
                "UpperThresholdNonCritical": round(t["UpperCaution"], 3),
                "UpperThresholdCritical": round(t["UpperCritical"], 3),
                "LowerThresholdNonCritical": round(t["LowerCaution"], 3),
                "LowerThresholdCritical": round(t["LowerCritical"], 3),
                "PhysicalContext": s.context,
                "Status": s.status(),
            })
        return {
            "@odata.id": "/redfish/v1/Chassis/1/Power",
            "@odata.type": "#Power.v1_7_1.Power",
            "Id": "Power",
            "Name": "Power",
            "PowerControl": [{
                "@odata.id": "/redfish/v1/Chassis/1/Power#/PowerControl/0",
                "MemberId": "0",
                "Name": "System Power Control",
                "PowerConsumedWatts": total.value,
                "PowerCapacityWatts": self.psu_capacity * self.psu_count,
                "PowerMetrics": {
                    "IntervalInMin": 1,
                    "MinConsumedWatts": round(total.base * 0.85) if total.value is not None else None,
                    "MaxConsumedWatts": round(total.base * 1.35) if total.value is not None else None,
                    "AverageConsumedWatts": round(total.base) if total.value is not None else None,
                },
                "Status": total.status(),
            }],
            "PowerSupplies": psus,
            "Voltages": volts,
        }

    def sensor_resource(self, s):
        res = {
            "@odata.id": "/redfish/v1/Chassis/1/Sensors/%s" % s.id,
            "@odata.type": "#Sensor.v1_7_0.Sensor",
            "Id": s.id,
            "Name": s.name,
            "ReadingType": s.reading_type(),
            "Reading": s.value,
            "ReadingUnits": s.units,
            "ReadingRangeMin": s.range[0],
            "ReadingRangeMax": round(s.range[1], 3),
            "PhysicalContext": s.context,
            "Status": s.status(),
            "Thresholds": {k: {"Reading": round(v, 3)} for k, v in s.thresholds.items()},
        }
        return res

    def manager(self):
        return {
            "@odata.id": "/redfish/v1/Managers/1",
            "@odata.type": "#Manager.v1_17_0.Manager",
            "Id": "1",
            "Name": "%s BMC" % self.name,
            "ManagerType": "BMC",
            "Model": self.bmc_model,
            "FirmwareVersion": self.bmc_firmware,
            "UUID": self.uuid,
            "DateTime": now_iso(),
            "PowerState": "On",
            "Status": {"State": "Enabled", "Health": "OK"},
            "Links": {"ManagerForServers": [{"@odata.id": "/redfish/v1/Systems/1"}],
                      "ManagerForChassis": [{"@odata.id": "/redfish/v1/Chassis/1"}]},
        }

    def mockup_info(self):
        return {
            "name": self.name,
            "port": self.port,
            "personality": self.personality,
            "error_rate": self.error_rate,
            "manufacturer": self.manufacturer,
            "model": self.model,
            "serial": self.serial,
            "cpus": self.cpu_count,
            "fans": self.fan_count,
            "psus": self.psu_count,
            "gpu": self.has_gpu,
            "power_state": self.power_state,
            "scenario_heat_c": round(self.extra_heat(), 2),
            "uptime_seconds": int(time.time() - self.boot_time),
            "stats": self.stats,
            "hint": "POST /mockup/config {\"error_rate\": 0.5} to change error rate; "
                    "send header 'X-Mockup-Error: <kind>' to force an error "
                    "(kinds: %s)" % ", ".join(k for k, _ in ERROR_KINDS),
        }


# ---------------------------------------------------------------------------
# HTTP handling
# ---------------------------------------------------------------------------

def redfish_error(code, message, message_id="Base.1.16.GeneralError", severity="Critical"):
    return {"error": {
        "code": message_id,
        "message": message,
        "@Message.ExtendedInfo": [{
            "@odata.type": "#Message.v1_1_1.Message",
            "MessageId": message_id,
            "Message": message,
            "Severity": severity,
            "Resolution": "Retry the operation; if the problem persists, contact support.",
        }],
    }}


def make_handler(mock):
    class Handler(BaseHTTPRequestHandler):
        server_version = "MockRedfish/1.0"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            mock.log("%s - %s" % (self.address_string(), fmt % args))

        # -- helpers --------------------------------------------------------

        def send_json(self, status, body, extra_headers=None):
            data = json.dumps(body, indent=2).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("OData-Version", "4.0")
            self.send_header("Content-Length", str(len(data)))
            for k, v in (extra_headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def read_body(self):
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            raw = self.rfile.read(length)
            try:
                return json.loads(raw or b"{}")
            except ValueError:
                return None

        def authorized(self):
            if not mock.require_auth:
                return True
            token = self.headers.get("X-Auth-Token")
            if token and token in mock.sessions.values():
                return True
            auth = self.headers.get("Authorization", "")
            if auth.startswith("Basic "):
                try:
                    user, _, pw = base64.b64decode(auth[6:]).decode().partition(":")
                except Exception:
                    return False
                return (user, pw) == mock.require_auth
            return False

        def inject_error(self, path):
            """Returns True if an error response was sent (request is finished)."""
            if path.startswith("/mockup"):
                return False
            forced = self.headers.get("X-Mockup-Error")
            if forced == "none":
                return False
            if forced:
                kind = forced
            elif mock.rng.random() < mock.error_rate:
                kind = mock.rng.choices([k for k, _ in ERROR_KINDS],
                                        [w for _, w in ERROR_KINDS])[0]
            else:
                return False

            with mock.lock:
                mock.stats["errors_injected"] += 1
                mock.stats["by_kind"][kind] = mock.stats["by_kind"].get(kind, 0) + 1
            mock.log("injecting error '%s' on %s %s" % (kind, self.command, path))

            if kind == "http_500":
                self.send_json(500, redfish_error(500, "An internal error occurred.",
                                                  "Base.1.16.InternalError"))
            elif kind == "http_503":
                self.send_json(503, redfish_error(503, "The service is temporarily unavailable.",
                                                  "Base.1.16.ServiceTemporarilyUnavailable"),
                               {"Retry-After": str(mock.rng.randint(1, 10))})
            elif kind == "http_504":
                self.send_json(504, redfish_error(504, "Upstream sensor bus timed out.",
                                                  "Base.1.16.OperationTimeout"))
            elif kind == "http_404":
                self.send_json(404, redfish_error(404, "The resource %s was not found." % path,
                                                  "Base.1.16.ResourceNotFound"))
            elif kind == "slow":
                time.sleep(mock.rng.uniform(2, 6))
                return False  # continue with a normal (late) response
            elif kind == "timeout":
                time.sleep(mock.timeout_seconds)
                try:
                    self.send_json(504, redfish_error(504, "Request timed out.",
                                                      "Base.1.16.OperationTimeout"))
                except OSError:
                    pass  # client gave up already
            elif kind == "drop":
                self.close_connection = True
                try:
                    self.connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            elif kind == "malformed":
                data = json.dumps(mock.service_root()).encode()
                data = data[: mock.rng.randint(5, max(6, len(data) // 2))]
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            else:
                self.send_json(400, redfish_error(400, "Unknown X-Mockup-Error kind '%s'." % kind))
            return True

        # -- routing --------------------------------------------------------

        def route_get(self, path):
            m = mock
            routes = {
                "/redfish": lambda: {"v1": "/redfish/v1/"},
                "/redfish/v1": m.service_root,
                "/redfish/v1/Systems": lambda: m.collection(
                    "/redfish/v1/Systems", "#ComputerSystemCollection.ComputerSystemCollection",
                    "Computer System Collection", ["/redfish/v1/Systems/1"]),
                "/redfish/v1/Systems/1": m.system,
                "/redfish/v1/Chassis": lambda: m.collection(
                    "/redfish/v1/Chassis", "#ChassisCollection.ChassisCollection",
                    "Chassis Collection", ["/redfish/v1/Chassis/1"]),
                "/redfish/v1/Chassis/1": m.chassis,
                "/redfish/v1/Chassis/1/Thermal": m.thermal,
                "/redfish/v1/Chassis/1/Power": m.power,
                "/redfish/v1/Chassis/1/Sensors": lambda: m.collection(
                    "/redfish/v1/Chassis/1/Sensors", "#SensorCollection.SensorCollection",
                    "Sensors", ["/redfish/v1/Chassis/1/Sensors/%s" % s.id for s in m.sensors]),
                "/redfish/v1/Managers": lambda: m.collection(
                    "/redfish/v1/Managers", "#ManagerCollection.ManagerCollection",
                    "Manager Collection", ["/redfish/v1/Managers/1"]),
                "/redfish/v1/Managers/1": m.manager,
                "/redfish/v1/SessionService": lambda: {
                    "@odata.id": "/redfish/v1/SessionService",
                    "@odata.type": "#SessionService.v1_1_9.SessionService",
                    "Id": "SessionService", "Name": "Session Service",
                    "ServiceEnabled": True, "SessionTimeout": 1800,
                    "Sessions": {"@odata.id": "/redfish/v1/SessionService/Sessions"}},
                "/redfish/v1/SessionService/Sessions": lambda: m.collection(
                    "/redfish/v1/SessionService/Sessions", "#SessionCollection.SessionCollection",
                    "Session Collection",
                    ["/redfish/v1/SessionService/Sessions/%s" % sid for sid in m.sessions]),
            }
            if path in routes:
                return 200, routes[path]()
            sm = re.fullmatch(r"/redfish/v1/Chassis/1/Sensors/([^/]+)", path)
            if sm and m.sensor(sm.group(1)):
                return 200, m.sensor_resource(m.sensor(sm.group(1)))
            return 404, redfish_error(404, "The resource %s was not found." % path,
                                      "Base.1.16.ResourceNotFound")

        def do_GET(self):
            path = urlparse(self.path).path.rstrip("/") or "/"
            with mock.lock:
                mock.stats["requests"] += 1
            if path == "/mockup":
                return self.send_json(200, mock.mockup_info())
            if path == "/mockup/errors":
                return self.send_json(200, dict(error_catalog(mock.error_rate), server=mock.name,
                                                port=mock.port, error_rate=mock.error_rate))
            if self.inject_error(path):
                return
            if path.startswith("/redfish/v1") and path != "/redfish/v1" and not self.authorized():
                return self.send_json(401, redfish_error(401, "Authentication required.",
                                                         "Base.1.16.NoValidSession"),
                                      {"WWW-Authenticate": 'Basic realm="Redfish"'})
            mock.sample_all()
            with mock.lock:
                status, body = self.route_get(path)
            self.send_json(status, body)

        def do_POST(self):
            path = urlparse(self.path).path.rstrip("/")
            with mock.lock:
                mock.stats["requests"] += 1
            body = self.read_body()
            if body is None:
                return self.send_json(400, redfish_error(400, "Malformed JSON body.",
                                                         "Base.1.16.MalformedJSON"))

            if path == "/mockup/config":
                if "error_rate" in body:
                    mock.error_rate = max(0.0, min(1.0, float(body["error_rate"])))
                if "power_state" in body and body["power_state"] in ("On", "Off"):
                    mock.power_state = body["power_state"]
                return self.send_json(200, mock.mockup_info())

            if self.inject_error(path):
                return

            if path == "/redfish/v1/SessionService/Sessions":
                user, pw = body.get("UserName"), body.get("Password")
                if mock.require_auth and (user, pw) != mock.require_auth:
                    return self.send_json(401, redfish_error(401, "Invalid credentials.",
                                                             "Base.1.16.NoValidSession"))
                sid, token = uuid.uuid4().hex[:12], uuid.uuid4().hex
                mock.sessions[sid] = token
                loc = "/redfish/v1/SessionService/Sessions/%s" % sid
                return self.send_json(201, {
                    "@odata.id": loc, "@odata.type": "#Session.v1_6_0.Session",
                    "Id": sid, "Name": "User Session", "UserName": user},
                    {"X-Auth-Token": token, "Location": loc})

            if not self.authorized():
                return self.send_json(401, redfish_error(401, "Authentication required.",
                                                         "Base.1.16.NoValidSession"))

            if path == "/redfish/v1/Systems/1/Actions/ComputerSystem.Reset":
                rt = body.get("ResetType")
                if rt in ("ForceOff", "GracefulShutdown"):
                    mock.power_state = "Off"
                elif rt in ("On", "ForceRestart", "GracefulRestart"):
                    mock.power_state = "On"
                else:
                    return self.send_json(400, redfish_error(
                        400, "ResetType '%s' is not supported." % rt,
                        "Base.1.16.ActionParameterValueNotInList", "Warning"))
                mock.log("ResetType=%s -> PowerState=%s" % (rt, mock.power_state))
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return

            self.send_json(405, redfish_error(405, "POST not allowed on %s." % path,
                                              "Base.1.16.ActionNotSupported", "Warning"))

        def do_DELETE(self):
            path = urlparse(self.path).path.rstrip("/")
            m = re.fullmatch(r"/redfish/v1/SessionService/Sessions/([^/]+)", path)
            if m and m.group(1) in mock.sessions:
                del mock.sessions[m.group(1)]
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_json(404, redfish_error(404, "The resource %s was not found." % path,
                                              "Base.1.16.ResourceNotFound"))

    return Handler


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Run several Redfish mockup BMC servers.")
    ap.add_argument("--count", type=int, default=10, help="number of servers (default 10)")
    ap.add_argument("--host", default="127.0.0.1", help="bind address (default 127.0.0.1)")
    ap.add_argument("--base-port", type=int, default=8001, help="first port (default 8001)")
    ap.add_argument("--error-rate", type=float, default=0.05,
                    help="base probability a request fails (default 0.05); "
                         "scaled per server personality")
    ap.add_argument("--timeout-seconds", type=float, default=30,
                    help="how long a 'timeout' error hangs (default 30)")
    ap.add_argument("--seed", type=int, default=42,
                    help="seed for server profiles (identities stay stable across runs)")
    ap.add_argument("--auth", metavar="USER:PASS",
                    help="require Basic auth / session token with these credentials")
    ap.add_argument("--quiet", action="store_true", help="don't log every request")
    ap.add_argument("--sensors", type=int, default=6,
                    help="standalone (non-Redfish) temperature sensors to start (default 6, 0 = none)")
    ap.add_argument("--control-port", type=int, default=8100,
                    help="port for the scenario control API (default 8100, 0 = off)")
    ap.add_argument("--sensor-base-port", type=int, default=9001,
                    help="first port for standalone sensors (default 9001)")
    args = ap.parse_args()

    require_auth = tuple(args.auth.split(":", 1)) if args.auth else None
    servers = []
    for i in range(args.count):
        port = args.base_port + i
        mock = MockServer(i, args.host, port, args.error_rate, args.seed,
                          args.timeout_seconds, require_auth, args.quiet)
        httpd = ThreadingHTTPServer((args.host, port), make_handler(mock))
        httpd.daemon_threads = True
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        servers.append((mock, httpd))

    print("Redfish mockup: %d servers running\n" % len(servers))
    print("  %-12s %-34s %-20s %-12s %5s %4s %4s %4s %6s" %
          ("NAME", "URL", "VENDOR", "PERSONALITY", "ERR%", "CPU", "FAN", "PSU", "GPU"))
    for mock, _ in servers:
        print("  %-12s %-34s %-20s %-12s %5.1f %4d %4d %4d %6s" % (
            mock.name, "http://%s:%d/redfish/v1" % (args.host, mock.port),
            mock.manufacturer, mock.personality, mock.error_rate * 100,
            mock.cpu_count, mock.fan_count, mock.psu_count, "yes" if mock.has_gpu else "no"))
    print("\nTry: curl -s http://%s:%d/redfish/v1/Chassis/1/Thermal" % (args.host, args.base_port))

    probes, probe_httpds, probe_stop = [], [], None
    if args.sensors > 0:
        probes, probe_httpds, probe_stop = temp_sensors.start_probes(
            args.sensors, args.host, args.sensor_base_port, args.error_rate,
            timeout_seconds=args.timeout_seconds, seed=args.seed, quiet=args.quiet)
        print("\nStandalone temperature sensors (own API, not Redfish): %d running\n" % len(probes))
        temp_sensors.print_probe_table(probes, args.host)
        print("\nTry: curl -s http://%s:%d/api/v1/temperature" % (args.host, args.sensor_base_port))
    control = None
    if args.control_port:
        _, control = scenarios.start_control(
            [m for m, _ in servers], probes, args.host, args.control_port,
            {"redfish": error_catalog, "sensor": temp_sensors.error_catalog})
        print("\nScenario control API: http://%s:%d/api/scenarios" % (args.host, args.control_port))
        print("  errors: curl http://%s:%d/api/errors" % (args.host, args.control_port))
        print("  start: curl -X POST http://%s:%d/api/scenarios/rising-temperature/start"
              % (args.host, args.control_port))
    print("Press Ctrl+C to stop.\n", flush=True)

    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        print("\nShutting down...")
        for _, httpd in servers:
            httpd.shutdown()
            httpd.server_close()
        if probe_stop:
            temp_sensors.stop_probes(probe_httpds, probe_stop)
        if control:
            control.shutdown()
            control.server_close()


if __name__ == "__main__":
    main()
