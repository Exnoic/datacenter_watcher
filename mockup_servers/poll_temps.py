#!/usr/bin/env python3
"""
Poll the Thermal resource of every Redfish mockup server, plus the standalone
temperature sensors, and print summary tables.

    python3 poll_temps.py                 # poll ports 8001-8010 once
    python3 poll_temps.py --watch 5       # repeat every 5 seconds
"""

import argparse
import json
import socket
import time
import urllib.error
import urllib.request


def fetch_thermal(host, port, timeout):
    return fetch_json("http://%s:%d/redfish/v1/Chassis/1/Thermal" % (host, port), timeout)


def fetch_json(url, timeout):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read()), None
    except urllib.error.HTTPError as e:
        retry = e.headers.get("Retry-After")
        code = ""
        try:  # standalone sensors put an error code in the body
            code = " " + json.loads(e.read())["error"]["code"]
        except Exception:
            pass
        return None, "HTTP %d%s%s" % (e.code, code, " (retry %ss)" % retry if retry else "")
    except (socket.timeout, TimeoutError):
        return None, "timeout"
    except urllib.error.URLError as e:
        return None, "conn error: %s" % e.reason
    except (ConnectionError, http_client_errors()) as e:
        return None, "dropped: %s" % type(e).__name__
    except ValueError:
        return None, "malformed JSON"


def http_client_errors():
    import http.client
    return http.client.HTTPException


def summarize(thermal):
    temps = thermal.get("Temperatures", [])
    fans = thermal.get("Fans", [])

    def pick(name_part):
        for t in temps:
            if name_part in t["Name"]:
                return t
        return None

    def fmt(t):
        if t is None:
            return "-"
        v = t.get("ReadingCelsius")
        return "n/a" if v is None else "%.1f" % v

    cpu = [t for t in temps if t.get("PhysicalContext") == "CPU"]
    cpu_max = max((t["ReadingCelsius"] for t in cpu if t["ReadingCelsius"] is not None),
                  default=None)
    fan_vals = [f["Reading"] for f in fans if f.get("Reading") is not None]
    bad = []
    for t in temps + fans:
        st = t["Status"]
        if st["Health"] == "OK":
            continue
        if st["State"] != "Enabled":
            bad.append("%s OFFLINE" % t["Name"])
        else:
            v = t.get("ReadingCelsius", t.get("Reading"))
            bad.append("%s=%s %s" % (t["Name"], v, st["Health"].upper()))
    return {
        "inlet": fmt(pick("Inlet")),
        "exhaust": fmt(pick("Exhaust")),
        "cpu_max": "-" if cpu_max is None else "%.1f" % cpu_max,
        "fan_min": "-" if not fan_vals else str(min(fan_vals)),
        "health": thermal.get("Status", {}).get("HealthRollup", "?"),
        "alerts": ", ".join(bad),
    }


def poll(args):
    print("%-6s %8s %8s %8s %8s %-9s %s" %
          ("PORT", "INLET", "EXHAUST", "CPU MAX", "FAN MIN", "HEALTH", "ALERTS / ERROR"))
    for i in range(args.count):
        port = args.base_port + i
        thermal, err = fetch_thermal(args.host, port, args.timeout)
        if err:
            print("%-6d %8s %8s %8s %8s %-9s %s" % (port, "", "", "", "", "ERROR", err))
            continue
        s = summarize(thermal)
        print("%-6d %8s %8s %8s %8s %-9s %s" % (port, s["inlet"], s["exhaust"], s["cpu_max"],
                                                s["fan_min"], s["health"], s["alerts"]))


def poll_sensors(args):
    print("%-6s %-12s %8s %7s %-8s %-9s %s" %
          ("PORT", "SENSOR", "TEMP C", "RH %", "LEVEL", "HEALTH", "TIMESTAMP / ALARMS / ERROR"))
    for i in range(args.sensors):
        port = args.sensor_base_port + i
        base = "http://%s:%d/api/v1" % (args.host, port)
        reading, err = fetch_json(base + "/temperature", args.timeout)
        status, _ = fetch_json(base + "/status", args.timeout)
        st = status["data"] if status else {}
        alarms = ", ".join(a["code"] for a in st.get("alarms", []))
        health = st.get("health", "?")
        sid = st.get("sensor_id", "?")
        if err:
            print("%-6d %-12s %8s %7s %-8s %-9s %s" % (port, sid, "", "", "ERROR", health,
                                                       err + ("  [%s]" % alarms if alarms else "")))
            continue
        d = reading["data"]
        print("%-6d %-12s %8.2f %7s %-8s %-9s %s %s" % (
            port, d["sensor_id"], d["value"], d["humidity_pct"], d["level"], health,
            d["timestamp"], alarms))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--base-port", type=int, default=8001)
    ap.add_argument("--count", type=int, default=10)
    ap.add_argument("--timeout", type=float, default=8)
    ap.add_argument("--sensor-base-port", type=int, default=9001)
    ap.add_argument("--sensors", type=int, default=6, help="standalone sensors to poll (0 = skip)")
    ap.add_argument("--watch", type=float, metavar="SECONDS", help="repeat every N seconds")
    args = ap.parse_args()
    while True:
        if args.count:
            poll(args)
        if args.sensors:
            print()
            poll_sensors(args)
        if not args.watch:
            break
        print()
        time.sleep(args.watch)


if __name__ == "__main__":
    main()
