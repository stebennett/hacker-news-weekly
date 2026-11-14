"""Read-only GitHub-production and live-content adapters.

`resolve` identifies the current main commit ONLY if its committed public issue
matches the frozen review. It does not create PRs, push, merge, or bypass checks.
A human-approved content PR must already be merged before this can succeed.
"""
from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from .orchestrator import RuntimeError, digest, instant


def api(settings, path):
    result = subprocess.run([settings.get('gh','gh'),'api',path],capture_output=True,
                            stdin=subprocess.DEVNULL,timeout=float(settings.get('timeout',30)))
    if result.returncode:
        raise RuntimeError(f'GitHub read API failed ({result.returncode}): {result.stderr[:1024].decode(errors="replace")}')
    return json.loads(result.stdout)


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
    legacy=json.loads(Path(context['reviewed']).read_bytes())
    if digest(Path(context['reviewed']).read_bytes())!=context['reviewedSha256']:
        raise RuntimeError('frozen reviewed digest mismatch')
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
    raw=committed(settings,commit,f'dist/data/issues/{context["issueId"]}.json')
    parity(json.loads(raw),context)
    return dict(commit=commit)


def live(settings,path):
    url='https://twihn.com/'+path
    with urlopen(Request(url,headers={'Cache-Control':'no-cache','User-Agent':'TWIHN-runtime-readback/1'}),
                 timeout=float(settings.get('timeout',30))) as response:
        # Do not accept a preview, downgraded redirect, or unrelated host.
        if response.geturl()!=url or response.status!=200:
            raise RuntimeError('live readback redirected or was not HTTP 200')
        cache=response.headers.get('Cache-Control','').lower()
        if 'immutable' in cache or not any(value in cache for value in ('no-cache','max-age=0')):
            raise RuntimeError('mutable issue/manifest cache policy not verified')
        raw=response.read(20*1024*1024+1)
        if len(raw)>20*1024*1024:
            raise RuntimeError('live readback exceeds 20 MiB')
        return raw,dict(url=url,cacheControl=cache,sha256=digest(raw))


def verify(settings,context):
    commit=context['commit']
    repo=repository(settings)
    # Exact SHA and named production environment, not "latest successful check".
    deployments=api(settings,f'repos/{repo}/deployments?'+urlencode(dict(sha=commit,environment=settings.get('environment','Production'),per_page=100)))
    deployment_id=None
    for deployment in deployments:
        if deployment.get('sha')!=commit or deployment.get('environment')!=settings.get('environment','Production'):
            continue
        statuses=api(settings,f'repos/{repo}/deployments/{deployment["id"]}/statuses?per_page=1')
        if statuses and statuses[0].get('state')=='success':
            deployment_id=str(deployment['id']);break
    if deployment_id is None:
        raise RuntimeError('no exact-commit successful production deployment readback; Cloudflare project may need another scoped read adapter')
    issue=context['issueId']
    paths=['build-manifest.json',f'issues/{issue}/index.html',f'data/issues/{issue}.json']
    tracked={path:committed(settings,commit,'dist/'+path) for path in paths}
    manifest=json.loads(tracked['build-manifest.json'])
    parity(json.loads(tracked[paths[2]]),context)
    for path in paths[1:]:
        if manifest['artifacts'].get(path)!=digest(tracked[path]):
            raise RuntimeError('production commit manifest does not match its tracked artifacts')
    evidence=[]
    for path in paths:
        # Public permalink is /issues/id/, not /issues/id/index.html.
        request_path=path[:-10] if path.endswith('index.html') else path
        raw,details=live(settings,request_path)
        if raw!=tracked[path]:
            raise RuntimeError(f'live artifact differs from production commit: {path}')
        evidence.append(details)
    return dict(verified=True,production=True,https=True,manifestMatched=True,htmlMatched=True,
                jsonMatched=True,cacheChecked=True,issueId=issue,reviewedSha256=context['reviewedSha256'],
                commit=commit,deploymentId=deployment_id,url=f'https://twihn.com/issues/{issue}/',artifacts=evidence)


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
