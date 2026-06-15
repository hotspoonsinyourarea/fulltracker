document.addEventListener('DOMContentLoaded', () => {
  const statusDiv = document.getElementById('status');
  const toggleBtn = document.getElementById('toggleBtn');
  const recDiv = document.getElementById('recommendations');
  const recList = document.getElementById('rec-list');
  const clearBtn = document.getElementById('clearBtn');
  const autoClearCheck = document.getElementById('autoClearCheck');

  browser.storage.local.get(['trackingEnabled', 'clearOnDisable']).then((result) => {
    updateUI(result.trackingEnabled === true);
    autoClearCheck.checked = result.clearOnDisable !== false;
  });

  toggleBtn.addEventListener('click', () => {
    browser.storage.local.get(['trackingEnabled', 'clearOnDisable']).then((result) => {
      const newState = !result.trackingEnabled;
      browser.storage.local.set({ trackingEnabled: newState }).then(() => {
        updateUI(newState);
        browser.runtime.sendMessage({ type: 'TRACKING_TOGGLE', enabled: newState, autoClear: result.clearOnDisable !== false });
      });
    });
  });

  clearBtn.addEventListener('click', () => {
    browser.runtime.sendMessage({ type: 'CLEAR_HISTORY' }, (response) => {
      if (response && response.ok) {
        clearBtn.textContent = '✓ Очищено';
        setTimeout(() => { clearBtn.textContent = '🗑 Очистить историю'; }, 2000);
      }
    });
  });

  autoClearCheck.addEventListener('change', () => {
    browser.storage.local.set({ clearOnDisable: autoClearCheck.checked });
  });

  browser.runtime.sendMessage({ type: 'GET_POPUP_RECOMMENDATIONS' }, (response) => {
    if (!response || !response.recommendations || response.recommendations.length === 0) {
      recDiv.style.display = 'none';
      return;
    }
    recList.innerHTML = '';
    response.recommendations.forEach(rec => {
      const link = document.createElement('a');
      link.href = rec.link;
      link.target = '_blank';
      link.textContent = rec.title;
      recList.appendChild(link);
    });
    recDiv.style.display = 'block';
  });

  function updateUI(enabled) {
    if (enabled) {
      statusDiv.textContent = 'Отслеживание активно';
      statusDiv.className = 'status on';
      toggleBtn.textContent = 'Отключить отслеживание';
    } else {
      statusDiv.textContent = 'Отслеживание отключено';
      statusDiv.className = 'status off';
      toggleBtn.textContent = 'Включить отслеживание';
    }
  }
});
