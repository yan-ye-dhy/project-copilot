"""Structural contracts; runtime guarantees are covered by the Docker suite."""
from pathlib import Path
import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_ingress_network_never_connects_tunnel_directly_to_api():
    config = yaml.safe_load((ROOT / 'docker-compose.public-demo.yml').read_text())
    services = config['services']
    assert set(services['api']['networks']) == {'backend', 'egress'}
    assert services['cloudflared']['networks'] == ['ingress']
    assert set(services['ingress']['networks']) == {'backend', 'ingress'}
    assert services['cloudflared']['command'][-1] == 'http://ingress:8080'
    assert services['cloudflared']['depends_on']['ingress']['condition'] == 'service_healthy'
    assert all(not service.get('ports') for service in services.values())
    assert services['ingress']['read_only']
    assert services['ingress']['cap_drop'] == ['ALL']
    assert services['ingress']['user'] == '101:101'
    assert services['ingress']['mem_limit'] == '128m'
    assert 'X-Project-Copilot-Proxy-Secret' in services['ingress']['healthcheck']['test'][1]
    assert 'http://127.0.0.1:8080/health' in services['ingress']['healthcheck']['test'][1]


def test_shared_limit_keys_do_not_trust_forwarded_client_identity():
    source = (ROOT / 'deploy/nginx/public-demo.conf').read_text()
    assert 'worker_processes 2;' in source
    assert 'error_log /dev/null crit;' in source
    assert 'access_log off;' in source
    assert 'limit_req_zone $global_key zone=all_requests:1m rate=30r/s;' in source
    assert 'limit_req_zone $write_key zone=write_requests:1m rate=2r/s;' in source
    assert 'map $request_method $write_key { default ""; POST public; DELETE public; }' in source
    assert 'limit_conn all_connections 24;' in source
    assert 'limit_conn write_connections 4;' in source
    assert 'proxy_pass_request_headers off;' in source
    assert 'proxy_cache off;' in source
    assert 'proxy_next_upstream off;' in source
    assert 'client_max_body_size 4259840;' in source
    assert 'proxy_read_timeout 110s;' in source
    assert 'proxy_set_header X-Forwarded-For' not in source
    assert 'proxy_set_header CF-Connecting-IP' not in source
    assert 'proxy_set_header Cookie $http_cookie;' in source
    assert 'proxy_set_header X-CSRF-Token $http_x_csrf_token;' in source
    assert 'return 403' in source


def test_ingress_image_version_and_digest_are_fixed():
    source = (ROOT / 'deploy/nginx/Dockerfile').read_text()
    assert 'nginx:1.30.6-alpine@sha256:0e52bd054126cdc040b1276c0af347aeb02a41b9cfe305d6094767ac30e97949' in source
    assert ':latest' not in source
    assert 'USER 101:101' in source
