// Run:  node --test demo-overlay/tests      (Node's built-in runner, no packages needed)
const test = require('node:test');
const assert = require('node:assert/strict');
const H = require('../varhelpers.js');

test('prettyName makes variable names readable', () => {
  assert.equal(H.prettyName('learners_json'), 'Learners');
  assert.equal(H.prettyName('moduleProgressMetrics_json'), 'Module progress metrics');
  assert.equal(H.prettyName('firstName'), 'First name');
  assert.equal(H.prettyName('userID'), 'User ID');
  assert.equal(H.prettyName('total_students'), 'Total students');
  assert.equal(H.prettyName('loc0_name'), 'Loc0 name');
  assert.equal(H.prettyName(''), '');
});

const students = [
  { name: 'Asha', grade: 'A', score: 91, passed: true, address: { city: 'Mumbai', zip: '400001' }, tags: ['a', 'b'], marks: [{ s: 1 }] },
  { name: 'Ravi', grade: 'B', score: 78, passed: false, address: { city: 'Pune', zip: '411001' }, tags: [], marks: [] },
];

test('columnsFor spreads one level of nested objects and types the columns', () => {
  const cols = H.columnsFor(students);
  const byLabel = Object.fromEntries(cols.map((c) => [c.label, c]));
  assert.deepEqual(Object.keys(byLabel), ['Name', 'Grade', 'Score', 'Passed', 'Address › City', 'Address › Zip', 'Tags', 'Marks']);
  assert.equal(byLabel['Score'].type, 'number');
  assert.equal(byLabel['Passed'].type, 'boolean');
  assert.equal(byLabel['Name'].type, 'text');
  assert.deepEqual(byLabel['Address › City'].path, ['address', 'city']);
  assert.equal(byLabel['Tags'].type, 'list');
  assert.equal(byLabel['Marks'].type, 'nested');          // a list of objects opens on its own
  assert.equal(H.columnsFor(students, 3).length, 3);
});

test('mixed number and text values fall back to text', () => {
  assert.equal(H.columnsFor([{ a: 1 }, { a: 'x' }])[0].type, 'text');
});

test('coerce keeps numbers and booleans typed and rejects bad input', () => {
  assert.deepEqual(H.coerce('1,250', 'number'), { ok: true, value: 1250 });
  assert.deepEqual(H.coerce('', 'number'), { ok: true, value: null });
  assert.equal(H.coerce('abc', 'number').ok, false);
  assert.deepEqual(H.coerce('Yes', 'boolean'), { ok: true, value: true });
  assert.deepEqual(H.coerce('no', 'boolean'), { ok: true, value: false });
  assert.equal(H.coerce('maybe', 'boolean').ok, false);
  assert.deepEqual(H.coerce('a, b ,, c', 'list', ['x']), { ok: true, value: ['a', 'b', 'c'] });
  assert.deepEqual(H.coerce('1, 2', 'list', [5, 6]), { ok: true, value: [1, 2] });   // a number list stays numbers
  assert.equal(H.coerce('1, x', 'list', [5, 6]).ok, false);
  assert.deepEqual(H.coerce(' keep spaces ', 'text'), { ok: true, value: ' keep spaces ' });
});

test('blankRow, cloneRow, insertAfter and removeAt edit rows without touching the others', () => {
  const cols = H.columnsFor(students);
  const blank = H.blankRow(cols);
  assert.deepEqual(blank, { name: '', grade: '', score: 0, passed: false, address: { city: '', zip: '' }, tags: [], marks: [] });
  const copy = H.cloneRow(students[0]);
  copy.address.city = 'Delhi';
  assert.equal(students[0].address.city, 'Mumbai');          // the copy is independent
  const added = H.insertAfter(students, 0, blank);
  assert.equal(added.length, 3);
  assert.equal(added[1], blank);
  assert.equal(students.length, 2);                           // the original list is unchanged
  assert.deepEqual(H.removeAt(added, 1), students);
});

test('setAt and getAt follow paths and create missing objects', () => {
  const o = {};
  H.setAt(o, ['a', 'b'], 5);
  assert.deepEqual(o, { a: { b: 5 } });
  assert.equal(H.getAt(o, ['a', 'b']), 5);
  assert.equal(H.getAt(o, ['x', 'y']), undefined);
});

test('friendlySummary has no JSON braces', () => {
  assert.equal(H.friendlySummary(students).startsWith('2 rows · Name, Grade, Score, Passed, Address › City'), true);
  assert.equal(H.friendlySummary([]), 'Empty list — click to add');
  assert.equal(H.friendlySummary(['x', 'y']), '2 values · x, y');
  assert.equal(H.friendlySummary({ firstName: 'A', total_count: 2 }), '2 fields · First name, Total count');
  assert.equal(/[{}\[\]]/.test(H.friendlySummary(students)), false);
});

test('urlPatternMatches follows the same rules as the page hook', () => {
  const u = 'https://app.acme.com/cw-api/69/students?page=2';
  assert.equal(H.urlPatternMatches('*/cw-api/69/students*', 'GET', u), true);
  assert.equal(H.urlPatternMatches('*/cw-api/70/students*', 'GET', u), false);
  assert.equal(H.urlPatternMatches('GET */cw-api/69/students*', 'GET', u), true);
  assert.equal(H.urlPatternMatches('POST */cw-api/69/students*', 'GET', u), false);
  assert.equal(H.urlPatternMatches('/cw-api/69/students?page=2', 'GET', u), true);       // path + query form
  assert.equal(H.urlPatternMatches('app.acme.com/cw-api/*', 'GET', u), true);            // host + path form
  assert.equal(H.urlPatternMatches('*/api/a.b*', 'GET', 'http://x.test/api/aXb'), false); // the dot is literal
  assert.equal(H.urlPatternMatches('', 'GET', u), false);
  assert.equal(H.urlPatternMatches('*/x*', 'GET', 'http://[bad'), false);                 // an unreadable URL never matches
});

test('empty means off: isSet and unsetNames', () => {
  assert.equal(H.isSet({ a: 'x' }, 'a'), true);
  assert.equal(H.isSet({ a: '' }, 'a'), false);
  assert.equal(H.isSet({ a: '   ' }, 'a'), false);
  assert.equal(H.isSet({ a: '[]' }, 'a'), true);              // an empty list still means "show no rows"
  assert.equal(H.isSet({}, 'a'), false);
  assert.equal(H.isSet({ a: 0 }, 'a'), true);
  assert.deepEqual(H.unsetNames('{{a}} and {{b}} and {{a}}', { a: 'x' }), ['b']);
  assert.deepEqual(H.unsetNames('{{a}}', { a: '' }), ['a']);
  assert.deepEqual(H.unsetNames('plain text', {}), []);
});

test('generated filter names map back to the variable the person edits', () => {
  assert.equal(H.derivedBase('revenue__currency'), 'revenue');
  assert.equal(H.derivedBase('rate__percent_1'), 'rate');
  assert.equal(H.derivedBase('revenue'), null);
  assert.equal(H.derivedBase('a__b'), null);                   // not one of the known filters
  assert.deepEqual(H.unsetNames('{{revenue__currency}}', {}), ['revenue']);
  assert.deepEqual(H.unsetNames('{{revenue__currency}}', { revenue: '5', revenue__currency: '$5' }), []);
});

test('the extension scripts contain no control characters (Chrome refuses to load a script with a NUL byte)', () => {
  const fs = require('node:fs');
  const path = require('node:path');
  for (const f of ['varhelpers.js', 'injectshape.js', 'content.js', 'page-hook.js', 'background.js', 'popup.js']) {
    const bytes = fs.readFileSync(path.join(__dirname, '..', f));
    const bad = [...bytes].filter((c) => c < 32 && c !== 9 && c !== 10 && c !== 13);
    assert.equal(bad.length, 0, f + ' has control characters');
  }
});
