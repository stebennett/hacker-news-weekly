"""Repository-scoped GitHub App client; stdlib + openssl, no persisted tokens."""
from __future__ import annotations

import base64
from datetime import datetime
import json
import os
import re
import subprocess
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlsplit, urlencode, parse_qsl
from urllib.request import Request, build_opener, HTTPRedirectHandler

from .orchestrator import RuntimeError


class GitHubError(RuntimeError):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise GitHubError('GitHub redirect refused', code)


def repository(settings):
    repo = settings['repository']
    if not isinstance(repo, str) or not re.fullmatch(r'[A-Za-z0-9_-][A-Za-z0-9_.-]*/[A-Za-z0-9_-][A-Za-z0-9_.-]*', repo):
        raise RuntimeError('invalid literal repository')
    return repo


class GitHub:
    def __init__(self, settings, write=False):
        self.repository = repository(settings)
        self.write = write
        self.timeout = min(float(settings.get('timeout', 30)), 30)
        if self.timeout <= 0:
            raise RuntimeError('invalid GitHub timeout')
        self.deadline = settings.get('_deadline')
        self._token = None
        self._expires = 0
        self._opener = build_opener(NoRedirect())

    def _timeout(self):
        remaining = self.timeout if self.deadline is None else min(self.timeout, self.deadline - time.monotonic())
        if remaining <= 0:
            raise RuntimeError('GitHub stage deadline exceeded')
        return remaining

    def _json(self, method, path, payload, token) -> Any:
        request = Request('https://api.github.com' + path,
                          data=None if payload is None else json.dumps(payload).encode(), method=method,
                          headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/vnd.github+json',
                                   'X-GitHub-Api-Version': '2022-11-28', 'User-Agent': 'TWIHN-runtime/1',
                                   'Content-Type': 'application/json'})
        try:
            with self._opener.open(request, timeout=self._timeout()) as response:
                if response.geturl() != request.full_url:
                    raise GitHubError('GitHub redirect refused')
                raw = response.read(20 * 1024 * 1024 + 1)
                if len(raw) > 20 * 1024 * 1024:
                    raise GitHubError('GitHub response too large')
                return json.loads(raw) if raw else None
        except HTTPError as exc:
            # Never log response bodies, request headers, JWTs or token payloads.
            raise GitHubError(f'GitHub API HTTP {exc.code}', exc.code) from None
        except (URLError, OSError, ValueError):
            raise GitHubError('GitHub API transport or JSON failure') from None

    def _installation_token(self):
        if self._token and time.time() < self._expires - 60:
            return self._token
        try:
            app = os.environ['GITHUB_APP_ID']
            installation = os.environ['GITHUB_APP_INSTALLATION_ID']
            key = os.environ['GITHUB_APP_PRIVATE_KEY'].replace('\\n', '\n')
            if len(key.encode()) > 8192 or not re.fullmatch(r'[0-9]+', app) or not re.fullmatch(r'[0-9]+', installation):
                raise ValueError()
        except (KeyError, ValueError):
            raise GitHubError('GitHub App environment incomplete or invalid') from None
        encode = lambda raw: base64.urlsafe_b64encode(raw).rstrip(b'=')
        now = int(time.time())
        unsigned = b'.'.join(encode(json.dumps(x, separators=(',', ':')).encode()) for x in
                             ({'alg': 'RS256', 'typ': 'JWT'}, {'iat': now - 60, 'exp': now + 540, 'iss': app}))
        # Anonymous pipe: the PEM never appears in argv, a file, or logs.
        read_fd, write_fd = os.pipe()
        try:
            os.write(write_fd, key.encode())
            os.close(write_fd)
            write_fd = None
            result = subprocess.run(['openssl', 'dgst', '-sha256', '-sign', f'/dev/fd/{read_fd}'],
                                    input=unsigned, capture_output=True, pass_fds=(read_fd,), timeout=self._timeout())
        except (OSError, subprocess.TimeoutExpired):
            raise GitHubError('GitHub App signing failed') from None
        finally:
            os.close(read_fd)
            if write_fd is not None:
                os.close(write_fd)
        if result.returncode:
            raise GitHubError('GitHub App signing failed')
        jwt = (unsigned + b'.' + encode(result.stdout)).decode()
        mode = 'write' if self.write else 'read'
        data = self._json('POST', f'/app/installations/{installation}/access_tokens',
                          {'repositories': [self.repository.split('/')[1]],
                           'permissions': {'contents': mode, 'pull_requests': mode, 'checks': 'read', 'statuses': 'read'}}, jwt)
        try:
            token = data['token']
            expires = datetime.fromisoformat(data['expires_at'].replace('Z', '+00:00')).timestamp()
            if not isinstance(token, str) or not token or expires <= time.time() + 60:
                raise ValueError()
            # Confirm repository restriction returned by GitHub when present.
            if 'repositories' in data and [x['full_name'].lower() for x in data['repositories']] != [self.repository.lower()]:
                raise ValueError()
        except (KeyError, TypeError, ValueError):
            raise GitHubError('invalid scoped installation token response') from None
        self._token, self._expires = token, expires
        return token

    def request(self, method, path, payload=None) -> Any:
        method = method.upper()
        path = '/' + path.lstrip('/')
        parsed = urlsplit(path)
        prefix = '/repos/' + self.repository
        decoded = unquote(parsed.path)
        if (parsed.scheme or parsed.netloc or parsed.fragment or '\\' in path or
                any(ord(c) < 32 for c in path) or '%' in decoded or
                any(p in ('.', '..') for p in decoded.split('/')) or
                not (decoded == prefix or decoded.startswith(prefix + '/'))):
            raise GitHubError('GitHub endpoint outside configured repository')
        if method not in ('GET', 'POST', 'PATCH', 'PUT', 'DELETE') or (method != 'GET' and not self.write):
            raise GitHubError('GitHub write not authorized')
        return self._json(method, path, payload, self._installation_token())

    def paginate(self, path, key=None, max_pages=100):
        parts = urlsplit(path)
        query = dict(parse_qsl(parts.query))
        query['per_page'] = '100'
        output = []
        for page in range(1, max_pages + 1):
            query['page'] = str(page)
            data = self.request('GET', parts.path + '?' + urlencode(query))
            batch = data[key] if key else data
            if not isinstance(batch, list):
                raise GitHubError('invalid paginated GitHub response')
            output.extend(batch)
            if len(batch) < 100:
                if key and 'total_count' in data and len(output) != data['total_count']:
                    raise GitHubError('incomplete paginated GitHub response')
                return output
        raise GitHubError('GitHub pagination limit exceeded')

    def git_env(self):
        """Return credential-bearing child env; never persist or print this mapping."""
        basic = base64.b64encode(('x-access-token:' + self._installation_token()).encode()).decode()
        return {'GIT_TERMINAL_PROMPT': '0', 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null',
                'GIT_CONFIG_COUNT': '2', 'GIT_CONFIG_KEY_0': 'http.https://github.com/.extraheader',
                'GIT_CONFIG_VALUE_0': 'Authorization: Basic ' + basic,
                'GIT_CONFIG_KEY_1': 'credential.helper', 'GIT_CONFIG_VALUE_1': ''}
