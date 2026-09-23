-- Frozen pre-C1 schema from fork/main ae7db6c1c9; synthetic rows only.

        CREATE TABLE IF NOT EXISTS turns (
            turn_id TEXT PRIMARY KEY,
            parent_turn_id TEXT,
            is_subagent INT,
            depth INT,
            ts_start REAL,
            ts_end REAL,
            profile TEXT,
            provider TEXT,
            model TEXT,
            platform TEXT,
            chat_id TEXT,
            chat_name TEXT,
            api_calls INT,
            tools TEXT,
            input_tokens INT,
            output_tokens INT,
            cache_read INT,
            cache_write INT,
            reasoning INT,
            context_used INT,
            context_length INT,
            last_cache_read INT,
            last_cache_write INT,
            last_uncached INT,
            comp_sys_tokens INT,
            comp_tool_schema_tokens INT,
            comp_history_tokens INT,
            comp_history_message_count INT,
            comp_tool_result_tokens INT,
            comp_tool_arg_tokens INT,
            comp_tool_result_count INT,
            comp_skills_tokens INT,
            comp_skills_count INT,
            comp_framing_tokens INT,
            comp_calls_json TEXT,
            cost_usd REAL,
            cost_status TEXT,
            cost_uncached_usd REAL,
            cost_cache_read_usd REAL,
            cost_cache_write_usd REAL,
            cost_output_usd REAL,
            interrupted INT,
            alerted INT DEFAULT 0,
            user_text TEXT,
            final_text TEXT,
            cli_invocation_id TEXT
        );

        CREATE TABLE IF NOT EXISTS turn_tool_calls (
            turn_id TEXT,
            seq INT,
            name TEXT,
            args_preview TEXT,
            result_preview TEXT,
            PRIMARY KEY(turn_id, seq)
        );

        CREATE TABLE IF NOT EXISTS last_turn (
            platform TEXT,
            chat_id TEXT,
            turn_id TEXT,
            PRIMARY KEY(platform, chat_id)
        );

        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT
        );

        -- New-model pricing sentinel ledger (card t_2e382a4b). One row per
        -- (model, provider) that recorded an unpriced turn while absent from
        -- the pricing snapshot. The PRIMARY KEY *is* the dedup: the sentinel
        -- uses INSERT OR IGNORE and treats rowcount == 1 as "first sighting,
        -- alert now", so the alert fires exactly once per model no matter how
        -- many unpriced turns follow.
        CREATE TABLE IF NOT EXISTS seen_unpriced_models (
            model TEXT,
            provider TEXT,
            first_seen TEXT,
            alerted_at TEXT,
            PRIMARY KEY(model, provider)
        );

        CREATE INDEX IF NOT EXISTS idx_blackbox_turns_chat_end
            ON turns(platform, chat_id, ts_end);
        CREATE INDEX IF NOT EXISTS idx_blackbox_turns_cost
            ON turns(cost_usd);
        