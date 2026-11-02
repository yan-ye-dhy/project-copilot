"""Worker PID handles container termination without abandoning a claimed job."""
import os
import select
import signal
import subprocess
import sys


def test_idle_worker_exits_cleanly_on_sigterm():
    source = '''
from types import SimpleNamespace
import scripts.ai_worker as worker
class FakeDatabase:
    def __init__(self, *args, **kwargs): pass
    def close(self): print("CLOSED", flush=True)
worker.Database = FakeDatabase
worker.load_settings = lambda: SimpleNamespace(public_demo=True, environment="test", database_url=SimpleNamespace(get_secret_value=lambda: "synthetic"), database_connect_timeout_seconds=1, database_pool_timeout_seconds=1, public_job_queue_ttl_seconds=86400)
worker.expire_unclaimed = lambda *args, **kwargs: 0
def idle(*args):
    print("READY", flush=True)
    return False
worker.process_one = idle
worker.main()
'''
    process = subprocess.Popen([sys.executable, "-u", "-c", source], stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True, env={**os.environ})
    try:
        ready, _, _ = select.select([process.stdout], [], [], 10)
        assert ready and process.stdout.readline().strip() == "READY"
        process.send_signal(signal.SIGTERM)
        output, errors = process.communicate(timeout=5)
        assert process.returncode == 0, errors
        assert "CLOSED" in output
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def test_worker_finishes_in_flight_job_before_sigterm_exit():
    source = '''
from types import SimpleNamespace
import time
import scripts.ai_worker as worker
class FakeDatabase:
    def __init__(self, *args, **kwargs): pass
    def close(self): print("CLOSED", flush=True)
worker.Database = FakeDatabase
worker.load_settings = lambda: SimpleNamespace(public_demo=True, environment="test", database_url=SimpleNamespace(get_secret_value=lambda: "synthetic"), database_connect_timeout_seconds=1, database_pool_timeout_seconds=1, public_job_queue_ttl_seconds=86400)
worker.expire_unclaimed = lambda *args, **kwargs: 0
def active(*args):
    print("ACTIVE", flush=True)
    time.sleep(0.5)
    print("FINISHED", flush=True)
    return True
worker.process_one = active
worker.main()
'''
    process = subprocess.Popen([sys.executable, "-u", "-c", source], stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True, env={**os.environ})
    try:
        ready, _, _ = select.select([process.stdout], [], [], 10)
        assert ready and process.stdout.readline().strip() == "ACTIVE"
        process.send_signal(signal.SIGTERM)
        output, errors = process.communicate(timeout=5)
        assert process.returncode == 0, errors
        assert output.splitlines() == ["FINISHED", "CLOSED"]
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
