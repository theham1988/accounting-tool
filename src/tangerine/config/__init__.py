"""Config loader for Wave 1 (recipes, SKU mappings, current SKU prices).

Two YAML files feed the engine at startup:

- ``recipes.yaml`` — recipes plus Loyverse-item -> SKU mappings, loaded into a
  :class:`~tangerine.recipes.RecipeCatalog`.
- ``costs.yaml`` — current SKU per-unit prices, loaded into a
  :class:`~tangerine.cost.CostBook`.

Validation fails loudly at startup (PRD user story 24): malformed YAML,
unknown SKU references (a mapping pointing at a recipe that does not exist),
and missing required fields each raise :class:`ConfigError` with a readable
message. The tool does not start in a half-working state.
"""
