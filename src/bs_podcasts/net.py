"""One place for outbound HTTP policy: pool sizes, retries, and identity.

`requests` (and its urllib3 stack) is deliberately imported inside
`make_session`: it costs ~150 ms and nothing may touch the network before
the first window paints, so no startup path should pay for it.
"""

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


class SessionSlot:
    """Class attribute providing the shared lazy-Session contract.

    The Session is created on first use (constructors stay network-free);
    assigning to the attribute — a constructor's `session=` injection or a
    test double — replaces it. Construction kwargs are per-consumer policy.
    """

    def __init__(self, **session_kwargs):
        self._kwargs = session_kwargs

    def __set_name__(self, owner, name):
        self._attr = "_" + name

    def __get__(self, instance, owner=None):
        if instance is None:
            return self
        current = getattr(instance, self._attr, None)
        if current is None:
            current = make_session(**self._kwargs)
            setattr(instance, self._attr, current)
        return current

    def __set__(self, instance, value):
        setattr(instance, self._attr, value)


def make_session(pool: int = 8, retries: int = 2, backoff: float = 0.5, read_retries: bool = True, max_redirects: int = 8):
    """A Session safe to share across the job pool.

    `retries` covers connection errors and 429/5xx responses with backoff;
    long streaming transfers pass `read_retries=False` so a mid-body failure
    is reported (and resumed by the caller) rather than replayed from zero.
    """
    from requests import Session
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry

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
    session.max_redirects = max_redirects
    return session
