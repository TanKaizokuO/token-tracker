# Token Ledger Rebuild — 76-Feature Acceptance and Reconciliation Matrix

**Status:** Scoped evidence only; not fully accepted. Legacy cutover is blocked.  
**Date:** 2026-10-27  
**Reference Specs:** `TOKEN_LEDGER_REBUILD_PLAN.md` (lines 130–208), `AI_Usage_Dashboard_76_Features.md`

Scoped status: 0 of 76 features are verified, 24 are partial, and 52 are pending. Each row's gap and cited runtime artifact is in `docs/rebuild-acceptance.md`. The earlier claim that all 76 features were implemented and verified is withdrawn. All evidence uses synthetic fixtures only.

Performance gate (10k canonical requests, p95 under 100 ms; threshold unchanged, not waived; PYTHONHASHSEED=0; source `/tmp/ledger-integration-20261028/gate-all-families-current.json`) is unmet for three families: traces 173.55 ms, model_profiles 136.17 ms, speed 134.55 ms. Outcomes (67.83 ms) and anomalies (61.32 ms) meet the 100 ms p95 gate in this PYTHONHASHSEED=0 measurement; their earlier readings (130.65 ms, 123.31 ms) did not reproduce and are unexplained. The three unmet families are not passing.

**Legacy cutover is blocked** pending full WP23 acceptance (`docs/rebuild-acceptance.md`).

---

## 76-Feature Reconciliation Table

| ID | Feature Title | Owner / Phase | Implementation File(s) | Test File(s) | Status |
|---:|---|---|---|---|:---:|
| 1 | Unified Usage Overview | WP17b / Phase 1 core | `src/token_ledger/metrics/tokens.py`, `src/token_ledger/query/engine.py`, `web/src/pages/Overview.tsx` | `tests/test_token_metrics.py`, `tests/test_query.py` | Pending |
| 2 | Interactive Token Consumption Timeline | WP10 / Phase 1 core | `src/token_ledger/metrics/tokens.py`, `src/token_ledger/query/engine.py`, `web/src/pages/Overview.tsx`, `web/src/components/ChartPanel.tsx` | `tests/test_token_metrics.py`, `tests/test_query.py` | Pending |
| 3 | Model Usage Distribution | WP10 / Phase 1 core | `src/token_ledger/metrics/models.py`, `src/token_ledger/query/engine.py`, `web/src/pages/Overview.tsx`, `web/src/pages/Models.tsx` | `tests/test_model_metrics.py`, `tests/test_query.py` | Pending |
| 4 | Oh My Pi Vs. Claude Code Comparison | WP17b / Phase 1 core | `src/token_ledger/metrics/tokens.py`, `src/token_ledger/metrics/models.py`, `web/src/pages/Overview.tsx`, `web/src/pages/Harness.tsx` | `tests/test_harness_comparison_http.py`, `tests/test_query.py` | Partial |
| 5 | Activity Heatmap And Usage Calendar | WP10 / Phase 1 core | `src/token_ledger/query/engine.py`, `web/src/pages/Overview.tsx` | `tests/test_query.py` | Pending |
| 6 | Global Filters And Saved Views | WP09 / Phase 1 core | `src/token_ledger/query/engine.py`, `src/token_ledger/api/server.py`, `web/src/App.tsx` | `tests/test_query.py`, `tests/test_api_security.py` | Partial |
| 7 | Complete Token Breakdown | WP12 / Phase 1 core | `src/token_ledger/metrics/tokens.py`, `web/src/pages/TokensCache.tsx` | `tests/test_token_metrics.py` | Pending |
| 8 | Cache Efficiency Dashboard | WP12 / Phase 1 core | `src/token_ledger/metrics/tokens.py`, `src/token_ledger/metrics/efficiency.py`, `web/src/pages/TokensCache.tsx` | `tests/test_token_metrics.py`, `tests/test_efficiency_metrics.py` | Pending |
| 9 | Cache-Write Economics And Break-Even Analysis | WP12 / Phase 1 core | `src/token_ledger/metrics/tokens.py`, `src/token_ledger/metrics/efficiency.py`, `web/src/pages/TokensCache.tsx` | `tests/test_token_metrics.py`, `tests/test_efficiency_metrics.py` | Pending |
| 10 | Context Window Utilization Monitor | WP15 / Phase 2 deep obs | `src/token_ledger/metrics/traces.py`, `web/src/pages/Sessions.tsx` | `tests/test_trace_metrics.py`, `tests/test_native_events.py` | Pending |
| 11 | Context Compaction Analytics | WP15 / Phase 2 deep obs | `src/token_ledger/metrics/traces.py`, `src/token_ledger/ingest/events.py`, `web/src/pages/Sessions.tsx` | `tests/test_trace_metrics.py`, `tests/test_native_events.py` | Partial |
| 12 | Context Growth And Repetition Analysis | WP15 / Phase 2 deep obs | `src/token_ledger/metrics/traces.py`, `src/token_ledger/metrics/efficiency.py`, `web/src/pages/Sessions.tsx`, `web/src/pages/Efficiency.tsx` | `tests/test_trace_metrics.py`, `tests/test_efficiency_metrics.py` | Pending |
| 13 | Output Verbosity And Reasoning Analysis | WP13b / Phase 1 core + ext | `src/token_ledger/metrics/models.py`, `src/token_ledger/metrics/tokens.py`, `web/src/pages/Models.tsx` | `tests/test_model_metrics.py`, `tests/test_token_metrics.py` | Pending |
| 14 | Token Consumption Per User Prompt | WP15 / Phase 2 deep obs | `src/token_ledger/metrics/traces.py`, `src/token_ledger/ingest/normalize.py`, `web/src/pages/Sessions.tsx` | `tests/test_trace_metrics.py`, `tests/test_normalization.py` | Pending |
| 15 | Individual Model Analytics Pages | WP13b / Phase 1 core + ext | `src/token_ledger/metrics/models.py`, `src/token_ledger/pricing.py`, `web/src/pages/Models.tsx` | `tests/test_model_metrics.py`, `tests/test_pricing.py` | Pending |
| 16 | Side-By-Side Model Comparison | WP13b / Phase 1 core + ext | `src/token_ledger/metrics/models.py`, `web/src/pages/Models.tsx` | `tests/test_model_metrics.py` | Pending |
| 17 | Model Switching Timeline | WP13a / Phase 1 core | `src/token_ledger/metrics/models.py`, `web/src/pages/Models.tsx`, `web/src/pages/Sessions.tsx` | `tests/test_model_metrics.py` | Pending |
| 18 | Model Performance By Task Type | WP18 / Phase 3 opt | `src/token_ledger/metrics/outcomes.py`, `src/token_ledger/outcomes.py`, `web/src/pages/Models.tsx` | `tests/test_outcomes.py` | Pending |
| 19 | Model Reliability And Failure Analysis | WP13a / Phase 1 core | `src/token_ledger/metrics/models.py`, `web/src/pages/Models.tsx` | `tests/test_model_metrics.py` | Pending |
| 20 | Model Routing Opportunity Detector | WP19 / Phase 3 opt | `src/token_ledger/metrics/efficiency.py`, `web/src/pages/Efficiency.tsx` | `tests/test_efficiency_metrics.py` | Pending |
| 21 | Provider And Access-Route Analytics | WP16 / Phase 2 deep obs | `src/token_ledger/metrics/providers.py`, `web/src/pages/Providers.tsx` | `tests/test_provider_metrics.py` | Pending |
| 22 | Actual Cost Vs. Api-Equivalent Cost | WP06 / Foundation | `src/token_ledger/pricing.py`, `src/token_ledger/metrics/providers.py`, `web/src/pages/Providers.tsx` | `tests/test_pricing.py`, `tests/test_pricing_http.py`, `tests/test_provider_metrics.py` | Pending |
| 23 | Subscription Quota And Usage-Limit Monitor | WP16 / Phase 2 deep obs | `src/token_ledger/metrics/providers.py`, `web/src/pages/Providers.tsx` | `tests/test_provider_metrics.py` | Pending |
| 24 | Token Burn-Rate Monitor | WP16 / Phase 2 deep obs | `src/token_ledger/metrics/providers.py`, `web/src/pages/Providers.tsx` | `tests/test_provider_metrics.py` | Pending |
| 25 | Usage Forecasting | WP20 / Phase 3 opt | `src/token_ledger/metrics/forecast.py`, `web/src/pages/Efficiency.tsx` | `tests/test_forecast_metrics.py` | Pending |
| 26 | Usage Budgets And Thresholds | WP16 / Phase 2 deep obs | `src/token_ledger/metrics/providers.py`, `src/token_ledger/api/server.py`, `web/src/pages/Providers.tsx` | `tests/test_provider_metrics.py`, `tests/test_api_security.py` | Pending |
| 27 | Project Usage Explorer | WP11 / Phase 1 core | `src/token_ledger/query/engine.py`, `web/src/pages/Projects.tsx` | `tests/test_query.py` | Partial |
| 28 | Advanced Session Explorer | WP11 / Phase 1 core | `src/token_ledger/query/engine.py`, `src/token_ledger/ingest/normalize.py`, `web/src/pages/Sessions.tsx` | `tests/test_query.py`, `tests/test_normalization.py` | Pending |
| 29 | Session Execution Timeline | WP15 / Phase 2 deep obs | `src/token_ledger/metrics/traces.py`, `src/token_ledger/ingest/events.py`, `web/src/pages/Sessions.tsx` | `tests/test_trace_metrics.py`, `tests/test_native_events.py` | Partial |
| 30 | Individual Request Inspector | WP11 / Phase 1 core | `src/token_ledger/metrics/traces.py`, `src/token_ledger/privacy.py`, `web/src/pages/Sessions.tsx` | `tests/test_trace_metrics.py`, `tests/test_privacy.py` | Partial |
| 31 | Most Expensive Sessions And Requests | WP11 / Phase 1 core | `src/token_ledger/query/engine.py`, `web/src/pages/Sessions.tsx`, `web/src/pages/Projects.tsx` | `tests/test_query.py` | Pending |
| 32 | Tool Usage And Overhead Analysis | WP15 / Phase 2 deep obs | `src/token_ledger/metrics/traces.py`, `src/token_ledger/ingest/events.py`, `web/src/pages/Sessions.tsx` | `tests/test_trace_metrics.py`, `tests/test_native_events.py` | Pending |
| 33 | Subagent Usage Analytics | WP15 / Phase 2 deep obs | `src/token_ledger/metrics/traces.py`, `src/token_ledger/ingest/events.py`, `web/src/pages/Sessions.tsx` | `tests/test_trace_metrics.py`, `tests/test_native_events.py` | Pending |
| 34 | Error, Retry And Loop Detection | WP15 / Phase 2 deep obs | `src/token_ledger/metrics/traces.py`, `src/token_ledger/metrics/efficiency.py`, `web/src/pages/Sessions.tsx`, `web/src/pages/Efficiency.tsx` | `tests/test_trace_metrics.py`, `tests/test_efficiency_metrics.py` | Pending |
| 35 | Coding Activity And Outcome Tracking | WP18 / Phase 3 opt | `src/token_ledger/outcomes.py`, `src/token_ledger/metrics/outcomes.py`, `web/src/components/OutcomeEditor.tsx`, `web/src/pages/Efficiency.tsx` | `tests/test_outcomes.py`, `tests/test_manual_facts_http.py` | Pending |
| 36 | Automatic Efficiency Insights | WP19 / Phase 3 opt | `src/token_ledger/metrics/efficiency.py`, `web/src/pages/Efficiency.tsx` | `tests/test_efficiency_metrics.py` | Partial |
| 37 | Token Inefficiency Breakdown | WP19 / Phase 3 opt | `src/token_ledger/metrics/efficiency.py`, `web/src/pages/Efficiency.tsx` | `tests/test_efficiency_metrics.py` | Pending |
| 38 | Token Savings Simulator | WP19 / Phase 3 opt | `src/token_ledger/metrics/efficiency.py`, `web/src/pages/Efficiency.tsx` | `tests/test_efficiency_metrics.py` | Partial |
| 39 | Conversation Restart And Compaction Recommendations | WP19 / Phase 3 opt | `src/token_ledger/metrics/efficiency.py`, `web/src/pages/Efficiency.tsx` | `tests/test_efficiency_metrics.py` | Pending |
| 40 | Repeated Content And File-Read Detector | WP15 / Phase 2 deep obs | `src/token_ledger/metrics/traces.py`, `src/token_ledger/privacy.py`, `web/src/pages/Sessions.tsx` | `tests/test_trace_metrics.py`, `tests/test_privacy.py` | Pending |
| 41 | Skills, Hooks And Mcp Usage Analysis | WP15 / Phase 2 deep obs | `src/token_ledger/metrics/traces.py`, `src/token_ledger/ingest/events.py`, `web/src/pages/Sessions.tsx` | `tests/test_trace_metrics.py`, `tests/test_native_events.py` | Pending |
| 42 | Before-And-After Efficiency Comparisons | WP20 / Phase 3 opt | `src/token_ledger/metrics/forecast.py`, `web/src/pages/Efficiency.tsx` | `tests/test_forecast_metrics.py` | Pending |
| 43 | Task Outcome And Model Effectiveness Scoring | WP18 / Phase 3 opt | `src/token_ledger/outcomes.py`, `src/token_ledger/metrics/outcomes.py`, `web/src/pages/Efficiency.tsx` | `tests/test_outcomes.py` | Pending |
| 44 | Customizable Dashboard Layouts | WP22 / Phase 3 opt | `src/token_ledger/api/server.py`, `web/src/components/LayoutControls.tsx` | `tests/test_api_security.py` | Partial |
| 45 | Automated Daily, Weekly And Monthly Reports | WP21 / Phase 3 opt | `src/token_ledger/export/reports.py`, `web/src/pages/Reports.tsx` | `tests/test_exports.py`, `tests/test_reports_http.py` | Partial |
| 46 | Usage Anomaly Detection | WP20 / Phase 3 opt | `src/token_ledger/metrics/forecast.py`, `web/src/pages/Efficiency.tsx` | `tests/test_forecast_metrics.py` | Partial |
| 47 | Live Active-Session Dashboard | WP16 / Phase 2 deep obs | `src/token_ledger/metrics/providers.py`, `src/token_ledger/ingest/runner.py`, `web/src/pages/Providers.tsx` | `tests/test_provider_metrics.py`, `tests/test_ingestion.py` | Pending |
| 48 | Advanced Exploration And Chart Interactions | WP22 / Phase 3 opt | `web/src/components/ChartPanel.tsx`, `web/src/components/DataTable.tsx`, `web/src/App.tsx` | `tests/test_query.py` | Pending |
| 49 | Data Export And Shareable Reports | WP21 / Phase 3 opt | `src/token_ledger/export/snapshot.py`, `src/token_ledger/export/reports.py`, `web/src/pages/Reports.tsx` | `tests/test_exports.py`, `tests/test_reports_http.py` | Partial |
| 50 | Data Completeness And Historical Coverage | WP05 / Foundation | `src/token_ledger/ingest/archive.py`, `web/src/pages/Overview.tsx`, `web/src/pages/Providers.tsx` | `tests/test_archive.py` | Partial |
| 51 | Source Sync And Collection Health | WP04 / Foundation | `src/token_ledger/ingest/runner.py`, `web/src/pages/Providers.tsx` | `tests/test_ingestion.py` | Partial |
| 52 | Pricing And Model Identity Management | WP06 / Foundation | `src/token_ledger/pricing.py`, `web/src/pages/Reports.tsx` | `tests/test_pricing.py`, `tests/test_pricing_http.py` | Partial |
| 53 | Transparent Metric Definitions | WP08 / Foundation | `src/token_ledger/query/engine.py`, `web/src/components/ChartPanel.tsx`, `web/src/App.tsx` | `tests/test_query.py` | Pending |
| 54 | Local-First Privacy Controls | WP07 / Foundation | `src/token_ledger/privacy.py`, `web/src/pages/Reports.tsx` | `tests/test_privacy.py` | Partial |
| 55 | Generation Speed | WP14a / Phase 1 core | `src/token_ledger/metrics/performance.py`, `web/src/pages/Speed.tsx` | `tests/test_performance_metrics.py`, `tests/test_speed_history_http.py` | Partial |
| 56 | Time to First Token (TTFT) | WP14b / Phase 2 deep obs | `src/token_ledger/metrics/performance.py`, `web/src/pages/Speed.tsx` | `tests/test_performance_metrics.py`, `tests/test_speed_history_http.py` | Pending |
| 57 | Speed Comparison | WP14b / Phase 2 deep obs | `src/token_ledger/metrics/performance.py`, `web/src/pages/Speed.tsx` | `tests/test_performance_metrics.py` | Pending |
| 58 | Speed Timeline | WP14b / Phase 2 deep obs | `src/token_ledger/metrics/performance.py`, `web/src/pages/Speed.tsx` | `tests/test_performance_metrics.py`, `tests/test_speed_history_http.py` | Pending |
| 59 | Speed Distribution | WP14b / Phase 2 deep obs | `src/token_ledger/metrics/performance.py`, `web/src/pages/Speed.tsx` | `tests/test_performance_metrics.py` | Pending |
| 60 | Speed by Task | WP14c / Phase 2 deep obs | `src/token_ledger/metrics/performance.py`, `src/token_ledger/metrics/outcomes.py`, `web/src/pages/Speed.tsx` | `tests/test_performance_metrics.py`, `tests/test_outcomes.py` | Pending |
| 61 | Speed vs. Context Size | WP14c / Phase 2 deep obs | `src/token_ledger/metrics/performance.py`, `web/src/pages/Speed.tsx` | `tests/test_performance_metrics.py` | Pending |
| 62 | Speed vs. Token Usage | WP14b / Phase 2 deep obs | `src/token_ledger/metrics/performance.py`, `web/src/pages/Speed.tsx` | `tests/test_performance_metrics.py` | Pending |
| 63 | Latency Breakdown | WP14b / Phase 2 deep obs | `src/token_ledger/metrics/performance.py`, `web/src/pages/Speed.tsx` | `tests/test_performance_metrics.py` | Pending |
| 64 | Speed Anomalies | WP20 / Phase 3 opt | `src/token_ledger/metrics/forecast.py`, `web/src/pages/Speed.tsx` | `tests/test_forecast_metrics.py` | Partial |
| 65 | Harness Selector | WP17a / Phase 1 core | `src/token_ledger/query/engine.py`, `web/src/App.tsx` | `tests/test_query.py` | Pending |
| 66 | Harness Overview | WP17b / Phase 1 core | `src/token_ledger/metrics/tokens.py`, `web/src/pages/Harness.tsx` | `tests/test_harness_comparison_http.py`, `tests/test_token_metrics.py` | Partial |
| 67 | Harness Model Usage | WP17b / Phase 1 core | `src/token_ledger/metrics/models.py`, `web/src/pages/Harness.tsx` | `tests/test_harness_comparison_http.py`, `tests/test_model_metrics.py` | Partial |
| 68 | Harness Token Breakdown | WP17b / Phase 1 core | `src/token_ledger/metrics/tokens.py`, `web/src/pages/Harness.tsx` | `tests/test_harness_comparison_http.py`, `tests/test_token_metrics.py` | Pending |
| 69 | Harness Speed Comparison | WP17c / Phase 2 deep obs | `src/token_ledger/metrics/performance.py`, `web/src/pages/Harness.tsx` | `tests/test_harness_comparison_http.py`, `tests/test_performance_metrics.py` | Pending |
| 70 | Harness Cache Comparison | WP17b / Phase 1 core | `src/token_ledger/metrics/tokens.py`, `web/src/pages/Harness.tsx` | `tests/test_harness_comparison_http.py`, `tests/test_token_metrics.py` | Pending |
| 71 | Harness Session Analytics | WP17b / Phase 1 core | `src/token_ledger/query/engine.py`, `web/src/pages/Harness.tsx` | `tests/test_harness_comparison_http.py`, `tests/test_query.py` | Pending |
| 72 | Harness Tool Analytics | WP17c / Phase 2 deep obs | `src/token_ledger/metrics/traces.py`, `web/src/pages/Harness.tsx` | `tests/test_harness_comparison_http.py`, `tests/test_trace_metrics.py` | Pending |
| 73 | Harness Efficiency | WP18 / Phase 3 opt | `src/token_ledger/metrics/outcomes.py`, `src/token_ledger/metrics/efficiency.py`, `web/src/pages/Harness.tsx` | `tests/test_harness_comparison_http.py`, `tests/test_outcomes.py` | Partial |
| 74 | Harness Reliability | WP17c / Phase 2 deep obs | `src/token_ledger/metrics/models.py`, `web/src/pages/Harness.tsx` | `tests/test_harness_comparison_http.py`, `tests/test_model_metrics.py` | Partial |
| 75 | Harness Usage Trends | WP17b / Phase 1 core | `src/token_ledger/metrics/tokens.py`, `web/src/pages/Harness.tsx` | `tests/test_harness_comparison_http.py`, `tests/test_token_metrics.py` | Partial |
| 76 | Harness Comparison Dashboard | WP17c / Phase 1+2+3 | `src/token_ledger/query/engine.py`, `web/src/pages/Harness.tsx` | `tests/test_harness_comparison_http.py`, `tests/test_query.py` | Partial |
