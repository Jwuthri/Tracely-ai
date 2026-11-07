-- Whether a span has any input / output, stored as one byte instead of re-derived from the text.
--
-- The traces list asks this of every root span on every page load (`_root_has` in async_reader).
-- `input` holds whole LLM prompts — conversation history included — so `input != ''` decompressed
-- the entire column: 11.7 GiB read and ~1.3 GiB of memory per query at ~550k spans, three of
-- them per page load, until ClickHouse hit its memory limit and the traces tab 500'd. A String in
-- 24.8 has no size-only subcolumn, so `notEmpty(input)` reads the same bytes; only a stored flag
-- avoids them.
--
-- `input`/`output` are Nullable: `notEmpty(NULL)` is NULL, which a UInt8 can't hold — without the
-- `ifNull` every insert carrying a NULL text fails. MATERIALIZED, so every insert path fills it with
-- no code change. Existing parts compute it from
-- the text on read until backfilled — correct, just as slow as before. Backfill ONCE per existing
-- deployment (a mutation that rewrites every part, so it is not in this re-applied file):
--   ALTER TABLE events MATERIALIZE COLUMN has_input, MATERIALIZE COLUMN has_output
--
-- One statement (comma-separated actions), per the runner's one-statement-per-file convention.
ALTER TABLE events
    ADD COLUMN IF NOT EXISTS has_input UInt8 MATERIALIZED ifNull(notEmpty(input), 0),
    ADD COLUMN IF NOT EXISTS has_output UInt8 MATERIALIZED ifNull(notEmpty(output), 0)
