-- Queries an inventory analyst would actually run.
-- Paste one into the SQL tab, or run it with:
--   .venv/bin/python -c "import duckdb,sys;print(duckdb.connect('data/inventory.duckdb',read_only=True).sql(open(sys.argv[1]).read()))" file.sql
-- Everything is read-only. Names were transcribed from photos, so match them with ILIKE, not '='.

-- 1. What is on the shelf, per article
SELECT article,
       sum(quantity)                     AS units,
       count(*)                          AS lines,
       count(*) FILTER (backorder)       AS backorder_lines,
       max(delivery_date)                AS last_delivery
FROM inventory_lines
GROUP BY article
ORDER BY units DESC;

-- 2. Spend concentration: how much of the volume comes from each supplier
SELECT supplier,
       count(DISTINCT note_id)                                        AS notes,
       sum(quantity)                                                  AS units,
       round(100 * sum(quantity) / sum(sum(quantity)) OVER (), 1)     AS share_percent
FROM inventory_lines
GROUP BY supplier
ORDER BY units DESC;

-- 3. Lead time between order and delivery, per supplier
SELECT supplier,
       count(*)                                        AS lines_with_both_dates,
       round(avg(delivery_date - order_date), 1)       AS avg_lead_days,
       max(delivery_date - order_date)                 AS worst_lead_days
FROM inventory_lines
WHERE order_date IS NOT NULL
GROUP BY supplier
ORDER BY avg_lead_days DESC;

-- 4. Still to come: open backorders, oldest first
SELECT note_id, delivery_date, supplier, article, quantity, site, designation
FROM inventory_lines
WHERE backorder
ORDER BY delivery_date;

-- 5. Deliveries per week, to see the rhythm of a job
SELECT date_trunc('week', delivery_date) AS week,
       count(DISTINCT note_id)           AS notes,
       count(*)                          AS lines,
       sum(quantity)                     AS units
FROM inventory_lines
GROUP BY week
ORDER BY week;

-- 6. What each site consumed
SELECT coalesce(site, '(no site)') AS site,
       count(DISTINCT article)     AS distinct_articles,
       sum(quantity)               AS units,
       min(delivery_date)          AS first_delivery,
       max(delivery_date)          AS last_delivery
FROM inventory_lines
GROUP BY site
ORDER BY units DESC;

-- 7. Ordered against what arrived.
--    Both tables use the same article vocabulary, so this join is meaningful.
SELECT l.article,
       sum(l.quantity)                   AS asked,
       coalesce(sum(d.units), 0)         AS delivered,
       sum(l.quantity) - coalesce(sum(d.units), 0) AS gap
FROM purchase_order_lines l
LEFT JOIN (SELECT article, sum(quantity) AS units FROM inventory_lines GROUP BY article) d
       ON d.article = l.article
GROUP BY l.article
ORDER BY gap DESC;

-- 8. Hardware lines ticked "to order" and never seen on a delivery note since
SELECT h.list_id, h.list_date, h.site, l.article, l.quantity
FROM purchase_orders h
JOIN purchase_order_lines l USING (list_id)
WHERE l.in_stock IS FALSE
  AND NOT EXISTS (
    SELECT 1 FROM inventory_lines d
    WHERE d.article = l.article AND d.delivery_date >= h.list_date
  )
ORDER BY h.list_date, l.line_no;

-- 9. Names used on only one side: candidates for a merge in the vocabulary card
SELECT n.article, coalesce(d.lines, 0) AS delivery_lines, coalesce(l.lines, 0) AS list_lines
FROM article_names n
LEFT JOIN (SELECT article, count(*) AS lines FROM inventory_lines GROUP BY article) d USING (article)
LEFT JOIN (SELECT article, count(*) AS lines FROM purchase_order_lines GROUP BY article) l USING (article)
WHERE coalesce(d.lines, 0) = 0 OR coalesce(l.lines, 0) = 0
ORDER BY n.seen DESC;

-- 10. Same product, two suppliers: where a price or source comparison is possible
SELECT article, count(DISTINCT supplier) AS suppliers,
       string_agg(DISTINCT supplier, ', ') AS seen_from
FROM inventory_lines
GROUP BY article
HAVING count(DISTINCT supplier) > 1
ORDER BY suppliers DESC, article;

-- 11. Who entered what, last 30 days (the audit trail)
SELECT user_name, action, count(*) AS events, max(happened_at) AS last_time
FROM audit
WHERE happened_at >= now() - INTERVAL 30 DAY
GROUP BY user_name, action
ORDER BY user_name, events DESC;

-- 12. How much the reading had to be corrected, week by week
SELECT date_trunc('week', measured_at)                       AS week,
       count(*)                                              AS documents,
       sum(fields)                                           AS fields,
       sum(corrected)                                        AS corrected,
       round(100.0 * (sum(fields) - sum(corrected)) / sum(fields), 1) AS accuracy_percent
FROM readings
GROUP BY week
ORDER BY week;
