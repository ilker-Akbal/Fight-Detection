// No browser/network timing: split headers/JPEGs and coalesced packets.
const assert = require('node:assert/strict');
const Parser = require('../Fight_backend_project/backend_frontend_project/static/operations/preview_stream.js');
const delivered = [];
const parser = new Parser((id, data) => delivered.push([id, Buffer.from(data).toString()]));
const wire = Buffer.from('3 cam-a\nabc4 cam-b\ndefg');
for (const byte of wire) parser.feed(Uint8Array.of(byte));
assert.deepEqual(delivered, [['cam-a', 'abc'], ['cam-b', 'defg']]);
parser.feed(Buffer.from('1 cam-a\nx1 cam-a\ny'));
assert.deepEqual(delivered.slice(2), [['cam-a', 'x'], ['cam-a', 'y']]);
assert.throws(() => new Parser(() => {}).feed(Buffer.from('999999999 a\n')));
assert.throws(() => new Parser(() => {}).feed(Buffer.from('1 unsafe/id\nx')));
assert.throws(() => new Parser(() => {}).feed(Buffer.alloc(129, 65)));
console.log('Preview stream framing: 5 assertions passed');
