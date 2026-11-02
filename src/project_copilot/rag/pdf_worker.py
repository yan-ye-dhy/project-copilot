"""Resource-bounded PDFium child; inputs and extracted text never enter argv/logs.

This process isolation constrains accidental resource exhaustion; it is not an
OS security sandbox against a native PDF parser compromise.
"""
import json
import math
import resource
import signal
import sys
from contextlib import closing


def main():
    try:
        page_limit, char_limit = (int(value) for value in sys.argv[1:3])
        timeout = float(sys.argv[3])
        if not 1 <= page_limit <= 1000 or not 1 <= char_limit <= 500000 or not 0 < timeout <= 30:
            raise ValueError("invalid limits")
        # A wall-clock deadline remains in force even if the parent is killed.
        signal.signal(signal.SIGALRM, signal.SIG_DFL)
        signal.alarm(max(1, math.ceil(timeout)))
        # Apply before importing the native parser. No subprocess preexec_fn
        # in the multi-threaded parent; core dumps and oversized output disabled.
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        resource.setrlimit(resource.RLIMIT_CPU, (5, 6))
        resource.setrlimit(resource.RLIMIT_AS, (768 * 1024 * 1024, 768 * 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_FSIZE, (3 * 1024 * 1024, 3 * 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_NOFILE, (32, 32))
        import pypdfium2 as pdfium

        data = sys.stdin.buffer.read(4 * 1024 * 1024 + 1)
        if not data or len(data) > 4 * 1024 * 1024:
            raise ValueError("invalid input size")
        pages, total = [], 0
        with closing(pdfium.PdfDocument(data)) as pdf:
            if len(pdf) > page_limit:
                raise ValueError("page limit")
            for page_index in range(len(pdf)):
                with closing(pdf[page_index]) as page:
                    with closing(page.get_textpage()) as text_page:
                        # This may conservatively reject characters outside the
                        # visible page; it avoids allocating a very large string.
                        if total + text_page.count_chars() > char_limit:
                            raise ValueError("character limit")
                        text = text_page.get_text_bounded()
                text = text.replace("\r\n", "\n").replace("\r", "\n")
                total += len(text)
                if total > char_limit:
                    raise ValueError("character limit")
                pages.append(text)
        sys.stdout.write(json.dumps({"pages": pages}, ensure_ascii=True, separators=(",", ":")))
    except Exception:
        # Native parser details, paths and document content are never reported.
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
