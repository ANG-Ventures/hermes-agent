-- tokens-ace/src/tokens_data/aggregations.py:303 (2026-09-21)

            SELECT COUNT(*) AS total_turns,
                   COALESCE(SUM(cost_usd),0) AS total_cost,
                   COALESCE(SUM(((cost_status='unknown' OR cost_usd IS NULL) AND (COALESCE(cache_read,0) + COALESCE(cache_write,0) + COALESCE(input_tokens,0) + COALESCE(output_tokens,0)) > 0)),0) AS unpriced_turns,
                   COALESCE(SUM(cache_read),0) AS cache_read,
                   COALESCE(SUM(cache_write),0) AS cache_write,
                   COALESCE(SUM(input_tokens),0) AS uncached,
                   COALESCE(SUM(output_tokens),0) AS output,
                   COALESCE(SUM(reasoning),0) AS reasoning,
                   COALESCE(SUM(MAX(0, COALESCE(output_tokens,0) - COALESCE(reasoning,0))),0) AS final_out,
                   COALESCE(SUM(api_calls),0) AS api_calls,
                   COALESCE(SUM(COALESCE(cache_read,0) + COALESCE(cache_write,0) + COALESCE(input_tokens,0)),0) AS total_input_billed,
                   COALESCE(SUM(
                     CASE WHEN COALESCE(reasoning,0) > COALESCE(output_tokens,0)
                          THEN 1 ELSE 0 END),0) AS reasoning_gt_output_rows
            FROM turns WHERE ts_end >= ?
            