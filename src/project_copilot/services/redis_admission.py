"""Additional global/account token buckets; PostgreSQL remains authoritative."""
from hashlib import sha256
import json
import math
import re
import time
from uuid import UUID

from project_copilot.infrastructure.redis_runtime import RedisUnavailable, get_redis_runtime
from project_copilot.infrastructure.tracing import trace_span


class RateLimited(RuntimeError):
    def __init__(self, retry_after):
        self.retry_after = max(1, math.ceil(retry_after))
        super().__init__("Request admission rate exceeded")


# All reads/validation precede mutation. A denial does not consume either
# bucket or create attacker-selected account keys. Redis TIME avoids clocks
# diverging across API/worker processes. TTLs allow fully refilled idle keys to
# disappear; PostgreSQL retains durable quotas, ownership and idempotency.
TOKEN_BUCKET_LUA = r"""
local stamp = redis.call('TIME')
local now = tonumber(stamp[1]) * 1000 + math.floor(tonumber(stamp[2]) / 1000)
local global_capacity = tonumber(ARGV[1])
local global_rate = tonumber(ARGV[2]) / 1000
local user_capacity = tonumber(ARGV[3])
local user_rate = tonumber(ARGV[4]) / 1000

local function read_bucket(key, capacity, rate)
    local values = redis.call('HMGET', key, 'tokens', 'updated_ms')
    if not values[1] and not values[2] then
        return capacity, now
    end
    local tokens = tonumber(values[1])
    local updated = tonumber(values[2])
    if not tokens or not updated or not (tokens >= 0 and tokens <= capacity)
       or not (updated >= 0 and updated < 9007199254740991) then
        error('invalid admission bucket')
    end
    local effective = math.max(now, updated)
    return math.min(capacity, tokens + (effective - updated) * rate), effective
end

local global_tokens, global_updated = read_bucket(KEYS[1], global_capacity, global_rate)
local user_tokens, user_updated = read_bucket(KEYS[2], user_capacity, user_rate)
local wait_global = global_tokens < 1 and (math.max(0, global_updated - now) + (1 - global_tokens) / global_rate) or 0
local wait_user = user_tokens < 1 and (math.max(0, user_updated - now) + (1 - user_tokens) / user_rate) or 0
local retry_ms = math.ceil(math.max(wait_global, wait_user))
if global_tokens < 1 or user_tokens < 1 then
    return {0, math.max(1, retry_ms)}
end

redis.call('HSET', KEYS[1], 'tokens', global_tokens - 1, 'updated_ms', global_updated)
redis.call('PEXPIRE', KEYS[1], math.max(1000, math.ceil(math.max(0, global_updated - now) + global_capacity / global_rate) + 1000))
redis.call('HSET', KEYS[2], 'tokens', user_tokens - 1, 'updated_ms', user_updated)
redis.call('PEXPIRE', KEYS[2], math.max(1000, math.ceil(math.max(0, user_updated - now) + user_capacity / user_rate) + 1000))
return {1, 0}
"""


def _bucket_configuration(settings, scope):
    if scope not in {"jobs", "provider"}:
        raise RedisUnavailable()
    prefix = "redis_admission" if scope == "jobs" else "redis_provider"
    names = ("global_capacity", "global_refill_per_second", "user_capacity", "user_refill_per_second")
    values = tuple(getattr(settings, prefix + "_" + name,
                           getattr(settings, "redis_admission_" + name, None)) for name in names)
    if any(type(value) not in {int, float} or not math.isfinite(value) or not 0 < value <= 1_000_000
           for value in values) or values[0] < 1 or values[2] < 1:
        raise RedisUnavailable()
    # Extremely slow refill must still fit Redis's millisecond TTL range and
    # retain useful numerical precision. Settings applies the same lower bound.
    if values[1] < 0.000001 or values[3] < 0.000001:
        raise RedisUnavailable()
    return values


def bucket_keys(settings, user_id, scope="jobs"):
    """Return non-secret, version-bound keys for validation and isolated tests."""
    if not isinstance(user_id, UUID):
        raise RedisUnavailable()
    namespace = getattr(settings, "redis_namespace", "")
    if not isinstance(namespace, str) or not re.fullmatch(r"[A-Za-z0-9_-]{3,64}", namespace):
        raise RedisUnavailable()
    values = _bucket_configuration(settings, scope)
    version = sha256(json.dumps([float(value) for value in values], separators=(",", ":")).encode()).hexdigest()[:20]
    # Matching hash tags also keep both keys in one Redis Cluster slot.
    prefix = f"{namespace}:admission:{{{namespace}-{scope}-{version}}}"
    account = sha256(user_id.bytes).hexdigest()
    return (prefix + ":global", prefix + ":account:" + account), values


def admit(settings, user_id, scope="jobs"):
    """Consume one unit, or fail closed. Disabled admission does no work."""
    if not getattr(settings, "redis_admission_enabled", False):
        return
    started = time.perf_counter()
    with trace_span("admission.redis", {"admission.scope": scope if scope in {"jobs", "provider"} else "invalid"}) as span:
        try:
            return _admit(settings, user_id, scope)
        except RateLimited:
            span.set_attribute("rate_limit_rejected", True)
            raise
        except RedisUnavailable:
            span.set_attribute("admission.unavailable", True)
            raise
        finally:
            # End-to-end gate duration, not a claim of pure server execution.
            span.set_attribute("admission_wait_ms", (time.perf_counter() - started) * 1000)


def _admit(settings, user_id, scope):
    try:
        keys, arguments = bucket_keys(settings, user_id, scope)
        runtime = get_redis_runtime(settings)
        if runtime is None:
            raise RedisUnavailable()
        result = runtime.eval(TOKEN_BUCKET_LUA, keys, arguments)
        if (not isinstance(result, (list, tuple)) or len(result) != 2
                or any(type(value) is not int for value in result)
                or result[0] not in {0, 1} or result[1] < 0
                or (result[0] == 1 and result[1] != 0)
                or (result[0] == 0 and result[1] == 0)):
            raise RedisUnavailable()
    except Exception:
        raise RedisUnavailable() from None
    if result[0] == 0:
        raise RateLimited(result[1] / 1000)
