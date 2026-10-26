"""Recognize temporary builder failures, including historical failure messages."""
from requests.exceptions import ConnectionError, SSLError, Timeout
from docker.errors import APIError


def transient_build_failure(error: BaseException | str) -> bool:
    if isinstance(error, (ValueError, SSLError)):
        return False
    if isinstance(error, (ConnectionError, Timeout, TimeoutError)):
        return True
    if isinstance(error, APIError):
        status = getattr(error.response, 'status_code', None)
        if status in {500, 502, 503, 504}:
            return True
        if status != 404:
            return False
        # A missing base image needs administrator action; only disappeared
        # build/probe containers are eligible for an automatic retry.
        return any(marker in str(error).lower() for marker in (
            'no such container', 'container is not running',
        ))
    message = str(error).lower()
    return any(marker in message for marker in (
        'no such container', 'container is not running',
        'context deadline exceeded', 'exceeded its total budget',
        'connection aborted', 'connection reset', 'read timed out',
        '500 server error', '502 bad gateway', '503 service unavailable',
        '504 gateway timeout', 'error while fetching server api version',
        'environment builder lease expired',
    ))
