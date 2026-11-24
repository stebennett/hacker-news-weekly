import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from runtime import weekly
from runtime.orchestrator import Issue, RuntimeError, Store, digest
from test_runtime import reviewed
from test_tagging import candidate_bytes


class WeeklyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.work = self.root / 'work'
        self.state = self.root / 'state'
        self.meta = self.root / 'metadata'
        self.issue = Issue.resolve('2026-11-13')
        self.review = reviewed(self.issue)
        self.selected = copy.deepcopy(self.review)
        for group in self.selected['sections']:
            for item in group['items']:
                del item['story']; del item['discussion']
        self.counts = [100, 100, 20, 0]
        self.rankings = dict(score=[item['id'] for item in self.selected['sections'][0]['items']], comments=[item['id'] for item in self.selected['sections'][1]['items']])
        self.now = datetime(2026, 11, 13, 8, 1, tzinfo=timezone.utc)

    def begin(self):
        with patch.object(weekly, 'collect_issue', return_value=(self.selected, self.counts, self.rankings)) as collect:
            result = weekly.begin(self.work, self.issue, now=self.now)
        return result, collect

    def approve(self, sources=True, tagging=True):
        self.begin()
        path = self.root / 'review.json'
        path.write_text(json.dumps(self.review))
        candidate = self.root / 'candidate.json'
        candidate.write_bytes(candidate_bytes(path.read_bytes(), self.issue.issue_id))
        candidate.chmod(0o600)
        provenance = self.root / 'sources.json'
        ids = {x['id'] for g in self.review['sections'] for x in g['items']}
        provenance.write_text(json.dumps({'itemSources': {i: {'sources': [], 'availability': {}} for i in ids}}))
        options = dict(tagging_path=candidate) if tagging else {}
        result = weekly.approve(self.work, self.state, self.meta, self.issue, path,
                                provenance if sources else None, approved=True, **options)
        return result

    def retry(self, candidate=None):
        return weekly.approve(self.work, self.state, self.meta, self.issue,
            self.root/'review.json', self.root/'sources.json', approved=True,
            tagging_path=candidate or self.root/'candidate.json')

    def test_missing_tagging_preserves_email_but_blocks_website(self):
        result = self.approve(tagging=False)
        self.assertTrue(result['emailPrepared'])
        self.assertRegex(result['websitePreparationError'] or '', 'tagging')
        self.assertFalse(Path(result['metadata']).exists())
        self.assertEqual(Store(self.state).load(self.issue.issue_id)['email']['status'], 'pending')

    def test_tagging_exact_bytes_retry_and_private_modes(self):
        result = self.approve()
        self.assertIsNone(result['websitePreparationError'])
        meta_path = Path(result['metadata'])
        meta_raw = meta_path.read_bytes()
        metadata = json.loads(meta_raw)
        frozen = Path(metadata['taggingPath'])
        expected = (self.root/'candidate.json').read_bytes()
        self.assertEqual(frozen.read_bytes(), expected)
        self.assertEqual(metadata['taggingSha256'], digest(expected))
        self.assertTrue(frozen.is_absolute())
        self.assertEqual(frozen.stat().st_mode & 0o777, 0o600)
        self.assertEqual(frozen.parent.stat().st_mode & 0o777, 0o700)
        self.assertIsNone(self.retry()['websitePreparationError'])
        self.assertEqual(frozen.read_bytes(), expected)
        self.assertEqual(meta_path.read_bytes(), meta_raw)
        # Tagging must never contaminate collector/email review fields.
        self.assertEqual((self.state/self.issue.issue_id/'reviewed.json').read_bytes(),
                         (self.root/'review.json').read_bytes())

    def test_changed_candidate_cannot_replace_frozen_bytes_or_metadata(self):
        result = self.approve()
        meta_path = Path(result['metadata']); old_meta = meta_path.read_bytes()
        frozen = Path(json.loads(old_meta)['taggingPath']); old_raw = frozen.read_bytes()
        candidate = self.root/'candidate.json'
        value = json.loads(old_raw)
        for change in ('assignment', 'bytes', 'review', 'registry', 'root', 'id'):
            with self.subTest(change=change):
                altered = copy.deepcopy(value)
                if change == 'assignment': altered['assignments']['1'] = ['ai']
                if change == 'review': altered['reviewedSha256'] = '0'*64
                if change == 'registry': altered['registrySha256'] = '0'*64
                if change == 'root': altered['extra'] = True
                if change == 'id': altered['assignments']['999999'] = ['ai']
                candidate.write_bytes(json.dumps(altered).encode() + (b' ' if change == 'bytes' else b''))
                retry = self.retry()
                self.assertTrue(retry['emailPrepared'])
                self.assertIsNotNone(retry['websitePreparationError'])
                self.assertEqual(frozen.read_bytes(), old_raw)
                self.assertEqual(meta_path.read_bytes(), old_meta)

    def test_invalid_or_missing_candidate_leaves_no_metadata_or_frozen_sidecar(self):
        result = self.approve(tagging=False)
        candidate = self.root/'candidate.json'
        for raw in (b'{}', b'[]', b'not JSON', b'null'):
            with self.subTest(raw=raw):
                candidate.write_bytes(raw)
                retry = self.retry()
                self.assertTrue(retry['emailPrepared'])
                self.assertIsNotNone(retry['websitePreparationError'])
                self.assertFalse(Path(retry['metadata']).exists())
                self.assertFalse((self.state/self.issue.issue_id/'tagging.json').exists())
        candidate.unlink()
        self.assertIsNotNone(self.retry()['websitePreparationError'])
        self.assertFalse(Path(result['metadata']).exists())

    def test_tagging_failure_repair_never_resets_delivery_state(self):
        result = self.approve(tagging=False)
        store = Store(self.state)
        for status in ('verified', 'ambiguous', 'in_progress'):
            with self.subTest(status=status):
                saved = store.load(self.issue.issue_id)
                saved['email'] = dict(status=status, messageId='frozen-id', messageSha256='a'*64)
                saved['website'] = dict(status='failed', error='original website error')
                store.save(saved)
                before = (self.state/self.issue.issue_id/'state.json').read_bytes()
                retry = self.retry()
                self.assertIsNone(retry['websitePreparationError'])
                self.assertEqual((self.state/self.issue.issue_id/'state.json').read_bytes(), before)
                self.assertTrue(Path(result['metadata']).exists())

    def test_candidate_is_snapshotted_once_before_freeze(self):
        from runtime import tagging
        original_validate = tagging.validate_tagging
        original = candidate_bytes(json.dumps(self.review).encode(), self.issue.issue_id)
        def mutate_after_validation(raw, *args):
            result = original_validate(raw, *args)
            (self.root/'candidate.json').write_bytes(b'{}')
            return result
        with patch.object(tagging, 'validate_tagging', side_effect=mutate_after_validation):
            result = self.approve()
        self.assertIsNone(result['websitePreparationError'])
        metadata = json.loads(Path(result['metadata']).read_bytes())
        self.assertEqual(Path(metadata['taggingPath']).read_bytes(), original)
        self.assertEqual(metadata['taggingSha256'], digest(original))
        self.assertEqual((self.root/'candidate.json').read_bytes(), b'{}')

    def test_candidate_bound_to_changed_source_after_parity_is_not_accepted(self):
        original_parity = weekly.selected_parity
        def change_both_after_parity(selected, review):
            original_parity(selected, review)
            changed = copy.deepcopy(self.review)
            changed['sections'][0]['items'][0]['story'] = 'Changed after parity.'
            raw = json.dumps(changed).encode()
            (self.root/'review.json').write_bytes(raw)
            (self.root/'candidate.json').write_bytes(candidate_bytes(raw, self.issue.issue_id))
        with patch.object(weekly, 'selected_parity', side_effect=change_both_after_parity):
            result = self.approve()
        self.assertTrue(result['emailPrepared'])
        self.assertIsNotNone(result['websitePreparationError'])
        self.assertFalse(Path(result['metadata']).exists())
        self.assertEqual((self.state/self.issue.issue_id/'reviewed.json').read_bytes(),
                         json.dumps(self.review).encode())

    def test_candidate_and_frozen_symlinks_and_orphans_fail_closed(self):
        result = self.approve(tagging=False)
        candidate = self.root/'candidate.json'
        link = self.root/'link.json'; link.symlink_to(candidate)
        self.assertIsNotNone(self.retry(link)['websitePreparationError'])
        frozen = self.state/self.issue.issue_id/'tagging.json'
        frozen.symlink_to(candidate)
        self.assertIsNotNone(self.retry()['websitePreparationError'])
        self.assertTrue(frozen.is_symlink())
        frozen.unlink(); frozen.write_bytes(candidate.read_bytes()); frozen.chmod(0o600)
        self.assertRegex(self.retry()['websitePreparationError'], 'orphan')
        self.assertFalse(Path(result['metadata']).exists())

    def test_missing_tampered_or_symlinked_frozen_sidecar_is_not_recreated(self):
        result = self.approve()
        meta_path = Path(result['metadata']); old_meta = meta_path.read_bytes()
        frozen = Path(json.loads(old_meta)['taggingPath'])
        frozen.unlink()
        self.assertIsNotNone(self.retry()['websitePreparationError'])
        self.assertFalse(frozen.exists())
        frozen.write_bytes(b'{}'); frozen.chmod(0o600)
        self.assertIsNotNone(self.retry()['websitePreparationError'])
        self.assertEqual(frozen.read_bytes(), b'{}')
        frozen.unlink(); frozen.symlink_to(self.root/'candidate.json')
        self.assertIsNotNone(self.retry()['websitePreparationError'])
        self.assertTrue(frozen.is_symlink())
        self.assertEqual(meta_path.read_bytes(), old_meta)

    def test_metadata_symlink_is_refused_without_overwriting_target(self):
        result = self.approve(tagging=False)
        target = self.root/'foreign.json'; target.write_bytes(b'foreign')
        Path(result['metadata']).symlink_to(target)
        self.assertIsNotNone(self.retry()['websitePreparationError'])
        self.assertEqual(target.read_bytes(), b'foreign')

    def test_tagging_and_metadata_roots_cannot_be_repository_paths(self):
        result = self.approve(tagging=False)
        # Patch only the privacy boundary, not weekly's renderer source root.
        from runtime import tagging
        with patch.object(tagging, 'ROOT', self.root):
            retry = self.retry()
        self.assertTrue(retry['emailPrepared'])
        self.assertIsNotNone(retry['websitePreparationError'])
        self.assertFalse(Path(result['metadata']).exists())

    def test_cli_tagging_option_prepares_real_sidecar(self):
        result = self.approve(tagging=False)
        command = [sys.executable, '-m', 'runtime.weekly', 'approve', '--issue', self.issue.issue_id,
            '--work-root', str(self.work), '--state-root', str(self.state), '--metadata-root', str(self.meta),
            '--reviewed', str(self.root/'review.json'), '--sources', str(self.root/'sources.json'),
            '--tagging', str(self.root/'candidate.json'), '--approve-reviewed']
        output = subprocess.run(command, cwd=weekly.ROOT, capture_output=True, timeout=15)
        self.assertEqual(output.returncode, 0, output.stderr.decode())
        report = json.loads(output.stdout)
        self.assertTrue(report['emailPrepared'])
        self.assertIsNone(report['websitePreparationError'])
        self.assertTrue(Path(result['metadata']).exists())

    def test_freeze_and_retry_do_not_recollect(self):
        result, collect = self.begin()
        self.assertEqual(collect.call_count, 1)
        old = Path(result['selected']).read_bytes()
        other, collect = self.begin()
        self.assertTrue(other['reused'])
        self.assertEqual(collect.call_count, 0)
        self.assertEqual(Path(result['selected']).read_bytes(), old)
        self.assertEqual(Path(result['selected']).stat().st_mode & 0o777, 0o600)

    def test_no_future_or_nonfriday_window(self):
        with self.assertRaises(RuntimeError):
            weekly.begin(self.work, Issue.resolve('2026-11-20'), now=self.now)
        with self.assertRaises(ValueError):
            Issue.resolve('2026-11-16')

    def test_selected_tamper_blocks_reuse(self):
        result, _ = self.begin()
        Path(result['selected']).write_text('{}')
        with self.assertRaises(RuntimeError): self.begin()

    def test_orphan_is_not_overwritten(self):
        folder = Store(self.work).issue_dir(self.issue.issue_id)
        (folder / 'selected.json').write_text('{}')
        with self.assertRaises(RuntimeError): self.begin()

    def test_original_dispatch_not_today_determines_issue(self):
        jobs = self.root / 'jobs.json'
        jobs.write_text(json.dumps({'jobs': [{'id': 'weekly', 'timezone': 'Europe/London',
            'schedule': {'expr': '0 8 * * 5'}, 'last_dispatch': {'scheduled_at': '2026-11-13T08:00:00Z'}}]}))
        self.assertEqual(weekly.dispatch_issue(jobs, 'weekly'), self.issue)
        data = json.loads(jobs.read_text())
        data['jobs'][0]['last_dispatch']['scheduled_at'] = '2026-11-13T08:06:00Z'
        jobs.write_text(json.dumps(data))
        with self.assertRaises(RuntimeError): weekly.dispatch_issue(jobs, 'weekly')

    def test_summer_dispatch_resolves_london_hour(self):
        jobs = self.root / 'jobs.json'
        jobs.write_text(json.dumps({'jobs': [{'id': 'weekly', 'timezone': 'Europe/London',
            'schedule': {'expr': '0 8 * * 5'}, 'last_dispatch': {'scheduled_at': '2026-07-10T07:00:00Z'}}]}))
        self.assertEqual(weekly.dispatch_issue(jobs, 'weekly').issue_id, '2026-07-10')

    def test_approval_binds_authentic_counts_provenance_and_review_digest(self):
        result = self.approve()
        self.assertIsNone(result['websitePreparationError'])
        meta = json.loads(Path(result['metadata']).read_bytes())
        self.assertEqual(meta['reviewedSha256'], result['reviewedSha256'])
        self.assertEqual(meta['qualifyingCounts'], self.counts)
        self.assertEqual(meta['scheduledAt'], self.issue.end)
        self.assertEqual(meta['selectedSha256'], digest((self.work / self.issue.issue_id / 'selected.json').read_bytes()))
        self.assertEqual(Path(result['metadata']).stat().st_mode & 0o777, 0o600)

    def test_approval_freezes_validated_bytes_despite_concurrent_source_save(self):
        original = json.dumps(self.review).encode()
        changed = copy.deepcopy(self.review)
        for group in changed['sections'][:2]:
            group['items'][0]['score'] += 1
        changed_raw = json.dumps(changed).encode()
        parity = weekly.selected_parity

        def save_after_parity(selected, review):
            parity(selected, review)
            (self.root / 'review.json').write_bytes(changed_raw)

        with patch.object(weekly, 'selected_parity', side_effect=save_after_parity) as check:
            result = self.approve()
        check.assert_called_once()
        self.assertEqual((self.root / 'review.json').read_bytes(), changed_raw)
        frozen = (self.state / self.issue.issue_id / 'reviewed.json').read_bytes()
        self.assertEqual(frozen, original)
        self.assertEqual(result['reviewedSha256'], digest(frozen))
        self.assertEqual(Store(self.state).load(self.issue.issue_id)['reviewedSha256'], digest(frozen))
        metadata = json.loads(Path(result['metadata']).read_bytes())
        self.assertEqual(metadata['reviewedSha256'], digest(frozen))
        self.assertIsNone(result['websitePreparationError'])

    def test_missing_metadata_directory_is_created(self):
        self.meta = self.root / 'missing' / 'metadata'
        self.assertFalse(self.meta.exists())
        result = self.approve()
        self.assertTrue(result['emailPrepared'])
        self.assertIsNone(result['websitePreparationError'])
        self.assertTrue(Path(result['metadata']).is_file())
        self.assertEqual(self.meta.stat().st_mode & 0o777, 0o700)

    def test_metadata_root_file_does_not_block_email_preparation(self):
        self.meta.write_text('not a directory')
        result = self.approve()
        self.assertTrue(result['emailPrepared'])
        self.assertIsNotNone(result['websitePreparationError'])
        self.assertEqual(Store(self.state).load(self.issue.issue_id)['email']['status'], 'pending')
        self.assertFalse(Path(result['metadata']).exists())
        self.assertEqual(self.meta.read_text(), 'not a directory')

    def test_metadata_directory_permission_failure_does_not_block_email_preparation(self):
        self.assertFalse(self.meta.exists())
        with patch.object(weekly, 'secure_directory', side_effect=PermissionError('metadata denied')):
            result = self.approve()
        self.assertTrue(result['emailPrepared'])
        self.assertEqual(result['websitePreparationError'], 'metadata denied')
        self.assertEqual(Store(self.state).load(self.issue.issue_id)['email']['status'], 'pending')
        self.assertFalse(Path(result['metadata']).exists())
        self.assertFalse(self.meta.exists())

    def test_website_provenance_failure_does_not_block_email_preparation(self):
        result = self.approve(sources=False)
        self.assertTrue(result['emailPrepared'])
        self.assertIsNotNone(result['websitePreparationError'])
        self.assertEqual(Store(self.state).load(self.issue.issue_id)['email']['status'], 'pending')
        self.assertFalse(Path(result['metadata']).exists())

    def test_research_cannot_change_selection_even_identically_in_both_rankings(self):
        for group in self.review['sections'][:2]:
            group['items'][0]['score'] += 1
        with self.assertRaises(RuntimeError): self.approve()
        self.assertFalse((self.state / self.issue.issue_id / 'state.json').exists())

    def test_review_cannot_omit_or_add_private_item_fields(self):
        self.review['sections'][0]['items'][0]['privateAddress'] = 'not-public@example.org'
        with self.assertRaises(RuntimeError): self.approve()

    def test_missing_summaries_rejected_before_state(self):
        del self.review['sections'][0]['items'][0]['story']
        with self.assertRaises((RuntimeError, ValueError)): self.approve()

    def test_changed_frozen_review_is_rejected(self):
        self.approve()
        for group in self.review['sections'][:2]:
            group['items'][0]['story'] = 'Different reviewed summary.'
        with self.assertRaises(RuntimeError): self.approve()

    def test_approval_required(self):
        with self.assertRaises(RuntimeError):
            weekly.approve(self.work, self.state, self.meta, self.issue, Path('absent'), None)

    def test_launch_gate_compares_source_to_main_without_merging(self):
        client = MagicMock()
        client.git_env.return_value = {'GIT_TERMINAL_PROMPT':'0'}
        with patch.dict(sys.modules, {'runtime.github_api':SimpleNamespace(GitHub=lambda settings: client)}), \
             patch.object(weekly.subprocess, 'run') as run, \
             patch.object(weekly.subprocess, 'check_output', side_effect=['', 'a'*40+'\n']):
            result = weekly.main_source({'repository':'owner/repo'})
        self.assertTrue(result['mergedSource'])
        self.assertEqual(result['commit'], 'a'*40)
        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual(commands[0], ['git','fetch','--no-tags','origin','main'])
        self.assertIn(':(exclude)dist/**', commands[1])
        self.assertFalse(any('push' in args or 'merge' in args for args in commands))

    def test_launch_gate_refuses_source_diff_and_untracked_source(self):
        client = MagicMock()
        with patch.dict(sys.modules, {'runtime.github_api':SimpleNamespace(GitHub=lambda settings: client)}):
            with patch.object(weekly.subprocess, 'run', side_effect=[None, subprocess.CalledProcessError(1, ['git','diff'])]):
                with self.assertRaises(subprocess.CalledProcessError): weekly.main_source({})
            with patch.object(weekly.subprocess, 'run'), \
                 patch.object(weekly.subprocess, 'check_output', return_value='unreviewed.py\n'):
                with self.assertRaisesRegex(RuntimeError, 'untracked source'):
                    weekly.main_source({})


if __name__ == '__main__':
    unittest.main()
