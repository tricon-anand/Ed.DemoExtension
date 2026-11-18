// Run:  node --test demo-overlay/tests
const test = require('node:test');
const assert = require('node:assert/strict');
const H = require('../injectshape.js');

test('fillMissing adds the ids and flags the real items had, never real text', () => {
  const original = [
    { resourceNodeId: 1, resourceVersion: 1, resourceName: 'Real Course A', isNew: false, owner: { id: 7, email: 'real@x.com' }, deleted: null },
    { resourceNodeId: 2, resourceVersion: 3, resourceName: 'Real Course B', isNew: true, owner: { id: 8, email: 'other@x.com' }, deleted: null },
  ];
  const injected = [
    { resourceNodeId: 90, resourceName: 'Astronomy Basics', owner: { email: 'demo@x.com' } },
    { resourceNodeId: 91, resourceName: 'Stargazing' },
    { resourceNodeId: 92, resourceName: 'Orbits' },
  ];
  const out = H.fillMissing(injected, original);
  assert.equal(out[0].resourceVersion, 1);                      // taken from the real item at the same position
  assert.equal(out[1].resourceVersion, 3);
  assert.equal(out[2].resourceVersion, 1);                      // cycles when the injected list is longer
  assert.equal(out[0].isNew, false);
  assert.equal(out[0].deleted, null);
  assert.equal(out[0].resourceNodeId, 90);                      // fields the injected item has are never overwritten
  assert.equal(out[0].owner.id, 7);                             // nested objects are completed too
  assert.equal(out[0].owner.email, 'demo@x.com');
  assert.equal('owner' in out[1], false);                       // a whole missing object is not copied (it may hold real data)
  const withEmpty = H.fillMissing([{ id: 5 }], [{ id: 1, sections: [], tags: ['real'] }]);
  assert.deepEqual(withEmpty[0].sections, []);                  // an empty list holds no real data: filled in
  assert.equal('tags' in withEmpty[0], false);                  // a list with real content is not copied
  assert.equal(JSON.stringify(out).includes('Real Course'), false);
  assert.equal(JSON.stringify(out).includes('real@x.com'), false);
  assert.equal('resourceVersion' in injected[0], false);        // the input is not modified
});

test('fillMissing leaves non-list shapes alone', () => {
  assert.deepEqual(H.fillMissing([1, 2], [5]), [1, 2]);
  assert.deepEqual(H.fillMissing({ a: 1 }, [{ b: 2 }]), { a: 1 });
  assert.deepEqual(H.fillMissing([{ a: 1 }], []), [{ a: 1 }]);
  assert.deepEqual(H.fillMissing('x', { a: 1 }), 'x');
});

test('fillMissing matches an injected item to the real item it stands for, not just by position', () => {
  const original = [
    { resourceNodeId: 100, resourceVersion: 4, resourceName: 'A' },
    { resourceNodeId: 45513143, resourceVersion: 1, resourceName: 'B' },
    { resourceNodeId: 300, resourceVersion: 7, resourceName: 'C' },
  ];
  // the invented list keeps the real id 45513143 (first position) but invents a new name
  const out = H.fillMissing([{ resourceNodeId: 45513143, resourceName: 'Intro Algebra' }, { resourceNodeId: 999, resourceName: 'New' }], original);
  assert.equal(out[0].resourceVersion, 1);        // matched on the shared id, not original[0] (version 4)
  assert.equal(out[1].resourceVersion, 1);        // nothing shared: position 1 -> original[1]
});
