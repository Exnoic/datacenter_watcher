from contextlib import asynccontextmanager
from typing import Annotated

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlmodel import Session, col, select

import db
from notifications import resolve_notification
import redfish_api
import sensor_api
from db import create_db_and_tables, get_sensors_db, get_server_db_data, get_servers_db, get_session
from errors import AuthFailed, ConnectionFailed, NotFound, RedfishError, Timeout
from health_check import health_check_runner, health_check_servers
from models import Notification

STATUS = {NotFound: 404, AuthFailed: 502, Timeout: 504, ConnectionFailed: 503}
SessionDep = Annotated[Session, Depends(get_session)]


@asynccontextmanager
async def lifespan(app: FastAPI):
    create_db_and_tables()
    scheduler = BackgroundScheduler()
    scheduler.add_job(
        health_check_runner, "interval", seconds=30, max_instances=1, coalesce=True
    )
    scheduler.start()
    yield
    scheduler.shutdown()


app = FastAPI(lifespan=lifespan)
api = APIRouter(prefix="/api")

@app.post("/rack/create_zone/{rack}")
async def create_zone(rack: str, session: SessionDep):
    data = db.zone_creator_db(session, rack)
    return data

@api.get("/servers")
def get_all_servers(session: SessionDep):
    data = get_servers_db(session, 0, 100)
    return data

@api.get("/sensors")
def get_all_sensors(sesion: SessionDep):
    data = get_sensors_db(sesion, 0, 100)
    return data

@api.get("/servers/{server_id}")
def get_server_db(session: SessionDep, server_id: str):
    data = get_server_db_data(server_id, session)
    return data

@api.get("/notifications")
def active_alerts(session: SessionDep):
    return session.exec(
        select(Notification)
        .where(col(Notification.resolved_at).is_(None))
        .order_by(col(Notification.created_at).desc())
    ).all()

@api.put("/notification/{key}/resolve")
def resolve_notify(key: str, session: SessionDep):
    response = resolve_notification(key=key)

    return response



@app.exception_handler(RedfishError)
def redfish_error_handler(request: Request, exc: RedfishError):
    status = STATUS.get(type(exc), 502)
    return JSONResponse(
        status_code=status, content={"error": type(exc).__name__, "detail": str(exc)}
    )

app.include_router(api)
app.mount("/", StaticFiles(directory="dashboard/dist", html=True), name="dashboard")
