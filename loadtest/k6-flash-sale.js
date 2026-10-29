import http from 'k6/http';
import { check, sleep } from 'k6';
import { Counter } from 'k6/metrics';

const BASE = __ENV.BASE || 'http://localhost:8080';
const RATE = Number(__ENV.RATE || 10000);
const DURATION = __ENV.DURATION || '10s';
const STOCK = Number(__ENV.STOCK || 500);

http.setResponseCallback(http.expectedStatuses(200, 202, 409, 429));

const reserved = new Counter('reserved_total');
const soldOut = new Counter('sold_out_total');
const limited = new Counter('rate_limited_total');
const serverErrors = new Counter('server_errors_total');

export const options = {
  scenarios: {
    burst: {
      executor: 'constant-arrival-rate',
      rate: RATE,
      timeUnit: '1s',
      duration: DURATION,
      preAllocatedVUs: Number(__ENV.VUS || 6000),
      maxVUs: Number(__ENV.MAX_VUS || 20000),
    },
  },
  thresholds: {
    server_errors_total: ['count==0'],
    reserved_total: [`count<=${STOCK}`],
    http_req_duration: ['p(95)<1500'],
  },
};

let token = null;
let attemptKey = null;

export function setup() {
  const sale = http.get(`${BASE}/api/sale`).json();
  const wait = (sale.startsAt - Date.now()) / 1000;
  if (wait > 0) sleep(wait + 0.2);
}

export default function () {
  if (!token) {
    const session = http.post(`${BASE}/api/session`, JSON.stringify({ userId: `k6_${__VU}_${Date.now() % 100000}` }), {
      headers: { 'content-type': 'application/json', 'User-Agent': 'Mozilla/5.0 k6-loadtest' },
    });
    if (session.status !== 200) return;
    token = session.json('token');
    attemptKey = `${__VU}-${Math.random().toString(16).slice(2)}-0000000000000000`.slice(0, 40);
  }
  const response = http.post(`${BASE}/api/checkout`, null, {
    headers: { Authorization: `Bearer ${token}`, 'Idempotency-Key': attemptKey, 'User-Agent': 'Mozilla/5.0 k6-loadtest' },
  });
  if (response.status === 202) reserved.add(1);
  else if (response.status === 409) soldOut.add(1);
  else if (response.status === 429) limited.add(1);
  else if (response.status !== 200) serverErrors.add(1);
  check(response, { 'explicit outcome': (r) => [200, 202, 409, 429].includes(r.status) });
}
