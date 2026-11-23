from datetime import UTC, datetime, timedelta

from sqlmodel import Session, col, select

from db import SessionDep, engine, get_sensors_db, get_servers_db
from errors import DeviceError, DeviceOffline
from models import Sensor, Server, Temperature
from notifications import resolve_notification, send_notification
from redfish_api import get_server_health, get_server_temperature
from sensor_api import get_sensor_temp


def health_check_servers(session: SessionDep):
    faulty_servers = []

    for server in get_servers_db(session, 0, 100):
        try:
            health = get_server_health(server.ip)
            temp = get_server_temperature(server.ip)
        except DeviceError as error:
            server.health = "Unreachable"
            faulty_servers.append(server)
            session.add(server)
            continue

        server.health = health
        session.add(server)
        session.add(Temperature(server_serial=server.serial_number, value=temp))

        if health != "OK":
            faulty_servers.append(server)

    session.commit()
    return faulty_servers


def health_check_sensor(session: SessionDep):
    sensors_with_rising = []

    for sensor in get_sensors_db(session, 0, 100):
        try:
            temp = get_sensor_temp(sensor.ip)
        except DeviceOffline:
            temp = None
        except DeviceError as error:
            continue

        session.add(Temperature(sensor_id=sensor.sensor_id, value=temp))
        session.flush()

        rise = rise_temp(session, sensor)
        if rise is not None and rise > 3:
            sensors_with_rising.append(sensor)

    session.commit()
    return sensors_with_rising


def health_check_runner():
    print("running1")
    with Session(engine) as session:
        sensors = health_check_sensor(session)
        servers: list[Server] = health_check_servers(session)

        if len(sensors) > 0:
            air_flow_blocked(sensors)

        if servers is not None and len(servers) > 0:
            for server in servers:
                key = f"server_health:{server.name}:rack{server.rack}"
                send_notification(
                    key,
                    f"Server health is not OK on server {server.name} with serialnumber {server.serial_number} in rack {server.rack}",
                )


def rise_temp(session: Session, device: Server | Sensor, minutes: int = 1):
    time_span = datetime.now(UTC) - timedelta(minutes=minutes)

    match device:
        case Server():
            source = col(Temperature.server_serial) == device.serial_number
        case Sensor():
            source = col(Temperature.sensor_id) == device.sensor_id

    readings = session.exec(
        select(Temperature)
        .where(source)
        .where(col(Temperature.timestamp) >= time_span)
        .where(col(Temperature.value).is_not(None))
        .order_by(col(Temperature.timestamp))
    ).all()

    if len(readings) < 2:
        return None

    return readings[-1].value - readings[0].value


def air_flow_blocked(sensors: list[Sensor]):
    RISE_THRESHOLD = 3.0
    STABLE_THRESHOLD = 1.0

    posible_blockage = False
    with Session(engine) as session:
        for sensor in sensors:
            key=f"airflow:sensor{sensor.sensor_id}:rack{sensor.rack}"
            a_sensor_rise = rise_temp(session, sensor)
            if a_sensor_rise is None or a_sensor_rise < RISE_THRESHOLD:
                resolve_notification(key)
                continue

            sensors_above = session.exec(
                select(Sensor)
                .where(col(Sensor.rack) == sensor.rack)
                .where(col(Sensor.zone) > sensor.zone)
                .order_by(col(Sensor.zone))
            ).all()

            above_rises = [rise_temp(session, s) for s in sensors_above]
            above_rises = [r for r in above_rises if r is not None]

            posible_blockage = bool(above_rises) and all(
                b_sensor_rise < STABLE_THRESHOLD for b_sensor_rise in above_rises
            )


            if posible_blockage is True:
                send_notification(
                    key=key,
                    message=(
                        f"Possible airflow blockage in rack {sensor.rack}, zone {sensor.zone}: "
                        f"+{a_sensor_rise:.1f} °C while the zones above are stable"
                    ),
                )
            else:
                resolve_notification(key)
