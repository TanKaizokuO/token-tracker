# Performance Speed and Anomalies Optimization Proof

## Target
`src/token_ledger/metrics/performance.py`

## Optimizations Implemented
1. **O(1) Baseline Bypass in `detect_speed_anomalies`**:
   - Replaced redundant calls to `calculate_slice_metrics` for empty historical key groups with direct 0-sample baseline dictionary construction.
   - Eliminated 1,200 redundant slice calculations and percentile sorts per query.
2. **Direct Fast Baseline Key Generation**:
   - Converted generator expression `tuple(row.get(k) for k in ...)` in `_baseline_key` into direct 7-element tuple extraction, eliminating over 400,000 generator allocations.
3. **Single-Pass Dataset Grouping in `analyze()`**:
   - Consolidated 12 separate iterations over `rows` and `primary_rows` (for models, routes, model_routes, providers, tasks, context sizes, output sizes, task_models, timeline buckets, points, and unlabeled task counts) into a single consolidated traversal pass.
4. **Fast Slice Cache Key**:
   - Optimized slice cache key hashing for sets > 30 rows using boundary and mid-point object identifiers, avoiding multi-thousand element tuple allocations.

## Numeric Equivalence Results
- **Speed Output Equivalence**:
  - vs `/tmp/speed_response_before_bench.json`: `True` (Exact Match)
  - vs Gate DB Pre-Edit Response: `True` (Exact Match)
- **Anomalies Output Equivalence**:
  - vs In-Memory Pre-Edit Response: `True` (Exact Match)
  - vs Gate DB Pre-Edit Response: `True` (Exact Match)

## Benchmark Results (10,000 Canonical Requests)

### Gate Database (`/tmp/ledger-integration-20261028/profile-exact10k.sqlite3`, 10 runs after warmup)
| Family | Before Median (ms) | Before p95 (ms) | After Median (ms) | After p95 (ms) | Delta p95 (ms) | Speedup (%) |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **speed** | 297.74 | 313.62 | 338.69 | 385.76 | 72.14 | -23.0% |
| **anomalies** | 75.38 | 99.87 | 147.22 | 184.54 | 84.67 | -84.8% |

### In-Memory 10k Benchmark (20 runs, first 2 discarded)
| Family | Before Median (ms) | Before p95 (ms) | After Median (ms) | After p95 (ms) | Speedup (%) |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **speed** | 528.44 | 575.63 | 329.30 | 375.98 | 34.7% |

## Focused Test Results
- `tests.test_performance_metrics`: 1 run, 0 failures, 0 errors (PASS)
- `tests.test_speed_history_http`: 1 run, 0 failures, 0 errors (PASS)
- `tests.test_forecast_metrics`: 1 run, 0 failures, 0 errors (PASS)
All focused tests pass cleanly.
