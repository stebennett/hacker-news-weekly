import test from 'node:test';
import assert from 'node:assert/strict';
import { parseStrictJSON } from '../scripts/strict-json.mjs';

const parse = text => parseStrictJSON(Buffer.from(text));

test('strict JSON preserves normal parsed semantics and never mutates valid source bytes',()=>{
  assert.equal(typeof parseStrictJSON,'function');
  const samples=[
    ' \r\n{ "z": [null,true,false,-0,1.2e+3], "a": "é 😀 \\u0041 \\ud800" }\t\n',
    String.raw`{"text":"braces { } [ ] and key-looking \"x\":1,\"x\":2; slash \\; quote \""}`,
    '{"a":{"key":1},"b":{"key":2},"array":[{"key":3},{"key":4}]}',
    '{"__proto__":1,"constructor":2,"toString":3,"hasOwnProperty":4,"":5}',
    '"a string"','123','null','true','false','[]','{}',
    String.raw`{"é":1,"e\u0301":2,"𝄞":3}`
  ];
  for (const text of samples) {
    const bytes=Buffer.from(text), before=Buffer.from(bytes);
    assert.deepEqual(parseStrictJSON(bytes),JSON.parse(text));
    assert.deepEqual(bytes,before,'accepted whitespace/order/Unicode/escapes stay byte-identical');
  }
});

for (const [name,text] of [
  ['identical names','{"a":1,"a":2}'],
  ['escaped equivalent',String.raw`{"tagSlugs":1,"tag\u0053lugs":2}`],
  ['all escaped key',String.raw`{"ai":1,"\u0061\u0069":2}`],
  ['nested object','{"outer":{"a":1,"a":2}}'],
  ['discarded nested private-shaped value','{"tags":{"hidden":{"note":"TEST_ONLY","note":null}},"tags":[]}'],
  ['object within arrays','[[{"a":1,"a":2}]]'],
  ['proto-like names','{"__proto__":1,"__proto__":2}'],
  ['constructor names','{"constructor":1,"constructor":2}'],
  ['empty names','{"":1,"":2}'],
  ['Unicode decoded names',String.raw`{"é":1,"\u00e9":2}`],
  ['surrogate pair equivalent',String.raw`{"𝄞":1,"\ud834\udd1e":2}`],
  ['escaped quote and slash',String.raw`{"a\"\\":1,"\u0061\"\\":2}`]
]) test(`strict JSON rejects ${name} without disclosing source data`,()=>{
  assert.equal(typeof parseStrictJSON,'function');
  assert.deepEqual(parse('{"valid":1}'),{valid:1});
  assert.throws(()=>parse(text),error=>{
    assert.equal(error.name,'SyntaxError');
    assert.equal(error.message,'Duplicate JSON property name');
    assert.equal(error.cause,undefined,'native parser diagnostics must not escape');
    return true;
  });
});

for (const text of [
  '{"private":"TEST_ONLY",}',
  '{"private":"TEST_ONLY"} trailing',
  String.raw`{"private":"TEST_ONLY\q"}`,
  '{"private":"TEST_ONLY\n"}',
  '{"a":01}', '[1,]', '{"a" 1}', '{"a":NaN}', '', '\ufeff{}'
]) test(`malformed JSON is rejected with a sanitized error (${JSON.stringify(text)})`,()=>{
  assert.deepEqual(parse('{}'),{});
  assert.throws(()=>parse(text),{name:'SyntaxError',message:'Invalid UTF-8 JSON'});
});

test('malformed UTF-8 is not silently replaced before validation or raw export',()=>{
  const bytes=Buffer.concat([Buffer.from('{"label":"'),Buffer.from([0xc3,0x28]),Buffer.from('"}')]);
  assert.throws(()=>parseStrictJSON(bytes),{name:'SyntaxError',message:'Invalid UTF-8 JSON'});
});

test('nesting is bounded with a sanitized safe failure',()=>{
  const nested=depth=>'['.repeat(depth)+'0'+']'.repeat(depth);
  assert.deepEqual(parse(nested(128)),JSON.parse(nested(128)));
  for (const depth of [129,10000]) assert.throws(()=>parse(nested(depth)),{
    name:'SyntaxError',message:'JSON nesting exceeds limit'
  });
});
