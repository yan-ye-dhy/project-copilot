"""Extract bounded PDF text in a short-lived, resource-limited Linux process."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile


class IsolatedPdfError(ValueError):
    """Invalid input, exhausted resource limits or unavailable extractor."""


MAX_INPUT_BYTES = 4 * 1024 * 1024
MAX_OUTPUT_BYTES = 3 * 1024 * 1024
_WORKER = Path(__file__).with_name("pdf_worker.py")


def extract_pdf(data: bytes, *, max_pages: int = 100, max_chars: int = 32000,
                timeout_seconds: float = 10) -> tuple[str, ...]:
    if (sys.platform != "linux" or not isinstance(data, bytes) or not 1 <= len(data) <= MAX_INPUT_BYTES or
            type(max_pages) is not int or not 1 <= max_pages <= 1000 or
            type(max_chars) is not int or not 1 <= max_chars <= 500000 or
            type(timeout_seconds) not in (int, float) or not 0 < timeout_seconds <= 30):
        raise IsolatedPdfError("无法安全解析 PDF")
    process = None
    try:
        # TemporaryFile has no persistent filename on Linux. The child can write
        # no more than its RLIMIT_FSIZE, so its output cannot exhaust memory.
        with tempfile.TemporaryFile() as output:
            process = subprocess.Popen(
                [sys.executable, "-I", str(_WORKER), str(max_pages), str(max_chars), str(timeout_seconds)],
                stdin=subprocess.PIPE, stdout=output, stderr=subprocess.DEVNULL,
                env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}, close_fds=True,
                start_new_session=True,
            )
            try:
                process.communicate(input=data, timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=5)
                raise IsolatedPdfError("PDF 解析超时") from None
            if process.returncode != 0:
                raise IsolatedPdfError("PDF 解析失败或超过资源限制")
            output.seek(0)
            raw = output.read(MAX_OUTPUT_BYTES + 1)
            if len(raw) > MAX_OUTPUT_BYTES:
                raise IsolatedPdfError("PDF 解析结果超过限制")
            result = json.loads(raw)
            pages = result.get("pages") if isinstance(result, dict) and set(result) == {"pages"} else None
            if (not isinstance(pages, list) or len(pages) > max_pages or
                    any(not isinstance(page, str) for page in pages) or
                    sum(len(page) for page in pages) > max_chars):
                raise IsolatedPdfError("PDF 解析结果无效")
            return tuple(pages)
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        raise IsolatedPdfError("PDF 解析失败或超过资源限制") from None
    finally:
        # Includes cancellation/KeyboardInterrupt: no orphan parser continues
        # after the caller exits or loses interest in the result.
        if process is not None and process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
