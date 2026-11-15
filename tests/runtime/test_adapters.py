import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from runtime import adapters
from runtime.orchestrator import Issue, Store, Runtime, RuntimeError, digest
from test_runtime import reviewed


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.fake=self.root/'fake-himalaya'
        self.fake.write_text('''#!/usr/bin/env python3
import json,pathlib,sys
root=pathlib.Path(__file__).parent
args=sys.argv[1:]
with (root/'commands').open('a') as f:f.write(json.dumps(args)+'\\n')
if args[:2]==['account','check']:print(json.dumps({'success':True}))
elif args[:2]==['message','send']:
 (root/'sent.eml').write_bytes(pathlib.Path(args[-1]).read_bytes())
 print('fake local transport only')
elif args[:2]==['envelope','list']:print(json.dumps([{'id':'42'}] if (root/'sent.eml').exists() else []))
elif args[:2]==['message','read']:sys.stdout.buffer.write((root/'sent.eml').read_bytes())
else:sys.exit(9)
''')
        self.fake.chmod(0o700)
        self.settings=self.root/'mail.json'
        self.settings.write_text(json.dumps(dict(binary=str(self.fake),timeout=2)))
        self.issue=Issue.resolve('2026-11-13')
        self.store=Store(self.root/'state')
        source=self.root/'reviewed.json';source.write_text(json.dumps(reviewed(self.issue)))
        self.store.prepare(self.issue,source,approved=True)
        def cmd(mode):return [sys.executable,'-m','runtime.adapters',mode,'--settings',str(self.settings),'--context','{context}']
        self.config=self.root/'config.json'
        self.config.write_text(json.dumps(dict(sender='sender@example.org',recipient='reader@example.org',
            email_preflight=cmd('preflight'),email_send=cmd('send'),email_verify=cmd('verify'),
            website_publish=cmd('website-disabled'),command_timeout=10,website_timeout=3)))

    def run_runtime(self):
        with self.store.lock(): return Runtime(self.store,self.issue.issue_id,self.config).resume()

    def test_real_adapters_with_fake_local_transport_never_contact_smtp(self):
        cli=[sys.executable,'-m','runtime','--state-dir',str(self.store.root),'--config',str(self.config),'resume','--issue',self.issue.issue_id]
        result=subprocess.run(cli,capture_output=True,text=True,cwd=Path(__file__).resolve().parents[2])
        self.assertEqual(result.returncode,0,result.stderr)
        state=json.loads(result.stdout)
        self.assertEqual(state['website']['status'],'failed')
        self.assertEqual(state['email']['status'],'verified')
        self.assertEqual(state['email']['evidence']['sentItemsId'],'42')
        commands=(self.root/'commands').read_text()
        self.assertIn('"--",',commands)
        result=subprocess.run(cli,capture_output=True,text=True,cwd=Path(__file__).resolve().parents[2])
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual((self.root/'commands').read_text(),commands)

    def test_sent_content_parity_ignores_transit_headers_and_line_endings(self):
        self.run_runtime()
        path=self.root/'sent.eml'
        path.write_bytes(b'Received: by test transport\n'+path.read_bytes().replace(b'\r\n',b'\n'))
        context=json.loads((self.store.issue_dir(self.issue.issue_id)/'context.json').read_bytes())
        result=adapters.verify(json.loads(self.settings.read_bytes()),context)
        self.assertTrue(result['contentMatched'])
        self.assertNotEqual(result['readbackSha256'],context['messageSha256'])

    def test_matching_message_id_with_changed_body_is_not_verification(self):
        self.run_runtime();path=self.root/'sent.eml'
        path.write_bytes(path.read_bytes().replace(b'Reviewed source summary.',b'INVENTED source summary.'))
        context=json.loads((self.store.issue_dir(self.issue.issue_id)/'context.json').read_bytes())
        with self.assertRaises(RuntimeError):adapters.verify(json.loads(self.settings.read_bytes()),context)

    def test_missing_sent_copy_never_triggers_send_in_reconcile(self):
        self.run_runtime();(self.root/'sent.eml').unlink()
        context=json.loads((self.store.issue_dir(self.issue.issue_id)/'context.json').read_bytes())
        before=(self.root/'commands').read_text().count('"send"')
        with self.assertRaises(RuntimeError):adapters.verify(json.loads(self.settings.read_bytes()),context)
        self.assertEqual((self.root/'commands').read_text().count('"send"'),before)

if __name__=='__main__':unittest.main()
