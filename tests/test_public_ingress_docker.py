"""Actual Nginx shared-zone/HTTP tests on fresh local containers, no cloud.

Enable with PROJECT_COPILOT_RUN_INGRESS_DOCKER=1. No persistent volumes, original
containers or public resources are used. Slow-origin test changes ONLY read
 timeout 110s -> 1s in a separate derived config, documented as accelerated.
"""
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
import http.client
import json
import os
from pathlib import Path
import subprocess
import time
from uuid import uuid4

import pytest

ROOT = Path(__file__).resolve().parents[1]
SECRET = 'synthetic-proxy-header-for-test-only'
APP_IMAGE = os.environ.get('PROJECT_COPILOT_INGRESS_TEST_API_IMAGE', 'project-copilot:default-domain-validation')
JOB = '00000000-0000-0000-0000-000000000001'
pytestmark = pytest.mark.skipif(os.environ.get('PROJECT_COPILOT_RUN_INGRESS_DOCKER') != '1',
                                reason='explicit fresh local Docker ingress test')
UPSTREAM = r'''
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json,time
from urllib.parse import urlsplit,parse_qs
class Handler(BaseHTTPRequestHandler):
 def log_message(self,*args): pass
 def handle_request(self):
  query=parse_qs(urlsplit(self.path).query)
  if query.get('delay'): time.sleep(float(query['delay'][0]))
  if query.get('disconnect'): self.connection.close(); return
  body=self.rfile.read(int(self.headers.get('Content-Length','0')))
  status=503 if query.get('closed') else 403 if self.headers.get('X-Project-Copilot-Proxy-Secret')!='synthetic-proxy-header-for-test-only' else 200
  data=json.dumps({'bytes':len(body),'headers':dict(self.headers),'worker':self.headers.get('X-Test-Worker')}).encode()
  if urlsplit(self.path).path.endswith('/events') and status==200:
   self.send_response(200)
   self.send_header('Content-Type','text/event-stream')
   self.send_header('Cache-Control','no-store, no-transform')
   self.send_header('X-Accel-Buffering','no')
   self.end_headers()
   self.wfile.write(b'id: 1\nevent: progress\ndata: '+data+b'\n\n')
   self.wfile.flush()
   time.sleep(1)
   try:self.wfile.write(b'id: 2\nevent: terminal\ndata: {"status":"succeeded"}\n\n')
   except (BrokenPipeError, ConnectionResetError):pass
   return
  self.send_response(status)
  self.send_header('Content-Type','application/json')
  self.send_header('Content-Length',str(len(data)))
  self.send_header('Set-Cookie','session=synthetic; Path=/; Secure; HttpOnly; SameSite=Lax')
  self.send_header('Set-Cookie','csrf=synthetic; Path=/; Secure; SameSite=Lax')
  self.end_headers()
  try:self.wfile.write(data)
  except (BrokenPipeError, ConnectionResetError):pass
 do_GET=do_POST=do_DELETE=handle_request
ThreadingHTTPServer(('0.0.0.0',8002),Handler).serve_forever()
'''


def docker(*args):
    result = subprocess.run(['docker', *map(str, args)], capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError('Synthetic Docker test failed: ' + result.stderr[-2000:])
    return result.stdout.strip()


def request(port, method='GET', path='/health', body=None, headers=None):
    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=8)
    try:
        connection.request(method, path, body=body, headers={'X-Project-Copilot-Proxy-Secret': SECRET, **(headers or {})})
        response = connection.getresponse()
        return response.status, response.getheaders(), response.read()
    finally:
        connection.close()


@pytest.fixture(scope='module')
def ingress(tmp_path_factory):
    path = tmp_path_factory.mktemp('synthetic-ingress')
    upstream = path / 'upstream.py'
    upstream.write_text(UPSTREAM)
    upstream.chmod(0o644)
    name = 'pc-ingress-test-' + uuid4().hex[:12]
    network = name + '-net'
    containers = []
    docker('network', 'create', '--label', 'io.project-copilot.test=default-domain', network)
    try:
        docker('build', '--pull=false', '-q', '-f', ROOT / 'deploy/nginx/Dockerfile', '-t', 'project-copilot-ingress:default-domain', ROOT)
        api = name + '-api'
        docker('run', '-d', '--rm', '--name', api, '--network', network, '--network-alias', 'api',
               '--mount', f'type=bind,src={upstream},dst=/qa/upstream.py,readonly',
               APP_IMAGE, 'python', '/qa/upstream.py')
        containers.append(api)
        time.sleep(.5)
        ports = []
        for accelerated in (False, True):
            config = path / ('accelerated.conf' if accelerated else 'normal.conf')
            source = (ROOT / 'deploy/nginx/public-demo.conf').read_text()
            if accelerated:
                source = source.replace('proxy_read_timeout 110s;', 'proxy_read_timeout 1s;')
            config.write_text(source)
            config.chmod(0o644)
            container = name + ('-fast' if accelerated else '-normal')
            docker('run', '-d', '--rm', '--name', container, '--network', network,
                   '--read-only', '--tmpfs', '/tmp:size=64m,uid=101,gid=101,mode=700',
                   '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges', '--pids-limit', '64',
                   '-p', '127.0.0.1::8080', '--mount', f'type=bind,src={config},dst=/etc/nginx/nginx.conf,readonly',
                   'project-copilot-ingress:default-domain')
            containers.append(container)
            port = int(docker('port', container, '8080/tcp').rsplit(':',1)[1])
            for attempt in range(30):
                try:
                    if request(port)[0] == 200:
                        break
                except OSError:
                    pass
                time.sleep(.1)
            else:
                raise RuntimeError(docker('logs', container))
            ports.append(port)
        yield ports, containers[-2], network
    finally:
        for container in reversed(containers):
            subprocess.run(['docker','stop','--time','3',container], capture_output=True)
        subprocess.run(['docker','network','rm',network], capture_output=True)


def test_cookie_csrf_body_and_header_boundary(ingress):
    port = ingress[0][0]
    status, headers, body = request(port, 'POST', '/jobs', b'{"message":"hello"}', {
        'Cookie':'session=synthetic; csrf=synthetic', 'Origin':'https://synthetic.edgeone.cool',
        'X-CSRF-Token':'synthetic', 'X-Forwarded-For':'1.2.3.4', 'CF-Connecting-IP':'1.2.3.4',
        'X-Internal-Address':'secret.internal', 'Authorization':'untrusted', 'Content-Type':'application/json'})
    assert status == 200
    forwarded = {k.lower():v for k,v in json.loads(body)['headers'].items()}
    assert forwarded['cookie'] == 'session=synthetic; csrf=synthetic'
    assert forwarded['x-csrf-token'] == 'synthetic'
    assert forwarded['origin'] == 'https://synthetic.edgeone.cool'
    assert not {'x-forwarded-for','cf-connecting-ip','x-internal-address','authorization'} & forwarded.keys()
    assert len([value for key,value in headers if key.lower() == 'set-cookie']) == 2
    assert dict(headers)['Cache-Control'] == 'no-store'


def test_upload_four_mib_and_oversized_rejection(ingress):
    port = ingress[0][0]
    status, _, body = request(port, 'POST', '/jobs/upload', b'x' * (4*1024*1024+100))
    assert status == 200 and json.loads(body)['bytes'] == 4*1024*1024+100
    assert request(port, 'POST', '/jobs/upload', b'x' * (4*1024*1024+65537))[0] == 413


def test_missing_secret_bad_paths_and_closed_remain_rejected(ingress):
    port = ingress[0][0]
    assert request(port, headers={'X-Project-Copilot-Proxy-Secret':''})[0] == 403
    # Response arrives without any body bytes; rejection precedes buffering.
    assert request(port, 'POST', '/jobs/upload', headers={'X-Project-Copilot-Proxy-Secret':'', 'Content-Length':'4194304'})[0] == 403
    assert request(port, headers={'X-Project-Copilot-Proxy-Secret':'A'*32})[0] == 403
    assert request(port, 'GET', '/docs')[0] == 404
    assert request(port, 'GET', '/%68ealth')[0] == 400
    assert request(port, 'PUT', '/jobs')[0] == 404
    assert request(port, 'GET', '/health?closed=1')[0] == 503


def parallel_writes(port):
    with ThreadPoolExecutor(max_workers=8) as pool:
        return list(pool.map(lambda _:request(port, 'POST', '/jobs?private_test_marker=synthetic-query', b'{}')[0],range(8)))


def test_shared_limits_across_workers_and_client_processes_then_recover(ingress):
    port, container = ingress[0][0], ingress[1]
    time.sleep(5)
    processes = docker('top',container,'-eo','pid,args')
    assert processes.count('nginx: worker process') == 2
    with ProcessPoolExecutor(max_workers=2) as pool:
        codes = sum(list(pool.map(parallel_writes, [port,port])),[])
    assert 429 in codes and 200 in codes
    assert codes.count(200) <= 10 # shared burst8 + leader + refill, NOT16 per worker
    logs = subprocess.run(['docker','logs',container],capture_output=True,text=True,check=True)
    assert 'private_test_marker' not in logs.stdout + logs.stderr
    time.sleep(2)
    assert request(port,'POST','/jobs',b'{}')[0] == 200


def test_polling_excluded_from_low_write_limit(ingress):
    port = ingress[0][0]
    time.sleep(2)
    assert [request(port,'GET',f'/jobs/{JOB}')[0] for _ in range(15)] == [200]*15


def test_event_route_streams_before_completion_and_forwards_replay_cursor(ingress):
    connection = http.client.HTTPConnection('127.0.0.1', ingress[0][0], timeout=4)
    try:
        started = time.monotonic()
        connection.request('GET', f'/jobs/{JOB}/events?after=0', headers={
            'X-Project-Copilot-Proxy-Secret': SECRET,
            'Last-Event-ID': '0', 'Accept': 'text/event-stream',
        })
        response = connection.getresponse()
        assert response.status == 200
        assert response.getheader('Content-Type') == 'text/event-stream'
        first = response.read1(8192)
        assert b'event: progress' in first and b'event: terminal' not in first
        assert b'"Last-Event-ID": "0"' in first
        assert time.monotonic() - started < .8
        assert b'event: terminal' in response.read()
    finally:
        connection.close()
    assert request(ingress[0][0], 'POST', f'/jobs/{JOB}/events', b'{}')[0] == 404


def test_upstream_disconnect_and_accelerated_timeout_are_sanitized(ingress):
    port = ingress[0][1]
    for path in ('/health?disconnect=1','/health?delay=2'):
        status, _, body = request(port,'GET',path)
        assert status == 503
        assert SECRET.encode() not in body and b'api:8002' not in body and b'nginx' not in body


def test_write_connection_budget_shared_and_polling_still_works(ingress):
    port = ingress[0][0]
    time.sleep(5)
    with ThreadPoolExecutor(max_workers=8) as pool:
        pending = [pool.submit(request, port, 'POST', '/jobs?delay=1', b'{}') for _ in range(8)]
        time.sleep(.2)
        assert request(port,'GET',f'/jobs/{JOB}')[0] == 200
        codes = [item.result()[0] for item in pending]
    assert codes.count(200) <= 4 and 429 in codes


def test_real_three_network_boundary_denies_tunnel_direct_api(tmp_path):
    name = 'pc-ingress-topology-' + uuid4().hex[:12]
    networks = [name + suffix for suffix in ('-backend', '-egress', '-ingress')]
    containers = []
    for i, network in enumerate(networks):
        docker('network', 'create', *(['--internal'] if i == 0 else []), network)
    try:
        api, entry = name + '-api', name + '-ingress'
        docker('run','-d','--rm','--name',api,'--network',networks[0],'--network-alias','api',
               '-e','PROJECT_COPILOT_PUBLIC_DEMO=true',
               '-e',f'PROJECT_COPILOT_PUBLIC_PROXY_SECRET={SECRET}',
               '-e','PROJECT_COPILOT_AUTH_PUBLIC_ORIGIN=https://synthetic.edgeone.cool',
               APP_IMAGE)
        containers.append(api)
        docker('network','connect',networks[1],api)
        docker('run','-d','--rm','--name',entry,'--network',networks[0],
               '--read-only','--tmpfs','/tmp:size=64m,uid=101,gid=101,mode=700',
               'project-copilot-ingress:default-domain')
        containers.append(entry)
        docker('network','connect','--alias','ingress',networks[2],entry)
        probe = '''import socket,urllib.request,urllib.error,json
try:
 socket.getaddrinfo('api',8002)
 raise SystemExit('direct API unexpectedly resolvable')
except socket.gaierror: pass
try: urllib.request.urlopen('http://ingress:8080/health',timeout=3)
except urllib.error.HTTPError as e: assert e.code==403
else: raise SystemExit('missing proxy secret unexpectedly admitted')
try:
 urllib.request.urlopen(urllib.request.Request('http://ingress:8080/health',headers={'X-Project-Copilot-Proxy-Secret':'synthetic-proxy-header-for-test-only'}),timeout=3)
except urllib.error.HTTPError as e: assert e.code==503
else: raise SystemExit('unconfigured real API unexpectedly admitted')
print(json.dumps({'direct_api_dns':'denied','ingress_missing_secret':403,'real_api_fail_closed':503}))'''
        time.sleep(2)
        direct_probe = '''import urllib.request,urllib.error,time,json
for attempt in range(200):
 try:
  urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:8002/health',headers={'X-Project-Copilot-Proxy-Secret':'synthetic-proxy-header-for-test-only'}),timeout=1)
 except urllib.error.HTTPError as e:
  assert e.code==503
  assert json.loads(e.read())['detail']=='公开服务尚未完成安全验收'
  break
 except urllib.error.URLError: time.sleep(.1)
 else: raise SystemExit('real API unexpectedly admitted')
else: raise SystemExit('real API never became ready for fail-closed probe')'''
        docker('exec',api,'python','-c',direct_probe)
        result = docker('run','--rm','--network',networks[2],
                        APP_IMAGE,'python','-c',probe)
        assert json.loads(result) == {'direct_api_dns':'denied','ingress_missing_secret':403,'real_api_fail_closed':503}
        data = json.loads(docker('inspect',api,entry))
        assert set(data[0]['NetworkSettings']['Networks']) == set(networks[:2])
        assert set(data[1]['NetworkSettings']['Networks']) == {networks[0],networks[2]}
    finally:
        for container in reversed(containers):
            subprocess.run(['docker','stop','--time','3',container],capture_output=True)
        for network in networks:
            subprocess.run(['docker','network','rm',network],capture_output=True)
