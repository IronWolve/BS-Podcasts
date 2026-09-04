"""One place for outbound HTTP policy: pool sizes, retries, and identity.

`requests` (and its urllib3 stack) is deliberately imported inside
`make_session`: it costs ~150 ms and nothing may touch the network before
the first window paints, so no startup path should pay for it.
"""

from .urlguard import UnsafeUrl, ensure_fetchable

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def _redirect_guard(response, *args, **kwargs):
    """Re-check every hop before it is followed.

    A feed-supplied URL can pass the first check and then 30x to loopback or a
    metadata endpoint, so validating only the URL we were handed protects
    nothing. Raised as `InvalidURL` (a `RequestException`) so the existing
    per-feed/per-item handlers treat it as an ordinary fetch failure instead
    of escaping as an unhandled error on a worker thread.
    """
    if not response.is_redirect:
        return response
    location = response.headers.get("Location")
    if location:
        from urllib.parse import urljoin
        from requests.exceptions import InvalidURL

        try:
            ensure_fetchable(urljoin(response.url, location), "Redirect target")
        except UnsafeUrl as exc:
            raise InvalidURL(str(exc), response=response) from exc
    return response


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
    session.hooks["response"].append(_redirect_guard)
    return session


def describe_network_error(exc, what: str = "the link") -> str:
    """A sentence a person can act on, instead of urllib3's pool dump.

    The raw text ("HTTPSConnectionPool(host=..., port=443): Max retries
    exceeded with url: ... (Caused by ProtocolError(...ConnectionResetError
    (10054 ...)))") was what the episode row showed — truncated — when one
    tracker hop in a nine-redirect enclosure chain reset the connection.
    Name the host, name the failure, keep the detail as a tail. `what` is
    the thing being fetched ("the feed", "the episode link", "the directory").
    Anything that is not a requests exception passes through unchanged.
    """
    import requests
    from urllib.parse import urlsplit

    request = getattr(exc, "request", None)
    host = urlsplit(getattr(request, "url", "") or "").hostname or ""
    where = f" by {host}" if host else ""
    text = str(exc)
    lowered = text.lower()
    if isinstance(exc, requests.exceptions.TooManyRedirects):
        return f"Too many redirects while following {what}{where}."
    if isinstance(exc, requests.exceptions.SSLError):
        return f"Secure connection failed{where}. ({text[:120]})"
    if isinstance(exc, requests.exceptions.ConnectTimeout) or "timed out" in lowered:
        return f"Connection timed out{where}. Retry in a moment."
    if isinstance(exc, requests.exceptions.ConnectionError):
        if "reset" in lowered or "10054" in lowered or "forcibly closed" in lowered:
            return f"Connection reset{where} while following {what}. Retry in a moment."
        if "name or service not known" in lowered or "getaddrinfo" in lowered or "11001" in lowered:
            return f"Could not resolve {host or 'the server'}. Check the network."
        return f"Could not connect{where}. ({text[:120]})"
    return text
