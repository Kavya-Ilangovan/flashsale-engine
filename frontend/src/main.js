import { createMachine } from './machine.js';
import { request } from './net.js';

const $ = (id) => document.getElementById(id);
const el = {
  name: $('name'), price: $('price'), clockLabel: $('clock-label'), clock: $('clock'),
  meterBar: $('meter-bar'), meterText: $('meter-text'),
  status: $('status'), detail: $('detail'), buy: $('buy'), alt: $('alt'),
};
const inr = new Intl.NumberFormat('en-IN', { style: 'currency', currency: 'INR', maximumFractionDigits: 0 });

const ctx = { sale: null, offset: 0, ticket: null, expiresAt: 0, key: null, stopTrack: null, note: '', userId: null };
let checkoutMod = null;
const loadCheckout = () => (checkoutMod ||= import('./checkout.js'));

const serverNow = () => Date.now() + ctx.offset;
const fmt = (ms) => {
  const s = Math.max(0, Math.ceil(ms / 1000));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), x = s % 60;
  const p = (n) => String(n).padStart(2, '0');
  return h ? `${h}:${p(m)}:${p(x)}` : `${p(m)}:${p(x)}`;
};
const newKey = () => (crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random().toString(16).slice(2)}-xxxxxxxx`);
const store = {
  get: (k) => { try { return sessionStorage.getItem(k); } catch (_) { return null; } },
  set: (k, v) => { try { v == null ? sessionStorage.removeItem(k) : sessionStorage.setItem(k, v); } catch (_) {} },
};

const machine = createMachine('LOADING', (state, _prev, event, data) => {
  if (data && data.ticket) { ctx.ticket = data.ticket; store.set('ticket', ctx.ticket); }
  if (data && data.expiresAt) ctx.expiresAt = Number(data.expiresAt);
  if (state === 'RATE_LIMITED') ctx.note = `Many people are buying right now. Retrying in ${Math.ceil(data.waitMs / 1000)}s.`;
  else ctx.note = '';
  if (state === 'RESERVED' && event === 'RESERVED') startTracking(data && data.status);
  if (['SOLD_OUT', 'EXPIRED', 'FAILED', 'CANCELLED', 'CONFIRMED'].includes(state)) {
    if (state !== 'CONFIRMED') store.set('attempt', null);
    if (ctx.stopTrack) { ctx.stopTrack(); ctx.stopTrack = null; }
  }
  if (event === 'NOT_STARTED' && data && data.startsAt) { ctx.sale.startsAt = data.startsAt; }
  render();
});

function startTracking(initial) {
  loadCheckout().then(({ track }) => {
    if (ctx.stopTrack) ctx.stopTrack();
    ctx.stopTrack = track(ctx.ticket, (status) => machine.send(status));
    if (initial && initial !== 'PROCESSING') machine.send(initial);
  });
}

const COPY = {
  LOADING:         ['Loading the sale…', 'One moment.'],
  WAITING:         ['The sale has not started', 'The button unlocks the moment the clock hits zero.'],
  READY:           ['The sale is live', 'One unit per customer. Tap once; we handle the rest.'],
  SUBMITTING:      ['Securing your unit…', 'Your request is sent. Please do not refresh — if you do, it is safe: nothing is duplicated.'],
  RATE_LIMITED:    ['High demand', ''],
  RETRYABLE_ERROR: ['We could not reach the store', 'Your spot has not been lost. Check your connection and try again.'],
  RESERVED:        ['Request received', 'Securing your reservation…'],
  PAYMENT_PENDING: ['Unit reserved for you', ''],
  PAYING:          ['Confirming payment…', 'Do not close this page.'],
  CONFIRMED:       ['Order confirmed', ''],
  SOLD_OUT:        ['Sold out', 'All units are allocated. If a reservation lapses or a payment fails, a unit can return to the pool.'],
  EXPIRED:         ['Reservation expired', 'The unit went back to the pool before payment arrived. You can try again.'],
  FAILED:          ['Payment failed', 'The unit was released. You can try again.'],
  CANCELLED:       ['Reservation cancelled', 'The unit was released. You can try again.'],
};
const BUTTON = {
  LOADING: ['Loading…', true], WAITING: ['Buy now', true], READY: ['Buy now', false], SUBMITTING: ['Securing…', true],
  RATE_LIMITED: ['Retrying…', true], RETRYABLE_ERROR: ['Try again', false], RESERVED: ['Reserving…', true],
  PAYMENT_PENDING: ['Pay now (demo)', false], PAYING: ['Paying…', true], CONFIRMED: ['Done', true],
  SOLD_OUT: ['Check availability', false], EXPIRED: ['Try again', false], FAILED: ['Try again', false], CANCELLED: ['Try again', false],
};

function render() {
  const s = machine.state;
  const [title, detail] = COPY[s];
  el.status.textContent = title;
  el.detail.textContent = ctx.note || detail;
  const [label, disabled] = BUTTON[s];
  el.buy.textContent = s === 'PAYMENT_PENDING' && ctx.sale ? `Pay ${inr.format(ctx.sale.priceInr)} (demo)` : label;
  el.buy.disabled = disabled;
  el.buy.dataset.state = s;
  el.alt.hidden = s !== 'PAYMENT_PENDING';
  document.body.dataset.state = s;
  tick();
}

function tick() {
  const s = machine.state;
  if (!ctx.sale) return;
  if (s === 'WAITING' || s === 'LOADING') {
    const left = ctx.sale.startsAt - serverNow() - (ctx.jitter || 0);
    el.clockLabel.textContent = 'Opens in';
    el.clock.textContent = ctx.sale.startsAt ? fmt(left) : '--:--';
    if (ctx.sale.startsAt && left <= 0 && s === 'WAITING' && !ctx.sale.paused) machine.send('OPEN');
  } else if (s === 'PAYMENT_PENDING' || s === 'PAYING') {
    el.clockLabel.textContent = 'Pay within';
    el.clock.textContent = fmt(ctx.expiresAt - serverNow());
  } else if (s === 'CONFIRMED') {
    el.clockLabel.textContent = 'Order'; el.clock.textContent = ctx.ticket ? ctx.ticket.slice(0, 8) : '';
  } else {
    el.clockLabel.textContent = s === 'SOLD_OUT' ? 'Status' : 'Sale';
    el.clock.textContent = s === 'SOLD_OUT' ? '0 left' : 'Live';
  }
}

function renderMeter() {
  const { available, totalStock } = ctx.sale;
  if (available == null) return;
  el.meterBar.style.transform = `scaleX(${Math.max(0, Math.min(1, available / totalStock))})`;
  el.meterText.textContent = available > 0 ? `${available} of ${totalStock} units left` : 'All units allocated';
}

async function ensureSession() {
  const r = await request('POST', '/api/session', { body: { userId: localStorage.getItem('uid') || undefined } });
  if (r.status === 200) { ctx.userId = r.body.userId; try { localStorage.setItem('uid', r.body.userId); } catch (_) {} }
  return r.status === 200;
}

async function syncClock() {
  const t0 = Date.now();
  const r = await request('GET', '/api/time', { timeout: 4000 });
  const t1 = Date.now();
  if (r.status === 200 && r.body.t) ctx.offset = r.body.t * 1000 - (t0 + t1) / 2;
}

async function loadSale() {
  const r = await request('GET', '/api/sale');
  if (r.status !== 200) return false;
  ctx.sale = r.body;
  el.name.textContent = r.body.name;
  el.price.textContent = inr.format(r.body.priceInr);
  renderMeter();
  return true;
}

async function boot() {
  render();
  const [okSale] = await Promise.all([loadSale(), syncClock(), ensureSession()]);
  if (!okSale) { machine.send('LOAD_FAILED'); return; }
  loadCheckout();
  ctx.jitter = ctx.sale.jitterMs ? Math.random() * ctx.sale.jitterMs : 0;

  const mine = await request('GET', '/api/me/order');
  if (mine.status === 200 && mine.body.ticket) {
    ctx.ticket = mine.body.ticket; ctx.expiresAt = Number(mine.body.expiresAt) || 0;
    machine.send('RESUME', { ticket: mine.body.ticket, status: mine.body.status });
    startTracking(mine.body.status);
  } else {
    machine.send('LOADED');
  }
  setInterval(tick, 200);
  (function poll() {
    setTimeout(async () => {
      if (['WAITING', 'READY', 'SOLD_OUT'].includes(machine.state) && (await loadSale())) {
        if (machine.state === 'READY' && ctx.sale.paused) machine.send('CLOSE');
      }
      poll();
    }, 4000 + Math.random() * 3000);
  })();
}

el.buy.addEventListener('click', async () => {
  const s = machine.state;
  if (!ctx.sale) { location.reload(); return; }
  if (s === 'SOLD_OUT') { await loadSale(); if (ctx.sale.available > 0) machine.send('REOPEN'); return; }
  if (s === 'PAYMENT_PENDING') {
    if (!machine.send('PAY')) return;
    const { pay } = await loadCheckout();
    const r = await pay(ctx.ticket, 'success');
    if (r.status === 200 && r.body.status) machine.send(r.body.status);
    else if (r.status === 409 && r.body.status) machine.send(r.body.status);
    else machine.send('PAYMENT_PENDING');
    return;
  }
  if (!machine.send('CLICK')) return;
  const key = store.get('attempt') || newKey();
  store.set('attempt', key);
  const { submit } = await loadCheckout();
  await submit(key, (ev, data) => machine.send(ev, data), ensureSession);
});

el.alt.addEventListener('click', async () => {
  if (machine.state !== 'PAYMENT_PENDING') return;
  const { pay } = await loadCheckout();
  const r = await pay(ctx.ticket, 'cancel');
  if (r.status === 200 && r.body.status) machine.send(r.body.status);
});

boot();
