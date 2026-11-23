import time
from collections.abc import Callable

from redfish.rest.v1 import JsonDecodingError


class DeviceError(Exception):
    """Base device error class for every error from bmc or sensor."""
    retryable = False
    retry_after: float | None = None


RedfishError = DeviceError

class ConnectionFailed(DeviceError):
    retryable = True
    def __init__(self, url: str):
        super().__init__(f"Could not connect to {url}")
        self.url = url

class AuthFailed(DeviceError):
    pass

class NotFound(DeviceError):
    def __init__(self, path: str):
        super().__init__(f"Not found: {path}")
        self.path = path


class Timeout(DeviceError):
    retryable = True

class ServiceUnavailable(DeviceError):
    retryable = True

    def __init__(self, message: str = "", retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after

class BadResponse(DeviceError):
    retryable = True

class DeviceOffline(DeviceError):
    """The device sends respond."""

class ServerError(DeviceError):
    retryable = True
    def __init__(self, status: int, body: str):
        super().__init__(f"Device truned HTTP {status}")
        self.status = status
        self.body = body

def raise_for_status(status: int, path: str = "", body: str = "", retry_after: str | None = None):
    match status:
        case 200:
            return
        case 401 | 403:
            raise AuthFailed(f"Access Denied: {path}")
        case 404:
            raise NotFound(path)
        case 429 | 503:
            raise ServiceUnavailable(path, float(retry_after) if retry_after else None)
        case 504:
            raise Timeout(path)
        case _:
            raise ServerError(status, body)

def check_bmc(resp, path: str):
    raise_for_status(resp.status, path, resp.text, resp.getheader("Retry-After"))
    try:
        return resp.dict
    except (ValueError, JsonDecodingError) as error:
        raise BadResponse(path) from error

def with_retries[T](fn: Callable[[], T], retries: int = 3) -> T:
    for attempt in range(retries):
        try:
            return fn()
        except DeviceError as error:
            if not error.retryable or attempt == retries - 1:
                raise
            time.sleep(error.retry_after or 0.5 * 2**attempt)
    raise AssertionError("unreachable")
