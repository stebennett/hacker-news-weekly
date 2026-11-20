"""Freeze collection/review inputs for the existing research-capable weekly job.

Nothing in this module sends email or publishes content. `approve` prepares the
existing durable runtime; `resume` remains its only normal delivery entry point.
"""
from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
from zoneinfo import ZoneInfo

from . import tagging
from .orchestrator import (Issue, RuntimeError, Store, atomic, digest, instant,
                           json_bytes, load_module, secure_directory, validate_review)

ROOT = Path(__file__).resolve().parents[1]
HEADINGS = ['Top 10 stories by votes', 'Top 10 discussed stories',
            'Top 10 Ask HN threads by comments', 'All popular Show HN threads (50+ points)']


def dispatch_issue(jobs_path, job_id):
    """Use the scheduler's original occurrence, never the retry wall clock."""
    data = json.loads(Path(jobs_path).read_bytes())
    matches = [job for job in data['jobs'] if job['id'] == job_id]
    if len(matches) != 1:
        raise RuntimeError('scheduled job identity missing or ambiguous')
    job = matches[0]
    if job.get('timezone') != 'Europe/London' or job['schedule'].get('expr') != '0 8 * * 5':
        raise RuntimeError('unexpected weekly schedule/timezone')
    scheduled = instant(job['last_dispatch']['scheduled_at'])
    issue = Issue.resolve(scheduled.astimezone(ZoneInfo('Europe/London')).date().isoformat())
    if scheduled != instant(issue.end):
        raise RuntimeError('dispatch was not the intended Friday 08:00 occurrence')
    return issue


def collect_issue(issue):
    collector = load_module(ROOT / 'integrations/weekly_hn_digest.py', 'twihn_weekly_collector')
    stories, start, end = collector.collect(7, start=instant(issue.start), end=instant(issue.end))
    groups = collector.sections(stories, 10, 50)
    ordinary = sum(not {'ask_hn', 'show_hn'}.intersection(x.get('tags', [])) for x in stories)
    ask = sum('ask_hn' in x.get('tags', []) for x in stories)
    selected = json.loads(json.dumps(dict(start=issue.start, end=issue.end,
                         sections=[dict(heading=h, items=items) for h, items in zip(HEADINGS, groups)]), default=str))
    ordinary_stories = [x for x in stories if not {'ask_hn', 'show_hn'}.intersection(x.get('tags', []))]
    rankings = dict(score=[x['id'] for x in collector.rank(ordinary_stories, 'score')], comments=[x['id'] for x in collector.rank(ordinary_stories, 'comments')])
    return selected, [ordinary, ordinary, ask, len(groups[3])], rankings


def artifacts(folder, issue_id) -> dict:
    return dict(issueId=issue_id, selected=str(folder / 'selected.json'),
                collection=str(folder / 'collection.json'), reviewed=str(folder / 'reviewed.json'),
                sources=str(folder / 'sources.json'))


def load_collection(folder, issue):
    collection = json.loads((folder / 'collection.json').read_bytes())
    raw = (folder / 'selected.json').read_bytes()
    if (collection.get('issueId'), collection.get('start'), collection.get('end')) != (issue.issue_id, issue.start, issue.end):
        raise RuntimeError('collection identity/window mismatch')
    if digest(raw) != collection['selectedSha256']:
        raise RuntimeError('selected bytes changed after collection')
    return collection, json.loads(raw)


def begin(work_root, issue, *, now=None) -> dict:
    if (now or datetime.now(timezone.utc)) < instant(issue.end):
        raise RuntimeError('cannot collect an unfinished scheduled window')
    store = Store(work_root)
    with store.lock():
        folder = store.issue_dir(issue.issue_id)
        if (folder / 'collection.json').exists():
            load_collection(folder, issue)
            return dict(artifacts(folder, issue.issue_id), reused=True)
        if any(folder.iterdir()):
            raise RuntimeError('orphaned collection/research files; recover instead of recollecting')
        selected, counts, rankings = collect_issue(issue)
        collected_at = now or datetime.now(timezone.utc)
        raw = json_bytes(selected)
        collection = dict(version=1, issueId=issue.issue_id, start=issue.start, end=issue.end,
                          collectedAt=collected_at.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z'),
                          qualifyingCounts=counts, ordinaryRankings=rankings, selectedSha256=digest(raw))
        # The collection commit is written last. An interrupted write is refused,
        # not silently retried with newer metrics under an existing identity.
        atomic(folder / 'selected.json', raw)
        atomic(folder / 'collection.json', json_bytes(collection))
        return dict(artifacts(folder, issue.issue_id), reused=False)


def selected_parity(selected, reviewed):
    if set(reviewed) != {'start', 'end', 'sections'} or len(reviewed['sections']) != 4:
        raise RuntimeError('reviewed shape differs from selected collection')
    stripped = copy.deepcopy(reviewed)
    for section in stripped['sections']:
        if set(section) != {'heading', 'items'}:
            raise RuntimeError('unexpected reviewed section fields')
        for item in section['items']:
            for field in ('story', 'discussion'):
                item.pop(field, None)
    if stripped != selected:
        raise RuntimeError('research changed collected IDs, ordering, metrics, URLs, tags or bounds')


def approve(work_root, state_root, metadata_root, issue, reviewed_path, sources_path, *,
            approved=False, tagging_path=None):
    if not approved:
        raise RuntimeError('explicit --approve-reviewed required after grounded source research')
    work = Store(work_root)
    state = Store(state_root)
    # Global ordering: work lock before delivery lock. Neither delivery nor
    # website-only attempts acquire a work lock in reverse order.
    with work.lock(), state.lock():
        folder = work.issue_dir(issue.issue_id)
        collection, selected = load_collection(folder, issue)
        raw = Path(reviewed_path).read_bytes()
        reviewed = json.loads(raw)
        selected_parity(selected, reviewed)
        validate_review(reviewed, issue, load_module(ROOT / 'integrations/render_email.py'))
        result = state.prepare_bytes(issue, raw, approved=True)
        # Invalid/missing WEBSITE provenance must not suppress a validated email.
        # Freeze review first. The publisher refuses absent/mismatched metadata.
        error = None
        metadata_file = Path(metadata_root).expanduser().absolute() / f'{issue.issue_id}.json'
        try:
            tagging.private_path(metadata_file)
            secure_directory(metadata_root)
            if tagging_path is None:
                raise RuntimeError('tagging sidecar required for website preparation')
            provenance = json.loads(Path(sources_path).read_bytes()) if sources_path else None
            if not isinstance(provenance, dict) or set(provenance) != {'itemSources'}:
                raise RuntimeError('sources must contain only explicit itemSources')
            selected_ids = {item['id'] for group in reviewed['sections'] for item in group['items']}
            if set(provenance['itemSources']) != selected_ids:
                raise RuntimeError('source provenance IDs differ from selected review')
            # Snapshot once, after freezing the exact parity-validated review.
            # Never reread either mutable source when binding or writing bytes.
            tag_raw = tagging.read_private_bytes(tagging_path)
            tagging.validate_tagging(tag_raw, issue, result['reviewedSha256'], selected_ids)
            frozen_tagging = tagging.private_path(state.issue_dir(issue.issue_id) / 'tagging.json')
            metadata = dict(issueId=issue.issue_id, scheduledAt=issue.end, revision=1,
                            collectedAt=collection['collectedAt'], qualifyingCounts=collection['qualifyingCounts'], ordinaryRankings=collection['ordinaryRankings'],
                            itemSources=provenance['itemSources'], reviewedSha256=result['reviewedSha256'],
                            selectedSha256=collection['selectedSha256'],
                            collectionSha256=digest((folder / 'collection.json').read_bytes()),
                            taggingPath=str(frozen_tagging), taggingSha256=digest(tag_raw))
            meta_raw = json_bytes(metadata)
            committed = metadata_file.exists()
            if committed and tagging.read_private_bytes(metadata_file) != meta_raw:
                raise RuntimeError('website provenance/tagging already frozen; cannot overwrite on retry')
            tagging.freeze(frozen_tagging, tag_raw, committed=committed)
            # Commit marker last. A crash between these writes leaves an orphan
            # sidecar that must be explicitly recovered, never overwritten.
            if not committed:
                atomic(metadata_file, meta_raw)
        except (ValueError, OSError, TypeError, RuntimeError) as exc:
            error = str(exc)
        return dict(issueId=issue.issue_id, reviewedSha256=result['reviewedSha256'],
                    metadata=str(metadata_file), websitePreparationError=error,
                    emailPrepared=True, state=str(state.issue_dir(issue.issue_id) / 'state.json'))


def main_source(settings):
    """Read-only launch gate: locally tested source must match human-merged main.

    Compare all tracked paths outside weekly content, not just a claimed SHA;
    this also handles squash merges without relying on ancestry of the PR head.
    """
    from .github_api import GitHub
    client = GitHub(settings)
    env = client.git_env()
    subprocess.run(['git', 'fetch', '--no-tags', 'origin', settings.get('production_branch', 'main')],
                   cwd=ROOT, env=env, check=True, capture_output=True, timeout=45)
    ref = 'FETCH_HEAD'
    source_args = ['--', '.', ':(exclude)data/**', ':(exclude)dist/**']
    subprocess.run(['git', 'diff', '--exit-code', ref, *source_args], cwd=ROOT,
                   check=True, capture_output=True, timeout=15)
    # The diff above includes tracked working edits. Also reject untracked source.
    rows = subprocess.check_output(['git', 'ls-files', '--others', '--exclude-standard'], cwd=ROOT, text=True).splitlines()
    if rows:
        raise RuntimeError('untracked source files present; refusing production launch')
    return dict(mergedSource=True, commit=subprocess.check_output(['git', 'rev-parse', ref], cwd=ROOT, text=True).strip())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    gate = sub.add_parser('launch-check')
    gate.add_argument('--settings', type=Path, required=True)
    for name in ('begin', 'approve'):
        command = sub.add_parser(name)
        identity = command.add_mutually_exclusive_group(required=True)
        identity.add_argument('--issue')
        identity.add_argument('--job-id')
        command.add_argument('--jobs', type=Path)
        command.add_argument('--work-root', type=Path, required=True)
        if name == 'approve':
            command.add_argument('--state-root', type=Path, required=True)
            command.add_argument('--metadata-root', type=Path, required=True)
            command.add_argument('--reviewed', type=Path, required=True)
            command.add_argument('--sources', type=Path)
            command.add_argument('--tagging', type=Path)
            command.add_argument('--approve-reviewed', action='store_true')
    args = parser.parse_args(argv)
    try:
        if args.action == 'launch-check':
            result = main_source(json.loads(args.settings.read_bytes()))
        else:
            if args.job_id and args.jobs is None:
                raise RuntimeError('--jobs is required with --job-id')
            issue = Issue.resolve(args.issue) if args.issue else dispatch_issue(args.jobs, args.job_id)
            if args.action == 'begin':
                result = begin(args.work_root, issue)
            else:
                result = approve(args.work_root, args.state_root, args.metadata_root, issue,
                                 args.reviewed, args.sources, approved=args.approve_reviewed,
                                 tagging_path=args.tagging)
        print(json.dumps(result)); return 0
    except Exception as exc:
        print(f'twihn weekly: {exc}', file=sys.stderr); return 2


if __name__ == '__main__':
    raise SystemExit(main())
