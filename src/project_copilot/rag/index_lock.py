"""同一完成清单的跨进程互斥；锁文件必须位于各进程共享的本地文件系统。"""

from contextlib import contextmanager
import fcntl
from hashlib import sha256
import os
from pathlib import Path
from threading import local
import time


class IndexLockError(RuntimeError):
    """无法安全获得索引互斥锁，不能继续读写混合版本。"""


_held = local()


@contextmanager
def index_lock(manifest_path: Path, *, timeout_seconds: float = 120):
    # Keep lock artifacts outside sealed experiment output while all processes with
    # the same shared manifest path use the same persistent inode.
    resolved = manifest_path.resolve()
    key = str(resolved)
    held = getattr(_held, "paths", None)
    if held is None or getattr(_held, "pid", None) != os.getpid():
        held = _held.paths = set()
        _held.pid = os.getpid()
    # A final/hybrid retrieval holds the same lock as its dense child.  Only the
    # owning thread may nest it; other threads/processes still take flock.
    if key in held:
        yield
        return
    parent = resolved.parent
    archive = next((part for part in resolved.parents if part.name == "results" and part.parent.name == "evals"), None)
    if archive is not None:
        # Historic result directories are sealed; never create lock artifacts in them.
        shared_root = archive.parent.parent / "data" / "evaluation-locks"
    else:
        shared_root = parent.parent.parent if parent.name == "index_manifests" else parent
    lock_path = shared_root / ".project-copilot-locks" / (sha256(key.encode()).hexdigest() + ".lock")
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = lock_path.open("a+b")
    except OSError:
        raise IndexLockError("无法打开索引锁") from None
    try:
        deadline = time.monotonic() + timeout_seconds
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise IndexLockError("等待索引锁超时") from None
                time.sleep(0.05)
            except OSError:
                raise IndexLockError("无法获取索引锁") from None
        try:
            held.add(key)
            try:
                yield
            finally:
                held.remove(key)
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()
