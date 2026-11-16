import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from runtime.github_api import GitHub, GitHubError, NoRedirect


class GitHubTests(unittest.TestCase):
    def client(self, write=False):
        return GitHub({'repository': 'owner/repo'}, write=write)

    def test_scope_and_readonly_before_token_access(self):
        client = self.client()
        with patch.object(client, '_installation_token', side_effect=AssertionError('must not authenticate')):
            for path in ('https://evil.example/repos/owner/repo', '/repos/owner/repo2/a', '/repos/other/repo',
                         '/repos/owner/repo/../other', '/repos/owner/repo/%2e%2e/a',
                         '/repos/owner/repo/%252e%252e/a', '/repos/owner/repo/a#fragment',
                         '/repos/owner/repo/\\evil'):
                with self.subTest(path=path), self.assertRaises(GitHubError):
                    client.request('GET', path)
            with self.assertRaises(GitHubError): client.request('POST', '/repos/owner/repo/pulls', {})

    def test_request_interface_and_memory_only_git_auth(self):
        client = self.client(True)
        with patch.object(client, '_installation_token', return_value='secret'), patch.object(client, '_json', return_value={'ok': True}) as request:
            self.assertEqual(client.request('GET', 'repos/owner/repo/commits/main'), {'ok': True})
            request.assert_called_once_with('GET', '/repos/owner/repo/commits/main', None, 'secret')
            env = client.git_env()
        self.assertEqual(env['GIT_CONFIG_COUNT'], '2')
        self.assertEqual(env['GIT_CONFIG_VALUE_0'], 'Authorization: Basic ' + base64.b64encode(b'x-access-token:secret').decode())
        self.assertNotIn('GITHUB_APP_PRIVATE_KEY', env)
        self.assertEqual(env['GIT_CONFIG_VALUE_1'], '')

    def test_real_openssl_jwt_and_narrow_installation_permissions(self):
        with tempfile.TemporaryDirectory() as folder:
            key = Path(folder) / 'key.pem'
            subprocess.run(['openssl', 'genpkey', '-algorithm', 'RSA', '-pkeyopt', 'rsa_keygen_bits:2048', '-out', str(key)], check=True, capture_output=True, timeout=10)
            env = {'GITHUB_APP_ID': '123', 'GITHUB_APP_INSTALLATION_ID': '456', 'GITHUB_APP_PRIVATE_KEY': key.read_text().replace('\n', '\\n')}
            for write in (False, True):
                client = self.client(write)
                def exchange(method, path, payload, jwt):
                    self.assertEqual(path, '/app/installations/456/access_tokens')
                    mode = 'write' if write else 'read'
                    self.assertEqual(payload, {'repositories': ['repo'], 'permissions': {'contents': mode, 'pull_requests': mode, 'checks': 'read', 'statuses': 'read'}})
                    header, claims, signature = jwt.split('.')
                    decode = lambda x: base64.urlsafe_b64decode(x + '=' * (-len(x) % 4))
                    self.assertEqual(json.loads(decode(header))['alg'], 'RS256')
                    self.assertEqual(json.loads(decode(claims))['iss'], '123')
                    sig = Path(folder) / 'sig'; sig.write_bytes(decode(signature))
                    pub = Path(folder) / 'pub'
                    pub.write_bytes(subprocess.check_output(['openssl', 'pkey', '-in', str(key), '-pubout'], timeout=10))
                    verify = subprocess.run(['openssl', 'dgst', '-sha256', '-verify', str(pub), '-signature', str(sig)], input=(header + '.' + claims).encode(), capture_output=True, timeout=10)
                    self.assertEqual(verify.returncode, 0)
                    return {'token': 'ephemeral', 'expires_at': '2099-01-01T00:00:00Z', 'repositories': [{'full_name': 'owner/repo'}]}
                with patch.dict(os.environ, env), patch.object(client, '_json', side_effect=exchange) as request:
                    self.assertEqual(client._installation_token(), 'ephemeral')
                    self.assertEqual(client._installation_token(), 'ephemeral')
                    self.assertEqual(request.call_count, 1)

    def test_pagination_all_pages_and_incomplete_check_count(self):
        client = self.client()
        with patch.object(client, 'request', side_effect=[list(range(100)), [100]]) as request:
            self.assertEqual(len(client.paginate('/repos/owner/repo/pulls/files')), 101)
            self.assertIn('page=2', request.call_args.args[1])
        with patch.object(client, 'request', return_value={'check_runs': [], 'total_count': 10}):
            with self.assertRaises(GitHubError): client.paginate('/repos/owner/repo/checks', key='check_runs')
        with patch.object(client, 'request', return_value=list(range(100))):
            with self.assertRaises(GitHubError): client.paginate('/repos/owner/repo/files', max_pages=1)

    def test_redirects_and_errors_do_not_expose_secrets(self):
        with self.assertRaises(GitHubError): NoRedirect().redirect_request(None, None, 302, '', {}, 'https://evil.example')
        client = self.client()
        for error in (HTTPError('secret', 403, 'secret', {}, None), URLError('secret')):
            with patch.object(client._opener, 'open', side_effect=error):
                with self.assertRaises(GitHubError) as caught:
                    client._json('GET', '/repos/owner/repo', None, 'secret')
                self.assertNotIn('secret', str(caught.exception))

    def test_shared_deadline_recalculated_after_signing_and_token_exchange(self):
        from contextlib import nullcontext
        clock = [100.0]
        client = GitHub({'repository': 'owner/repo', '_deadline': 120.0}, write=True)
        budgets = []
        def sign(*args, **kwargs):
            budgets.append(('sign', kwargs['timeout']))
            clock[0] += 3
            return subprocess.CompletedProcess([], 0, b'signature', b'')
        def open_request(request, timeout):
            budgets.append((request.method, timeout))
            if request.method == 'POST':
                clock[0] += 4
                payload = dict(token='ephemeral', expires_at='2099-01-01T00:00:00Z')
            else:
                payload = dict(sha='a' * 40)
            response = Mock()
            response.geturl.return_value = request.full_url
            response.read.return_value = json.dumps(payload).encode()
            return nullcontext(response)
        env = dict(GITHUB_APP_ID='123', GITHUB_APP_INSTALLATION_ID='456', GITHUB_APP_PRIVATE_KEY='fake key')
        with patch.dict(os.environ, env), patch('runtime.github_api.time.monotonic', side_effect=lambda: clock[0]), \
             patch('runtime.github_api.subprocess.run', side_effect=sign), patch.object(client._opener, 'open', side_effect=open_request):
            self.assertEqual(client.request('GET', '/repos/owner/repo/commits/main'), dict(sha='a' * 40))
        self.assertEqual(budgets, [('sign', 20.0), ('POST', 17.0), ('GET', 13.0)])

    def test_deadline_and_invalid_configuration(self):
        with self.assertRaises(Exception): GitHub({'repository': 'owner/../repo'})
        client = GitHub({'repository': 'owner/repo', '_deadline': time.monotonic() - 1})
        with self.assertRaises(Exception): client._timeout()
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(GitHubError): self.client()._installation_token()


if __name__ == '__main__': unittest.main()
