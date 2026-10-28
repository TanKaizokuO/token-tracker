# Usage Forecasting, Anomalies, and Efficiency Comparisons (WP20)

This document specifies the architecture, statistical methods, sample adequacy policies, and public schemas for WP20 analytics in Token Ledger.

---

## 1. Overview & Rebuild Plan Features

WP20 implements four core features from `TOKEN_LEDGER_REBUILD_PLAN.md`:
1. **Feature 25: Usage Forecasting:** Remainder-of-day/week/month token, per-model, and equivalent-cost run-rate projections with declared empirical uncertainty intervals and rolling walk-forward backtests.
2. **Feature 46: Usage Anomaly Detection:** Outlier detection on token volumes, cache write surges, cache hit rate collapse, and failure spikes using robust median and Median Absolute Deviation (MAD) baselines.
3. **Feature 64: Speed Anomalies:** Latency spikes (TTFT) and throughput degradation (generation TPS drops) against model/route baselines with concrete evidence linking.
4. **Feature 42: Before-And-After Efficiency Comparisons:** Two-period observational comparisons across timezone-aware ranges reusing canonical query filtering, with confounder labeling (model shift, task shift, DST clock transitions) and strict non-causal disclaimers.

---

## 2. Statistical Methodology

### 2.1 Velocity & Uncertainty Intervals
- **Recent Velocity:** Linearly weighted daily run-rate over the last 7 active calendar days.
- **Longer Velocity:** Arithmetic daily mean across all historical active days.
- **Uncertainty Intervals:** Declared interval method `empirical_residual_spread` based on rolling walk-forward residual standard deviation:
  $$\text{interval} = \text{expected_point} \pm 1.96 \cdot \sigma_{\text{residual}} \cdot \sqrt{\text{days_ahead}}$$
- **No Invented Confidence:** Percentages are never fabricated; interval methodology and empirical residual spreads are explicitly declared.
- **Coverage Basis:** Forecast velocities and backtests use request-bearing local calendar dates only. A recorded request with zero tokens is an observed activity day; missing dates between observations remain unknown and are never imputed as zero. Without complete-source coverage records, no no-request day is confirmed as zero activity.
- **Elapsed-Day Basis:** Partial-day forecasts measure elapsed time between local midnights after converting both instants to UTC. A DST transition day therefore uses its actual 23- or 25-hour duration, not a fixed 24-hour divisor.

### 2.2 Walk-Forward Backtesting
- Employs a rolling 1-step walk-forward backtest (`rolling_walk_forward_1step`) over historical daily totals.
- Computes Mean Absolute Error (MAE), Mean Absolute Percentage Error (MAPE), and residual standard deviation.
- **Residual Sufficiency:** Backtest metrics can be eligible with one walk-forward residual, but uncertainty intervals require at least 2 residual samples (`MIN_UNCERTAINTY_BACKTEST_SAMPLES`). Below that threshold, the point forecast/backtest may remain available while its interval abstains.

### 2.3 Robust Anomaly Baselines
- Uses Median and Median Absolute Deviation (MAD):
  $$\text{MAD} = \text{median}(|x_i - \text{median}(x)|)$$
  $$\text{Cutoff} = \text{median} \pm k \cdot \max(\text{MAD}, 0.20 \cdot \text{median})$$
- Default multiplier $k = 3.0$.
- Resilient against past outliers and prevents threshold distortion.
- User-declared expected states from query settings or `manual_settings_and_outcomes` are distinctly separated from unexpected operational anomalies.

### 2.4 Sample Adequacy and Pricing Policies
- **Forecasting:** Minimum 3 observed activity days (`MIN_FORECAST_ACTIVE_DAYS`). Sparse history returns `eligible: False`, `status: "abstain"`. A zero-token request still counts as an observed day; a missing date does not.
- **Anomalies:** Minimum 5 baseline measurements (`MIN_ANOMALY_BASELINE_SAMPLES`). Slices with fewer samples abstain.
- **Uncertainty:** A backtest with fewer than 2 residuals reports its metrics where possible, but uncertainty intervals abstain rather than deriving a spread from inadequate samples.
- **Partial Pricing:** Unpriced requests are never treated as $0.00. With priced history, the service may report an explicitly scoped known-priced-subset estimate, but full-scope cost projections abstain and carry a partial-pricing status. With no priced history, cost projection values are unavailable (`null`) and cost intervals abstain.

### 2.5 Query Windows, Comparisons, and Timezones
- **Forecast Observation Window:** An explicit `history_window` constrains rows used for forecast history independently of the envelope's outer `time_range`. The response returns both the normalized outer query and the normalized history window. All provider/application/harness filters, archive inclusion, and search settings are retained in the history query.
- **Comparison Filters:** Both before-and-after periods reuse the same validated global filters and differ only by their half-open period ranges. Periods must use the same timezone; durations are calculated from UTC instants.
- **DST Comparisons:** Date-only local period boundaries resolve to local midnight and then UTC. A local DST transition day therefore has a measured duration of 82,800 or 90,000 seconds (23 or 25 hours), and its duration in days is computed from those actual instants.

---

## 3. Public API Contract (`local://token-ledger-forecast-contract.md`)

Exported from `token_ledger.metrics.forecast`:
- `analyze(conn, envelope) -> Dict[str, Any]`
- `forecast_usage(rows, tz_name, as_of) -> Dict[str, Any]`
- `detect_anomalies(conn, rows, envelope, threshold_multiplier) -> Dict[str, Any]`
- `compare_periods(conn, base_envelope, period_a, period_b) -> Dict[str, Any]`
- `walk_forward_backtest(daily_values, min_train_days, window_size) -> Dict[str, Any]`

## 4. Efficiency UI Interpretation

- Forecast horizon controls display the existing engine's day, week, or month point projections, intervals, per-model results, and backtests. The editable history window sets `history_window` independently of the global range; shared search and filters are retained. Cost projections use engine pricing coverage: a known-priced subset is explicitly scoped, while full-scope cost abstains when pricing is partial or unavailable.
- Anomaly results expose the baseline method, sample size, median/MAD, effective threshold, and request evidence. A user's expected-state note is persisted in the `efficiency_expected_anomalies` setting and passed back to the anomaly engine; this labels an expected observation, it does not erase its evidence or assert that the underlying measurement is harmless.
- Before-and-after controls provide two labelled, half-open date ranges. Both periods use the same global filters, and the UI displays available usage, cost, performance, and task-outcome metrics with the engine's match status and confounder warnings. These are observational comparisons, not causal estimates.
- Efficiency signals distinguish measured signal-associated token volume from counterfactual modeled avoidability; neither makes observed usage a ground-truth waste label. Trace recommendations link to supporting sessions/events, and missing context-window evidence remains unavailable rather than being inferred from cumulative input tokens.
- Counterfactual scenario controls use the existing pricing engine and effective provider/model/route schedules. Missing schedules or unknown usage keep costs unknown, never zero.
