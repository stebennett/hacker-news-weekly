"""Process/deadline guards for installed-code reconstruction (real pinned Node)."""
import json
from pathlib import Path
import shutil
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from runtime import trusted_build as trusted
from runtime.orchestrator import RuntimeError


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
