/* Views and routing.
 *
 * Five screens, one at a time, no client-side router library — the tab bar sets
 * a name and re-renders. State is deliberately thin: each view fetches what it
 * needs on entry and the server's snapshot is the only source of truth, so there
 * is no client cache to go stale against the sensors in Home Assistant.
 */
(function () {
  'use strict';

  const { el, clear, num, signed, money, dayName, isToday, relativeDate } = UI;

  const state = { view: 'today', snapshot: null, weekStart: null, busy: false };

  const viewEl = () => document.getElementById('view');
  const setTitle = (title, sub) => {
    document.getElementById('view-title').textContent = title;
    document.getElementById('view-sub').textContent = sub || '';
  };

  /* ---------------------------------------------------------------- today */

  async function renderToday() {
    setTitle('Today', new Date().toLocaleDateString(undefined,
      { weekday: 'long', month: 'long', day: 'numeric' }));
    const snap = await API.today();
    state.snapshot = snap;

    const notices = [].concat(snap.needs_setup || []);
    if ((snap.bp.advice || []).length && snap.bp.severity >= 2) notices.push(snap.bp.advice[0]);
    UI.banner(notices);

    const root = el('div');
    const t = snap.targets || {};
    const eaten = snap.eaten || {};
    const energy = snap.energy || {};

    /* --- ring + macro bars --- */
    const kcalTarget = energy.target_kcal || 0;
    root.appendChild(el('div', { class: 'card ring-card' }, [
      UI.ring(eaten.kcal || 0, kcalTarget),
      el('div', { class: 'grow' }, [
        el('div', { class: 'row-between' }, [
          el('div', {}, [
            el('div', { class: 'mono', text: `${num(eaten.kcal)} / ${num(kcalTarget)} kcal` }),
            el('div', { class: 'tiny faint', text: energyCaption(energy) }),
          ]),
        ]),
        el('div', { class: 'bars', style: 'margin-top:10px' }, [
          UI.bar('Protein', eaten.protein_g || 0, get(t, 'protein_g', 'goal'), { unit: 'g' }),
          UI.bar('Fibre', eaten.fiber_g || 0, get(t, 'fiber_g', 'goal'), { unit: 'g' }),
          UI.bar('Sodium', eaten.sodium_mg || 0, null,
            { unit: 'mg', ceiling: get(t, 'sodium_mg', 'ceiling') }),
          UI.bar('Potassium', eaten.potassium_mg || 0, get(t, 'potassium_mg', 'goal'), { unit: 'mg' }),
        ]),
      ]),
    ]));

    /* --- activity adjustment, when it happened --- */
    if (energy.activity_adjustment > 0) {
      root.appendChild(el('div', { class: 'card card-tight' }, [
        el('div', { class: 'row' }, [
          UI.chip(`+${num(energy.activity_adjustment)} kcal`, 'accent'),
          el('span', {
            class: 'small muted grow',
            text: 'Added back for today being more active than usual.',
          }),
        ]),
      ]));
    }

    /* --- next meal --- */
    const next = (snap.plan || {}).next_meal;
    if (next) {
      root.appendChild(el('div', { class: 'card' }, [
        el('div', { class: 'card-head' }, [
          el('h2', { text: 'Up next — ' + next.meal }),
          el('span', { class: 'hint', text: next.active_min ? `${next.active_min} min` : 'no cooking' }),
        ]),
        el('div', { class: 'row-between' }, [
          el('div', { class: 'grow' }, [
            el('div', { style: 'font-weight:600', text: next.title }),
            el('div', { class: 'tiny faint', text:
              `${num(next.kcal)} kcal · ${num(next.protein_g)} g protein` +
              (next.leftover_from ? ` · leftovers from ${dayName(next.leftover_from)}` : '') }),
          ]),
        ]),
        el('div', { class: 'btn-row', style: 'margin-top:10px' }, [
          el('button', {
            class: 'btn btn-primary grow',
            text: 'Ate it',
            onclick: () => logSlot(next.slot),
          }),
          el('button', {
            class: 'btn',
            text: 'Recipe',
            onclick: () => showRecipe(next.recipe_id),
          }),
        ]),
      ]));
    }

    /* --- the day's meals --- */
    const today = (snap.plan || {}).today;
    if (today && today.meals) {
      const logged = new Set(snap.plan.logged_slots || []);
      root.appendChild(el('div', { class: 'card' }, [
        el('div', { class: 'card-head' }, [
          el('h2', { text: "Today's meals" }),
          el('span', { class: 'hint', text: `DASH ${num(today.dash_score)}/100` }),
        ]),
        el('div', {}, today.meals.map((m) => mealRow(m, logged))),
      ]));
    }

    /* --- trend --- */
    const trend = snap.trend || {};
    root.appendChild(el('div', { class: 'card' }, [
      el('div', { class: 'card-head' }, [
        el('h2', { text: 'Progress' }),
        el('span', { class: 'hint', text: trend.readings
          ? `${trend.readings} weigh-ins` : 'no data yet' }),
      ]),
      el('div', { class: 'stats' }, [
        UI.stat(num(trend.trend_lb, 1) + ' lb', 'Trend weight',
          trend.raw_lb ? `last weigh-in ${num(trend.raw_lb, 1)}` : null),
        UI.stat(signed(trend.rate_lb_per_week, 2), 'lb / week',
          `target ${signed(snap.profile.target_rate_lb_per_week, 1)}`),
        UI.stat(num(energy.tdee), 'TDEE', energy.source),
        trend.goal_date
          ? UI.stat(relativeDate(trend.goal_date), `to ${num(snap.profile.goal_weight_lb)} lb`,
              trend.goal_date)
          : UI.stat('–', 'Goal ETA', 'not on track'),
      ]),
      el('button', {
        class: 'btn btn-block' + (trend.readings ? '' : ' btn-primary'),
        style: 'margin-top:12px',
        text: trend.readings ? 'Add measurement' : 'Add your first weigh-in',
        onclick: () => showMetricsSheet(snap),
      }),
    ]));

    /* --- blood pressure --- */
    const bp = snap.bp || {};
    if (!bp.systolic) {
      root.appendChild(el('div', { class: 'card card-tight' }, [
        el('div', { class: 'row-between' }, [
          el('span', { class: 'small muted grow', text:
            'No blood pressure readings. Add a few and the sodium, saturated fat ' +
            'and potassium targets adjust to match.' }),
          el('button', { class: 'btn btn-sm', text: 'Add',
            onclick: () => showMetricsSheet(snap) }),
        ]),
      ]));
    } else {
      root.appendChild(el('div', { class: 'card' }, [
        el('div', { class: 'card-head' }, [
          el('h2', { text: 'Blood pressure' }),
          el('span', { class: 'hint', text: `${bp.readings} readings, 14 days` }),
        ]),
        el('div', { class: 'row wrap' }, [
          el('div', { class: 'mono', style: 'font-size:1.5rem;font-weight:700',
            text: `${num(bp.systolic)}/${num(bp.diastolic)}` }),
          UI.chip(bp.category_label, bp.severity >= 2 ? 'bad' : bp.severity === 1 ? 'warn' : 'good'),
        ]),
        bp.advice && bp.advice.length
          ? el('ul', { class: 'note-list', style: 'margin-top:10px' },
              bp.advice.map((a) => el('li', { text: a })))
          : null,
      ]));
    }

    /* --- why these numbers --- */
    const notes = (energy.notes || []).concat((snap.target_meta || {}).notes || []);
    root.appendChild(el('details', { class: 'card' }, [
      el('summary', { text: 'How these targets were worked out' }),
      el('div', { class: 'stack' }, [
        el('p', { class: 'small muted', text: calibrationExplainer(energy) }),
        el('p', { class: 'small muted', text:
          `Protein target uses ${(snap.target_meta || {}).protein_basis || 'body mass'}.` }),
        notes.length
          ? el('ul', { class: 'note-list' }, notes.map((n) => el('li', { text: n })))
          : null,
        el('div', { class: 'disclaimer' },
          ((snap.target_meta || {}).disclaimers || []).map((d) => el('p', { text: d }))),
      ]),
    ]));

    return root;
  }

  function energyCaption(energy) {
    const bits = [`TDEE ${num(energy.tdee)}`];
    if (energy.deficit_kcal) bits.push(`${signed(energy.deficit_kcal, 0)} kcal/day`);
    if (energy.planned_rate) bits.push(`${signed(energy.planned_rate, 1)} lb/wk`);
    return bits.join(' · ');
  }

  function calibrationExplainer(energy) {
    if (energy.source === 'calibrated' && energy.observed_tdee) {
      const bias = energy.tracker_bias_pct;
      return (
        `Your tracker reports ${num(energy.prior_tdee)} kcal/day. Comparing what you logged ` +
        `against how your weight trend actually moved implies ${num(energy.observed_tdee)}. ` +
        `PLATE blends them at ${num(energy.confidence * 100)}% confidence to get ` +
        `${num(energy.tdee)}` +
        (bias ? `, which means the tracker reads about ${num(Math.abs(bias), 0)}% ` +
          (bias > 0 ? 'low' : 'high') + ' for you.' : '.')
      );
    }
    if (energy.source === 'tracker') {
      return (
        `Using your tracker's ${num(energy.tdee)} kcal/day as-is. Once there are about ` +
        `four weeks of weight readings and food logs, PLATE will correct it against your ` +
        `actual rate of change — trackers are commonly 10-25% out for an individual.`
      );
    }
    return (
      `No tracker data, so this is the ${energy.resting_formula} formula for resting rate ` +
      `(${num(energy.resting_kcal)} kcal) scaled by estimated activity.`
    );
  }

  function mealRow(meal, logged) {
    const done = logged.has(meal.slot);
    return el('button', {
      class: 'meal' + (done ? ' is-logged' : ''),
      onclick: () => showMeal(meal, done),
    }, [
      el('span', { class: 'meal-slot', text: meal.meal }),
      el('span', { class: 'meal-main' }, [
        el('span', { class: 'meal-title' }, [
          meal.vegetarian ? el('span', { class: 'veg-dot' }) : null,
          el('span', { text: (meal.recipe || {}).title || '—' }),
        ]),
        el('span', { class: 'meal-meta', text:
          `${meal.servings}× · ${num(meal.protein_g)} g protein` +
          (meal.leftover_from ? ` · from ${dayName(meal.leftover_from)}` : '') +
          (meal.active_min ? ` · ${meal.active_min} min` : '') }),
      ]),
      el('span', { class: 'meal-kcal mono', text: num(meal.kcal) }),
    ]);
  }

  /* ----------------------------------------------------------------- week */

  async function renderWeek() {
    const plan = await API.week(state.weekStart);
    state.weekStart = plan.start;
    const d = plan.diagnostics || {};
    setTitle('Week', `${d.vegetarian_lunches} veg lunches · ${d.distinct_recipes} recipes · ${d.total_active_min} min cooking`);
    UI.banner(d.issues && d.issues.length ? d.issues.slice(0, 3) : null);

    const root = el('div');
    root.appendChild(el('div', { class: 'btn-row', style: 'margin-bottom:12px' }, [
      el('button', { class: 'btn btn-sm', text: '‹ Previous',
        onclick: () => shiftWeek(-7) }),
      el('button', { class: 'btn btn-sm grow', text: 'Shuffle week',
        onclick: () => regenerate() }),
      el('button', { class: 'btn btn-sm', text: 'Next ›',
        onclick: () => shiftWeek(7) }),
    ]));

    const logged = new Set((state.snapshot && state.snapshot.plan.logged_slots) || []);
    for (const day of plan.days) {
      const off = day.kcal_target ? (day.kcal - day.kcal_target) / day.kcal_target : 0;
      root.appendChild(el('div', {
        class: 'card day-card' + (isToday(day.day) ? ' is-today' : ''),
      }, [
        el('div', { class: 'day-head' }, [
          el('span', { class: 'day-name', text: dayName(day.day, true) }),
          el('span', { class: 'day-stats mono', text:
            `${num(day.kcal)}/${num(day.kcal_target)} · P${num(day.nutrition.protein_g)}` +
            ` · Na${num(day.nutrition.sodium_mg)}` +
            (day.active_min ? ` · ${day.active_min}m` : '') }),
        ]),
        Math.abs(off) > 0.08
          ? el('div', { class: 'tiny', style: 'color:var(--warn);margin:4px 0',
              text: `${signed(off * 100, 0)}% off the calorie target` })
          : null,
        el('div', {}, day.meals.map((m) => mealRow(m, logged))),
      ]));
    }

    if (plan.batches && plan.batches.length) {
      root.appendChild(el('div', { class: 'card' }, [
        el('div', { class: 'card-head' }, [
          el('h2', { text: 'Cook once, eat twice' }),
          el('span', { class: 'hint', text: `${plan.batches.length} batches` }),
        ]),
        el('ul', { class: 'note-list' }, plan.batches.map((b) =>
          el('li', { text:
            `${dayName(b.cook_day, true)}: make ${b.servings_cooked} servings — ` +
            `covers ${b.feeds_slots.length} meals` })
        )),
      ]));
    }
    return root;
  }

  async function shiftWeek(days) {
    const base = new Date((state.weekStart || new Date().toLocaleDateString('en-CA')) + 'T12:00:00');
    base.setDate(base.getDate() + days);
    state.weekStart = base.toLocaleDateString('en-CA');
    await render();
  }

  async function regenerate() {
    UI.toast('Planning…');
    try {
      await API.regenerate(state.weekStart);
      await render();
      UI.toast('New week planned');
    } catch (err) {
      UI.toast(err.message);
    }
  }

  /* ----------------------------------------------------------------- shop */

  async function renderShop() {
    const list = await API.shopping(state.weekStart);
    const total = list.total_estimate;
    setTitle('Shopping', total ? `about ${money(total)} · week of ${list.plan_start}` : list.plan_start);
    UI.banner(null);

    const root = el('div');
    if (!list.stores.length) {
      root.appendChild(UI.empty('Nothing to buy.', 'Generate a plan first.'));
      return root;
    }

    for (const store of list.stores) {
      const card = el('div', { class: 'card' });
      card.appendChild(el('div', { class: 'store-head' }, [
        el('span', { class: 'store-name', text: store.store_name }),
        el('span', { class: 'store-total mono',
          text: store.subtotal != null ? money(store.subtotal) : '' }),
      ]));
      card.appendChild(el('div', { class: 'tiny faint', text:
        `${store.item_count} items` +
        (store.unpriced_lines ? ` · ${store.unpriced_lines} without a price` : '') +
        (store.delivers ? '' : ' · no delivery') }));

      for (const aisle of store.aisles) {
        card.appendChild(el('div', { class: 'aisle-label', text: aisle.label }));
        for (const line of aisle.lines) {
          card.appendChild(shopLine(list.plan_start, store.store_id, line));
        }
      }
      root.appendChild(card);
    }

    if (list.unmatched && list.unmatched.length) {
      root.appendChild(el('div', { class: 'card' }, [
        el('div', { class: 'card-head' }, [el('h2', { text: 'No store mapping' })]),
        el('div', { class: 'tiny faint', style: 'margin-bottom:6px',
          text: 'Add these to a stores YAML file to get prices and an aisle.' }),
        el('div', {}, list.unmatched.map((l) =>
          shopLine(list.plan_start, 'none', l))),
      ]));
    }

    root.appendChild(el('div', { class: 'card' }, [
      el('ul', { class: 'note-list' }, (list.notes || []).map((n) => el('li', { text: n }))),
    ]));
    return root;
  }

  function shopLine(planStart, storeId, line) {
    const row = el('div', { class: 'line' + (line.checked ? ' is-done' : '') });
    const box = el('button', {
      class: 'line-check',
      'aria-pressed': line.checked ? 'true' : 'false',
      'aria-label': 'Mark ' + line.name,
      onclick: async () => {
        const next = box.getAttribute('aria-pressed') !== 'true';
        box.setAttribute('aria-pressed', String(next));
        row.classList.toggle('is-done', next);
        try {
          await API.check(planStart, storeId, line.food_id, next);
        } catch (err) {
          box.setAttribute('aria-pressed', String(!next));
          row.classList.toggle('is-done', !next);
          UI.toast(err.message);
        }
      },
    }, [el('span', { class: 'line-box' })]);

    row.appendChild(box);
    row.appendChild(el('div', { class: 'line-body' }, [
      el('div', { class: 'line-name', text: line.name }),
      el('div', { class: 'line-meta', text:
        [line.quantity_text, line.product_name, line.note].filter(Boolean).join(' · ') }),
    ]));
    if (line.est_cost != null) {
      row.appendChild(el('span', { class: 'line-cost mono', text: money(line.est_cost) }));
    }
    const href = (line.links || {}).order || (line.links || {}).store;
    if (href) {
      row.appendChild(el('a', {
        class: 'line-link', href, target: '_blank', rel: 'noopener noreferrer',
        'aria-label': 'Search for ' + line.name,
        html: '<svg viewBox="0 0 24 24"><path d="M14 3v2h3.6l-9.8 9.8 1.4 1.4L19 6.4V10h2V3zM5 5h5V3H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-5h-2v5H5z"/></svg>',
      }));
    }
    return row;
  }

  /* -------------------------------------------------------------- insight */

  async function renderInsight() {
    const data = await API.insight(120);
    const snap = data.snapshot;
    const energy = snap.energy || {};
    setTitle('Insight', 'How the numbers were derived');
    UI.banner(data.library_warnings && data.library_warnings.length
      ? data.library_warnings.slice(0, 2) : null);

    const root = el('div');

    root.appendChild(el('div', { class: 'card' }, [
      el('div', { class: 'card-head' }, [
        el('h2', { text: 'Weight trend' }),
        el('span', { class: 'hint', text: `${snap.trend.readings} readings` }),
      ]),
      UI.weightChart(data.series.weight, snap.profile.goal_weight_lb),
      UI.legend([['Trend (EWMA)', ''], ['Scale readings', 'dot'], ['Goal weight', 'goal']]),
      el('p', { class: 'small muted', style: 'margin-top:10px', text:
        'Daily weight swings of two or three pounds are water and gut contents. ' +
        'Everything here works off the smoothed trend, never a single reading.' }),
    ]));

    root.appendChild(el('div', { class: 'card' }, [
      el('div', { class: 'card-head' }, [el('h2', { text: 'Energy balance' })]),
      el('div', { class: 'stats' }, [
        UI.stat(num(energy.prior_tdee), 'Tracker says', 'kcal/day'),
        UI.stat(energy.observed_tdee ? num(energy.observed_tdee) : '–', 'Your body says',
          'from weight change'),
        UI.stat(num(energy.tdee), 'PLATE uses', `${num(energy.confidence * 100)}% confidence`),
        UI.stat(energy.tracker_bias_pct != null ? signed(energy.tracker_bias_pct, 0) + '%' : '–',
          'Tracker error', energy.logged_days + ' logged days'),
      ]),
      el('p', { class: 'small muted', style: 'margin-top:12px', text: calibrationExplainer(energy) }),
    ]));

    root.appendChild(el('div', { class: 'card' }, [
      el('div', { class: 'card-head' }, [
        el('h2', { text: 'Intake vs target' }),
        el('span', { class: 'hint', text: snap.adherence != null
          ? `${num(snap.adherence * 100)}% on target` : 'no data' }),
      ]),
      UI.intakeChart(data.series.intake, data.series.target),
      UI.legend([['Eaten', ''], ['Target that day', 'goal']]),
    ]));

    if ((data.series.bp || []).length) {
      const bp = snap.bp;
      root.appendChild(el('div', { class: 'card' }, [
        el('div', { class: 'card-head' }, [
          el('h2', { text: 'Blood pressure and sodium' }),
          UI.chip(bp.category_label, bp.severity >= 2 ? 'bad' : bp.severity === 1 ? 'warn' : 'good'),
        ]),
        el('div', { class: 'stats' }, [
          UI.stat(`${num(bp.systolic)}/${num(bp.diastolic)}`, '14-day average', 'mmHg'),
          UI.stat(bp.trend_systolic != null ? signed(bp.trend_systolic, 1) : '–',
            'Systolic trend', 'mmHg/week'),
          UI.stat(num(get(snap.targets, 'sodium_mg', 'ceiling')), 'Sodium limit',
            bp.severity >= 1 ? 'AHA, elevated BP' : 'DGA general'),
        ]),
        el('p', { class: 'small muted', style: 'margin-top:12px', text:
          (snap.target_meta.rationale || {}).sodium_mg || '' }),
      ]));
    }

    root.appendChild(el('div', { class: 'card' }, [
      el('div', { class: 'card-head' }, [el('h2', { text: 'Data coverage' })]),
      el('div', { class: 'chips' }, Object.entries(snap.coverage || {}).map(([k, v]) =>
        UI.chip(`${k.replace(/_/g, ' ')}: ${v} days`, v > 14 ? 'good' : v > 0 ? 'warn' : null))),
    ]));

    root.appendChild(el('div', { class: 'card disclaimer' },
      (snap.target_meta.disclaimers || []).map((d) => el('p', { text: d }))));
    return root;
  }

  /* ------------------------------------------------------------- settings */

  async function renderSettings() {
    setTitle('Settings', 'Profile, entities and data');
    UI.banner(null);
    const [settings, discovered] = await Promise.all([
      API.settings(),
      API.discover().catch(() => ({ candidates: {}, current: {} })),
    ]);
    const cfg = settings.config;
    const standalone = settings.standalone;
    const root = el('div');
    const inputs = {};

    /* --- measurements: the standalone equivalent of connecting a tracker --- */
    if (standalone) {
      root.appendChild(el('div', { class: 'card' }, [
        el('div', { class: 'card-head' }, [
          el('h2', { text: 'Measurements' }),
          el('span', { class: 'hint', text: 'running standalone' }),
        ]),
        el('p', { class: 'small muted', text:
          'No Home Assistant connected, so weight and blood pressure are entered by ' +
          'hand. That is a fully supported way to run this — the calibration works ' +
          'the same, it just needs you to weigh in every few days.' }),
        el('div', { class: 'btn-row' }, [
          el('button', { class: 'btn btn-primary grow', text: 'Add measurement',
            onclick: () => showMetricsSheet(null) }),
          el('button', { class: 'btn', text: 'History',
            onclick: () => showMetricsHistory() }),
        ]),
      ]));
    }

    /* --- entities --- */
    const entityCard = standalone
      ? el('details', { class: 'card' }, [
          el('summary', { text: 'Home Assistant entities (not connected)' }),
          el('p', { class: 'small muted', text:
            'Nothing to pick while running standalone. Connect Home Assistant and ' +
            'these fill in automatically — your hand-entered measurements are kept ' +
            'and are never overwritten by a sync.' }),
        ])
      : el('div', { class: 'card' }, [
          el('div', { class: 'card-head' }, [
            el('h2', { text: 'Home Assistant entities' }),
            el('span', { class: 'hint', text: 'auto-detected' }),
          ]),
          el('p', { class: 'small muted', text:
            'PLATE suggests entities by name and unit. Confirm each one — calibrating ' +
            'against the wrong sensor produces confident nonsense.' }),
        ]);
    for (const [key, label] of Object.entries({
      weight: 'Weight', body_fat: 'Body fat %', calories_burned: 'Calories burned',
      steps: 'Steps', resting_hr: 'Resting heart rate', sleep_minutes: 'Sleep',
      bp_systolic: 'BP systolic', bp_diastolic: 'BP diastolic',
    })) {
      const current = cfg.entities[key] || '';
      const options = (discovered.candidates[key] || []);
      const select = el('select', { id: 'ent-' + key }, [
        el('option', { value: '', text: '— not set —' }),
        ...options.map((o) => el('option', {
          value: o.entity_id,
          text: `${o.name} (${o.state}${o.unit ? ' ' + o.unit : ''})`,
          selected: o.entity_id === current,
        })),
        current && !options.some((o) => o.entity_id === current)
          ? el('option', { value: current, text: current, selected: true })
          : null,
      ]);
      inputs['entities.' + key] = select;
      entityCard.appendChild(el('div', { class: 'field' }, [
        el('label', { for: 'ent-' + key, text: label }),
        select,
        options.length === 0
          ? el('div', { class: 'help', text: 'No candidates found for this one.' })
          : null,
      ]));
    }
    root.appendChild(entityCard);

    /* --- profile --- */
    const p = cfg.profile;
    const field = (key, label, attrs, help) => {
      const input = el('input', { id: 'f-' + key, ...attrs });
      inputs[key] = input;
      return el('div', { class: 'field' }, [
        el('label', { for: 'f-' + key, text: label }),
        input,
        help ? el('div', { class: 'help', text: help }) : null,
      ]);
    };
    const goalSelect = el('select', { id: 'f-goal' }, ['lose', 'maintain', 'gain'].map((g) =>
      el('option', { value: g, text: g, selected: p.goal === g })));
    inputs['profile.goal'] = goalSelect;

    root.appendChild(el('div', { class: 'card' }, [
      el('div', { class: 'card-head' }, [el('h2', { text: 'Profile' })]),
      el('div', { class: 'field' }, [el('label', { for: 'f-goal', text: 'Goal' }), goalSelect]),
      el('div', { class: 'field-row' }, [
        field('profile.target_rate_lb_per_week', 'Rate (lb/week)',
          { type: 'number', step: '0.1', value: p.target_rate_lb_per_week }),
        field('profile.goal_weight_lb', 'Goal weight (lb)',
          { type: 'number', step: '1', value: p.goal_weight_lb }),
      ]),
      el('div', { class: 'field-row' }, [
        field('profile.height_in', 'Height (in)',
          { type: 'number', step: '0.5', value: p.height_in }),
        field('profile.birth_year', 'Birth year',
          { type: 'number', step: '1', value: p.birth_year }),
      ]),
      field('profile.activity_passthrough', 'Activity add-back (0–1)',
        { type: 'number', step: '0.05', min: '0', max: '1', value: p.activity_passthrough },
        'How much of an unusually active day gets added to that day’s target. ' +
        '1 holds the deficit exactly; 0 ignores activity.'),
    ]));

    /* --- diet --- */
    const d = cfg.diet;
    root.appendChild(el('div', { class: 'card' }, [
      el('div', { class: 'card-head' }, [el('h2', { text: 'Diet preferences' })]),
      el('div', { class: 'field-row' }, [
        field('diet.vegetarian_lunch_ratio', 'Vegetarian lunches',
          { type: 'number', step: '0.05', min: '0', max: '1', value: d.vegetarian_lunch_ratio }),
        field('diet.vegetarian_dinner_ratio', 'Vegetarian dinners',
          { type: 'number', step: '0.05', min: '0', max: '1', value: d.vegetarian_dinner_ratio }),
      ]),
      el('div', { class: 'field-row' }, [
        field('diet.max_weekday_prep_min', 'Weeknight cooking (min)',
          { type: 'number', step: '5', value: d.max_weekday_prep_min }),
        field('diet.snacks_per_day', 'Snacks per day',
          { type: 'number', step: '1', min: '0', max: '3', value: d.snacks_per_day }),
      ]),
      field('diet.dislikes', 'Foods to avoid',
        { type: 'text', value: (d.dislikes || []).join(', ') },
        'Comma-separated food ids. Any recipe containing one is excluded outright.'),
    ]));

    /* --- save --- */
    root.appendChild(el('button', {
      class: 'btn btn-primary btn-block',
      text: 'Save settings',
      onclick: async (e) => {
        e.target.disabled = true;
        try {
          await API.saveSettings(collect(inputs));
          UI.toast('Saved. Recomputing…');
          state.view = 'today';
          await render();
        } catch (err) {
          UI.toast(err.message);
        } finally {
          e.target.disabled = false;
        }
      },
    }));

    /* --- data tools --- */
    root.appendChild(el('div', { class: 'card', style: 'margin-top:14px' }, [
      el('div', { class: 'card-head' }, [el('h2', { text: 'Data' })]),
      el('p', { class: 'small muted', text:
        'Your own YAML files live in the add-on config folder and override the ' +
        'bundled data. Reload after editing.' }),
      el('div', { class: 'btn-row' }, [
        el('button', { class: 'btn btn-sm', text: 'Reload data files',
          onclick: async () => {
            try { const r = await API.reloadData(); UI.toast(r.message); }
            catch (err) { UI.toast(err.message); }
          } }),
        cfg.mealie.token_set
          ? el('button', { class: 'btn btn-sm', text: 'Preview Mealie import',
              onclick: () => runMealie() })
          : null,
        cfg.usda.api_key_set
          ? el('button', { class: 'btn btn-sm', text: 'Backfill USDA nutrients',
              onclick: () => runUsda() })
          : null,
      ]),
      el('div', { class: 'chips', style: 'margin-top:10px' },
        Object.entries(settings.metrics || {}).map(([k, v]) => UI.chip(`${k}: ${v}`))),
    ]));

    return root;
  }

  function collect(inputs) {
    const out = { profile: {}, entities: {}, diet: {} };
    for (const [key, node] of Object.entries(inputs)) {
      const [block, name] = key.split('.');
      let value = node.value;
      if (node.type === 'number') value = value === '' ? null : Number(value);
      if (name === 'dislikes') {
        value = String(value).split(',').map((s) => s.trim()).filter(Boolean);
      }
      if (value !== null) out[block][name] = value;
    }
    return out;
  }

  async function runMealie() {
    UI.toast('Reading Mealie…');
    try {
      const result = await API.importMealie(60, false);
      UI.sheet('Mealie import', el('div', {}, [
        el('p', { class: 'small', text:
          `${result.server}. ${result.importable} of ${result.fetched} recipes can be ` +
          'imported cleanly.' }),
        el('p', { class: 'small muted', text: result.hint }),
        el('div', { class: 'scroll-x' }, [
          el('ul', { class: 'note-list' }, result.recipes.slice(0, 40).map((r) =>
            el('li', { text: `${r.importable ? '✓' : '✗'} ${r.title}` +
              (r.unmatched.length ? ` — unmatched: ${r.unmatched.slice(0, 3).join(', ')}` : '') })
          )),
        ]),
        el('button', {
          class: 'btn btn-primary btn-block', style: 'margin-top:12px',
          text: `Import ${result.importable} recipes`,
          onclick: async (e) => {
            e.target.disabled = true;
            try {
              const w = await API.importMealie(60, true);
              UI.closeSheet();
              UI.toast(`Imported to ${w.written_to}`);
            } catch (err) { UI.toast(err.message); }
          },
        }),
      ]));
    } catch (err) {
      UI.toast(err.message);
    }
  }

  async function runUsda() {
    UI.toast('Asking FoodData Central…');
    try {
      const result = await API.importUsda({ limit: 40, write: true });
      UI.sheet('USDA backfill', el('div', {}, [
        el('p', { class: 'small', text:
          `Matched ${result.matched} of ${result.requested} foods.` }),
        el('p', { class: 'small muted', text: result.warning }),
        el('ul', { class: 'note-list' }, (result.results || []).slice(0, 40).map((r) =>
          el('li', { text: `${r.food_id} → ${r.matched || r.note}` }))),
      ]));
    } catch (err) {
      UI.toast(err.message);
    }
  }

  /* --------------------------------------------------- measurement entry */

  /* The standalone path's equivalent of a Fitbit. Weight and blood pressure are
   * the two that matter — everything else the engine can estimate or do without.
   * Deliberately a short form: a measurement screen you dread is a measurement
   * screen you stop using, and sparse data is handled fine by the trend
   * smoothing. */
  function showMetricsSheet(snapshot) {
    const today = new Date().toLocaleDateString('en-CA');
    const fields = {};

    const field = (key, label, unit, attrs, help) => {
      const input = el('input', {
        id: 'm-' + key, type: 'number', inputmode: 'decimal', ...attrs,
      });
      fields[key] = input;
      return el('div', { class: 'field' }, [
        el('label', { for: 'm-' + key, text: unit ? `${label} (${unit})` : label }),
        input,
        help ? el('div', { class: 'help', text: help }) : null,
      ]);
    };

    const dayInput = el('input', { id: 'm-day', type: 'date', value: today, max: today });

    const body = el('div', {}, [
      el('p', { class: 'small muted', text:
        'Weight is the one that matters — a few times a week is plenty, the trend ' +
        'smoothing handles the gaps. Everything else is optional.' }),

      el('div', { class: 'field' }, [
        el('label', { for: 'm-day', text: 'Date' }),
        dayInput,
      ]),

      el('div', { class: 'section-label', text: 'Weight' }),
      el('div', { class: 'field-row' }, [
        field('weight_lb', 'Weight', 'lb', { step: '0.1', placeholder: '—' }),
        field('body_fat_pct', 'Body fat', '%', { step: '0.1', placeholder: 'optional' },
          'If your scale reports it, this switches the resting-rate formula to a more accurate one.'),
      ]),

      el('div', { class: 'section-label', text: 'Blood pressure' }),
      el('div', { class: 'field-row' }, [
        field('bp_systolic', 'Systolic', 'mmHg', { step: '1', placeholder: '—' }),
        field('bp_diastolic', 'Diastolic', 'mmHg', { step: '1', placeholder: '—' }),
      ]),
      el('div', { class: 'help', style: 'margin-top:-6px', text:
        'Both numbers or neither. Categories are based on an average of readings ' +
        'across several days, so one measurement won\'t move much on its own.' }),

      el('details', { style: 'margin-top:14px' }, [
        el('summary', { text: 'Activity (optional)' }),
        el('p', { class: 'small muted', text:
          'Only worth filling in if you have a real number from a tracker. Left ' +
          'blank, PLATE estimates expenditure and then corrects it against your ' +
          'weight trend, which ends up more accurate than a guess here.' }),
        el('div', { class: 'field-row' }, [
          field('calories_burned', 'Calories burned', 'kcal', { step: '10', placeholder: 'optional' }),
          field('steps', 'Steps', '', { step: '100', placeholder: 'optional' }),
        ]),
        el('div', { class: 'field-row' }, [
          field('resting_hr', 'Resting HR', 'bpm', { step: '1', placeholder: 'optional' }),
          field('sleep_minutes', 'Sleep', 'min', { step: '5', placeholder: 'optional' }),
        ]),
      ]),

      el('button', {
        class: 'btn btn-primary btn-block', style: 'margin-top:16px',
        text: 'Save measurement',
        onclick: async (e) => {
          const payload = { day: dayInput.value };
          let any = false;
          for (const [key, input] of Object.entries(fields)) {
            if (input.value !== '') { payload[key] = Number(input.value); any = true; }
          }
          if (!any) { UI.toast('Nothing to save'); return; }
          e.target.disabled = true;
          try {
            await API.putMetrics(payload);
            UI.closeSheet();
            UI.toast('Saved');
            await render();
          } catch (err) {
            UI.toast(err.message);
            e.target.disabled = false;
          }
        },
      }),

      el('button', {
        class: 'btn btn-block', style: 'margin-top:8px',
        text: 'Recent measurements',
        onclick: () => showMetricsHistory(),
      }),
    ]);

    UI.sheet('Add measurement', body);
    setTimeout(() => fields.weight_lb.focus(), 120);
  }

  async function showMetricsHistory() {
    try {
      const data = await API.metrics(60);
      const labels = Object.fromEntries(data.fields.map((f) => [f.key, f]));
      UI.sheet('Recent measurements', el('div', {}, [
        el('p', { class: 'small muted', text:
          'Hand-entered values are never overwritten by an integration, so if you ' +
          'connect Home Assistant later these stay put.' }),
        data.days.length
          ? el('div', {}, data.days.slice(0, 40).map((d) =>
              el('div', { class: 'line' }, [
                el('div', { class: 'line-body' }, [
                  el('div', { class: 'line-name', text: dayName(d.day, true) }),
                  el('div', { class: 'line-meta', text:
                    Object.entries(d.values).map(([k, v]) => {
                      const f = labels[k];
                      const src = d.sources[k] === 'manual' ? '' : ' (synced)';
                      return `${f ? f.label : k} ${num(v, 1)}${f && f.unit ? ' ' + f.unit : ''}${src}`;
                    }).join(' · ') }),
                ]),
              ])))
          : UI.empty('No measurements recorded yet.'),
      ]));
    } catch (err) {
      UI.toast(err.message);
    }
  }

  /* --------------------------------------------------------------- sheets */

  async function showRecipe(recipeId) {
    if (!recipeId) return;
    try {
      const r = await API.recipe(recipeId);
      const n = r.per_serving || {};
      UI.sheet(r.title, el('div', {}, [
        el('div', { class: 'chips', style: 'margin-bottom:10px' }, [
          UI.chip(`${num(n.kcal)} kcal`),
          UI.chip(`${num(n.protein_g)} g protein`),
          UI.chip(`${num(n.fiber_g)} g fibre`),
          UI.chip(`${num(n.sodium_mg)} mg sodium`,
            n.sodium_mg > 700 ? 'warn' : null),
          UI.chip(`${r.total_min} min`),
          r.vegetarian ? UI.chip(r.vegan ? 'vegan' : 'vegetarian', 'veg') : null,
        ]),
        el('div', { class: 'section-label', text: `Ingredients — ${r.servings} servings` }),
        el('ul', { class: 'ing-list' }, r.ingredients.map((i) =>
          el('li', {}, [
            el('span', { text: i.name + (i.prep ? `, ${i.prep}` : '') }),
            el('span', { class: 'mono faint nowrap', text: `${i.display} · ${i.weight}` }),
          ]))),
        r.steps.length
          ? el('div', {}, [
              el('div', { class: 'section-label', text: 'Method' }),
              el('ol', { class: 'steps' }, r.steps.map((s) => el('li', { text: s }))),
            ])
          : null,
        r.notes ? el('p', { class: 'small muted', style: 'margin-top:10px', text: r.notes }) : null,
      ]));
    } catch (err) {
      UI.toast(err.message);
    }
  }

  function showMeal(meal, done) {
    const recipe = meal.recipe || {};
    UI.sheet(recipe.title || 'Meal', el('div', {}, [
      el('div', { class: 'chips', style: 'margin-bottom:12px' }, [
        UI.chip(meal.meal),
        UI.chip(`${meal.servings}× · ${num(meal.kcal)} kcal`),
        meal.vegetarian ? UI.chip('vegetarian', 'veg') : null,
        meal.leftover_from ? UI.chip('leftovers from ' + dayName(meal.leftover_from)) : null,
      ]),
      el('div', { class: 'btn-row' }, [
        done
          ? el('button', { class: 'btn grow', text: 'Undo log',
              onclick: () => unlogSlot(meal.slot) })
          : el('button', { class: 'btn btn-primary grow', text: 'Ate it',
              onclick: () => logSlot(meal.slot) }),
        el('button', { class: 'btn', text: 'Recipe',
          onclick: () => showRecipe(recipe.id) }),
      ]),
      el('div', { class: 'btn-row', style: 'margin-top:8px' }, [
        el('button', { class: 'btn btn-sm grow', text: 'Swap this meal',
          onclick: () => showSwap(meal) }),
        el('button', { class: 'btn btn-sm', text: 'Keep it (pin)',
          onclick: async () => {
            try {
              await API.pin(meal.slot, recipe.id);
              UI.closeSheet();
              UI.toast('Pinned — the rest of the week replanned around it');
              await render();
            } catch (err) { UI.toast(err.message); }
          } }),
      ]),
    ]));
  }

  async function showSwap(meal) {
    try {
      const { recipes } = await API.recipes({ meal: meal.meal, limit: 60 });
      UI.sheet('Swap ' + meal.meal, el('div', {}, [
        el('p', { class: 'small muted', text:
          'Picking one pins it to this slot and replans the rest of the week around it.' }),
        el('div', {}, recipes.map((r) =>
          el('button', {
            class: 'meal',
            onclick: async () => {
              try {
                await API.pin(meal.slot, r.id);
                UI.closeSheet();
                UI.toast('Swapped');
                await render();
              } catch (err) { UI.toast(err.message); }
            },
          }, [
            el('span', { class: 'meal-main' }, [
              el('span', { class: 'meal-title' }, [
                r.vegetarian ? el('span', { class: 'veg-dot' }) : null,
                el('span', { text: r.title }),
              ]),
              el('span', { class: 'meal-meta', text:
                `${num(r.per_serving.protein_g)} g protein · ${r.total_min} min · ${r.cuisine}` }),
            ]),
            el('span', { class: 'meal-kcal mono', text: num(r.per_serving.kcal) }),
          ]))),
      ]));
    } catch (err) {
      UI.toast(err.message);
    }
  }

  async function logSlot(slot) {
    try {
      await API.log({ slot });
      UI.closeSheet();
      UI.toast('Logged');
      await render();
    } catch (err) {
      UI.toast(err.message);
    }
  }

  async function unlogSlot(slot) {
    try {
      await API.unlog({ slot });
      UI.closeSheet();
      UI.toast('Removed');
      await render();
    } catch (err) {
      UI.toast(err.message);
    }
  }

  /* -------------------------------------------------------------- routing */

  const VIEWS = {
    today: renderToday,
    week: renderWeek,
    shop: renderShop,
    insight: renderInsight,
    settings: renderSettings,
  };

  async function render() {
    if (state.busy) return;
    state.busy = true;
    const target = viewEl();
    try {
      const node = await VIEWS[state.view]();
      clear(target).appendChild(node);
      target.scrollTop = 0;
      window.scrollTo(0, 0);
    } catch (err) {
      clear(target).appendChild(el('div', { class: 'card' }, [
        el('h2', { text: 'Something went wrong' }),
        el('p', { class: 'small muted', text: err.message }),
        el('button', { class: 'btn btn-block', text: 'Try again', onclick: () => render() }),
      ]));
      UI.banner(err.message, true);
    } finally {
      state.busy = false;
    }
  }

  document.querySelectorAll('.tab').forEach((tab) => {
    tab.addEventListener('click', async () => {
      document.querySelectorAll('.tab').forEach((t) => {
        const active = t === tab;
        t.classList.toggle('is-active', active);
        t.setAttribute('aria-selected', String(active));
      });
      state.view = tab.dataset.view;
      await render();
    });
  });

  document.getElementById('btn-settings').addEventListener('click', async () => {
    state.view = state.view === 'settings' ? 'today' : 'settings';
    document.querySelectorAll('.tab').forEach((t) => {
      t.classList.toggle('is-active', t.dataset.view === state.view);
    });
    await render();
  });

  document.getElementById('btn-refresh').addEventListener('click', async (e) => {
    const btn = e.currentTarget;
    btn.classList.add('is-busy');
    try {
      const result = await API.refresh();
      // /api/refresh succeeds even with no Home Assistant — it still recomputes
      // and republishes. Report what actually happened rather than claiming a
      // sync that never occurred.
      const synced = result && result.sync && result.sync.ok;
      const written = synced
        ? Object.values(result.sync.written || {}).reduce((a, b) => a + b, 0)
        : 0;
      UI.toast(synced ? `Synced ${written} readings` : 'Recalculated');
      await render();
    } catch (err) {
      UI.toast(err.message);
    } finally {
      btn.classList.remove('is-busy');
    }
  });

  // Coming back to a backgrounded phone should not show yesterday's numbers.
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden && state.view === 'today') render();
  });

  function get(targets, nutrient, field) {
    return ((targets || {})[nutrient] || {})[field] || 0;
  }

  render();
})();
