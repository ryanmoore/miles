"""The one outbound HTTP path for every public service miles calls.

Each service has a ServicePolicy (interval, exclusivity, retryable statuses,
pause after "slow down"). State is process-wide, so concurrent jobs and
handlers share one budget per service. docs/external-services.md lists each
service's usage policy and how these numbers follow from it.
"""

import email.utils
import http.client
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from importlib import metadata
from typing import Callable, TypeVar

from . import metrics

T = TypeVar("T")

PROJECT_NAME = "miles"
CONTACT_ENV = "MILES_CONTACT"

_MAX_BACKOFF_S = 120.0
_MAX_RETRY_AFTER_S = 300.0

# IncompleteRead (a body cut short) is an HTTPException, not an OSError.
_TRANSIENT_ERRORS = (urllib.error.URLError, TimeoutError, ConnectionError, http.client.HTTPException)


class ServiceBackoffError(urllib.error.URLError):
    """The service has told us to stay away for longer than a request is worth
    waiting out (a block, not a rate limit); nothing was sent."""

# Called with (service, host, status, kind) for every attempt: status is the
# HTTP code, or "error" for a connection failure. outbound.claim() points it
# at the database; None leaves accounting off.
request_sink: Callable[[str, str, str, str], None] | None = None


# Called with (service, status) when a request ends on an HTTP error status:
# the last attempt, not one a retry went on to get past. None leaves it off.
outcome_sink: Callable[[str, int], None] | None = None


def report(service: str, host: str, status: int | str, kind: str = "") -> None:
    if request_sink is not None:
        request_sink(service, host, str(status), kind)


def report_outcome(service: str, status: int) -> None:
    if outcome_sink is not None:
        outcome_sink(service, status)


def _version() -> str:
    try:
        return metadata.version(PROJECT_NAME)
    except metadata.PackageNotFoundError:
        return "dev"


def user_agent() -> str:
    """`miles/<version> (+self-hosted[; contact: <MILES_CONTACT>])`; public
    services ask for a way to reach whoever generates the traffic."""
    contact = os.environ.get(CONTACT_ENV, "").strip()
    suffix = f"; contact: {contact}" if contact else ""
    return f"{PROJECT_NAME}/{_version()} (+self-hosted{suffix})"


@dataclass(frozen=True)
class ServicePolicy:
    name: str
    # Minimum seconds between one request's start and the next's (end and
    # next start for an exclusive service, where requests never overlap).
    min_interval_s: float = 0.0
    # Hold a process-wide lock for the whole request, retries included.
    exclusive: bool = False
    retry_statuses: tuple[int, ...] = (429, 503, 504)
    max_attempts: int = 4
    backoff_base_s: float = 5.0
    # status -> minimum seconds to stay quiet after seeing it, applied to the
    # retry and to every other caller of the service.
    min_pause_s: dict[int, float] = field(default_factory=dict)
    timeout_s: float = 60.0


# Nominatim: a 429 means back off a minute, a 403 means blocked (stay away an
# hour); neither is retried.
NOMINATIM = ServicePolicy(
    "nominatim",
    min_interval_s=1.1,
    exclusive=True,
    retry_statuses=(503,),
    max_attempts=2,
    backoff_base_s=2.0,
    min_pause_s={429: 60.0, 403: 3600.0},
    timeout_s=30.0,
)
# A search waits on OSRM before falling back to estimates, so a dead server
# must be given up on in seconds: one retry, short timeout.
OSRM = ServicePolicy("osrm", min_interval_s=1.1, max_attempts=2, backoff_base_s=1.0, timeout_s=3.0)
TERRARIUM = ServicePolicy("terrarium", min_interval_s=0.1, max_attempts=4, backoff_base_s=1.0, timeout_s=60.0)
# Responses are small and fast; a stalled TLS handshake is better retried than waited out.
OPEN_METEO = ServicePolicy(
    "open-meteo", min_interval_s=1.0, max_attempts=3, backoff_base_s=2.0, min_pause_s={429: 60.0}, timeout_s=10.0
)
# Two attempts: a dropped connection restarts the whole (large) download.
GEOFABRIK = ServicePolicy("geofabrik", exclusive=True, max_attempts=2, backoff_base_s=5.0, timeout_s=300.0)


class _State:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.stamp_lock = threading.Lock()
        self.quiet_lock = threading.Lock()
        self.last_at: float | None = None
        self.quiet_until = 0.0

    def stay_quiet_until(self, when: float) -> None:
        with self.quiet_lock:
            self.quiet_until = max(self.quiet_until, when)


_states: dict[str, _State] = {}
_states_lock = threading.Lock()


def _state(policy: ServicePolicy) -> _State:
    with _states_lock:
        return _states.setdefault(policy.name, _State())


def conditional_headers(etag: str | None, last_modified: str | None) -> dict[str, str]:
    """Request headers asking the server to answer 304 when the stored copy,
    identified by these validators, is still current."""
    headers: dict[str, str] = {}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    return headers


def pause(policy: ServicePolicy, seconds: float) -> None:
    """Keep every caller of the service quiet for `seconds` (a longer pause
    already in force stays)."""
    _state(policy).stay_quiet_until(time.monotonic() + seconds)


def quiet_remaining_s(policy: ServicePolicy) -> float:
    """Seconds left of the service's quiet period; a caller that would rather
    do without the service than wait it out checks this first."""
    return max(0.0, _state(policy).quiet_until - time.monotonic())


def _wait_turn(policy: ServicePolicy, state: _State) -> None:
    """Sleep until the interval and any quiet period pass, then stamp this
    request. The stamp lock is held across the sleep so two threads never both
    take the turn; the quiet period is re-read since a 429 can extend it."""
    with state.stamp_lock:
        while True:
            now = time.monotonic()
            ready = state.quiet_until
            if state.last_at is not None:
                ready = max(ready, state.last_at + policy.min_interval_s)
            if ready <= now:
                break
            if state.quiet_until - now > _MAX_RETRY_AFTER_S:
                raise ServiceBackoffError(
                    f"{policy.name} asked us to stay away; not contacting it for another "
                    f"{int((state.quiet_until - now) // 60) + 1} min"
                )
            metrics.HTTP_WAIT_SECONDS.labels(service=policy.name).inc(ready - now)
            time.sleep(ready - now)
        state.last_at = time.monotonic()


def _retry_after_s(headers: object) -> float | None:
    value = getattr(headers, "get", lambda _k: None)("Retry-After")
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(str(value))
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())


def _read_all(resp: object) -> bytes:
    return resp.read()  # type: ignore[attr-defined]


def _attempt_loop(policy: ServicePolicy, state: _State, send: Callable[[], T], host: str) -> T:
    attempt = 0
    while True:
        _wait_turn(policy, state)
        try:
            result = send()
        except urllib.error.HTTPError as e:
            report(policy.name, host, e.code)
            pause = policy.min_pause_s.get(e.code, 0.0)
            if pause:
                state.stay_quiet_until(time.monotonic() + pause)
            retryable = e.code in policy.retry_statuses
            if not retryable or attempt + 1 >= policy.max_attempts:
                report_outcome(policy.name, e.code)
                raise
            retry_after = _retry_after_s(e.headers) or 0.0
            if retry_after > _MAX_RETRY_AFTER_S:
                report_outcome(policy.name, e.code)
                raise  # retrying sooner would ignore the server's request
            delay = max(min(policy.backoff_base_s * (2**attempt), _MAX_BACKOFF_S), pause, retry_after)
        except _TRANSIENT_ERRORS as e:
            report(policy.name, host, "error")
            if attempt + 1 >= policy.max_attempts:
                if isinstance(e, http.client.HTTPException):
                    # Callers handle URLError and OSError; a body cut short
                    # is the same kind of failure.
                    raise urllib.error.URLError(e) from e
                raise
            delay = min(policy.backoff_base_s * (2**attempt), _MAX_BACKOFF_S)
        else:
            return result
        attempt += 1
        metrics.HTTP_RETRIES.labels(service=policy.name).inc()
        state.stay_quiet_until(time.monotonic() + delay)


def request(
    policy: ServicePolicy,
    url: str,
    *,
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout_s: float | None = None,
    reader: Callable[[object], T] = _read_all,  # type: ignore[assignment]
    method: str | None = None,
) -> T:
    """One polite request: throttled per policy, retried on the policy's
    statuses and connection errors with exponential backoff (honoring
    Retry-After and minimum pauses). The last error propagates when attempts
    run out; a non-retryable status propagates at once. `reader` turns the open
    response into the result (default: all bytes), so a download can stream to disk."""
    state = _state(policy)
    host = urllib.parse.urlsplit(url).hostname or ""
    all_headers = {"User-Agent": user_agent(), **(headers or {})}

    def send() -> T:
        req = urllib.request.Request(url, data=data, headers=all_headers, method=method)
        with urllib.request.urlopen(req, timeout=timeout_s or policy.timeout_s) as resp:
            # Reported after the body is read, so a connection dying mid-body
            # counts as an error, not also as a 200.
            result = reader(resp)
            report(policy.name, host, getattr(resp, "status", 200))
            return result

    if policy.exclusive:
        with state.lock:
            result = _attempt_loop(policy, state, send, host)
            with state.stamp_lock:
                state.last_at = time.monotonic()
            return result
    return _attempt_loop(policy, state, send, host)
