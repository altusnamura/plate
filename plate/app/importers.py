"""Bringing in outside data: Mealie recipes and USDA nutrition.

Both importers write **YAML overlay files into ``/config``** rather than mutating
anything bundled or hidden in the database. That is deliberate:

* You can read exactly what was imported, in the same format as the built-in data.
* You can edit or delete it with a text editor.
* Add-on updates replace the bundled files and leave yours alone.
* A bad import is undone by deleting one file.

The hard part of importing recipes is not HTTP, it's that "1 can chickpeas,
drained" has to become ``{item: chickpeas-canned, qty: 1, unit: can}``. Ingredient
matching is fuzzy and it *will* get things wrong, so nothing is imported silently:
every recipe reports which ingredients matched, with what confidence, and which
didn't. Unmatched ingredients block that single recipe rather than producing a
recipe whose calorie count is quietly missing a third of its food.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import httpx
import yaml

from .engine.models import Food, Library
from .engine.nutrients import CORE, OPTIONAL
from .engine.units import MASS_TO_G, VOLUME_TO_ML, normalise

log = logging.getLogger(__name__)

# Below this score a match is not offered at all.
MATCH_FLOOR = 0.45
# At or above this, the match is taken without asking.
MATCH_CONFIDENT = 0.82

_STOPWORDS = frozenset(
    """
    a an the of and or to in on with without fresh freshly chopped diced minced
    sliced grated shredded ground whole large medium small ripe raw cooked
    drained rinsed peeled seeded pitted halved quartered cubed thinly roughly
    finely optional taste plus more for garnish divided packed heaping level
    approximately about roughly each
    """.split()
)


def _tokens(text: str) -> list[str]:
    words = re.findall(r"[a-z]+", text.lower())
    return [w for w in words if w not in _STOPWORDS and len(w) > 1]


# --------------------------------------------------------------------------
# ingredient matching
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Match:
    food_id: str | None
    score: float
    reason: str

    @property
    def confident(self) -> bool:
        return self.food_id is not None and self.score >= MATCH_CONFIDENT


def build_index(library: Library, extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """Exact-match lookup: normalised phrase -> food id.

    ``extra`` holds user-taught mappings from the Settings screen, and is
    consulted first so a correction always wins over the built-in names.
    """
    index: dict[str, str] = {}
    for food in library.foods.values():
        for phrase in (food.id, food.name, *food.aliases):
            index[" ".join(_tokens(phrase))] = food.id
    for phrase, food_id in (extra or {}).items():
        if food_id in library.foods:
            index[" ".join(_tokens(phrase))] = food_id
    return index


def match_food(
    text: str,
    library: Library,
    index: Mapping[str, str],
) -> Match:
    """Best guess at which food ``text`` names.

    Three passes, cheapest first: exact normalised phrase, then substring
    containment, then Jaccard token overlap with a bonus for matching the *last*
    word — in English food names the head noun sits at the end ("smoked paprika",
    "red bell pepper"), so agreement there is worth more than agreement on a
    modifier.
    """
    tokens = _tokens(text)
    if not tokens:
        return Match(None, 0.0, "nothing to match")
    phrase = " ".join(tokens)

    if phrase in index:
        return Match(index[phrase], 1.0, "exact name")

    token_set = set(tokens)
    best: Match = Match(None, 0.0, "no candidate")

    for food in library.foods.values():
        candidates = [food.name, food.id.replace("-", " "), *food.aliases]
        for cand in candidates:
            cand_tokens = _tokens(cand)
            if not cand_tokens:
                continue
            cand_set = set(cand_tokens)

            if cand_set and cand_set <= token_set:
                # Every word of the food name appears in the text.
                score = 0.80 + 0.04 * min(len(cand_set), 4)
                reason = f"'{cand}' fully contained"
            else:
                overlap = len(token_set & cand_set)
                if not overlap:
                    continue
                union = len(token_set | cand_set)
                score = overlap / union
                if tokens[-1] == cand_tokens[-1]:
                    score += 0.18   # same head noun
                reason = f"{overlap}/{union} tokens with '{cand}'"

            if score > best.score:
                best = Match(food.id, min(score, 0.99), reason)

    return best if best.score >= MATCH_FLOOR else Match(None, best.score, "below threshold")


def pick_unit(raw_unit: str, quantity: float, food: Food) -> tuple[float, str] | None:
    """Convert an imported unit into one this food understands.

    Mass and volume units pass through when usable. Anything else — "can",
    "bunch", "clove", or nothing at all — only works if the food declares it, so
    the fallback is the food's own display unit, then grams via its per-item
    weight. Returns ``None`` when no honest conversion exists.
    """
    u = normalise(raw_unit or "")

    if u in MASS_TO_G:
        return quantity, u
    if u in VOLUME_TO_ML and (food.g_per_ml is not None or u in food.unit_g):
        return quantity, u
    if u in food.unit_g:
        return quantity, u
    if not u:
        # Bare number: "2 eggs", "1 avocado".
        if "ea" in food.unit_g:
            return quantity, "ea"
        if food.display_unit and food.display_unit in food.unit_g:
            return quantity, food.display_unit
    # Singularise a plural count unit the food knows ("cloves" -> "clove").
    if u.endswith("s") and u[:-1] in food.unit_g:
        return quantity, u[:-1]
    return None


# --------------------------------------------------------------------------
# Mealie
# --------------------------------------------------------------------------


@dataclass
class ImportedRecipe:
    record: dict[str, Any]
    matched: list[tuple[str, str, float]] = field(default_factory=list)
    unmatched: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def importable(self) -> bool:
        return not self.unmatched and bool(self.record.get("ingredients"))

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.record.get("id"),
            "title": self.record.get("title"),
            "importable": self.importable,
            "ingredient_count": len(self.record.get("ingredients") or []),
            "matched": [
                {"text": t, "food_id": f, "score": round(s, 2)} for t, f, s in self.matched
            ],
            "unmatched": list(self.unmatched),
            "warnings": list(self.warnings),
        }


class MealieImporter:
    """Reads recipes from a Mealie instance.

    Tandoor exposes a different schema; the matching machinery above is reusable
    but the JSON walking in :meth:`_to_record` would need its own version.
    """

    def __init__(self, base_url: str, token: str, timeout: float = 25.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}

    async def probe(self) -> tuple[bool, str]:
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as c:
                r = await c.get(f"{self.base_url}/api/app/about", headers=self._headers())
                if r.status_code == 401:
                    return False, "Mealie rejected the token"
                r.raise_for_status()
                version = (r.json() or {}).get("version", "unknown")
                return True, f"Mealie {version}"
        except httpx.HTTPError as exc:
            return False, f"could not reach Mealie: {exc}"

    async def list_slugs(self, limit: int = 200) -> list[str]:
        out: list[str] = []
        async with httpx.AsyncClient(timeout=self.timeout) as c:
            page = 1
            while len(out) < limit:
                r = await c.get(
                    f"{self.base_url}/api/recipes",
                    headers=self._headers(),
                    params={"page": page, "perPage": 50},
                )
                r.raise_for_status()
                body = r.json() or {}
                items = body.get("items") or []
                if not items:
                    break
                out.extend(i["slug"] for i in items if i.get("slug"))
                if page >= (body.get("total_pages") or 1):
                    break
                page += 1
        return out[:limit]

    async def fetch(self, slugs: Sequence[str]) -> list[Mapping[str, Any]]:
        out: list[Mapping[str, Any]] = []
        async with httpx.AsyncClient(timeout=self.timeout) as c:
            for slug in slugs:
                try:
                    r = await c.get(
                        f"{self.base_url}/api/recipes/{slug}", headers=self._headers()
                    )
                    r.raise_for_status()
                    out.append(r.json())
                except httpx.HTTPError as exc:
                    log.warning("mealie: could not fetch %s: %s", slug, exc)
        return out

    def convert(
        self, raw: Mapping[str, Any], library: Library, index: Mapping[str, str]
    ) -> ImportedRecipe:
        return _to_record(raw, library, index)


def _to_record(
    raw: Mapping[str, Any], library: Library, index: Mapping[str, str]
) -> ImportedRecipe:
    """Map one Mealie recipe onto this app's recipe schema."""
    slug = str(raw.get("slug") or raw.get("id") or "imported")
    title = str(raw.get("name") or slug)
    result = ImportedRecipe(record={})

    servings = _first_number(raw.get("recipeYield")) or 2.0
    prep = _minutes(raw.get("prepTime")) or 10
    cook = _minutes(raw.get("performTime") or raw.get("cookTime")) or 0

    ingredients: list[dict[str, Any]] = []
    for item in raw.get("recipeIngredient") or []:
        if isinstance(item, str):
            text, qty, unit = item, None, ""
        else:
            food_block = item.get("food") or {}
            unit_block = item.get("unit") or {}
            text = (
                food_block.get("name")
                or item.get("display")
                or item.get("note")
                or item.get("originalText")
                or ""
            )
            qty = item.get("quantity")
            unit = (unit_block.get("name") or unit_block.get("abbreviation") or "")
        text = str(text).strip()
        if not text:
            continue

        match = match_food(text, library, index)
        if match.food_id is None:
            result.unmatched.append(text)
            continue

        food = library.foods[match.food_id]
        quantity = _first_number(qty)
        if quantity is None or quantity <= 0:
            # Mealie allows quantity-free ingredients; guess one unit of whatever
            # the food's display unit is and flag it loudly.
            quantity = 1.0
            result.warnings.append(f"'{text}' had no quantity; assumed 1")

        converted = pick_unit(str(unit), quantity, food)
        if converted is None:
            result.unmatched.append(f"{text} (unit '{unit}' not usable for {food.id})")
            continue

        qty_out, unit_out = converted
        ingredients.append({"item": food.id, "qty": round(qty_out, 3), "unit": unit_out})
        result.matched.append((text, food.id, match.score))
        if not match.confident:
            result.warnings.append(
                f"'{text}' matched {food.id} with low confidence ({match.score:.2f}) — {match.reason}"
            )

    meals = _guess_meals(raw, title)
    result.record = {
        "id": f"mealie-{slug}",
        "title": title,
        "meals": meals,
        "servings": servings,
        "prep_min": prep,
        "cook_min": cook,
        "cuisine": _guess_cuisine(raw),
        "tags": sorted({str(t.get("name", t)).lower() for t in (raw.get("tags") or []) if t}),
        "ingredients": ingredients,
        "steps": [
            str(s.get("text", s)).strip()
            for s in (raw.get("recipeInstructions") or [])
            if str(s.get("text", s)).strip()
        ][:20],
        "notes": str(raw.get("description") or "")[:500],
        "keeps_days": 3,
        "packable": True,
        "source": "mealie",
        "source_url": str(raw.get("orgURL") or ""),
    }
    return result


def _guess_meals(raw: Mapping[str, Any], title: str) -> list[str]:
    """Infer which meals a recipe suits from its tags and categories.

    Defaults to lunch *and* dinner rather than guessing one, because a recipe the
    planner can't place anywhere is worse than one it places imperfectly.
    """
    text = " ".join(
        [title.lower()]
        + [str(t.get("name", t)).lower() for t in (raw.get("tags") or []) if t]
        + [str(c.get("name", c)).lower() for c in (raw.get("recipeCategory") or []) if c]
    )
    meals = [m for m in ("breakfast", "lunch", "dinner", "snack") if m in text]
    if "brunch" in text and "breakfast" not in meals:
        meals.append("breakfast")
    if "dessert" in text and "snack" not in meals:
        meals.append("snack")
    return meals or ["lunch", "dinner"]


_CUISINES = (
    "italian", "mexican", "indian", "thai", "chinese", "japanese", "korean",
    "vietnamese", "greek", "french", "spanish", "middle-eastern", "mediterranean",
    "american", "moroccan", "turkish", "asian",
)


def _guess_cuisine(raw: Mapping[str, Any]) -> str:
    text = " ".join(
        str(t.get("name", t)).lower() for t in (raw.get("tags") or []) if t
    ) + " " + str(raw.get("name") or "").lower()
    for c in _CUISINES:
        if c.replace("-", " ") in text or c in text:
            return c
    return "other"


def write_recipe_overlay(
    recipes: Iterable[ImportedRecipe], user_dir: Path, filename: str = "mealie-import.yaml"
) -> Path:
    """Write importable recipes to ``<user_dir>/recipes/<filename>``."""
    target = user_dir / "recipes" / filename
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = [r.record for r in recipes if r.importable]
    header = (
        "---\n"
        f"# Imported from Mealie on {date.today().isoformat()} by PLATE.\n"
        "# Safe to edit or delete. Recipes with unmatched ingredients were skipped\n"
        "# rather than imported with missing food — check the import report for those.\n"
    )
    with target.open("w", encoding="utf-8") as fh:
        fh.write(header)
        yaml.safe_dump({"recipes": payload}, fh, sort_keys=False, allow_unicode=True)
    log.info("wrote %d imported recipes to %s", len(payload), target)
    return target


# --------------------------------------------------------------------------
# USDA FoodData Central
# --------------------------------------------------------------------------

# FDC nutrient numbers. Stable identifiers, unlike the human-readable names.
FDC_NUTRIENTS: Mapping[str, str] = {
    "1008": "kcal",
    "1003": "protein_g",
    "1004": "fat_g",
    "1258": "satfat_g",
    "1005": "carb_g",
    "1079": "fiber_g",
    "2000": "sugar_g",
    "1093": "sodium_mg",
    "1092": "potassium_mg",
    "1087": "calcium_mg",
    "1090": "magnesium_mg",
    "1089": "iron_mg",
    "1162": "vitamin_c_mg",
}

FDC_BASE = "https://api.nal.usda.gov/fdc/v1"


@dataclass
class BackfillResult:
    food_id: str
    matched_description: str | None
    fdc_id: int | None
    nutrients: dict[str, float] = field(default_factory=dict)
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "food_id": self.food_id,
            "fdc_id": self.fdc_id,
            "matched": self.matched_description,
            "nutrients": {k: round(v, 2) for k, v in self.nutrients.items()},
            "note": self.note,
        }


class UsdaBackfill:
    """Replaces this app's estimated micronutrients with FoodData Central values.

    Only fills the *optional* nutrients by default — calcium, magnesium, iron,
    vitamin C — because those are the ones the bundled database mostly leaves
    blank. Core macros are left alone unless ``include_core`` is set, since
    overwriting them wholesale would silently change every recipe's calorie count
    based on a fuzzy name match, and a wrong match there is much more damaging
    than a missing magnesium figure.

    Needs a free api.data.gov key.
    """

    def __init__(self, api_key: str, timeout: float = 30.0) -> None:
        self.api_key = api_key
        self.timeout = timeout

    async def _search(self, client: httpx.AsyncClient, query: str) -> Mapping[str, Any] | None:
        r = await client.get(
            f"{FDC_BASE}/foods/search",
            params={
                "api_key": self.api_key,
                "query": query,
                # Foundation and SR Legacy are the analytically measured sets;
                # Branded is label data and much noisier.
                "dataType": "Foundation,SR Legacy",
                "pageSize": 3,
                "requireAllWords": "false",
            },
        )
        if r.status_code == 403:
            raise RuntimeError("USDA rejected the API key")
        r.raise_for_status()
        foods = (r.json() or {}).get("foods") or []
        return foods[0] if foods else None

    async def backfill(
        self,
        foods: Sequence[Food],
        include_core: bool = False,
    ) -> list[BackfillResult]:
        results: list[BackfillResult] = []
        wanted = set(OPTIONAL) | (set(CORE) if include_core else set())

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            for food in foods:
                try:
                    hit = await self._search(client, food.name)
                except (httpx.HTTPError, RuntimeError) as exc:
                    results.append(BackfillResult(food.id, None, None, note=str(exc)))
                    continue
                if not hit:
                    results.append(
                        BackfillResult(food.id, None, None, note="no FoodData Central match")
                    )
                    continue

                values: dict[str, float] = {}
                for n in hit.get("foodNutrients") or []:
                    number = str(n.get("nutrientNumber") or n.get("number") or "")
                    name = FDC_NUTRIENTS.get(number)
                    if name and name in wanted:
                        v = n.get("value")
                        if v is not None:
                            values[name] = float(v)

                results.append(
                    BackfillResult(
                        food_id=food.id,
                        matched_description=hit.get("description"),
                        fdc_id=hit.get("fdcId"),
                        nutrients=values,
                        note="" if values else "match found but no usable nutrients",
                    )
                )
        return results


def write_food_overlay(
    results: Sequence[BackfillResult],
    library: Library,
    user_dir: Path,
    filename: str = "usda-backfill.yaml",
) -> Path:
    """Write a foods overlay containing only the nutrients we learned.

    Each record repeats the food's existing core nutrition, because the loader
    requires all core fields on every record and an overlay replaces the record
    it shadows rather than merging into it.
    """
    target = user_dir / "foods" / filename
    target.parent.mkdir(parents=True, exist_ok=True)

    records: list[dict[str, Any]] = []
    for res in results:
        if not res.nutrients:
            continue
        food = library.foods.get(res.food_id)
        if food is None:
            continue
        per_100g = {n: food.per_100g.get(n) for n in CORE}
        per_100g.update(res.nutrients)

        record: dict[str, Any] = {
            "id": food.id,
            "name": food.name,
            "aisle": food.aisle,
            "vegan": food.vegan,
            "vegetarian": food.vegetarian,
            "pantry_staple": food.pantry_staple,
            "keeps_days": food.keeps_days,
            "per_100g": {k: round(float(v), 3) for k, v in per_100g.items() if v is not None},
        }
        if food.g_per_ml is not None:
            record["g_per_ml"] = food.g_per_ml
        if food.unit_g:
            record["unit_g"] = dict(food.unit_g)
        if food.display_unit:
            record["display_unit"] = food.display_unit
        if food.dash_group:
            record["dash_group"] = food.dash_group
        if food.dash_serving_g:
            record["dash_serving_g"] = food.dash_serving_g
        if food.tags:
            record["tags"] = sorted(food.tags)
        if food.aliases:
            record["aliases"] = list(food.aliases)
        record["notes"] = (
            f"Micronutrients from USDA FoodData Central "
            f"(FDC {res.fdc_id}, matched '{res.matched_description}')."
        )
        records.append(record)

    header = (
        "---\n"
        f"# USDA FoodData Central backfill written by PLATE on {date.today().isoformat()}.\n"
        "# These records OVERRIDE the bundled foods of the same id. Delete this file\n"
        "# to revert to the built-in estimates. Check the `notes` on each record for\n"
        "# which FDC entry it matched — fuzzy name matching is not infallible.\n"
    )
    with target.open("w", encoding="utf-8") as fh:
        fh.write(header)
        yaml.safe_dump({"foods": records}, fh, sort_keys=False, allow_unicode=True)
    log.info("wrote %d USDA-backfilled foods to %s", len(records), target)
    return target


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _first_number(v: Any) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v) or None
    m = re.search(r"(\d+(?:[.,]\d+)?)(?:\s*/\s*(\d+))?", str(v))
    if not m:
        return None
    value = float(m.group(1).replace(",", "."))
    if m.group(2):
        try:
            value /= float(m.group(2))
        except ZeroDivisionError:
            pass
    return value or None


def _minutes(v: Any) -> int | None:
    """Parse Mealie's free-text or ISO-8601 duration into minutes."""
    if v is None:
        return None
    text = str(v).strip()
    if not text:
        return None
    iso = re.match(r"^PT(?:(\d+)H)?(?:(\d+)M)?$", text, re.I)
    if iso:
        hours = int(iso.group(1) or 0)
        mins = int(iso.group(2) or 0)
        return hours * 60 + mins or None
    hours = re.search(r"(\d+(?:\.\d+)?)\s*(?:h|hour)", text, re.I)
    mins = re.search(r"(\d+)\s*(?:m|min)", text, re.I)
    total = 0
    if hours:
        total += int(float(hours.group(1)) * 60)
    if mins:
        total += int(mins.group(1))
    if total:
        return total
    n = _first_number(text)
    return int(n) if n else None
