# Groceries: what's possible and what isn't

The short version: **no grocery chain in this app has a usable public API**, so
PLATE does not fetch live prices, does not build carts, and does not place
orders. This document explains why, what it does instead, and how to make the
part that does work well.

## Why there's no live data

**Trader Joe's** has no API, no online ordering and no delivery partner. Their
website has a product search; that's the entire public surface. The link in your
shopping list opens that search, for reference.

**Whole Foods** was acquired by Amazon and its ordering runs through Amazon
rather than any Whole Foods system. There is no public product or price API. The
order link searches Amazon Fresh, which does not carry the full in-store range,
and Prime pricing differs from shelf pricing.

**Safeway** (Albertsons) has internal endpoints that people reverse-engineer.
They change without notice, they're rate-limited and bot-detected, and scraping
them is against the site's terms of service. Building a household's meal planning
on top of that means it works for a few weeks and then quietly starts returning
stale or empty results — which, in a tool whose whole job is telling you what to
buy, is worse than not having the feature.

Instacart has a partner API. It is for retailers and brands under contract, not
individuals, and Whole Foods and Trader Joe's aren't on Instacart anyway.

## What PLATE does instead

### A catalogue you own

`app/data/stores/*.yaml` maps every ingredient in the recipe library to real
products at each store, with package sizes, aisle sections and prices:

```yaml
- {store: trader-joes, food_id: feta, name: Feta Cheese,
   package_qty: 8, package_unit: oz, price_usd: 3.49, price_updated: "2026-08-01"}
```

227 mappings ship with the add-on. Every price is hand-entered for a West Coast
store in mid-2026.

### Packages, not grams

A recipe wants 180 g of feta; the tub is 227 g. You buy one. Needing 300 g means
buying two, and the estimate reflects that. Loose goods marked `by_weight: true`
round to a sensible increment instead.

### Aisle-ordered lists

Each store carries an `aisle_order` — the order you physically encounter sections
in that chain. The list sorts by it, so it reads as one walk. **Editing this to
match your actual local store is the second most valuable change you can make.**

### Trip consolidation

If one store's entire list is a single item that another store also stocks, it
moves. Saving forty cents is not worth a separate stop. The move is planned in
full and only committed if *every* item on the doomed list finds a home —
a partial move would leave you visiting the store anyway, for less.

### Search handoffs

| Store | Link goes to | Can you order? |
|---|---|---|
| Trader Joe's | traderjoes.com product search | No — no delivery exists |
| Safeway | Instacart store search | Yes, in Instacart |
| Whole Foods | Amazon Fresh search | Yes, in Amazon |

These URLs carry a search term and nothing else. No session, no credentials, no
cart state. There is a test asserting exactly that.

## Keeping prices useful

Prices drift. PLATE shows how stale each one is and warns when any are over four
months old. The total is described as a floor, not a forecast, whenever some
items have no price on file.

To correct one, edit the product in the add-on's config folder:

```yaml
# /addon_configs/xxxxxxxx_plate/stores/my-prices.yaml
products:
  - {store: safeway, food_id: chicken-breast, name: Boneless Skinless Chicken Breast,
     package_qty: 1, package_unit: lb, price_usd: 7.49, by_weight: true,
     price_updated: "2026-11-02"}
```

Records match on `(store, food_id, name)` and yours are read after the bundled
ones, so this overrides rather than duplicates. Hit **Reload data** in Settings.

Realistically: fixing the twenty things you buy every week gets the estimate
close, and there's no value in maintaining the long tail.

## Adding a store

```yaml
stores:
  - id: costco
    name: Costco
    short: CST
    delivers: true
    search_url: "https://www.costco.com/CatalogSearch?keyword={q}"
    instacart_url: "https://www.instacart.com/store/costco/s?k={q}"
    aisle_order: [produce, bakery, meat, seafood, dairy, frozen, grains, canned, other]
```

`{q}` is replaced with the URL-encoded product name. Then add it to the enabled
store list in Settings and map some products to it.

## If you really want live prices

You'd be scraping. Before you do:

- It's against Safeway's and Amazon's terms of service.
- Both use bot detection; expect to be blocked.
- Endpoints change without notice, so it needs ongoing maintenance forever.
- Prices vary by store location, and the endpoints usually need a store id that
  is itself awkward to obtain.

If you accept all that, `app/engine/shopping.py` is where a price source would
plug in: give `Product.price_usd` a fresher value before the list is built. The
package-rounding, aisle-sorting and consolidation logic doesn't care where the
number came from. I'd keep the YAML catalogue as the fallback for when the
scraper inevitably breaks.
