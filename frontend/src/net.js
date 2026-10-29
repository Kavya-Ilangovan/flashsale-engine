export const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
export const jitter = (ms, ratio = 0.3) => ms * (1 + (Math.random() * 2 - 1) * ratio);

export async function request(method, path, { headers, body, timeout = 8000 } = {}) {
  const ctl = new AbortController();
  const t = setTimeout(() => ctl.abort(), timeout);
  try {
    const res = await fetch(path, {
      method, signal: ctl.signal, credentials: 'same-origin',
      headers: { ...(body ? { 'content-type': 'application/json' } : {}), ...headers },
      body: body ? JSON.stringify(body) : undefined,
    });
    let json = null;
    try { json = await res.json(); } catch (_) {}
    return { status: res.status, body: json || {}, retryAfter: Number(res.headers.get('retry-after')) || 0 };
  } catch (_) {
    return { status: 0, body: {}, retryAfter: 0 };
  } finally {
    clearTimeout(t);
  }
}
