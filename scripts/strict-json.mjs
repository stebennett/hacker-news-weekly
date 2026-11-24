import { TextDecoder } from 'node:util';

// JSON.parse owns syntax and value semantics. Walk every source value as well:
// a reviver cannot see duplicate names (or nested values) already discarded by
// JSON.parse. Never expose native parse errors, property names or source values.
export function parseStrictJSON(bytes) {
  let text, parsed;
  try {
    // Keep a BOM visible to JSON.parse, and reject invalid UTF-8 rather than
    // validate replacement characters while publishing the original bytes.
    text = new TextDecoder('utf-8',{fatal:true,ignoreBOM:true}).decode(bytes);
    parsed = JSON.parse(text);
  } catch {
    throw new SyntaxError('Invalid UTF-8 JSON');
  }

  let offset = 0;
  const whitespace = () => {
    while (/[ \t\r\n]/u.test(text[offset] ?? '')) offset++;
  };
  const stringToken = () => {
    const start = offset++;
    // Syntax has already been checked, so every escape and closing quote is
    // valid. Skip escaped characters, including escaped quotes/backslashes.
    while (text[offset] !== '"') {
      if (text[offset] === '\\') offset++;
      offset++;
    }
    return text.slice(start,++offset);
  };
  function value(depth) {
    whitespace();
    const token = text[offset];
    if (token === '{' || token === '[') {
      if (++depth > 128) throw new SyntaxError('JSON nesting exceeds limit');
      const object = token === '{', end = object ? '}' : ']';
      const names = object ? new Set() : null;
      offset++; whitespace();
      if (text[offset] === end) {offset++; return;}
      while (true) {
        if (object) {
          const name = JSON.parse(stringToken());
          if (names.has(name)) throw new SyntaxError('Duplicate JSON property name');
          names.add(name);
          whitespace(); offset++; // colon
        }
        value(depth);
        whitespace();
        if (text[offset] === end) {offset++; return;}
        offset++; whitespace(); // comma
      }
    } else if (token === '"') {
      stringToken();
    } else {
      // Native JSON parsing has already validated numbers and literals.
      while (offset < text.length && !/[,\]} \t\r\n]/u.test(text[offset])) offset++;
    }
  }
  value(0);
  return parsed;
}
