"""notify.transport -- the one place notify/ touches a socket.

    http_post(url, body, headers) -> (status, body)   notify.channels
    http_post_status(url, body, headers) -> status    notify.webhook
    OPENER                                            for other callers

Every connection resolves the hostname once, refuses non-public
addresses (loopback, RFC1918, link-local/metadata, CGNAT, private v6),
and dials the exact IP it checked, so DNS rebinding can't slip past the
check. Redirects are never followed. TLS still verifies the hostname.
"""

import http.client
import socket
import ssl
import urllib.error
import urllib.request

from notify.webhook import is_public_ip

HTTP_TIMEOUT_SECONDS = 10


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect. webhook.is_safe_url() vets the subscriber's
    URL once; following a 30x would let that endpoint bounce the POST to
    an internal address the SSRF guard never saw."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class UnsafeDestination(OSError):
    """The hostname resolved to a non-public address. An OSError so
    callers treat it like any other transport failure ([PIPE])."""


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Resolve, vet, then connect to the vetted address -- one lookup.

    webhook.is_safe_url() deliberately skips DNS, so a subscriber's
    https://anything.example could resolve to 127.0.0.1, the 169.254
    metadata range, or a host's private network. Checking a lookup and
    then letting the socket do its own second lookup would lose to DNS
    rebinding; dialing the exact IP we checked closes that window.
    TLS still verifies against the hostname via SNI.
    """

    def connect(self):
        infos = socket.getaddrinfo(self.host, self.port,
                                   type=socket.SOCK_STREAM)
        last = None
        for family, socktype, proto, _, sockaddr in infos:
            if not is_public_ip(sockaddr[0]):
                last = UnsafeDestination("resolved to a non-public address")
                continue
            sock = socket.socket(family, socktype, proto)
            sock.settimeout(self.timeout)
            try:
                sock.connect(sockaddr)
            except OSError as exc:
                sock.close()
                last = exc
                continue
            self.sock = self._context.wrap_socket(sock,
                                                  server_hostname=self.host)
            return
        raise last or UnsafeDestination("no address to connect to")


class _PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self):
        super().__init__(context=ssl.create_default_context())

    def https_open(self, req):
        return self.do_open(_PinnedHTTPSConnection, req,
                            context=self._context)


OPENER = urllib.request.build_opener(_NoRedirect, _PinnedHTTPSHandler)


def http_post(url, body, headers):
    """Real transport for notify.channels: returns (status, body).
    Non-2xx comes back as a status, not an exception -- the channel
    classes own the status-to-error mapping. Socket-level failures
    propagate so the dispatcher's [PIPE] logging sees them."""
    req = urllib.request.Request(url, data=body, headers=headers,
                                 method="POST")
    try:
        with OPENER.open(req, timeout=HTTP_TIMEOUT_SECONDS) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except urllib.error.URLError as exc:
        # urllib wraps every connect-time OSError; surface the SSRF refusal
        # as itself so logs/tests can tell it from an ordinary outage.
        if isinstance(exc.reason, UnsafeDestination):
            raise exc.reason from None
        raise


def http_post_status(url, body, headers):
    """notify.webhook's narrower contract: a bare status int."""
    return http_post(url, body, headers)[0]
