import hashlib,io,ssl
import pytest
from urllib.error import URLError
from app.dependency_download import fetch_wheel,validate_url,wheel_urls,CheckedRedirect
from app.environment_failures import transient_build_failure

DATA=b'wheel fixture'
WHEEL={'filename':'fixture.whl','url':'https://files.pythonhosted.org/packages/aa/bb/fixture.whl','sha256':hashlib.sha256(DATA).hexdigest()}
class Opener:
 def __init__(self,results):self.results=iter(results);self.urls=[]
 def open(self,url,timeout):
  self.urls.append(url)
  r=next(self.results)
  if isinstance(r,Exception):raise r
  return io.BytesIO(r)

def test_mirror_first_and_origin_fallback_preserves_hash():
 o=Opener([TimeoutError(),DATA]);assert fetch_wheel(WHEEL,o)==DATA
 assert 'mirrors.tuna' in o.urls[0] and 'files.pythonhosted.org' in o.urls[1]

def test_download_recovers_on_second_bounded_attempt():
 o=Opener([TimeoutError(),TimeoutError(),DATA]);assert fetch_wheel(WHEEL,o)==DATA;assert len(o.urls)==3

def test_exhausted_download_is_retryable_and_names_package():
 o=Opener([TimeoutError()]*4)
 with pytest.raises(RuntimeError,match='fixture.whl') as e:fetch_wheel(WHEEL,o)
 assert len(o.urls)==4 and transient_build_failure(e.value)

def test_bad_digest_never_retries_or_installs():
 o=Opener([b'corrupt',b'corrupt'])
 with pytest.raises(ValueError,match='SHA256'):fetch_wheel(WHEEL,o)
 assert len(o.urls)==2

def test_bad_mirror_is_discarded_and_verified_origin_used():
 o=Opener([b'corrupt',DATA]);assert fetch_wheel(WHEEL,o)==DATA;assert len(o.urls)==2

def test_tls_verification_failure_never_downgrades():
 o=Opener([URLError(ssl.SSLCertVerificationError('certificate invalid'))])
 with pytest.raises(ValueError,match='TLS'):fetch_wheel(WHEEL,o)
 assert len(o.urls)==1

@pytest.mark.parametrize('url',['http://files.pythonhosted.org/packages/a','https://evil.example/packages/a','https://user:pass@files.pythonhosted.org/packages/a','https://files.pythonhosted.org:8443/packages/a','https://mirrors.tuna.tsinghua.edu.cn/other/a','https://files.pythonhosted.org/packages/a?token=x'])
def test_untrusted_sources_rejected(url):
 with pytest.raises(ValueError):validate_url(url)

def test_redirect_to_private_host_rejected():
 with pytest.raises(ValueError):CheckedRedirect().redirect_request(None,None,302,'',{},'https://127.0.0.1/a')

def test_historical_ssl_read_timeout_recognized():
 assert transient_build_failure('TimeoutError: The read operation timed out')
