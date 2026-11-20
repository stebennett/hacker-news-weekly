"""Publish validated weekly CONTENT only, through a SHA-guarded PR, never main push.

Settings: automatic_content_publication=true, repository, production_branch=main,
metadata_root, node (absolute pinned executable), npm (absolute executable),
timeout (API operation), publish_timeout (<600 seconds), optional workspace_root,
required_checks (default ["validate"]), cloudflare_app_id and poll_interval.
Metadata is adaptLegacyReviewed metadata plus reviewedSha256, collectionSha256,
selectedSha256, taggingPath and taggingSha256. These bindings are private.
Optional context.metadataSha256 binds exact metadata bytes; snapshots are read
once and copied outside the checkout. Only revision 1 new issues are automated.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from urllib.parse import quote, urlencode

from .github_api import GitHub, GitHubError, repository
from .orchestrator import Issue, RuntimeError, atomic, digest, instant, secure_directory
from .site_verify import parity
from . import tagging

SHA = re.compile(r'[0-9a-f]{40}')
PUBLIC_METADATA = {'issueId', 'scheduledAt', 'collectedAt', 'revision', 'qualifyingCounts', 'itemSources', 'ordinaryRankings'}
BINDINGS = {'reviewedSha256', 'collectionSha256', 'selectedSha256', 'taggingSha256'}
PRIVATE_METADATA = {'taggingPath'}


def sha(value):
    if not isinstance(value, str) or not SHA.fullmatch(value):
        raise RuntimeError('invalid GitHub commit SHA')
    return value


def snapshot(settings, context):
    issue = Issue.resolve(context['issueId'])
    if (type(context['revision']) is not int or context['revision'] != 1 or
            instant(context['start']) != instant(issue.start) or instant(context['end']) != instant(issue.end)):
        raise RuntimeError('automatic publication requires frozen initial weekly issue/window')
    raw = Path(context['reviewed']).read_bytes()
    if digest(raw) != context['reviewedSha256']:
        raise RuntimeError('frozen reviewed digest mismatch')
    legacy = json.loads(raw)
    if len(legacy.get('sections', [])) != 4:
        raise RuntimeError('four reviewed sections required')
    path = Path(settings['metadata_root']) / (issue.issue_id + '.json')
    if path.is_symlink():
        raise RuntimeError('symlink publication metadata refused')
    metadata_raw = path.read_bytes()
    if context.get('metadataSha256', digest(metadata_raw)) != digest(metadata_raw):
        raise RuntimeError('frozen metadata digest mismatch')
    metadata = json.loads(metadata_raw)
    if set(metadata) != PUBLIC_METADATA | BINDINGS | PRIVATE_METADATA:
        raise RuntimeError('unknown or missing publication metadata fields')
    for field in BINDINGS:
        if not isinstance(metadata[field], str) or not re.fullmatch(r'[0-9a-f]{64}', metadata[field]):
            raise RuntimeError('invalid publication checksum binding')
        if field in context and context[field] != metadata[field]:
            raise RuntimeError('publication checksum binding mismatch')
    if (metadata['reviewedSha256'] != digest(raw) or metadata['issueId'] != issue.issue_id or
            type(metadata['revision']) is not int or metadata['revision'] != context['revision'] or
            instant(metadata['scheduledAt']) != instant(issue.end)):
        raise RuntimeError('publication metadata differs from frozen review')
    if not isinstance(metadata['taggingPath'], str) or not Path(metadata['taggingPath']).is_absolute():
        raise RuntimeError('tagging: absolute private sidecar path required')
    tag_raw = tagging.read_private_bytes(metadata['taggingPath'])
    if digest(tag_raw) != metadata['taggingSha256']:
        raise RuntimeError('tagging: frozen sidecar digest mismatch')
    selected_ids = {item['id'] for section in legacy['sections'] for item in section['items']}
    tagging.validate_tagging(tag_raw, issue, digest(raw), selected_ids)
    # Preserve only the strict importer projection. Private bindings, registry
    # snapshot and delivery context never enter imported public issue JSON.
    return raw, {key: metadata[key] for key in PUBLIC_METADATA}, tag_raw


def allowed_paths(paths, issue, selected_ids=()):
    canonical = f'data/issues/{issue}.json'
    if canonical not in paths or not any(p.startswith('dist/') for p in paths):
        raise RuntimeError('content changes must include canonical issue and generated dist')
    for path in paths:
        story = re.fullmatch(r'data/stories/([1-9][0-9]*)\.json', path)
        if ('\\' in path or any(p in ('', '.', '..') for p in path.split('/')) or path.startswith('/') or
                not (path == canonical or path.startswith('dist/') or path == 'data/tags.json' or
                     story and story[1] in selected_ids)):
            raise RuntimeError('non-content changed path refused')


GITHUB_ACTIONS_ID = 15368


def required_checks(settings):
    names = settings.get('required_checks', ['validate'])
    if not isinstance(names, list) or not names or any(not isinstance(n, str) or not n for n in names):
        raise RuntimeError('explicit nonempty required checks required')
    return set(names)


def strict_policy(github, settings):
    """Read effective main rules with the SAME write-enabled merge identity.

    Merge's sha is a head guard, not base CAS. Only GitHub's strict checks can
    reject a stale base atomically. Missing bypass_actors proves nothing: GitHub
    omits it for readers, so require explicit current_user_can_bypass=never.
    """
    blocker = ('automatic publication blocked: configure an active main branch ruleset with strict '
               'required checks pinned to GitHub Actions integration 15368 and no publisher App bypass; '
               'readback must confirm current_user_can_bypass=never')
    try:
        names = required_checks(settings)
        if github.write is not True:
            raise ValueError()
        prefix = '/repos/' + repository(settings)
        rules = github.paginate(prefix + '/rules/branches/main')
        covered = set()
        for rule in rules:
            if rule['type'] != 'required_status_checks':
                continue
            identifier = rule['ruleset_id']
            if type(identifier) is not int or identifier <= 0:
                raise ValueError()
            params = rule['parameters']
            if params.get('strict_required_status_checks_policy') is not True:
                raise ValueError()
            checks = params['required_status_checks']
            if not isinstance(checks, list) or not checks:
                raise ValueError()
            details = github.request('GET', prefix + '/rulesets/' + str(identifier))
            if (details['id'] != identifier or details['enforcement'] != 'active' or
                    details['target'] != 'branch' or details.get('current_user_can_bypass') != 'never' or
                    not any(r['type'] == 'required_status_checks' and r.get('parameters') == params
                            for r in details['rules'])):
                raise ValueError()
            for check in checks:
                if check['context'] in names:
                    if type(check.get('integration_id')) is not int or check['integration_id'] != GITHUB_ACTIONS_ID:
                        raise ValueError()
                    covered.add(check['context'])
        if covered != names:
            raise ValueError()
    except (RuntimeError, KeyError, TypeError, ValueError, AttributeError):
        raise RuntimeError(blocker) from None


class Publisher:
    def __init__(self, settings, context):
        if settings.get('automatic_content_publication') is not True:
            raise RuntimeError('explicit automatic_content_publication=true required')
        self.settings = dict(settings)
        self.context = {k: context[k] for k in ('issueId', 'revision', 'reviewedSha256', 'start', 'end', 'reviewed')}
        self.reviewed, self.metadata, self.tagging = snapshot(settings, context)
        self.tagging_sha256 = digest(self.tagging)
        self.selected_ids = set(json.loads(self.tagging)['assignments'])
        duration = float(settings.get('publish_timeout', 540))
        if not 0 < duration < 600:
            raise RuntimeError('publication deadline must be below parent 600 seconds')
        self.deadline = time.monotonic() + duration
        self.settings['_deadline'] = self.deadline
        self.github = GitHub(self.settings, write=True)
        self.repo = repository(settings)
        self.prefix = '/repos/' + self.repo
        self.issue = context['issueId']
        if settings.get('production_branch', 'main') != 'main':
            raise RuntimeError('automatic content publication is restricted to production main')
        self.cwd = None
        # Do not expose App credentials or delivery settings to npm lifecycle scripts.
        self.env = {k: os.environ[k] for k in ('PATH', 'HOME', 'TMPDIR', 'LANG', 'LC_ALL', 'SSL_CERT_FILE') if k in os.environ}
        self.env.update(CI='true', GIT_TERMINAL_PROMPT='0', GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL='/dev/null')
        self.env['TWIHN_SOURCE_REVISION'] = 'unversioned'

    def remaining(self, limit=30):
        value = min(float(limit), self.deadline - time.monotonic())
        if value <= 0:
            raise RuntimeError('publication deadline exceeded')
        return value

    def run(self, argv, *, git=False, limit=120):
        env = dict(self.env)
        if git:
            env.update(self.github.git_env())
        # Children remain in the parent's supervised process group, never detach.
        try:
            with tempfile.TemporaryFile() as stdout:
                result = subprocess.run(argv, cwd=self.cwd, env=env, stdin=subprocess.DEVNULL,
                                        stdout=stdout, stderr=subprocess.DEVNULL, timeout=self.remaining(limit), umask=0o022)
                if result.returncode:
                    # Source/script output can contain private inputs. Do not forward it.
                    raise RuntimeError('publication subprocess failed: ' + Path(argv[0]).name)
                stdout.seek(0)
                output = stdout.read(20 * 1024 * 1024 + 1)
        except (OSError, subprocess.TimeoutExpired):
            raise RuntimeError('publication subprocess failed or exceeded deadline') from None
        if len(output) > 20 * 1024 * 1024:
            raise RuntimeError('publication subprocess output too large')
        return output

    def git(self, *args):
        return self.run(['git', *args], git=True, limit=45)

    def api(self, method, path, payload=None):
        self.remaining()
        return self.github.request(method, self.prefix + path, payload)

    def content(self, commit, path):
        response = self.api('GET', '/contents/' + quote(path, safe='/') + '?ref=' + sha(commit))
        if response.get('type') != 'file' or response.get('encoding') != 'base64':
            raise RuntimeError('exact GitHub file readback missing')
        raw = base64.b64decode(response['content'])
        expected = __import__('hashlib').sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest()
        if response.get('sha') != expected:
            raise RuntimeError('GitHub blob checksum mismatch')
        return raw

    def current(self):
        return sha(self.api('GET', '/commits/main')['sha'])

    def verify_issue(self, commit, expected):
        for path in (f'data/issues/{self.issue}.json', f'dist/data/issues/{self.issue}.json'):
            value = json.loads(self.content(commit, path))
            parity(value, self.context)
            if value != expected:
                raise RuntimeError('canonical public metadata/content differs from frozen import')

    def verify_tagging_copy(self, path):
        if tagging.read_private_bytes(path) != self.tagging:
            raise RuntimeError('frozen tagging copy differs from validated snapshot')

    def prepare(self, root):
        root = secure_directory(root)
        frozen_tagging = root / 'tagging.json'
        atomic(frozen_tagging, self.tagging)
        self.cwd = root / 'checkout'
        self.cwd.mkdir()
        self.git('init', '--quiet')
        self.git('remote', 'add', 'origin', 'https://github.com/' + self.repo + '.git')
        self.base = self.current()
        self.git('fetch', '--depth=1', 'origin', 'refs/heads/main')
        if self.git('rev-parse', 'FETCH_HEAD').decode().strip() != self.base:
            raise RuntimeError('production main moved during checkout; retry')
        self.git('checkout', '--detach', self.base)
        package = json.loads((self.cwd / 'package.json').read_bytes())
        node = str(Path(self.settings['node']).resolve())
        npm = str(Path(self.settings['npm']).absolute())
        if not Path(self.settings['node']).is_absolute() or not Path(self.settings['npm']).is_absolute() or not Path(npm).is_file():
            raise RuntimeError('absolute pinned node/npm executables required')
        self.env['PATH'] = str(Path(node).parent) + os.pathsep + self.env.get('PATH', '')
        if self.run([node, '--version']).decode().strip() != 'v' + (self.cwd / '.node-version').read_text().strip():
            raise RuntimeError('Node version differs from production pin')
        if self.run([npm, '--version']).decode().strip() != package['engines']['npm']:
            raise RuntimeError('npm version differs from production pin')
        if package['engines']['node'] != (self.cwd / '.node-version').read_text().strip():
            raise RuntimeError('inconsistent production toolchain pins')
        self.run([npm, 'ci', '--no-audit', '--no-fund'], limit=180)
        frozen = root / 'reviewed.json'
        frozen.write_bytes(self.reviewed)
        self.context['reviewed'] = str(frozen)
        metadata = root / 'metadata.json'
        metadata.write_text(json.dumps(self.metadata))
        generated = root / 'canonical.json'
        self.run([node, 'scripts/import-legacy.mjs', str(frozen), str(metadata), str(generated)])
        expected = json.loads(generated.read_bytes())
        parity(expected, self.context)
        destination = self.cwd / f'data/issues/{self.issue}.json'
        validate_tags = [node, 'scripts/verify-tag-changes.mjs', '--base', self.base,
                         '--sidecar', str(frozen_tagging), '--issue', str(generated)]
        if destination.exists():
            if json.loads(destination.read_bytes()) != expected:
                raise RuntimeError('existing issue immutable; changed content requires human correction')
            # Read-only semantic compatibility with the complete existing archive;
            # exact production tag artifact readback is a separate verification gate.
            self.verify_tagging_copy(frozen_tagging)
            self.run([*validate_tags, '--idempotent'])
            self.verify_issue(self.base, expected)
            return expected, True
        destination.write_bytes(generated.read_bytes())
        destination.chmod(0o644)
        self.verify_tagging_copy(frozen_tagging)
        self.run([node, 'scripts/tagging.mjs', 'apply', '--sidecar', str(frozen_tagging),
                  '--issue', str(generated), '--data-dir', str(self.cwd / 'data')])
        for command in ([npm, 'run', 'check'], [npm, 'test'], [npm, 'run', 'build'], [npm, 'run', 'verify:dist']):
            self.run(command, limit=180)
        # Stage everything, then inspect the COMPLETE index (including generated files).
        self.git('add', '-A')
        self.paths = self.git('diff', '--cached', '--name-only', '-z').decode().rstrip('\0').split('\0')
        allowed_paths(self.paths, self.issue, self.selected_ids)
        statuses = self.git('diff', '--cached', '--name-status', '-z').decode().split('\0')
        if any(statuses[i] not in ('A', 'M') for i in range(0, len(statuses) - 1, 2)):
            raise RuntimeError('deletion/rename of retained content refused')
        modes = self.git('ls-files', '--stage', '-z').decode().rstrip('\0').split('\0')
        if any(line.split(' ', 1)[0] != '100644' for line in modes if line.split('\t', 1)[-1] in self.paths):
            raise RuntimeError('nonregular content file refused')
        for path in self.paths:
            if path.startswith('dist/data/issues/') or path.startswith('dist/data/revisions/'):
                old = self.run(['git', 'ls-tree', self.base, '--', path]).decode()
                if old and path != f'dist/data/issues/{self.issue}.json':
                    raise RuntimeError('historic issue/revision changed')
        self.blobs = {p: self.git('rev-parse', ':' + p).decode().strip() for p in self.paths}
        # Validate the complete final archive AFTER lifecycle/build scripts;
        # staged blobs must also match this exact validated worktree.
        self.verify_tagging_copy(frozen_tagging)
        self.run(validate_tags)
        self.verify_tagging_copy(frozen_tagging)
        self.git('diff', '--exit-code', '--')
        self.git('-c', 'user.name=TWIHN publisher', '-c', 'user.email=publisher@users.noreply.github.com',
                 '-c', 'core.hooksPath=/dev/null', 'commit', '--quiet', '-m', f'content: weekly issue {self.issue}')
        self.head = sha(self.git('rev-parse', 'HEAD').decode().strip())
        return expected, False

    def check_pr(self, number):
        pr = self.api('GET', f'/pulls/{number}')
        if (pr['head']['sha'] != self.head or pr['head']['ref'] != self.branch or
                pr['head']['repo']['full_name'] != self.repo or pr['base']['ref'] != 'main' or
                pr['base']['sha'] != self.base or pr['base']['repo']['full_name'] != self.repo or
                pr.get('draft') or pr.get('state') != 'open' or pr.get('merged')):
            raise RuntimeError('PR identity/base/head changed; never auto-merge source PR')
        files = self.github.paginate(self.prefix + f'/pulls/{number}/files')
        if pr.get('changed_files') != len(files) or len(files) != len(self.paths):
            raise RuntimeError('incomplete PR changed-file readback')
        actual = {f['filename']: f['sha'] for f in files}
        allowed_paths(actual, self.issue, self.selected_ids)
        if actual != self.blobs or any(f['status'] not in ('added', 'modified') or 'previous_filename' in f for f in files):
            raise RuntimeError('server PR differs from validated complete content file set')
        commit = self.api('GET', '/git/commits/' + self.head)
        if [x['sha'] for x in commit['parents']] != [self.base]:
            raise RuntimeError('content PR must contain only isolated content commit')
        if self.current() != self.base:
            raise RuntimeError('production main moved; rebuild content from current main')
        return pr

    def checks(self):
        runs = self.github.paginate(self.prefix + '/commits/' + self.head + '/check-runs?filter=latest', key='check_runs')
        statuses = self.github.paginate(self.prefix + '/commits/' + self.head + '/statuses')
        latest = {}
        for item in statuses:
            latest.setdefault(item['context'], item)
        for status in latest.values():
            if status['state'] in ('failure', 'error'):
                raise RuntimeError('content CI status failed')
        checks = {}
        for run in sorted(runs, key=lambda x: x['id'], reverse=True):
            if run.get('head_sha') != self.head:
                raise RuntimeError('check head differs from content commit')
            checks.setdefault((run['name'], run.get('app', {}).get('id')), run)
        for run in checks.values():
            if run.get('conclusion') in ('failure', 'cancelled', 'timed_out', 'action_required', 'startup_failure', 'stale'):
                raise RuntimeError('content CI check failed')
        required = required_checks(self.settings)
        validate = [x for x in checks.values() if x['name'] in required and x.get('app', {}).get('id') == GITHUB_ACTIONS_ID]
        if set(x['name'] for x in validate) != set(required):
            return False
        # A Pages preview, if present, must pass too. Local build + verify:dist is
        # the explicit fallback when Pages emits no preview check.
        if not validate or not all(x['status'] == 'completed' and x['conclusion'] == 'success' for x in validate):
            return False
        if any(x['status'] != 'completed' or x.get('conclusion') not in ('success', 'neutral', 'skipped') for x in checks.values()):
            return False
        pages = [x for x in checks.values() if 'cloudflare' in x['name'].lower()]
        if self.settings.get('cloudflare_app_id') is not None and any(str(x.get('app', {}).get('id')) != str(self.settings['cloudflare_app_id']) for x in pages):
            raise RuntimeError('Cloudflare preview check app identity mismatch')
        if any(x.get('conclusion') != 'success' for x in pages):
            return False
        return all(s['state'] == 'success' for s in latest.values())

    def publish(self):
        strict_policy(self.github, self.settings)
        workspace = secure_directory(self.settings['workspace_root']) if self.settings.get('workspace_root') else None
        with tempfile.TemporaryDirectory(prefix='twihn-publish-', dir=workspace) as temporary:
            expected, exists = self.prepare(Path(temporary))
            if exists:
                if self.current() != self.base:
                    raise RuntimeError('production main moved during idempotent readback; retry')
                return {'commit': self.base}
            # Deterministic digest + exact base allows crash recovery without adopting
            # unrelated/source branches. Existing branch must have identical tree/parent.
            self.branch = f'content/weekly-{self.issue}-{self.context["reviewedSha256"][:20]}-{self.tagging_sha256[:20]}-{self.base[:12]}'
            refs = self.git('ls-remote', '--heads', 'origin', 'refs/heads/' + self.branch).decode().strip()
            if refs:
                remote = sha(refs.split()[0])
                self.git('fetch', '--depth=2', 'origin', 'refs/heads/' + self.branch)
                if self.git('rev-parse', remote + '^{tree}') != self.git('rev-parse', self.head + '^{tree}'):
                    raise RuntimeError('content branch collision')
                if self.git('rev-parse', remote + '^').decode().strip() != self.base:
                    raise RuntimeError('content branch parent collision')
                self.head = remote
            else:
                strict_policy(self.github, self.settings)
                self.git('push', 'origin', 'HEAD:refs/heads/' + self.branch)
            ref = self.api('GET', '/git/ref/heads/' + self.branch)
            if ref['object']['sha'] != self.head:
                raise RuntimeError('pushed content ref readback mismatch')
            prs = self.github.paginate(self.prefix + '/pulls?' + urlencode({'state': 'open', 'head': self.repo.split('/')[0] + ':' + self.branch, 'base': 'main'}))
            if len(prs) > 1:
                raise RuntimeError('ambiguous content PR')
            if not prs:
                strict_policy(self.github, self.settings)
            pr = prs[0] if prs else self.api('POST', '/pulls', {'title': f'content: weekly issue {self.issue}',
                'body': 'Validated initial weekly content and deterministic generated site only.',
                'head': self.branch, 'base': 'main', 'draft': False, 'maintainer_can_modify': False})
            number = int(pr['number'])
            while True:
                self.check_pr(number)
                if self.checks():
                    break
                time.sleep(self.remaining(self.settings.get('poll_interval', 3)))
            self.check_pr(number)
            self.verify_issue(self.head, expected)
            strict_policy(self.github, self.settings)
            result = self.api('PUT', f'/pulls/{number}/merge', {'sha': self.head, 'merge_method': 'merge',
                              'commit_title': f'content: weekly issue {self.issue}',
                              'commit_message': 'Validated weekly content publication.'})
            if result.get('merged') is not True:
                raise RuntimeError('content merge refused')
            merged = self.api('GET', f'/pulls/{number}')
            commit = sha(merged.get('merge_commit_sha'))
            if (merged.get('merged') is not True or merged['head']['sha'] != self.head or
                    result.get('sha') != commit):
                raise RuntimeError('merged content PR readback mismatch')
            actual = self.api('GET', '/git/commits/' + commit)
            if [x['sha'] for x in actual['parents']] != [self.base, self.head]:
                raise RuntimeError('merge included unexpected production/source changes')
            if self.current() != commit:
                raise RuntimeError('production merge no longer current; retry readback')
            self.verify_issue(commit, expected)
            return {'commit': commit}


def publish(settings, context):
    return Publisher(settings, context).publish()


def doctor(settings):
    """Read-only scoped authentication/read capability; does not claim write proof."""
    github = GitHub(settings, write=True)
    repo = github.request('GET', '/repos/' + repository(settings))
    result = {'repository': repo['full_name'], 'readable': True, 'writeTested': False,
              'productionCommit': sha(github.request('GET', '/repos/' + repository(settings) + '/commits/main')['sha']),
              'automaticPublicationReady': False}
    try:
        strict_policy(github, settings)
        result['automaticPublicationReady'] = True
    except RuntimeError as exc:
        result['blocker'] = str(exc)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--settings', type=Path, required=True)
    parser.add_argument('--context', type=Path)
    parser.add_argument('--doctor', action='store_true')
    args = parser.parse_args(argv)
    try:
        settings = json.loads(args.settings.read_bytes())
        if not args.doctor and args.context is None:
            raise RuntimeError('--context required for publication')
        result = doctor(settings) if args.doctor else publish(settings, json.loads(args.context.read_bytes()))
        print(json.dumps(result))
        return 0
    except Exception as exc:
        print('weekly publication failed: ' + str(exc), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
