"""Optional ranking metadata cache; neither source text nor authority lives here.

The file index lock and current Qdrant verification remain the correctness
boundary. Redis leases only coordinate cache filling. An expired lease never
authorizes a late write, even when the original computation is still running.
"""
from dataclasses import dataclass
from hashlib import sha256
import hmac
import json
import math
from pathlib import Path
import random
from time import perf_counter
from uuid import UUID, uuid4

from project_copilot.infrastructure.call_accounting import current_account_id
from project_copilot.infrastructure.redis_runtime import RedisUnavailable, get_redis_runtime
from project_copilot.infrastructure.tracing import trace_span


GET_LUA = "return redis.call('GET', KEYS[1])"

# One sequence per namespace, rather than an immortal sequence key per query.
# All keys share a hash tag. Ownership and its fence expire together; the
# namespace sequence itself is monotonic across process and lease lifetimes.
# A delayed miss must not acquire after another owner has published. Existing
# invalid data also blocks acquisition: callers compute without filling until
# its TTL expires, rather than overwriting a value under a stale observation.
ACQUIRE_LUA = r"""
if redis.call('EXISTS', KEYS[4]) == 1 then return 0 end
if redis.call('EXISTS', KEYS[1]) == 1 then return 0 end
local fence = redis.call('INCR', KEYS[3])
if fence < 1 or fence >= 9007199254740991 then error('invalid cache fence') end
local acquired = redis.call('SET', KEYS[1], ARGV[1], 'NX', 'PX', ARGV[2])
if not acquired then return 0 end
redis.call('SET', KEYS[2], string.format('%.0f', fence), 'PX', ARGV[2])
return fence
"""

PUBLISH_LUA = r"""
if redis.call('GET', KEYS[1]) ~= ARGV[1]
   or redis.call('GET', KEYS[2]) ~= ARGV[2] then return 0 end
redis.call('SET', KEYS[3], ARGV[3], 'PX', ARGV[4])
redis.call('DEL', KEYS[1], KEYS[2])
return 1
"""

RELEASE_LUA = r"""
if redis.call('GET', KEYS[1]) ~= ARGV[1]
   or redis.call('GET', KEYS[2]) ~= ARGV[2] then return 0 end
redis.call('DEL', KEYS[1], KEYS[2])
return 1
"""


@dataclass(frozen=True)
class CacheIdentity:
    data_key: str
    owner_key: str
    fence_key: str
    sequence_key: str


@dataclass(frozen=True)
class CacheLease:
    owner: str
    fence: int


@dataclass(frozen=True)
class RankingRecord:
    chunk_id: UUID
    score: float


def _records(value, *, allowed_ids=None, allow_negative=False):
    if not isinstance(value, list) or len(value) > 5 or (not value and not allow_negative):
        raise ValueError("invalid ranking metadata")
    result, seen = [], set()
    for item in value:
        if not isinstance(item, dict) or set(item) != {"chunk_id", "score"}:
            raise ValueError("invalid ranking metadata")
        identifier, score = item["chunk_id"], item["score"]
        if (not isinstance(identifier, str) or str(UUID(identifier)) != identifier
                or type(score) not in {int, float} or not math.isfinite(score)):
            raise ValueError("invalid ranking metadata")
        chunk_id = UUID(identifier)
        if chunk_id in seen or (allowed_ids is not None and chunk_id not in allowed_ids):
            raise ValueError("invalid ranking identity")
        seen.add(chunk_id)
        result.append(RankingRecord(chunk_id, float(score)))
    return result


class RankingCache:
    """One request's cache adapter; all failures fall back to full retrieval."""

    def __init__(self, settings, runtime=None):
        self.settings = settings
        self.runtime = runtime if runtime is not None else get_redis_runtime(settings)
        if self.runtime is None:
            raise RedisUnavailable()
        self.last_status = "miss"
        self.last_lock_wait_ms = 0.0

    def identity(self, query, manifest, *, pipeline):
        secret = self.settings.redis_cache_hmac_key.get_secret_value()
        if len(secret) < 32:
            raise RedisUnavailable()
        # Account identity comes from server-owned accounting context. All users
        # can currently read the shared library, but their cache scopes remain
        # separate. No request field can choose this scope.
        account = current_account_id()
        if account is None and _public_scope_required(self.settings):
            raise RedisUnavailable()
        if account is not None and not isinstance(account, UUID):
            raise RedisUnavailable()
        scope = {
            "account": str(account) if account is not None else "local",
            "environment": getattr(self.settings, "environment", "development"),
            "public": getattr(self.settings, "public_demo", False),
            "resource": getattr(self.settings, "public_resource_id", "local"),
            "qdrant": str(getattr(self.settings, "qdrant_url", "")),
            "manifest_dir": str(Path(getattr(self.settings, "manifest_dir", ".")).resolve()),
            "embedding_endpoint": str(getattr(self.settings, "zhipu_base_url", "")),
        }
        binding = {"format": 1, "query": query, "scope": scope,
                   "manifest": manifest.model_dump(mode="json"), "pipeline": pipeline}
        raw = json.dumps(binding, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        digest = hmac.new(secret.encode(), raw, sha256).hexdigest()
        namespace = self.settings.redis_namespace
        prefix = f"{namespace}:ranking:{{{namespace}-ranking-v1}}:"
        return CacheIdentity(prefix + "data:" + digest, prefix + "owner:" + digest,
                             prefix + "fence:" + digest, prefix + "sequence")

    def get(self, identity, allowed_ids, *, allow_negative=False):
        try:
            raw = self.runtime.eval(GET_LUA, [identity.data_key], [])
            if raw is None:
                self.last_status = "miss"
                return None
            if not isinstance(raw, (str, bytes)) or len(raw) > 4096:
                raise ValueError("invalid ranking payload")
            payload = json.loads(raw)
            if not isinstance(payload, dict) or set(payload) != {"format_version", "hits"}:
                raise ValueError("invalid ranking payload")
            if type(payload["format_version"]) is not int or payload["format_version"] != 1:
                raise ValueError("invalid ranking format")
            result = _records(payload["hits"], allowed_ids=allowed_ids, allow_negative=allow_negative)
            self.last_status = "hit" if result else "negative_hit"
            return result
        except RedisUnavailable:
            self.last_status = "unavailable"
        except (ValueError, TypeError, UnicodeError, OverflowError, RecursionError):
            self.last_status = "invalid"
        return None

    def acquire(self, identity):
        started = perf_counter()
        with trace_span("rag.cache.lock", {"openinference.span.kind": "CHAIN"}) as span:
            try:
                return self._acquire(identity)
            finally:
                self.last_lock_wait_ms = (perf_counter() - started) * 1000
                span.set_attribute("lock_wait_ms", self.last_lock_wait_ms)
                span.set_attribute("cache.status", self.last_status)

    def _acquire(self, identity):
        owner = uuid4().hex
        try:
            result = self.runtime.eval(ACQUIRE_LUA,
                [identity.owner_key, identity.fence_key, identity.sequence_key, identity.data_key],
                [owner, math.ceil(self.settings.redis_cache_lock_ttl_seconds * 1000)])
            if type(result) is not int or not 0 <= result < 9007199254740991:
                raise RedisUnavailable()
            self.last_status = "acquired" if result else "busy"
            return CacheLease(owner, result) if result else None
        except RedisUnavailable:
            self.last_status = "unavailable"
            return None

    def _ttl_ms(self, negative):
        seconds = (self.settings.redis_cache_negative_ttl_seconds if negative
                   else self.settings.redis_cache_ttl_seconds)
        jitter = self.settings.redis_cache_ttl_jitter_ratio
        return max(1, math.ceil(seconds * 1000 * random.uniform(1 - jitter, 1 + jitter)))

    def publish(self, identity, lease, records, *, negative=False):
        with trace_span("rag.cache.publish", {"openinference.span.kind": "CHAIN"}) as span:
            try:
                return self._publish(identity, lease, records, negative=negative)
            finally:
                span.set_attribute("cache.status", self.last_status)

    def _publish(self, identity, lease, records, *, negative=False):
        try:
            entries = [{"chunk_id": str(item.chunk_id), "score": item.score} for item in records]
            validated = _records(entries, allow_negative=negative)
            if negative != (not validated):
                raise ValueError("invalid negative cache entry")
            raw = json.dumps({"format_version": 1, "hits": entries}, separators=(",", ":"), allow_nan=False)
            if len(raw) > 4096:
                raise ValueError("ranking payload too large")
            result = self.runtime.eval(PUBLISH_LUA,
                [identity.owner_key, identity.fence_key, identity.data_key],
                [lease.owner, str(lease.fence), raw, self._ttl_ms(negative)])
            if type(result) is not int or result not in {0, 1}:
                raise RedisUnavailable()
            self.last_status = "published" if result else "lease_lost"
            return result == 1
        except RedisUnavailable:
            self.last_status = "unavailable"
        except (ValueError, TypeError, AttributeError, OverflowError):
            self.last_status = "invalid"
        return False

    def release(self, identity, lease):
        try:
            result = self.runtime.eval(RELEASE_LUA, [identity.owner_key, identity.fence_key],
                                       [lease.owner, str(lease.fence)])
            return type(result) is int and result == 1
        except RedisUnavailable:
            self.last_status = "unavailable"
            return False


def _public_scope_required(settings):
    return getattr(settings, "public_demo", False) or getattr(settings, "environment", "development") == "production"


def create_ranking_cache(settings):
    if not getattr(settings, "redis_cache_enabled", False):
        return None
    if _public_scope_required(settings) and current_account_id() is None:
        return None
    try:
        return RankingCache(settings)
    except RedisUnavailable:
        return None
