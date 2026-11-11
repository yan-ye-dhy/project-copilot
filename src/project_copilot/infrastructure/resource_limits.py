"""Host-local GPU model ownership on a shared, trusted manifest filesystem.

This is a physical process lock, not a Redis lease or a cross-host scheduler.
Its persistent inode is never removed. A caller releases only after real model
work has ended and the resident model has been unloaded.
"""
import fcntl
import os
from pathlib import Path
import stat


class ModelResourceBusy(RuntimeError):
    pass


class ModelResourceError(RuntimeError):
    pass


class GpuModelOwner:
    _directory = ".model-resources"
    _filename = "gpu-reranker.lock"

    def __init__(self, descriptor):
        self._fd = descriptor

    @classmethod
    def acquire(cls, manifest_dir):
        descriptor = None
        try:
            root = Path(manifest_dir).absolute()
            root.mkdir(parents=True, exist_ok=True)
            root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                try:
                    os.mkdir(cls._directory, 0o700, dir_fd=root_fd)
                except FileExistsError:
                    pass
                directory = os.open(cls._directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
            finally:
                os.close(root_fd)
            try:
                info = os.fstat(directory)
                if info.st_uid != os.getuid() or info.st_mode & 0o077:
                    raise ModelResourceError("GPU resource directory is not private")
                descriptor = os.open(cls._filename, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=directory)
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise ModelResourceBusy("GPU model is owned by another runtime") from None
                info = os.fstat(descriptor)
                named = os.stat(cls._filename, dir_fd=directory, follow_symlinks=False)
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077
                        or info.st_nlink != 1 or info.st_ino != named.st_ino or info.st_dev != named.st_dev):
                    raise ModelResourceError("GPU resource lock identity differs")
            finally:
                os.close(directory)
            owner = cls(descriptor)
            descriptor = None
            return owner
        except OSError:
            raise ModelResourceError("GPU resource ownership unavailable") from None
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def close(self):
        if self._fd is not None:
            descriptor, self._fd = self._fd, None
            os.close(descriptor)

    # No destructor: interrupted drain must keep the raw OS descriptor held
    # until explicit successful cleanup or process death.
