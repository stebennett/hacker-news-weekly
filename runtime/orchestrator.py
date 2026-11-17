"""Durable, fail-closed TWIHN orchestration. No implicit SMTP or Git commands."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from email import policy
from email.parser import BytesParser
import fcntl
import hashlib
from html import escape
import importlib.util
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
from zoneinfo import ZoneInfo
from urllib.parse import urlsplit

UK = ZoneInfo('Europe/London')
DEFAULT_RENDERER = Path(__file__).resolve().parents[1] / 'integrations/render_email.py'


class RuntimeError(Exception):
    """An operational failure, never authorization to retry SMTP."""


def instant(value):
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('timestamps must include UTC offsets')
    return result.astimezone(timezone.utc)


def utc(value):
    return value.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True)
class Issue:
    issue_id: str
    start: str
    end: str

    @classmethod
    def resolve(cls, issue_id):
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', issue_id):
            raise ValueError('issue must be an explicit YYYY-MM-DD Friday')
        day = date.fromisoformat(issue_id)
        if day.weekday() != 4:
            raise ValueError('issue must be a Friday in Europe/London')
        end = datetime.combine(day, time(8), UK).astimezone(timezone.utc)
        return cls(issue_id, utc(end - timedelta(days=7)), utc(end))


def load_module(path, name='twihn_legacy_renderer'):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f'cannot load helper: {path}')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def validate_review(data, issue, renderer):
    if instant(data['start']) != instant(issue.start) or instant(data['end']) != instant(issue.end):
        raise RuntimeError('reviewed JSON bounds do not match frozen issue')
    renderer.validate(data)
    previous = {}
    categories = {}
    for section_index, section in enumerate(data['sections']):
        category = 'story' if section_index < 2 else 'ask' if section_index == 2 else 'show'
        seen = set()
        for item in section['items']:
            ident = str(item['id'])
            if not re.fullmatch(r'[1-9][0-9]*', ident):
                raise RuntimeError('invalid literal HN identifier')
            if ident in seen:
                raise RuntimeError('duplicate ID within a section')
            seen.add(ident)
            if ident in categories and categories[ident] != category:
                raise RuntimeError('Ask/Show and ordinary sections must remain distinct')
            categories[ident] = category
            if 'tags' in item:
                if not isinstance(item['tags'], list):
                    raise RuntimeError('tags must be an array')
                actual = 'show' if 'show_hn' in item['tags'] else 'ask' if 'ask_hn' in item['tags'] else 'story'
                if actual != category:
                    raise RuntimeError('item category contradicts collected tags')
            if item.get('url'):
                parsed = urlsplit(item['url'])
                if parsed.username or parsed.password or any(ord(char) < 32 or char == '\\' for char in item['url']):
                    raise RuntimeError('unsafe source URL')
            if not instant(issue.start) <= instant(item['created']) < instant(issue.end):
                raise RuntimeError('item outside frozen half-open window')
            if not isinstance(item['title'], str) or not item['title'].strip():
                raise RuntimeError('invalid title')
            for key in ('score', 'comments'):
                if type(item[key]) is not int or item[key] < 0:
                    raise RuntimeError('metrics must be nonnegative integers')
            for key in ('story', 'discussion'):
                if not isinstance(item[key], str) or not item[key].strip():
                    raise RuntimeError('summary missing')
                if '\r' in item[key] or '\n' in item[key]:
                    raise RuntimeError('summary must be a single paragraph')
            canonical = {key: item.get(key) for key in ('id','title','score','comments','created','url','story','discussion')}
            if ident in previous and previous[ident] != canonical:
                raise RuntimeError('repeated story differs between rankings')
            previous[ident] = canonical
    # Retain the renderer's conservative exact-ten gate; never invent missing entries.
    for section, metric in zip(data['sections'], ('score','comments','comments','score')):
        values = [item[metric] for item in section['items']]
        if values != sorted(values, reverse=True):
            raise RuntimeError('section is not ranked by its documented metric')
    return data


def secure_directory(path):
    path = Path(path).expanduser().absolute()
    # Reject symlinks even in ancestors; state must not be redirected into Git/public.
    for component in [*reversed(path.parents), path]:
        if component.is_symlink():
            raise RuntimeError(f'symlink in private directory: {component}')
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.stat().st_uid != os.getuid():
        raise RuntimeError('private state directory not owned by current user')
    os.chmod(path, 0o700)
    return path


def atomic(path, raw):
    path = Path(path)
    if path.is_symlink():
        raise RuntimeError('refusing symlink file')
    fd, temporary = tempfile.mkstemp(prefix='.tmp-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def json_bytes(data):
    return (json.dumps(data, sort_keys=True, indent=2) + '\n').encode()


class Store:
    def __init__(self, root):
        self.root = secure_directory(root)

    @contextmanager
    def lock(self):
        fd = os.open(self.root / '.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            os.fchmod(fd, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError('another runtime owns the single-writer lock') from exc
            yield
        finally:
            os.close(fd)

    def issue_dir(self, issue_id):
        Issue.resolve(issue_id)
        return secure_directory(self.root / issue_id)

    def save(self, state):
        atomic(self.issue_dir(state['issueId']) / 'state.json', json_bytes(state))

    def load(self, issue_id):
        folder = self.issue_dir(issue_id)
        path = folder / 'state.json'
        if path.is_symlink():
            raise RuntimeError('refusing symlink state')
        state = json.loads(path.read_bytes())
        issue = Issue.resolve(issue_id)
        if state['issueId'] != issue_id or (state['start'], state['end']) != (issue.start, issue.end):
            raise RuntimeError('state identity/window mismatch')
        review = folder / 'reviewed.json'
        if review.is_symlink() or digest(review.read_bytes()) != state['reviewedSha256']:
            raise RuntimeError('reviewed bytes differ from frozen checksum')
        if state['email']['status'] not in {'pending','failed','in_progress','ambiguous','verified'}:
            raise RuntimeError('unknown email state')
        return state

    def prepare(self, issue, source, *, approved=False, renderer_path=DEFAULT_RENDERER) -> dict:
        if not approved:
            raise RuntimeError('explicit --approve-reviewed is required; runtime cannot review summaries')
        return self.prepare_bytes(issue, Path(source).read_bytes(), approved=approved, renderer_path=renderer_path)

    def prepare_bytes(self, issue, raw, *, approved=False, renderer_path=DEFAULT_RENDERER) -> dict:
        """Validate and freeze the caller's exact reviewed byte snapshot."""
        if not approved:
            raise RuntimeError('explicit --approve-reviewed is required; runtime cannot review summaries')
        validate_review(json.loads(raw), issue, load_module(renderer_path))
        folder = self.issue_dir(issue.issue_id)
        if (folder / 'state.json').exists():
            existing = self.load(issue.issue_id)
            if existing['reviewedSha256'] != digest(raw):
                raise RuntimeError('issue already frozen; corrections require a new reviewed revision workflow')
            return existing
        if (folder / 'reviewed.json').exists():
            # Crash between writes or lost state: never silently authorize a fresh send.
            raise RuntimeError('orphaned reviewed artifact: restore state backup; do not reinitialize delivery')
        atomic(folder / 'reviewed.json', raw)
        state = dict(version=1, revision=1, issueId=issue.issue_id, start=issue.start, end=issue.end,
                     timezone='Europe/London', reviewedSha256=digest(raw),
                     website=dict(status='pending'), email=dict(status='pending'))
        self.save(state)
        return state


def bounded(argv, timeout, *, cwd=None, own_group=True):
    if not isinstance(argv, list) or not argv or any(not isinstance(arg, str) for arg in argv):
        raise RuntimeError('adapter command must be a nonempty JSON argv array')
    if not 0 < float(timeout) <= 86400:
        raise RuntimeError('invalid subprocess deadline')
    # File-backed output prevents a misbehaving child exhausting parent RAM.
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        proc = subprocess.Popen(argv, cwd=cwd, stdout=stdout, stderr=stderr, start_new_session=own_group,
                                close_fds=True, stdin=subprocess.DEVNULL)
        try:
            proc.wait(timeout=float(timeout))
        except BaseException:
            try:
                os.killpg(proc.pid, signal.SIGKILL) if own_group else proc.kill()
            except ProcessLookupError:
                pass
            proc.wait()
            raise RuntimeError('subprocess exceeded deadline or interrupted')
        # No descendant may survive a stage, including children of an exited leader.
        try:
            if own_group:
                os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        stdout.seek(0); stderr.seek(0)
        output = stdout.read(1048577)
        error = stderr.read(4096).decode(errors='replace')
        if len(output) > 1048576:
            raise RuntimeError('adapter output exceeds 1 MiB')
        if proc.returncode != 0:
            raise RuntimeError(f'adapter exited {proc.returncode}: {error}')
        return output


def command(config, key, context_path, context, *, own_group=True):
    argv = config.get(key)
    if not isinstance(argv, list):
        raise RuntimeError(f'missing JSON argv adapter: {key}')
    tokens = dict(context, context=str(context_path))
    expanded = []
    for arg in argv:
        if not isinstance(arg, str):
            raise RuntimeError('argv elements must be strings')
        for name, value in tokens.items():
            if isinstance(value, (str, int)):
                arg = arg.replace('{' + name + '}', str(value))
        expanded.append(arg)
    raw = bounded(expanded, config.get('command_timeout', 60), cwd=config.get('cwd'), own_group=own_group)
    if key == 'email_send':
        return {}  # Submission stdout is not delivery evidence.
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise RuntimeError(f'{key} must return one JSON evidence object') from exc
    if not isinstance(data, dict):
        raise RuntimeError('adapter evidence must be an object')
    return data


def verify_website(evidence, context):
    for field in ('verified','production','https','manifestMatched','htmlMatched','jsonMatched','cacheChecked'):
        if evidence.get(field) is not True:
            raise RuntimeError(f'website readback missing {field}')
    for field in ('issueId','reviewedSha256','commit'):
        if evidence.get(field) != context[field]:
            raise RuntimeError(f'website readback mismatch: {field}')
    expected = f'https://twihn.com/issues/{context["issueId"]}/'
    if evidence.get('url') != expected or not evidence.get('deploymentId'):
        raise RuntimeError('wrong canonical URL or absent deployment identifier')
    return evidence


def website_worker(config_path, context_path, verify_only=False):
    config = json.loads(Path(config_path).read_bytes())
    context_path = Path(context_path)
    context = json.loads(context_path.read_bytes())
    if not verify_only:
        result = command(config, 'website_publish', context_path, context, own_group=False)
        commit = result.get('commit', '')
        if not isinstance(commit, str) or not re.fullmatch('[0-9a-f]{40,64}', commit):
            raise RuntimeError('publication adapter must identify the actual production commit')
        context['commit'] = commit
        atomic(context_path, json_bytes(context))
    evidence = command(config, 'website_verify', context_path, context, own_group=False)
    return verify_website(evidence, context)


class Runtime:
    def __init__(self, store, issue_id, config_path):
        self.store = store
        self.state = store.load(issue_id)
        self.folder = store.issue_dir(issue_id)
        self.config_path = Path(config_path).absolute()
        self.config = json.loads(self.config_path.read_bytes())
        self.sender = self.config.get('sender')
        self.recipient = self.config.get('recipient')
        for address in (self.sender, self.recipient):
            if not isinstance(address, str) or not re.fullmatch(r'[^\s<>@,;]+@[^\s<>@,;]+', address):
                raise RuntimeError('explicit sender and recipient are required in private config')
        settings = dict(sender=self.sender, recipient=self.recipient)
        if self.state.get('deliverySettings', settings) != settings:
            raise RuntimeError('delivery addresses differ from frozen issue settings')
        if 'deliverySettings' not in self.state:
            self.state['deliverySettings'] = settings
            self.store.save(self.state)
        self.context_path = self.folder / 'context.json'
        self.context = dict(issueId=issue_id, revision=self.state['revision'],
                            reviewedSha256=self.state['reviewedSha256'],
                            reviewed=str(self.folder / 'reviewed.json'),
                            start=self.state['start'], end=self.state['end'],
                            email=str(self.folder / 'send.eml'), sender=self.sender, recipient=self.recipient)
        # Recover a commit durably written by the child before a parent crash.
        # Do not discard it by replacing context during construction.
        if self.context_path.exists():
            previous = json.loads(self.context_path.read_bytes())
            if previous.get('issueId') != issue_id or previous.get('reviewedSha256') != self.state['reviewedSha256']:
                raise RuntimeError('saved adapter context does not match frozen issue')
            commit = previous.get('commit')
            if commit and re.fullmatch(r'[0-9a-f]{40,64}', commit):
                self.context['commit'] = commit
                self.state['website'].setdefault('commit', commit)
                self.save()
        self.write_context()

    def write_context(self):
        atomic(self.context_path, json_bytes(self.context))

    def save(self):
        self.store.save(self.state)

    def report(self):
        report = dict(issueId=self.state['issueId'], website=self.state['website'], email=self.state['email'],
                      websiteRepair=f'python3 -m runtime --state-dir {self.store.root} --config {self.config_path} website-only --issue {self.state["issueId"]}',
                      reconciliationRequired=self.state['email']['status'] in {'in_progress','ambiguous'})
        atomic(self.folder / 'report.json', json_bytes(report))
        return self.state

    def website(self, verify_only=False):
        channel = self.state['website']
        if channel['status'] == 'verified' and not verify_only:
            return
        if not verify_only and channel['status'] in {'in_progress','failed'} and channel.get('commit'):
            self.website(verify_only=True)
            if channel['status'] == 'verified':
                return
        try:
            if verify_only:
                self.context['commit'] = channel['commit']
            channel['status'] = 'in_progress'
            self.save(); self.write_context()
            argv = [sys.executable, '-m', 'runtime', '_website-worker', '--config', str(self.config_path), '--context', str(self.context_path)]
            if verify_only:
                argv.append('--verify-only')
            raw = bounded(argv, self.config.get('website_timeout', 600), cwd=Path(__file__).resolve().parents[1])
            self.context.update(json.loads(self.context_path.read_bytes()))
            evidence = verify_website(json.loads(raw), self.context)
            channel.update(status='verified', evidence=evidence, commit=evidence['commit'])
        except Exception as exc:
            # Preserve a commit written by publish even if live verification failed.
            if self.context_path.exists():
                context = json.loads(self.context_path.read_bytes())
                if context.get('commit'):
                    channel['commit'] = context['commit']
            channel.update(status='failed', error=str(exc))
        self.save()

    def render(self, website_url=None):
        renderer = load_module(self.config.get('renderer', DEFAULT_RENDERER))
        data = json.loads((self.folder / 'reviewed.json').read_bytes())
        validate_review(data, Issue.resolve(self.state['issueId']), renderer)
        message = renderer.build(data, recipient=self.recipient, sender=self.sender)
        if website_url:
            expected = f'https://twihn.com/issues/{self.state["issueId"]}/'
            if website_url != expected:
                raise RuntimeError('unexpected issue URL')
            parts = list(message.iter_parts())
            plain, html = parts[0].get_content(), parts[1].get_content()
            parts[0].set_content(plain + '\nRead on the web: ' + website_url + '\n')
            parts[1].set_content(html.replace('</body>', '<p>Read on the web: <a href="' + escape(website_url, quote=True) + '">This Week in Hacker News</a></p></body>'), subtype='html')
        return message

    def email_evidence(self):
        channel = self.state['email']
        if not channel.get('messageId') or not channel.get('messageSha256'):
            raise RuntimeError('no durable send intent to reconcile; restore state backup')
        self.context.update(messageId=channel['messageId'], messageSha256=channel['messageSha256'])
        self.write_context()
        evidence = command(self.config, 'email_verify', self.context_path, self.context)
        if evidence.get('verified') is not True or not evidence.get('sentItemsId'):
            raise RuntimeError('exact Sent Items readback not confirmed')
        for field, expected in [('messageId',channel['messageId']),('messageSha256',channel['messageSha256']),('recipient',self.recipient)]:
            if evidence.get(field) != expected:
                raise RuntimeError(f'Sent Items readback mismatch: {field}')
        channel.update(status='verified', evidence=evidence)

    def reconcile(self, channel):
        if channel == 'website':
            self.website(verify_only=True)
        else:
            email = self.state['email']
            if email['status'] == 'verified':
                return self.report()
            if email['status'] not in {'ambiguous','in_progress'}:
                raise RuntimeError('email reconciliation requires a durable prior send intent')
            email['status'] = 'ambiguous'; self.save()
            try:
                self.email_evidence()
            except Exception as exc:
                email['error'] = str(exc)
            self.save()
        return self.report()

    def resume(self, *, website_only=False):
        email = self.state['email']
        # A crash after durable send intent is ambiguous even if SMTP never began.
        if email['status'] == 'in_progress':
            email.update(status='ambiguous', error='interrupted send; reconciliation required')
            self.save()
        if website_only:
            self.website()
            return self.report()
        if email['status'] in {'verified', 'ambiguous'}:
            # Do not even render/preflight or mutate website on a completed/uncertain rerun.
            return self.report()
        try:
            baseline = self.render()
            validate_message(baseline, self.sender, self.recipient)
            atomic(self.folder / 'baseline.eml', baseline.as_bytes(policy=policy.SMTP))
        except Exception as exc:
            email.update(status='failed', error=f'baseline validation failed: {exc}')
            self.save()
            return self.report()
        self.website()
        message = baseline
        if self.state['website']['status'] == 'verified':
            try:
                linked = self.render(self.state['website']['evidence']['url'])
                validate_message(linked, self.sender, self.recipient)
                message = linked
            except Exception:
                message = baseline
        try:
            preflight = command(self.config, 'email_preflight', self.context_path, self.context)
            if any(preflight.get(key) is not True for key in ('accountChecked','sentItemsChecked')) or preflight.get('sender') != self.sender or preflight.get('recipient') != self.recipient:
                raise RuntimeError('account/Sent Items preflight not verified')
            # Stable across retries; never change this intent to evade ambiguous-send guard.
            message_id = f'<twihn.{self.state["issueId"]}.v{self.state["revision"]}.{self.state["reviewedSha256"][:20]}@{self.sender.rsplit("@",1)[1]}>'
            message['Message-ID'] = message_id
            raw = message.as_bytes(policy=policy.SMTP)
            atomic(self.folder / 'send.eml', raw)
            email.update(status='in_progress', attempt=1, messageId=message_id, messageSha256=digest(raw))
            self.save()  # Durable intent MUST precede invoking the submission adapter.
            self.context.update(messageId=message_id, messageSha256=digest(raw))
            self.write_context()
        except Exception as exc:
            # A durable intent may have been written before context persistence failed.
            email.update(status='ambiguous' if email['status'] == 'in_progress' else 'failed', error=str(exc))
            self.save()
            return self.report()
        try:
            command(self.config, 'email_send', self.context_path, self.context)
        except Exception as exc:
            email['error'] = str(exc)
        # Nonzero submit is also ambiguous (SMTP may succeed before IMAP save fails).
        email['status'] = 'ambiguous'; self.save()
        try:
            self.email_evidence()
        except Exception as exc:
            email['error'] = str(exc)
        self.save()
        return self.report()


def validate_message(message, sender, recipient):
    if message['From'] != sender or message['To'] != recipient or message.get('Bcc') or message.get('Cc'):
        raise RuntimeError('unexpected email addressing')
    if message.get_content_type() != 'multipart/alternative':
        raise RuntimeError('email must be multipart/alternative')
    parts = list(message.iter_parts())
    if [part.get_content_type() for part in parts] != ['text/plain', 'text/html']:
        raise RuntimeError('missing complete plain/HTML alternatives')
    if any(not part.get_content().strip() for part in parts):
        raise RuntimeError('empty MIME alternative')


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == '_website-worker':
        parser = argparse.ArgumentParser()
        parser.add_argument('_worker'); parser.add_argument('--config', required=True)
        parser.add_argument('--context', required=True); parser.add_argument('--verify-only', action='store_true')
        args = parser.parse_args(argv)
        try:
            print(json.dumps(website_worker(args.config, args.context, args.verify_only)))
            return 0
        except Exception as exc:
            print(str(exc), file=sys.stderr); return 1
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', type=Path, default=Path.home()/'.hermes/data/twihn')
    parser.add_argument('--config', type=Path)
    commands = parser.add_subparsers(dest='action', required=True)
    for name in ('bounds','prep','resume','website-only','reconcile'):
        sub = commands.add_parser(name)
        sub.add_argument('--issue', required=True)
        if name == 'prep':
            sub.add_argument('--reviewed', type=Path, required=True)
            sub.add_argument('--approve-reviewed', action='store_true')
            sub.add_argument('--renderer', type=Path, default=DEFAULT_RENDERER)
        if name == 'reconcile':
            sub.add_argument('--channel', choices=('website','email'), required=True)
    args = parser.parse_args(argv)
    try:
        issue = Issue.resolve(args.issue)
        if args.action == 'bounds':
            print(json.dumps(dict(issueId=issue.issue_id,start=issue.start,end=issue.end,timezone='Europe/London')))
            return 0
        store = Store(args.state_dir)
        with store.lock():
            if args.action == 'prep':
                state = store.prepare(issue,args.reviewed,approved=args.approve_reviewed,renderer_path=args.renderer)
            else:
                if args.config is None:
                    raise RuntimeError('--config is required for runtime adapters')
                runtime = Runtime(store,issue.issue_id,args.config)
                state = runtime.reconcile(args.channel) if args.action == 'reconcile' else runtime.resume(website_only=args.action == 'website-only')
            print(json.dumps(state, sort_keys=True))
            if (args.action == 'resume' or args.action == 'reconcile' and args.channel == 'email') and state['email']['status'] in {'failed','ambiguous'}:
                return 2
            if (args.action == 'website-only' or args.action == 'reconcile' and args.channel == 'website') and state['website']['status'] != 'verified':
                return 2
            return 0
    except Exception as exc:
        print(f'twihn runtime: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
