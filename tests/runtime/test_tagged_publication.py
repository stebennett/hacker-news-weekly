"""Offline CLI publication regressions; only network/install boundaries are fake.

The publisher still imports/applies/validates with Node, runs its real npm gates,
commits through local Git, and verifies a real rebuilt archive through the actual
production verifier. The local mail executable is consumed by runtime.adapters.
No fixture or generated artifact is written into the source checkout.
"""
import base64
from email import policy
from email.parser import BytesParser
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from uuid import uuid4
from urllib.parse import parse_qs, unquote, urlsplit

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from runtime.orchestrator import Issue, digest
from runtime.tagging import registry_sha256
from test_runtime import reviewed


def executable(name):
    path = shutil.which(name)
    assert path is not None, name + ' must be installed (use the pinned toolchain)'
    return path


def offline_environment(root):
    guard = root / 'guard'
    guard.mkdir()
    (guard / 'sitecustomize.py').write_text(
        'import json,socket,sys\n'
        'def forbidden(*args, **kwargs):\n'
        f' open({str(root / "unexpected-network")!r}, "a").write(repr(args)+"\\n")\n'
        ' raise AssertionError("Unexpected real network in offline publication test")\n'
        'socket.socket.connect=forbidden\nsocket.create_connection=forbidden\n'
        'def audit(event,args):\n'
        ' if event=="subprocess.Popen":\n'
        f'  with open({str(root / "processes.jsonl")!r},"a") as stream: stream.write(json.dumps(args[1])+"\\n")\n'
        'sys.addaudithook(audit)\n')
    (guard / 'offline.mjs').write_text('''import net from 'node:net';
import https from 'node:https';
import {appendFileSync} from 'node:fs';
const deny=()=>{
 appendFileSync(new URL('../unexpected-network',import.meta.url),'Node network refused; ');
 throw Error('Unexpected external network in offline publication test');
};
const local=host=>['127.0.0.1','localhost','::1','[::1]'].includes(host);
const original=net.Socket.prototype.connect;
net.Socket.prototype.connect=function(...args){
 const options=Array.isArray(args[0])?args[0]:args;
 const x=options[0];
 const host=(x && typeof x==='object'?(x.host||x.hostname):typeof options[1]==='string'?options[1]:null)||'localhost';
 if(!local(host)) return deny();
 return original.apply(this,args);
};
https.request=deny; https.get=deny; globalThis.fetch=((original)=>function(url,...args){
 if(!local(new URL(url).hostname)) return deny();
 return original(url,...args);
})(globalThis.fetch);
''')
    env = {key: os.environ[key] for key in ('PATH', 'HOME', 'TMPDIR', 'LANG') if key in os.environ}
    env.update(PYTHONPATH=str(guard), NODE_OPTIONS='--import ' + str(guard / 'offline.mjs'),
               ASTRO_TELEMETRY_DISABLED='1', CI='true', GIT_ALLOW_PROTOCOL='file', TWIHN_SOURCE_REVISION='unversioned')
    return env


def write_json(path, value, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))
    path.chmod(mode)


def log(root, name, value):
    with (root / (name + '.jsonl')).open('a') as stream:
        stream.write(json.dumps(value) + '\n')


def rows(root, name):
    path = root / (name + '.jsonl')
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def git(root, *args):
    env = {key: os.environ[key] for key in ('PATH', 'HOME', 'TMPDIR', 'LANG') if key in os.environ}
    env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL='/dev/null', GIT_ALLOW_PROTOCOL='file', GIT_TERMINAL_PROMPT='0')
    return subprocess.check_output(['git', *args], cwd=root, env=env, stderr=subprocess.PIPE, timeout=30)


class LocalGitHub:
    """GitHub transport double backed by real local refs, commits and blobs."""
    def __init__(self, settings, write=False):
        self.root = Path(settings['offline_root'])
        self.origin = self.root / 'origin.git'
        self.write = write
        # Deterministic filesystem race AFTER Publisher.snapshot has returned.
        if self.fault().get('snapshot-race'):
            metadata = self.root / 'metadata/2026-11-13.json'
            sidecar = Path(json.loads(metadata.read_bytes())['taggingPath'])
            metadata.write_bytes(b'{}'); sidecar.write_bytes(b'{}')
            log(self.root, 'races', 'changed mutable originals after snapshot')

    def fault(self):
        path = self.root / 'fault.json'
        return json.loads(path.read_bytes()) if path.exists() else {}

    def at(self, *args):
        return git(self.origin, *args)

    def git_env(self):
        return {'GIT_ALLOW_PROTOCOL': 'file'}

    def server(self):
        path = self.root / 'server.json'
        return json.loads(path.read_bytes()) if path.exists() else {}

    def pr(self):
        server = self.server()
        head, base = server['head'], server['base']
        files = self.files(base, head)
        return dict(number=1, head=dict(sha=head, ref=server['branch'], repo=dict(full_name='offline/fixture')),
                    base=dict(sha=base, ref='main', repo=dict(full_name='offline/fixture')),
                    changed_files=len(files), draft=False, state='closed' if server.get('merged') else 'open',
                    merged=server.get('merged', False), merge_commit_sha=server.get('commit'))

    def files(self, base, head):
        names = self.at('diff', '--name-status', base, head).decode().splitlines()
        return [dict(filename=name, status={'A': 'added', 'M': 'modified'}[status],
                     sha=self.at('rev-parse', head + ':' + name).decode().strip())
                for status, name in (line.split('\t') for line in names)]

    @staticmethod
    def parameters():
        return dict(strict_required_status_checks_policy=True,
                    required_status_checks=[dict(context='validate', integration_id=15368)])

    def paginate(self, path, key=None):
        log(self.root, 'api', ['GET', path])
        if '/rules/branches/main' in path:
            return [dict(type='required_status_checks', ruleset_id=1, parameters=self.parameters())]
        if '/pulls?' in path:
            return []
        if '/pulls/1/files' in path:
            server = self.server()
            return self.files(server['base'], server['head'])
        if '/check-runs?' in path:
            head = path.split('/commits/')[1].split('/')[0]
            return [dict(id=1, name='validate', head_sha=head, app=dict(id=15368),
                         status='completed', conclusion='success')]
        if path.endswith('/statuses'):
            return []
        raise AssertionError(('unexpected paginated API', path))

    def request(self, method, path, payload=None):
        log(self.root, 'api', [method, path, payload])
        path = path.removeprefix('/repos/offline/fixture')
        if method == 'GET' and path == '/rulesets/1':
            return dict(id=1, enforcement='active', target='branch', current_user_can_bypass='never',
                        rules=[dict(type='required_status_checks', parameters=self.parameters())])
        if method == 'GET' and path == '/commits/main':
            return dict(sha='f' * 40 if self.fault().get('stale-base') else
                        self.at('rev-parse', 'refs/heads/main').decode().strip())
        if method == 'GET' and path.startswith('/contents/'):
            name, commit = path.removeprefix('/contents/').split('?ref=')
            raw = self.at('show', commit + ':' + unquote(name))
            return dict(type='file', encoding='base64', content=base64.b64encode(raw).decode(),
                        sha=self.at('rev-parse', commit + ':' + unquote(name)).decode().strip())
        if method == 'GET' and path.startswith('/git/commits/'):
            parents = self.at('show', '-s', '--format=%P', path.split('/')[-1]).decode().split()
            return dict(parents=[dict(sha=parent) for parent in parents])
        if method == 'GET' and path.startswith('/git/ref/heads/'):
            return dict(object=dict(sha=self.at('rev-parse', 'refs/heads/' + path.split('/heads/')[1]).decode().strip()))
        if method == 'POST' and path == '/pulls':
            assert payload is not None
            self.assert_payload(payload)
            write_json(self.root / 'server.json', dict(branch=payload['head'],
                base=self.at('rev-parse', 'main').decode().strip(),
                head=self.at('rev-parse', 'refs/heads/' + payload['head']).decode().strip()))
            return dict(number=1)
        if method == 'GET' and path == '/pulls/1':
            return self.pr()
        if method == 'PUT' and path == '/pulls/1/merge':
            assert payload is not None
            server = self.server()
            assert payload['sha'] == server['head']
            assert self.at('rev-parse', 'main').decode().strip() == server['base']
            tree = self.at('rev-parse', server['head'] + '^{tree}').decode().strip()
            commit = self.at('-c', 'user.name=Offline test', '-c', 'user.email=test@example.invalid',
                             'commit-tree', tree, '-p', server['base'], '-p', server['head'],
                             '-m', 'Local fake GitHub merge').decode().strip()
            self.at('update-ref', 'refs/heads/main', commit, server['base'])
            write_json(self.root / 'server.json', dict(server, merged=True, commit=commit))
            return dict(merged=True, sha=commit)
        raise AssertionError(('unexpected API', method, path))

    @staticmethod
    def assert_payload(payload):
        assert payload['base'] == 'main' and payload['draft'] is False
        assert payload['maintainer_can_modify'] is False


class LocalResponse(io.BytesIO):
    status = 200
    headers = {'Cache-Control': 'no-cache'}

    def __init__(self, url, raw):
        super().__init__(raw)
        self.url = url

    def geturl(self):
        return self.url


def boundary(root, action, argv):
    """Run real module mains with only HTTP/GitHub/mail boundaries replaced."""
    if action == 'weekly':
        import urllib.request
        def algolia(request, timeout):
            url = request.full_url
            parsed = urlsplit(url)
            assert parsed.scheme == 'https' and parsed.netloc == 'hn.algolia.com'
            query = parse_qs(parsed.query)
            assert query['page'] == ['0']
            start, end = map(int, re.findall(r'[0-9]+', query['numericFilters'][0]))
            from datetime import datetime
            hits = [hit for hit in json.loads((root / 'hits.json').read_bytes())
                    if start <= int(datetime.fromisoformat(hit['created_at']).timestamp()) < end]
            log(root, 'collection', url)
            return LocalResponse(url, json.dumps(dict(nbPages=1, nbHits=len(hits), hits=hits)).encode())
        urllib.request.urlopen = algolia
        from runtime import weekly
        return weekly.main(argv)
    if action == 'publish':
        from runtime import publisher
        publisher.GitHub = LocalGitHub
        return publisher.main(argv)
    if action == 'verify':
        from runtime import site_verify

        origin = root / 'origin.git'
        def at(*args): return git(origin, *args)
        def api(settings, path):
            log(root, 'readback', ['GET', path])
            current = at('rev-parse', 'main').decode().strip()
            app = dict(id=site_verify.CLOUDFLARE_APP_ID, slug=site_verify.CLOUDFLARE_APP_SLUG)
            deployment = '11111111-1111-4111-8111-111111111111'
            check = dict(id=1, name='Cloudflare Pages', head_sha=current, app=app,
                status='completed', conclusion='success', check_suite=dict(id=2), external_id=deployment,
                details_url='https://dash.cloudflare.com/?to=%2Foffline-account%2Fpages%2Fview%2Ftwihn%2F' + deployment)
            if '/commits/main' in path:
                return dict(sha=current)
            if '/commits/' + current + '/check-runs?' in path:
                return dict(total_count=1, check_runs=[check])
            if path.endswith('/check-runs/1'):
                return check
            if path.endswith('/check-suites/2'):
                return dict(id=2, head_branch='main', head_sha=current, app=app)
            raise AssertionError(('unexpected verification API', path))
        def http(request, timeout):
            url = request.full_url
            log(root, 'http', url)
            if url.startswith('https://codeload.github.com/offline/fixture/tar.gz/'):
                commit = url.rsplit('/', 1)[1]
                return LocalResponse(url, at('archive', '--format=tar.gz', '--prefix=fixture-' + commit + '/', commit))
            assert url.startswith('https://twihn.com/'), url
            name = url.removeprefix('https://twihn.com/')
            if name.endswith('/'): name += 'index.html'
            commit = at('rev-parse', 'main').decode().strip()
            return LocalResponse(url, at('show', commit + ':dist/' + name))
        site_verify.api = api
        site_verify.urlopen = http
        return site_verify.main(argv)
    if action == 'mail':
        log(root, 'mail', argv)
        fault = json.loads((root / 'fault.json').read_bytes())
        sent = root / 'sent.eml'
        if argv[:2] == ['account', 'check']:
            print(json.dumps({'success': True}))
        elif argv[:2] == ['envelope', 'search']:
            print(json.dumps({'queued': 0, 'envelopes': [{'id': 'local-sent'}] if sent.exists() else []}))
        elif argv[:2] == ['message', 'send']:
            assert '--save' in argv and argv[-2] == '--'
            raw = Path(argv[-1]).read_bytes()
            (root / 'submitted.eml').write_bytes(raw)
            if fault.get('ambiguous-mail'): return 1
            sent.write_bytes(raw)
        elif argv[:2] == ['message', 'read']:
            sys.stdout.buffer.write(sent.read_bytes())
        else:
            raise AssertionError(('unexpected mail command', argv))
        return 0
    raise AssertionError(action)


class PublicationFixture:
    def __init__(self, root, source, origin):
        self.root, self.source = root, source
        self.result = {}
        root.mkdir(mode=0o700)
        self.issue = Issue.resolve('2026-11-13')
        self.env = offline_environment(root)
        (root / 'bin').mkdir()
        real_node, self.npm = executable('node'), executable('npm')
        self.node = str(root / 'bin/node')
        real_git = executable('git')
        # Production subprocesses deliberately strip NODE_OPTIONS. A transparent
        # launcher reinstalls the offline guard and execs the real pinned binary,
        # including for the independent exact-archive verifier's clean environment.
        (root / 'bin/node').write_text(f'''#!{sys.executable}
import os,sys
os.environ['NODE_OPTIONS']='--import '+{str(root / 'guard/offline.mjs')!r}
os.environ['ASTRO_TELEMETRY_DISABLED']='1'
os.execv({real_node!r},[{real_node!r},*sys.argv[1:]])
''')
        (root / 'bin/git').write_text(f'''#!{sys.executable}
import json,os,pathlib,sys
root=pathlib.Path({str(root)!r}); args=sys.argv[1:]
with (root/'git.jsonl').open('a') as f:f.write(json.dumps(args)+'\\n')
args=[str(root/'origin.git') if x=='https://github.com/offline/fixture.git' else x for x in args]
assert not any(x.startswith(('https://','http://','ssh://','git@')) for x in args), args
os.environ['GIT_ALLOW_PROTOCOL']='file'
os.execv({real_git!r},[{real_git!r},*args])
''')
        (root / 'bin/npm').write_text(f'''#!{sys.executable}
import json,os,pathlib,subprocess,sys
root=pathlib.Path({str(root)!r}); args=sys.argv[1:]
category='npm' if pathlib.Path.cwd().name=='checkout' and pathlib.Path.cwd().is_relative_to(root/'workspace') else 'npm-nested'
with (root/(category+'.jsonl')).open('a') as f:f.write(json.dumps(args)+'\\n')
# Only installation is replaced: reuse the already-installed pinned dependency
# tree offline. All check/test/build/verify commands execute their real scripts.
if args==['ci','--no-audit','--no-fund']:
 pathlib.Path('node_modules').symlink_to({str(ROOT / 'node_modules')!r},target_is_directory=True)
 fault=json.loads((root/'fault.json').read_bytes())
 if fault.get('frozen-copy-race'):
  (pathlib.Path.cwd().parent/'tagging.json').write_bytes(b'{{}}')
 sys.exit(0)
os.environ['NODE_OPTIONS']='--import '+str(root/'guard/offline.mjs')
os.environ['ASTRO_TELEMETRY_DISABLED']='1'
result=subprocess.run([{self.npm!r},*args],capture_output=True)
with (root/(category+'-results.jsonl')).open('a') as f:
 f.write(json.dumps(dict(argv=args,returncode=result.returncode,stdout=result.stdout.decode(),stderr=result.stderr.decode()))+'\\n')
sys.stdout.buffer.write(result.stdout);sys.stderr.buffer.write(result.stderr)
sys.exit(result.returncode)
''')
        (root / 'bin/mail').write_text(f'''#!{sys.executable}
import os,sys
os.execv(sys.executable,[sys.executable,{str(Path(__file__).resolve())!r},'--boundary',{str(root)!r},'mail',*sys.argv[1:]])
''')
        for path in (root / 'bin').iterdir(): path.chmod(0o700)
        self.env['PATH'] = str(root / 'bin') + os.pathsep + self.env['PATH']
        self.run(['git', 'clone', '--bare', '--no-hardlinks', str(origin), str(root / 'origin.git')])
        write_json(root / 'fault.json', {})
        self.settings = dict(automatic_content_publication=True, repository='offline/fixture',
            offline_root=str(root), metadata_root=str(root / 'metadata'), node=self.node,
            npm=str(root / 'bin/npm'), workspace_root=str(root / 'workspace'),
            deployment_evidence='cloudflare-check-run', cloudflare_project='twihn',
            publish_timeout=540, timeout=10, verify_timeout=20, poll_interval=0.01)
        write_json(root / 'site.json', self.settings)
        write_json(root / 'mail.json', dict(binary=str(root / 'bin/mail'), timeout=5))
        def adapter(mode):
            return [sys.executable, '-m', 'runtime.adapters', mode, '--settings', str(root / 'mail.json'), '--context', '{context}']
        self.config = dict(sender='sender@example.org', recipient='reader@example.org', cwd=str(ROOT),
            website_publish=self.boundary_command('publish') + ['--settings', str(root / 'site.json'), '--context', '{context}'],
            website_verify=self.boundary_command('verify') + ['verify', '--settings', str(root / 'site.json'), '--context', '{context}'],
            email_preflight=adapter('preflight'), email_send=adapter('send'), email_verify=adapter('verify'),
            website_timeout=590, command_timeout=580)
        write_json(root / 'config.json', self.config)
        selected = reviewed(self.issue)
        hits = []
        for index, section in enumerate(selected['sections']):
            for item in section['items']:
                hits.append(dict(objectID=item['id'], title=item['title'], url=item['url'],
                    points=item['score'], num_comments=item['comments'], created_at=item['created'],
                    created_at_i=int(__import__('datetime').datetime.fromisoformat(item['created']).timestamp()),
                    _tags=['story', 'ask_hn'] if index == 2 else ['story']))
        write_json(root / 'hits.json', hits)

    def run(self, argv, expected=0, timeout=620, cwd=ROOT):
        result = subprocess.run(list(map(str, argv)), cwd=cwd, env=self.env, capture_output=True, timeout=timeout, umask=0o022)
        log(self.root, 'commands', dict(argv=list(map(str, argv)), returncode=result.returncode,
                                      stdout=result.stdout.decode(errors='replace'), stderr=result.stderr.decode(errors='replace')))
        if expected is not None and result.returncode != expected:
            raise AssertionError(f'{argv}: exit {result.returncode}, expected {expected}\n{result.stdout.decode()}\n{result.stderr.decode()}')
        return result

    def boundary_command(self, action):
        return [sys.executable, str(Path(__file__).resolve()), '--boundary', str(self.root), action]

    def begin(self):
        result = self.run(self.boundary_command('weekly') + ['begin', '--issue', self.issue.issue_id,
            '--work-root', self.root / 'work'])
        return json.loads(result.stdout)

    def prepare_review(self):
        self.begin()
        data = json.loads((self.root / 'work' / self.issue.issue_id / 'selected.json').read_bytes())
        sources = {}
        for section in data['sections']:
            for item in section['items']:
                item.update(story='Reviewed source summary.', discussion='Reviewed discussion summary.')
                sources[item['id']] = dict(sources=[dict(kind='story', url=item['url']),
                    dict(kind='discussion', url='https://news.ycombinator.com/item?id=' + item['id'])],
                    availability=dict(story=dict(status='reviewed', note='Reviewed.'), discussion=dict(status='reviewed', note='Reviewed.')))
        write_json(self.root / 'review.json', data)
        write_json(self.root / 'sources.json', dict(itemSources=sources))

    def candidate(self):
        input_path, response, candidate = (self.root / name for name in ('input.json', 'response.json', 'candidate.json'))
        self.run([self.node, 'scripts/tagging.mjs', 'input', '--reviewed', self.root / 'review.json',
                  '--data-dir', self.source / 'data', '--issue', self.issue.issue_id, '--output', input_path])
        envelope = json.loads(input_path.read_bytes())
        # Explicitly synthetic structured model response: no model/API is called.
        write_json(response, dict(assignments={story['id']: ['AI', 'Security'] for story in envelope['stories']}))
        self.run([self.node, 'scripts/tagging.mjs', 'validate', '--input', input_path, '--response', response,
                  '--data-dir', self.source / 'data', '--output', candidate])
        return candidate

    def approve(self, tagging=True):
        args = [sys.executable, '-m', 'runtime.weekly', 'approve', '--issue', self.issue.issue_id,
            '--work-root', self.root / 'work', '--state-root', self.root / 'state', '--metadata-root', self.root / 'metadata',
            '--reviewed', self.root / 'review.json', '--sources', self.root / 'sources.json', '--approve-reviewed']
        if tagging: args += ['--tagging', self.root / 'candidate.json']
        return json.loads(self.run(args).stdout)

    def resume(self, action='resume', expected=0, extra=()):
        result = self.run([sys.executable, '-m', 'runtime', '--state-dir', self.root / 'state',
            '--config', self.root / 'config.json', action, '--issue', self.issue.issue_id, *extra], expected=expected)
        return json.loads(result.stdout)

    def state_path(self, name):
        return self.root / 'state' / self.issue.issue_id / name

    def main(self):
        return git(self.root / 'origin.git', 'rev-parse', 'main').decode().strip()

    def content(self, name):
        return git(self.root / 'origin.git', 'show', self.main() + ':' + name)

    def publish(self, expected=0, **bindings):
        context = dict(issueId=self.issue.issue_id, revision=1, start=self.issue.start, end=self.issue.end,
            reviewed=str(self.state_path('reviewed.json')), reviewedSha256=digest(self.state_path('reviewed.json').read_bytes()), **bindings)
        write_json(self.root / 'publish-context.json', context)
        return self.run(self.boundary_command('publish') + ['--settings', self.root / 'site.json',
                        '--context', self.root / 'publish-context.json'], expected=expected)


class TaggedPublicationTests(unittest.TestCase):
    success: PublicationFixture | None = None

    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='twihn-tagged-e2e-')
        cls.addClassCleanup(cls.temp.cleanup)
        cls.root = Path(cls.temp.name)
        cls.base = cls.root / 'base'
        cls.base.mkdir()
        env = offline_environment(cls.root)
        # A complete isolated source checkout, not a replacement build script.
        tracked = git(ROOT, 'ls-files', '-z').decode().split('\0')
        for name in filter(None, tracked):
            if name.startswith(('data/', 'dist/')): continue
            target = cls.base / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / name, target)
        (cls.base / 'node_modules').symlink_to(ROOT / 'node_modules', target_is_directory=True)
        # Git's trailing-slash directory ignore does not cover our offline
        # dependency symlink; model a normal ignored installation in this fixture.
        with (cls.base / '.gitignore').open('a') as stream:
            stream.write('\nnode_modules\n')
        for name in ('issues', 'revisions', 'stories'): (cls.base / 'data' / name).mkdir(parents=True)
        cls.historical = {}
        for date, offset in [('2026-10-30', 100), ('2026-11-06', 200)]:
            issue = Issue.resolve(date)
            review = reviewed(issue)
            for section in review['sections']:
                for item in section['items']:
                    item['id'] = str(int(item['id']) + offset)
            raw, metadata = cls.root / 'old-review.json', cls.root / 'old-metadata.json'
            write_json(raw, review)
            write_json(metadata, dict(issueId=date, revision=1, scheduledAt=issue.end, collectedAt=issue.end,
                qualifyingCounts=[20, 20, 10, 0], ordinaryRankings=dict(
                    score=[item['id'] for section in review['sections'][:2] for item in section['items']],
                    comments=[item['id'] for section in review['sections'][:2] for item in section['items']]),
                itemSources={item['id']: dict(sources=[dict(kind='story', url=item['url']),
                    dict(kind='discussion', url='https://news.ycombinator.com/item?id=' + item['id'])],
                    availability=dict(story=dict(status='reviewed', note='Reviewed.'), discussion=dict(status='reviewed', note='Reviewed.')))
                    for section in review['sections'] for item in section['items']}))
            result = subprocess.run([executable('node'), 'scripts/import-legacy.mjs', str(raw), str(metadata),
                str(cls.base / 'data/issues' / (date + '.json'))], cwd=ROOT, env=env, capture_output=True, timeout=20, umask=0o022)
            if result.returncode: raise AssertionError(result.stderr.decode())
            path = cls.base / 'data/issues' / (date + '.json')
            if offset == 100:
                revision = cls.base / 'data/revisions' / date / 'v1.json'
                revision.parent.mkdir(); revision.write_bytes(path.read_bytes()); revision.chmod(0o644)
                current = json.loads(path.read_bytes())
                current.update(revision=2, correction=dict(previousRevision=1, correctedAt='2026-10-31T10:00:00Z', note='Test-only correction.'))
                write_json(path, current, 0o644)
            for section in review['sections']:
                for item in section['items']:
                    write_json(cls.base / 'data/stories' / (item['id'] + '.json'),
                        dict(schemaVersion=1, id=item['id'], tagSlugs=['ai'], issueIds=[date]), 0o644)
        write_json(cls.base / 'data/tags.json', dict(schemaVersion=1, tags=[dict(slug='ai', label='AI')]), 0o644)
        # Run the same real builder/verifier on the retained archive before the
        # publisher adds the new occurrence. Output is never tracked source dist.
        result = subprocess.run([executable('npm'), 'run', 'build'], cwd=cls.base, env=env,
                                capture_output=True, timeout=60, umask=0o022)
        if result.returncode: raise AssertionError(result.stdout.decode() + result.stderr.decode())
        for path in (cls.base / 'data').rglob('*.json'):
            cls.historical[str(path.relative_to(cls.base))] = path.read_bytes()
        # Issue/revision exports retain their existing canonical-JSON contract;
        # tags/stories export original bytes. Preserve each independently, rather
        # than accidentally imposing the new raw-byte contract on old schemas.
        cls.historical_public = {name: (cls.base / 'dist' / name).read_bytes() for name in cls.historical}
        git(cls.base, 'init', '--quiet', '--initial-branch=main'); git(cls.base, 'add', '-A')
        git(cls.base, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '--quiet', '-m', 'Retained tagged fixture archive')
        cls.success = None

    def fixture(self, name='case', published=False):
        original = self.successful() if published else None
        origin = original.root / 'origin.git' if original else self.base
        fixture = PublicationFixture(self.root / (name + '-' + uuid4().hex), self.base, origin)
        if original:
            # A retry restores the original frozen collection, including actual
            # collectedAt. Recollecting now is correctly NOT identical content.
            shutil.copytree(original.root / 'work', fixture.root / 'work')
        return fixture

    def successful(self):
        cached = type(self).success
        if cached is None:
            fixture = self.fixture('success')
            fixture.prepare_review(); fixture.candidate()
            approval = fixture.approve()
            self.assertIsNone(approval['websitePreparationError'])
            # Both mutable original inputs race after the real publisher snapshot.
            metadata = fixture.root / 'metadata/2026-11-13.json'
            originals = {metadata: metadata.read_bytes(), fixture.state_path('tagging.json'): fixture.state_path('tagging.json').read_bytes()}
            write_json(fixture.root / 'fault.json', {'snapshot-race': True})
            fixture.result = fixture.resume()
            for path, raw in originals.items(): path.write_bytes(raw)
            write_json(fixture.root / 'fault.json', {})
            self.assertEqual(fixture.result['website']['status'], 'verified', fixture.result)
            type(self).success = fixture
            cached = fixture
        return cached

    def assert_no_mutation(self, fixture, before):
        self.assertEqual(fixture.main(), before)
        self.assertTrue(all(row[0] == 'GET' for row in rows(fixture.root, 'api')))
        commands = rows(fixture.root, 'git')
        self.assertFalse(any('push' in row or 'commit' in row or 'update-ref' in row for row in commands))
        self.assertFalse((fixture.root / 'server.json').exists())
        self.assertFalse((fixture.root / 'unexpected-network').exists())

    def assert_baseline(self, fixture, state):
        self.assertEqual(state['website']['status'], 'failed')
        self.assertEqual(state['email']['status'], 'verified')
        raw = fixture.state_path('send.eml').read_bytes()
        self.assertEqual(raw, (fixture.root / 'submitted.eml').read_bytes())
        self.assertEqual(raw, (fixture.root / 'sent.eml').read_bytes())
        self.assertEqual(digest(raw), state['email']['messageSha256'])
        self.assertTrue(state['email']['evidence']['contentMatched'])
        message = BytesParser(policy=policy.default).parsebytes(raw)
        baseline = BytesParser(policy=policy.default).parsebytes(fixture.state_path('baseline.eml').read_bytes())
        self.assertEqual(message['From'], 'sender@example.org')
        self.assertEqual(message['To'], 'reader@example.org')
        self.assertEqual(message['Message-ID'], state['email']['messageId'])
        parts = list(message.iter_parts())
        self.assertEqual([part.get_content_type() for part in parts], ['text/plain', 'text/html'])
        data = json.loads(fixture.state_path('reviewed.json').read_bytes())
        for part, base in zip(parts, baseline.iter_parts()):
            text = part.get_content()
            self.assertEqual(text, base.get_content())
            self.assertNotIn('https://twihn.com/issues/', text)
            for section in data['sections']:
                for item in section['items']:
                    for field in ('title', 'story', 'discussion'): self.assertIn(item[field], text)
        self.assertEqual(sum(row[:2] == ['message', 'send'] for row in rows(fixture.root, 'mail')), 1)

    def test_complete_tagged_cli_publication_rebuilds_all_retained_archives(self):
        fixture = self.successful()
        state = fixture.result
        self.assertEqual(state['email']['status'], 'verified')
        self.assertTrue(state['email']['evidence']['contentMatched'])
        self.assertEqual(fixture.state_path('send.eml').read_bytes(), (fixture.root / 'sent.eml').read_bytes())
        plain = BytesParser(policy=policy.default).parsebytes(
            (fixture.root / 'sent.eml').read_bytes()).get_body(preferencelist=('plain',))
        self.assertIsNotNone(plain)
        assert plain is not None
        self.assertIn('https://twihn.com/issues/2026-11-13/', plain.get_content())
        # Independent expected membership, not merely an observed `npm build` argv.
        expected = [str(n) for n in range(30, 0, -1)] + [str(n) for n in range(230, 200, -1)] + [str(n) for n in range(130, 100, -1)]
        actual = []
        for page in range(1, 6):
            name = 'dist/tags/ai/' + ('' if page == 1 else str(page) + '/') + 'index.html'
            ids = re.findall(r'data-hn-id="([0-9]+)"', fixture.content(name).decode())
            self.assertEqual(ids, expected[(page - 1) * 20:page * 20])
            actual.extend(ids)
        self.assertEqual(actual, expected)
        self.assertEqual(len(set(actual)), 90)
        for path, raw in self.historical.items():
            if path == 'data/tags.json': continue
            self.assertEqual(fixture.content(path), raw, path)
            self.assertEqual(fixture.content('dist/' + path), self.historical_public[path], 'public retained ' + path)
        for date in ('2026-10-30', '2026-11-06', '2026-11-13'):
            self.assertIn('data-hn-id=', fixture.content('dist/issues/' + date + '/index.html').decode())
        evidence = state['website']['evidence']
        self.assertEqual(evidence['commit'], fixture.main())
        self.assertEqual(evidence['deploymentEvidence'], 'cloudflare-check-run')
        self.assertEqual(evidence['productionBranch'], 'main')
        self.assertTrue(evidence['tagging']['allExpectedRoutesLocallyVerified'])
        self.assertEqual(evidence['tagging']['localIssueRouteCount'], 3)
        self.assertEqual(evidence['tagging']['localTagPageCount'], 7)
        self.assertTrue({'tags/ai/index.html', 'tags/ai/2/index.html', 'tags/ai/4/index.html', 'tags/ai/5/index.html'} <= set(evidence['checkedPaths']))
        self.assertEqual(rows(fixture.root, 'races'), ['changed mutable originals after snapshot'])
        for name in ('reviewed.json', 'tagging.json', 'state.json', 'context.json', 'baseline.eml', 'send.eml'):
            path = fixture.state_path(name)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600, name)
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual((fixture.root / 'metadata/2026-11-13.json').stat().st_mode & 0o777, 0o600)
        self.assertEqual(fixture.state_path('reviewed.json').read_bytes(), (fixture.root / 'review.json').read_bytes())
        self.assertTrue(all(fixture.content('data/stories/' + str(n) + '.json') == fixture.content('dist/data/stories/' + str(n) + '.json') for n in range(1, 31)))
        self.assertFalse((fixture.root / 'unexpected-network').exists())
        expected_commands = [['--version'], ['ci', '--no-audit', '--no-fund'],
            ['run', 'check'], ['test'], ['run', 'build'], ['run', 'verify:dist']]
        self.assertEqual(rows(fixture.root, 'npm'), expected_commands)
        self.assertEqual([row[1:] for row in rows(fixture.root, 'processes')
            if row[0] == str(fixture.root / 'bin/npm')], expected_commands)

    def test_missing_and_invalid_tags_deliver_complete_baseline_mime(self):
        for invalid in (False, True):
            with self.subTest(invalid=invalid):
                fixture = self.fixture()
                fixture.prepare_review()
                if invalid: write_json(fixture.root / 'candidate.json', {})
                before = fixture.main()
                result = fixture.approve(tagging=invalid)
                self.assertTrue(result['emailPrepared'])
                self.assertRegex(result['websitePreparationError'], 'tagging')
                self.assertFalse((fixture.root / 'metadata/2026-11-13.json').exists())
                self.assertFalse(fixture.state_path('tagging.json').exists())
                state = fixture.resume()
                self.assert_baseline(fixture, state)
                self.assertEqual(json.loads(fixture.state_path('report.json').read_bytes())['email'], state['email'])
                self.assert_no_mutation(fixture, before)

    def test_frozen_retry_has_no_collection_tag_generation_or_delivery_work(self):
        fixture = self.successful()
        names = ('reviewed.json', 'tagging.json', 'send.eml', 'state.json')
        frozen = {name: fixture.state_path(name).read_bytes() for name in names}
        artifacts = {name: (fixture.root / name).read_bytes() for name in ('input.json', 'response.json', 'candidate.json', 'metadata/2026-11-13.json')}
        before = {name: rows(fixture.root, name) for name in ('collection', 'api', 'npm', 'mail', 'http', 'processes')}
        self.assertTrue(fixture.begin()['reused'])
        self.assertIsNone(fixture.approve()['websitePreparationError'])
        self.assertEqual(fixture.resume()['email']['status'], 'verified')
        for name, raw in frozen.items(): self.assertEqual(fixture.state_path(name).read_bytes(), raw)
        for name, raw in artifacts.items(): self.assertEqual((fixture.root / name).read_bytes(), raw)
        for name, previous in before.items(): self.assertEqual(rows(fixture.root, name), previous, name)
        # No input/validate command may be launched by a retry; no AI executable exists.
        commands = rows(fixture.root, 'processes')
        self.assertFalse(any('scripts/tagging.mjs' in row and ('input' in row or 'validate' in row) for row in commands))

    def test_stale_base_and_conflicting_registry_refuse_before_any_public_mutation(self):
        for conflict in (False, True):
            with self.subTest(conflict=conflict):
                fixture = self.fixture()
                fixture.prepare_review(); candidate = fixture.candidate()
                if conflict:
                    value = json.loads(candidate.read_bytes())
                    value['registry']['tags'][0]['label'] = 'Different meaning'
                    value['registrySha256'] = registry_sha256(value['registry'])
                    write_json(candidate, value)
                else:
                    write_json(fixture.root / 'fault.json', {'stale-base': True})
                self.assertIsNone(fixture.approve()['websitePreparationError'])
                before = fixture.main()
                result = fixture.publish(expected=2)
                self.assertRegex(result.stderr.decode(), 'subprocess failed: node' if conflict else 'moved during checkout')
                processes = rows(fixture.root, 'processes')
                self.assertEqual(any('scripts/tagging.mjs' in row and 'apply' in row for row in processes), conflict)
                self.assert_no_mutation(fixture, before)
                self.assertEqual(fixture.state_path('reviewed.json').read_bytes(), (fixture.root / 'review.json').read_bytes())
                self.assert_baseline(fixture, fixture.resume())
                self.assert_no_mutation(fixture, before)

    def test_byte_identical_existing_content_is_read_only_cli_recovery(self):
        fixture = self.fixture(published=True)
        fixture.prepare_review(); fixture.candidate(); fixture.approve()
        before = fixture.main()
        source = {path: fixture.content(path) for path in self.historical}
        result = fixture.publish()
        self.assertEqual(json.loads(result.stdout), dict(commit=before))
        self.assert_no_mutation(fixture, before)
        self.assertEqual(rows(fixture.root, 'npm'), [['--version'], ['ci', '--no-audit', '--no-fund']])
        processes = rows(fixture.root, 'processes')
        self.assertTrue(any('scripts/verify-tag-changes.mjs' in row and '--idempotent' in row for row in processes))
        self.assertFalse(any('scripts/tagging.mjs' in row and 'apply' in row for row in processes))
        self.assertTrue(all(fixture.content(path) == raw for path, raw in source.items()))

    def test_historical_verified_and_ambiguous_repairs_never_reopen_email(self):
        for ambiguous in (False, True):
            with self.subTest(ambiguous=ambiguous):
                fixture = self.fixture(published=True)
                fixture.prepare_review(); fixture.candidate(); fixture.approve(tagging=False)
                write_json(fixture.root / 'fault.json', {'ambiguous-mail': ambiguous})
                state = fixture.resume(expected=2 if ambiguous else 0)
                self.assertEqual(state['email']['status'], 'ambiguous' if ambiguous else 'verified')
                frozen = {name: fixture.state_path(name).read_bytes() for name in ('send.eml', 'baseline.eml', 'reviewed.json')}
                mtimes = {name: fixture.state_path(name).stat().st_mtime_ns for name in frozen}
                mail = rows(fixture.root, 'mail')
                # Completing absent website metadata after a legacy/tagging failure
                # must preserve the actual prior intent/evidence, not seed pending.
                self.assertIsNone(fixture.approve()['websitePreparationError'])
                self.assertEqual(json.loads(fixture.state_path('state.json').read_bytes())['email'], state['email'])
                write_json(fixture.root / 'fault.json', {})
                repaired = fixture.resume('website-only')
                self.assertEqual(repaired['website']['status'], 'verified')
                self.assertEqual(repaired['email'], state['email'])
                fixture.resume(expected=2 if ambiguous else 0)
                self.assertEqual(rows(fixture.root, 'mail'), mail, 'repair and resume never even preflight mail')
                for name, raw in frozen.items():
                    self.assertEqual(fixture.state_path(name).read_bytes(), raw)
                    self.assertEqual(fixture.state_path(name).stat().st_mtime_ns, mtimes[name], name + ' must not be rewritten')
                fixture.resume('reconcile', expected=2 if ambiguous else 0, extra=['--channel', 'email'])
                self.assertEqual(sum(row[:2] == ['message', 'send'] for row in rows(fixture.root, 'mail')), 1)
                self.assertFalse(any(row[0] != 'GET' for row in rows(fixture.root, 'api')))

    def test_metadata_and_sidecar_byte_races_fail_closed_without_resetting_email(self):
        for race in ('metadata', 'sidecar', 'frozen-copy'):
            with self.subTest(race=race):
                fixture = self.fixture()
                fixture.prepare_review(); fixture.candidate(); fixture.approve()
                metadata = fixture.root / 'metadata/2026-11-13.json'
                bound = digest(metadata.read_bytes())
                if race == 'metadata':
                    # Context binds the original bytes; the ordinary unbound
                    # runtime must also reject this unapproved metadata shape.
                    value = json.loads(metadata.read_bytes())
                    value['unapproved'] = True
                    write_json(metadata, value)
                if race == 'sidecar':
                    path = fixture.state_path('tagging.json'); path.write_bytes(path.read_bytes() + b' ')
                if race == 'frozen-copy': write_json(fixture.root / 'fault.json', {'frozen-copy-race': True})
                before = fixture.main()
                result = fixture.publish(expected=2, metadataSha256=bound)
                self.assertRegex(result.stderr.decode(), {'metadata': 'metadata digest mismatch',
                    'sidecar': 'sidecar digest mismatch', 'frozen-copy': 'frozen tagging copy differs'}[race])
                self.assert_no_mutation(fixture, before)
                if race != 'frozen-copy': self.assertEqual(rows(fixture.root, 'api'), [])
                self.assertEqual(json.loads(fixture.state_path('state.json').read_bytes())['email']['status'], 'pending')
                error = fixture.approve()['websitePreparationError']
                if race == 'frozen-copy': self.assertIsNone(error)
                else: self.assertIsNotNone(error)
                self.assert_baseline(fixture, fixture.resume())
                self.assert_no_mutation(fixture, before)

    def test_offline_guards_block_unexpected_python_node_and_git_network(self):
        fixture = self.fixture()
        python = fixture.run([sys.executable, '-c',
            "import urllib.request;urllib.request.urlopen('https://example.invalid/')"], expected=1)
        self.assertIn('Unexpected real network', python.stderr.decode())
        self.assertTrue((fixture.root / 'unexpected-network').exists())
        node = fixture.run([fixture.node, '-e', "fetch('https://example.invalid/')"], expected=1)
        self.assertIn('Unexpected external network', node.stderr.decode())
        http = fixture.run([fixture.node, '-e', "require('node:http').get('http://example.invalid/')"], expected=1)
        self.assertIn('Unexpected external network', http.stderr.decode())
        network = fixture.run(['git', 'ls-remote', 'https://example.invalid/repo.git'], expected=1)
        self.assertIn('AssertionError', network.stderr.decode())


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--boundary':
        raise SystemExit(boundary(Path(sys.argv[2]), sys.argv[3], sys.argv[4:]))
    unittest.main()
