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
        self.settings=dict(repository='owner/repo',environment='Production')

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
            response.headers={'Cache-Control':'public, max-age=31536000, immutable'}
            with self.assertRaises(RuntimeError):site.live(self.settings,'build-manifest.json')
            response.headers={'Cache-Control':'no-cache'}
            response.geturl=lambda:'https://preview.example.org/build-manifest.json'
            with self.assertRaises(RuntimeError):site.live(self.settings,'build-manifest.json')

if __name__=='__main__':unittest.main()
