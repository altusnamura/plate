# PLATE

A menu planner that runs as a Home Assistant add-on and adapts to your own
metrics: Fitbit expenditure, scale weight, and blood pressure from your Omron
monitor. Lunches are mostly vegetarian, calorie targets track what you actually
burn and how your weight is actually moving, and the shopping list comes out
sorted by store aisle.

Opens in the HA sidebar and works properly on a phone through the companion app.

---

## What it actually does

**It doesn't trust your Fitbit.** Wrist trackers estimate daily expenditure from
heart rate and steps and are commonly 10–25% off for any given person —
consistently off, which is the useful part. PLATE treats the tracker figure as a
*prior* and corrects it against the one thing that can't lie over time: the rate
of change of your own body mass.

```
intake − expenditure = energy stored        (≈3500 kcal per pound)

so:  TDEE = (what you logged − 3500 × pounds gained) ÷ days
```

That estimate is noisy at first and sharp after a month, so the two are blended
with a confidence weight that grows as your logging history does. Your target
starts out equal to what the watch says and converges on your real metabolism
over a few weeks. The Insight screen shows both numbers and the gap between them.

Weight is never read raw — daily swings of two or three pounds are water and gut
contents. Everything works off an exponentially weighted trend, and the rate of
change comes from the *slope* of that trend rather than the difference between
its endpoints (an EWMA lags, and endpoint differences inherit that lag).

**Blood pressure reshapes the menu, not just the display.** If your Omron
readings average into an elevated or hypertensive range, the sodium ceiling drops
from the DGA's 2300 mg to the AHA's 1500 mg, saturated fat tightens from 10% to
6% of energy, the potassium target rises to the DASH trial's 4700 mg, and the
planner's cost function starts actively selecting for that pattern. The
difference is measurable in the generated week, not cosmetic.

**Every target is traceable.** Nothing is invented. Protein comes from the ISSN
position stand, fibre from the IOM adequate intake, sodium and potassium from the
DGA and AHA, blood pressure categories from the 2017 ACC/AHA guideline. Each one
is shown in the app with the reasoning attached.

**The planner is honest about compromises.** Fitting seven days of meals to a
calorie target, a protein floor, a sodium ceiling, a vegetarian-lunch ratio,
weeknight time limits and a manageable shopping list is over-constrained. When
something has to give, the Week screen tells you which day missed what and why.

---

## Install

**Settings → Add-ons → Add-on Store → ⋮ → Repositories**, add:

```
https://github.com/altusnamura/plate
```

Then install **PLATE**, **Start** it, and open it from the sidebar.

> While the repository is private, Home Assistant can't read it — the Add-on
> Store fetches anonymously. Either make the repo public, or use the local
> install below, which needs no repository at all.

**Local install** (works regardless of repo visibility) — copy the `plate/`
subdirectory, not the whole project:

```bash
scp -r plate/ root@homeassistant.local:/addons/plate
```

Then **⋮ → Check for updates**; it appears under "Local add-ons".

Once running, open it and go to **Settings** (gear, top right).

### Point it at your entities

The Settings screen auto-detects candidates by name, unit and device class, and
offers them in dropdowns. Confirm each one — calibrating a metabolism against the
wrong sensor produces confident nonsense, so nothing is selected silently.

| Setting | Typical entity | Why it matters |
|---|---|---|
| Weight | `sensor.weight` (Fitbit Aria, Withings, any scale) | **Required.** Everything else is derived from the trend. |
| Calories burned | `sensor.fitbit_calories` | The prior for TDEE. Without it, targets fall back to a formula. |
| Body fat % | `sensor.body_fat` | Switches resting rate from Mifflin-St Jeor to the more accurate Katch-McArdle. |
| Blood pressure systolic/diastolic | your Omron integration | Drives the DASH adjustments. Optional but it's why you're building this. |
| Steps, resting HR, sleep | Fitbit | Used only to estimate activity when tracker calories are missing. |

Then set your goal, rate, height and birth year on the same screen.

> **History depth.** Home Assistant's recorder purges detailed history after ten
> days by default, which isn't enough to calibrate anything. PLATE therefore reads
> **long-term statistics** over the WebSocket API (kept indefinitely for any
> sensor with a `state_class`) and mirrors everything into its own database, so
> the problem shrinks the longer it runs. If your weight sensor has no
> `state_class`, only the last ten days are recoverable and the calibration will
> take a month to warm up.

### Log what you eat

Tap a meal → **Ate it**. That single tap is what powers the calibration: without
food logs, PLATE can only report what your tracker claims. Roughly 60% of days
logged over a four-week window is the threshold where calibration engages, and
the app tells you how far off that you are.

---

## Groceries: read this before expecting magic

**None of Trader Joe's, Safeway or Whole Foods has a usable public API.** Trader
Joe's has none at all. Whole Foods is behind Amazon's authentication. Safeway has
unofficial endpoints that break regularly and are against their terms of service.

So PLATE does the achievable thing:

- A **local product catalogue** — package sizes, aisle sections and prices you
  maintain — covering every ingredient in the recipe library across all three
  stores.
- Quantities **rounded up to whole packages**, because needing 180 g of a 150 g
  tub means buying two.
- Lists **grouped by store and sorted by that store's aisle order**, so it's one
  walk rather than a scavenger hunt.
- **Single-item trips folded away** — if the whole Safeway list is one jar of
  tahini that Whole Foods also stocks, it moves rather than sending you across
  town.
- **Search deep links** into Instacart (Safeway), Amazon Fresh (Whole Foods) and
  each retailer's own site. These carry a search term and nothing else. They do
  not log in, build a cart, or place an order.

**Prices are estimates you type in**, shown with how stale each one is. Correcting
them as you shop is the single highest-value edit you can make. See
[docs/GROCERY.md](docs/GROCERY.md).

---

## Your own data

The add-on's config folder is yours, survives updates, and overrides the bundled
data by `id`:

```
foods/    *.yaml   food records (per-100 g nutrition, units, aisle)
recipes/  *.yaml   recipes (ingredients reference food ids)
stores/   *.yaml   store definitions and product/price mappings
```

Ships with ~173 foods, 50 recipes (21 of them vegetarian lunches) and 227 store
product mappings. Hit **Reload data** in Settings after editing; a broken file is
reported there and ignored rather than taking the app down.

Two optional importers:

- **Mealie** — pulls your existing recipes and maps their ingredients onto the
  food database. Fuzzy matching *will* get things wrong, so it previews first and
  skips any recipe with an unmatched ingredient rather than importing one whose
  calorie count is quietly missing a third of its food.
- **USDA FoodData Central** — replaces my estimated micronutrients with
  analytically measured ones. Needs a free api.data.gov key. Only fills the
  optional nutrients by default; overwriting macros on a fuzzy name match would
  silently change every recipe's calories.

Nutrition values in the bundled database are reference figures, good enough for
planning, not laboratory analyses. Grains and dried legumes are recorded **dry**;
canned beans **drained**; meat and fish **raw**.

---

## Sensors published back to Home Assistant

`sensor.plate_calorie_target`, `calories_eaten`, `calories_remaining`,
`protein_remaining`, `sodium_today`, `potassium_today`, `fiber_today`,
`weight_trend`, `weekly_rate`, `tdee`, `tracker_bias`, `calibration_confidence`,
`adherence_7d`, `dash_score`, `next_meal`, `bp_systolic_avg`, `bp_category`,
plus `binary_sensor.plate_shopping_needed`.

Two transports. **REST** works with no configuration but creates entities no
integration owns — HA forgets them on restart and they never reach long-term
statistics. **MQTT discovery** needs a broker and produces real, durable,
graphable entities. If you already run Mosquitto, enable it in the add-on
options. See [docs/HOME_ASSISTANT.md](docs/HOME_ASSISTANT.md) for dashboard cards
and automation examples.

---

## Limitations, honestly

- **The weeknight cooking budget is a strong preference, not a hard cap.** With
  the shipped weights, mean weeknight active cooking lands near the 35-minute
  budget, but individual evenings run over when the nutrition targets need a dish
  that takes longer. Measured: raising the penalty until it *is* a hard cap makes
  the week noticeably blander for very little time saved.
- **Calibration needs food logs.** No logging, no correction — you get your
  tracker's number with a note saying so.
- **The 3500 kcal/lb constant** is the classic Wishnofsky figure for fat tissue.
  Real weight change mixes fat, lean tissue and glycogen-bound water. Over
  multi-week windows on a moderate deficit it's close enough, and any systematic
  error lands in the calibration factor rather than the target.
- **50 recipes is a starter library.** It's enough for the planner to satisfy its
  constraints with variety, and it's meant to grow.
- **Not medical software.** These targets come from population-level published
  guidelines adjusted for your metrics. PLATE knows nothing about your
  medications, kidney function or history. The raised potassium target in
  particular is worth raising with a doctor first — with some kidney conditions,
  and on ACE inhibitors, ARBs or potassium-sparing diuretics, the correct advice
  is the opposite. If your readings land in the hypertensive crisis range, the app
  says to see a doctor, and it means it.

---

## Development

```bash
python -m venv .venv && .venv/bin/pip install -r plate/requirements.txt pytest
python plate/tools/validate.py --recipes   # check the data set
python plate/tools/demo.py                 # full dry run with synthetic metrics
python plate/tools/smoke.py                # boot the app, hit every endpoint
python plate/tools/dev.py                  # dev server on :8099
python -m pytest plate/tests -q
```

`demo.py` is the fastest way to see whether a change to the planner's cost
weights improved anything — it prints the week, the diagnostics and the shopping
list in one go.

### Layout

```
plate/
  config.yaml            add-on manifest (ingress, options schema)
  app/
    engine/              pure domain logic, no I/O
      nutrients.py       nutrient vectors with missing-data coverage tracking
      units.py           unit conversion to grams; fails loudly, never guesses
      models.py          foods, recipes, stores, products
      library.py         YAML loading, validation, user overlay
      energy.py          weight trend, TDEE calibration, calorie targets
      targets.py         guideline-derived nutrient targets, BP, DASH scoring
      planner.py         greedy seed + local search over a weighted cost function
      shopping.py        package rounding, store assignment, trip consolidation
    ha.py                Home Assistant REST + WebSocket statistics client
    metrics.py           entity history -> clean daily series
    store.py             SQLite persistence
    service.py           orchestration; the single source of truth for the UI
    api.py / main.py     FastAPI
    static/              vanilla-JS mobile frontend, no build step
    data/                the bundled foods, recipes and store catalogue
  tests/                 86 tests, run against the real data set
```

Design notes worth knowing before changing things:

- **Recipe nutrition is always computed from ingredients, never stored.** Stored
  macros drift the moment someone edits an ingredient, and they make portion
  scaling a lie.
- **Vegetarian status is derived from ingredients too**, so a recipe can't claim
  to be vegetarian while containing anchovies. (Parmesan is flagged non-vegetarian
  — animal rennet.)
- **`service.snapshot()` is the only place numbers are computed.** The UI renders
  it and the sensor publisher publishes it. That's what keeps the ring in the app
  and the sensor on your dashboard from disagreeing.
- **The planner's cost weights are interpretable.** A weight of 100 on `kcal`
  means a day 10% off target costs 1.0 point; everything else is scaled to match.
  `shopping_breadth` is deliberately tiny — it scales with the total number of
  distinct ingredients (40–70 a week), so a weight in the low single digits
  silently dominates every nutrition term and collapses the menu onto three
  recipes. That bug is easy to reintroduce.
