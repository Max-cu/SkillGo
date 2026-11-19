"""Bounded resolver-report cache, scoped to exact base, requests and resolver code."""
import hashlib
import json
import logging
import os
import tempfile
import time
from pathlib import Path
from .dependency_download import wheel_urls

TTL = 86400
MAX_REPORT = 256 * 1024
MAX_ENTRIES = 256


def resolution_key(base, requests, resolver):
    data = json.dumps([1, base, sorted(requests), resolver], separators=(',', ':'))
    return hashlib.sha256(data.encode()).hexdigest()


def validate_report(report):
    if not isinstance(report, dict) or not isinstance(report.get('wheels'), list) or len(report['wheels']) > 128:
        raise ValueError('Invalid dependency resolution report')
    for wheel in report['wheels']:
        if not isinstance(wheel, dict):
            raise ValueError('Invalid dependency lock entry')
        digest = wheel.get('sha256', '')
        name = wheel.get('filename', '')
        if (not isinstance(digest, str) or len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest)
                or not isinstance(name, str) or not name.endswith('.whl') or '/' in name or '\\' in name
                or not isinstance(wheel.get('name'), str) or not isinstance(wheel.get('version'), str)):
            raise ValueError('Invalid dependency lock entry')
        wheel_urls(wheel.get('url', ''))
    return report


def read_resolution(cache, key):
    if cache is None:
        return None
    path = Path(cache) / 'resolutions' / key
    try:
        if time.time() - path.stat().st_mtime > TTL:
            return None
        with path.open('rb') as stream:
            raw = stream.read(MAX_REPORT + 1)
        if len(raw) > MAX_REPORT:
            return None
        return validate_report(json.loads(raw))
    except (OSError, ValueError, TypeError, KeyError):
        return None


def write_resolution(cache, key, report):
    validate_report(report)
    if cache is None:
        return
    data = json.dumps(report).encode()
    if len(data) > MAX_REPORT:
        return
    directory = Path(cache) / 'resolutions'
    temporary = None
    try:
        directory.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=directory, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
        temporary.replace(directory / key)
        entries = sorted((p for p in directory.iterdir() if len(p.name) == 64), key=lambda p: p.stat().st_mtime)
        for path in entries[:-MAX_ENTRIES]:
            path.unlink(missing_ok=True)
    except OSError:
        logging.getLogger(__name__).warning('Could not store dependency resolution cache')
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
