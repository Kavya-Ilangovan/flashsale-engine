import { request, sleep, jitter } from './net.js';

const MAX_ATTEMPTS = 6;
const TERMINAL = new Set(['CONFIRMED', 'FAILED', 'EXPIRED', 'CANCELLED', 'REJECTED']);

export async function submit(key, send, ensureSession) {
  for (let attempt = 0; attempt < MAX_ATTEMPTS; attempt++) {
    const r = await request('POST', '/api/checkout', { headers: { 'Idempotency-Key': key } });

    if (r.status === 202 || r.status === 200) {
      return send('RESERVED', { ticket: r.body.ticket, status: r.body.status, expiresAt: r.body.expiresAt });
    }
    if (r.status === 409) {
      switch (r.body.code) {
        case 'SOLD_OUT': return send('SOLD_OUT');
        case 'ALREADY_RESERVED': return send('RESERVED', { ticket: r.body.ticket, status: 'PROCESSING' });
        case 'SALE_NOT_STARTED': return send('NOT_STARTED', { startsAt: r.body.startsAt, serverTime: r.body.serverTime });
        default: return send('FAILED_RETRYABLE');
      }
    }
    if (r.status === 401) { await ensureSession(); continue; }

    const wait = jitter(Math.max(r.retryAfter * 1000, Math.min(8000, 400 * 2 ** attempt)));
    if (attempt === MAX_ATTEMPTS - 1) break;
    send('RATE_LIMITED', { waitMs: wait, status: r.status });
    await sleep(wait);
    send('RETRY');
  }
  send('FAILED_RETRYABLE');
}

export function track(ticket, onStatus) {
  let es = null; let timer = null; let guard = null; let stopped = false; let step = 0; let sseErrors = 0;
  const steps = [1000, 2000, 4000, 5000];

  const stop = () => { stopped = true; if (es) es.close(); clearTimeout(timer); clearInterval(guard); };
  const apply = (status) => { if (stopped) return; onStatus(status); if (TERMINAL.has(status)) stop(); };

  async function poll() {
    if (stopped) return;
    const r = await request('GET', `/api/orders/${ticket}`, { timeout: 6000 });
    if (r.status === 200) { apply(r.body.status); step = 0; }
    else if (r.status === 404) return apply('FAILED');
    else step = Math.min(step + 1, steps.length - 1);
    if (!stopped) timer = setTimeout(poll, jitter(steps[step]));
  }

  if ('EventSource' in window) {
    es = new EventSource(`/api/orders/${ticket}/events`);
    es.addEventListener('status', (e) => { sseErrors = 0; try { apply(JSON.parse(e.data).status); } catch (_) {} });
    es.onerror = () => {
      if (stopped) return;
      sseErrors++;
      if (es.readyState === 2 || sseErrors >= 3) { es.close(); es = null; poll(); }
    };
    guard = setInterval(async () => {
      if (stopped || !es) return;
      const r = await request('GET', `/api/orders/${ticket}`, { timeout: 6000 });
      if (r.status === 200) apply(r.body.status);
    }, 15000);
  } else {
    poll();
  }
  return stop;
}

export const pay = (ticket, outcome) => request('POST', `/api/orders/${ticket}/pay`, { body: { outcome } });
