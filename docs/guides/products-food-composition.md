# Products food composition and labelling

The Products pack's optional `food` feature (default off, #2216) answers: given a food product (GTIN) or a generic
food, what did each provider publish as ingredients, allergens, nutrient values and labelling claims at a date,
with source, revision and as-of time, and which notices already held by Products safety cite it?

Out of scope: health or diet advice, and nutrition scores, health ratings or rankings computed by Noesis. Open Food
Facts' own scores (Nutri-Score, NOVA, Eco-Score) are excluded at acquisition.

## Sources and licences

| Provider | Class | Source id | Access | Licence |
| --- | --- | --- | --- | --- |
| Open Food Facts | crowd-sourced | `off-food-products` | product API by GTIN (optional pinned `rev`) | ODbL 1.0 / DbCL 1.0; images CC BY-SA (never mirrored) |
| USDA FoodData Central | reference | `fdc-foods` | `/fdc/v1/food/{fdcId}`, key `NOESIS_FDC_API_KEY` | public domain (CC0), citation requested |
| Ciqual (Anses) | reference | `ciqual-composition` | pinned edition archive, selected food codes | Licence Ouverte / Etalab 2.0 |

EFSA, BLS and CoFID are reference-only decisions and Frida is excluded; see
[the source audit](../development/food-composition-evidence/source-audit.md). All sources are in
`config/source_packs/products.json` (`products-displays` 1.3.0, operation `food`) and are `unverified-live` until
the dated live run (#2302).

**ODbL obligations.** Every Open Food Facts record, answer value and export carries the attribution and
`share_alike: true`. OFF-derived records stay in their own provider rows with the `crowd-sourced` class, so an
adapted database can be offered under the ODbL (or removed) without touching reference data.

## Workflow

1. Select the feature: `coordinator.select("products", version, features=["food", "safety"])` (the notice links
   read the store of the `safety` feature).
2. Acquire: `run_source_pack_execution` with pack `products-displays`, operation `food` (and `notices` for RASFF).
   Each OFF revision, FDC publication or table edition is an immutable label revision; replays add nothing.
3. Identity: `propose_food_matches` proposes GTIN candidates (UPC-A, EAN-13 and GTIN-14 normalised to one key)
   between OFF, FDC Branded and Products models, and name candidates between generic foods; `review_food_match`
   accepts, rejects or defers with a reason. `list_food_identity_conflicts` shows a GTIN published under different
   brands; conflicts cannot be accepted.
4. Notices: `link_food_notices` links a food product to a notice revision only when the notice cites its GTIN or its
   brand and exact designation, or a reviewed notice match exists. Each later notice revision is evaluated again
   without deleting earlier links.
5. Ask: `food_composition_as_of(namespace, gtin=..., as_of=...)` returns per provider the revision current at the
   date, every value with its citation, crowd-sourced and reference values side by side (differences named, never
   converted or reconciled), unknowns (unmatched GTIN, absent or unknown units, missing nutrients, no label history
   before the date) and linked notices with the label's allergens and the notice's hazard both quoted. A product
   without a link has "no notice on record", which is not a statement that it is safe. `food_label_history` lists
   every revision.
6. Monitor: `create_food_composition_monitor` on GTINs, foods or table editions; `run_food_composition_monitor`
   reports label revisions, nutrient-value and allergen-declaration changes, new table editions and newly linked
   notices, each citing both revisions.

## Evidence

Offline: `tests/unit/domains/test_food_composition_acceptance.py` (the tracker journey through the MCP tools, sockets
blocked) and the `test_food_*` suites. Fixtures are synthetic. Live evidence is reported separately under #2302.
