"""Private preparation contracts, with real Node-produced sidecar fixtures."""
import copy
from functools import lru_cache
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from runtime.orchestrator import Issue, RuntimeError, digest
from test_runtime import reviewed


def node_json(script, value):
    # Tests use the verified pinned executable on PATH, never a model/network.
    node = shutil.which('node')
    if not node:
        raise AssertionError('pinned Node must be on PATH for cross-layer fixtures')
    return subprocess.check_output([node, '--input-type=module', '-e', script],
        input=json.dumps(value).encode(), cwd=ROOT, timeout=15,
        env={'PATH': str(Path(node).parent), 'HOME': os.environ.get('HOME', '')})


@lru_cache(maxsize=32)
def candidate_bytes(review_raw, issue_id):
    return node_json('''
import {validateTagging, registrySha256} from './scripts/tags.mjs';
import {stableJSON} from './scripts/content.mjs';
let text=''; for await (const chunk of process.stdin) text+=chunk;
const {review,issueId,reviewedSha256}=JSON.parse(text);
const registry={schemaVersion:1,tags:[{slug:'ai',label:'AI'}]};
const ids=[...new Set(review.sections.flatMap(section=>section.items.map(item=>item.id)))];
const input={schemaVersion:1,issueId,reviewedSha256,registrySha256:registrySha256(registry),registry,
 stories:ids.map(id=>({id,title:'Fixture',story:'Reviewed story.',discussion:'Reviewed discussion.',existingTagSlugs:[]}))};
const response={assignments:Object.fromEntries(ids.map(id=>[id,['AI','Security']]))};
process.stdout.write(stableJSON(validateTagging(input,response)));
''', dict(review=json.loads(review_raw), issueId=issue_id, reviewedSha256=digest(review_raw)))


class TaggingTests(unittest.TestCase):
    def setUp(self):
        from runtime import tagging
        self.tagging = tagging
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.issue = Issue.resolve('2026-11-13')
        self.review_raw = json.dumps(reviewed(self.issue)).encode()
        self.raw = candidate_bytes(self.review_raw, self.issue.issue_id)
        self.value = json.loads(self.raw)
        self.ids = set(self.value['assignments'])
        self.path = self.root / 'candidate.json'
        self.path.write_bytes(self.raw)
        self.path.chmod(0o600)

    def validate(self, value):
        return self.tagging.validate_tagging(json.dumps(value).encode(), self.issue,
            digest(self.review_raw), self.ids)

    def test_real_node_sidecar_and_registry_hash_match(self):
        self.assertEqual(self.validate(self.value), self.value)
        self.assertEqual(self.tagging.registry_sha256(self.value['registry']), self.value['registrySha256'])

    def test_rejects_changed_binding_issue_root_ids_and_assignments(self):
        mutations = [lambda v: v.update(reviewedSha256='0'*64),
            lambda v: v.update(issueId='2026-11-20'),
            lambda v: v.update(registrySha256='0'*64),
            lambda v: v.update(extra='private'),
            lambda v: v.update(schemaVersion=True),
            lambda v: v['assignments'].pop('1'),
            lambda v: v['assignments'].update({'01':['ai']}),
            lambda v: v['assignments'].update({'1':[]}),
            lambda v: v['assignments'].update({'1':['unknown']}),
            lambda v: v['assignments'].update({'1':['ai','ai']}),
            lambda v: v['assignments'].update({'1':['security','ai']}),
            lambda v: v.update(additions=[{'slug':'ai','label':'Other'}]),
            lambda v: v.update(additions=[{'slug':'security','label':'Security','private':True}]),
            lambda v: v.update(additions=[{'slug':'unused','label':'Unused'}])]
        for index, mutate in enumerate(mutations):
            with self.subTest(index=index):
                value = copy.deepcopy(self.value); mutate(value)
                with self.assertRaises(RuntimeError): self.validate(value)
        for raw in [b'[]', b'null', b'{"schemaVersion":1,"schemaVersion":1}', b'\xff', b'{']:
            with self.subTest(raw=raw), self.assertRaises(RuntimeError):
                self.tagging.validate_tagging(raw, self.issue, digest(self.review_raw), self.ids)

    def test_registry_common_vectors_match_node_and_unicode_canonical_hash(self):
        valid = [dict(schemaVersion=1,tags=[]), dict(schemaVersion=1,tags=[
            {'slug':'c','label':'C'}, {'slug':'c-plus-plus','label':'C++'},
            {'slug':'c-sharp','label':'C#'}, {'slug':'cafe','label':'Café ☕'},
            {'slug':'space','label':'Deep  Space'}])]
        invalid = []
        for tag in [{'slug':'../bad','label':'Bad'}, {'slug':'a\n','label':'A'},
                    {'slug':'a','label':' A'}, {'slug':'a','label':'A\x7f'},
                    {'slug':'a','label':'Cafe\u0301'}, {'slug':'a','label':'x'*81},
                    {'slug':'a','label':''}, {'slug':'a','label':'\ufeff'},
                    {'slug':'a','label':'A','private':True}]:
            invalid.append(dict(schemaVersion=1,tags=[tag]))
        invalid += [dict(schemaVersion=1,tags=[{'slug':'b','label':'B'},{'slug':'a','label':'A'}]),
            dict(schemaVersion=1,tags=[{'slug':'a','label':'Deep Space'},{'slug':'b','label':'deep  space'}]),
            dict(schemaVersion=1,tags=[{'slug':'a','label':'A'},{'slug':'a','label':'B'}]),
            dict(schemaVersion=1,tags=[],private=True)]
        vectors = valid + invalid
        actual = json.loads(node_json('''
import {validateRegistry,registrySha256} from './scripts/tags.mjs';
let text=''; for await(const chunk of process.stdin) text+=chunk;
console.log(JSON.stringify(JSON.parse(text).map(v=>{try {validateRegistry(v); return registrySha256(v)}catch{return null}})));
''', vectors))
        for value, expected in zip(vectors, actual):
            with self.subTest(value=value):
                if expected is None:
                    with self.assertRaises(RuntimeError): self.tagging.registry_sha256(value)
                else:
                    self.assertEqual(self.tagging.registry_sha256(value), expected)

    def test_reads_once_bounded_owned_private_regular_file(self):
        self.assertEqual(self.tagging.read_private_bytes(self.path), self.raw)
        self.path.chmod(0o644)
        with self.assertRaises(RuntimeError): self.tagging.read_private_bytes(self.path)
        self.path.chmod(0o600)
        with patch.object(self.tagging, 'MAX_BYTES', 10):
            with self.assertRaises(RuntimeError): self.tagging.read_private_bytes(self.path)
        with patch.object(self.tagging.os, 'getuid', return_value=os.getuid()+1):
            with self.assertRaises(RuntimeError): self.tagging.read_private_bytes(self.path)
        self.root.chmod(0o755)
        with self.assertRaises(RuntimeError): self.tagging.read_private_bytes(self.path)

    def test_symlink_file_parent_fifo_and_repository_are_refused(self):
        link = self.root / 'link.json'; link.symlink_to(self.path)
        with self.assertRaises(RuntimeError): self.tagging.read_private_bytes(link)
        parent = self.root / 'alias'; parent.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(RuntimeError): self.tagging.read_private_bytes(parent/'candidate.json')
        fifo = self.root/'fifo'; os.mkfifo(fifo, 0o600)
        with self.assertRaises(RuntimeError): self.tagging.read_private_bytes(fifo)
        with self.assertRaises(RuntimeError): self.tagging.read_private_bytes(ROOT/'data/tags.json')
        with self.assertRaises(RuntimeError): self.tagging.private_path(ROOT/'new-private/tagging.json')


if __name__ == '__main__':
    unittest.main()
