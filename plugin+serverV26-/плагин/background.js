importScripts('browser-polyfill.js', 'lib/educational-sites.js');

const { DEFAULT_SITES, isEducational: _isEdu } = globalThis.__EDU;

const API_BASE = 'http://83.242.96.235:5002';
const SIMILARITY_URL = 'http://83.242.96.235:5006/similarity';
const RECOMMEND_URL = 'http://83.242.96.235:5006/recommend';
const MAX_OFFLINE_QUEUE = 100;
const EDU_CACHE = new Map();

let educationalSites = [...DEFAULT_SITES];
let activeTabs = {};
let anonId = null;

function isEducational(url) {
  if (!url) return false;
  const cached = EDU_CACHE.get(url);
  if (cached !== undefined) return cached;
  const result = _isEdu(url, educationalSites);
  if (EDU_CACHE.size > 500) EDU_CACHE.clear();
  EDU_CACHE.set(url, result);
  return result;
}

async function getAnonId() {
  const result = await browser.storage.local.get(['anonId']);
  if (result.anonId) {
    anonId = result.anonId;
  } else {
    anonId = crypto.randomUUID();
    await browser.storage.local.set({ anonId });
  }
  return anonId;
}

async function init() {
  await getAnonId();
  checkExistingTabs();
}

async function sendVisit(data) {
  const { trackingEnabled } = await browser.storage.local.get('trackingEnabled');
  if (!trackingEnabled) return;
  const id = await getAnonId();
  const visitData = { ...data, anon_id: id };
  try {
    await fetch(`${API_BASE}/api/visits`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(visitData)
    });
  } catch (error) {
    console.error('Ошибка отправки данных:', error);
    const { offlineQueue = [] } = await browser.storage.local.get('offlineQueue');
    if (offlineQueue.length < MAX_OFFLINE_QUEUE) {
      offlineQueue.push(visitData);
      browser.storage.local.set({ offlineQueue });
    }
  }
}

async function checkExistingTabs() {
  if (!browser.tabs) return;
  const tabs = await browser.tabs.query({});
  for (const tab of tabs) {
    if (tab.url && isEducational(tab.url)) {
      activeTabs[tab.id] = {
        url: tab.url, domain: new URL(tab.url).hostname, startTime: Date.now()
      };
      await sendVisit({
        url: tab.url, domain: new URL(tab.url).hostname,
        duration: 0, timestamp: new Date().toISOString(), action: 'page_view'
      });
    }
  }
}

async function finishTabSession(tabId) {
  const session = activeTabs[tabId];
  if (!session) return;
  const duration = Math.floor((Date.now() - session.startTime) / 1000);
  if (duration > 5) {
    await sendVisit({
      url: session.url, domain: session.domain, duration,
      timestamp: new Date(session.startTime).toISOString(), action: 'page_exit'
    });
  }
  delete activeTabs[tabId];
}

if (browser.tabs) {
  browser.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
    if (changeInfo.status === 'complete' && tab.url) {
      finishTabSession(tabId);
      if (isEducational(tab.url)) {
        activeTabs[tabId] = {
          url: tab.url, domain: new URL(tab.url).hostname, startTime: Date.now()
        };
        sendVisit({
          url: tab.url, domain: new URL(tab.url).hostname,
          duration: 0, timestamp: new Date().toISOString(), action: 'page_view'
        });
      }
    }
  });

  browser.tabs.onRemoved.addListener((tabId) => {
    finishTabSession(tabId);
  });
}

if (browser.alarms) {
  browser.alarms.create('offlineSync', { periodInMinutes: 1 });
  browser.alarms.onAlarm.addListener(async (alarm) => {
    if (alarm.name === 'offlineSync') {
      const { offlineQueue = [] } = await browser.storage.local.get('offlineQueue');
      if (!offlineQueue.length) return;
      const newQueue = [];
      for (const item of offlineQueue) {
        try {
          await fetch(`${API_BASE}/api/visits`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(item)
          });
        } catch {
          newQueue.push(item);
        }
      }
      browser.storage.local.set({ offlineQueue: newQueue });
    }
  });
}

browser.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message.type === 'SEND_VISIT') {
    sendVisit(message.data).then(() => sendResponse({ ok: true })).catch(() => sendResponse({ ok: false }));
    return true;
  }
  if (message.type === 'GET_RECOMMENDATION') {
    (async () => {
      const { trackingEnabled } = await browser.storage.local.get('trackingEnabled');
      if (!trackingEnabled) return sendResponse({ recommendations: [] });
      try {
        const response = await fetch(SIMILARITY_URL, {
          method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ text: message.data.text })
        });
        const data = await response.json();
        sendResponse({ recommendations: data });
      } catch {
        sendResponse({ recommendations: [] });
      }
    })();
    return true;
  }
  if (message.type === 'PAGE_TEXT') {
    (async () => {
      const { pageTexts = [] } = await browser.storage.local.get('pageTexts');
      const idx = pageTexts.findIndex(t => t.url === message.data.url);
      if (idx !== -1) pageTexts.splice(idx, 1);
      pageTexts.unshift(message.data);
      if (pageTexts.length > 20) pageTexts.length = 20;
      await browser.storage.local.set({ pageTexts });
    })();
    return true;
  }
  if (message.type === 'TRACKING_TOGGLE') {
    (async () => {
      if (!message.enabled && message.autoClear) {
        await browser.storage.local.remove('pageTexts');
      }
    })();
    return true;
  }
  if (message.type === 'CLEAR_HISTORY') {
    (async () => {
      await browser.storage.local.remove('pageTexts');
      sendResponse({ ok: true });
    })();
    return true;
  }
  if (message.type === 'GET_POPUP_RECOMMENDATIONS') {
    (async () => {
      const { trackingEnabled } = await browser.storage.local.get('trackingEnabled');
      if (!trackingEnabled) return sendResponse({ recommendations: [] });
      const { pageTexts = [] } = await browser.storage.local.get('pageTexts');
      const texts = pageTexts.filter(t => t.text && t.text.length > 50).map(t => t.text);
      if (texts.length < 2) return sendResponse({ recommendations: [] });
      try {
        const response = await fetch(RECOMMEND_URL, {
          method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ texts })
        });
        sendResponse({ recommendations: await response.json() });
      } catch {
        sendResponse({ recommendations: [] });
      }
    })();
    return true;
  }
});

init();