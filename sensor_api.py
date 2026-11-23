from datetime import UTC, datetime, timedelta
from time import time

import httpx

from errors import (
    BadResponse,
    ConnectionFailed,
    DeviceOffline,
    ServerError,
    Timeout,
    raise_for_status,
    with_retries,
)

MAX_READING_AGE = timedelta(seconds=60)

def _get_once(ip: str, path:str):
    try:
        resp = httpx.get(f"http://{ip}{path}", timeout=5)
    except httpx.TimeoutException as error:
        raise Timeout(path) from error
    except httpx.TransportError as error:
        raise ConnectionFailed(ip) from error

    try:
        body = resp.json()
    except ValueError as error:
        raise BadResponse(path) from error

    if resp.status_code == 502 and (body["error"] or {}["code"]) == "PROBE_OFFLINE":
        raise DeviceOffline(f"{ip}: probe offline")

    raise_for_status(resp.status_code, path, resp.text, resp.headers.get("Retry-After"))
    if not body.get("ok") or "data" not in body:
        raise ServerError(resp.status_code, resp.text)

    return body["data"]


def get_sensor_info(ip):
    return with_retries(lambda: _get_once(ip, "/api/v1/info"))


def get_sensor_temp(ip):
    data = with_retries(lambda: _get_once(ip, "/api/v1/temperature?unit=C"))

    return data.get("value")
