-- =====================================================================
-- Amazon Payments vs Settlement reconciliation schema
-- Re-runnable: drops and recreates everything in schema "recon".
-- =====================================================================
DROP SCHEMA IF EXISTS recon CASCADE;
CREATE SCHEMA recon;
SET search_path = recon, public;

-- ---------------------------------------------------------------------
-- 1. Mapping configuration (data, not code). Columns mirror the CSVs
--    exactly so MAPPING_FIXES.sql reads naturally against them.
-- ---------------------------------------------------------------------
CREATE TABLE payment_mapping_config (
    rule_id                                serial PRIMARY KEY,
    csv_line                               int,          -- line in the original CSV (NULL = added by a fix)
    transaction_type                       text NOT NULL DEFAULT '',
    description                            text NOT NULL DEFAULT '',
    amount_field                           text NOT NULL,
    record_ref                             text NOT NULL,
    to_summary_field_when_positive_amount  text NOT NULL DEFAULT '',
    to_summary_field_when_negative_amount  text NOT NULL DEFAULT ''
);

CREATE TABLE settlement_mapping_config (
    rule_id                                serial PRIMARY KEY,
    csv_line                               int,
    transaction_type                       text NOT NULL DEFAULT '',
    amount_type                            text NOT NULL DEFAULT '',
    amount_description                     text NOT NULL DEFAULT '',
    record_ref                             text NOT NULL,
    to_summary_field_when_positive_amount  text NOT NULL DEFAULT '',
    to_summary_field_when_negative_amount  text NOT NULL DEFAULT ''
);

-- Payments-report column header -> amount_field name used by the config.
CREATE TABLE payment_column_alias (
    source_header text PRIMARY KEY,
    amount_field  text NOT NULL
);

-- Summary sheet layout and the routing of summary fields onto its lines.
CREATE TABLE summary_line (
    line_code  text PRIMARY KEY,
    section    text NOT NULL,           -- Sales / Refunds / Expenses / Paid To Amazon
    label      text NOT NULL,
    sort_order int  NOT NULL
);
CREATE TABLE summary_field_map (
    summary_field text NOT NULL,
    sign          text NOT NULL CHECK (sign IN ('ANY','POS','NEG')),
    line_code     text NOT NULL REFERENCES summary_line(line_code),
    PRIMARY KEY (summary_field, sign)
);

-- Every change to a mapping table is captured, so the fix history lives in the DB.
CREATE TABLE mapping_config_audit (
    audit_id   bigserial PRIMARY KEY,
    table_name text NOT NULL,
    op         text NOT NULL,
    rule_id    int,
    old_row    jsonb,
    new_row    jsonb,
    changed_at timestamptz NOT NULL DEFAULT now()
);
CREATE FUNCTION audit_mapping() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO recon.mapping_config_audit(table_name, op, rule_id, old_row, new_row)
    VALUES (TG_TABLE_NAME, TG_OP,
            COALESCE((to_jsonb(NEW)->>'rule_id')::int, (to_jsonb(OLD)->>'rule_id')::int),
            CASE WHEN TG_OP <> 'INSERT' THEN to_jsonb(OLD) END,
            CASE WHEN TG_OP <> 'DELETE' THEN to_jsonb(NEW) END);
    RETURN COALESCE(NEW, OLD);
END $$;
-- (triggers are attached after the initial seed, see recon/configs.py)

-- ---------------------------------------------------------------------
-- 2. Ingestion. ONE table holds the rows of BOTH files.
-- ---------------------------------------------------------------------
CREATE TABLE ingest_run (
    run_id       serial PRIMARY KEY,
    label        text,
    started_at   timestamptz NOT NULL DEFAULT now(),
    finished_at  timestamptz,
    config_hash  text            -- md5 of both mapping tables at ingest time
);

CREATE TABLE source_file (
    file_id    serial PRIMARY KEY,
    run_id     int NOT NULL REFERENCES ingest_run ON DELETE CASCADE,
    source     text NOT NULL CHECK (source IN ('PAYMENT','SETTLEMENT')),
    file_name  text NOT NULL,
    sha256     text NOT NULL,
    data_rows  int
);

CREATE TABLE raw_record (
    record_id            bigserial PRIMARY KEY,
    run_id               int  NOT NULL REFERENCES ingest_run ON DELETE CASCADE,
    file_id              int  NOT NULL REFERENCES source_file ON DELETE CASCADE,
    source               text NOT NULL CHECK (source IN ('PAYMENT','SETTLEMENT')),
    line_no              int  NOT NULL,          -- 1-based physical line in the source file
    is_header            boolean NOT NULL DEFAULT false,  -- settlement summary row (total-amount)
    raw                  jsonb NOT NULL,         -- the untouched source row, header -> value
    -- normalised, typed projection used for matching
    settlement_id        text,
    transaction_type     text,                   -- payments: type, settlement: transaction-type
    description          text,                   -- payments: description, settlement: amount-description
    amount_type          text,                   -- settlement only
    transaction_type_key text,
    description_key      text,
    amount_type_key      text,
    txn_ref              text,                   -- order id
    sku                  text,
    posted_at            timestamptz,            -- UTC
    recon_date           date,                   -- UTC date used in record_ref
    transaction_status   text,                   -- payments: Released / Deferred; settlement: NULL
    net_amount           numeric(14,2),          -- payments: total, settlement: amount
    component_check_diff numeric(14,2),          -- payments: sum(components) - total (should be 0)
    record_ref           text,                   -- primary reconciliation key of the row
    UNIQUE (file_id, line_no)
);
CREATE INDEX raw_record_ref_idx ON raw_record(record_ref);
CREATE INDEX raw_record_src_idx ON raw_record(source, settlement_id, transaction_status);

-- One row per (raw row x amount component x applied rule).
CREATE TABLE record_line (
    line_id       bigserial PRIMARY KEY,
    run_id        int    NOT NULL REFERENCES ingest_run ON DELETE CASCADE,
    record_id     bigint NOT NULL REFERENCES raw_record ON DELETE CASCADE,
    source        text   NOT NULL,
    amount_field  text   NOT NULL,       -- payments column name / 'amount' for settlement
    amount        numeric(14,2) NOT NULL,
    rule_table    text,                  -- payment_mapping_config / settlement_mapping_config
    rule_id       int,                   -- NULL when no rule matched (UNMAPPED)
    match_tier    text,                  -- how the rule matched (exact / prefix / any / fallback)
    status        text NOT NULL CHECK (status IN ('MAPPED','UNMAPPED','MULTI_RULE')),
    record_ref    text,
    summary_field text,                  -- NULL = not summarised
    is_net        boolean NOT NULL       -- true for the line that carries the row's net amount
);
CREATE INDEX record_line_ref_idx ON record_line(record_ref);
CREATE INDEX record_line_rec_idx ON record_line(record_id);

-- Summary accumulated *while* ingesting (streamed, persisted per batch).
CREATE TABLE running_summary (
    run_id             int  NOT NULL REFERENCES ingest_run ON DELETE CASCADE,
    source             text NOT NULL,
    settlement_id      text NOT NULL,
    transaction_status text NOT NULL,
    summary_field      text NOT NULL,
    sign               text NOT NULL,
    amount             numeric(16,2) NOT NULL,
    line_count         int NOT NULL,
    PRIMARY KEY (run_id, source, settlement_id, transaction_status, summary_field, sign)
);

-- ---------------------------------------------------------------------
-- 3. Reconciliation output
-- ---------------------------------------------------------------------
CREATE TABLE recon_scope (
    settlement_id text PRIMARY KEY      -- settlements being reconciled
);

CREATE TABLE recon_result (
    record_ref        text PRIMARY KEY,
    status            text NOT NULL CHECK (status IN
                        ('RECONCILED','UNRECONCILED_PAYMENT','UNRECONCILED_SETTLEMENT')),
    in_scope          boolean NOT NULL,
    reason            text,
    payment_rows      int NOT NULL,
    settlement_rows   int NOT NULL,
    payment_net       numeric(14,2),
    settlement_net    numeric(14,2),
    diff              numeric(14,2)
);

-- Convenience view: every record line with its source row and summary line.
CREATE VIEW v_line_trace AS
SELECT l.line_id, l.source, l.record_ref, l.amount_field, l.amount, l.summary_field,
       sl.section, sl.label AS summary_line, l.rule_table, l.rule_id, l.match_tier, l.status,
       r.record_id, f.file_name, r.line_no, r.settlement_id, r.transaction_status,
       r.transaction_type, r.amount_type, r.description, r.txn_ref, r.sku, r.recon_date, r.raw
FROM record_line l
JOIN raw_record r  ON r.record_id = l.record_id
JOIN source_file f ON f.file_id = r.file_id
LEFT JOIN summary_field_map m
       ON m.summary_field = l.summary_field
      AND (m.sign = 'ANY' OR (m.sign = 'POS' AND l.amount > 0) OR (m.sign = 'NEG' AND l.amount < 0))
LEFT JOIN summary_line sl ON sl.line_code = m.line_code;
