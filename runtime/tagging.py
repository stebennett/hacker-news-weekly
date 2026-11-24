"""Bound and freeze private website tagging bytes; never alter email review data.

These are bounded structural/binding checks, not a second taxonomy. Node's
public-data layer remains authoritative for archive reconciliation at publish.
The small repeated registry contract is exercised against Node in test_tagging.
Call freeze only while holding weekly's work-then-delivery Store locks.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import stat
import unicodedata

from .orchestrator import RuntimeError, atomic, digest, secure_directory

ROOT = Path(__file__).resolve().parents[1]
MAX_BYTES = 16 * 1024 * 1024
SLUG = re.compile(r'[a-z0-9]+(?:-[a-z0-9]+)*')
IDENT = re.compile(r'[1-9][0-9]*')
SHA = re.compile(r'[a-f0-9]{64}')
# ECMAScript whitespace, not Python's broader isspace (e.g. U+0085).
JS_SPACE = '\t\n\v\f\r \u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff'


def require(condition, message):
    if not condition:
        raise RuntimeError(f'tagging: {message}')


def exact_keys(value, keys):
    require(isinstance(value, dict) and set(value) == set(keys), 'invalid object fields')


def validate_registry(value):
    exact_keys(value, ('schemaVersion', 'tags'))
    require(type(value['schemaVersion']) is int and value['schemaVersion'] == 1,
            'invalid registry version')
    require(isinstance(value['tags'], list), 'registry tags must be an array')
    slugs, labels = [], set()
    for tag in value['tags']:
        exact_keys(tag, ('slug', 'label'))
        slug, label = tag['slug'], tag['label']
        require(isinstance(slug, str) and len(slug) <= 80 and SLUG.fullmatch(slug), 'invalid slug')
        require(isinstance(label, str) and 1 <= len(label) <= 80 and
                not re.search(r'[\x00-\x1f\x7f-\x9f]', label), 'invalid label')
        require(label == label.strip(JS_SPACE) and label == unicodedata.normalize('NFC', label),
                'label must be trimmed and NFC')
        key = re.sub(f'[{JS_SPACE}]+', ' ', label).lower()
        require(key and key not in labels, 'duplicate or blank registry label')
        labels.add(key); slugs.append(slug)
    require(all(a < b for a, b in zip(slugs, slugs[1:])), 'registry slugs must be unique and sorted')
    return value


def registry_sha256(value):
    # Match JS stableJSON: recursively sorted keys, two spaces, literal Unicode
    # and a trailing newline. JSON.stringify escapes unpaired UTF-16 surrogates.
    text = json.dumps(validate_registry(value), sort_keys=True, indent=2, ensure_ascii=False) + '\n'
    text = re.sub(r'[\ud800-\udfff]', lambda m: f'\\u{ord(m[0]):04x}', text)
    return digest(text.encode('utf-8'))


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, 'duplicate JSON object field')
        result[key] = value
    return result


def validate_tagging(raw, issue, reviewed_sha256, selected_ids):
    """Validate the caller's exact byte snapshot against the actual review."""
    require(len(raw) <= MAX_BYTES, 'candidate exceeds byte limit')
    try:
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_object)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise RuntimeError('tagging: invalid UTF-8 JSON') from exc
    exact_keys(value, ('schemaVersion', 'issueId', 'reviewedSha256', 'registrySha256',
                       'registry', 'assignments', 'additions'))
    require(type(value['schemaVersion']) is int and value['schemaVersion'] == 1, 'invalid version')
    require(value['issueId'] == issue.issue_id, 'issue binding mismatch')
    require(isinstance(value['reviewedSha256'], str) and SHA.fullmatch(value['reviewedSha256'])
            and value['reviewedSha256'] == reviewed_sha256, 'actual reviewed bytes binding mismatch')
    require(isinstance(value['registrySha256'], str) and SHA.fullmatch(value['registrySha256'])
            and value['registrySha256'] == registry_sha256(value['registry']), 'registry digest mismatch')
    additions = value['additions']
    require(isinstance(additions, list), 'additions must be an array')
    validate_registry(dict(schemaVersion=1, tags=additions))
    tags = value['registry']['tags'] + additions
    validate_registry(dict(schemaVersion=1, tags=sorted(tags, key=lambda tag: tag['slug'])))
    slugs = {tag['slug'] for tag in tags}
    assignments = value['assignments']
    require(isinstance(assignments, dict) and assignments and set(assignments) == set(selected_ids)
            and all(isinstance(key, str) and IDENT.fullmatch(key) for key in assignments),
            'assignments must cover exact unique reviewed IDs')
    used = set()
    for assigned in assignments.values():
        require(isinstance(assigned, list) and assigned and
                all(isinstance(slug, str) and slug in slugs for slug in assigned),
                'assignment must contain nonempty valid tag references')
        require(all(a < b for a, b in zip(assigned, assigned[1:])), 'assignments must be unique and sorted')
        used.update(assigned)
    require(all(tag['slug'] in used for tag in additions), 'unused addition')
    return value


def private_path(filename):
    """Refuse repository/public destinations and symlinked path components.

    Do not forbid directories merely named 'data': the runtime's private root
    itself lives under ~/.hermes/data. Repository data/public/dist are all
    excluded by rejecting the entire source tree, as in the Node candidate CLI.
    """
    path = Path(filename).expanduser().absolute()
    require(not Path(os.path.abspath(path)).is_relative_to(ROOT), 'path must be outside repository/public data')
    for component in [*reversed(path.parents), path]:
        require(not component.is_symlink(), 'symlink private path')
    return path


def read_private_bytes(filename):
    """Read one bounded, owned, private regular-file snapshot without following links."""
    path = private_path(filename)
    parent = path.parent.stat()
    require(parent.st_uid == os.getuid() and stat.S_ISDIR(parent.st_mode)
            and parent.st_mode & 0o077 == 0, 'parent must be a private owned directory')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        require(stat.S_ISREG(before.st_mode) and before.st_uid == os.getuid()
                and before.st_mode & 0o077 == 0, 'file must be private, owned and regular')
        require(before.st_size <= MAX_BYTES, 'file exceeds byte limit')
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            raw = stream.read(MAX_BYTES + 1)
        after = os.fstat(fd)
        require(len(raw) == before.st_size and
                (before.st_size, before.st_mtime_ns, before.st_ctime_ns) ==
                (after.st_size, after.st_mtime_ns, after.st_ctime_ns), 'file changed while reading')
        return raw
    finally:
        os.close(fd)


def freeze(filename, raw, *, committed):
    """Metadata is the commit marker. Never repair lost or orphaned frozen bytes."""
    path = private_path(filename)
    secure_directory(path.parent)
    exists = path.exists()  # private_path already rejects even dangling symlinks
    if committed:
        require(exists, 'committed frozen sidecar is missing; restore backup')
        require(read_private_bytes(path) == raw, 'sidecar already frozen; cannot overwrite on retry')
    else:
        require(not exists, 'orphan frozen sidecar; restore metadata backup instead of overwriting')
        atomic(path, raw)
    return path
