"""Trusted public wheel sources; injected into isolated build containers."""
import hashlib
import urllib.request
import urllib.parse
import urllib.error
import sys
import time

MIRROR_INDEX = 'https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple'
ALLOWED_HOSTS = {'pypi.org', 'files.pythonhosted.org', 'mirrors.tuna.tsinghua.edu.cn', 'pypi.tuna.tsinghua.edu.cn'}
MAX_BYTES = 128 * 1024 * 1024

def validate_url(url):
    u = urllib.parse.urlsplit(url)
    if (u.scheme != 'https' or u.hostname not in ALLOWED_HOSTS or u.username or u.password
            or u.port not in (None, 443) or u.query or u.fragment):
        raise ValueError('Untrusted dependency source')
    if u.hostname.startswith('mirrors.') and not u.path.startswith('/pypi/web/'):
        raise ValueError('Untrusted mirror path')
    return u

class CheckedRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)

def wheel_urls(url):
    u = validate_url(url)
    path = u.path.removeprefix('/pypi/web')
    if not path.startswith('/packages/') or '..' in path.split('/'):
        raise ValueError('Invalid wheel source path')
    return ['https://mirrors.tuna.tsinghua.edu.cn/pypi/web' + path,
            'https://files.pythonhosted.org' + path]

def fetch_wheel(wheel, opener=None):
    name = wheel['filename']; digest = wheel['sha256']
    if '/' in name or '\\' in name or not name.endswith('.whl'):
        raise ValueError('Invalid wheel filename')
    if len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
        raise ValueError('Invalid wheel digest')
    opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}), CheckedRedirect())
    integrity_failed = False
    for attempt in range(2):
        for url in wheel_urls(wheel['url']):
            started = time.monotonic()
            host = urllib.parse.urlsplit(url).hostname
            print('Wheel download started:', name, 'source='+host, file=sys.stderr, flush=True)
            try:
                with opener.open(url, timeout=60) as response:
                    data = response.read(MAX_BYTES + 1)
            except (OSError, urllib.error.URLError) as exc:
                print('Wheel download retry:', name, 'source='+host,
                      'error='+type(exc).__name__, file=sys.stderr, flush=True)
                # Certificate failures are not repaired by disabling TLS.
                import ssl
                if isinstance(getattr(exc, 'reason', exc), ssl.SSLCertVerificationError):
                    raise ValueError('Dependency TLS verification failed') from exc
                continue
            if len(data) > MAX_BYTES:
                raise ValueError('Wheel exceeds size limit: ' + name)
            if hashlib.sha256(data).hexdigest() != digest:
                if urllib.parse.urlsplit(url).hostname != 'files.pythonhosted.org':
                    integrity_failed = True
                    continue  # Discard mirror bytes; try the same hash at origin.
                raise ValueError('Wheel SHA256 mismatch: ' + name)
            print('Wheel download completed:', name, 'bytes='+str(len(data)),
                  'seconds='+str(round(time.monotonic()-started, 1)), file=sys.stderr, flush=True)
            return data
    if integrity_failed:
        raise ValueError('Wheel SHA256 mismatch: ' + name)
    raise RuntimeError('Dependency download timed out or unavailable after mirror/origin retries: ' + name)
