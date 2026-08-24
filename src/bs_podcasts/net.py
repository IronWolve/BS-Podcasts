"""One place for outbound HTTP policy: pool sizes, retries, and identity."""

from requests import Session
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def make_session(pool: int = 8, retries: int = 2, backoff: float = 0.5, read_retries: bool = True) -> Session:
    """A Session safe to share across the job pool.

    `retries` covers connection errors and 429/5xx responses with backoff;
    long streaming transfers pass `read_retries=False` so a mid-body failure
    is reported (and resumed by the caller) rather than replayed from zero.
    """
    session = Session()
    session.headers["User-Agent"] = USER_AGENT
    retry = Retry(
        total=retries,
        connect=retries,
        read=retries if read_retries else 0,
        status=retries,
        backoff_factor=backoff,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET", "HEAD"}),
        respect_retry_after_header=True,
        raise_on_status=False,
    )
    adapter = HTTPAdapter(pool_connections=pool, pool_maxsize=pool, max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.max_redirects = 8
    return session
