-- Issue #35: SKU roles. Roles are derived from relations (purchasable = no
-- recipe; produced = has one), but "prep" — a produced SKU declared usable
-- inside other recipes — is the one stored fact. The seed writes this flag
-- from usage (a recipe whose output SKU another recipe consumes is a prep);
-- this migration adds the column and backfills the flag for any database
-- seeded before the column existed, using the same usage rule.

ALTER TABLE recipes ADD COLUMN prep INTEGER NOT NULL DEFAULT 0;

UPDATE recipes
   SET prep = 1
 WHERE sku_id IN (
       SELECT DISTINCT ri.ingredient_sku_id
         FROM recipe_ingredients AS ri
         JOIN recipes AS r ON r.sku_id = ri.ingredient_sku_id
       );
