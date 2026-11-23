"""Read-only GitHub-production and live-content adapters.

`resolve` identifies the current main commit ONLY if its committed public issue
matches the frozen review. It does not create PRs, push, merge, or bypass checks.
A human-approved content PR must already be merged before this can succeed.
"""
from __future__ import annotations

import argparse
import base64
import io
import json
from pathlib import Path
import shutil
import tarfile
import tempfile
import math
import re
import sys
import time
from urllib.error import URLError
from urllib.parse import parse_qs, quote, urlencode, urlsplit
from urllib.request import Request, HTTPRedirectHandler, build_opener

from .orchestrator import RuntimeError, digest, instant
from .tagging import _unique_object
from . import trusted_build


MAX_REQUESTS=128
MAX_COMPRESSED_BYTES=32*1024*1024
MAX_ARCHIVE_BYTES=128*1024*1024
ROOT=Path(__file__).resolve().parents[1]


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl):
        raise RuntimeError('production readback redirect refused')


# Retain stdlib TLS verification; do not spend uncounted requests following a
# preview/downgrade/other-host redirect before rejecting its final response URL.
urlopen=build_opener(_NoRedirect()).open


def strict_json(raw):
    def invalid_constant(_):
        raise ValueError('nonfinite JSON constant')
    try:
        return json.loads(raw.decode('utf-8'),object_pairs_hook=_unique_object,
                          parse_constant=invalid_constant)
    except (ValueError,UnicodeError,RecursionError) as exc:
        raise RuntimeError('invalid production UTF-8 JSON') from exc


# Execute only the installed, reviewed verifier, NEVER scripts from the fetched
# commit. It independently verifies every local JSON/HTML route against source;
# the manifest is not allowed to authorize its own missing/wrong tag pages.
_LOCAL_EXPECTATIONS=r"""
const {readFile}=await import('node:fs/promises');
const {pathToFileURL}=await import('node:url');
const [root,issueId,maxPaths]=process.argv.slice(1);
const {verifyDist}=await import(pathToFileURL(root+'/scripts/verify-dist.mjs'));
const {expectedData}=await import(pathToFileURL(root+'/scripts/artifacts.mjs'));
console.log=()=>{};
await verifyDist('dist');
const {issues,records,pages}=await expectedData();
const issue=issues.find(issue=>issue.issueId===issueId);
if (!issue) throw Error('missing expected issue');
const selected=records.filter(record=>Object.hasOwn(issue.items,record.id))
    .sort((a,b)=>BigInt(a.id)<BigInt(b.id)?-1:1);
const assignments=Object.fromEntries(selected.map(record=>[record.id,record.tagSlugs]));
const affectedTags=[...new Set(selected.flatMap(record=>record.tagSlugs))].sort();
const paths=new Set(['build-manifest.json',`issues/${issueId}/index.html`,`data/issues/${issueId}.json`,
    'data/tags.json',...selected.map(record=>`data/stories/${record.id}.json`),'tags/index.html']);
const boundaries=[];
const route=page=>page.url.slice(1)+'index.html';
for (const slug of affectedTags) {
    const group=pages.filter(page=>page.tag.slug===slug);
    for (const index of new Set([0,1,group.length-2,group.length-1])) {
        if (group[index]) paths.add(route(group[index]));
    }
    for (const index of new Set([0,group.length-2])) {
        if (group[index] && group[index+1]) boundaries.push({slug,left:route(group[index]),right:route(group[index+1]),
            lastId:group[index].stories.at(-1).item.id,firstId:group[index+1].stories[0].item.id});
    }
}
if (paths.size>Number(maxPaths)) process.exit(3);
const tracked=Object.fromEntries(await Promise.all([...paths].map(async path=>[path,(await readFile('dist/'+path)).toString('base64')])));
process.stdout.write(JSON.stringify({tracked,tagging:{verified:true,expectation:'exact-commit-validated-inventory',
    affectedTags,assignments,boundaries,localTagPageCount:pages.length,localIssueRouteCount:issues.length,
    allExpectedRoutesLocallyVerified:true}}));
"""


def expected_artifacts(settings,context):
    """One capped immutable archive request, then complete *local* validation.

    No lifetime HTTP crawl, npm install, fetched script execution, delivery-state
    writes, or new frozen tagging sidecar is needed for historical read-only repair.
    Codeload is GitHub's public exact-commit archive endpoint; private repos fail
    closed. Both compressed bytes and extracted inventory have hard limits.
    """
    commit=context['commit']
    if not re.fullmatch(r'[0-9a-f]{40,64}',commit):
        raise RuntimeError('invalid exact expected commit')
    repo=repository(settings)
    url=f'https://codeload.github.com/{repo}/tar.gz/{commit}'
    deadline=settings['_deadline']
    def remaining():
        value=deadline-time.monotonic()
        if value<=0:raise _Deadline('production verification deadline exceeded')
        return value
    with urlopen(Request(url,headers={'User-Agent':'TWIHN-runtime-readback/1'}),
                 timeout=min(float(settings.get('timeout',30)),remaining())) as response:
        if response.status!=200 or response.geturl()!=url:
            raise RuntimeError('exact production archive redirected or was not HTTP 200')
        raw=response.read(MAX_COMPRESSED_BYTES+1)
        if len(raw)>MAX_COMPRESSED_BYTES:raise RuntimeError('production archive compressed byte limit exceeded')
    remaining()
    installed=trusted_build.build_inputs(ROOT,remaining)
    with tempfile.TemporaryDirectory(prefix='twihn-readback-') as temporary:
        root=Path(temporary);root.chmod(0o700)
        prefix=repo.split('/')[1]+'-'+commit
        total=0;seen=set();matched=set();files=set();directories=set()
        # Explicit and implicit directories participate in the input inventory;
        # empty extra input directories cannot hide new auto-loaded policy.
        input_dirs={str(parent) for name in installed for parent in Path(name).parents if str(parent)!='.'}
        try:
            with tarfile.open(fileobj=io.BytesIO(raw),mode='r|gz') as archive:
                for member in archive:
                    remaining()
                    parts=member.name.rstrip('/').split('/')
                    normalized='/'.join(parts)
                    if (parts[0]!=prefix or len(parts)>128 or len(member.name)>4096 or
                            any(part in ('','.','..') for part in parts) or
                            '\\' in member.name or not (member.isdir() or member.isfile()) or normalized in seen or member.size<0):
                        raise RuntimeError('invalid production archive inventory')
                    parents={'/'.join(parts[:index]) for index in range(1,len(parts))}
                    if (parents & files or (member.isfile() and (normalized in directories or len(parts)==1))):
                        raise RuntimeError('invalid production archive inventory')
                    directories.update(parents)
                    (directories if member.isdir() else files).add(normalized)
                    seen.add(normalized);total+=member.size
                    if len(seen)>50000 or total>MAX_ARCHIVE_BYTES:
                        raise RuntimeError('production archive extracted inventory limit exceeded')
                    relative='/'.join(parts[1:])
                    if not relative:continue
                    if trusted_build.is_build_input(relative):
                        if member.isdir():
                            if relative not in input_dirs:
                                raise RuntimeError('production build input inventory mismatch')
                        else:
                            if relative not in installed:
                                raise RuntimeError('production build input inventory mismatch')
                            source=archive.extractfile(member)
                            if source is None:raise RuntimeError('invalid production archive inventory')
                            with source:
                                if source.read()!=installed[relative]:
                                    raise RuntimeError('production build input bytes differ from installed trusted source')
                            matched.add(relative)
                    elif member.isfile() and relative.startswith('data/'):
                        source=archive.extractfile(member)
                        if source is None:raise RuntimeError('invalid production archive inventory')
                        with source:
                            data=source.read()
                        if relative.endswith('.json'):strict_json(data)
                        trusted_build.write_public(root/relative,data)
                    # Ignore transitional dist, never extract/use fetched output,
                    # code, schema, configuration or public/_headers as authority.
        except (tarfile.TarError,OSError) as exc:
            raise RuntimeError('invalid production archive') from exc
        if matched!=set(installed):
            raise RuntimeError('production build input inventory mismatch')
        env=trusted_build.stage(ROOT,root,installed)
        node=settings.get('node',shutil.which('node'))
        if not node or not Path(node).is_absolute():raise RuntimeError('absolute production verifier Node required')
        status,version=trusted_build.run_node(node,['--version'],root,env,remaining,max_output=1024)
        if status or version.decode().strip()!='v'+installed['.node-version'].decode().strip():
            raise RuntimeError('production verifier Node differs from pin')
        status,_=trusted_build.run_node(node,['scripts/build.mjs'],root,env,remaining)
        if status:
            raise RuntimeError('production local artifact/tag inventory verification failed during trusted build')
        status,value=trusted_build.run_node(node,['--input-type=module','-e',_LOCAL_EXPECTATIONS,str(root),context['issueId'],
                                                str(settings.get('verify_max_requests',MAX_REQUESTS))],root,env,remaining)
        if status==3:
            raise RuntimeError('complete tagged readback exceeds production verification request budget')
        if status:
            raise RuntimeError('production local artifact/tag inventory verification failed')
        remaining()
        result=strict_json(value)
        tracked={path:base64.b64decode(raw,validate=True) for path,raw in result['tracked'].items()}
        for path,raw in tracked.items():
            if path.endswith('.json'):strict_json(raw)
        return tracked,result['tagging']


def api(settings, path):
    # Lazy import keeps this module and its api(settings, path) test seam usable
    # before the shared auth adapter is installed. No gh/subprocess dependency.
    from .github_api import GitHub
    return GitHub(settings,write=False).request('GET',path)


def repository(settings):
    repo=settings['repository']
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+',repo):
        raise RuntimeError('invalid literal repository')
    return repo


def committed(settings, commit, path):
    response=api(settings,f'repos/{repository(settings)}/contents/{path}?ref={quote(commit)}')
    if response.get('encoding')!='base64' or response.get('type')!='file':
        raise RuntimeError('GitHub must return exact tracked file bytes')
    return base64.b64decode(response['content'],validate=False)


def parity(public,context):
    if public.get('issueId')!=context['issueId'] or public.get('revision')!=context['revision']:
        raise RuntimeError('public issue identity/revision differs from review')
    reviewed=Path(context['reviewed']).read_bytes()
    if digest(reviewed)!=context['reviewedSha256']:
        raise RuntimeError('frozen reviewed digest mismatch')
    legacy=json.loads(reviewed)
    if instant(public['window']['start'])!=instant(context['start']) or instant(public['window']['end'])!=instant(context['end']):
        raise RuntimeError('public window differs from frozen review')
    if [section['id'] for section in public['sections']]!=['votes','comments','ask','show']:
        raise RuntimeError('public section ordering differs')
    for source, target in zip(legacy['sections'],public['sections']):
        if [str(item['id']) for item in source['items']]!=target['itemIds']:
            raise RuntimeError('public ordered IDs differ from review')
        for item in source['items']:
            record=public['items'][str(item['id'])]
            for key in ('title','score','comments','story','discussion'):
                if record[key]!=item[key]:
                    raise RuntimeError(f'public {key} differs from review')
            if instant(record['submittedAt'])!=instant(item['created']) or record.get('sourceUrl','')!=(item.get('url') or ''):
                raise RuntimeError('public item timestamp/source differs from review')
    return public


def resolve(settings,context):
    response=api(settings,f'repos/{repository(settings)}/commits/{quote(settings.get("production_branch","main"))}')
    commit=response['sha']
    if not re.fullmatch(r'[0-9a-f]{40,64}',commit):
        raise RuntimeError('invalid production commit readback')
    raw=committed(settings,commit,f'data/issues/{context["issueId"]}.json')
    parity(strict_json(raw),context)
    return dict(commit=commit)


def live(settings,path):
    url='https://twihn.com/'+path
    with urlopen(Request(url,headers={'Cache-Control':'no-cache','User-Agent':'TWIHN-runtime-readback/1'}),
                 timeout=float(settings.get('timeout',30))) as response:
        # Do not accept a preview, downgraded redirect, or unrelated host.
        if response.geturl()!=url or response.status!=200:
            raise RuntimeError('live readback redirected or was not HTTP 200')
        cache=response.headers.get('Cache-Control','').lower()
        directives={key.strip():value.strip().strip('"') for key,_,value in
                    (part.partition('=') for part in cache.split(','))}
        if 'immutable' in directives or not ('no-cache' in directives or directives.get('max-age')=='0'):
            raise RuntimeError('mutable issue/manifest cache policy not verified')
        raw=response.read(20*1024*1024+1)
        if len(raw)>20*1024*1024:
            raise RuntimeError('live readback exceeds 20 MiB')
        return raw,dict(url=url,cacheControl=cache,sha256=digest(raw))


# Observed on production check-run 133578306759 (2026-11-16), not the
# unrelated GitHub Actions "validate" check. Pin BOTH identity fields.
CLOUDFLARE_APP_ID=85455
CLOUDFLARE_APP_SLUG='cloudflare-workers-and-pages'


class _Pending(RuntimeError):
    """Evidence/live bytes may converge before the verification deadline."""


class _Deadline(RuntimeError):
    pass


def _production(read,repo,branch,commit):
    current=read(f'repos/{repo}/commits/{quote(branch,safe="")}')
    if current.get('sha')!=commit:
        # Conservative repair policy: an ancestor is NOT current production.
        # Manifest sourceRevision describes templates, not a deployed commit.
        raise RuntimeError('production branch no longer equals exact expected commit')


def _cloudflare_app(record):
    app=record.get('app') or {}
    return app.get('id')==CLOUDFLARE_APP_ID and app.get('slug')==CLOUDFLARE_APP_SLUG


def _cloudflare(read,settings,repo,branch,commit):
    query=urlencode(dict(check_name='Cloudflare Pages',filter='latest',per_page=100))
    response=read(f'repos/{repo}/commits/{commit}/check-runs?{query}')
    # Fail closed on truncated evidence instead of picking a stale success.
    if response.get('total_count',0)>100:
        raise RuntimeError('Cloudflare check-run evidence exceeds bounded page')
    runs=[run for run in response.get('check_runs',[]) if
          run.get('name')=='Cloudflare Pages' and _cloudflare_app(run)]
    if not runs:
        raise _Pending('no trusted Cloudflare Pages check-run for exact commit')
    newest=max(runs,key=lambda run:int(run['id']))
    check_id=newest['id']
    if not isinstance(check_id,int) or isinstance(check_id,bool) or check_id<=0:
        raise RuntimeError('invalid Cloudflare check-run identifier')
    # Read the actual run, not merely the commit's summary/list result.
    check=read(f'repos/{repo}/check-runs/{check_id}')
    if (newest.get('head_sha')!=commit or check.get('id')!=check_id or check.get('name')!='Cloudflare Pages' or
            not _cloudflare_app(check) or check.get('head_sha')!=commit):
        raise _Pending('Cloudflare check-run identity/SHA readback mismatch')
    # A pending/failing newer run must never fall back to an older success.
    if newest.get('status')!='completed' or newest.get('conclusion')!='success' or \
            check.get('status')!='completed' or check.get('conclusion')!='success':
        raise _Pending('Cloudflare Pages exact-commit check not completed successfully')
    suite_id=(check.get('check_suite') or {}).get('id')
    if not isinstance(suite_id,int) or isinstance(suite_id,bool) or suite_id<=0:
        raise RuntimeError('missing Cloudflare check-suite identifier')
    suite=read(f'repos/{repo}/check-suites/{suite_id}')
    # A preview check on a feature/PR branch is NOT production evidence,
    # even when the identical commit later appears on main.
    if (suite.get('id')!=suite_id or suite.get('head_branch')!=branch or
            suite.get('head_sha')!=commit or not _cloudflare_app(suite)):
        raise _Pending('Cloudflare check-suite is not exact-commit production branch')
    details=check.get('details_url','')
    url=urlsplit(details)
    target=parse_qs(url.query).get('to',[])
    project=settings.get('cloudflare_project','twihn')
    if not re.fullmatch(r'[a-z0-9-]+',project):
        raise RuntimeError('invalid literal Cloudflare project')
    match=re.fullmatch(r'/([^/]+)/pages/view/'+re.escape(project)+
                       r'/([0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12})',target[0]) if len(target)==1 else None
    if (url.scheme!='https' or url.netloc!='dash.cloudflare.com' or url.path!='/' or
            url.fragment or match is None):
        raise _Pending('Cloudflare check details do not identify configured Pages project')
    if settings.get('cloudflare_account_id') and match[1]!=settings['cloudflare_account_id']:
        raise _Pending('Cloudflare check details account mismatch')
    # external_id is the REAL Pages deployment UUID, confirmed independently
    # in its dashboard URL. Never label a check-run id as a deployment id.
    deployment_id=check.get('external_id')
    if deployment_id!=match[2]:
        raise _Pending('Cloudflare deployment UUID/details URL mismatch')
    return dict(deploymentEvidence='cloudflare-check-run',deploymentId=deployment_id,
                checkRunId=str(check_id),checkRunDetailsUrl=details,checkSuiteId=str(suite_id),
                cloudflareAppId=CLOUDFLARE_APP_ID,cloudflareAppSlug=CLOUDFLARE_APP_SLUG,
                productionBranch=branch)


def _deployments(read,settings,repo,commit):
    environment=settings.get('environment','Production')
    deployments=read(f'repos/{repo}/deployments?'+urlencode(dict(sha=commit,environment=environment,per_page=100)))
    for deployment in deployments:
        if deployment.get('sha')!=commit or deployment.get('environment')!=environment:
            continue
        statuses=read(f'repos/{repo}/deployments/{deployment["id"]}/statuses?per_page=1')
        if statuses and statuses[0].get('state')=='success':
            return dict(deploymentEvidence='deployments',deploymentId=str(deployment['id']))
    raise _Pending('no exact-commit successful production deployment readback')


def verify(settings,context):
    """Bounded, read-only verification; no publication or source-change retry.

    deployment_evidence: deployments (legacy default), cloudflare-check-run.
    verify_timeout/poll_interval: positive finite seconds (defaults 180/5).
    Cloudflare requires the observed trusted app, a production-branch suite,
    current branch SHA and canonical live bytes; a green CI/preview alone fails.
    """
    commit=context['commit']
    if not re.fullmatch(r'[0-9a-f]{40,64}',commit):
        raise RuntimeError('invalid exact expected commit')
    repo=repository(settings)
    branch=settings.get('production_branch','main')
    mode=settings.get('deployment_evidence','deployments')
    if mode not in ('deployments','cloudflare-check-run'):
        raise RuntimeError('unsupported deployment evidence mode')
    timeout=float(settings.get('verify_timeout',180))
    interval=float(settings.get('poll_interval',5))
    request_timeout=float(settings.get('timeout',30))
    if not all(math.isfinite(value) and value>0 for value in (timeout,interval,request_timeout)):
        raise RuntimeError('verification/request timeout and poll interval must be positive finite seconds')
    max_requests=settings.get('verify_max_requests',MAX_REQUESTS)
    if type(max_requests) is not int or not 1<=max_requests<=MAX_REQUESTS:
        raise RuntimeError('verification request budget must be an integer from 1 to 128')
    requests=0
    deadline=time.monotonic()+timeout

    def bounded(cost=0):
        nonlocal requests
        remaining=deadline-time.monotonic()
        if requests+cost>max_requests:raise RuntimeError('production verification request budget exceeded')
        requests+=cost
        if remaining<=0:
            raise _Deadline('production verification deadline exceeded')
        return dict(settings,timeout=min(request_timeout,remaining),_deadline=deadline)

    def read(path):
        # api() creates a fresh scoped client: reserve its token POST AND GET.
        return api(bounded(2),path)

    _production(read,repo,branch,commit)
    issue=context['issueId']
    # Immutable source and complete inventory validated once, before polling.
    # Wrong source never authorizes another commit/review or a legacy fallback.
    tracked,tagging=expected_artifacts(bounded(1),context)
    if (tagging.get('verified') is not True or not tagging.get('assignments') or
            not tagging.get('affectedTags') or 'data/tags.json' not in tracked or 'tags/index.html' not in tracked):
        raise RuntimeError('complete tagging evidence required')
    paths=list(tracked)
    issue_path=f'data/issues/{issue}.json'
    manifest=strict_json(tracked['build-manifest.json'])
    parity(strict_json(tracked[issue_path]),context)
    for path in paths:
        if path=='build-manifest.json':continue
        if manifest['artifacts'].get(path)!=digest(tracked[path]):
            raise RuntimeError('production commit manifest does not match its tracked artifacts')
    # Reserve a complete first attempt; never weaken/partially sample tags to fit.
    overhead=10 if mode=='cloudflare-check-run' else 8
    if requests+overhead+len(paths)>max_requests:
        raise RuntimeError('complete tagged readback exceeds production verification request budget')
    while True:
        try:
            _production(read,repo,branch,commit)
            deployment=(_cloudflare(read,settings,repo,branch,commit) if mode=='cloudflare-check-run'
                        else _deployments(read,settings,repo,commit))
            evidence=[]
            for path in paths:
                # Public permalink is /issues/id/, not /issues/id/index.html.
                request_path=path[:-10] if path.endswith('index.html') else path
                try:
                    raw,details=live(bounded(1),request_path)
                except _Deadline:
                    raise
                except RuntimeError as exc:
                    raise _Pending(str(exc)) from exc
                if raw!=tracked[path]:
                    raise _Pending(f'live artifact differs from production commit: {path}')
                evidence.append(dict(details,path=path,sha256=digest(raw)))
            # Reject branch movement while requests were in flight.
            _production(read,repo,branch,commit)
            parity(strict_json(tracked[issue_path]),context)
            bounded()
            return dict(verified=True,production=True,https=True,manifestMatched=True,htmlMatched=True,
                        jsonMatched=True,cacheChecked=True,issueId=issue,reviewedSha256=context['reviewedSha256'],
                        commit=commit,url=f'https://twihn.com/issues/{issue}/',artifacts=evidence,
                        checkedPaths=paths,requestBudgetUsed=requests,requestBudget=max_requests,
                        tagging=dict(tagging,allHistoricalPagesFetched=False,
                                     inventorySha256=digest(tracked['build-manifest.json'])),**deployment)
        except (_Pending,URLError,TimeoutError) as exc:
            remaining=deadline-time.monotonic()
            if remaining<=0:
                raise RuntimeError(f'production verification timed out: {exc}') from exc
            time.sleep(min(interval,remaining))
            if deadline-time.monotonic()<=0:
                raise RuntimeError(f'production verification timed out: {exc}') from exc


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('resolve','verify'))
    parser.add_argument('--settings',type=Path,required=True)
    parser.add_argument('--context',type=Path,required=True)
    args=parser.parse_args(argv)
    try:
        settings=json.loads(args.settings.read_bytes());context=json.loads(args.context.read_bytes())
        result={'resolve':resolve,'verify':verify}[args.action](settings,context)
        print(json.dumps(result));return 0
    except Exception as exc:
        print(str(exc),file=sys.stderr);return 2


if __name__=='__main__':raise SystemExit(main())
