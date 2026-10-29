import test from 'node:test';
import assert from 'node:assert/strict';
import { createMachine, TRANSITIONS } from '../src/machine.js';

test('only READY accepts CLICK: 50 rapid clicks start exactly one attempt', () => {
  let submits = 0;
  const m = createMachine('READY', (s, _p, ev) => { if (ev === 'CLICK') submits++; });
  const results = Array.from({ length: 50 }, () => m.send('CLICK'));
  assert.equal(results.filter(Boolean).length, 1);
  assert.equal(submits, 1);
  assert.equal(m.state, 'SUBMITTING');
});

test('invalid events are ignored', () => {
  const m = createMachine('WAITING', () => {});
  assert.equal(m.send('CLICK'), false);
  assert.equal(m.state, 'WAITING');
});

test('happy path and terminal states', () => {
  const m = createMachine('READY', () => {});
  for (const ev of ['CLICK', 'RESERVED', 'PAYMENT_PENDING', 'PAY', 'CONFIRMED']) assert.ok(m.send(ev), ev);
  assert.equal(m.state, 'CONFIRMED');
  assert.equal(m.send('CLICK'), false);
});

test('every transition target is a defined state', () => {
  for (const [from, t] of Object.entries(TRANSITIONS)) for (const to of Object.values(t)) assert.ok(TRANSITIONS[to], `${from} -> ${to}`);
});

test('rate-limited attempts keep the button locked until retry', () => {
  const m = createMachine('SUBMITTING', () => {});
  m.send('RATE_LIMITED');
  assert.equal(m.send('CLICK'), false);
  assert.ok(m.send('RETRY'));
});
