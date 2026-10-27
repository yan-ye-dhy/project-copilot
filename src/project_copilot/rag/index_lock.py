"""同一完成清单的跨进程互斥；锁文件必须位于各进程共享的本地文件系统。"""

from contextlib import contextmanager
import fcntl
from hashlib import sha256
from pathlib import Path
import time


class IndexLockError(RuntimeError):
    """无法安全获得索引互斥锁，不能继续读写混合版本。"""


@contextmanager
def index_lock(manifest_path: Path, *, timeout_seconds: float = 120):
    # Keep lock artifacts outside sealed experiment output while all processes with
    # the same shared manifest path use the same persistent inode.
    parent = manifest_path.parent
    shared_root = parent.parent.parent if parent.name == "index_manifests" else parent
    lock_path = shared_root / ".project-copilot-locks" / (sha256(str(manifest_path.resolve()).encode()).hexdigest() + ".lock")
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
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()
