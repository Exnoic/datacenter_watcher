from typing import Annotated

from fastapi import Depends, Query
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine, select

from models import Sensor, Server

sqlite_file = "database.db"
sqlite_url = f"sqlite:///{sqlite_file}"

connect_args = {"check_same_thread": False}
engine = create_engine(sqlite_url, connect_args=connect_args)


def create_db_and_tables():
    SQLModel.metadata.create_all(engine)


def get_session():
    with Session(engine) as session:
        yield session


SessionDep = Annotated[Session, Depends(get_session)]

@event.listens_for(engine, "connect")
def _enable_foreign_keys(dbapi_connection, _):
    dbapi_connection.execute("PRAGMA foreign_keys=ON")


def add_server_db(server: Server, session: SessionDep):
    session.add(server)
    session.commit()
    session.refresh(server)
    return server


def delete_server(server: Server, session: SessionDep):
    session.delete(server)
    session.commit()

    return {"ok": True}


def get_server_db_data(server_id: str, session: SessionDep):
    server = session.get(Server, server_id)
    return server


def get_servers_db(
    session: SessionDep,
    offset: int = 0,
    limit: Annotated[int, Query(le=100)] = 100,
):
    servers = session.exec(select(Server).offset(offset).limit(limit)).all()
    return servers

def add_sensor_db(server: Sensor, session: SessionDep):
    session.add(server)
    session.commit()
    session.refresh(server)
    return server


def delete_sensor_db(server: Sensor, session: SessionDep):
    session.delete(server)
    session.commit()

    return {"ok": True}


def get_sensor_db(sensor_id: str, session: SessionDep):
    sensor = session.get(Sensor, sensor_id)
    return sensor


def get_sensors_db(
    session: SessionDep,
    offset: int = 0,
    limit: Annotated[int, Query(le=100)] = 100,
):
    servers = session.exec(select(Sensor).offset(offset).limit(limit)).all()
    return servers

def zone_creator_db(session, rack):
    servers = session.exec(
        select(Server).where(Server.rack == rack).order_by(Server.position)).all()
    sensors = session.exec(
        select(Sensor).where(Sensor.rack == rack).order_by(Sensor.position)).all()

    if not sensors:
        return

    per_zone = len(servers) // len(sensors)

    for zone, sensor in enumerate(sensors, start=1):
        start = (zone - 1) * per_zone
        is_last = zone == len(sensors)
        end = None if is_last else start + per_zone

        for server in servers[start:end]:
            server.zone = zone
            session.add(server)

        sensor.zone = zone
        session.add(sensor)
    session.commit()

    return "Sucessful"
