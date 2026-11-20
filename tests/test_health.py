import copy
import json
import os
import socket
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import MagicMock, patch

from shared_ci import health
from shared_ci.common import PipelineError


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/redirect':
            self.send_response(302); self.send_header('Location', '/ready'); self.end_headers(); return
        self.send_response(200 if self.path == '/ready' else 503)
        self.send_header('Content-Type', 'application/json'); self.send_header('X-Release', 'fixture-v1'); self.end_headers()
        self.wfile.write(b'{"ready":true,"build":{"id":"fixture-v1"}}')
    def log_message(self, *args): pass


class HealthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True); cls.thread.start()
        cls.url = 'http://127.0.0.1:' + str(cls.server.server_port)
    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close(); cls.thread.join(timeout=2)
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.check = {'kind': 'http', 'url': self.url + '/ready', 'headers': {'x-release': 'fixture-v1'}, 'body_contains': ['fixture-v1'], 'json_equals': [{'path': ['build', 'id'], 'value': 'fixture-v1'}]}
        self.config = {'schema': 1, 'checks': [self.check], 'attempts': 1, 'receipt': 'health.json'}
    def test_real_http_checks_headers_body_json_and_records_only_hash(self):
        result = health.execute(self.config, self.root)
        self.assertEqual(result['status'], 'passed'); self.assertEqual(result['checks'][0]['status_code'], 200)
        self.assertNotIn('fixture-v1', (self.root / 'health.json').read_text())
    def test_status_body_header_json_redirect_and_size_fail(self):
        changes = [{'url': self.url + '/broken'}, {'url': self.url + '/redirect'}, {'body_contains': ['missing']}, {'headers': {'x-release': 'wrong'}}, {'json_equals': [{'path': ['missing'], 'value': True}]}, {'max_bytes': 4}]
        for change in changes:
            config = copy.deepcopy(self.config); config['checks'][0].update(change)
            with self.subTest(change=change), self.assertRaises(PipelineError): health.execute(config, self.root)
    def test_private_target_dns_and_expected_address(self):
        result = health.execute({'schema': 1, 'attempts': 1, 'checks': [{'kind': 'dns', 'host': 'localhost', 'expected_addresses': ['127.0.0.1']}]}, self.root)
        self.assertIn('127.0.0.1', result['checks'][0]['addresses'])
        with patch('shared_ci.health.socket.getaddrinfo', return_value=[]), self.assertRaises(PipelineError):
            health._dns({'kind': 'dns', 'host': 'example.invalid'}, .1)
    def test_dns_deadline_does_not_block_caller(self):
        def slow(*args, **kwargs): time.sleep(.3); return []
        start = time.monotonic()
        with patch('shared_ci.health.socket.getaddrinfo', side_effect=slow), self.assertRaisesRegex(PipelineError, 'deadline'):
            health._dns({'kind': 'dns', 'host': 'example.invalid'}, .02)
        self.assertLess(time.monotonic() - start, .2)
    def test_retry_count_is_bounded(self):
        self.config['attempts'] = 3; self.config['retry_seconds'] = 0
        with patch('shared_ci.health._http', side_effect=PipelineError('unavailable')) as probe, self.assertRaises(PipelineError):
            health.execute(self.config, self.root)
        self.assertEqual(probe.call_count, 3)
    def test_credentials_require_trusted_context_and_https(self):
        self.check['header_env'] = {'Authorization': 'HEALTH_AUTH'}
        with patch.dict(os.environ, {'HEALTH_AUTH': 'synthetic'}), patch('shared_ci.health.trusted_context', return_value={}) as trust:
            with self.assertRaisesRegex(PipelineError, 'HTTPS'): health.execute(self.config, self.root)
            trust.assert_called_once()
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(PipelineError): health.execute(self.config, self.root)
    def test_tls_verification_and_expiry(self):
        context = MagicMock(); secured = context.wrap_socket.return_value.__enter__.return_value
        secured.getpeercert.return_value = {'notAfter': 'Jan  1 00:00:00 2099 GMT'}
        with patch('shared_ci.health.ssl.create_default_context', return_value=context), patch('shared_ci.health.socket.create_connection'):
            result = health._tls({'kind': 'tls', 'host': 'example.invalid'}, 1, self.root)
            self.assertIn('2099', result['expires_at']); self.assertEqual(context.wrap_socket.call_args.kwargs['server_hostname'], 'example.invalid')
            secured.getpeercert.return_value = {'notAfter': 'Jan  1 00:00:00 2000 GMT'}
            with self.assertRaisesRegex(PipelineError, 'expiry'): health._tls({'kind': 'tls', 'host': 'example.invalid'}, 1, self.root)
    def test_empty_unknown_and_secret_url_rejected(self):
        for check in [[], [{'kind': 'unknown'}], [{'kind': 'http', 'url': self.url + '/ready?token=synthetic'}]]:
            with self.subTest(check=check), self.assertRaises(PipelineError): health.execute({'schema': 1, 'attempts': 1, 'checks': check}, self.root)


if __name__ == '__main__': unittest.main()
