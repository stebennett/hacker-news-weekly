import base64
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from runtime import publisher as pub
from runtime.orchestrator import Issue, RuntimeError, digest
from test_runtime import reviewed

BASE, HEAD, MERGED = 'a' * 40, 'b' * 40, 'c' * 40


class PublisherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        issue = Issue.resolve('2026-11-13')
        self.legacy = reviewed(issue)
        path = self.root / 'reviewed.json'; path.write_text(json.dumps(self.legacy))
        self.context = dict(issueId=issue.issue_id, revision=1, start=issue.start, end=issue.end,
                            reviewed=str(path), reviewedSha256=digest(path.read_bytes()),
                            sender='PRIVATE-SENDER', recipient='PRIVATE-RECIPIENT', email='PRIVATE-EMAIL')
        self.metadata = dict(issueId=issue.issue_id, revision=1, scheduledAt=issue.end, collectedAt=issue.end,
                             qualifyingCounts=[10, 10, 10, 0], itemSources={},
                             reviewedSha256=self.context['reviewedSha256'], collectionSha256='d' * 64, selectedSha256='e' * 64)
        self.public = dict(schemaVersion=1, issueId=issue.issue_id, revision=1, scheduledAt=issue.end,
                           collectedAt=issue.end, displayTimezone='Europe/London', window=dict(start=issue.start, end=issue.end), sections=[], items={})
        for group, section in zip(self.legacy['sections'], ['votes', 'comments', 'ask', 'show']):
            self.public['sections'].append(dict(id=section, itemIds=[x['id'] for x in group['items']], qualifyingCount=len(group['items'])))
            for x in group['items']:
                provenance = dict(sources=[dict(kind='story', url=x['url']), dict(kind='discussion', url='https://news.ycombinator.com/item?id=' + x['id'])],
                                  availability=dict(story=dict(status='reviewed', note='Reviewed.'), discussion=dict(status='reviewed', note='Reviewed.')))
                self.metadata['itemSources'][x['id']] = provenance
                self.public['items'][x['id']] = dict(id=x['id'], kind='ask' if section == 'ask' else 'story', title=x['title'],
                    score=x['score'], comments=x['comments'], submittedAt=x['created'], sourceUrl=x['url'],
                    discussionUrl='https://news.ycombinator.com/item?id=' + x['id'], story=x['story'], discussion=x['discussion'], **provenance)
        self.metafile = self.root / (issue.issue_id + '.json'); self.save_metadata()
        self.settings = dict(automatic_content_publication=True, repository='owner/repo', metadata_root=str(self.root), node='/fake/node', npm='/fake/npm', poll_interval=0.001)
        self.patch = patch.object(pub, 'GitHub'); self.github_type = self.patch.start(); self.addCleanup(self.patch.stop)
        self.github = self.github_type.return_value
        self.github.write = True
        self.publisher = pub.Publisher(self.settings, self.context)
        self.paths = [f'data/issues/{issue.issue_id}.json', f'dist/data/issues/{issue.issue_id}.json', 'dist/index.html']
        self.blobs = {p: str(i + 1) * 40 for i, p in enumerate(self.paths)}
        self.calls = []
        self.parameters = dict(strict_required_status_checks_policy=True,
                               required_status_checks=[dict(context='validate', integration_id=15368)])
        self.rules = [dict(type='required_status_checks', ruleset_id=42, parameters=self.parameters)]
        self.ruleset = dict(id=42, enforcement='active', target='branch', current_user_can_bypass='never',
                            rules=[dict(type='required_status_checks', parameters=self.parameters)])
        self.github.paginate.side_effect = self.paginate
        self.github.request.side_effect = lambda method, path, payload=None: self.api(method, path.removeprefix('/repos/owner/repo'), payload)
        self.bad = None
        self.merged = False
        self.collision = False
        self.duplicate = False

    def save_metadata(self): self.metafile.write_text(json.dumps(self.metadata))

    def prepare(self, root):
        p = self.publisher
        p.base, p.head, p.paths, p.blobs = BASE, HEAD, self.paths, self.blobs
        return self.public, False

    def pr(self):
        head = dict(sha=HEAD, ref=self.publisher.branch, repo=dict(full_name='owner/repo'))
        base = dict(sha=BASE, ref='main', repo=dict(full_name='owner/repo'))
        result = dict(number=7, head=head, base=base, changed_files=len(self.paths), draft=False,
                      state='closed' if self.merged else 'open', merged=self.merged, merge_commit_sha=MERGED)
        if self.bad == 'head': result['head']['sha'] = 'f' * 40
        if self.bad == 'base': result['base']['sha'] = 'f' * 40
        if self.bad == 'sourcepr': result['head']['ref'] = 'feature/source'
        if self.bad == 'foreign': result['head']['repo']['full_name'] = 'attacker/repo'
        if self.bad == 'readback': result['merged'] = False
        return result

    def api(self, method, path, payload=None):
        self.calls.append((method, path, payload))
        if path == '/rulesets/42': return self.ruleset
        if path == '/commits/main': return dict(sha=MERGED if self.merged else BASE)
        if '/git/ref/heads/' in path: return dict(object=dict(sha=HEAD))
        if path == '/pulls' and method == 'POST': return dict(number=7)
        if path == '/pulls/7': return self.pr()
        if path == '/pulls/7/merge':
            self.assertEqual(payload['sha'], HEAD)
            self.merged = True
            return dict(merged=True, sha=MERGED)
        if path == '/git/commits/' + HEAD: return dict(parents=[dict(sha=BASE)])
        if path == '/git/commits/' + MERGED:
            return dict(parents=[dict(sha=BASE), dict(sha=HEAD if self.bad != 'mergeparent' else 'f' * 40)])
        raise AssertionError((method, path))

    def paginate(self, path, key=None):
        if '/rules/branches/main' in path: return self.rules
        if '/pulls?' in path: return [dict(number=7)] if self.duplicate else []
        if '/pulls/7/files' in path:
            files = [dict(filename=p, sha=v, status='added') for p, v in self.blobs.items()]
            if self.bad == 'source': files[-1]['filename'] = 'runtime/publisher.py'
            if self.bad == 'blob': files[-1]['sha'] = 'f' * 40
            if self.bad == 'delete': files[-1]['status'] = 'removed'
            if self.bad == 'extra': files.append(dict(filename='dist/extra', sha='f' * 40, status='added'))
            return files
        if '/check-runs?' in path:
            run = dict(id=1, name='validate', head_sha=HEAD, app=dict(id=15368, slug='github-actions'), status='completed', conclusion='success')
            if self.bad == 'ci': run['conclusion'] = 'failure'
            if self.bad == 'checkhead': run['head_sha'] = BASE
            if self.bad == 'missingci': return []
            if self.bad == 'impostor': run['app']['id'] = 1
            if self.bad == 'pages': return [run, dict(id=2, name='Cloudflare Pages', head_sha=HEAD, status='completed', conclusion='failure')]
            return [run]
        if '/statuses' in path: return []
        raise AssertionError(path)

    def git(self, *args):
        self.calls.append(('git', args, None))
        if args[0] == 'ls-remote': return (HEAD + '\trefs/heads/branch').encode() if self.duplicate or self.collision else b''
        if args[0] == 'rev-parse':
            if args[1].endswith('^{tree}'): return b'bad' if self.collision and args[1].startswith(HEAD) else b'tree'
            return BASE.encode()
        return b''

    def publish(self):
        self.github.paginate.side_effect = self.paginate
        with patch.object(self.publisher, 'prepare', side_effect=self.prepare), patch.object(self.publisher, 'git', side_effect=self.git), \
             patch.object(self.publisher, 'api', side_effect=self.api), patch.object(self.publisher, 'verify_issue') as verify:
            result = self.publisher.publish()
        return result, verify

    def test_missing_server_policy_stops_before_checkout_or_any_mutation(self):
        self.rules = []
        with patch.object(self.publisher, 'prepare') as prepare, patch.object(self.publisher, 'git') as git:
            with self.assertRaisesRegex(RuntimeError, 'strict'):
                self.publisher.publish()
        prepare.assert_not_called(); git.assert_not_called()
        self.assertTrue(all(method == 'GET' for method, _, _ in self.calls))

    def test_policy_rejects_untrusted_or_unprovable_server_enforcement(self):
        original = copy.deepcopy((self.rules, self.ruleset))
        cases = ['nonstrict', 'empty', 'wrongcheck', 'wrongapp', 'missingapp', 'missingruleset',
                 'inactive', 'evaluate', 'wrongtarget', 'wrongid', 'uncorroborated',
                 'always', 'pull_requests_only', 'exempt', 'unknown', 'missingbypass', 'omittedstrict']
        for case in cases:
            with self.subTest(case=case):
                self.rules, self.ruleset = copy.deepcopy(original)
                params = self.rules[0]['parameters']
                if case == 'nonstrict': params['strict_required_status_checks_policy'] = False
                if case == 'omittedstrict': params.pop('strict_required_status_checks_policy')
                if case == 'empty': params['required_status_checks'] = []
                if case == 'wrongcheck': params['required_status_checks'][0]['context'] = 'other'
                if case == 'wrongapp': params['required_status_checks'][0]['integration_id'] = 1
                if case == 'missingapp': params['required_status_checks'][0].pop('integration_id')
                if case == 'missingruleset': self.rules[0].pop('ruleset_id')
                if case == 'inactive': self.ruleset['enforcement'] = 'disabled'
                if case == 'evaluate': self.ruleset['enforcement'] = 'evaluate'
                if case == 'wrongtarget': self.ruleset['target'] = 'tag'
                if case == 'wrongid': self.ruleset['id'] = 43
                if case == 'uncorroborated': self.ruleset['rules'] = []
                if case in ('always', 'pull_requests_only', 'exempt', 'unknown'):
                    self.ruleset['current_user_can_bypass'] = case
                if case == 'missingbypass': self.ruleset.pop('current_user_can_bypass')
                with patch.object(self.publisher, 'prepare') as prepare, patch.object(self.publisher, 'git') as git:
                    with self.assertRaisesRegex(RuntimeError, 'strict'):
                        self.publisher.publish()
                prepare.assert_not_called(); git.assert_not_called()

    def test_policy_read_denied_stops_before_checkout(self):
        self.github.paginate.side_effect = pub.GitHubError('GitHub API HTTP 403', 403)
        with patch.object(self.publisher, 'prepare') as prepare, patch.object(self.publisher, 'git') as git:
            with self.assertRaisesRegex(RuntimeError, 'configure'): self.publisher.publish()
        prepare.assert_not_called(); git.assert_not_called()

    def test_policy_rechecked_before_push_and_pr_creation(self):
        for mutation in ('push', 'pr'):
            with self.subTest(mutation=mutation):
                original_rules = copy.deepcopy(self.rules)
                self.calls = []; self.merged = False
                original_prepare = self.prepare
                def prepare(root):
                    result = original_prepare(root)
                    if mutation == 'push': self.rules.clear()
                    return result
                original_git = self.git
                def git(*args):
                    result = original_git(*args)
                    if mutation == 'pr' and args[0] == 'push': self.rules.clear()
                    return result
                self.github.paginate.side_effect = self.paginate
                with patch.object(self.publisher, 'prepare', side_effect=prepare), patch.object(self.publisher, 'git', side_effect=git), \
                     patch.object(self.publisher, 'api', side_effect=self.api):
                    with self.assertRaisesRegex(RuntimeError, 'strict'): self.publisher.publish()
                self.assertFalse(any(method in ('POST', 'PUT') for method, _, _ in self.calls))
                if mutation == 'push':
                    self.assertFalse(any(method == 'git' and args[0] == 'push' for method, args, _ in self.calls))
                self.rules = original_rules

    def test_policy_changed_after_validation_blocks_merge(self):
        self.github.paginate.side_effect = self.paginate
        with patch.object(self.publisher, 'prepare', side_effect=self.prepare), patch.object(self.publisher, 'git', side_effect=self.git), \
             patch.object(self.publisher, 'api', side_effect=self.api), patch.object(self.publisher, 'verify_issue', side_effect=lambda *args: self.rules.clear()):
            with self.assertRaisesRegex(RuntimeError, 'strict'): self.publisher.publish()
        self.assertFalse(self.merged)
        self.assertFalse(any(method == 'PUT' for method, _, _ in self.calls))

    def test_mocked_server_rejects_stale_base_after_last_client_read(self):
        original_api = self.api
        def api(method, path, payload=None):
            if method == 'PUT':
                # Emulate protected merge rejecting main advancing after our reads.
                self.calls.append((method, path, payload))
                raise pub.GitHubError('GitHub API HTTP 405', 405)
            return original_api(method, path, payload)
        self.github.paginate.side_effect = self.paginate
        with patch.object(self.publisher, 'prepare', side_effect=self.prepare), patch.object(self.publisher, 'git', side_effect=self.git), \
             patch.object(self.publisher, 'api', side_effect=api), patch.object(self.publisher, 'verify_issue'):
            with self.assertRaises(pub.GitHubError) as caught: self.publisher.publish()
        self.assertEqual(caught.exception.status, 405)
        self.assertFalse(self.merged)

    def test_doctor_reports_read_success_but_missing_policy_blocker(self):
        self.rules = []
        self.github.request.side_effect = [dict(full_name='owner/repo'), dict(sha=BASE)]
        result = pub.doctor(self.settings)
        self.assertTrue(result['readable'])
        self.assertFalse(result['automaticPublicationReady'])
        self.assertFalse(result['writeTested'])
        self.assertIn('strict', result['blocker'])

    def test_complete_guarded_content_merge_and_readback(self):
        result, verify = self.publish()
        self.assertEqual(result, {'commit': MERGED})
        self.assertEqual(verify.call_args.args, (MERGED, self.public))
        text = json.dumps(self.calls)
        self.assertNotIn('PRIVATE', text)
        self.assertNotIn('force', text)
        pushes = [args for method, args, _ in self.calls if method == 'git' and args[0] == 'push']
        self.assertEqual(len(pushes), 1)
        self.assertNotIn('main', pushes[0][-1])
        self.assertTrue(any(method == 'GET' and path == '/pulls/7' for method, path, _ in self.calls[self.calls.index(next(x for x in self.calls if x[:2] == ('PUT', '/pulls/7/merge'))) + 1:]))

    def test_failed_content_guards_never_merge(self):
        for bad in ('head', 'base', 'sourcepr', 'foreign', 'source', 'blob', 'delete', 'extra', 'ci', 'checkhead', 'pages'):
            with self.subTest(bad=bad):
                self.bad = bad; self.calls = []; self.merged = False
                with self.assertRaises(RuntimeError): self.publish()
                self.assertFalse(any(method == 'PUT' for method, _, _ in self.calls))

    def test_existing_matching_branch_and_pr_recovered_without_push(self):
        self.duplicate = True
        result, _ = self.publish()
        self.assertEqual(result['commit'], MERGED)
        self.assertFalse(any(method == 'POST' or method == 'git' and path[0] == 'push' for method, path, _ in self.calls))

    def test_branch_collision_rejected(self):
        self.collision = True
        # Distinct remote tree, but identical HEAD token in fake transport must
        # explicitly distinguish the two lookups to emulate conflicting branch.
        old = self.git
        count = 0
        def git(*args):
            nonlocal count
            if args[0] == 'rev-parse' and args[1].endswith('^{tree}'):
                count += 1
                return str(count).encode()
            return old(*args)
        self.github.paginate.side_effect = self.paginate
        with patch.object(self.publisher, 'prepare', side_effect=self.prepare), patch.object(self.publisher, 'git', side_effect=git), patch.object(self.publisher, 'api', side_effect=self.api):
            with self.assertRaisesRegex(RuntimeError, 'collision'): self.publisher.publish()
        self.assertFalse(any(method in ('POST', 'PUT') for method, _, _ in self.calls))

    def test_merge_readback_must_match_actual_merge_parents(self):
        self.bad = 'mergeparent'
        with self.assertRaisesRegex(RuntimeError, 'unexpected'): self.publish()

    def test_idempotent_existing_issue_returns_current_main_without_write(self):
        with patch.object(self.publisher, 'prepare', return_value=(self.public, True)), patch.object(self.publisher, 'current', return_value=BASE):
            self.publisher.base = BASE
            self.assertEqual(self.publisher.publish(), {'commit': BASE})
        self.assertTrue(all(call.args[0] == 'GET' for call in self.github.request.call_args_list))

    def test_matching_slug_without_trusted_app_id_does_not_count(self):
        self.bad = 'impostor'; self.publisher.head = HEAD
        self.github.paginate.side_effect = self.paginate
        self.assertFalse(self.publisher.checks())

    def test_missing_validate_does_not_count_as_success(self):
        self.bad = 'missingci'; self.publisher.head = HEAD
        self.github.paginate.side_effect = self.paginate
        self.assertFalse(self.publisher.checks())

    def test_private_metadata_binding_and_unknown_fields(self):
        for key, value in [('reviewedSha256', '0' * 64), ('sender', 'PRIVATE'), ('collectionSha256', 'not-a-digest'), ('revision', 2)]:
            with self.subTest(key=key):
                original = copy.deepcopy(self.metadata); self.metadata[key] = value; self.save_metadata()
                with self.assertRaises(RuntimeError): pub.snapshot(self.settings, self.context)
                self.metadata = original
        self.save_metadata()
        context = dict(self.context, metadataSha256='0' * 64)
        with self.assertRaises(RuntimeError): pub.snapshot(self.settings, context)
        self.assertEqual(set(pub.snapshot(self.settings, self.context)[1]), pub.PUBLIC_METADATA)

    def test_tampered_review_and_invalid_weekly_context_rejected(self):
        Path(self.context['reviewed']).write_text('{}')
        with self.assertRaises(RuntimeError): pub.snapshot(self.settings, self.context)
        context = dict(self.context, revision=2)
        with self.assertRaises(RuntimeError): pub.snapshot(self.settings, context)

    def test_all_changed_paths_including_dist_are_exact(self):
        for paths in ([self.paths[0]], self.paths + ['package.json'], self.paths + ['data/revisions/x.json'], self.paths + ['dist/../runtime/x'], self.paths + ['dist\\evil']):
            with self.assertRaises(RuntimeError): pub.allowed_paths(paths, self.context['issueId'])
        pub.allowed_paths(self.paths, self.context['issueId'])

    def test_exact_canonical_and_metadata_readback(self):
        value = copy.deepcopy(self.public)
        with patch.object(self.publisher, 'content', return_value=json.dumps(value).encode()):
            self.publisher.verify_issue(BASE, self.public)
        value['collectedAt'] = '2026-11-13T09:00:00Z'
        with patch.object(self.publisher, 'content', return_value=json.dumps(value).encode()), self.assertRaises(RuntimeError):
            self.publisher.verify_issue(BASE, self.public)

    def test_content_blob_checksum_required(self):
        raw = b'{}'; blob = hashlib.sha1(b'blob 2\0{}').hexdigest()
        with patch.object(self.publisher, 'api', return_value=dict(type='file', encoding='base64', content=base64.b64encode(raw).decode(), sha=blob)):
            self.assertEqual(self.publisher.content(BASE, 'dist/a.json'), raw)
        with patch.object(self.publisher, 'api', return_value=dict(type='file', encoding='base64', content='e30=', sha=HEAD)), self.assertRaises(RuntimeError):
            self.publisher.content(BASE, 'dist/a.json')

    def test_subprocess_deadline_and_secrets_not_forwarded(self):
        with patch.object(pub.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, b'', b'PRIVATE')) as run:
            with self.assertRaises(RuntimeError) as caught: self.publisher.run(['npm', 'test'])
            self.assertNotIn('PRIVATE', str(caught.exception))
            self.assertNotIn('GITHUB_APP_PRIVATE_KEY', run.call_args.kwargs['env'])
            self.assertGreater(run.call_args.kwargs['timeout'], 0)
        self.publisher.deadline = time.monotonic() - 1
        with self.assertRaises(RuntimeError): self.publisher.remaining()

    def test_doctor_only_read_calls(self):
        self.github_type.return_value.request.side_effect = [dict(full_name='owner/repo'), dict(sha=BASE), self.ruleset]
        result = pub.doctor(self.settings)
        self.assertTrue(result['readable']); self.assertFalse(result['writeTested'])
        self.assertTrue(result['automaticPublicationReady'])
        self.github_type.assert_called_with(self.settings, write=True)
        self.assertTrue(all(call.args[0] == 'GET' for call in self.github.request.call_args_list))

    def prepared_fixture(self, existing=None, extra_source=False, moved=False):
        p = self.publisher
        node, npm = self.root / 'node', self.root / 'npm'
        node.touch(); npm.touch()
        p.settings.update(node=str(node), npm=str(npm))
        commands = []
        def git(*args):
            commands.append(('git', args))
            if args[0] == 'checkout':
                (p.cwd / 'data/issues').mkdir(parents=True)
                (p.cwd / '.node-version').write_text('22.23.4\n')
                (p.cwd / 'package.json').write_text(json.dumps(dict(engines=dict(node='22.23.4', npm='12.0.2'))))
                if existing is not None:
                    (p.cwd / self.paths[0]).write_text(json.dumps(existing))
            if args[:2] == ('rev-parse', 'FETCH_HEAD'): return (HEAD if moved else BASE).encode()
            if args[:2] == ('diff', '--cached'):
                paths = self.paths + (['runtime/source.py'] if extra_source else [])
                if '--name-only' in args: return ('\0'.join(paths) + '\0').encode()
                return ''.join('A\0' + path + '\0' for path in paths).encode()
            if args[0] == 'ls-files': return ''.join('100644 ' + self.blobs[path] + ' 0\t' + path + '\0' for path in self.paths).encode()
            if args[0] == 'rev-parse': return self.blobs[args[1][1:]].encode() if args[1].startswith(':') else HEAD.encode()
            return b''
        def run(argv, **kwargs):
            commands.append(('run', argv))
            if argv == [str(node), '--version']: return b'v22.23.4\n'
            if argv == [str(npm), '--version']: return b'12.0.2\n'
            if 'scripts/import-legacy.mjs' in argv: Path(argv[-1]).write_text(json.dumps(self.public))
            return b''
        buildroot = self.root / 'isolated'; buildroot.mkdir()
        with patch.object(p, 'git', side_effect=git), patch.object(p, 'run', side_effect=run), patch.object(p, 'current', return_value=BASE), patch.object(p, 'verify_issue') as verify:
            result = p.prepare(buildroot)
        return result, commands, verify

    def test_prepare_full_pinned_validation_sequence_offline(self):
        (value, exists), commands, _ = self.prepared_fixture()
        self.assertFalse(exists); self.assertEqual(value, self.public)
        self.assertEqual(self.publisher.paths, self.paths)
        npm_commands = [args[1:] for kind, args in commands if kind == 'run' and Path(args[0]).name == 'npm']
        self.assertEqual(npm_commands, [['--version'], ['ci', '--no-audit', '--no-fund'], ['run', 'check'], ['test'], ['run', 'build'], ['run', 'verify:dist']])
        self.assertTrue(any(kind == 'run' and 'scripts/import-legacy.mjs' in args for kind, args in commands))

    def test_prepare_existing_same_issue_does_not_build_commit_or_overwrite(self):
        (_, exists), commands, verify = self.prepared_fixture(existing=self.public)
        self.assertTrue(exists); verify.assert_called_once_with(BASE, self.public)
        self.assertFalse(any(kind == 'git' and 'commit' in args for kind, args in commands))
        self.assertFalse(any(kind == 'run' and 'build' in args for kind, args in commands))

    def test_prepare_changed_existing_issue_is_immutable(self):
        changed = copy.deepcopy(self.public); changed['items']['1']['story'] = 'Unreviewed edit.'
        with self.assertRaisesRegex(RuntimeError, 'immutable'): self.prepared_fixture(existing=changed)

    def test_prepare_stages_complete_source_change_and_rejects(self):
        with self.assertRaisesRegex(RuntimeError, 'non-content'): self.prepared_fixture(extra_source=True)

    def test_prepare_checkout_moved_main_stops_before_install(self):
        with self.assertRaisesRegex(RuntimeError, 'moved'): self.prepared_fixture(moved=True)

    def test_ambiguous_merge_retry_reads_production_without_second_merge(self):
        old_api = self.api
        def api(method, path, payload=None):
            result = old_api(method, path, payload)
            if method == 'PUT':
                raise RuntimeError('merge response lost')
            return result
        self.github.paginate.side_effect = self.paginate
        with patch.object(self.publisher, 'prepare', side_effect=self.prepare), patch.object(self.publisher, 'git', side_effect=self.git), patch.object(self.publisher, 'api', side_effect=api), patch.object(self.publisher, 'verify_issue'):
            with self.assertRaisesRegex(RuntimeError, 'response lost'): self.publisher.publish()
        self.assertTrue(self.merged)
        writes = len([x for x in self.calls if x[0] == 'PUT'])
        self.publisher.base = MERGED
        with patch.object(self.publisher, 'prepare', return_value=(self.public, True)), patch.object(self.publisher, 'current', return_value=MERGED):
            self.assertEqual(self.publisher.publish(), {'commit': MERGED})
        self.assertEqual(writes, len([x for x in self.calls if x[0] == 'PUT']))

    def test_idempotent_readback_moved_main_requires_retry(self):
        self.publisher.base = BASE
        with patch.object(self.publisher, 'prepare', return_value=(self.public, True)), patch.object(self.publisher, 'current', return_value=MERGED):
            with self.assertRaisesRegex(RuntimeError, 'moved'): self.publisher.publish()

    def test_automatic_publication_requires_explicit_opt_in(self):
        with self.assertRaisesRegex(RuntimeError, 'automatic_content_publication'):
            pub.Publisher(dict(self.settings, automatic_content_publication=False), self.context)


if __name__ == '__main__': unittest.main()
