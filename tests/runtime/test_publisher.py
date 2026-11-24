import base64
import copy
import hashlib
import json
from pathlib import Path
import shutil
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
from test_tagging import candidate_bytes
from runtime import tagging

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
                             qualifyingCounts=[20, 20, 10, 0], itemSources={},
                             ordinaryRankings=dict(score=[str(n) for n in range(1, 21)], comments=[str(n) for n in range(11, 21)]),
                             reviewedSha256=self.context['reviewedSha256'], collectionSha256='d' * 64, selectedSha256='e' * 64)
        self.tagfile = self.root / 'tagging.json'
        self.tagraw = candidate_bytes(path.read_bytes(), issue.issue_id)
        self.tagfile.write_bytes(self.tagraw); self.tagfile.chmod(0o600)
        self.metadata.update(taggingPath=str(self.tagfile), taggingSha256=digest(self.tagraw))
        self.public = dict(schemaVersion=1, issueId=issue.issue_id, revision=1, scheduledAt=issue.end,
                           collectedAt=issue.end, displayTimezone='Europe/London', window=dict(start=issue.start, end=issue.end), sections=[], items={})
        for group, section in zip(self.legacy['sections'], ['votes', 'comments', 'ask', 'show']):
            self.public['sections'].append(dict(id=section, itemIds=[x['id'] for x in group['items']], qualifyingCount=20 if section in ('votes', 'comments') else len(group['items'])))
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
        self.paths = [f'data/issues/{issue.issue_id}.json']
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

    def test_server_dist_path_is_rejected_even_with_canonical_issue_and_matching_blobs(self):
        self.paths += ['dist/index.html']
        self.blobs['dist/index.html'] = 'd' * 40
        with self.assertRaisesRegex(RuntimeError, 'non-content'):
            self.publish()
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

    def test_sidecar_snapshot_exact_digest_and_private_projection(self):
        with patch.object(tagging, 'read_private_bytes', wraps=tagging.read_private_bytes) as read:
            p = pub.Publisher(self.settings, self.context)
        read.assert_called_once_with(str(self.tagfile))
        self.assertEqual(p.tagging, self.tagraw)
        self.assertNotIn('taggingPath', p.metadata)
        self.assertNotIn('taggingSha256', p.metadata)
        self.assertNotIn('registry', p.metadata)
        self.assertEqual(p.tagging_sha256, digest(self.tagraw))

    def test_sidecar_changes_during_snapshot_do_not_replace_validated_bytes(self):
        read = tagging.read_private_bytes
        def racing_read(path):
            raw = read(path)
            Path(path).write_text('{}')
            return raw
        with patch.object(tagging, 'read_private_bytes', side_effect=racing_read) as snapshot:
            p = pub.Publisher(self.settings, self.context)
        snapshot.assert_called_once_with(str(self.tagfile))
        self.assertEqual(p.tagging, self.tagraw)

    def test_sidecar_invalid_binding_shape_or_digest_stops_before_github(self):
        for mutate in [lambda v: v.update(reviewedSha256='0'*64),
                       lambda v: v.update(issueId='2026-11-20'),
                       lambda v: v.update(registrySha256='0'*64),
                       lambda v: v['assignments'].pop('1'),
                       lambda v: v.update(private='PRIVATE')]:
            with self.subTest(mutate=mutate):
                value = json.loads(self.tagraw); mutate(value)
                raw = json.dumps(value).encode(); self.tagfile.write_bytes(raw)
                self.metadata['taggingSha256'] = digest(raw); self.save_metadata()
                self.github_type.reset_mock()
                with self.assertRaisesRegex(RuntimeError, 'tagging'):
                    pub.Publisher(self.settings, self.context)
                self.github_type.assert_not_called()
        self.tagfile.write_bytes(self.tagraw + b' ')
        self.metadata['taggingSha256'] = digest(self.tagraw); self.save_metadata()
        with self.assertRaisesRegex(RuntimeError, 'tagging.*digest'):
            pub.Publisher(self.settings, self.context)

    def test_tagging_digest_changes_deterministic_recovery_branch(self):
        self.publish(); first = self.publisher.branch
        self.publisher.tagging_sha256 = digest(self.tagraw + b' ')
        self.merged = False; self.calls = []
        self.publish()
        self.assertNotEqual(self.publisher.branch, first)

    def test_only_literal_story_paths_justified_by_review_are_allowed(self):
        ids = set(self.public['items'])
        valid = self.paths + ['data/tags.json', 'data/stories/1.json']
        pub.allowed_paths(valid, self.context['issueId'], ids)
        # Registry and story diffs are optional when no actual content changes.
        pub.allowed_paths(self.paths, self.context['issueId'], ids)
        for path in ['data/stories/99.json', 'data/stories/01.json', 'data/stories/1.json\n',
                     'data/stories/../1.json', 'data/issues/2026-11-06.json', 'data/private.json']:
            with self.subTest(path=path), self.assertRaisesRegex(RuntimeError, 'non-content'):
                pub.allowed_paths(self.paths + [path], self.context['issueId'], ids)

    def test_tampered_review_and_invalid_weekly_context_rejected(self):
        Path(self.context['reviewed']).write_text('{}')
        with self.assertRaises(RuntimeError): pub.snapshot(self.settings, self.context)
        context = dict(self.context, revision=2)
        with self.assertRaises(RuntimeError): pub.snapshot(self.settings, context)

    def test_all_changed_paths_are_source_only_and_exact(self):
        for paths in ([], ['data/tags.json'], self.paths + ['dist/index.html'], self.paths + ['dist/data/issues/2026-11-13.json'], self.paths + ['package.json'], self.paths + ['data/revisions/x.json'], self.paths + ['dist/../runtime/x'], self.paths + ['dist\\evil']):
            with self.assertRaises(RuntimeError): pub.allowed_paths(paths, self.context['issueId'])
        pub.allowed_paths(self.paths, self.context['issueId'])

    def test_exact_canonical_and_metadata_readback(self):
        value = copy.deepcopy(self.public)
        self.publisher.tag_bytes={'data/tags.json':json.dumps(value).encode()}
        with patch.object(self.publisher, 'content', return_value=json.dumps(value).encode()):
            self.publisher.verify_issue(BASE, self.public)
        value['collectedAt'] = '2026-11-13T09:00:00Z'
        with patch.object(self.publisher, 'content', return_value=json.dumps(value).encode()), self.assertRaises(RuntimeError):
            self.publisher.verify_issue(BASE, self.public)

    def test_exact_registry_and_selected_story_readback_includes_reused_records(self):
        p=self.publisher
        expected={'data/tags.json':b'{"schemaVersion":1,"tags":[{"slug":"ai","label":"AI"}]}',
                  **{f'data/stories/{ident}.json':json.dumps(dict(schemaVersion=1,id=ident,tagSlugs=['ai'],issueIds=['2026-11-13'])).encode() for ident in p.selected_ids}}
        p.tag_bytes=dict(expected)
        issue_raw=json.dumps(self.public).encode();seen=[]
        def content(commit,path):
            self.assertEqual(commit,MERGED);seen.append(path)
            return p.tag_bytes.get(path,issue_raw)
        self.assertTrue(callable(p.verify_issue))
        with patch.object(p,'content',side_effect=content):p.verify_issue(MERGED,self.public)
        self.assertTrue(set(p.tag_bytes)<=set(seen))
        # Same JSON meaning is not the exact blob that was validated/committed.
        self.assertTrue(all(not path.startswith('dist/') for path in seen))
        for wrong in ('data/tags.json','data/stories/1.json'):
            def changed(commit,path):return content(commit,path)+(b' ' if path==wrong else b'')
            with self.subTest(path=wrong),patch.object(p,'content',side_effect=changed),self.assertRaisesRegex(RuntimeError,'tagging blob differs'):
                p.verify_issue(MERGED,self.public)

    def test_readback_without_validated_tag_snapshot_fails_closed(self):
        with patch.object(self.publisher,'content',return_value=json.dumps(self.public).encode()),self.assertRaisesRegex(RuntimeError,'tagging snapshot'):
            self.publisher.verify_issue(MERGED,self.public)

    def test_actual_post_merge_readback_checks_committed_tags_before_content_result(self):
        original_prepare=self.prepare
        tagged={'data/tags.json':json.dumps(dict(schemaVersion=1,tags=[dict(slug='ai',label='AI'),dict(slug='security',label='Security')])).encode(),
                **{f'data/stories/{ident}.json':json.dumps(dict(schemaVersion=1,id=ident,tagSlugs=['ai','security'],issueIds=[self.context['issueId']])).encode() for ident in self.publisher.selected_ids}}
        expected=dict(tagged)
        self.paths+=['data/tags.json','data/stories/1.json']
        for path in self.paths:
            raw=expected.get(path,b'not a tag')
            self.blobs[path]=hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest()
        def prepare(root):
            result=original_prepare(root);self.publisher.tag_bytes=dict(expected);return result
        corrupt=[None];reads=[]
        old_api=self.api
        def api(method,path,payload=None):
            if path.startswith('/contents/'):
                name,commit=path.removeprefix('/contents/').split('?ref=');reads.append((commit,name))
                raw=expected.get(name,json.dumps(self.public).encode())
                if commit==MERGED and name==corrupt[0]:raw+=b' '
                blob=hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest()
                return dict(type='file',encoding='base64',sha=blob,content=base64.b64encode(raw).decode())
            return old_api(method,path,payload)
        self.assertTrue(callable(self.publisher.verify_issue))
        for wrong in (None,'data/tags.json','data/stories/1.json'):
            self.merged=False;self.calls=[];reads=[];corrupt[0]=wrong
            with self.subTest(path=wrong),patch.object(self.publisher,'prepare',side_effect=prepare),patch.object(self.publisher,'git',side_effect=self.git),patch.object(self.publisher,'api',side_effect=api):
                if wrong:
                    with self.assertRaisesRegex(RuntimeError,'tagging blob differs'):
                        self.publisher.publish()
                    self.assertTrue(self.merged,'post-merge failure must not claim a content result')
                else:
                    self.assertEqual(self.publisher.publish(),{'commit':MERGED})
                    self.assertTrue({(MERGED,path) for path in expected}<=set(reads))
                    self.assertTrue({(HEAD,path) for path in expected}<=set(reads))

    def test_capture_tagging_binds_changed_bytes_to_the_staged_blob_not_only_worktree(self):
        (_,exists),_,_=self.prepared_fixture()
        self.assertFalse(exists)
        p=self.publisher;path='data/stories/1.json'
        self.assertEqual(p.tag_bytes[path],(p.cwd/('dist/'+path)).read_bytes())
        self.assertTrue(all(not name.startswith('dist/') for name in p.tag_bytes))
        p.blobs[path]='f'*40
        with self.assertRaisesRegex(RuntimeError,'staged tagging blob'):
            p.capture_tagging()

    def test_content_blob_checksum_required(self):
        raw = b'{}'; blob = hashlib.sha1(b'blob 2\0{}').hexdigest()
        with patch.object(self.publisher, 'api', return_value=dict(type='file', encoding='base64', content=base64.b64encode(raw).decode(), sha=blob)):
            self.assertEqual(self.publisher.content(BASE, 'data/tags.json'), raw)
        with patch.object(self.publisher, 'api', return_value=dict(type='file', encoding='base64', content='e30=', sha=HEAD)), self.assertRaises(RuntimeError):
            self.publisher.content(BASE, 'data/tags.json')

    def test_subprocess_deadline_and_secrets_not_forwarded(self):
        with patch.object(pub.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, b'', b'PRIVATE')) as run:
            with self.assertRaises(RuntimeError) as caught: self.publisher.run(['npm', 'test'])
            self.assertNotIn('PRIVATE', str(caught.exception))
            self.assertNotIn('GITHUB_APP_PRIVATE_KEY', run.call_args.kwargs['env'])
            self.assertGreater(run.call_args.kwargs['timeout'], 0)
            self.assertEqual(run.call_args.kwargs['umask'], 0o022)
        self.publisher.deadline = time.monotonic() - 1
        with self.assertRaises(RuntimeError): self.publisher.remaining()

    def test_doctor_only_read_calls(self):
        self.github_type.return_value.request.side_effect = [dict(full_name='owner/repo'), dict(sha=BASE), self.ruleset]
        result = pub.doctor(self.settings)
        self.assertTrue(result['readable']); self.assertFalse(result['writeTested'])
        self.assertTrue(result['automaticPublicationReady'])
        self.github_type.assert_called_with(self.settings, write=True)
        self.assertTrue(all(call.args[0] == 'GET' for call in self.github.request.call_args_list))

    def prepared_fixture(self, existing=None, extra_source=False, extra_dist=False, moved=False, validator_error=False, mutate_sidecar=False, tamper_frozen=False):
        p = self.publisher
        node, npm = self.root / 'node', self.root / 'npm'
        node.touch(); npm.touch()
        p.settings.update(node=str(node), npm=str(npm))
        if mutate_sidecar:
            self.tagfile.write_text('{}')
        commands = []
        def write_tags():
            values={'data/tags.json':json.dumps(dict(schemaVersion=1,tags=[dict(slug='ai',label='AI'),dict(slug='security',label='Security')])).encode(),
                    **{f'data/stories/{ident}.json':json.dumps(dict(schemaVersion=1,id=ident,tagSlugs=['ai','security'],issueIds=[self.context['issueId']])).encode() for ident in p.selected_ids}}
            for path,raw in values.items():
                for target in (p.cwd/path,p.cwd/('dist/'+path)):
                    target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(raw)
        def git(*args):
            commands.append(('git', args))
            if args[0] == 'checkout':
                (p.cwd / 'data/issues').mkdir(parents=True)
                write_tags()
                (p.cwd / '.node-version').write_text('22.23.4\n')
                (p.cwd / 'package.json').write_text(json.dumps(dict(engines=dict(node='22.23.4', npm='12.0.2'))))
                if existing is not None:
                    (p.cwd / self.paths[0]).write_text(json.dumps(existing))
            if args[:2] == ('rev-parse', 'FETCH_HEAD'): return (HEAD if moved else BASE).encode()
            if args[:2] == ('diff', '--cached'):
                paths = self.paths + (['runtime/source.py'] if extra_source else []) + (['dist/index.html'] if extra_dist else [])
                if '--name-only' in args: return ('\0'.join(paths) + '\0').encode()
                return ''.join('A\0' + path + '\0' for path in paths).encode()
            if args[0] == 'ls-files': return ''.join('100644 ' + self.blobs[path] + ' 0\t' + path + '\0' for path in self.paths).encode()
            if args[0] == 'rev-parse': return self.blobs[args[1][1:]].encode() if args[1].startswith(':') else HEAD.encode()
            return b''
        def run(argv, **kwargs):
            commands.append(('run', argv))
            if tamper_frozen and argv[1:] == ['run', 'build']:
                (p.cwd.parent / 'tagging.json').write_bytes(self.tagraw + b' ')
            if argv == [str(node), '--version']: return b'v22.23.4\n'
            if argv == [str(npm), '--version']: return b'12.0.2\n'
            if 'scripts/import-legacy.mjs' in argv: Path(argv[-1]).write_text(json.dumps(self.public))
            if argv[1:] == ['run', 'build']:
                destination = p.cwd / ('dist/' + self.paths[0])
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes((p.cwd / self.paths[0]).read_bytes())
            if 'scripts/tagging.mjs' in argv or 'scripts/verify-tag-changes.mjs' in argv:
                frozen = Path(argv[argv.index('--sidecar') + 1])
                self.assertEqual(frozen.read_bytes(), self.tagraw)
                self.assertNotEqual(frozen, self.tagfile)
                self.assertFalse(frozen.is_relative_to(p.cwd))
                self.assertEqual(frozen.stat().st_mode & 0o777, 0o600)
                self.assertEqual(frozen.parent.stat().st_mode & 0o777, 0o700)
            if 'scripts/verify-tag-changes.mjs' in argv and validator_error:
                raise RuntimeError('semantic tag diff refused')
            return b''
        buildroot = self.root / 'isolated'; buildroot.mkdir()
        inputs = {'.node-version': b'22.23.4\n', 'package.json': b'{"engines":{"node":"22.23.4"}}'}
        def trusted_run(node, args, cwd, env, remaining):
            self.assertNotEqual(cwd, p.cwd)
            self.assertFalse(cwd.is_relative_to(p.cwd))
            self.assertEqual(cwd.name, 'trusted-validation')
            return 0, run([node, *args])
        with patch.object(p, 'git', side_effect=git), patch.object(p, 'run', side_effect=run), patch.object(p, 'current', return_value=BASE), patch.object(p, 'verify_issue') as verify, \
             patch.object(p, 'recovery_inputs', return_value=inputs), patch.object(pub.trusted_build, 'stage', return_value={}), \
             patch.object(pub.trusted_build, 'run_node', side_effect=trusted_run):
            result = p.prepare(buildroot)
        return result, commands, verify

    def test_prepare_full_pinned_validation_sequence_offline(self):
        (value, exists), commands, _ = self.prepared_fixture()
        self.assertFalse(exists); self.assertEqual(value, self.public)
        self.assertEqual(self.publisher.paths, self.paths)
        npm_commands = [args[1:] for kind, args in commands if kind == 'run' and Path(args[0]).name == 'npm']
        self.assertEqual(npm_commands, [['--version'], ['ci', '--no-audit', '--no-fund'], ['run', 'check'], ['test'], ['run', 'build'], ['run', 'verify:dist']])
        self.assertTrue(any(kind == 'run' and 'scripts/import-legacy.mjs' in args for kind, args in commands))
        applied = [args for kind, args in commands if kind == 'run' and 'scripts/tagging.mjs' in args]
        validated = [args for kind, args in commands if kind == 'run' and 'scripts/verify-tag-changes.mjs' in args]
        self.assertEqual(len(applied), 1); self.assertIn('apply', applied[0])
        self.assertGreaterEqual(len(validated), 1)
        self.assertTrue(all(args[args.index('--base') + 1] == BASE for args in validated))
        last_validator = max(i for i, (kind, args) in enumerate(commands) if kind == 'run' and 'scripts/verify-tag-changes.mjs' in args)
        commit = next(i for i, (kind, args) in enumerate(commands) if kind == 'git' and 'commit' in args)
        self.assertLess(last_validator, commit)
        build = next(i for i, (kind, args) in enumerate(commands) if kind == 'run' and args[1:] == ['run', 'build'])
        self.assertTrue(all(i > build for i, (kind, args) in enumerate(commands) if kind == 'run' and 'scripts/verify-tag-changes.mjs' in args))

    def test_prepare_uses_constructor_snapshot_not_mutable_original_sidecar(self):
        (_, exists), commands, _ = self.prepared_fixture(mutate_sidecar=True)
        self.assertFalse(exists)
        self.assertTrue(any(kind == 'run' and 'scripts/tagging.mjs' in args for kind, args in commands))

    def test_prepare_semantic_diff_failure_stops_before_commit_or_push(self):
        with self.assertRaisesRegex(RuntimeError, 'semantic tag diff'):
            self.prepared_fixture(validator_error=True)

    def test_prepare_rechecks_exact_frozen_copy_after_lifecycle_scripts(self):
        with self.assertRaisesRegex(RuntimeError, 'frozen tagging'):
            self.prepared_fixture(tamper_frozen=True)

    def test_server_readback_checks_complete_registry_and_story_blob_set(self):
        self.paths += ['data/tags.json', 'data/stories/1.json']
        self.blobs.update({'data/tags.json':'d'*40, 'data/stories/1.json':'e'*40})
        result, _ = self.publish()
        self.assertEqual(result['commit'], MERGED)
        self.merged = False; self.bad = 'blob'; self.calls = []
        with self.assertRaisesRegex(RuntimeError, 'complete content file set'):
            self.publish()
        self.assertFalse(any(method == 'PUT' for method, _, _ in self.calls))

    def test_existing_issue_still_requires_frozen_tag_compatibility(self):
        with self.assertRaisesRegex(RuntimeError, 'semantic tag diff'):
            self.prepared_fixture(existing=self.public, validator_error=True)

    def test_real_local_import_apply_and_diff_pipeline_uses_frozen_private_sidecar(self):
        source = Path(__file__).resolve().parents[2]
        origin = self.root / 'local-origin'; origin.mkdir()
        for name in ['scripts', 'schema']:
            shutil.copytree(source / name, origin / name)
        for name in ['package.json', '.node-version']:
            shutil.copyfile(source / name, origin / name)
        (origin / '.gitignore').write_text('node_modules\ndist/\n')
        (origin / 'node_modules').symlink_to(source / 'node_modules', target_is_directory=True)
        for name in ['issues', 'revisions', 'stories']:
            (origin / 'data' / name).mkdir(parents=True)
        old = copy.deepcopy(self.public)
        issue = Issue.resolve('2026-11-06')
        old.update(issueId=issue.issue_id, scheduledAt=issue.end, collectedAt=issue.end,
                   window=dict(start=issue.start, end=issue.end))
        for item in old['items'].values(): item['submittedAt'] = issue.start
        (origin / 'data/issues' / (issue.issue_id + '.json')).write_text(json.dumps(old))
        registry = dict(schemaVersion=1, tags=[dict(slug='ai', label='AI'), dict(slug='security', label='Security')])
        (origin / 'data/tags.json').write_text(json.dumps(registry))
        for ident in old['items']:
            (origin / 'data/stories' / (ident + '.json')).write_text(json.dumps(dict(schemaVersion=1,
                id=ident, tagSlugs=['ai', 'security'], issueIds=[issue.issue_id])))
        (origin / 'data/revisions/.gitkeep').touch()
        def git_at(root, *args):
            return subprocess.check_output(['git', *args], cwd=root, stderr=subprocess.DEVNULL, timeout=15)
        git_at(origin, 'init', '--quiet', '--initial-branch=main'); git_at(origin, 'add', '-A')
        git_at(origin, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '--quiet', '-m', 'base')
        base = git_at(origin, 'rev-parse', 'HEAD').decode().strip()
        p = self.publisher
        p.settings.update(node=shutil.which('node'), npm=shutil.which('npm'))
        original_run, original_git = p.run, p.git
        def local_git(*args):
            if args[:3] == ('remote', 'add', 'origin'):
                return original_git('remote', 'add', 'origin', str(origin))
            result = original_git(*args)
            if args[0] == 'checkout':
                (p.cwd / 'node_modules').symlink_to(source / 'node_modules', target_is_directory=True)
            return result
        def run(argv, **kwargs):
            if argv[0] == p.settings['npm'] and argv[1:] != ['--version']:
                if argv[1:] == ['run', 'build']:
                    (p.cwd / 'dist').mkdir()
                    (p.cwd / 'dist/index.html').write_text('offline generated fixture')
                    (p.cwd/'dist/data/issues').mkdir(parents=True)
                    shutil.copyfile(p.cwd / self.paths[0], p.cwd / ('dist/' + self.paths[0]))
                    shutil.copyfile(p.cwd/'data/tags.json',p.cwd/'dist/data/tags.json')
                    shutil.copytree(p.cwd/'data/stories',p.cwd/'dist/data/stories')
                return b''
            return original_run(argv, **kwargs)
        self.tagfile.write_text('{}') # Cannot influence the constructor snapshot.
        buildroot = self.root / 'real-isolated'; buildroot.mkdir()
        with patch.object(p, 'git', side_effect=local_git), patch.object(p, 'run', side_effect=run), \
             patch.object(p, 'current', return_value=base), patch.object(p, 'verify_issue'):
            expected, exists = p.prepare(buildroot)
        self.assertFalse(exists); self.assertEqual(expected, self.public)
        self.assertNotIn('data/tags.json', p.paths, 'known labels need no registry diff')
        self.assertIn('data/stories/1.json', p.paths)
        self.assertEqual((buildroot / 'tagging.json').read_bytes(), self.tagraw)
        self.assertEqual(json.loads((p.cwd / 'data/stories/1.json').read_bytes())['issueIds'], ['2026-11-06', '2026-11-13'])
        self.assertEqual((p.cwd / 'data/issues/2026-11-06.json').read_bytes(), (origin / 'data/issues/2026-11-06.json').read_bytes())

    def real_recovery(self, fault=None):
        source = Path(__file__).resolve().parents[2]
        origin = self.root / ('recovery-origin-' + str(len(list(self.root.glob('recovery-origin-*')))))
        origin.mkdir()
        for name, raw in pub.trusted_build.build_inputs(source, lambda: 30).items():
            pub.trusted_build.write_public(origin / name, raw)
        registry = dict(schemaVersion=1, tags=[dict(slug='ai', label='AI'), dict(slug='security', label='Security')])
        files = {'data/tags.json': registry, f'data/issues/{self.context["issueId"]}.json': self.public}
        for ident in self.publisher.selected_ids:
            files[f'data/stories/{ident}.json'] = dict(schemaVersion=1, id=ident,
                tagSlugs=['ai', 'security'], issueIds=[self.context['issueId']])
        marker = self.root / 'fetched-code-executed'
        if fault == 'immutable':
            files[f'data/issues/{self.context["issueId"]}.json'] = copy.deepcopy(self.public)
            files[f'data/issues/{self.context["issueId"]}.json']['items']['1']['story'] = 'Unapproved edit.'
        pub.trusted_build.write_public(origin / 'data/revisions/.gitkeep', b'')
        for name, value in files.items():
            pub.trusted_build.write_public(origin / name, json.dumps(value).encode())
        if fault in ('script', 'schema', 'package-hook'):
            if fault == 'script':
                path = origin / 'scripts/import-legacy.mjs'
                path.write_text(f"import {{writeFileSync}} from 'node:fs';writeFileSync({json.dumps(str(marker))},'BAD');\n")
            elif fault == 'schema':
                path = origin / 'schema/issue.schema.json'
                value = json.loads(path.read_bytes()); value['$comment'] = str(marker); path.write_text(json.dumps(value))
            else:
                path = origin / 'package.json'
                value = json.loads(path.read_bytes())
                value['scripts']['preinstall'] = f'node -e "require(\'fs\').writeFileSync(\'{marker}\',\'BAD\')"'
                path.write_text(json.dumps(value))
        def at(*args):
            return subprocess.check_output(['git', *args], cwd=origin, stderr=subprocess.DEVNULL, timeout=15)
        at('init', '--quiet', '--initial-branch=main'); at('add', '-A')
        at('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '--quiet', '-m', 'source-only recovery')
        base = at('rev-parse', 'HEAD').decode().strip()
        self.assertEqual(at('ls-files', 'dist'), b'')
        p = pub.Publisher(self.settings, self.context)
        p.settings.update(node=shutil.which('node'), npm='/does/not/exist/npm')
        if fault == 'frozen':
            value = json.loads(p.tagging); value['assignments']['1'] = ['ai']
            p.tagging = json.dumps(value).encode()
        original_git, original_run, original_inputs = p.git, p.run, p.recovery_inputs
        def recovery_inputs():
            inputs = original_inputs()
            if fault == 'checkout-race':
                # A binding/read race cannot turn cwd-relative fetched schemas
                # or a checked-out script into recovery validator authority.
                (p.cwd / 'scripts/import-legacy.mjs').write_text(
                    f"import {{writeFileSync}} from 'node:fs';writeFileSync({json.dumps(str(marker))},'BAD');\n")
                (p.cwd / 'schema/issue.schema.json').write_text('{}')
            return inputs
        commands, reads = [], []
        def local_git(*args):
            commands.append(args)
            if args[:3] == ('remote', 'add', 'origin'):
                return original_git('remote', 'add', 'origin', str(origin))
            return original_git(*args)
        def run(argv, **kwargs):
            self.assertNotIn('npm', Path(argv[0]).name)
            return original_run(argv, **kwargs)
        def content(commit, name):
            self.assertEqual(commit, base); self.assertTrue(name.startswith('data/'))
            reads.append(name); return at('show', commit + ':' + name)
        workspace = self.root / ('recovery-work-' + origin.name); workspace.mkdir()
        before = {name: (origin / name).read_bytes() for name in files}
        with patch.object(p, 'git', side_effect=local_git), patch.object(p, 'run', side_effect=run), \
             patch.object(p, 'current', return_value=base), patch.object(p, 'content', side_effect=content), \
             patch.object(p, 'recovery_inputs', side_effect=recovery_inputs):
            if fault == 'dependencies':
                installed = self.root / 'installed-without-dependencies'; installed.mkdir()
                for name, raw in pub.trusted_build.build_inputs(source, lambda: 30).items():
                    pub.trusted_build.write_public(installed / name, raw)
                with patch.object(pub, 'ROOT', installed):
                    with self.assertRaisesRegex(RuntimeError, 'dependencies required; no automatic install'):
                        p.prepare(workspace)
            elif fault:
                with self.assertRaisesRegex(RuntimeError, 'incompatible' if fault in ('script', 'schema', 'package-hook') else 'immutable' if fault == 'immutable' else 'installed publication validation failed'):
                    p.prepare(workspace)
            else:
                expected, exists = p.prepare(workspace)
                self.assertTrue(exists); self.assertEqual(expected, self.public)
                self.assertEqual(set(reads), set(files))
                self.assertFalse((p.cwd / 'dist').exists())
        if fault == 'checkout-race':
            self.assertEqual(json.loads((workspace / 'canonical.json').read_bytes()), self.public)
        self.assertFalse(marker.exists())
        self.assertFalse(any('commit' in args or 'push' in args or 'add' == args[0] for args in commands))
        for name, raw in before.items(): self.assertEqual((origin / name).read_bytes(), raw)

    def test_real_source_only_recovery_installed_importer_and_complete_diff_no_npm(self):
        self.real_recovery()

    def test_real_recovery_refuses_fetched_script_schema_hooks_without_execution(self):
        for fault in ('script', 'schema', 'package-hook', 'checkout-race'):
            with self.subTest(fault=fault): self.real_recovery(fault)

    def test_real_recovery_missing_dependencies_never_installs(self):
        self.real_recovery('dependencies')

    def test_real_recovery_preserves_immutable_review_and_frozen_assignments(self):
        for fault in ('immutable', 'frozen'):
            with self.subTest(fault=fault): self.real_recovery(fault)

    def test_prepare_existing_same_issue_does_not_build_commit_or_overwrite(self):
        (_, exists), commands, verify = self.prepared_fixture(existing=self.public)
        self.assertTrue(exists); verify.assert_called_once_with(BASE, self.public)
        self.assertFalse(any(kind == 'git' and 'commit' in args for kind, args in commands))
        self.assertFalse(any(kind == 'run' and 'build' in args for kind, args in commands))
        self.assertFalse(any(kind == 'run' and Path(args[0]).name == 'npm' for kind, args in commands))
        validators = [args for kind, args in commands if kind == 'run' and 'scripts/verify-tag-changes.mjs' in args]
        self.assertEqual(len(validators), 1)
        self.assertIn('--idempotent', validators[0])

    def test_prepare_changed_existing_issue_is_immutable(self):
        changed = copy.deepcopy(self.public); changed['items']['1']['story'] = 'Unreviewed edit.'
        with self.assertRaisesRegex(RuntimeError, 'immutable'): self.prepared_fixture(existing=changed)

    def test_prepare_stages_complete_source_change_and_rejects(self):
        with self.assertRaisesRegex(RuntimeError, 'non-content'): self.prepared_fixture(extra_source=True)

    def test_prepare_rejects_any_generated_path_from_the_complete_index(self):
        with self.assertRaisesRegex(RuntimeError, 'non-content'):
            self.prepared_fixture(extra_dist=True)

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
