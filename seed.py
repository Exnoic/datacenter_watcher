"""Lägger in mock-servrarna i databasen. Kör: uv run seed.py"""

import time
from datetime import datetime, timezone

import httpx
from sqlmodel import Session

from db import create_db_and_tables, engine
from models import Sensor, Server

AUTH = ("admin", "password")

# (ip:port, rack, position i racket)
RACK = 1
SERVERS = [(f"127.0.0.1:{8001 + i}", RACK, i) for i in range(10)]


def fetch_system(ip: str, retries: int = 3) -> dict | None:
    for attempt in range(retries):
        try:
            resp = httpx.get(f"http://{ip}/redfish/v1/Systems/1", auth=AUTH, timeout=5)
            if resp.status_code == 200:
                return resp.json()
            print(f"  {ip}: HTTP {resp.status_code}, försöker igen")
        except (httpx.HTTPError, ValueError) as e:  # ValueError = trasig JSON
            print(f"  {ip}: {type(e).__name__}, försöker igen")
        time.sleep(0.5 * 2**attempt)
    return None

# (ip:port, zon) – starta mocken med --sensor-base-port 8081
# (ip:port, rack, zone). Start the mock with --sensor-base-port 8081
SENSORS = [
    ("127.0.0.1:8081", 1, 1),
    ("127.0.0.1:8082", 1, 2),
    ("127.0.0.1:8083", 1, 3),
]


def fetch_sensor(ip: str, path: str, retries: int = 3) -> dict | None:
    for attempt in range(retries):
        try:
            resp = httpx.get(f"http://{ip}{path}", timeout=5)
            body = resp.json()
            if resp.status_code == 200 and body.get("ok"):
                return body["data"]
            print(f"  {ip}{path}: HTTP {resp.status_code}, retrying")
        except (httpx.HTTPError, ValueError) as e:  # ValueError = broken JSON
            print(f"  {ip}{path}: {type(e).__name__}, retrying")
        time.sleep(0.5 * 2**attempt)
    return None


def seed_sensors(session: Session):
    for ip, rack, zone in SENSORS:
        info = fetch_sensor(ip, "/api/v1/info")
        if info is None:
            print(f"✗ {ip}: not responding, skipping (run the script again)")
            continue

        reading = fetch_sensor(ip, "/api/v1/temperature?unit=C")  # may be None if offline

        fields = {
            "sensor_id": info["sensor_id"],
            "ip": ip,
            "location": info.get("location", ""),
            "rack": rack,
            "temp": reading.get("value") if reading else None,
            "serial": info.get("serial", ""),
            "firmware": info.get("firmware", ""),
            "zone": zone,
            "position": zone,
        }
        sensor = session.get(Sensor, fields["sensor_id"])
        if sensor is None:
            sensor = Sensor(**fields)
        else:
            sensor.sqlmodel_update(fields)

        session.add(sensor)
        print(f"✓ {ip}: {fields['sensor_id']}, rack {rack}, {fields['temp']} °C, position {fields['position']}")

def main():
    create_db_and_tables()
    with Session(engine) as session:
        for ip, rack, position in SERVERS:
            data = fetch_system(ip)
            if data is None:
                print(f"✗ {ip}: svarar inte, hoppar över (kör skriptet igen)")
                continue

            serial = data["SerialNumber"]
            memory_gib = (data.get("MemorySummary") or {}).get("TotalSystemMemoryGiB")

            server = session.get(Server, serial) or Server(serial_number=serial)
            server.name = data.get("Name", "")
            server.ip = ip
            server.model = data.get("Model", "")
            server.rack = rack
            server.position = position
            server.health = (data.get("Status") or {}).get("HealthRollup", "Unknown")
            server.bios_version = data.get("BiosVersion", "")
            server.manufacturer = data.get("Manufacturer", "")
            server.power_state = data.get("PowerState", "")
            server.processor_model = (data.get("ProcessorSummary") or {}).get("Model", "")
            server.memory = f"{memory_gib} GiB" if memory_gib else ""

            session.add(server)
            print(f"✓ {ip}: {server.name} ({serial}), rack {rack} pos {position}")
        seed_sensors(session)
        session.commit()


if __name__ == "__main__":
    main()
