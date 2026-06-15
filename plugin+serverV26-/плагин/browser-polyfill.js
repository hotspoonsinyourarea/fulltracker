(function(global) {
  if (typeof global.browser !== 'undefined' && global.browser.runtime) return;
  const $c = global.chrome;
  if (!$c) return;
  function p(fn) {
    return (...args) => {
      try {
        const r = fn(...args);
        if (r && typeof r.then === 'function') return r;
        return new Promise((resolve, reject) => {
          fn(...args, (...res) => {
            const e = $c.runtime.lastError;
            e ? reject(new Error(e.message)) : resolve(res.length <= 1 ? res[0] : res);
          });
        });
      } catch (e) { return Promise.reject(e); }
    };
  }
  function safeWrap(obj, method) {
    try { return p(obj[method].bind(obj)); } catch (e) { return undefined; }
  }
  global.browser = {
    storage: $c.storage ? { local: { get: safeWrap($c.storage.local, 'get'), set: safeWrap($c.storage.local, 'set') } } : undefined,
    tabs: $c.tabs ? { query: safeWrap($c.tabs, 'query'), onUpdated: $c.tabs.onUpdated, onRemoved: $c.tabs.onRemoved } : undefined,
    runtime: $c.runtime ? { sendMessage: (...a) => $c.runtime.sendMessage(...a), onMessage: $c.runtime.onMessage } : undefined,
    alarms: $c.alarms ? { create: (...a) => $c.alarms.create(...a), onAlarm: $c.alarms.onAlarm } : undefined,
    action: $c.action || undefined
  };
})(typeof globalThis !== 'undefined' ? globalThis : typeof window !== 'undefined' ? window : self);