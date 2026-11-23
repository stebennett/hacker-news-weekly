import base64
import gzip
import io
import json
from pathlib import Path
import shutil
import subprocess
import tarfile
import time
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
        self.registry=dict(schemaVersion=1,tags=[dict(slug='ai',label='AI')])
        self.tracked['data/tags.json']=json.dumps(self.registry).encode()
        self.records={ident:dict(schemaVersion=1,id=ident,tagSlugs=['ai'],issueIds=[issue.issue_id]) for ident in public['items']}
        for ident,record in self.records.items():
            self.tracked[f'data/stories/{ident}.json']=json.dumps(record).encode()
        for route in ['tags/index.html','tags/ai/index.html','tags/ai/2/index.html']:
            self.tracked[route]=f'<!doctype html><html>{route}</html>'.encode()
        self.tracked['build-manifest.json']=json.dumps(dict(artifacts={p:digest(b) for p,b in self.tracked.items() if p!='build-manifest.json'})).encode()
        self.tagging=dict(verified=True,expectation='exact-commit-validated-inventory',affectedTags=['ai'],
                          assignments={ident:['ai'] for ident in public['items']},localTagPageCount=2,
                          boundaries=[dict(slug='ai',left='tags/ai/index.html',right='tags/ai/2/index.html')])
        # Mock only the immutable archive acquisition/local semantic-verifier
        # boundary. Dedicated integration tests exercise that boundary for real.
        expected=patch.object(site,'expected_artifacts',side_effect=lambda settings,context:(dict(self.tracked),dict(self.tagging)))
        expected.start();self.addCleanup(expected.stop)
        self.settings=dict(repository='owner/repo',environment='Production',verify_timeout=0.02,poll_interval=0.01)

    def api(self,settings,path):
        if '/contents/' in path:
            name=path.split('/contents/')[1].split('?')[0]
            self.assertTrue(name.startswith('data/'), 'resolve must read canonical source, never dist')
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
        self.assertEqual(result['deploymentId'],'7');self.assertEqual(len(result['artifacts']),len(self.tracked))

    def test_tag_evidence_contains_exact_paths_assignments_and_boundary_not_full_crawl_claim(self):
        self.assertTrue(callable(site.verify))
        with patch.object(site,'api',self.api),patch.object(site,'live',self.live):
            result=site.verify(self.settings,self.context)
        self.assertEqual(result['tagging']['assignments'],self.tagging['assignments'])
        self.assertEqual(result['tagging']['boundaries'],self.tagging['boundaries'])
        self.assertFalse(result['tagging']['allHistoricalPagesFetched'])
        # Conservative HTTP debit includes the one archive request and each
        # fresh GitHub client's authentication POST as well as its target GET.
        overhead=13 if self.settings.get('deployment_evidence')=='cloudflare-check-run' else 11
        self.assertEqual(result['requestBudgetUsed'],len(self.tracked)+overhead)
        self.assertLessEqual(result['requestBudgetUsed'],result['requestBudget'])
        self.assertEqual(result['checkedPaths'],list(self.tracked))
        self.assertEqual({item['path']:item['sha256'] for item in result['artifacts']},
                         {path:digest(raw) for path,raw in self.tracked.items()})

    def test_wrong_live_tag_registry_story_directory_page_or_manifest_is_rejected(self):
        self.assertTrue(callable(site.verify))
        with patch.object(site,'api',self.api),patch.object(site,'live',self.live):
            self.assertTrue(site.verify(self.settings,self.context)['verified'])
        for wrong in ['data/tags.json','data/stories/1.json','tags/index.html',
                      'tags/ai/index.html','tags/ai/2/index.html','build-manifest.json']:
            def live(settings,path):
                name=path+'index.html' if path.endswith('/') else path
                return (b'wrong',dict(url=path)) if name==wrong else self.live(settings,path)
            with self.subTest(path=wrong),patch.object(site,'api',self.api),patch.object(site,'live',live),self.assertRaisesRegex(RuntimeError,'live artifact differs.*'+wrong):
                site.verify(self.settings,self.context)

    def test_missing_tag_evidence_never_passes_as_complete_deployment(self):
        self.tagging.clear()
        with patch.object(site,'api',self.api),patch.object(site,'live',self.live),self.assertRaisesRegex(RuntimeError,'tagging evidence'):
            site.verify(self.settings,self.context)

    def test_request_budget_fails_before_live_fetch_without_weakening_sample(self):
        self.settings['verify_max_requests']=8
        with patch.object(site,'api',self.api),patch.object(site,'live',side_effect=AssertionError('no partial sampling')),self.assertRaisesRegex(RuntimeError,'request budget'):
            site.verify(self.settings,self.context)

    def test_global_request_cap_includes_retries_and_does_not_reset_per_poll(self):
        self.settings.update(verify_max_requests=80,verify_timeout=100,poll_interval=1)
        clock=[0.0];requests=[];live_reads=[]
        # The existing api seam creates a fresh scoped GitHub client per call:
        # reserve BOTH installation-token POST and target GET, not just the GET.
        def api(settings,path):requests.extend(['auth',path]);return self.api(settings,path)
        def live(settings,path):requests.append(path);live_reads.append(path);return b'stale',dict(url=path)
        with patch.object(site,'api',api),patch.object(site,'live',live),patch.object(site.time,'monotonic',side_effect=lambda:clock[0]),patch.object(site.time,'sleep',side_effect=lambda n:clock.__setitem__(0,clock[0]+n)),self.assertRaisesRegex(RuntimeError,'request budget'):
            site.verify(self.settings,self.context)
        self.assertLessEqual(len(requests)+1,80) # one immutable archive request
        self.assertGreater(len(live_reads),1)
        self.assertLess(clock[0],100)

    def test_invalid_request_budgets_fail_before_any_remote_read(self):
        for value in (0,-1,True,1.5,129):
            with self.subTest(value=value),patch.dict(self.settings,verify_max_requests=value),patch.object(site,'api',side_effect=AssertionError('no remote read')),self.assertRaisesRegex(RuntimeError,'request budget'):
                site.verify(self.settings,self.context)

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
        self.assertEqual(len(result['artifacts']),len(self.tracked))

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


class ExactArchiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp=tempfile.TemporaryDirectory();cls.addClassCleanup(cls.tmp.cleanup)
        cls.root=Path(cls.tmp.name);source=Path(__file__).resolve().parents[2]
        # Five pages prove that the bounded sample is NOT a lifetime page crawl.
        script=r"""
        import {isolatedBuildRoot,runBuild} from './tests/site-artifact-fixtures.mjs';
        import {fixture} from './tests/site-fixtures.mjs';
        import {writeTagData,recordsFor} from './tests/site-tag-fixtures.mjs';
        import {mkdir,writeFile,copyFile} from 'node:fs/promises';
        import {stableJSON} from './scripts/content.mjs';
        const root=process.argv[1], data=root+'/data', issue=fixture(), template=issue.items['2'];
        const ids=Array.from({length:81},(_,n)=>String(n+1));
        issue.items=Object.fromEntries(ids.map(id=>[id,{...structuredClone(template),id,kind:'show',score:100,comments:10,
            title:'Show HN: fixture '+id,submittedAt:'2026-11-07T12:00:00Z',
            discussionUrl:'https://news.ycombinator.com/item?id='+id,
            sources:[{kind:'discussion',url:'https://news.ycombinator.com/item?id='+id}]}]));
        issue.sections.forEach(section=>{section.itemIds=[];section.qualifyingCount=0;});
        issue.sections[3].itemIds=[...ids].sort().reverse();issue.sections[3].qualifyingCount=81;
        for(const name of ['issues','revisions','stories']) await mkdir(data+'/'+name,{recursive:true});
        await writeFile(data+'/revisions/.gitkeep','');
        await writeFile(data+'/issues/'+issue.issueId+'.json',stableJSON(issue));
        const old=structuredClone(issue);
        old.issueId='2026-11-06';old.scheduledAt='2026-11-06T08:00:00Z';old.collectedAt=old.scheduledAt;
        old.window={start:'2026-10-30T08:00:00Z',end:old.scheduledAt};
        old.items={'82':{...structuredClone(issue.items['1']),id:'82',submittedAt:'2026-11-01T12:00:00Z',
            discussionUrl:'https://news.ycombinator.com/item?id=82',sources:[{kind:'discussion',url:'https://news.ycombinator.com/item?id=82'}]}};
        old.sections[3].itemIds=['82'];old.sections[3].qualifyingCount=1;
        await writeFile(data+'/issues/'+old.issueId+'.json',stableJSON(old));
        const registry={schemaVersion:1,tags:[{slug:'ai',label:'AI'},{slug:'single',label:'Single'},{slug:'untouched',label:'Untouched'}]};
        const records=recordsFor([old,issue]);
        records.find(record=>record.id==='1').tagSlugs.push('single');
        records.find(record=>record.id==='82').tagSlugs=['untouched'];
        await writeTagData(data,{registry,records});
        const buildRoot=await isolatedBuildRoot(root), result=runBuild(buildRoot,data,root+'/dist','unversioned');
        if(result.status!==0) throw Error(result.stdout+result.stderr);
        await mkdir(root+'/public');await copyFile('public/_headers',root+'/public/_headers');
        """
        result=subprocess.run([shutil.which('node'),'--input-type=module','-e',script,str(cls.root)],
                              cwd=source,capture_output=True,timeout=120)
        if result.returncode:raise AssertionError(result.stderr.decode()+result.stdout.decode())
        cls.output={str(path.relative_to(cls.root/'dist')):path.read_bytes()
                    for path in (cls.root/'dist').rglob('*') if path.is_file()}
        cls.files={str(path.relative_to(cls.root)):path.read_bytes()
                   for path in (cls.root/'data').rglob('*') if path.is_file()}
        # Exact source fixture: immutable build inputs and public data, NO output.
        for name in ('src','scripts','schema','public'):
            cls.files.update({str(path.relative_to(source)):path.read_bytes()
                              for path in (source/name).rglob('*') if path.is_file()})
        for name in ('astro.config.mjs','tsconfig.json','package.json','package-lock.json','.node-version','.npmrc'):
            if (source/name).exists():cls.files[name]=(source/name).read_bytes()

    def setUp(self):
        self.network=patch.object(site,'urlopen',side_effect=AssertionError('offline archive tests must not open network'))
        self.network.start();self.addCleanup(self.network.stop)
        self.files=dict(self.files)
        self.settings=dict(repository='owner/repo',timeout=30,_deadline=time.monotonic()+30,node=shutil.which('node'))
        self.context=dict(commit='a'*40,issueId='2026-11-13')

    def archive(self,files=None):
        stream=io.BytesIO()
        files=self.files if files is None else files
        prefix='repo-'+'a'*40
        with tarfile.open(fileobj=stream,mode='w:gz') as tar:
            # Match codeload's explicit directories, not only a file-only tar.
            directories={'.'} | {str(parent) for path in files for parent in Path(path).parents}
            for path in sorted(directories):
                info=tarfile.TarInfo(prefix+('' if path=='.' else '/'+path));info.type=tarfile.DIRTYPE
                tar.addfile(info)
            for path,raw in files.items():
                info=tarfile.TarInfo(prefix+'/'+path);info.size=len(raw);tar.addfile(info,io.BytesIO(raw))
        return stream.getvalue()

    def expected(self,raw=None,url=None):
        raw=self.archive() if raw is None else raw
        canonical='https://codeload.github.com/owner/repo/tar.gz/'+'a'*40
        class Response:
            status=200
            def geturl(self):return url or canonical
            def read(self,size):return raw[:size]
            def __enter__(self):return self
            def __exit__(self,*args):pass
        with patch.object(site,'urlopen',return_value=Response()) as read:
            result=site.expected_artifacts(self.settings,self.context)
        self.assertEqual(read.call_count,1)
        self.assertEqual(read.call_args.args[0].full_url,canonical)
        self.assertGreater(read.call_args.kwargs['timeout'],0)
        return result

    def test_real_trusted_inventory_selects_first_last_adjacent_boundaries_and_dedupes(self):
        self.assertTrue(callable(site.expected_artifacts))
        tracked,evidence=self.expected()
        self.assertEqual(evidence['localTagPageCount'],7)
        self.assertEqual(evidence['localIssueRouteCount'],2)
        self.assertNotIn('tags/untouched/index.html',tracked)
        self.assertNotIn('data/stories/82.json',tracked)
        routes=[path for path in tracked if path.startswith('tags/')]
        self.assertEqual(routes,['tags/index.html','tags/ai/index.html','tags/ai/2/index.html',
                                 'tags/ai/4/index.html','tags/ai/5/index.html','tags/single/index.html'])
        self.assertNotIn('tags/ai/3/index.html',tracked)
        self.assertEqual(evidence['affectedTags'],['ai','single'])
        self.assertEqual(evidence['assignments']['1'],['ai','single'])
        self.assertEqual(evidence['boundaries'],[
            dict(slug='ai',left='tags/ai/index.html',right='tags/ai/2/index.html',lastId='62',firstId='61'),
            dict(slug='ai',left='tags/ai/4/index.html',right='tags/ai/5/index.html',lastId='2',firstId='1')])
        self.assertTrue(evidence['allExpectedRoutesLocallyVerified'])
        self.assertEqual(tracked['data/tags.json'],self.files['data/tags.json'])
        self.assertEqual(tracked['data/stories/1.json'],self.files['data/stories/1.json'])

    def test_source_only_release_matches_independent_full_build_and_ignores_legacy_output(self):
        tracked,evidence=self.expected()
        self.assertFalse(any(path.startswith('dist/') for path in self.files))
        self.assertEqual(tracked,{path:self.output[path] for path in tracked})
        self.assertEqual(json.loads(tracked['build-manifest.json'])['sourceRevision'],'unversioned')
        # A transitional archive's old output is not authority, even if rehashed.
        self.files.update({'dist/'+path:b'FORGED OUTPUT' for path in self.output})
        self.assertEqual(self.expected(),(tracked,evidence))

    def test_changed_missing_or_extra_build_inputs_are_rejected_before_execution(self):
        self.expected() # real valid callable control before every negative family
        original=dict(self.files)
        marker=self.root/'fetched-code-executed'
        malicious=f"import {{writeFileSync}} from 'node:fs';writeFileSync({json.dumps(str(marker))},'bad');".encode()
        for path in ('scripts/build.mjs','scripts/verify-dist.mjs','src/layouts/Layout.astro',
                     'astro.config.mjs','schema/issue.schema.json','public/_headers','package-lock.json',
                     'tsconfig.json','package.json','.node-version'):
            for operation in ('change','missing'):
                self.files=dict(original)
                if operation=='change':self.files[path]=malicious
                else:del self.files[path]
                with self.subTest(path=path,operation=operation),self.assertRaisesRegex(RuntimeError,'build input.*(mismatch|differ)'):
                    self.expected()
                self.assertFalse(marker.exists())
        for path in ('scripts/extra.mjs','src/pages/extra.astro','schema/extra.json','public/extra',
                     '.npmrc','.env','vite.config.mjs','postcss.config.cjs'):
            self.files=dict(original);self.files[path]=malicious
            with self.subTest(extra=path),self.assertRaisesRegex(RuntimeError,'build input.*(mismatch|differ)'):
                self.expected()
        self.assertFalse(marker.exists())

    def test_source_semantic_errors_are_not_hidden_by_forged_output(self):
        self.expected()
        original=dict(self.files)
        for mode in ('missing-record','wrong-record-id','unknown-tag','invalid-issue'):
            self.files=dict(original)
            self.files.update({'dist/'+path:raw for path,raw in self.output.items()})
            if mode=='missing-record':del self.files['data/stories/1.json']
            if mode=='wrong-record-id':self.files['data/stories/1.json']=self.files['data/stories/2.json']
            if mode=='unknown-tag':self.files['data/tags.json']=b'{"schemaVersion":1,"tags":[]}'
            if mode=='invalid-issue':self.files['data/issues/2026-11-13.json']=b'{}'
            with self.subTest(mode=mode),self.assertRaisesRegex(RuntimeError,'local artifact/tag inventory verification failed'):
                self.expected()

    def test_noncanonical_validated_source_bytes_preserve_export_policy(self):
        self.expected()
        for path in ('data/tags.json','data/stories/1.json','data/issues/2026-11-13.json'):
            self.files[path]=json.dumps(json.loads(self.files[path]),separators=(',',':')).encode()
        tracked,_=self.expected()
        self.assertEqual(tracked['data/tags.json'],self.files['data/tags.json'])
        self.assertEqual(tracked['data/stories/1.json'],self.files['data/stories/1.json'])
        self.assertEqual(tracked['data/issues/2026-11-13.json'],self.output['data/issues/2026-11-13.json'])
        self.assertNotEqual(tracked['data/issues/2026-11-13.json'],self.files['data/issues/2026-11-13.json'])

    def test_duplicate_decoded_keys_are_rejected_in_all_public_source_json(self):
        self.expected()
        original=dict(self.files)
        for path in ('data/tags.json','data/stories/1.json','data/issues/2026-11-13.json'):
            self.files=dict(original)
            raw=self.files[path];self.files[path]=raw.replace(b'{',b'{"schemaVersion":1,',1)
            with self.subTest(path=path),self.assertRaisesRegex(RuntimeError,'local artifact/tag inventory|duplicate JSON'):
                self.expected()

    def test_no_tags_or_wrong_commit_archive_never_uses_legacy_fallback(self):
        for mode in ('no-tags','wrong-commit'):
            files=dict(self.files)
            if mode=='no-tags':del files['data/tags.json']
            raw=self.archive(files)
            if mode=='wrong-commit':
                stream=io.BytesIO()
                with tarfile.open(fileobj=stream,mode='w:gz') as tar:
                    info=tarfile.TarInfo('repo-'+'b'*40+'/data/tags.json');info.size=2;tar.addfile(info,io.BytesIO(b'{}'))
                raw=stream.getvalue()
            with self.subTest(mode=mode),self.assertRaisesRegex(RuntimeError,'inventory'):
                self.expected(raw)

    def test_oversized_required_sample_reports_budget_not_generic_validator_failure(self):
        self.expected()
        self.settings['verify_max_requests']=8
        with self.assertRaisesRegex(RuntimeError,'^complete tagged readback exceeds.*request budget'):
            self.expected()

    def test_wrong_node_pin_and_compressed_byte_overflow_fail_closed(self):
        self.expected()
        with patch.dict(self.settings,node=sys.executable),self.assertRaisesRegex(RuntimeError,'Node differs from pin'):
            self.expected()
        with patch.object(site,'MAX_COMPRESSED_BYTES',1),self.assertRaisesRegex(RuntimeError,'compressed byte limit'):
            self.expected()

    def test_missing_installed_dependencies_fail_closed_without_install(self):
        self.expected()
        with tempfile.TemporaryDirectory() as temporary:
            installed=Path(temporary)
            for path,raw in self.files.items():
                if path.startswith('data/'):continue
                target=installed/path;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(raw)
            with patch.object(site,'ROOT',installed),self.assertRaisesRegex(RuntimeError,'installed.*dependencies'):
                self.expected()

    def test_build_is_isolated_no_installed_writes_credentials_or_npm(self):
        import os
        def snapshot():
            return {str(path.relative_to(site.ROOT)):(path.lstat().st_mode,path.lstat().st_size,path.lstat().st_mtime_ns)
                    for path in site.ROOT.rglob('*') if '.git' not in path.relative_to(site.ROOT).parts}
        before=snapshot();calls=[];popen=subprocess.Popen
        def run(args,**kwargs):
            calls.append((args,kwargs))
            self.assertEqual(args[0],self.settings['node'])
            self.assertNotIn('npm',' '.join(args[1:]))
            env=kwargs['env'];cwd=Path(kwargs['cwd'])
            for key in ('GH_TOKEN','GITHUB_TOKEN','SMTP_PASSWORD','NODE_OPTIONS','TWIHN_SOURCE_REVISION'):
                self.assertNotIn(key,env)
            self.assertNotEqual(cwd,site.ROOT)
            self.assertEqual(cwd.stat().st_mode & 0o777,0o700)
            self.assertEqual((cwd/'data/tags.json').stat().st_mode & 0o777,0o644)
            self.assertNotEqual(env['HOME'],os.environ.get('HOME'))
            self.assertEqual(kwargs.get('umask'),0o022)
            self.assertTrue(str(env['TMPDIR']).startswith(str(cwd)))
            return popen(args,**kwargs)
        with patch.dict(os.environ,GH_TOKEN='secret',GITHUB_TOKEN='secret',SMTP_PASSWORD='secret',
                        NODE_OPTIONS='--require /DO-NOT-LOAD',TWIHN_SOURCE_REVISION='wrong'),\
             patch.object(subprocess,'Popen',side_effect=run):
            self.expected()
        self.assertGreaterEqual(len(calls),3) # pin, build, independent verifier
        self.assertEqual(before,snapshot())

    def test_real_reconstruction_verifies_live_release_and_rejects_corrupted_selected_bytes(self):
        tracked,_=self.expected()
        public=json.loads(self.files['data/issues/2026-11-13.json'])
        legacy={'sections':[{'items':[dict(id=id,title=public['items'][id]['title'],
                    score=public['items'][id]['score'],comments=public['items'][id]['comments'],
                    story=public['items'][id]['story'],discussion=public['items'][id]['discussion'],
                    created=public['items'][id]['submittedAt'],url=public['items'][id].get('sourceUrl',''))
                    for id in section['itemIds']]} for section in public['sections']]}
        review=self.root/'exact-reviewed.json';review.write_text(json.dumps(legacy))
        context=dict(self.context,revision=1,**public['window'],reviewed=str(review),reviewedSha256=digest(review.read_bytes()))
        def api(settings,path):
            if '/commits/' in path:return {'sha':context['commit']}
            if '/statuses?' in path:return [{'state':'success'}]
            return [dict(id=7,sha=context['commit'],environment='Production')]
        def live(settings,path):
            path=path+'index.html' if path.endswith('/') else path
            return tracked[path],dict(url='https://twihn.com/'+path,cacheControl='no-cache')
        # Reuse only the archive transport stub, NEVER mock expected_artifacts.
        raw=self.archive();canonical='https://codeload.github.com/owner/repo/tar.gz/'+'a'*40
        class Response:
            status=200
            def geturl(self):return canonical
            def read(self,size):return raw[:size]
            def __enter__(self):return self
            def __exit__(self,*args):pass
        with patch.object(site,'urlopen',return_value=Response()),patch.object(site,'api',api),patch.object(site,'live',live):
            result=site.verify(dict(self.settings,verify_timeout=30),context)
            self.assertFalse(result['tagging']['allHistoricalPagesFetched'])
            self.assertLessEqual(result['requestBudgetUsed'],128)
            for wrong in ('build-manifest.json','data/tags.json','data/stories/1.json',
                          'issues/2026-11-13/index.html','tags/index.html','tags/ai/5/index.html'):
                original=tracked[wrong];tracked[wrong]=b'LIVE CORRUPTION'
                # Building is inside the same deadline. Use a mocked clock only
                # to expire immediately on the FIRST actual divergent live read.
                clock=[0.0]
                def corrupt(settings,path):
                    value=live(settings,path)
                    if value[0]==b'LIVE CORRUPTION':clock[0]=31.0
                    return value
                with self.subTest(path=wrong),patch.object(site,'live',corrupt),\
                     patch.object(site.time,'monotonic',side_effect=lambda:clock[0]),\
                     self.assertRaisesRegex(RuntimeError,'live artifact differs.*'+wrong):
                    site.verify(dict(self.settings,verify_timeout=30),context)
                tracked[wrong]=original

    def test_archive_root_and_parent_file_collisions_are_rejected(self):
        self.expected()
        prefix='repo-'+'a'*40
        for names in ((prefix,prefix+'/docs/file'),
                      (prefix+'/docs/parent',prefix+'/docs/parent/child'),
                      (prefix+'/docs/parent/child',prefix+'/docs/parent')):
            stream=io.BytesIO()
            with tarfile.open(fileobj=stream,mode='w:gz') as tar:
                for name in names:tar.addfile(tarfile.TarInfo(name))
                for path,raw in self.files.items():
                    info=tarfile.TarInfo(prefix+'/'+path);info.size=len(raw);tar.addfile(info,io.BytesIO(raw))
            with self.subTest(names=names),self.assertRaisesRegex(RuntimeError,'archive inventory'):
                self.expected(stream.getvalue())

    def test_archive_links_special_files_duplicates_and_entry_cap_even_in_ignored_output(self):
        self.expected()
        for kind in (tarfile.SYMTYPE,tarfile.LNKTYPE,tarfile.FIFOTYPE,tarfile.CHRTYPE):
            stream=io.BytesIO()
            with tarfile.open(fileobj=stream,mode='w:gz') as tar:
                info=tarfile.TarInfo('repo-'+'a'*40+'/dist/ignored');info.type=kind;info.linkname='/escape';tar.addfile(info)
            with self.subTest(kind=kind),self.assertRaisesRegex(RuntimeError,'archive inventory'):
                self.expected(stream.getvalue())
        stream=io.BytesIO()
        with tarfile.open(fileobj=stream,mode='w:gz') as tar:
            for _ in range(2):tar.addfile(tarfile.TarInfo('repo-'+'a'*40+'/docs/duplicate'))
        with self.assertRaisesRegex(RuntimeError,'archive inventory'):self.expected(stream.getvalue())
        stream=io.BytesIO()
        with tarfile.open(fileobj=stream,mode='w:gz') as tar:
            for n in range(50001):tar.addfile(tarfile.TarInfo('repo-'+'a'*40+'/docs/'+str(n)))
        with self.assertRaisesRegex(RuntimeError,'inventory limit'):self.expected(stream.getvalue())

    def metadata_archive(self,kind,payload,*,count=1,declared_size=None):
        info=tarfile.TarInfo('metadata');info.type=kind
        info.size=len(payload) if declared_size is None else declared_size
        record=info.tobuf(format=tarfile.GNU_FORMAT)+payload+b'\0'*((-len(payload))%512)
        return gzip.compress(record*count+gzip.decompress(self.archive()))

    def test_codeload_global_and_per_path_pax_preserve_real_source_reconstruction(self):
        self.assertTrue(callable(site.expected_artifacts))
        stream=io.BytesIO()
        with tarfile.open(fileobj=io.BytesIO(self.archive()),mode='r:gz') as source,\
             tarfile.open(fileobj=stream,mode='w:gz',format=tarfile.PAX_FORMAT,
                          pax_headers={'comment':self.context['commit']}) as target:
            for info in source:
                # Git codeload uses global commit comments and per-path PAX.
                info.pax_headers={'path':info.name,'mtime':'1700000000.123456789'}
                target.addfile(info,source.extractfile(info) if info.isfile() else None)
        tracked,evidence=self.expected(stream.getvalue())
        self.assertEqual(tracked,{path:self.output[path] for path in tracked})
        self.assertEqual(tracked['data/tags.json'],self.files['data/tags.json'])
        self.assertTrue(evidence['allExpectedRoutesLocallyVerified'])

    def test_hidden_global_pax_bytes_cannot_escape_whole_archive_budget(self):
        self.assertTrue(callable(site.expected_artifacts))
        # This is a complete source-only archive that used to build successfully:
        # regular bytes fit exactly, while hidden metadata alone exceeds the cap.
        regular=sum(map(len,self.files.values()))
        stream=io.BytesIO()
        with tarfile.open(fileobj=io.BytesIO(self.archive()),mode='r:gz') as source,\
             tarfile.open(fileobj=stream,mode='w:gz',format=tarfile.PAX_FORMAT,
                          pax_headers={'comment':'x'*(regular+10000)}) as target:
            for info in source:target.addfile(info,source.extractfile(info) if info.isfile() else None)
        raw=stream.getvalue()
        self.assertLess(len(raw),site.MAX_COMPRESSED_BYTES)
        self.assertGreater(len(gzip.decompress(raw)),regular)
        with patch.object(site,'MAX_ARCHIVE_BYTES',regular),self.assertRaisesRegex(RuntimeError,'archive.*inventory limit'):
            self.expected(raw)

    def test_whole_archive_budget_counts_padding_and_bytes_after_tar_eof(self):
        self.assertTrue(callable(site.expected_artifacts))
        expanded=gzip.decompress(self.archive())
        # The unmodified expanded stream fits exactly, including its padding.
        with patch.object(site,'MAX_ARCHIVE_BYTES',len(expanded)):
            tracked,_=self.expected(gzip.compress(expanded))
        self.assertEqual(tracked,{path:self.output[path] for path in tracked})
        for concatenated,raw in enumerate((gzip.compress(expanded+b'\0'),
                                           gzip.compress(expanded)+gzip.compress(b'\0'*512))):
            with self.subTest(concatenated=bool(concatenated)),patch.object(site,'MAX_ARCHIVE_BYTES',len(expanded)),\
                 self.assertRaisesRegex(RuntimeError,'archive.*inventory limit'):
                self.expected(raw)

    def test_hidden_extension_headers_count_toward_archive_member_cap(self):
        self.assertTrue(callable(site.expected_artifacts))
        records=[]
        for n in range(25001):
            pax=tarfile.TarInfo('global');pax.type=tarfile.XGLTYPE;pax.size=0
            directory=tarfile.TarInfo('repo-'+'a'*40+'/docs/'+str(n));directory.type=tarfile.DIRTYPE
            records.extend((pax.tobuf(),directory.tobuf()))
        raw=gzip.compress(b''.join(records)+gzip.decompress(self.archive()))
        self.assertLess(len(raw),site.MAX_COMPRESSED_BYTES)
        self.assertLess(len(gzip.decompress(raw)),site.MAX_ARCHIVE_BYTES)
        with self.assertRaisesRegex(RuntimeError,'archive.*inventory limit'):
            self.expected(raw)

    def test_inflation_is_bounded_and_deadline_checked_before_tar_parsing(self):
        self.assertTrue(callable(site.expected_artifacts))
        raw=self.archive();limit=2*64*1024+5;reads=[]
        read=gzip.GzipFile.read
        def bounded(stream,size=-1):
            self.assertGreater(size,0);self.assertLessEqual(size,64*1024)
            value=read(stream,size);reads.append(len(value));return value
        with patch.object(site,'MAX_ARCHIVE_BYTES',limit),patch.object(gzip.GzipFile,'read',bounded),\
             patch.object(tarfile.TarInfo,'_proc_member',side_effect=AssertionError('parser ran before inflation budget')),\
             self.assertRaisesRegex(RuntimeError,'archive.*inventory limit'):
            self.expected(raw)
        self.assertEqual(sum(reads),limit+1)
        clock=[0.0];self.settings['_deadline']=30
        def expired(stream,size=-1):
            value=read(stream,size);clock[0]=31;return value
        with patch.object(site.time,'monotonic',side_effect=lambda:clock[0]),patch.object(gzip.GzipFile,'read',expired),\
             patch.object(tarfile.TarInfo,'_proc_member',side_effect=AssertionError('expired inflation reached parser')),\
             self.assertRaisesRegex(RuntimeError,'deadline'):
            self.expected(raw)

    def test_pax_assignments_count_even_when_hidden_keys_repeat(self):
        self.assertTrue(callable(site.expected_artifacts))
        pax=tarfile.TarInfo.create_pax_global_header({'key'+str(n):'value' for n in range(1000)})
        records=[]
        for n in range(51):
            directory=tarfile.TarInfo('repo-'+'a'*40+'/docs/'+str(n));directory.type=tarfile.DIRTYPE
            records.extend((pax,directory.tobuf()))
        raw=gzip.compress(b''.join(records)+gzip.decompress(self.archive()))
        with self.assertRaisesRegex(RuntimeError,'archive.*inventory limit'):
            self.expected(raw)

    def test_metadata_size_and_extension_chains_are_rejected_before_processing(self):
        self.assertTrue(callable(site.expected_artifacts))
        for kind,processor in ((tarfile.XGLTYPE,'_proc_pax'),(tarfile.XHDTYPE,'_proc_pax'),
                               (tarfile.GNUTYPE_LONGNAME,'_proc_gnulong'),(tarfile.GNUTYPE_LONGLINK,'_proc_gnulong')):
            for payload,declared in ((b'',site.MAX_ARCHIVE_BYTES+1),(b'x'*(64*1024+1),None)):
                raw=self.metadata_archive(kind,payload,declared_size=declared)
                with self.subTest(kind=kind,declared=declared),\
                     patch.object(tarfile.TarInfo,processor,side_effect=AssertionError('oversized metadata was processed')),\
                     self.assertRaisesRegex(RuntimeError,'archive.*inventory limit'):
                    self.expected(raw)
        for kind,payload in ((tarfile.XGLTYPE,b''),(tarfile.GNUTYPE_LONGNAME,('repo-'+'a'*40).encode()+b'\0')):
            with self.subTest(chain=kind),self.assertRaisesRegex(RuntimeError,'archive.*(chain|inventory limit)'):
                self.expected(self.metadata_archive(kind,payload,count=64))

    def test_sparse_formats_fail_closed_before_sparse_processing(self):
        self.assertTrue(callable(site.expected_artifacts))
        raw=self.metadata_archive(tarfile.GNUTYPE_SPARSE,b'')
        with patch.object(tarfile.TarInfo,'_proc_sparse',side_effect=AssertionError('GNU sparse was processed')),\
             self.assertRaisesRegex(RuntimeError,'unsupported production archive sparse'):
            self.expected(raw)
        for headers in ({'GNU.sparse.map':'0,1'}, {'GNU.sparse.size':'1'},
                        {'GNU.sparse.major':'1','GNU.sparse.minor':'0'},
                        {'GNU.sparse.realsize':'1'}, {'GNU.sparse.name':'sparse'}):
            payload=tarfile.TarInfo.create_pax_global_header(headers)
            raw=gzip.compress(payload+gzip.decompress(self.archive()))
            with self.subTest(headers=headers),\
                 patch.object(tarfile.TarInfo,'_proc_gnusparse_00',side_effect=AssertionError('sparse 0.0 was processed')),\
                 patch.object(tarfile.TarInfo,'_proc_gnusparse_01',side_effect=AssertionError('sparse 0.1 was processed')),\
                 patch.object(tarfile.TarInfo,'_proc_gnusparse_10',side_effect=AssertionError('sparse 1.0 was processed')),\
                 self.assertRaisesRegex(RuntimeError,'unsupported production archive sparse'):
                self.expected(raw)

    def test_deadline_is_checked_before_each_hidden_extension_processor(self):
        self.assertTrue(callable(site.expected_artifacts))
        header_method='_frombuf' if hasattr(tarfile.TarInfo,'_frombuf') else 'frombuf'
        original=getattr(tarfile.TarInfo,header_method).__func__
        for kind,processor in ((tarfile.XGLTYPE,'_proc_pax'),(tarfile.GNUTYPE_LONGNAME,'_proc_gnulong'),
                               (tarfile.GNUTYPE_SPARSE,'_proc_sparse')):
            raw=self.metadata_archive(kind,b'')
            clock=[0.0];calls=[]
            self.settings['_deadline']=30
            def header(cls,*args,**kwargs):
                info=original(cls,*args,**kwargs)
                if info.type==kind:clock[0]=31
                return info
            process=getattr(tarfile.TarInfo,processor)
            def processed(info,archive):
                calls.append(info.type)
                return process(info,archive)
            with self.subTest(kind=kind),patch.object(site.time,'monotonic',side_effect=lambda:clock[0]),\
                 patch.object(tarfile.TarInfo,header_method,classmethod(header)),\
                 patch.object(tarfile.TarInfo,processor,processed),self.assertRaisesRegex(RuntimeError,'deadline'):
                self.expected(raw)
            self.assertEqual(calls,[], 'expired metadata must not reach its parser')

    def test_archive_redirect_path_traversal_byte_limits_and_deadline_fail_closed(self):
        self.expected()
        with self.assertRaisesRegex(RuntimeError,'redirected'):self.expected(url='https://preview.invalid/')
        with self.assertRaisesRegex(RuntimeError,'inventory'):self.expected(self.archive({'../escape':b'{}'}))
        with patch.object(site,'MAX_ARCHIVE_BYTES',1),self.assertRaisesRegex(RuntimeError,'inventory limit'):self.expected()
        self.settings['_deadline']=time.monotonic()-1
        with self.assertRaisesRegex(RuntimeError,'deadline'):self.expected()


class StrictPublicJSONTests(unittest.TestCase):
    def test_transport_does_not_follow_even_one_redirect_outside_the_request_budget(self):
        self.assertTrue(callable(site.urlopen))
        # The exported test seam is the real default opener, not a per-call
        # default urllib client that follows redirects before final URL checking.
        opener=getattr(site.urlopen,'__self__',None)
        self.assertIsNotNone(opener,'bounded transport must use an explicit no-redirect opener')
        redirects=[handler for handler in opener.handlers if hasattr(handler,'redirect_request')]
        self.assertEqual(len(redirects),1)
        with self.assertRaisesRegex(RuntimeError,'redirect refused'):
            redirects[0].redirect_request(site.Request('https://twihn.com/tags/'),None,302,'Found',{},'https://preview.invalid/')


    def test_recursive_decoded_duplicate_keys_invalid_utf8_and_constants_fail(self):
        self.assertTrue(callable(site.strict_json))
        self.assertEqual(site.strict_json(b'{"tags":[{"slug":"ai"}]}'),{'tags':[{'slug':'ai'}]})
        for raw in (br'{"a":1,"\u0061":2}',br'{"nested":[{"a":1,"a":2}]}',b'{"a":"\xff"}',b'{"a":NaN}'):
            with self.subTest(raw=raw),self.assertRaisesRegex(RuntimeError,'duplicate JSON|invalid production UTF-8 JSON'):
                site.strict_json(raw)


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
