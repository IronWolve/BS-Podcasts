"""One place for outbound HTTP policy: pool sizes, retries, and identity.

`requests` (and its urllib3 stack) is deliberately imported inside
`make_session`: it costs ~150 ms and nothing may touch the network before
the first window paints, so no startup path should pay for it.
"""

from .urlguard import UnsafeUrl, _blocked_reason, ensure_web_url
import threading

_request_context = threading.local()
# Cooldowns are kept per request class: (host, foreground). A pause asked for
# by a background fetch never refuses a user's Play; playback's own pause
# still holds back background work on that host.
_rate_limited_until = {}
_rate_limit_lock = threading.Lock()
_RATE_LIMIT_CAP = 6 * 60 * 60


def _rate_limit_message(host: str, seconds: float = 0.0) -> str:
    if seconds >= 90:
        wait = f"Retry in about {int(seconds / 60 + .5)} min."
    elif seconds >= 1:
        wait = f"Retry in about {int(seconds + .5)} s."
    else:
        wait = "Retry in a moment."
    return f"Rate limited by {host or 'the server'}. {wait}"


def _rate_limit_error(host: str, seconds: float = 0.0):
    """The refusal for a host that asked for a pause; `retry_after` says how long."""
    from requests.exceptions import RetryError
    exc = RetryError(_rate_limit_message(host, seconds))
    exc.retry_after = seconds
    return exc


def _remember_rate_limit(host: str, seconds: float, foreground: bool = False) -> None:
    if not host or seconds <= 0:
        return
    import time
    seconds = min(float(seconds), _RATE_LIMIT_CAP)
    until = time.monotonic() + seconds
    key = (host, bool(foreground))
    with _rate_limit_lock:
        now = time.monotonic()
        expired = [name for name, value in _rate_limited_until.items() if value <= now]
        for name in expired:
            del _rate_limited_until[name]
        if until > _rate_limited_until.get(key, 0.0):
            _rate_limited_until[key] = until


def _rate_limit_remaining(host: str, foreground: bool = False) -> float:
    """Seconds left on a host's cooldown. Playback only waits for its own
    pauses; background work also honours the ones playback was given."""
    if not host:
        return 0.0
    import time
    with _rate_limit_lock:
        until = _rate_limited_until.get((host, True), 0.0)
        if not foreground:
            until = max(until, _rate_limited_until.get((host, False), 0.0))
    return max(0.0, until - time.monotonic())


def _refuse_while_rate_limited(url, foreground: bool) -> None:
    host = _request_host(url)
    left = _rate_limit_remaining(host, foreground)
    if left > 0:
        raise _rate_limit_error(host, left)


def _retry_after_seconds(response) -> float | None:
    raw = response.headers.get("Retry-After") if response is not None else None
    if raw is None:
        return None
    text = str(raw).strip()
    if text.isascii() and text.isdigit():
        try:
            return min(float(text), _RATE_LIMIT_CAP)
        except (ValueError, OverflowError):
            return None
    import email.utils
    import time
    try:
        parsed = email.utils.parsedate_tz(text)
        if parsed is None:
            return None
        return max(0.0, min(_RATE_LIMIT_CAP, email.utils.mktime_tz(parsed) - time.time()))
    except (ValueError, OverflowError, TypeError):
        return None


def _request_host(url) -> str:
    from urllib.parse import urlsplit
    try:
        return (urlsplit(str(url)).hostname or "").lower()
    except ValueError:
        return ""


def _fail_long_retry_after(response, state, request_host: str) -> None:
    """A Retry-After past the deadline is a rate limit, not a timeout."""
    if getattr(response, "status_code", None) not in (429, 500, 502, 503, 504):
        return
    seconds = _retry_after_seconds(response)
    if seconds is None:
        return
    response_host = _request_host(getattr(response, "url", "") or "")
    # Short waits also apply to subsequent requests for the same host. Never
    # sleep/drain the error body here; callers can retry after the cooldown.
    # Only the host that answered is cooled (not the first hop of a redirect
    # chain that many shows share), and only for this request class.
    _remember_rate_limit(response_host or request_host, seconds, getattr(state, "foreground", False))
    from .netlimits import NetworkDeadline
    try:
        left = state.remaining()
    except NetworkDeadline:
        left = 0
    if seconds <= left:
        return
    from .netlimits import abort_response
    abort_response(response)
    raise _rate_limit_error(response_host or request_host, seconds)

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
        from urllib.parse import urljoin, urlsplit
        from requests.exceptions import InvalidURL

        try:
            target = urljoin(response.url, location)
            if urlsplit(response.url).scheme.lower() == 'https' and urlsplit(target).scheme.lower() == 'http':
                raise UnsafeUrl('A secure URL redirected to insecure HTTP; use an HTTPS endpoint.')
            # The connection pins and screens the address it actually uses.
            # Resolving here looked up a different name for some international hosts.
            checked = ensure_web_url(target, "Redirect target")
            host = urlsplit(checked).hostname or ""
            import ipaddress
            try:
                literal = ipaddress.ip_address(host)
            except ValueError:
                literal = None
            if literal is not None:
                reason = _blocked_reason(literal)
                if reason:
                    raise UnsafeUrl(f"Redirect target resolves to {reason}: {host}")
        except UnsafeUrl as exc:
            response.close()
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


def make_session(pool: int = 8, retries: int = 2, backoff: float = 0.5, read_retries: bool = True, max_redirects: int = 8, total_timeout: float = 60, max_response_bytes=20 * 1024 * 1024, foreground=False):
    """A Session safe to share across the job pool.

    `retries` covers connection errors and optional read retries, not status
    answers. 429/5xx fail without draining/retrying their bodies; Retry-After
    sets a bounded host cooldown. Long streaming transfers pass
    `read_retries=False` so a mid-body failure is reported for caller resume.
    """
    from requests import Session
    from .safe_transport import guarded_adapter
    from urllib3.util.retry import Retry

    from requests.exceptions import RequestException, Timeout
    from .netlimits import Deadline, NetworkDeadline, ResponseTooLarge, bounded_call, bounded_chunks, abort_response, is_foreground, request_scope

    class DeadlineRetry(Retry):
        def increment(self, *args, **kwargs):
            state = getattr(_request_context, "state", None)
            if state is not None:
                state.remaining()
            return super().increment(*args, **kwargs)

        def sleep(self, response=None):
            state = getattr(_request_context, "state", None)
            if state is None:
                return super().sleep(response)
            seconds = self.get_retry_after(response) if response is not None and self.respect_retry_after_header else None
            seconds = seconds or self.get_backoff_time()
            while seconds > 0:
                interval = min(.05, seconds, state.remaining())
                state.cancelled.wait(interval)
                seconds -= interval

    class BoundedSession(Session):
        supports_deadlines = True

        def request(self, method, url, **kwargs):
            streaming = kwargs.pop("stream", False)
            budget = float(kwargs.pop("total_timeout", total_timeout))
            state = Deadline(min(budget, 45), kwargs.pop("cancel_event", None), foreground=foreground or is_foreground())
            host = _request_host(url)
            _refuse_while_rate_limited(url, state.foreground)
            def work():
                _request_context.state = state
                try:
                    # Redirect screening runs here too, on the header worker;
                    # inherit priority, cancellation and the remaining budget.
                    with request_scope(state.cancelled, state.foreground, state):
                        return super(BoundedSession, self).request(method, url, stream=True, **kwargs)
                finally:
                    _request_context.state = None
            try:
                response = bounded_call(work, state, dispose=abort_response)
                state.expires = state.started + budget
                _fail_long_retry_after(response, state, host)
                if not streaming:
                    response.content
                return response
            except NetworkDeadline as exc:
                raise Timeout(str(exc)) from exc

        def send(self, request, **kwargs):
            state = getattr(_request_context, "state", None)
            if state is not None:
                remaining = state.remaining()
                # Each redirect hop passes through here; a hop whose host is
                # cooling down is refused without sending anything to it.
                _refuse_while_rate_limited(request.url, state.foreground)
                timeout = kwargs.get("timeout") or (8, 20)
                kwargs["timeout"] = tuple(min(float(value or remaining), remaining) for value in timeout) if isinstance(timeout, tuple) else min(float(timeout), remaining)
            return super().send(request, **kwargs)

    def guard_body(response, *args, **kwargs):
        state = getattr(_request_context, "state", None)
        if state is None:
            return response
        state.remaining()
        original = response.iter_content
        def chunks(chunk_size=1, decode_unicode=False):
            if response._content is not False:
                yield from original(chunk_size, decode_unicode=decode_unicode)
                return
            try:
                yield from bounded_chunks(response, original, state, max_response_bytes, chunk_size, decode_unicode)
            except ResponseTooLarge as exc:
                raise RequestException(str(exc), response=response) from exc
            except NetworkDeadline as exc:
                raise Timeout(str(exc), response=response) from exc
        response.iter_content = chunks
        return response

    session = BoundedSession()
    # Untrusted feed URLs must never select credentials from the machine's
    # .netrc, or silently route through process-wide proxy/CA configuration.
    session.trust_env = False
    session.headers["User-Agent"] = USER_AGENT
    from http.cookiejar import DefaultCookiePolicy

    class _NoCookies(DefaultCookiePolicy):
        def set_ok(self, cookie, request):
            return False

        def return_ok(self, cookie, request):
            return False

        def domain_return_ok(self, domain, request):
            return False

        def path_return_ok(self, path, request):
            return False

    session.cookies.set_policy(_NoCookies())
    # Status retries drain the error body with no byte cap. Fail the response
    # and let the caller try again instead of holding this worker.
    retry = DeadlineRetry(
        total=retries,
        connect=retries,
        read=retries if read_retries else 0,
        status=0,
        backoff_factor=backoff,
        status_forcelist=(),
        allowed_methods=frozenset({"GET", "HEAD"}),
        respect_retry_after_header=False,
        raise_on_status=False,
    )
    adapter = guarded_adapter(pool_connections=pool, pool_maxsize=pool, max_retries=retry,
                              deadline_provider=lambda: getattr(_request_context, 'state', None))
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.max_redirects = max_redirects
    session.hooks["response"].append(_redirect_guard)
    session.hooks["response"].append(guard_body)
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
    from .privacy import redact
    text = redact(exc)
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
