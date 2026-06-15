(function(global) {
  const DEFAULT_SITES = [
    { domain: 'stackoverflow.com', url_pattern: '' },
    { domain: 'github.com', url_pattern: '' },
    { domain: 'checkio.org', url_pattern: '' },
    { domain: 'kaggle.com', url_pattern: '' },
    { domain: 'coursera.org', url_pattern: '' },
    { domain: 'codecademy.com', url_pattern: '' },
    { domain: 'pythontutor.com', url_pattern: '' },
    { domain: 'codewars.com', url_pattern: '' },
    { domain: 'python.org', url_pattern: '' },
    { domain: 'skillbox.ru', url_pattern: '' },
    { domain: 'wikipedia.org', url_pattern: '' },
    { domain: 'stepik.org', url_pattern: '' }
  ];

  function isEducational(url, sites) {
    if (!url || !sites || !sites.length) return false;
    try {
      const urlObj = new URL(url);
      const domain = urlObj.hostname.replace('www.', '');
      return sites.some(site =>
        domain.includes(site.domain) || (site.url_pattern && url.includes(site.url_pattern))
      );
    } catch {
      return false;
    }
  }

  global.__EDU = { DEFAULT_SITES, isEducational };
})(typeof globalThis !== 'undefined' ? globalThis : typeof window !== 'undefined' ? window : self);