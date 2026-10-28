-- Snapshot the currency and dated FX evidence used by each derived valuation.
-- Existing financial facts have unknown provenance until they are recalculated.
ALTER TABLE financial_facts ADD COLUMN valuation_provenance_json TEXT;
