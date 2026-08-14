/* Small DOM and formatting helpers, plus the two SVG charts.
 *
 * No framework on purpose. An add-on that ships a build step is an add-on that
 * needs a toolchain to patch, and the whole UI here is a few hundred lines of
 * DOM. Charts are hand-drawn SVG for the same reason: a charting library would
 * be the single largest thing in the image, and would need CDN loading that a
 * strict-CSP Ingress frame may block anyway.
 */
(function () {
  'use strict';

  /* ---------- DOM ---------- */

  const el = (tag, attrs = {}, children = []) => {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs)) {
      if (value === null || value === undefined || value === false) continue;
      if (key === 'class') node.className = value;
      else if (key === 'text') node.textContent = value;
      else if (key === 'html') node.innerHTML = value;
      else if (key.startsWith('on') && typeof value === 'function') {
        node.addEventListener(key.slice(2).toLowerCase(), value);
      } else if (value === true) node.setAttribute(key, '');
      else node.setAttribute(key, value);
    }
    for (const child of [].concat(children)) {
      if (child === null || child === undefined || child === false) continue;
      node.appendChild(typeof child === 'string' ? document.createTextNode(child) : child);
    }
    return node;
  };

  const svgEl = (tag, attrs = {}, children = []) => {
    const node = document.createElementNS('http://www.w3.org/2000/svg', tag);
    for (const [key, value] of Object.entries(attrs)) {
      if (value === null || value === undefined) continue;
      node.setAttribute(key, value);
    }
    for (const child of [].concat(children)) {
      if (child) node.appendChild(child);
    }
    return node;
  };

  const clear = (node) => { while (node.firstChild) node.removeChild(node.firstChild); return node; };

  /* ---------- formatting ---------- */

  const num = (v, digits = 0) =>
    v === null || v === undefined || Number.isNaN(v)
      ? '–'
      : Number(v).toLocaleString(undefined, {
          minimumFractionDigits: digits,
          maximumFractionDigits: digits,
        });

  const signed = (v, digits = 1) =>
    v === null || v === undefined ? '–' : (v > 0 ? '+' : '') + num(v, digits);

  const money = (v) => (v === null || v === undefined ? '' : '$' + Number(v).toFixed(2));

  const dayName = (iso, long = false) => {
    const d = new Date(iso + 'T12:00:00');
    return d.toLocaleDateString(undefined, long
      ? { weekday: 'long', month: 'short', day: 'numeric' }
      : { weekday: 'short' });
  };

  const isToday = (iso) => iso === new Date().toLocaleDateString('en-CA');

  const relativeDate = (iso) => {
    if (!iso) return '';
    const days = Math.round((new Date(iso + 'T12:00:00') - new Date()) / 86400000);
    if (days <= 0) return 'today';
    if (days < 60) return `in ${days} days`;
    if (days < 365) return `in ${Math.round(days / 30)} months`;
    return `in ${(days / 365).toFixed(1)} years`;
  };

  /* ---------- feedback ---------- */

  let toastTimer = null;
  function toast(message, ms = 2600) {
    const node = document.getElementById('toast');
    node.textContent = message;
    node.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { node.hidden = true; }, ms);
  }

  function banner(content, isError = false) {
    const node = document.getElementById('banner');
    if (!content || (Array.isArray(content) && !content.length)) {
      node.hidden = true;
      return;
    }
    clear(node);
    node.className = 'banner' + (isError ? ' is-error' : '');
    if (Array.isArray(content)) {
      if (content.length === 1) node.appendChild(el('div', { text: content[0] }));
      else node.appendChild(el('ul', {}, content.map((c) => el('li', { text: c }))));
    } else {
      node.appendChild(el('div', { text: String(content) }));
    }
    node.hidden = false;
  }

  /* ---------- bottom sheet ---------- */

  function sheet(title, bodyNode) {
    const root = document.getElementById('sheet');
    document.getElementById('sheet-title').textContent = title;
    const body = clear(document.getElementById('sheet-body'));
    body.appendChild(bodyNode);
    root.hidden = false;
    document.body.style.overflow = 'hidden';
  }

  function closeSheet() {
    document.getElementById('sheet').hidden = true;
    document.body.style.overflow = '';
  }

  document.addEventListener('click', (e) => {
    if (e.target.closest('[data-close]')) closeSheet();
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') closeSheet();
  });

  /* ---------- pieces ---------- */

  function ring(eaten, target) {
    const radius = 54;
    const circumference = 2 * Math.PI * radius;
    const fraction = target > 0 ? Math.min(eaten / target, 1) : 0;
    const over = target > 0 && eaten > target * 1.02;
    const remaining = Math.max(0, Math.round(target - eaten));

    const wrap = el('div', { class: 'ring' });
    wrap.appendChild(
      svgEl('svg', { viewBox: '0 0 128 128', 'aria-hidden': 'true' }, [
        svgEl('circle', { class: 'ring-track', cx: 64, cy: 64, r: radius }),
        svgEl('circle', {
          class: 'ring-fill' + (over ? ' is-over' : ''),
          cx: 64, cy: 64, r: radius,
          'stroke-dasharray': circumference,
          'stroke-dashoffset': circumference * (1 - fraction),
        }),
      ])
    );
    wrap.appendChild(
      el('div', { class: 'ring-label' }, [
        el('div', { class: 'ring-big', text: num(over ? eaten - target : remaining) }),
        el('div', { class: 'ring-sub', text: over ? 'kcal over' : 'kcal left' }),
      ])
    );
    return wrap;
  }

  function bar(name, value, goal, opts = {}) {
    const { unit = '', ceiling = null, floor = null, digits = 0 } = opts;
    const reference = ceiling || goal || floor || 1;
    const pct = Math.min(100, (value / reference) * 100);

    let state = '';
    if (ceiling !== null && value > ceiling) state = ' is-bad';
    else if (ceiling !== null && value > ceiling * 0.9) state = ' is-warn';
    else if (goal && value >= goal * 0.95) state = ' is-good';

    const goalText = ceiling !== null ? `≤ ${num(ceiling, digits)}` : num(goal, digits);
    return el('div', { class: 'bar-row' }, [
      el('span', { class: 'bar-name', text: name }),
      el('span', {
        class: 'bar-val mono',
        text: `${num(value, digits)} / ${goalText} ${unit}`.trim(),
      }),
      el('div', { class: 'bar-track' }, [
        el('div', { class: 'bar-fill' + state, style: `width:${pct}%` }),
      ]),
    ]);
  }

  function chip(text, kind) {
    return el('span', { class: 'chip' + (kind ? ' is-' + kind : ''), text });
  }

  function stat(value, name, note) {
    return el('div', { class: 'stat' }, [
      el('div', { class: 'stat-val', text: value }),
      el('div', { class: 'stat-name', text: name }),
      note ? el('div', { class: 'stat-note', text: note }) : null,
    ]);
  }

  function empty(message, sub) {
    return el('div', { class: 'empty' }, [
      el('div', { text: message }),
      sub ? el('div', { class: 'small', text: sub }) : null,
    ]);
  }

  /* ---------- charts ---------- */

  /* Weight: raw readings as faint dots, the EWMA trend as a line, goal weight as
   * a dashed rule. The dots matter — showing only the smoothed line invites the
   * reader to trust it more than the underlying data deserves. */
  function weightChart(series, goalWeight) {
    const W = 320, H = 150, padL = 30, padR = 8, padT = 10, padB = 18;
    if (!series || series.length < 2) return empty('Not enough weight data yet.');

    const values = series.map((p) => p.trend).concat(
      series.filter((p) => p.raw != null).map((p) => p.raw)
    );
    if (goalWeight) values.push(goalWeight);
    let lo = Math.min(...values), hi = Math.max(...values);
    const pad = Math.max(1.5, (hi - lo) * 0.12);
    lo -= pad; hi += pad;

    const x = (i) => padL + (i / (series.length - 1)) * (W - padL - padR);
    const y = (v) => padT + (1 - (v - lo) / (hi - lo)) * (H - padT - padB);

    const kids = [];
    for (let t = 0; t <= 3; t++) {
      const v = lo + (t / 3) * (hi - lo);
      kids.push(svgEl('line', { class: 'chart-grid', x1: padL, x2: W - padR, y1: y(v), y2: y(v) }));
      kids.push(svgEl('text', { class: 'chart-axis', x: 2, y: y(v) + 3, text: '' },
        [document.createTextNode(v.toFixed(0))]));
    }
    if (goalWeight && goalWeight > lo && goalWeight < hi) {
      kids.push(svgEl('line', {
        class: 'chart-goal', x1: padL, x2: W - padR, y1: y(goalWeight), y2: y(goalWeight),
      }));
    }
    series.forEach((p, i) => {
      if (p.raw != null) kids.push(svgEl('circle', { class: 'chart-raw', cx: x(i), cy: y(p.raw), r: 1.7 }));
    });
    kids.push(svgEl('path', {
      class: 'chart-trend',
      d: series.map((p, i) => `${i ? 'L' : 'M'}${x(i).toFixed(1)},${y(p.trend).toFixed(1)}`).join(' '),
    }));

    const first = series[0].day, last = series[series.length - 1].day;
    kids.push(svgEl('text', { class: 'chart-axis', x: padL, y: H - 4 },
      [document.createTextNode(dayName(first) + ' ' + first.slice(5))]));
    kids.push(svgEl('text', { class: 'chart-axis', x: W - padR, y: H - 4, 'text-anchor': 'end' },
      [document.createTextNode(last.slice(5))]));

    return svgEl('svg', {
      class: 'chart', viewBox: `0 0 ${W} ${H}`, role: 'img',
      'aria-label': 'Weight trend over time',
    }, kids);
  }

  /* Intake vs target: bars for what was eaten, a dashed rule per day for the
   * target that was live on that day. */
  function intakeChart(intake, targets) {
    const W = 320, H = 130, padL = 30, padR = 8, padT = 10, padB = 18;
    if (!intake || intake.length < 2) {
      return empty('Log a few meals to see this.', 'Intake history builds up as you log.');
    }
    const byDay = new Map(targets.map((t) => [t.day, t.kcal]));
    const points = intake.slice(-42);
    const maxV = Math.max(
      ...points.map((p) => p.kcal),
      ...points.map((p) => byDay.get(p.day) || 0),
      1200
    ) * 1.12;

    const bw = (W - padL - padR) / points.length;
    const y = (v) => padT + (1 - v / maxV) * (H - padT - padB);
    const kids = [];

    for (let t = 0; t <= 2; t++) {
      const v = (t / 2) * maxV;
      kids.push(svgEl('line', { class: 'chart-grid', x1: padL, x2: W - padR, y1: y(v), y2: y(v) }));
      kids.push(svgEl('text', { class: 'chart-axis', x: 2, y: y(v) + 3 },
        [document.createTextNode(v.toFixed(0))]));
    }

    points.forEach((p, i) => {
      const target = byDay.get(p.day);
      const over = target && p.kcal > target * 1.05;
      const x = padL + i * bw;
      kids.push(svgEl('rect', {
        class: 'chart-bar' + (over ? ' is-over' : ''),
        x: x + bw * 0.15, y: y(p.kcal),
        width: Math.max(1.5, bw * 0.7),
        height: Math.max(0, H - padB - y(p.kcal)),
        rx: 1.5,
      }));
      if (target) {
        kids.push(svgEl('line', {
          class: 'chart-target',
          x1: x + bw * 0.05, x2: x + bw * 0.95,
          y1: y(target), y2: y(target),
        }));
      }
    });

    return svgEl('svg', {
      class: 'chart', viewBox: `0 0 ${W} ${H}`, role: 'img',
      'aria-label': 'Calories eaten against target',
    }, kids);
  }

  function legend(items) {
    return el('div', { class: 'legend' }, items.map(([label, cls]) =>
      el('span', { class: 'legend-item' }, [
        el('span', { class: 'legend-swatch ' + (cls || '') }),
        el('span', { text: label }),
      ])
    ));
  }

  window.UI = {
    el, svgEl, clear,
    num, signed, money, dayName, isToday, relativeDate,
    toast, banner, sheet, closeSheet,
    ring, bar, chip, stat, empty,
    weightChart, intakeChart, legend,
  };
})();
