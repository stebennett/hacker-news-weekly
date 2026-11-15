import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from runtime.orchestrator import Issue, Store, Runtime, RuntimeError as RunError, bounded, main
from runtime import orchestrator

HEADINGS = ['Top 10 stories by votes', 'Top 10 discussed stories', 'Top 10 Ask HN threads by comments', 'All popular Show HN threads (50+ points)']

def reviewed(issue) -> dict:
    def item(n):
        return dict(id=str(n), title=f'Title {n}', score=100-n, comments=100-n,
                    created=issue.start, url='https://example.org/article',
                    story='Reviewed source summary.', discussion='Reviewed discussion summary.')
    ordinary = [item(n) for n in range(1, 11)]
    ask = [item(n) for n in range(11, 21)]
    return dict(start=issue.start, end=issue.end, sections=[dict(heading=h, items=copy.deepcopy(items)) for h, items in zip(HEADINGS, [ordinary, ordinary, ask, []])])

class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.issue = Issue.resolve('2026-11-13')
        self.store = Store(self.root / 'private')
        self.source = self.root / 'reviewed.json'
        self.source.write_text(json.dumps(reviewed(self.issue)))
        self.adapter = self.root / 'adapter.py'
        self.adapter.write_text('''import json,sys,pathlib,hashlib,time,os
mode=sys.argv[1]; ctx=json.loads(pathlib.Path(sys.argv[2]).read_text()); root=pathlib.Path(sys.argv[3])
with (root/'calls').open('a') as f:f.write(mode+'\\n')
if mode=='fail':sys.exit(1)
if mode=='sleep':time.sleep(30)
if mode=='publish':print(json.dumps({'commit':'a'*40}))
if mode=='verify-site':print(json.dumps(dict(verified=True,issueId=ctx['issueId'],reviewedSha256=ctx['reviewedSha256'],commit=ctx['commit'],deploymentId='deploy-1',url='https://twihn.com/issues/'+ctx['issueId']+'/',production=True,https=True,manifestMatched=True,htmlMatched=True,jsonMatched=True,cacheChecked=True)))
if mode=='preflight':print(json.dumps(dict(accountChecked=True,sentItemsChecked=True,sender='sender@example.org',recipient='reader@example.org')))
if mode=='verify-email':print(json.dumps(dict(verified=True,messageId=ctx['messageId'],messageSha256=ctx['messageSha256'],recipient='reader@example.org',sentItemsId='sent-1')))
''')
        def cmd(mode): return [sys.executable, str(self.adapter), mode, '{context}', str(self.root)]
        self.config: dict = dict(sender='sender@example.org', recipient='reader@example.org', website_publish=cmd('publish'), website_verify=cmd('verify-site'),
                           email_preflight=cmd('preflight'), email_send=cmd('send'), email_verify=cmd('verify-email'),
                           website_timeout=2, command_timeout=2)
        self.config_path = self.root / 'config.json'
        self.save_config()

    def save_config(self): self.config_path.write_text(json.dumps(self.config))
    def prep(self):
        return self.store.prepare(self.issue, self.source, approved=True)
    def run_runtime(self, website_only=False):
        self.save_config()
        with self.store.lock():
            return Runtime(self.store, self.issue.issue_id, self.config_path).resume(website_only=website_only)
    def calls(self):
        p = self.root / 'calls'
        return p.read_text().splitlines() if p.exists() else []

    def test_dst_bounds_are_168_hours_not_local_week(self):
        for issue_id, end, start in [('2026-03-27','2026-03-27T08:00:00Z','2026-03-20T08:00:00Z'),('2026-04-03','2026-04-03T07:00:00Z','2026-03-27T07:00:00Z'),('2026-10-30','2026-10-30T08:00:00Z','2026-10-23T08:00:00Z')]:
            issue = Issue.resolve(issue_id)
            self.assertEqual((issue.start, issue.end), (start,end))
        with self.assertRaises(ValueError): Issue.resolve('2026-11-14')

    def test_prep_freezes_bytes_restricts_modes_and_rejects_changed_review(self):
        self.prep()
        folder = self.store.issue_dir(self.issue.issue_id)
        self.assertEqual((folder / 'reviewed.json').read_bytes(), self.source.read_bytes())
        self.assertEqual(folder.stat().st_mode & 0o777, 0o700)
        self.assertEqual((folder/'state.json').stat().st_mode & 0o777, 0o600)
        self.source.write_text(self.source.read_text()+' ')
        with self.assertRaises(RunError): self.prep()

    def test_missing_review_approval_and_mismatched_window_rejected(self):
        with self.assertRaises(RunError): self.store.prepare(self.issue,self.source,approved=False)
        data = reviewed(self.issue);data['end']='2026-11-13T09:00:00Z';self.source.write_text(json.dumps(data))
        with self.assertRaises((RunError,ValueError)): self.prep()

    def test_cross_section_summary_must_be_identical(self):
        data=reviewed(self.issue);data['sections'][1]['items'][0]['story']='Changed.'
        self.source.write_text(json.dumps(data))
        with self.assertRaises((RunError,ValueError)): self.prep()

    def test_ask_ordinary_overlap_rejected_even_when_tags_missing(self):
        data=reviewed(self.issue)
        data['sections'][2]['items'][0]=copy.deepcopy(data['sections'][0]['items'][0])
        self.source.write_text(json.dumps(data))
        with self.assertRaises((RunError,ValueError)):self.prep()

    def test_zero_hn_id_is_not_a_valid_literal_identifier(self):
        data=reviewed(self.issue)
        for section in data['sections'][:2]:section['items'][0]['id']='0'
        self.source.write_text(json.dumps(data))
        with self.assertRaises((RunError,ValueError)):self.prep()

    def test_full_mock_integration_and_completed_noop(self):
        self.prep();state=self.run_runtime()
        self.assertEqual(state['email']['status'],'verified');self.assertEqual(state['website']['status'],'verified')
        self.assertIn('https://twihn.com/issues/2026-11-13/',(self.store.issue_dir(self.issue.issue_id)/'send.eml').read_text())
        before=self.calls();self.run_runtime();self.assertEqual(self.calls(),before)

    def test_website_failure_still_sends_baseline(self):
        self.config['website_publish'][2]='fail';self.prep();state=self.run_runtime()
        self.assertEqual(state['website']['status'],'failed');self.assertEqual(state['email']['status'],'verified')
        self.assertNotIn('https://twihn.com/issues/',(self.store.issue_dir(self.issue.issue_id)/'send.eml').read_text())

    def test_unverified_exit_zero_cannot_add_link(self):
        self.config['website_verify'][2]='send';self.prep();state=self.run_runtime()
        self.assertEqual(state['website']['status'],'failed');self.assertEqual(state['email']['status'],'verified')

    def test_ambiguous_send_never_retried_even_if_readback_absent(self):
        self.config['email_send'][2]='fail';self.config['email_verify'][2]='send'
        self.prep();state=self.run_runtime();self.assertEqual(state['email']['status'],'ambiguous')
        self.run_runtime();self.assertEqual(self.calls().count('fail'),1)
        self.save_config()
        self.assertEqual(main(['--state-dir',str(self.store.root),'--config',str(self.config_path),'resume','--issue',self.issue.issue_id]),2)
        self.assertEqual(self.calls().count('fail'),1)

    def test_exit_zero_without_email_readback_is_ambiguous(self):
        self.config['email_verify'][2]='send';self.prep()
        self.assertEqual(self.run_runtime()['email']['status'],'ambiguous')

    def test_crash_send_intent_blocks_cli_resend(self):
        state=self.prep();state['email']['status']='in_progress';self.store.save(state)
        self.run_runtime();self.assertNotIn('send',self.calls())
        self.assertEqual(self.store.load(self.issue.issue_id)['email']['status'],'ambiguous')

    def test_website_repair_never_runs_email_commands(self):
        self.prep();state=self.run_runtime(website_only=True)
        self.assertEqual(state['email']['status'],'pending')
        self.assertEqual(self.calls(),['publish','verify-site'])

    def test_reconcile_only_verifies_does_not_send(self):
        self.config['email_verify'][2]='send';self.prep();self.run_runtime()
        self.config['email_verify'][2]='verify-email';self.save_config()
        with self.store.lock(): state=Runtime(self.store,self.issue.issue_id,self.config_path).reconcile('email')
        self.assertEqual(state['email']['status'],'verified');self.assertEqual(self.calls().count('send'),2)

    def test_tamper_blocked_before_any_external_commands(self):
        self.prep();(self.store.issue_dir(self.issue.issue_id)/'reviewed.json').write_text('{}')
        with self.assertRaises(RunError): self.run_runtime()
        self.assertEqual(self.calls(),[])

    def test_lock_rejects_concurrent_writer(self):
        with self.store.lock():
            result=subprocess.run([sys.executable,'-c','from runtime.orchestrator import Store;import sys;\nwith Store(sys.argv[1]).lock():pass',str(self.store.root)],cwd=ROOT,capture_output=True)
        self.assertNotEqual(result.returncode,0)

    def test_timeout_kills_descendants(self):
        pidfile=self.root/'pid'
        code='import subprocess,time,sys; p=subprocess.Popen([sys.executable,"-c","import time;time.sleep(30)"]);open(sys.argv[1],"w").write(str(p.pid));time.sleep(30)'
        with self.assertRaises(RunError): bounded([sys.executable,'-c',code,str(pidfile)],0.3)
        pid=int(pidfile.read_text());stat=Path(f'/proc/{pid}/stat')
        self.assertTrue(not stat.exists() or stat.read_text().split()[2]=='Z')

    def test_whole_website_deadline_kills_adapter_descendants_and_email_survives(self):
        pidfile=self.root/'website-child'
        code='import subprocess,time,sys; p=subprocess.Popen([sys.executable,"-c","import time;time.sleep(30)"]);open(sys.argv[1],"w").write(str(p.pid));time.sleep(30)'
        self.config['website_publish']=[sys.executable,'-c',code,str(pidfile)]
        self.config['website_timeout']=0.5;self.config['command_timeout']=20
        self.prep();state=self.run_runtime()
        self.assertEqual(state['website']['status'],'failed')
        self.assertEqual(state['email']['status'],'verified')
        pid=int(pidfile.read_text());stat=Path(f'/proc/{pid}/stat')
        self.assertTrue(not stat.exists() or stat.read_text().split()[2]=='Z')

    def test_linked_render_failure_falls_back_to_validated_baseline(self):
        original=Runtime.render
        def renderer(runtime,website_url=None):
            if website_url:raise ValueError('linked template failure')
            return original(runtime)
        self.prep()
        with patch.object(Runtime,'render',renderer):state=self.run_runtime()
        self.assertEqual(state['email']['status'],'verified')
        self.assertNotIn('https://twihn.com/issues/',(self.store.issue_dir(self.issue.issue_id)/'send.eml').read_text())

    def test_baseline_validation_failure_stops_both_channels(self):
        self.prep()
        with patch.object(Runtime,'render',side_effect=ValueError('invalid baseline')):state=self.run_runtime()
        self.assertEqual(state['email']['status'],'failed');self.assertEqual(self.calls(),[])

    def test_preflight_failure_is_retryable_before_any_send_intent(self):
        self.config['email_preflight'][2]='fail';self.prep()
        state=self.run_runtime();self.assertEqual(state['email']['status'],'failed')
        self.assertNotIn('send',self.calls());self.assertNotIn('messageId',state['email'])
        self.config['email_preflight'][2]='preflight'
        self.assertEqual(self.run_runtime()['email']['status'],'verified')

    def test_frozen_recipient_change_rejected_before_external_work(self):
        self.prep();self.run_runtime(website_only=True);before=self.calls()
        self.config['recipient']='another@example.org'
        with self.assertRaises(RunError):self.run_runtime()
        self.assertEqual(self.calls(),before)

    def test_orphan_review_cannot_reset_missing_delivery_state(self):
        self.prep();(self.store.issue_dir(self.issue.issue_id)/'state.json').unlink()
        with self.assertRaises(RunError):self.prep()

    def test_symlink_state_root_is_rejected(self):
        (self.root/'link').symlink_to(self.store.root,target_is_directory=True)
        with self.assertRaises(RunError):Store(self.root/'link')

    def test_crash_after_submission_never_retries_on_resume(self):
        self.prep();self.save_config();original=orchestrator.command
        def crash_after_send(config,key,*args,**kwargs):
            result=original(config,key,*args,**kwargs)
            if key=='email_send':raise SystemExit('simulated crash after external submission')
            return result
        with self.store.lock(),patch.object(orchestrator,'command',side_effect=crash_after_send):
            with self.assertRaises(SystemExit):Runtime(self.store,self.issue.issue_id,self.config_path).resume()
        self.assertEqual(self.store.load(self.issue.issue_id)['email']['status'],'in_progress')
        state=self.run_runtime()
        self.assertEqual(state['email']['status'],'ambiguous');self.assertEqual(self.calls().count('send'),1)

    def test_interrupted_website_commit_is_read_back_before_republishing(self):
        state=self.prep();state['website']['status']='in_progress';self.store.save(state)
        context=dict(issueId=self.issue.issue_id,reviewedSha256=state['reviewedSha256'],commit='a'*40)
        (self.store.issue_dir(self.issue.issue_id)/'context.json').write_text(json.dumps(context))
        result=self.run_runtime(website_only=True)
        self.assertEqual(result['website']['status'],'verified')
        self.assertEqual(self.calls(),['verify-site'])

if __name__=='__main__': unittest.main()
