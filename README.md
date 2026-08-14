# PLATE

A menu planner that adapts to your own numbers. It learns what you actually burn
by watching what your weight does, keeps lunches mostly vegetarian, tightens
sodium if your blood pressure asks for it, and hands you a shopping list sorted
by store aisle.

Runs standalone on any machine. Optionally installs as a Home Assistant add-on
later, without losing anything.

---

## Start it

### Windows

Double-click **`PLATE.bat`**. First run sets up a Python environment (a minute or
two), then it opens in your browser and stays reachable from your phone on the
same wifi.

### Anything with Python

```bash
pip install -r plate/requirements.txt
python run.py --lan
```

`--lan` prints a second URL that works from your phone. Drop it to keep the app
on this machine only.

### Docker

```bash
docker compose up -d
```

Then `http://<host>:8099`. Your data lives in `./data`, your own recipe and price
overrides in `./config` — both plain folders you can back up by copying.

---

## First five minutes

1. Open **Settings** (gear, top right) and set your goal, target rate, height and
   birth year.
2. Back on **Today**, tap **Add your first weigh-in**. Weight is the only
   measurement that really matters; a few times a week is plenty.
3. Add a blood pressure reading if you take them.
4. Eat something from the plan and tap **Ate it**.

That last one is what makes the whole thing work. Read on for why.

---

## What it actually does

**It works out your metabolism from your weight, not from a formula.** Calorie
calculators are guesses and fitness trackers are commonly 10–25% off for any one
person. PLATE uses the only measurement that can't lie over time — the rate your
body mass is actually changing:

```
intake − expenditure = energy stored        (≈3500 kcal per pound)

so:  TDEE = (what you logged − 3500 × pounds gained) ÷ days
```

Feed it three or four weeks of weigh-ins and food logs and it will tell you what
you burn, to within a couple of hundred calories, with no wearable involved.
There's a test that proves this: it fabricates a subject with a known metabolism,
feeds PLATE only hand-typed weights and food logs, and checks the number comes
back right.

If you *do* connect a tracker, its estimate becomes a starting point that gets
corrected the same way — and the app tells you how far off your watch reads.

Weight is never used raw. Daily swings of two or three pounds are water and gut
contents, so everything works off an exponentially weighted trend, with the rate
taken from that trend's *slope* rather than its endpoints (an EWMA lags, and
endpoint differences inherit the lag).

**Blood pressure changes the menu, not just a number on screen.** Readings
averaging into an elevated or hypertensive range drop the sodium ceiling from the
DGA's 2300 mg to the AHA's 1500 mg, tighten saturated fat from 10% to 6% of
energy, raise potassium to the DASH trial's 4700 mg, and push the planner to
actually select for that pattern. There's a test asserting the generated week
comes out measurably lower in sodium.

**Every target is traceable.** Protein from the ISSN position stand, fibre from
the IOM adequate intake, sodium and potassium from the DGA and AHA, blood
pressure categories from the 2017 ACC/AHA guideline. Each is shown in the app
with its reasoning.

**The planner admits when it compromised.** Fitting seven days to a calorie
target, a protein floor, a sodium ceiling, a vegetarian-lunch ratio, weeknight
time limits and a sane shopping list is over-constrained. When something gives,
the Week screen says which day missed what.

---

## Measurements

| | Standalone | With Home Assistant |
|---|---|---|
| Weight | Typed in, few times a week | Read from your scale automatically |
| Blood pressure | Typed in when you measure | Read from your monitor |
| Body fat % | Optional, if your scale shows it | Automatic |
| Calories burned | Optional — usually better left blank | From Fitbit |
| Food logged | Tap **Ate it** | Tap **Ate it** |

Leaving calories burned blank is genuinely fine, often better. PLATE estimates
expenditure from a formula and then corrects that estimate against your weight
trend, which beats a number you half-remember.

Hand-entered values are **never overwritten by a sync**. If you connect Home
Assistant later, everything you typed stays exactly as you typed it — you were
standing on the scale; an integration is guessing at a day boundary.

---

## Groceries: read this before expecting magic

**None of Trader Joe's, Safeway or Whole Foods has a usable public API.** Trader
Joe's has none. Whole Foods is behind Amazon's authentication. Safeway has
unofficial endpoints that break regularly and are against their terms.

So PLATE does the achievable thing:

- A **local product catalogue** — package sizes, aisle sections, prices you
  maintain — covering every ingredient in the recipe library at all three stores.
- Quantities **rounded up to whole packages**, because needing 180 g of a 150 g
  tub means buying two.
- Lists **grouped by store and sorted by that store's aisle order**, so it's one
  walk instead of a scavenger hunt.
- **Single-item trips folded away** — if the whole Safeway list is one jar of
  tahini that Whole Foods also stocks, it moves.
- **Search deep links** into Instacart, Amazon Fresh and each retailer's site.
  These carry a search term and nothing else: no login, no cart, no order.

Prices are estimates you type, shown with how stale each is. Correcting the
twenty things you buy weekly is the highest-value edit you can make. Details in
[docs/GROCERY.md](docs/GROCERY.md).

---

## Your own recipes and prices

`config/` (or the add-on's config folder) overrides the bundled data by `id`:

```
foods/    *.yaml   food records (per-100 g nutrition, units, aisle)
recipes/  *.yaml   recipes (ingredients reference food ids)
stores/   *.yaml   store definitions and product/price mappings
```

Ships with 173 foods, 50 recipes (21 vegetarian lunches) and 227 store product
mappings. Hit **Reload data** in Settings after editing; a broken file is reported
there and skipped rather than taking the app down.

Two optional importers:

- **Mealie** — pulls your existing recipes and maps ingredients onto the food
  database. Fuzzy matching gets things wrong, so it previews first and skips any
  recipe with an unmatched ingredient rather than importing one whose calories
  are quietly missing a third of its food.
- **USDA FoodData Central** — replaces my estimated micronutrients with measured
  ones. Free api.data.gov key. Only fills optional nutrients by default;
  overwriting macros on a fuzzy name match would silently change every recipe.

Bundled nutrition figures are reference estimates, fine for planning, not lab
analyses. Grains and dried legumes are recorded **dry**, canned beans **drained**,
meat and fish **raw**.

---

## Connecting Home Assistant (optional, later)

Two ways, and you can start with neither.

**Point standalone PLATE at your HA.** Set two environment variables and restart:

```bash
HA_URL=http://homeassistant.local:8123
HA_TOKEN=<long-lived access token from your HA profile page>
```

It then reads your scale, Fitbit and blood-pressure entities automatically and
publishes 17 sensors back. Your hand-typed history is preserved.

**Or install it as an add-on**, so it lives in the HA sidebar with no separate
server. Copy the `plate/` subdirectory to `/addons/plate` on your HA machine
(Samba share is easiest from Windows), then **Add-on Store → ⋮ → Check for
updates**. See [docs/HOME_ASSISTANT.md](docs/HOME_ASSISTANT.md) for the sensor
list, dashboard cards and automations.

---

## Limitations, honestly

- **Calibration needs food logs.** Without them PLATE can only report a formula
  estimate, and it says so rather than pretending.
- **It needs three or four weeks before it knows anything.** The first fortnight
  is an educated guess. That's inherent — you can't measure a metabolism faster
  than the body reveals it.
- **The weeknight cooking budget is a strong preference, not a hard cap.** Mean
  weeknight cooking lands near the 35-minute budget; individual evenings run over.
  Measured: pushing the penalty until it *is* a cap makes the week blander for
  very little time saved.
- **3500 kcal/lb** is the classic Wishnofsky figure for fat tissue. Real weight
  change mixes fat, lean tissue and water. Over multi-week windows on a moderate
  deficit it's close enough, and systematic error lands in the calibration factor
  rather than the target.
- **50 recipes is a starter library.** Enough for the planner to satisfy its
  constraints with variety, meant to grow.
- **`--lan` has no password.** It serves to your whole network. Fine at home, not
  on public or shared wifi. The add-on deployment gets authentication from Home
  Assistant's Ingress; standalone has none.
- **Not medical software.** Targets come from population-level published
  guidelines adjusted for your metrics. PLATE knows nothing about your
  medications, kidney function or history. The raised potassium target that comes
  with elevated blood pressure is worth checking with a doctor first — with some
  kidney conditions, and on ACE inhibitors, ARBs or potassium-sparing diuretics,
  the correct advice is the opposite. If readings land in the hypertensive crisis
  range, the app says to see a doctor, and means it.

---

## Development

```bash
python -m venv .venv && .venv/bin/pip install -r plate/requirements.txt pytest
python plate/tools/validate.py --recipes   # check the data set
python plate/tools/demo.py                 # full dry run with synthetic metrics
python plate/tools/smoke.py                # boot the app, hit every endpoint
python run.py --reload                     # dev server with auto-reload
python -m pytest plate/tests -q            # 105 tests
```

`demo.py` is the fastest way to see whether a change to the planner's cost
weights improved anything — it prints the week, the diagnostics and the shopping
list in one go.

### Layout

```
run.py                   standalone launcher
PLATE.bat                Windows double-click starter
Dockerfile               standalone image
docker-compose.yml
plate/
  config.yaml            HA add-on manifest (ingress, options schema)
  Dockerfile             HA add-on image — not the standalone one
  app/
    engine/              pure domain logic, no I/O
      nutrients.py       nutrient vectors with missing-data coverage tracking
      units.py           unit conversion to grams; fails loudly, never guesses
      models.py          foods, recipes, stores, products
      library.py         YAML loading, validation, user overlay
      energy.py          weight trend, TDEE calibration, calorie targets
      targets.py         guideline-derived targets, blood pressure, DASH scoring
      planner.py         greedy seed + local search over a weighted cost function
      shopping.py        package rounding, store assignment, trip consolidation
    ha.py                Home Assistant REST + WebSocket statistics client
    metrics.py           entity history and hand entry -> clean daily series
    store.py             SQLite persistence
    service.py           orchestration; single source of truth for the UI
    api.py / main.py     FastAPI
    static/              vanilla-JS mobile frontend, no build step
    data/                bundled foods, recipes, store catalogue
  tests/                 105 tests against the real data set
```

Design notes worth knowing before changing things:

- **Recipe nutrition is always computed from ingredients, never stored.** Stored
  macros drift the moment an ingredient changes, and make portion scaling a lie.
- **Vegetarian status is derived from ingredients too**, so a recipe can't claim
  to be vegetarian while containing anchovies. (Parmesan is flagged
  non-vegetarian — animal rennet.)
- **`service.snapshot()` is the only place numbers are computed.** The UI renders
  it and the sensor publisher publishes it. That's what stops the ring in the app
  and the sensor on a dashboard from disagreeing.
- **Manual metrics outrank synced ones**, enforced in `Store.put_metrics` via a
  `WHERE source <> 'manual'` on the upsert.
- **The planner's cost weights are interpretable.** Weight 100 on `kcal` means a
  day 10% off target costs 1.0 point; everything else is scaled to match.
  `shopping_breadth` is deliberately tiny — it scales with the total number of
  distinct ingredients (40–70 a week), so a weight in the low single digits
  silently dominates every nutrition term and collapses the menu onto three
  recipes. Easy bug to reintroduce.
