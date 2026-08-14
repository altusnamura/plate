# Changelog

## 0.1.0

First release.

- Home Assistant add-on with Ingress; appears in the sidebar and works in the
  companion app.
- Adaptive calorie targets: Fitbit expenditure treated as a prior and corrected
  against the observed weight trend via energy balance, blended by a confidence
  that grows with logging history. Reports how far off your tracker reads.
- Weight handled as an EWMA trend with gap-aware smoothing; rate of change taken
  from the regression slope rather than trend endpoints, which would inherit the
  EWMA's lag.
- Nutrient targets derived from published guidance (DGA, AHA, ISSN, IOM) with the
  reasoning shown in-app.
- Blood pressure from an Omron integration tightens sodium to 1500 mg and
  saturated fat to 6% of energy, raises potassium to 4700 mg, and shifts the
  planner toward the DASH pattern — a measurable change to the generated menu.
- Menu planner: greedy seed plus simulated-annealing local search over a weighted
  cost function covering calories, protein, sodium, fibre, potassium, the
  vegetarian-lunch ratio, variety, weeknight cooking time, batch cooking and
  shopping breadth. Explains its own compromises.
- Batch cooking emerges from the assignment rather than being planned separately,
  capped at 3 sittings per cook and the recipe's keep window.
- Per-store shopping lists for Trader Joe's, Safeway and Whole Foods: whole-package
  rounding, aisle-ordered sorting, pantry deduction, single-item trip
  consolidation and search deep links into Instacart / Amazon Fresh.
- 173 foods, 50 recipes (21 vegetarian lunches), 227 store product mappings, all
  user-overridable via YAML in the add-on config folder.
- Mealie recipe import and USDA FoodData Central nutrient backfill, both writing
  reviewable YAML overlays rather than mutating hidden state.
- 17 sensors published back to Home Assistant over REST, with optional MQTT
  discovery for durable entities.
- Mobile-first frontend: bottom navigation, 44px touch targets, safe-area insets,
  no horizontal scroll, no build step.
- 86 tests run against the real bundled data set.
