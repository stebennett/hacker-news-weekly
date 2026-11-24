"""Process/deadline guards for installed-code reconstruction (real pinned Node)."""
import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from runtime import trusted_build as trusted
from runtime.orchestrator import RuntimeError, bounded

ROOT = Path(__file__).resolve().parents[2]


def cleanup_probe(identity):
    """Kill only the exact probe task, even if its numeric PID was reused."""
    if not identity.exists():
        return
    record = json.loads(identity.read_text())
    try:
        fd = os.pidfd_open(record['pid'])
    except ProcessLookupError:
        return
    try:
        stat = Path(f"/proc/{record['pid']}/stat")
        if stat.exists() and stat.read_text().rsplit(')', 1)[1].split()[19] == record['start']:
            signal.pidfd_send_signal(fd, signal.SIGKILL)
    finally:
        os.close(fd)


class TrustedProcessTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);(self.root/'tmp').mkdir()
        self.node=shutil.which('node')
        self.assertIsNotNone(self.node)
        self.env={'HOME':str(self.root),'PATH':'/usr/bin:/bin'}
        self.assertTrue(callable(trusted.run_node))
        status,raw=self.run_node(['-e','process.stdout.write("control")'])
        self.assertEqual((status,raw),(0,b'control'))

    def run_node(self,args,seconds=5.0,**kwargs):
        deadline=time.monotonic()+seconds
        def remaining():
            value=deadline-time.monotonic()
            if value<=0:raise RuntimeError('test deadline exceeded')
            return value
        return trusted.run_node(self.node,args,self.root,self.env,remaining,**kwargs)

    def test_deadline_refuses_to_launch_even_pin_process(self):
        with patch.object(trusted.subprocess,'Popen',side_effect=AssertionError('must not launch after deadline')):
            with self.assertRaisesRegex(RuntimeError,'test deadline exceeded'):
                self.run_node(['--version'],seconds=-1)

    def test_timeout_kills_build_descendants_before_disposable_workspace_is_removed(self):
        marker=self.root/'orphan'
        child=f"setTimeout(()=>require('node:fs').writeFileSync({json.dumps(str(marker))},'bad'),1000)"
        script="require('node:child_process').spawn(process.execPath,['-e',"+json.dumps(child)+"]);setTimeout(()=>{},10000)"
        with self.assertRaisesRegex(RuntimeError,'exceeded deadline'):
            self.run_node(['-e',script],seconds=0.3)
        time.sleep(1.2)
        self.assertFalse(marker.exists(),'timed-out child must not survive process-group cleanup')

    def probe_script(self, name):
        identity, marker = self.root / (name + '.pid'), self.root / (name + '.late')
        self.addCleanup(cleanup_probe, identity)
        script = (
            "const f=require('node:fs');const stat=f.readFileSync('/proc/self/stat','utf8').split(') ')[1].split(' ');"
            f"f.writeFileSync({json.dumps(str(identity))},JSON.stringify({{pid:process.pid,"
            "start:stat[19],group:Number(stat[2]),session:Number(stat[3]),env:Object.keys(process.env)}));"
            f"setTimeout(()=>f.writeFileSync({json.dumps(str(marker))},'bad'),1500);"
            "setTimeout(()=>{},15000);"
        )
        return script, identity, marker

    def website_probe(self, script, *, exit_code=0, inner_seconds: float=12.0,
                      supervisor_override: int | None=None):
        # Real worker -> command(website_verify, own_group=False) -> helper -> Node.
        context_data = dict(issueId='2026-11-13', reviewedSha256='b'*64, commit='a'*40)
        evidence = dict(context_data, verified=True, production=True, https=True,
                        manifestMatched=True, htmlMatched=True, jsonMatched=True, cacheChecked=True,
                        deploymentId='probe', url='https://twihn.com/issues/2026-11-13/')
        adapter = (
            'import sys,time,json,os;from pathlib import Path;'
            f'sys.path.insert(0,{str(ROOT)!r});'
            'from runtime.trusted_build import run_node;'
            f'Path({str(self.root / "adapter.json")!r}).write_text(json.dumps('
            '{"pid":os.getpid(),"group":os.getpgrp(),"session":os.getsid(0),"parent":os.getppid()}));'
            + (f'os.environ["_TWIHN_STAGE_SUPERVISOR_PID"]={str(supervisor_override)!r};'
               if supervisor_override is not None else '') +
            f'deadline=time.monotonic()+{inner_seconds!r};'
            f'run_node({self.node!r},["-e",{script!r}],Path({str(self.root)!r}),'
            '{"PATH":"/usr/bin:/bin"},lambda:deadline-time.monotonic());'
            f'print({json.dumps(evidence)!r});sys.exit({exit_code})'
        )
        config, context = self.root / 'config.json', self.root / 'context.json'
        config.write_text(json.dumps({'website_verify': [sys.executable, '-c', adapter],
                                      'command_timeout': 20}))
        context.write_text(json.dumps(context_data))
        return [sys.executable, '-m', 'runtime', '_website-worker', '--config', str(config),
                '--context', str(context), '--verify-only']

    def assert_managed_identity(self, identity):
        node = json.loads(identity.read_text())
        adapter = json.loads((self.root / 'adapter.json').read_text())
        self.assertEqual(node['group'], adapter['parent'])
        self.assertEqual(node['session'], adapter['parent'])
        self.assertEqual(adapter['group'], adapter['parent'])
        self.assertNotEqual(node['pid'], node['group'])
        self.assertNotIn('_TWIHN_WEBSITE_GROUP_PID', node['env'])
        self.assertNotIn('_TWIHN_STAGE_SUPERVISOR_PID', node['env'])

    def exited_leader_script(self, child, identity, exit_code):
        # Do not exit the leader until its real child confirms startup.
        return (
            "const f=require('node:fs');require('node:child_process').spawn("
            f"process.execPath,['-e',{json.dumps(child)}],{{stdio:'ignore'}});"
            f"setInterval(()=>{{if(f.existsSync({json.dumps(str(identity))}))"
            f"process.exit({exit_code})}},5);"
        )

    def test_outer_website_timeout_kills_real_node_before_late_write(self):
        script, identity, marker = self.probe_script('outer-timeout')
        argv = self.website_probe(script)
        with self.assertRaisesRegex(RuntimeError, 'deadline'):
            bounded(argv, 0.7, cwd=ROOT)
        self.assertTrue(identity.is_file(), 'positive control: real Node must launch before outer timeout')
        time.sleep(1.6)
        self.assertFalse(marker.exists(), 'Node must not write after the outer website stage exits')
        self.assert_managed_identity(identity)

    def test_standalone_exited_leader_leaves_no_descendant_on_success_or_failure(self):
        for code in (0, 9):
            with self.subTest(exit_code=code):
                child, identity, marker = self.probe_script(f'standalone-{code}')
                script = self.exited_leader_script(child, identity, code)
                self.assertEqual(self.run_node(['-e', script]), (code, b''))
                self.assertTrue(identity.is_file())
                time.sleep(1.6)
                self.assertFalse(marker.exists(), 'an exited Node leader must not leave group descendants')

    def test_outer_group_cleans_descendants_after_website_success_or_nonzero_exit(self):
        for code in (0, 9):
            with self.subTest(exit_code=code):
                child, identity, marker = self.probe_script(f'managed-{code}')
                script = self.exited_leader_script(child, identity, 0)
                argv = self.website_probe(script, exit_code=code)
                if code:
                    with self.assertRaisesRegex(RuntimeError, 'adapter exited'):
                        bounded(argv, 5, cwd=ROOT)
                else:
                    self.assertTrue(json.loads(bounded(argv, 5, cwd=ROOT))['verified'])
                self.assertTrue(identity.is_file())
                self.assert_managed_identity(identity)
                time.sleep(1.6)
                self.assertFalse(marker.exists(), 'outer cleanup must include descendants of exited Node leaders')

    def test_managed_inner_timeout_leaves_descendant_cleanup_with_outer_owner(self):
        child, identity, marker = self.probe_script('managed-inner-timeout')
        script = (
            "require('node:child_process').spawn(process.execPath,"
            f"['-e',{json.dumps(child)}],{{stdio:'ignore'}});setTimeout(()=>{{}},15000);"
        )
        argv = self.website_probe(script, inner_seconds=0.3)
        with self.assertRaisesRegex(RuntimeError, 'adapter exited'):
            bounded(argv, 5, cwd=ROOT)
        self.assertTrue(identity.is_file())
        self.assert_managed_identity(identity)
        time.sleep(1.6)
        self.assertFalse(marker.exists())

    def test_worker_rejects_mismatched_stage_supervisor_before_adapter_launch(self):
        marker = self.root / 'must-not-run-adapter'
        script = f"require('node:fs').writeFileSync({json.dumps(str(marker))},'bad')"
        argv = self.website_probe(script)
        env = dict(os.environ, _TWIHN_STAGE_SUPERVISOR_PID='2147483647')
        env.pop('_TWIHN_WEBSITE_GROUP_PID', None)
        result = subprocess.run(argv, cwd=ROOT, env=env, capture_output=True, timeout=5,
                                start_new_session=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b'invalid website process supervisor', result.stderr)
        self.assertFalse(marker.exists())
        self.assertFalse((self.root / 'adapter.json').exists())

    def test_managed_adapter_rejects_existing_but_unrelated_outer_supervisor(self):
        marker = self.root / 'unrelated-supervisor-launched-node'
        script = f"require('node:fs').writeFileSync({json.dumps(str(marker))},'bad')"
        # PID 1 exists outside the newly-created website session, but is not
        # the test process that actually owns bounded's website worker.
        argv = self.website_probe(script, supervisor_override=1)
        with self.assertRaisesRegex(RuntimeError, 'invalid website process supervisor'):
            bounded(argv, 5, cwd=ROOT)
        self.assertTrue((self.root / 'adapter.json').exists(), 'real managed adapter must be reached')
        self.assertFalse(marker.exists(), 'a plausible but unrelated supervisor cannot authorize launch')

    def test_mismatched_managed_group_identity_refuses_node_launch(self):
        marker = self.root / 'must-not-launch'
        claims = [('invalid', str(os.getppid())), (str(os.getpid()), str(os.getppid())),
                  (str(os.getpgrp()), ''), (str(os.getpgrp()), str(os.getpid())),
                  ('2147483647', str(os.getppid())), (str(os.getppid()), '2147483647')]
        for claimed, supervisor in claims:
            with self.subTest(claimed=claimed, supervisor=supervisor):
                with patch.dict(os.environ, {'_TWIHN_WEBSITE_GROUP_PID': claimed,
                                             '_TWIHN_STAGE_SUPERVISOR_PID': supervisor}):
                    with self.assertRaisesRegex(RuntimeError, 'invalid website process supervisor'):
                        self.run_node(['-e', f"require('node:fs').writeFileSync({json.dumps(str(marker))},'bad')"])
                self.assertFalse(marker.exists())

    def test_nonzero_exit_and_output_byte_limit_are_not_synthesized_success(self):
        self.assertEqual(self.run_node(['-e','process.exit(9)']), (9,b''))
        with self.assertRaisesRegex(RuntimeError,'expectation byte limit'):
            self.run_node(['-e','process.stdout.write("oversized")'],max_output=1)

    def test_public_output_is_0644_even_when_parent_umask_is_private(self):
        import os
        old=os.umask(0o077)
        try:
            status,_=self.run_node(['-e',"require('node:fs').writeFileSync('public.json','{}')"])
        finally:
            os.umask(old)
        self.assertEqual(status,0)
        self.assertEqual((self.root/'public.json').stat().st_mode & 0o777,0o644)


if __name__=='__main__':unittest.main()
