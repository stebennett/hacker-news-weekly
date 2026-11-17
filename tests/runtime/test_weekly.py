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
        self.now = datetime(2026, 11, 13, 8, 1, tzinfo=timezone.utc)

    def begin(self):
        with patch.object(weekly, 'collect_issue', return_value=(self.selected, self.counts)) as collect:
            result = weekly.begin(self.work, self.issue, now=self.now)
        return result, collect

    def approve(self, sources=True):
        self.begin()
        path = self.root / 'review.json'
        path.write_text(json.dumps(self.review))
        provenance = self.root / 'sources.json'
        ids = {x['id'] for g in self.review['sections'] for x in g['items']}
        provenance.write_text(json.dumps({'itemSources': {i: {'sources': [], 'availability': {}} for i in ids}}))
        result = weekly.approve(self.work, self.state, self.meta, self.issue, path,
                                provenance if sources else None, approved=True)
        return result

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
