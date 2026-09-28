"""Connect only to the screened DNS answers, retaining the original TLS hostname."""
import ipaddress
import socket

from .netlimits import bounded_call, Deadline, DNS
from .urlguard import UnsafeUrl, _blocked_reason, ensure_web_url


def connect_checked(host, port, timeout=None, source_address=None, socket_options=None, deadline=None):
    if deadline is not None:
        deadline.remaining()
    answers = bounded_call(
        lambda: socket.getaddrinfo(host, port, type=socket.SOCK_STREAM), Deadline(8), gate=DNS)
    if not answers:
        raise UnsafeUrl("The server has no usable network address.")
    # Validate every answer before any socket exists; mixed public/private-
    # service answers cannot select a forbidden fallback later.
    for family, kind, protocol, canonname, address in answers:
        if family not in (socket.AF_INET, socket.AF_INET6):
            raise UnsafeUrl("Unsupported server address family.")
        if _blocked_reason(ipaddress.ip_address(address[0])):
            raise UnsafeUrl("Connection to a protected network address was blocked.")
    failure = None
    for family, kind, protocol, canonname, address in answers:
        remaining = deadline.remaining() if deadline is not None else None
        stream = socket.socket(family, kind, protocol)
        try:
            for option in socket_options or ():
                stream.setsockopt(*option)
            limit = timeout if timeout is None or isinstance(timeout, (int, float)) else socket.getdefaulttimeout()
            if remaining is not None:
                limit = min(limit, remaining) if limit is not None else remaining
            stream.settimeout(limit)
            if source_address:
                stream.bind(source_address)
            stream.connect(address)  # A numeric sockaddr: no second DNS resolution.
            if deadline is not None:
                deadline.remaining()
            return stream
        except OSError as exc:
            failure = exc
            stream.close()
    raise failure or OSError("No usable server connection.")


def guarded_adapter(deadline_provider=None, **kwargs):
    from requests.adapters import HTTPAdapter
    from requests.exceptions import InvalidURL
    from urllib3.connection import HTTPConnection, HTTPSConnection
    from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool
    from urllib3.exceptions import ConnectTimeoutError, NameResolutionError, NewConnectionError

    class CheckedConnection:
        def _new_conn(self):
            try:
                return connect_checked(self._dns_host, self.port, self.timeout,
                                       self.source_address, self.socket_options,
                                       deadline_provider() if deadline_provider is not None else None)
            except socket.gaierror as exc:
                raise NameResolutionError(self.host, self, exc) from exc
            except socket.timeout as exc:
                raise ConnectTimeoutError(self, "Connection timed out.") from exc
            except OSError as exc:
                raise NewConnectionError(self, "Could not connect to the server.") from exc

    class CheckedHTTP(CheckedConnection, HTTPConnection):
        pass

    class CheckedHTTPS(CheckedConnection, HTTPSConnection):
        pass

    class HTTPPool(HTTPConnectionPool):
        ConnectionCls = CheckedHTTP

    class HTTPSPool(HTTPSConnectionPool):
        ConnectionCls = CheckedHTTPS

    class Adapter(HTTPAdapter):
        def init_poolmanager(self, *args, **options):
            super().init_poolmanager(*args, **options)
            self.poolmanager.pool_classes_by_scheme = {"http": HTTPPool, "https": HTTPSPool}

        def send(self, request, **options):
            try:
                ensure_web_url(request.url)
                if request.url.lower().startswith('http:') and 'Authorization' in request.headers:
                    raise UnsafeUrl('Authenticated requests require HTTPS.')
                # A proxy resolves the destination outside this adapter's
                # address checks. No app setting currently enables proxies.
                if any((options.get("proxies") or {}).values()):
                    raise UnsafeUrl("Proxy requests require an explicitly supported secure transport.")
                return super().send(request, **options)
            except UnsafeUrl as exc:
                raise InvalidURL(str(exc), request=request) from exc

    return Adapter(**kwargs)
