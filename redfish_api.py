import os
import time
from contextlib import suppress

import redfish
from redfish.rest.v1 import (
    InvalidCredentialsError,
    JsonDecodingError,
    RetriesExhaustedError,
    ServerDownOrUnreachableError,
)

from errors import (
    AuthFailed,
    BadResponse,
    ConnectionFailed,
    RedfishError,
    ServiceUnavailable,
    check_bmc,
    with_retries,
)

BMC_USER = os.getenv("BMC_USER", "admin")
BMC_PASSWORD = os.getenv("BMC_PASSWORD", "password")


def _client(server_ip):
    try:
        bmc_url = f"http://{server_ip}"
        client = redfish.redfish_client(
            base_url=bmc_url,
            username=BMC_USER,
            password=BMC_PASSWORD,
            default_prefix="/redfish/v1",
            timeout=5,
            max_retry=1,
        )

        client.login(auth="basic")

        return client

    except ServerDownOrUnreachableError as error:
        raise ServiceUnavailable(str(error)) from error
    except RetriesExhaustedError as error:
        raise ConnectionFailed(server_ip) from error
    except InvalidCredentialsError as error:
        raise AuthFailed(str(error)) from error
    except JsonDecodingError as error:
        raise BadResponse(server_ip) from error


def _get_once(server_ip: str, path: str):
    client = _client(server_ip)
    try:
        try:
            resp = client.get(path)
        except RetriesExhaustedError as error:
            raise ConnectionFailed(server_ip) from error
        return check_bmc(resp, path)
    finally:
        with suppress(Exception):
            client.logout()


def get_server_data(server_ip: str, path: str, retires: int = 3):
    return with_retries(lambda: _get_once(server_ip, path), retires)


def get_server_temperature(server_ip: str):
    path = "/redfish/v1/Chassis/1/Thermal"
    data = get_server_data(server_ip, path)
    for temp in data["Temperatures"]:
        if temp["PhysicalContext"] == "Intake":
            return temp["ReadingCelsius"]
    return None


def get_server_health(server_ip):
    path = "/redfish/v1/Systems/1"
    data = get_server_data(server_ip, path)
    status = data["Status"]["HealthRollup"]
    return status
