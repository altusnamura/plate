/* API client.
 *
 * The single tricky thing here is the base path. Home Assistant Ingress serves
 * this app from a rotating URL like /api/hassio_ingress/8fK.../ — the token
 * changes between sessions, so nothing can be hard-coded, and a leading-slash
 * URL like /api/today would escape the Ingress mount and 404 against HA itself.
 *
 * Relative URLs solve it, but only if the current path ends in a slash. When it
 * doesn't, "api/today" resolves against the parent directory and breaks. So the
 * base is computed from location.pathname with the trailing segment trimmed,
 * which is correct in both cases and also when running bare on :8099.
 */
(function () {
  'use strict';

  const path = window.location.pathname;
  const BASE = path.endsWith('/') ? path : path.replace(/[^/]*$/, '');
  const API = BASE + 'api/';

  class ApiError extends Error {
    constructor(message, status, payload) {
      super(message);
      this.name = 'ApiError';
      this.status = status;
      this.payload = payload;
    }
  }

  async function request(path, options = {}) {
    const url = API + path.replace(/^\/+/, '');
    let response;
    try {
      response = await fetch(url, {
        credentials: 'same-origin',
        headers: options.body ? { 'Content-Type': 'application/json' } : {},
        ...options,
      });
    } catch (err) {
      // Network-level failure: the add-on is restarting, or the phone lost wifi.
      throw new ApiError('Cannot reach PLATE. Is the add-on running?', 0, null);
    }

    const text = await response.text();
    let payload = null;
    if (text) {
      try { payload = JSON.parse(text); } catch (_) { payload = { raw: text }; }
    }

    if (!response.ok) {
      const detail =
        (payload && (payload.detail || payload.error)) ||
        `Request failed (${response.status})`;
      throw new ApiError(
        typeof detail === 'string' ? detail : JSON.stringify(detail),
        response.status,
        payload
      );
    }
    return payload;
  }

  const get = (path, params) => {
    const query = params
      ? '?' + new URLSearchParams(
          Object.entries(params).filter(([, v]) => v !== undefined && v !== null && v !== '')
        )
      : '';
    return request(path + query);
  };

  const post = (path, body) =>
    request(path, { method: 'POST', body: JSON.stringify(body || {}) });

  const put = (path, body) =>
    request(path, { method: 'PUT', body: JSON.stringify(body || {}) });

  window.API = {
    base: BASE,
    ApiError,
    get,
    post,
    put,

    today: (day, force) => get('today', { day, force: force ? 'true' : undefined }),
    refresh: () => post('refresh'),
    health: () => get('health'),

    week: (start) => get('week', { start }),
    regenerate: (start, seed) => post('week/regenerate', { start, seed }),
    pin: (slot, recipe_id, regenerate = true) =>
      post('week/pin', { slot, recipe_id, regenerate }),

    recipes: (filters) => get('recipes', filters),
    recipe: (id) => get('recipes/' + encodeURIComponent(id)),

    log: (body) => post('log', body),
    unlog: (body) => post('log/delete', body),
    logList: (day) => get('log', { day }),

    shopping: (start) => get('shopping', { start }),
    check: (plan_start, store_id, food_id, checked) =>
      post('shopping/check', { plan_start, store_id, food_id, checked }),
    pantry: (food_id, grams) => post('pantry', { food_id, grams }),

    insight: (days) => get('insight', { days }),

    putMetrics: (body) => post('metrics', body),
    metrics: (days) => get('metrics', { days }),
    deleteMetric: (day, key) => post('metrics/delete', { day, key }),

    settings: () => get('settings'),
    saveSettings: (body) => put('settings', body),
    discover: () => get('entities/discover'),
    reloadData: () => post('data/reload'),
    importMealie: (limit, write) => post('import/mealie', { limit, write }),
    importUsda: (body) => post('import/usda', body || {}),
  };
})();
