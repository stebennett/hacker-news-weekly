import base64
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from runtime import site_verify as site
from runtime.orchestrator import Issue, RuntimeError, digest
from test_runtime import reviewed


class SiteReadbackTests(unittest.TestCase):
    def setUp(self):
        network=patch.object(site,'urlopen',side_effect=AssertionError('offline verifier tests must not open network'))
        network.start();self.addCleanup(network.stop)
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);issue=Issue.resolve('2026-11-13')
        legacy=reviewed(issue);path=self.root/'reviewed.json';path.write_text(json.dumps(legacy))
        self.context=dict(issueId=issue.issue_id,revision=1,start=issue.start,end=issue.end,
                          reviewed=str(path),reviewedSha256=digest(path.read_bytes()),commit='a'*40)
        public: dict = dict(issueId=issue.issue_id,revision=1,window=dict(start=issue.start,end=issue.end),sections=[],items={})
        for section,ident in zip(legacy['sections'],['votes','comments','ask','show']):
            public['sections'].append(dict(id=ident,itemIds=[str(x['id']) for x in section['items']]))
            for x in section['items']:
                public['items'][x['id']]=dict(id=x['id'],title=x['title'],score=x['score'],comments=x['comments'],
                    submittedAt=x['created'],sourceUrl=x['url'],story=x['story'],discussion=x['discussion'])
        self.html=b'<!doctype html><html>reviewed fixture</html>'
        data=json.dumps(public).encode()
        self.tracked={'issues/2026-11-13/index.html':self.html,'data/issues/2026-11-13.json':data}
        self.tracked['build-manifest.json']=json.dumps(dict(artifacts={p:digest(b) for p,b in self.tracked.items()})).encode()
        self.settings=dict(repository='owner/repo',environment='Production',verify_timeout=0.02,poll_interval=0.01)

    def api(self,settings,path):
        if '/contents/' in path:
            name=path.split('/contents/dist/')[1].split('?')[0]
            return dict(type='file',encoding='base64',content=base64.b64encode(self.tracked[name]).decode())
        if '/commits/' in path:return dict(sha='a'*40)
        if '/statuses?' in path:return [dict(state='success')]
        return [dict(id=7,sha='a'*40,environment='Production')]

    def live(self,settings,path):
        name=path+'index.html' if path.endswith('/') else path
        raw=self.tracked[name]
        return raw,dict(url='https://twihn.com/'+path,cacheControl='public, max-age=0, must-revalidate',sha256=digest(raw))

    def test_resolve_and_verify_exact_commit_artifacts_and_semantic_parity(self):
        with patch.object(site,'api',self.api),patch.object(site,'live',self.live):
            self.assertEqual(site.resolve(self.settings,self.context),dict(commit='a'*40))
            result=site.verify(self.settings,self.context)
        self.assertEqual(result['deploymentId'],'7');self.assertEqual(len(result['artifacts']),3)

    def test_stale_deployment_cannot_verify_current_commit(self):
        def api(settings,path):
            if '/deployments?' in path:return [dict(id=7,sha='b'*40,environment='Production')]
            return self.api(settings,path)
        with patch.object(site,'api',api),self.assertRaises(RuntimeError):site.verify(self.settings,self.context)

    def test_live_heading_match_without_exact_bytes_is_rejected(self):
        def live(settings,path):return (b'<!doctype html><html>same heading but wrong data</html>',dict(url=path))
        with patch.object(site,'api',self.api),patch.object(site,'live',live),self.assertRaises(RuntimeError):site.verify(self.settings,self.context)

    def test_changed_summary_blocks_production_resolution(self):
        path='data/issues/2026-11-13.json';data=json.loads(self.tracked[path]);data['items']['1']['story']='Unreviewed.'
        self.tracked[path]=json.dumps(data).encode()
        with patch.object(site,'api',self.api),self.assertRaises(RuntimeError):site.resolve(self.settings,self.context)

    def test_https_live_fetch_requires_revalidation_and_exact_canonical_url(self):
        class Response:
            status=200
            headers={'Cache-Control':'public, max-age=0, must-revalidate'}
            def geturl(self):return 'https://twihn.com/build-manifest.json'
            def read(self,size):return b'{}'
            def __enter__(self):return self
            def __exit__(self,*args):pass
        response=Response()
        with patch.object(site,'urlopen',return_value=response):
            raw,_=site.live(self.settings,'build-manifest.json');self.assertEqual(raw,b'{}')
            for cache in ('', 'public, max-age=31536000, immutable', 'public, max-age=01',
                          'public, s-max-age=0', 'x-no-cache', 'public, max-age=3600'):
                response.headers={'Cache-Control':cache}
                with self.subTest(cache=cache),self.assertRaises(RuntimeError):site.live(self.settings,'build-manifest.json')
            response.headers={'Cache-Control':'no-cache'}
            for url in ('https://preview.example.org/build-manifest.json',
                        'http://twihn.com/build-manifest.json',
                        'https://twihn.com/build-manifest.json?redirected=1'):
                response.geturl=lambda:url
                with self.subTest(url=url),self.assertRaises(RuntimeError):site.live(self.settings,'build-manifest.json')
            response.geturl=lambda:'https://twihn.com/build-manifest.json'
            response.status=304
            with self.assertRaises(RuntimeError):site.live(self.settings,'build-manifest.json')

class CloudflareReadbackTests(SiteReadbackTests):
    def setUp(self):
        super().setUp()
        self.settings['deployment_evidence']='cloudflare-check-run'
        self.check=dict(id=123,name='Cloudflare Pages',head_sha='a'*40,status='completed',conclusion='success',
            app=dict(id=85455,slug='cloudflare-workers-and-pages'),check_suite=dict(id=456),
            external_id='8396c9cb-b6d2-4e59-a20d-66fc811fbc23',
            details_url='https://dash.cloudflare.com/?to=/account/pages/view/twihn/8396c9cb-b6d2-4e59-a20d-66fc811fbc23')
        self.suite=dict(id=456,head_branch='main',head_sha='a'*40,app=self.check['app'])
        self.records=[self.check]

    def api(self,settings,path):
        if '/check-runs?' in path:return dict(total_count=len(self.records),check_runs=self.records)
        if '/check-runs/' in path:return self.check
        if '/check-suites/' in path:return self.suite
        return super().api(settings,path)

    # Compatibility assertions above apply specifically to the legacy mode.
    def test_resolve_and_verify_exact_commit_artifacts_and_semantic_parity(self):
        with patch.object(site,'api',self.api),patch.object(site,'live',self.live):
            self.assertEqual(site.resolve(self.settings,self.context),dict(commit='a'*40))
            result=site.verify(self.settings,self.context)
        self.assertEqual(result['checkRunId'],'123')
        self.assertEqual(result['checkRunDetailsUrl'],self.check['details_url'])
        self.assertEqual(result['deploymentEvidence'],'cloudflare-check-run')
        self.assertEqual(result['deploymentId'],self.check['external_id'])
        self.assertEqual(result['productionBranch'],'main')
        self.assertEqual(len(result['artifacts']),3)

    def test_stale_deployment_cannot_verify_current_commit(self):
        self.check['head_sha']='b'*40
        with patch.object(site,'api',self.api),self.assertRaises(RuntimeError):site.verify(self.settings,self.context)

    def test_reject_wrong_conclusion_status_name_app_sha_or_project(self):
        changes=[('conclusion','failure'),('conclusion','neutral'),('status','in_progress'),
                 ('name','validate'),('head_sha','b'*40),('app',dict(id=15368,slug='github-actions')),
                 ('app',dict(id=85455,slug='imposter')),('app',dict(id=1,slug='cloudflare-workers-and-pages')),
                 ('details_url','https://preview.example.org/'),
                 ('details_url',self.check['details_url'].replace('/twihn/','/another/'))]
        for key,value in changes:
            with self.subTest(key=key,value=value),patch.dict(self.check,{key:value}),\
                 patch.object(site,'api',self.api),patch.object(site,'live',self.live),self.assertRaises(RuntimeError):
                site.verify(self.settings,self.context)

    def test_no_records_is_not_production(self):
        self.records=[]
        with patch.object(site,'api',self.api),self.assertRaises(RuntimeError):site.verify(self.settings,self.context)

    def test_preview_or_wrong_suite_sha_or_app_is_rejected(self):
        for key,value in [('head_branch','feature/preview'),('head_sha','b'*40),('app',dict(id=1,slug='fake'))]:
            with self.subTest(key=key),patch.dict(self.suite,{key:value}),\
                 patch.object(site,'api',self.api),patch.object(site,'live',self.live),self.assertRaises(RuntimeError):
                site.verify(self.settings,self.context)

    def test_current_production_branch_must_still_equal_expected_sha(self):
        def api(settings,path):
            if path.endswith('/commits/main'):return dict(sha='b'*40)
            return self.api(settings,path)
        with patch.object(site,'api',api),patch.object(site,'live',self.live),self.assertRaises(RuntimeError):
            site.verify(self.settings,self.context)

    def test_production_branch_move_during_live_readback_is_rejected(self):
        moved=False
        def live(settings,path):
            nonlocal moved
            moved=True
            return self.live(settings,path)
        def api(settings,path):
            if path.endswith('/commits/main') and moved:return dict(sha='b'*40)
            return self.api(settings,path)
        with patch.object(site,'api',api),patch.object(site,'live',live),self.assertRaises(RuntimeError):
            site.verify(self.settings,self.context)

    def test_newer_pending_check_cannot_be_hidden_by_older_success(self):
        self.records=[self.check,dict(self.check,id=124,status='in_progress',conclusion=None)]
        with patch.object(site,'api',self.api),patch.object(site,'live',self.live),self.assertRaises(RuntimeError):
            site.verify(self.settings,self.context)

    def test_eventual_check_and_live_success_with_bounded_polling(self):
        clock=[0.0];attempts=[0]
        self.settings.update(verify_timeout=10,poll_interval=2)
        def sleep(seconds):clock[0]+=seconds
        def api(settings,path):
            if '/check-runs?' in path:
                attempts[0]+=1
                if attempts[0]==1:return dict(total_count=0,check_runs=[])
            return self.api(settings,path)
        def live(settings,path):
            if attempts[0]==2:return b'stale',dict(url=path)
            return self.live(settings,path)
        with patch.object(site,'api',api),patch.object(site,'live',live),\
             patch.object(site.time,'monotonic',side_effect=lambda:clock[0]),patch.object(site.time,'sleep',sleep):
            self.assertTrue(site.verify(self.settings,self.context)['verified'])
        self.assertEqual(attempts[0],3);self.assertEqual(clock[0],4)

    def test_poll_deadline_limits_sleep_and_request_timeouts(self):
        clock=[0.0];timeouts=[];self.records=[]
        self.settings.update(verify_timeout=3,poll_interval=2,timeout=30)
        def api(settings,path):
            timeouts.append(settings['timeout']);self.assertEqual(settings['_deadline'],3);return self.api(settings,path)
        def sleep(seconds):clock[0]+=seconds
        with patch.object(site,'api',api),patch.object(site.time,'monotonic',side_effect=lambda:clock[0]),\
             patch.object(site.time,'sleep',sleep),self.assertRaises(RuntimeError):site.verify(self.settings,self.context)
        self.assertEqual(clock[0],3);self.assertTrue(all(0<x<=3 for x in timeouts))

    def test_check_run_readback_must_match_listed_id(self):
        self.records=[dict(self.check,id=999)]
        with patch.object(site,'api',self.api),patch.object(site,'live',self.live),self.assertRaises(RuntimeError):
            site.verify(self.settings,self.context)

    def test_listed_sha_must_also_match_expected_commit(self):
        self.records=[dict(self.check,head_sha='b'*40)]
        with patch.object(site,'api',self.api),patch.object(site,'live',self.live),self.assertRaises(RuntimeError):
            site.verify(self.settings,self.context)

    def test_frozen_review_change_during_readback_is_rejected(self):
        def live(settings,path):
            Path(self.context['reviewed']).write_text('{}')
            return self.live(settings,path)
        with patch.object(site,'api',self.api),patch.object(site,'live',live),self.assertRaises(RuntimeError):
            site.verify(self.settings,self.context)

    def test_real_cloudflare_deployment_uuid_is_required_not_fabricated(self):
        for external in (None,'','not-a-deployment','123'):
            with self.subTest(external=external),patch.dict(self.check,external_id=external),\
                 patch.object(site,'api',self.api),patch.object(site,'live',self.live),self.assertRaises(RuntimeError):
                site.verify(self.settings,self.context)

    def test_wrong_account_is_rejected_when_explicitly_pinned(self):
        self.settings['cloudflare_account_id']='different'
        with patch.object(site,'api',self.api),patch.object(site,'live',self.live),self.assertRaises(RuntimeError):
            site.verify(self.settings,self.context)

    def test_invalid_polling_settings_are_rejected_before_any_api(self):
        for key in ('verify_timeout','poll_interval','timeout'):
            for value in (0,-1,float('inf'),float('nan')):
                with self.subTest(key=key,value=value),patch.dict(self.settings,{key:value}),\
                     patch.object(site,'api',side_effect=AssertionError('must not call API')),self.assertRaises(RuntimeError):
                    site.verify(self.settings,self.context)

    def test_manifest_inconsistency_is_terminal_before_polling(self):
        self.tracked['build-manifest.json']=b'{"artifacts":{}}'
        with patch.object(site,'api',self.api),patch.object(site.time,'sleep',side_effect=AssertionError('must not poll')),\
             self.assertRaises(RuntimeError):site.verify(self.settings,self.context)

    def test_unknown_evidence_mode_fails_closed(self):
        self.settings['deployment_evidence']='generic-ci'
        with patch.object(site,'api',self.api),self.assertRaises(RuntimeError):site.verify(self.settings,self.context)


class GitHubAdapterTests(unittest.TestCase):
    def test_lazy_read_only_helper_without_gh_or_subprocess(self):
        from types import ModuleType
        from unittest.mock import Mock
        helper=ModuleType('runtime.github_api');helper.GitHub=Mock()
        helper.GitHub.return_value.request.return_value={'sha':'a'*40}
        settings={'repository':'owner/repo'}
        with patch.dict(sys.modules,{'runtime.github_api':helper}),patch('subprocess.run',side_effect=AssertionError('no gh')):
            self.assertEqual(site.api(settings,'repos/owner/repo/commits/main'),{'sha':'a'*40})
        helper.GitHub.assert_called_once_with(settings,write=False)
        helper.GitHub.return_value.request.assert_called_once_with('GET','repos/owner/repo/commits/main')


if __name__=='__main__':unittest.main()
