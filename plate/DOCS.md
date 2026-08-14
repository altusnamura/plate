# PLATE

Adaptive menu planning driven by your own Fitbit, scale and blood pressure data.
Vegetarian-leaning lunches, calorie targets that correct themselves against your
actual weight trend, and shopping lists sorted by store aisle.

## Setup

1. Start the add-on and open it from the sidebar.
2. Tap the gear icon, top right.
3. Confirm the detected entities — at minimum **Weight**, ideally **Calories
   burned** and your **blood pressure** pair. PLATE suggests candidates but never
   picks one silently, because calibrating against the wrong sensor produces
   confident nonsense.
4. Set your goal, target rate, height and birth year.
5. Save. The first plan generates immediately.

Then log meals as you eat them — tap a meal, tap **Ate it**. That's what powers
the calibration.

## Options

| Option | Default | Notes |
|---|---|---|
| `profile.goal` | `lose` | `lose` / `maintain` / `gain` |
| `profile.target_rate_lb_per_week` | `-1.0` | Capped at 1% of body mass per week |
| `profile.goal_weight_lb` | `175` | Used for the projected goal date |
| `profile.height_in`, `birth_year`, `sex` | | For the resting-rate formula |
| `entities.*` | empty | Easier to set in the app than here |
| `diet.vegetarian_lunch_ratio` | `0.85` | Share of lunches to keep meat-free |
| `diet.vegetarian_dinner_ratio` | `0.4` | |
| `diet.max_weekday_prep_min` | `35` | Active cooking budget. A strong preference, not a hard cap. |
| `diet.snacks_per_day` | `1` | |
| `diet.dislikes` | `[]` | Food ids. Any recipe containing one is excluded outright. |
| `stores.enabled` | all three | `trader-joes`, `safeway`, `whole-foods` |
| `stores.delivery_partner` | `instacart` | Which handoff link to show |
| `mealie.url` / `token` | empty | Optional recipe import |
| `usda.api_key` | empty | Free key from api.data.gov, for nutrient backfill |
| `mqtt.*` | disabled | Enable for durable HA entities |

Options set here are defaults; the in-app Settings screen overrides them without
needing a restart.

## Your own recipes and prices

The add-on's config folder holds YAML that overrides the bundled data:

```
foods/    recipes/    stores/
```

A file here with the same `id` as a built-in one replaces it. Files starting with
`_` are ignored. Hit **Reload data** in Settings after editing — errors are
reported there, and a broken file is skipped rather than taking the app down.

The most useful edits: correcting **prices** as you shop, and setting each
store's **`aisle_order`** to match the layout of the branch you actually go to.

## What this is not

Nutrition planning software reading a home monitor, not a clinician. Targets come
from population-level published guidelines (DGA, AHA, ISSN, IOM, 2017 ACC/AHA)
adjusted for your metrics. It knows nothing about your medications, kidney
function or history. The raised potassium target that comes with elevated blood
pressure is worth checking with a doctor first — with some kidney conditions, and
on ACE inhibitors, ARBs or potassium-sparing diuretics, the correct advice is the
opposite.

No grocery chain here has a usable API. Prices are estimates you maintain and the
"order" links are search handoffs, not carts.

Full documentation, including the maths behind the calibration:
<https://github.com/altusnamura/plate>
