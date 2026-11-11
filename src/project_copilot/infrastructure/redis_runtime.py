"""Optional, process-local Redis pools; construction never opens a socket."""
from threading import Lock
from urllib.parse import urlsplit
import math
import re


class RedisUnavailable(RuntimeError):
    """A redacted failure: credentials and server exception text stay private."""

    def __init__(self):
        super().__init__("Redis admission is unavailable")


def _connection_configuration(settings):
    secret = getattr(settings, "redis_url", None)
    value = secret.get_secret_value() if hasattr(secret, "get_secret_value") else secret
    try:
        parsed = urlsplit(value)
        timeout = settings.redis_socket_timeout_seconds
        maximum = settings.redis_pool_max_connections
        valid = (isinstance(value, str) and not any(ord(c) < 33 for c in value)
                 and parsed.scheme in {"redis", "rediss"} and parsed.hostname
                 and parsed.port is not None and 1 <= parsed.port <= 65535
                 and not parsed.query and not parsed.fragment
                 and re.fullmatch(r"/[0-9]{1,2}", parsed.path) and int(parsed.path[1:]) <= 15
                 and type(timeout) in {int, float} and math.isfinite(timeout) and 0 < timeout <= 30
                 and type(maximum) is int and 1 <= maximum <= 1000)
    except Exception:
        raise RedisUnavailable() from None
    if not valid:
        raise RedisUnavailable()
    return value, timeout, maximum


def _create_client(settings):
    # Import only after explicit enablement. The default app has no Redis
    # import, DNS lookup, health probe, or dependency on a running Redis server.
    try:
        from redis import ConnectionPool, Redis
        from redis.backoff import NoBackoff
        from redis.retry import Retry

        url, timeout, maximum = _connection_configuration(settings)
        pool = ConnectionPool.from_url(
            url, socket_connect_timeout=timeout, socket_timeout=timeout,
            max_connections=maximum, retry=Retry(NoBackoff(), 0),
            retry_on_timeout=False, health_check_interval=0, decode_responses=False,
        )
        try:
            return Redis(connection_pool=pool)
        except Exception:
            pool.disconnect()
            raise
    except Exception:
        raise RedisUnavailable() from None


class RedisRuntime:
    """One bounded synchronous pool shared by this process's worker threads."""

    def __init__(self, settings):
        self.client = _create_client(settings)
        self._closed = False
        self._close_lock = Lock()

    def ping(self):
        try:
            if self._closed or self.client.ping() is not True:
                raise RedisUnavailable()
        except Exception:
            raise RedisUnavailable() from None
        return True

    def eval(self, script, keys, arguments):
        try:
            if self._closed:
                raise RedisUnavailable()
            # EVAL has exactly one transmission attempt. Do not use Script's
            # EVALSHA/NOSCRIPT fallback or retry an ambiguous write response.
            return self.client.eval(script, len(keys), *keys, *arguments)
        except Exception:
            raise RedisUnavailable() from None

    def close(self):
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
            failed = False
            try:
                self.client.close()
            except Exception:
                failed = True
            try:
                # Redis(connection_pool=...) does not own the supplied pool.
                self.client.connection_pool.disconnect()
            except Exception:
                failed = True
            if failed:
                raise RedisUnavailable() from None


_runtimes = {}
_runtime_lock = Lock()


def get_redis_runtime(settings):
    if not getattr(settings, "redis_enabled", False):
        return None
    with _runtime_lock:
        identity = id(settings)
        existing = _runtimes.get(identity)
        if existing is not None:
            return existing[1]
        runtime = RedisRuntime(settings)
        # Retain settings until close so Python cannot recycle its identity.
        _runtimes[identity] = (settings, runtime)
        return runtime


def close_redis_runtime(settings):
    with _runtime_lock:
        existing = _runtimes.pop(id(settings), None)
    if existing is not None:
        existing[1].close()
