export const TRANSITIONS = {
  LOADING:         { LOADED: 'WAITING', LOAD_FAILED: 'RETRYABLE_ERROR', RESUME: 'RESERVED' },
  WAITING:         { OPEN: 'READY', RESUME: 'RESERVED' },
  READY:           { CLICK: 'SUBMITTING', RESUME: 'RESERVED', CLOSE: 'WAITING' },
  SUBMITTING:      { RESERVED: 'RESERVED', SOLD_OUT: 'SOLD_OUT', NOT_STARTED: 'WAITING', RATE_LIMITED: 'RATE_LIMITED', FAILED_RETRYABLE: 'RETRYABLE_ERROR' },
  RATE_LIMITED:    { RETRY: 'SUBMITTING', FAILED_RETRYABLE: 'RETRYABLE_ERROR' },
  RETRYABLE_ERROR: { CLICK: 'SUBMITTING', LOADED: 'WAITING' },
  RESERVED:        { PROCESSING: 'RESERVED', PAYMENT_PENDING: 'PAYMENT_PENDING', CONFIRMED: 'CONFIRMED', FAILED: 'FAILED', EXPIRED: 'EXPIRED', CANCELLED: 'CANCELLED', REJECTED: 'SOLD_OUT' },
  PAYMENT_PENDING: { PAY: 'PAYING', CONFIRMED: 'CONFIRMED', FAILED: 'FAILED', EXPIRED: 'EXPIRED', CANCELLED: 'CANCELLED' },
  PAYING:          { CONFIRMED: 'CONFIRMED', FAILED: 'FAILED', EXPIRED: 'EXPIRED', CANCELLED: 'CANCELLED', PAYMENT_PENDING: 'PAYMENT_PENDING' },
  SOLD_OUT:        { REOPEN: 'READY' },
  EXPIRED:         { CLICK: 'SUBMITTING' },
  FAILED:          { CLICK: 'SUBMITTING' },
  CANCELLED:       { CLICK: 'SUBMITTING' },
  CONFIRMED:       {},
};

export function createMachine(initial, onChange) {
  let state = initial;
  return {
    get state() { return state; },

    send(event, data) {
      const next = TRANSITIONS[state] && TRANSITIONS[state][event];
      if (!next) return false;
      const prev = state;
      state = next;
      onChange(state, prev, event, data);
      return true;
    },
  };
}
