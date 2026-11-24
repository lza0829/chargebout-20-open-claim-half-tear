// 落账联动总线：任一写操作（开充/脉冲/结算/超时/急停/改桩态）成功后广播，
// 所有挂着的读路径（总览/桩位/枪线/会话册/开会话/结算/超时）统一重拉，同刷同帧。
const EVT = 'ledger:changed'

export function notifyLedgerChanged(detail = {}) {
  window.dispatchEvent(new CustomEvent(EVT, { detail }))
}

export function onLedgerChanged(handler) {
  const fn = (e) => handler(e.detail || {})
  window.addEventListener(EVT, fn)
  return () => window.removeEventListener(EVT, fn)
}
