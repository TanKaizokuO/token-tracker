import React, { useCallback, useEffect, useState } from "react";
import type { PageProps, QueryResponse } from "../App";

interface SignalBreakdown {
  requests_count: number;
  observed_tokens: number;
  avoidable_tokens: number;
  is_additive?: boolean;
}

interface EvidenceItem {
  signal: string;
  basis: string;
  canonical_id: string;
  reason: string;
  observed_tokens: number;
  avoidable_tokens: number;
  event_ids?: string[];
}

interface Insight {
  id: string;
  category: string;
  title: string;
  priority: "high" | "medium" | "low";
  confidence: "high" | "medium" | "low";
  basis: string;
  observed_tokens: number;
  avoidable_tokens: number;
  requests_count: number;
  description: string;
  actionable_recommendation: string;
  causality_disclaimer: string;
  evidence_sample?: EvidenceItem[];
}

interface RoutingAlternative {
  source_model: string;
  recommended_target: string;
  recommendation_type: "validated_routing" | "experiment_suggestion";
  adequate_sample: boolean;
  completed_sample_count: number;
  sample_threshold: number;
  confidence: string;
  basis: string;
  rationale: string;
  causality_disclaimer: string;
  experiment_design?: {
    sample_size: number;
    target_task_types: string[];
    success_criteria: string;
  };
}

interface ScenarioTokenTotals {
  fresh_input_tokens: number | null;
  cache_read_tokens: number | null;
  cache_write_5m_tokens: number | null;
  cache_write_1h_tokens: number | null;
  cache_write_unknown_ttl_tokens: number | null;
  output_tokens: number | null;
  total_tokens: number | null;
}

interface PricingEvidence {
  role: string;
  provider: string;
  model: string;
  requested_route: string;
  rate_route: string | null;
  pricing_version_id: string | null;
  effective_start: string | null;
  effective_end: string | null;
  pricing_hash: string | null;
  rate_id: number | null;
  rate_status: string;
  source_url: string | null;
  verified_on: string | null;
  reason: string | null;
  request_count: number;
}

interface ScenarioResult {
  scenario_type: string;
  is_priced: boolean;
  request_count: number;
  query_filtered_request_count: number;
  baseline_tokens: ScenarioTokenTotals;
  projected_tokens: ScenarioTokenTotals;
  token_reduction: number | null;
  token_reduction_pct: number | null;
  financials: {
    baseline_cost_usd: number | null;
    projected_cost_usd: number | null;
    projected_savings_usd: number | null;
    unpriced_tokens: number | null;
  };
  pricing_coverage: {
    baseline_priced_requests: number;
    projected_priced_requests: number;
    unknown_usage_counters: string[];
    unknown_ttl_tokens: number | null;
  };
  pricing_evidence: PricingEvidence[];
  assumptions: string[];
  disclaimer: string;
  warnings: string[];
}
interface ForecastInterval {
  eligible?: boolean;
  status?: string;
  lower_bound?: number;
  expected_point?: number;
  upper_bound?: number;
  method?: string;
  samples_evaluated?: number;
  abstention_reason?: string | null;
  cost_projection_scope?: string;
  cost_projection_is_full_scope?: boolean;
}

interface ForecastPoint {
  expected_tokens?: number;
  expected_remainder_tokens?: number;
  projected_day_total_tokens?: number;
  projected_week_total_tokens?: number;
  projected_month_total_tokens?: number;
  uncertainty_interval?: ForecastInterval;
  projected_day_cost_usd?: number | null;
  projected_week_cost_usd?: number | null;
  projected_month_cost_usd?: number | null;
  cost_uncertainty_interval?: ForecastInterval;
}

interface ModelForecast {
  observed_activity_days?: number;
  projected_day_tokens?: number;
  projected_week_tokens?: number;
  projected_month_tokens?: number;
  projected_day_cost_usd?: number | null;
  projected_week_cost_usd?: number | null;
  projected_month_cost_usd?: number | null;
  uncertainty_intervals?: Record<string, ForecastInterval>;
  cost_projection_scope?: string;
}

interface ForecastValues extends Record<string, unknown> {
  remainder_of_day?: ForecastPoint;
  remainder_of_week?: ForecastPoint;
  remainder_of_month?: ForecastPoint;
  per_model?: Record<string, ModelForecast>;
}

interface ForecastBacktest {
  eligible?: boolean;
  method?: string;
  samples_evaluated?: number;
  mae?: number | null;
  mape?: number | null;
  residual_stddev?: number | null;
  uncertainty_eligible?: boolean;
  abstention_reason?: string | null;
  uncertainty_abstention_reason?: string | null;
}

interface ForecastResponse extends Omit<QueryResponse, "values" | "eligible"> {
  values?: ForecastValues;
  eligible?: boolean;
  status?: string;
  abstention_reason?: string | null;
  velocity?: { method?: string; recent_daily_tokens?: number; recent_daily_usd?: number | null };
  active_days_observed?: number;
  history_window?: { start?: string | null; end?: string | null; timezone?: string; basis?: string };
  backtest?: ForecastBacktest;
  cost_backtest?: ForecastBacktest;
  coverage?: Record<string, unknown> & {
    activity_days_observed?: number;
    unknown_days_between_activity_days?: number;
    priced_requests_count?: number;
    unpriced_requests_count?: number;
    cost_projection_scope?: string;
    cost_projection_is_full_scope?: boolean;
    full_scope_cost_abstention_reason?: string;
  };
}

interface ExpectedAnomalyState {
  id: string;
  notes: string;
}

interface AnomalyBaseline extends Record<string, unknown> {
  eligible?: boolean;
  sample_size?: number;
  median?: number;
  mad?: number;
  cutoff?: number;
  drop_cutoff?: number;
  spike_cutoff?: number;
  method?: string;
  threshold_multiplier?: number;
  abstention_reason?: string;
}

interface AnomalyRow extends Record<string, unknown> {
  canonical_id: string;
  session_id?: string | null;
  request_timestamp?: string;
  anomaly_type: string;
  metric_name: string;
  observed_value?: number | string;
  baseline_median?: number;
  baseline_mad?: number;
  threshold_cutoff?: number;
  is_expected?: boolean;
  expected_state?: { notes?: string; source?: string };
  evidence?: Record<string, unknown>;
}

interface AnomalyResponse extends Omit<QueryResponse, "values" | "rows"> {
  values?: Record<string, unknown> & {
    total_audited?: number;
    total_anomalies?: number;
    unexpected_anomalies?: number;
    expected_anomalies?: number;
  };
  rows?: AnomalyRow[];
  baselines?: Record<string, AnomalyBaseline>;
}

interface PeriodMetrics extends Record<string, unknown> {
  requests_count?: number;
  total_tokens?: number;
  tokens_per_request?: number;
  cache_hit_rate?: number;
  api_equivalent_usd?: number;
  generation_speed_tps_weighted?: number | null;
  ttft_ms_mean?: number | null;
  duration_ms_mean?: number | null;
  retry_rate?: number;
  outcomes?: {
    total_tasks_recorded?: number;
    completed_tasks?: number;
    completion_rate?: number | null;
    manual_rating_mean?: number | null;
  };
}

interface ComparisonDelta {
  delta?: number | null;
  pct_change?: number | null;
}

interface ComparisonPeriodResult {
  label?: string;
  start?: string;
  end?: string;
  metrics?: PeriodMetrics;
}

interface ComparisonResponse extends Omit<QueryResponse, "values"> {
  values?: Record<string, ComparisonDelta>;
  period_a?: ComparisonPeriodResult;
  period_b?: ComparisonPeriodResult;
  matched_status?: string;
  confounders?: string[];
  causal_claim_disclaimer?: string;
}

interface TraceEvent {
  event_id: string;
  session_id?: string | null;
  timestamp?: string | null;
  started_at?: string | null;
  event_kind?: string;
  event_name?: string | null;
}

interface CompactionTraceEvent {
  event_id: string;
  session_id?: string | null;
  timestamp?: string | null;
  before_tokens?: number | null;
  after_tokens?: number | null;
  reduction_tokens?: number | null;
  turns_compacted?: number | null;
}

interface TraceResponse extends Omit<QueryResponse, "values"> {
  values?: Record<string, unknown> & {
    avg_context_occupancy_ratio?: number | null;
    max_context_occupancy_ratio?: number | null;
    unobserved_window_requests_count?: number;
  };
  events?: TraceEvent[];
  compaction?: { status?: string; compaction_events_count?: number; total_reduction_tokens?: number | null; events?: CompactionTraceEvent[] };
  context_occupancy?: {
    avg_context_occupancy_ratio?: number | null;
    peak_occupancy_ratio?: number | null;
    unobserved_window_requests_count?: number;
  };
  missingness?: { unobserved_context_window_requests_count?: number };
}

const EXPECTED_ANOMALIES_SETTING_KEY = "efficiency_expected_anomalies";

const formatCount = (value: unknown): string =>
  typeof value === "number" && Number.isFinite(value) ? Math.round(value).toLocaleString() : "Unavailable";

const formatDecimal = (value: unknown, digits = 2): string =>
  typeof value === "number" && Number.isFinite(value) ? value.toLocaleString(undefined, { maximumFractionDigits: digits }) : "Unavailable";

const formatUsd = (value: unknown): string =>
  typeof value === "number" && Number.isFinite(value) ? `$${value.toFixed(4)}` : "Unavailable";

const formatPercent = (value: unknown): string =>
  typeof value === "number" && Number.isFinite(value) ? `${(value * 100).toFixed(1)}%` : "Unavailable";

const formatInterval = (interval?: ForecastInterval): string => {
  if (!interval?.eligible) {
    return `Abstain${interval?.abstention_reason ? `: ${interval.abstention_reason}` : ""}`;
  }
  return `${formatCount(interval.lower_bound)} – ${formatCount(interval.upper_bound)} tokens (${interval.samples_evaluated ?? 0} backtest samples)`;
};
type ComparisonDisplayFormat = "count" | "usd" | "decimal" | "rate";

const formatComparisonValue = (value: unknown, format: ComparisonDisplayFormat): string => {
  if (typeof value !== "number" || !Number.isFinite(value)) return "Unavailable";
  if (format === "count") return formatCount(value);
  if (format === "usd") return formatUsd(value);
  if (format === "rate") return formatPercent(value);
  return formatDecimal(value);
};

const formatComparisonDelta = (value: unknown, format: ComparisonDisplayFormat): string => {
  if (typeof value !== "number" || !Number.isFinite(value)) return "Unavailable";
  const sign = value > 0 ? "+" : "";
  if (format === "rate") return `${sign}${(value * 100).toFixed(1)} pp`;
  if (format === "usd") return `${sign}${formatUsd(value)}`;
  if (format === "count") return `${sign}${formatCount(value)}`;
  return `${sign}${formatDecimal(value)}`;
};

interface EfficiencyValues {
  efficiency_ratio: number;
  observed_tokens: number;
  avoidable_tokens: number;
  flagged_requests_count: number;
  total_requests: number;
}

type EfficiencyResponse = Omit<QueryResponse, 'values'> & {
  values?: EfficiencyValues;
  signals?: { exclusive_breakdown?: Record<string, SignalBreakdown> };
  insights?: Insight[];
  routing_alternatives?: RoutingAlternative[];
  compaction_recommendation?: {
    status: string;
    reason: string;
    basis: string;
    actionable?: boolean;
    sessions_eligible?: number;
    disclaimer?: string;
  };
  scenarios?: ScenarioResult[];
};

export const Efficiency: React.FC<PageProps> = ({ context }) => {
  const [loading, setLoading] = useState<boolean>(true);
  const [selectedInsight, setSelectedInsight] = useState<Insight | null>(null);

  const [data, setData] = useState<EfficiencyResponse | null>(null);
  const [forecasts, setForecasts] = useState<ForecastResponse | null>(null);
  const [anomalies, setAnomalies] = useState<AnomalyResponse | null>(null);
  const [traces, setTraces] = useState<TraceResponse | null>(null);
  const [comparison, setComparison] = useState<ComparisonResponse | null>(null);
  const [forecastHorizon, setForecastHorizon] = useState<"day" | "week" | "month">("day");
  const [historyStart, setHistoryStart] = useState("");
  const [historyEnd, setHistoryEnd] = useState("");
  const [appliedHistoryWindow, setAppliedHistoryWindow] = useState<{ start?: string; end?: string }>({});
  const [thresholdMultiplier, setThresholdMultiplier] = useState(3);
  const [appliedThresholdMultiplier, setAppliedThresholdMultiplier] = useState(3);
  const [expectedAnomalies, setExpectedAnomalies] = useState<ExpectedAnomalyState[]>([]);
  const [expectedSettingsLoaded, setExpectedSettingsLoaded] = useState(false);
  const [expectedSettingsError, setExpectedSettingsError] = useState<string | null>(null);
  const [expectedNotes, setExpectedNotes] = useState<Record<string, string>>({});
  const [savingExpectedId, setSavingExpectedId] = useState<string | null>(null);
  const [beforeStart, setBeforeStart] = useState("");
  const [beforeEnd, setBeforeEnd] = useState("");
  const [afterStart, setAfterStart] = useState("");
  const [afterEnd, setAfterEnd] = useState("");
  const [changeLabel, setChangeLabel] = useState("");
  const [comparisonLoading, setComparisonLoading] = useState(false);
  const [comparisonError, setComparisonError] = useState<string | null>(null);
  const [scenarioType, setScenarioType] = useState<string>("cache-read");
  const [cacheExpansionPct, setCacheExpansionPct] = useState<number>(50);
  const [outputReductionPct, setOutputReductionPct] = useState<number>(25);
  const [contextThreshold, setContextThreshold] = useState<number>(50000);
  const [sourceModel, setSourceModel] = useState<string>("");
  const [targetModel, setTargetModel] = useState<string>("");
  const [sourceProvider, setSourceProvider] = useState<string>("");
  const [sourceRoute, setSourceRoute] = useState<string>("");
  const [targetProvider, setTargetProvider] = useState<string>("");
  const [targetRoute, setTargetRoute] = useState<string>("");
  const [activeScenario, setActiveScenario] = useState<ScenarioResult | null>(null);

  const fetchEfficiencyData = useCallback(
    async (scenarioInputs?: Record<string, string | number>) => {
      setLoading(true);
      const efficiencyResponse = await context.request<EfficiencyResponse>(
        "efficiency",
        scenarioInputs ? { scenario_inputs: scenarioInputs } : undefined,
      );
      setData(efficiencyResponse);
      setActiveScenario(efficiencyResponse.scenarios?.[0] ?? null);
      setLoading(false);
    },
    [context.request],
  );

  const fetchForecasts = useCallback(async () => {
    const historyWindow = appliedHistoryWindow.start || appliedHistoryWindow.end
      ? { ...appliedHistoryWindow, timezone: context.query.time_range.timezone }
      : undefined;
    const response = await context.request<ForecastResponse>(
      "forecasts",
      historyWindow ? { history_window: historyWindow } : undefined,
    );
    setForecasts(response);
  }, [appliedHistoryWindow, context.query.time_range.timezone, context.request]);

  const fetchAnomalies = useCallback(async () => {
    const response = await context.request<AnomalyResponse>("anomalies", {
      threshold_multiplier: appliedThresholdMultiplier,
      anomaly_settings: { expected_anomalies: expectedAnomalies },
    });
    setAnomalies(response);
  }, [appliedThresholdMultiplier, context.request, expectedAnomalies]);

  const fetchTraces = useCallback(async () => {
    const response = await context.request<TraceResponse>("traces");
    setTraces(response);
  }, [context.request]);

  useEffect(() => {
    let active = true;
    void (async () => {
      try {
        const response = await fetch("/api/v1/settings", { credentials: "same-origin" });
        if (!response.ok) throw new Error(`Settings request failed: HTTP ${response.status}`);
        const body = (await response.json()) as {
          settings?: Array<{ setting_key?: string; setting_value?: string }>;
        };
        const saved = body.settings?.find((setting) => setting.setting_key === EXPECTED_ANOMALIES_SETTING_KEY);
        const parsed: unknown = saved?.setting_value ? JSON.parse(saved.setting_value) : [];
        const normalized = Array.isArray(parsed)
          ? parsed.filter(
              (item): item is ExpectedAnomalyState =>
                typeof item?.id === "string" && typeof item?.notes === "string",
            )
          : [];
        if (active) {
          setExpectedAnomalies(normalized);
          setExpectedNotes(Object.fromEntries(normalized.map((item) => [item.id, item.notes])));
          setExpectedSettingsError(null);
        }
      } catch (error) {
        if (active) {
          setExpectedSettingsError(error instanceof Error ? error.message : "Unable to load expected anomaly settings.");
        }
      } finally {
        if (active) setExpectedSettingsLoaded(true);
      }
    })();
    return () => {
      active = false;
    };
  }, []);

  const saveExpectedAnomaly = useCallback(
    async (id: string, expected: boolean) => {
      const current = expectedAnomalies.find((item) => item.id === id);
      const notes = expectedNotes[id]?.trim() || current?.notes || "";
      const next = expected
        ? [...expectedAnomalies.filter((item) => item.id !== id), { id, notes }]
        : expectedAnomalies.filter((item) => item.id !== id);
      setSavingExpectedId(id);
      try {
        await context.write("settings", {
          setting_key: EXPECTED_ANOMALIES_SETTING_KEY,
          setting_value: JSON.stringify(next),
          description: "User-marked expected anomaly states",
          opt_in_enabled: false,
        });
        setExpectedAnomalies(next);
        setExpectedSettingsError(null);
      } catch (error) {
        setExpectedSettingsError(error instanceof Error ? error.message : "Unable to save expected anomaly state.");
      } finally {
        setSavingExpectedId(null);
      }
    },
    [context.write, expectedAnomalies, expectedNotes],
  );

  const runComparison = useCallback(async () => {
    setComparisonLoading(true);
    setComparisonError(null);
    const response = await context.request<ComparisonResponse>("comparisons", {
      comparison_periods: {
        period_a: {
          start: beforeStart,
          end: beforeEnd,
          timezone: context.query.time_range.timezone,
          label: "Before",
        },
        period_b: {
          start: afterStart,
          end: afterEnd,
          timezone: context.query.time_range.timezone,
          label: changeLabel.trim() || "After",
        },
      },
    });
    setComparison(response.error ? null : response);
    setComparisonError(response.error ?? null);
    setComparisonLoading(false);
  }, [
    afterEnd,
    afterStart,
    beforeEnd,
    beforeStart,
    changeLabel,
    context.query.time_range.timezone,
    context.request,
  ]);

  const queryKey = JSON.stringify(context.query);
  useEffect(() => {
    void fetchEfficiencyData();
    void fetchForecasts();
    void fetchTraces();
  }, [fetchEfficiencyData, fetchForecasts, fetchTraces, queryKey]);

  useEffect(() => {
    if (expectedSettingsLoaded) void fetchAnomalies();
  }, [expectedSettingsLoaded, fetchAnomalies, queryKey]);

  const runScenario = () => {
    const scenarioInputs: Record<string, string | number> = { scenario_type: scenarioType };
    if (sourceModel.trim()) scenarioInputs.source_model = sourceModel.trim();
    if (sourceProvider.trim()) scenarioInputs.source_provider = sourceProvider.trim();
    if (sourceRoute.trim()) scenarioInputs.source_route = sourceRoute.trim();
    if (scenarioType === "cache-read") scenarioInputs.cache_expansion_pct = cacheExpansionPct;
    if (scenarioType === "output-reduction") scenarioInputs.output_reduction_pct = outputReductionPct;
    if (scenarioType === "longcontext") scenarioInputs.context_threshold_tokens = contextThreshold;
    if (scenarioType === "model-switch") {
      if (targetModel.trim()) scenarioInputs.target_model = targetModel.trim();
      if (targetProvider.trim()) scenarioInputs.target_provider = targetProvider.trim();
      if (targetRoute.trim()) scenarioInputs.target_route = targetRoute.trim();
    }
    void fetchEfficiencyData(scenarioInputs);
  };

  if (loading) {
    return <div className="p-8 text-neutral-400">Loading efficiency analytics...</div>;
  }

  const values = data?.values ?? {
    efficiency_ratio: 0,
    observed_tokens: 0,
    avoidable_tokens: 0,
    flagged_requests_count: 0,
    total_requests: 0,
  };
  const exclusiveSignals = data?.signals?.exclusive_breakdown ?? {};
  const insights: Insight[] = data?.insights ?? [];
  const routingAlts: RoutingAlternative[] = data?.routing_alternatives ?? [];
  const compaction = data?.compaction_recommendation;
  const signalAssociatedObservedTokens = Object.entries(exclusiveSignals).reduce(
    (total, [signal, breakdown]) => signal === "clean" ? total : total + breakdown.observed_tokens,
    0,
  );
  const selectedForecastPoint = forecastHorizon === "day"
    ? forecasts?.values?.remainder_of_day
    : forecastHorizon === "week"
      ? forecasts?.values?.remainder_of_week
      : forecasts?.values?.remainder_of_month;
  const forecastTokenRemainder = forecastHorizon === "day"
    ? selectedForecastPoint?.expected_tokens
    : selectedForecastPoint?.expected_remainder_tokens;
  const forecastTokenTotal = forecastHorizon === "day"
    ? selectedForecastPoint?.projected_day_total_tokens
    : forecastHorizon === "week"
      ? selectedForecastPoint?.projected_week_total_tokens
      : selectedForecastPoint?.projected_month_total_tokens;
  const forecastCostTotal = forecastHorizon === "day"
    ? selectedForecastPoint?.projected_day_cost_usd
    : forecastHorizon === "week"
      ? selectedForecastPoint?.projected_week_cost_usd
      : selectedForecastPoint?.projected_month_cost_usd;
  const modelTokenIntervalKey = forecastHorizon === "day"
    ? "day_tokens"
    : forecastHorizon === "week"
      ? "week_tokens"
      : "month_tokens";
  const modelCostIntervalKey = forecastHorizon === "day"
    ? "day_cost_usd"
    : forecastHorizon === "week"
      ? "week_cost_usd"
      : "month_cost_usd";
  const modelForecasts = Object.entries(forecasts?.values?.per_model ?? {});
  const anomalyRows = anomalies?.rows ?? [];
  const anomalyBaselines = anomalies?.baselines ?? {};
  const taskBoundaryEvents = traces?.events?.filter(
    (event) => event.event_kind === "task_switch" || (event.event_name ?? "").toLowerCase().includes("task"),
  ) ?? [];
  const compactionEvents = traces?.compaction?.events ?? [];
  const comparisonMetricsA = comparison?.period_a?.metrics;
  const comparisonMetricsB = comparison?.period_b?.metrics;
  const comparisonMetricRows: Array<{ key: keyof PeriodMetrics; label: string; format: ComparisonDisplayFormat }> = [
    { key: "requests_count", label: "Requests", format: "count" },
    { key: "total_tokens", label: "Total tokens", format: "count" },
    { key: "tokens_per_request", label: "Tokens per request", format: "decimal" },
    { key: "cache_hit_rate", label: "Cache-hit rate", format: "rate" },
    { key: "api_equivalent_usd", label: "API-equivalent cost", format: "usd" },
    { key: "generation_speed_tps_weighted", label: "Weighted generation speed (tokens/s)", format: "decimal" },
    { key: "ttft_ms_mean", label: "Mean time to first token (ms)", format: "decimal" },
    { key: "duration_ms_mean", label: "Mean request duration (ms)", format: "decimal" },
    { key: "retry_rate", label: "Retry rate", format: "rate" },
  ];
  return (
    <div className="efficiency-page" style={{ display: "grid", gap: 24, minWidth: 0 }}>
      {/* Header Banner */}
      <header className="content-box">
        <div className="content-box-header" style={{ flexWrap: "wrap", gap: 12 }}>
          <h1 className="content-box-title">Efficiency Signals & Counterfactual Scenarios</h1>
          <span className="badge-tag badge-green">
            {values.efficiency_ratio != null ? `${(values.efficiency_ratio * 100).toFixed(1)}% modeled ratio` : "Model ratio unavailable"}
          </span>
        </div>
        <p className="content-box-body">
          Observed usage and signal-associated volume are measurements; avoidability and savings are counterfactual estimates, not ground-truth waste labels.
        </p>
      </header>

      {/* KPI Overview Grid */}
      <div className="card-grid">
        <div className="metric-card">
          <div className="text-xs text-neutral-400 font-medium uppercase tracking-wider">Observed Tokens</div>
          <div className="text-2xl font-semibold mt-1">{(values.observed_tokens || 0).toLocaleString()}</div>
          <div className="text-xs text-neutral-500 mt-1">Historical measured usage</div>
        </div>
        <div className="metric-card">
          <div className="text-xs text-amber-400 font-medium uppercase tracking-wider">Modeled Avoidable Tokens</div>
          <div className="text-2xl font-semibold text-amber-300 mt-1">{(values.avoidable_tokens || 0).toLocaleString()}</div>
          <div className="text-xs text-neutral-500 mt-1">Counterfactual estimated savings; not directly observed</div>
        </div>
        <div className="metric-card">
          <div className="text-xs text-neutral-400 font-medium uppercase tracking-wider">Observed Signal-Associated Tokens</div>
          <div className="text-2xl font-semibold mt-1">{signalAssociatedObservedTokens.toLocaleString()}</div>
          <div className="text-xs text-neutral-500 mt-1">Observed volume attributed to exclusive primary signals</div>
        </div>
        <div className="metric-card">
          <div className="text-xs text-neutral-400 font-medium uppercase tracking-wider">Flagged Requests</div>
          <div className="text-2xl font-semibold mt-1">
            {values.flagged_requests_count || 0} / {values.total_requests || 0}
          </div>
          <div className="text-xs text-neutral-500 mt-1">With 1+ efficiency signals</div>
        </div>
        <div className="metric-card">
          <div className="text-xs text-neutral-400 font-medium uppercase tracking-wider">Forecast Status</div>
          <div className="text-2xl font-semibold mt-1">
            {forecasts?.error ? "Unavailable" : forecasts?.eligible === false ? "Abstained" : forecasts ? "Available" : "Loading"}
          </div>
          <div className="text-xs text-neutral-500 mt-1">
            {forecasts?.status ?? "Run-rate forecast"}
          </div>
        </div>
      </div>

      {/* Exclusive Signal Breakdown Chart (Additive, No Double-Counting) */}
      <section className="content-box content-box-body">
        <h2 className="text-lg font-semibold text-white mb-2">Exclusive Signal Attribution: Observed Volume vs Modeled Avoidability</h2>
        <p className="text-xs text-neutral-400 mb-6">
          Mutually exclusive primary signals avoid double-counting. Observed tokens are associated usage; modeled avoidable tokens are counterfactual estimates, not observed waste.
        </p>
        <div className="space-y-4">
          {Object.entries(exclusiveSignals)
            .filter(([sig]) => sig !== "clean")
            .map(([sig, info]) => {
              const pct = values.avoidable_tokens > 0 ? (info.avoidable_tokens / values.avoidable_tokens) * 100 : 0;
              return (
                <div key={sig} className="space-y-1">
                  <div className="flex justify-between text-xs">
                    <span className="font-medium capitalize text-neutral-200">{sig}</span>
                    <span className="text-neutral-400">
                      {info.observed_tokens.toLocaleString()} observed associated • {info.avoidable_tokens.toLocaleString()} modeled avoidable ({pct.toFixed(1)}% of modeled avoidability) • {info.requests_count} reqs
                    </span>
                  </div>
                  <div className="w-full bg-neutral-800 rounded-full h-2.5 overflow-hidden">
                    <div
                      className="bg-amber-500 h-2.5 rounded-full"
                      style={{ width: `${Math.min(100, Math.max(2, pct))}%` }}
                    />
                  </div>
                </div>
              );
            })}
        </div>
      </section>

      <section className="content-box content-box-body" style={{ display: "grid", gap: 16 }}>
        <div>
          <h2 className="text-lg font-semibold text-white">Usage Forecasts</h2>
          <p className="text-xs text-neutral-400 mt-1">
            Day, week, and month projections use the existing run-rate and walk-forward engine. Global filters are retained; history dates only set the forecast sample window.
          </p>
        </div>
        <div className="card-grid">
          <div className="space-y-2">
            <h3 className="text-xs font-semibold text-neutral-200">Forecast history window</h3>
            <div className="card-grid">
              <label className="input-field-group">
                Start date
                <input
                  type="date"
                  value={historyStart}
                  onChange={(event) => setHistoryStart(event.target.value)}
                  className="mt-1 w-full bg-neutral-800 border border-neutral-700 rounded p-2 text-xs text-white"
                />
              </label>
              <label className="text-[11px] text-neutral-400">
                End date
                <input
                  type="date"
                  value={historyEnd}
                  onChange={(event) => setHistoryEnd(event.target.value)}
                  className="mt-1 w-full bg-neutral-800 border border-neutral-700 rounded p-2 text-xs text-white"
                />
              </label>
            </div>
            <div className="flex flex-wrap gap-2">
              <button
                onClick={() => setAppliedHistoryWindow({
                  ...(historyStart ? { start: historyStart } : {}),
                  ...(historyEnd ? { end: historyEnd } : {}),
                })}
                className="bg-sky-700 hover:bg-sky-600 text-white rounded px-3 py-1.5 text-xs font-semibold"
              >
                Apply history window
              </button>
              <button
                onClick={() => {
                  setHistoryStart("");
                  setHistoryEnd("");
                  setAppliedHistoryWindow({});
                }}
                className="bg-neutral-800 hover:bg-neutral-700 text-neutral-200 rounded px-3 py-1.5 text-xs"
              >
                Use all matching history
              </button>
            </div>
          </div>
          <div>
            <h3 className="text-xs font-semibold text-neutral-200 mb-2">Forecast horizon</h3>
            <div role="group" aria-label="Forecast horizon" className="flex flex-wrap gap-2">
              {(["day", "week", "month"] as const).map((horizon) => (
                <button
                  key={horizon}
                  aria-pressed={forecastHorizon === horizon}
                  onClick={() => setForecastHorizon(horizon)}
                  className={`rounded px-3 py-1.5 text-xs font-semibold capitalize ${
                    forecastHorizon === horizon
                      ? "bg-amber-600 text-white"
                      : "bg-neutral-800 text-neutral-300 hover:bg-neutral-700"
                  }`}
                >
                  {horizon}
                </button>
              ))}
            </div>
            <div className="text-[11px] text-neutral-500 mt-3">
              Sample window: {forecasts?.history_window?.start ?? "unbounded"} → {forecasts?.history_window?.end ?? "unbounded"}
              {" · "}{forecasts?.history_window?.timezone ?? context.query.time_range.timezone}
              {" · "}{forecasts?.history_window?.basis ?? "waiting for forecast"}
            </div>
          </div>
        </div>
        {forecasts?.error ? (
          <p className="text-xs text-rose-300">{forecasts.error}</p>
        ) : forecasts?.eligible ? (
          <>
            <div className="card-grid">
              <div className="bg-neutral-950 border border-neutral-800 rounded p-4">
                <div className="text-xs text-neutral-400">Projected {forecastHorizon} total</div>
                <div className="text-xl font-semibold text-white mt-1">{formatCount(forecastTokenTotal)} tokens</div>
                <div className="text-[11px] text-neutral-500 mt-1">
                  Expected remaining: {formatCount(forecastTokenRemainder)} tokens
                </div>
                <div className="text-[11px] text-neutral-400 mt-2">
                  Empirical interval: {formatInterval(selectedForecastPoint?.uncertainty_interval)}
                </div>
              </div>
              <div className="bg-neutral-950 border border-neutral-800 rounded p-4">
                <div className="text-xs text-neutral-400">Projected API-equivalent cost</div>
                <div className="text-xl font-semibold text-white mt-1">{formatUsd(forecastCostTotal)}</div>
                <div className="text-[11px] text-neutral-500 mt-1">
                  Scope: {forecasts.coverage?.cost_projection_scope ?? "not reported"}
                  {forecasts.coverage?.cost_projection_is_full_scope ? " (full scope)" : " (known-priced subset only)"}
                </div>
                <div className="text-[11px] text-neutral-400 mt-2">
                  Cost interval: {selectedForecastPoint?.cost_uncertainty_interval?.eligible
                    ? `${formatUsd(selectedForecastPoint.cost_uncertainty_interval.lower_bound)} – ${formatUsd(selectedForecastPoint.cost_uncertainty_interval.upper_bound)}`
                    : `Abstain${selectedForecastPoint?.cost_uncertainty_interval?.abstention_reason
                      ? `: ${selectedForecastPoint.cost_uncertainty_interval.abstention_reason}`
                      : ""}`}
                </div>
                <div className="text-[11px] text-neutral-500 mt-1">
                  {formatCount(forecasts.coverage?.priced_requests_count)} priced · {formatCount(forecasts.coverage?.unpriced_requests_count)} unpriced requests
                </div>
                {forecasts.coverage?.full_scope_cost_abstention_reason && (
                  <div className="text-amber-300 mt-1">Full-scope cost abstention: {forecasts.coverage.full_scope_cost_abstention_reason}</div>
                )}
              </div>
              <div className="bg-neutral-950 border border-neutral-800 rounded p-4 text-xs">
                <div className="text-neutral-400">Run-rate and sample coverage</div>
                <div className="mt-2">Method: {forecasts.velocity?.method ?? "not reported"}</div>
                <div>Active history days: {formatCount(forecasts.coverage?.activity_days_observed ?? forecasts.active_days_observed)}</div>
                <div>Unobserved dates between activity: {formatCount(forecasts.coverage?.unknown_days_between_activity_days)}</div>
                <div>Backtest: {forecasts.backtest?.method ?? "not reported"} · {formatCount(forecasts.backtest?.samples_evaluated)} samples</div>
                <div>MAE: {formatCount(forecasts.backtest?.mae)} tokens · MAPE: {formatPercent(forecasts.backtest?.mape)}</div>
                <div>Cost backtest: {forecasts.cost_backtest?.method ?? "not reported"} · {formatCount(forecasts.cost_backtest?.samples_evaluated)} samples</div>
                <div>Cost MAE: {formatUsd(forecasts.cost_backtest?.mae)} · MAPE: {formatPercent(forecasts.cost_backtest?.mape)}</div>
                {forecasts.cost_backtest?.abstention_reason && (
                  <div className="text-amber-300 mt-1">Cost backtest abstention: {forecasts.cost_backtest.abstention_reason}</div>
                )}
                <div>Interval method: {selectedForecastPoint?.uncertainty_interval?.method ?? "abstained"} · {formatCount(selectedForecastPoint?.uncertainty_interval?.samples_evaluated)} residual samples</div>
                {forecasts.backtest?.abstention_reason && (
                  <div className="text-amber-300 mt-1">Backtest abstention: {forecasts.backtest.abstention_reason}</div>
                )}
                {selectedForecastPoint?.uncertainty_interval?.abstention_reason && (
                  <div className="text-amber-300 mt-1">Interval abstention: {selectedForecastPoint.uncertainty_interval.abstention_reason}</div>
                )}
              </div>
            </div>
            {modelForecasts.length > 0 && (
              <div>
                <h3 className="text-xs font-semibold text-neutral-200 mb-2">Per-model {forecastHorizon} projections</h3>
                <div className="overflow-x-auto">
                  <table className="w-full text-xs text-left">
                    <thead className="text-neutral-500 border-b border-neutral-800">
                      <tr>
                        <th className="py-2 pr-3">Model</th>
                        <th className="py-2 pr-3">Active days</th>
                        <th className="py-2 pr-3">Projected tokens</th>
                        <th className="py-2 pr-3">Interval</th>
                        <th className="py-2">Projected cost · priced subset</th>
                      </tr>
                    </thead>
                    <tbody>
                      {modelForecasts.map(([model, projection]) => {
                        const modelTokens = forecastHorizon === "day"
                          ? projection.projected_day_tokens
                          : forecastHorizon === "week"
                            ? projection.projected_week_tokens
                            : projection.projected_month_tokens;
                        const modelCost = forecastHorizon === "day"
                          ? projection.projected_day_cost_usd
                          : forecastHorizon === "week"
                            ? projection.projected_week_cost_usd
                            : projection.projected_month_cost_usd;
                        const modelInterval = projection.uncertainty_intervals?.[modelTokenIntervalKey];
                        const modelCostInterval = projection.uncertainty_intervals?.[modelCostIntervalKey];
                        return (
                          <tr key={model} className="border-b border-neutral-900">
                            <td className="py-2 pr-3 text-neutral-200">{model}</td>
                            <td className="py-2 pr-3">{formatCount(projection.observed_activity_days)}</td>
                            <td className="py-2 pr-3">{formatCount(modelTokens)}</td>
                            <td className="py-2 pr-3">{formatInterval(modelInterval)}</td>
                            <td className="py-2">
                              {formatUsd(modelCost)} · {projection.cost_projection_scope ?? "scope not reported"}
                              <div className="text-neutral-500">Cost interval: {modelCostInterval?.eligible
                                ? `${formatUsd(modelCostInterval.lower_bound)} – ${formatUsd(modelCostInterval.upper_bound)}`
                                : `Abstain${modelCostInterval?.abstention_reason ? `: ${modelCostInterval.abstention_reason}` : ""}`}</div>
                            </td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
              </div>
            )}
            {forecasts.warnings && forecasts.warnings.length > 0 && (
              <ul className="list-disc pl-4 text-[11px] text-amber-300 space-y-1">
                {forecasts.warnings.map((warning, index) => <li key={index}>{warning}</li>)}
              </ul>
            )}
          </>
        ) : (
          <div className="bg-neutral-950 border border-amber-900 rounded p-4 text-xs text-amber-200">
            Forecast abstention: {forecasts?.abstention_reason ?? "Waiting for a forecast response."}
            {" "}No numeric forecast or interval is substituted for insufficient history.
          </div>
        )}
      </section>

      <section className="content-box content-box-body" style={{ display: "grid", gap: 16 }}>
        <div>
          <h2 className="text-lg font-semibold text-white">Anomaly Baselines & Evidence</h2>
          <p className="text-xs text-neutral-400 mt-1">
            Robust metric baselines show their sample size and threshold. Expected labels are user annotations, not exclusions from observed evidence.
          </p>
        </div>
        <div className="secondary-filter-drawer">
          <label className="input-field-group">
            MAD threshold multiplier
            <input
              aria-label="MAD threshold multiplier"
              type="number"
              min="1"
              step="0.5"
              value={thresholdMultiplier}
              onChange={(event) => setThresholdMultiplier(Number(event.target.value))}
              className="mt-1 block w-28 bg-neutral-800 border border-neutral-700 rounded p-2 text-xs text-white"
            />
          </label>
          <button
            onClick={() => setAppliedThresholdMultiplier(thresholdMultiplier)}
            className="bg-sky-700 hover:bg-sky-600 text-white rounded px-3 py-2 text-xs font-semibold"
          >
            Recalculate anomaly thresholds
          </button>
          <span className="text-[11px] text-neutral-500">
            Threshold changes are applied by the shared anomaly engine to the current global filters.
          </span>
        </div>
        {expectedSettingsError && (
          <div role="alert" className="text-xs text-rose-300">
            Saved expected-state setting unavailable: {expectedSettingsError}
          </div>
        )}
        {anomalies?.error ? (
          <div role="alert" className="text-xs text-rose-300">{anomalies.error}</div>
        ) : anomalies ? (
          <>
            <div className="flex flex-wrap gap-x-5 gap-y-2 text-xs text-neutral-300">
              <span>Requests audited: {formatCount(anomalies.values?.total_audited)}</span>
              <span>Anomalies: {formatCount(anomalies.values?.total_anomalies)}</span>
              <span>Unexpected: {formatCount(anomalies.values?.unexpected_anomalies)}</span>
              <span>User-marked expected: {formatCount(anomalies.values?.expected_anomalies)}</span>
              <span>Threshold multiplier: {formatDecimal(appliedThresholdMultiplier)}</span>
            </div>
            <div className="card-grid">
              {Object.entries(anomalyBaselines).map(([metric, baseline]) => {
                const cutoff = baseline.cutoff ?? baseline.drop_cutoff ?? baseline.spike_cutoff;
                return (
                  <div key={metric} className="bg-neutral-950 border border-neutral-800 rounded p-3 text-[11px]">
                    <div className="font-semibold text-neutral-200">{metric}</div>
                    <div className={baseline.eligible ? "text-emerald-300 mt-1" : "text-amber-300 mt-1"}>
                      {baseline.eligible ? "Baseline eligible" : "Baseline abstained"}
                    </div>
                    <div className="text-neutral-400 mt-1">
                      {baseline.method ?? "median/MAD"} · {formatCount(baseline.sample_size)} samples
                    </div>
                    <div className="text-neutral-500">
                      Median {formatDecimal(baseline.median)} · MAD {formatDecimal(baseline.mad)} · cutoff {formatDecimal(cutoff)}
                    </div>
                    {!baseline.eligible && baseline.abstention_reason && (
                      <div className="text-amber-300 mt-1">{baseline.abstention_reason}</div>
                    )}
                  </div>
                );
              })}
            </div>
            <div className="space-y-3">
              {anomalyRows.length === 0 ? (
                <p className="text-xs text-neutral-500">No anomaly rows were returned for this filter and threshold.</p>
              ) : anomalyRows.map((row) => {
                const savedState = expectedAnomalies.find((item) => item.id === row.canonical_id);
                const metricEvidence = Object.entries(row.evidence ?? {}).filter(([key, value]) =>
                  /tokens|duration|ttft|status/i.test(key) &&
                  (typeof value === "number" || typeof value === "string"),
                );
                return (
                  <article key={`${row.canonical_id}:${row.anomaly_type}:${row.metric_name}`} className="bg-neutral-950 border border-neutral-800 rounded p-4">
                    <div className="flex flex-wrap justify-between gap-3">
                      <div>
                        <div className="text-sm font-semibold text-white">{row.anomaly_type} · {row.metric_name}</div>
                        <div className="text-[11px] text-neutral-500 mt-1">
                          {row.request_timestamp ?? "Timestamp unavailable"} · request {row.canonical_id}
                        </div>
                        <div className="text-xs text-neutral-300 mt-2">
                          Observed {row.observed_value ?? "Unavailable"} · baseline {formatDecimal(row.baseline_median)} · MAD {formatDecimal(row.baseline_mad)} · threshold {formatDecimal(row.threshold_cutoff)}
                        </div>
                        {row.expected_state?.notes && (
                          <div className="text-[11px] text-emerald-300 mt-1">Expected-state note: {row.expected_state.notes}</div>
                        )}
                        {metricEvidence.length > 0 && (
                          <div className="text-[11px] text-neutral-500 mt-1">
                            Evidence: {metricEvidence.map(([key, value]) => `${key} ${value}`).join(" · ")}
                          </div>
                        )}
                      </div>
                      <div className="flex flex-col items-start sm:items-end gap-2">
                        <span className={`text-[11px] font-semibold ${row.is_expected ? "text-emerald-300" : "text-amber-300"}`}>
                          {row.is_expected ? "Expected by user annotation" : "Unexpected"}
                        </span>
                        {expectedSettingsLoaded && (
                          <div className="flex flex-wrap gap-2">
                            <input
                              aria-label={`Expected-state notes for ${row.canonical_id}`}
                              value={expectedNotes[row.canonical_id] ?? savedState?.notes ?? ""}
                              onChange={(event) => setExpectedNotes((previous) => ({ ...previous, [row.canonical_id]: event.target.value }))}
                              placeholder="Optional expected-state note"
                              className="w-48 bg-neutral-800 border border-neutral-700 rounded p-1.5 text-[11px] text-white"
                            />
                            {savedState ? (
                              <button
                                disabled={savingExpectedId === row.canonical_id}
                                onClick={() => void saveExpectedAnomaly(row.canonical_id, false)}
                                className="bg-neutral-800 hover:bg-neutral-700 disabled:opacity-50 rounded px-2 py-1.5 text-[11px] text-neutral-200"
                              >
                                Remove saved marker
                              </button>
                            ) : (
                              <button
                                disabled={savingExpectedId === row.canonical_id}
                                onClick={() => void saveExpectedAnomaly(row.canonical_id, true)}
                                className="bg-emerald-800 hover:bg-emerald-700 disabled:opacity-50 rounded px-2 py-1.5 text-[11px] text-white"
                              >
                                Save as expected
                              </button>
                            )}
                          </div>
                        )}
                        {row.session_id && (
                          <button
                            onClick={() => {
                              context.setQuery((previous) => ({ ...previous, search: row.session_id ?? undefined }));
                              context.navigateToPage("sessions");
                            }}
                            className="text-[11px] text-sky-300 hover:text-sky-200 underline"
                          >
                            Open session evidence
                          </button>
                        )}
                      </div>
                    </div>
                  </article>
                );
              })}
            </div>
          </>
        ) : (
          <p className="text-xs text-neutral-500">
            {expectedSettingsLoaded ? "Waiting for anomaly analysis." : "Loading saved expected-state annotations."}
          </p>
        )}
        <p className="text-[11px] text-neutral-500">
          A missing or ineligible baseline is shown as abstained; it is not interpreted as zero anomalies.
        </p>
      </section>

      <section className="content-box content-box-body" style={{ display: "grid", gap: 16 }}>
        <div>
          <h2 className="text-lg font-semibold text-white">Matched Before / After Comparison</h2>
          <p className="text-xs text-neutral-400 mt-1">
            Choose user-defined half-open ranges in the same timezone. Equal-duration windows are recommended; the engine reports duration, DST, model-mix, and prompt-shift caveats. All shared global filters remain in force for both periods.
          </p>
        </div>
        <div className="secondary-filter-drawer">
          <label className="input-field-group">
            Before start
            <input
              type="date"
              value={beforeStart}
              onChange={(event) => setBeforeStart(event.target.value)}
              className="mt-1 block w-full bg-neutral-800 border border-neutral-700 rounded p-2 text-xs text-white"
            />
          </label>
          <label className="text-xs text-neutral-400">
            Before end (exclusive)
            <input
              type="date"
              value={beforeEnd}
              onChange={(event) => setBeforeEnd(event.target.value)}
              className="mt-1 block w-full bg-neutral-800 border border-neutral-700 rounded p-2 text-xs text-white"
            />
          </label>
          <label className="text-xs text-neutral-400">
            After start
            <input
              type="date"
              value={afterStart}
              onChange={(event) => setAfterStart(event.target.value)}
              className="mt-1 block w-full bg-neutral-800 border border-neutral-700 rounded p-2 text-xs text-white"
            />
          </label>
          <label className="text-xs text-neutral-400">
            After end (exclusive)
            <input
              type="date"
              value={afterEnd}
              onChange={(event) => setAfterEnd(event.target.value)}
              className="mt-1 block w-full bg-neutral-800 border border-neutral-700 rounded p-2 text-xs text-white"
            />
          </label>
        </div>
        <div className="secondary-filter-drawer">
          <label className="input-field-group">
            Change / intervention label
            <input
              value={changeLabel}
              onChange={(event) => setChangeLabel(event.target.value)}
              placeholder="Optional descriptive label"
              className="mt-1 block w-72 max-w-full bg-neutral-800 border border-neutral-700 rounded p-2 text-xs text-white"
            />
          </label>
          <button
            disabled={!beforeStart || !beforeEnd || !afterStart || !afterEnd || comparisonLoading}
            onClick={() => void runComparison()}
            className="bg-sky-700 hover:bg-sky-600 disabled:opacity-50 text-white rounded px-3 py-2 text-xs font-semibold"
          >
            {comparisonLoading ? "Comparing…" : "Compare periods"}
          </button>
          <span className="text-[11px] text-neutral-500">
            Timezone: {context.query.time_range.timezone}
          </span>
        </div>
        {comparisonError && <div role="alert" className="text-xs text-rose-300">{comparisonError}</div>}
        {comparison ? (
          <>
            <div className="text-xs text-neutral-300">
              {comparison.period_a?.label ?? "Before"}: {comparison.period_a?.start ?? "Unavailable"} → {comparison.period_a?.end ?? "Unavailable"}
              {" · "}{comparison.period_b?.label ?? (changeLabel.trim() || "After")}: {comparison.period_b?.start ?? "Unavailable"} → {comparison.period_b?.end ?? "Unavailable"}
              {" · Status: "}
              <span className={comparison.matched_status === "matched" ? "text-emerald-300" : "text-amber-300"}>
                {comparison.matched_status ?? "not reported"}
              </span>
            </div>
            <div className="overflow-x-auto">
              <table className="w-full text-xs text-left">
                <thead className="text-neutral-500 border-b border-neutral-800">
                  <tr>
                    <th className="py-2 pr-3">Metric</th>
                    <th className="py-2 pr-3">{comparison.period_a?.label ?? "Before"}</th>
                    <th className="py-2 pr-3">{comparison.period_b?.label ?? (changeLabel.trim() || "After")}</th>
                    <th className="py-2 pr-3">Change (after − before)</th>
                    <th className="py-2">Relative change</th>
                  </tr>
                </thead>
                <tbody>
                  {comparisonMetricRows.map((metric) => {
                    const delta = comparison.values?.[metric.key as string];
                    return (
                      <tr key={metric.key} className="border-b border-neutral-900">
                        <td className="py-2 pr-3 text-neutral-300">{metric.label}</td>
                        <td className="py-2 pr-3">{formatComparisonValue(comparisonMetricsA?.[metric.key], metric.format)}</td>
                        <td className="py-2 pr-3">{formatComparisonValue(comparisonMetricsB?.[metric.key], metric.format)}</td>
                        <td className="py-2 pr-3">{formatComparisonDelta(delta?.delta, metric.format)}</td>
                        <td className="py-2">{typeof delta?.pct_change === "number" ? `${delta.pct_change > 0 ? "+" : ""}${formatDecimal(delta.pct_change)}%` : "Unavailable"}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
            <div>
              <h3 className="text-xs font-semibold text-neutral-200 mb-2">Recorded task outcomes</h3>
              <div className="overflow-x-auto">
                <table className="w-full text-xs text-left">
                  <thead className="text-neutral-500 border-b border-neutral-800">
                    <tr>
                      <th className="py-2 pr-3">Outcome</th>
                      <th className="py-2 pr-3">{comparison.period_a?.label ?? "Before"}</th>
                      <th className="py-2">{comparison.period_b?.label ?? (changeLabel.trim() || "After")}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {[
                      { key: "total_tasks_recorded", label: "Tasks recorded", format: "count" as const },
                      { key: "completed_tasks", label: "Completed tasks", format: "count" as const },
                      { key: "completion_rate", label: "Completion rate", format: "rate" as const },
                      { key: "manual_rating_mean", label: "Mean manual rating", format: "decimal" as const },
                    ].map((outcome) => (
                      <tr key={outcome.key} className="border-b border-neutral-900">
                        <td className="py-2 pr-3 text-neutral-300">{outcome.label}</td>
                        <td className="py-2 pr-3">{formatComparisonValue((comparisonMetricsA?.outcomes as Record<string, unknown> | undefined)?.[outcome.key], outcome.format)}</td>
                        <td className="py-2">{formatComparisonValue((comparisonMetricsB?.outcomes as Record<string, unknown> | undefined)?.[outcome.key], outcome.format)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                <p className="text-[11px] text-neutral-500 mt-2">The comparison contract reports outcome values but does not provide outcome deltas.</p>
              </div>
            </div>
            <div className="bg-neutral-950 border border-neutral-800 rounded p-4">
              <h3 className="text-xs font-semibold text-neutral-200">Confounders and interpretation limits</h3>
              {comparison.confounders?.length ? (
                <ul className="list-disc pl-4 text-[11px] text-amber-300 mt-2 space-y-1">
                  {comparison.confounders.map((confounder, index) => <li key={index}>{confounder}</li>)}
                </ul>
              ) : (
                <p className="text-[11px] text-neutral-400 mt-2">
                  No confounders were flagged by the engine’s available checks; this does not establish matching or causality.
                </p>
              )}
              {comparison.causal_claim_disclaimer && (
                <p className="text-[11px] text-neutral-400 mt-2">{comparison.causal_claim_disclaimer}</p>
              )}
            </div>
          </>
        ) : (
          <p className="text-xs text-neutral-500">Choose all four range boundaries to run a comparison. No before/after result is inferred from the current dashboard totals.</p>
        )}
      </section>

      {/* Priority Insight List & Evidence Drilldown */}
      <section className="content-box content-box-body" style={{ display: "grid", gap: 24 }}>
        <div className="lg:col-span-2 space-y-4">
          <h2 className="text-lg font-semibold text-white">Priority Actionable Insights</h2>
          <div className="space-y-3">
            {insights.map((ins) => (
              <div
                key={ins.id}
                onClick={() => setSelectedInsight(ins)}
                className={`p-4 rounded-lg border cursor-pointer transition-colors ${
                  selectedInsight?.id === ins.id
                    ? "bg-neutral-800 border-amber-500"
                    : "bg-neutral-900 border-neutral-800 hover:border-neutral-700"
                }`}
              >
                <div className="flex items-center justify-between">
                  <div className="flex items-center gap-2">
                    <span
                      className={`text-[10px] font-bold uppercase px-2 py-0.5 rounded ${
                        ins.priority === "high"
                          ? "bg-rose-950 text-rose-300 border border-rose-800"
                          : "bg-amber-950 text-amber-300 border border-amber-800"
                      }`}
                    >
                      {ins.priority}
                    </span>
                    <span className="text-xs text-neutral-400 font-mono">[{ins.basis}]</span>
                    <h3 className="text-sm font-semibold text-neutral-100">{ins.title}</h3>
                  </div>
                  <span className="text-xs text-amber-300 font-medium">
                    ~{ins.avoidable_tokens.toLocaleString()} modeled avoidable
                  </span>
                </div>
                <p className="text-xs text-neutral-400 mt-2">{ins.description}</p>
                <div className="text-xs text-emerald-400 mt-2 font-medium">
                  → {ins.actionable_recommendation}
                </div>
              </div>
            ))}
          </div>
        </div>

        {/* Evidence Drilldown Panel */}
        <div className="bg-neutral-900 border border-neutral-800 rounded-lg p-5">
          <h3 className="text-sm font-semibold text-white mb-2">Evidence Trail & Grounding</h3>
          {selectedInsight ? (
            <div className="space-y-4 text-xs">
              <div>
                <span className="text-neutral-500">Basis:</span>{" "}
                <span className="text-neutral-200 font-mono">{selectedInsight.basis}</span>
              </div>
              <div>
                <span className="text-neutral-500">Confidence:</span>{" "}
                <span className="text-neutral-200 uppercase font-bold">{selectedInsight.confidence}</span>
              </div>
              <div className="border-t border-neutral-800 pt-2">
                <span className="text-neutral-400 font-medium">Sample Traces:</span>
                <div className="space-y-2 mt-2">
                  {selectedInsight.evidence_sample?.map((ev, idx) => (
                    <div key={idx} className="bg-neutral-950 p-2 rounded border border-neutral-800">
                      <div className="font-mono text-[11px] text-amber-300">{ev.canonical_id}</div>
                      <div className="text-neutral-400 text-[11px] mt-1">{ev.reason}</div>
                      <button
                        onClick={() => {
                          context.setQuery((prev) => ({ ...prev, search: ev.canonical_id }));
                          context.navigateToPage('sessions');
                        }}
                        className="mt-2 text-[10px] text-sky-400 underline"
                      >
                        Inspect Request Trace →
                      </button>
                    </div>
                  ))}
                </div>
              </div>
              <div className="text-[11px] text-neutral-500 italic border-t border-neutral-800 pt-2">
                {selectedInsight.causality_disclaimer}
              </div>
            </div>
          ) : (
            <div className="text-xs text-neutral-500 py-12 text-center">
              Select an insight on the left to drill into concrete evidence traces and event links.
            </div>
          )}
        </div>
      </section>

      {/* Model Routing & Context Compaction Advice */}
      <section className="content-box content-box-body" style={{ display: "grid", gap: 24 }}>
        {/* Routing Alternatives */}
        <div className="bg-neutral-900 border border-neutral-800 rounded-lg p-6">
          <h2 className="text-base font-semibold text-white mb-2">Safe Model Routing Guidance</h2>
          {routingAlts.length > 0 ? (
            routingAlts.map((r, i) => (
              <div key={i} className="space-y-2 text-xs">
                <div className="flex justify-between items-center">
                  <span className="font-semibold text-neutral-200">
                    {r.source_model} → {r.recommended_target}
                  </span>
                  <span
                    className={`px-2 py-0.5 rounded text-[10px] font-bold ${
                      r.adequate_sample
                        ? "bg-emerald-950 text-emerald-300 border border-emerald-800"
                        : "bg-amber-950 text-amber-300 border border-amber-800"
                    }`}
                  >
                    {r.recommendation_type}
                  </span>
                </div>
                <p className="text-neutral-400">{r.rationale}</p>
                {r.experiment_design && (
                  <div className="bg-neutral-950 p-3 rounded border border-neutral-800 space-y-1">
                    <div className="font-semibold text-neutral-300">Suggested Controlled Experiment:</div>
                    <div>Target Sample: {r.experiment_design.sample_size} tasks</div>
                    <div>Criteria: {r.experiment_design.success_criteria}</div>
                  </div>
                )}
                <div className="text-[10px] text-neutral-500 italic">{r.causality_disclaimer}</div>
              </div>
            ))
          ) : (
            <div className="text-xs text-neutral-500">No active model routing alternatives suggested.</div>
          )}
        </div>

        {/* Compaction Guidance */}
        <div className="bg-neutral-900 border border-neutral-800 rounded-lg p-6">
          <h2 className="text-base font-semibold text-white mb-2">Context Restart & Compaction</h2>
          <p className="text-[11px] text-neutral-400 mb-3">
            Recommendations rely on explicit task-boundary events. Context-window occupancy is reported separately from cumulative input counters.
          </p>
          {compaction ? (
            <div className="space-y-3 text-xs">
              <div className="flex items-center gap-2">
                <span className="text-neutral-400">Recommendation status:</span>
                <span className={`font-bold uppercase ${compaction.actionable ? "text-emerald-300" : "text-amber-300"}`}>
                  {compaction.status}
                </span>
              </div>
              <p className="text-neutral-300">{compaction.reason}</p>
              <div className="text-[11px] text-neutral-500">
                Basis: <code className="text-neutral-400">{compaction.basis}</code>
                {typeof compaction.sessions_eligible === "number" && ` · ${compaction.sessions_eligible} eligible sessions`}
              </div>
              {!compaction.actionable && (
                <div className="bg-neutral-950 border border-amber-900 rounded p-3 text-[11px] text-amber-200">
                  Recommendation abstained; no action is inferred from input volume or missing context-window evidence.
                </div>
              )}
              {compaction.disclaimer && (
                <div className="text-[10px] text-neutral-500 italic">{compaction.disclaimer}</div>
              )}
            </div>
          ) : (
            <div className="text-xs text-neutral-500">
              {traces?.error ? `Trace analysis unavailable: ${traces.error}` : "Compaction recommendation was not returned."}
            </div>
          )}
          <div className="border-t border-neutral-800 pt-3 space-y-2">
            <h3 className="text-xs font-semibold text-neutral-200">Reported context-window occupancy</h3>
            <p className="text-[11px] text-neutral-400">
              Mean {formatPercent(traces?.context_occupancy?.avg_context_occupancy_ratio)} · peak {formatPercent(traces?.context_occupancy?.peak_occupancy_ratio)}
              {" · "}{formatCount(traces?.missingness?.unobserved_context_window_requests_count ?? traces?.context_occupancy?.unobserved_window_requests_count)} requests without a resolved model window
            </p>
            <p className="text-[10px] text-neutral-500">
              Occupancy is the reported per-turn token count divided by the resolved model window. Cumulative input is not used as occupancy or compaction evidence.
            </p>
            {traces?.context_occupancy?.avg_context_occupancy_ratio == null &&
              traces?.context_occupancy?.peak_occupancy_ratio == null && (
                <p className="text-[11px] text-amber-300">
                  Context-window occupancy is unavailable for this filtered sample; no occupancy-based recommendation is made.
                </p>
              )}
          </div>
          <div className="border-t border-neutral-800 pt-3 space-y-2">
            <h3 className="text-xs font-semibold text-neutral-200">Task-boundary evidence</h3>
            {taskBoundaryEvents.length > 0 ? (
              taskBoundaryEvents.map((event) => (
                <div key={event.event_id} className="bg-neutral-950 border border-neutral-800 rounded p-2 text-[11px]">
                  <div className="text-neutral-300">{event.event_name ?? event.event_kind ?? "Task event"} · {event.timestamp ?? event.started_at ?? "Timestamp unavailable"}</div>
                  <div className="text-neutral-500">Event {event.event_id}</div>
                  {event.session_id ? (
                    <button
                      onClick={() => {
                        context.setQuery((previous) => ({ ...previous, search: event.session_id ?? undefined }));
                        context.navigateToPage("sessions");
                      }}
                      className="mt-1 text-sky-300 hover:text-sky-200 underline"
                    >
                      Open supporting session
                    </button>
                  ) : (
                    <div className="text-amber-300 mt-1">No session ID; session evidence link unavailable.</div>
                  )}
                </div>
              ))
            ) : (
              <p className="text-[11px] text-neutral-500">
                No task-boundary event evidence was returned for this filter. A positive compaction recommendation without visible supporting events is not treated as verified evidence.
              </p>
            )}
          </div>
          <div className="border-t border-neutral-800 pt-3 space-y-2">
            <h3 className="text-xs font-semibold text-neutral-200">Observed compaction events</h3>
            {compactionEvents.length > 0 ? (
              <>
                <p className="text-[11px] text-neutral-400">
                  {formatCount(traces?.compaction?.compaction_events_count)} events · {formatCount(traces?.compaction?.total_reduction_tokens)} recorded reduction tokens
                </p>
                {compactionEvents.map((event) => (
                  <div key={event.event_id} className="bg-neutral-950 border border-neutral-800 rounded p-2 text-[11px]">
                    <div className="text-neutral-300">{event.timestamp ?? "Timestamp unavailable"} · event {event.event_id}</div>
                    <div className="text-neutral-500">
                      Before {formatCount(event.before_tokens)} · after {formatCount(event.after_tokens)} · recorded reduction {formatCount(event.reduction_tokens)} · turns compacted {formatCount(event.turns_compacted)}
                    </div>
                    {event.session_id && (
                      <button
                        onClick={() => {
                          context.setQuery((previous) => ({ ...previous, search: event.session_id ?? undefined }));
                          context.navigateToPage("sessions");
                        }}
                        className="mt-1 text-sky-300 hover:text-sky-200 underline"
                      >
                        Open compaction session
                      </button>
                    )}
                  </div>
                ))}
              </>
            ) : (
              <p className="text-[11px] text-neutral-500">
                No compaction event measurements in this filtered trace sample; event count and reduction are unavailable, not evidence of zero compactions.
              </p>
            )}
          </div>
        </div>
      </section>

      {/* Counterfactual Scenario Model */}
      <section className="content-box">
        <div className="content-box-header">
          <h2 className="content-box-title">Counterfactual Scenario Model</h2>
        </div>
        <p className="content-box-body">
          Scenarios use the current shared query selection, then price each selected request against its effective provider, model, and route schedule. Unknown usage or prices remain unknown.
        </p>

        <div className="secondary-filter-drawer">
          <div className="input-field-group">
            <label htmlFor="scenario-type">Scenario</label>
            <select id="scenario-type"
              value={scenarioType}
              onChange={(event) => setScenarioType(event.target.value)}
              className="w-full bg-neutral-800 border border-neutral-700 rounded p-2 text-xs text-white"
            >
              <option value="cache-read">Cache-read expansion</option>
              <option value="output-reduction">Output reduction</option>
              <option value="longcontext">Long-context reduction</option>
              <option value="model-switch">Model switch</option>
            </select>
          </div>
          <div className="input-field-group">
            <label htmlFor="scenario-source-model">Source model (optional scope)</label>
            <input id="scenario-source-model"
              value={sourceModel}
              onChange={(event) => setSourceModel(event.target.value)}
              placeholder="All models in query"
              className="w-full bg-neutral-800 border border-neutral-700 rounded p-2 text-xs text-white"
            />
          </div>
          <div className="input-field-group">
            <label htmlFor="scenario-source-provider">Source provider (optional scope)</label>
            <input id="scenario-source-provider"
              value={sourceProvider}
              onChange={(event) => setSourceProvider(event.target.value)}
              placeholder="All providers in query"
              className="w-full bg-neutral-800 border border-neutral-700 rounded p-2 text-xs text-white"
            />
          </div>
          <div className="input-field-group">
            <label htmlFor="scenario-source-route">Source route (optional scope)</label>
            <input id="scenario-source-route"
              value={sourceRoute}
              onChange={(event) => setSourceRoute(event.target.value)}
              placeholder="All routes in query"
              className="w-full bg-neutral-800 border border-neutral-700 rounded p-2 text-xs text-white"
            />
          </div>

          {scenarioType === "cache-read" && (
            <div className="input-field-group">
              <label htmlFor="scenario-cache-expansion">Fresh input converted: {cacheExpansionPct}%</label>
              <input id="scenario-cache-expansion"
                type="range"
                min="10"
                max="90"
                value={cacheExpansionPct}
                onChange={(event) => setCacheExpansionPct(Number(event.target.value))}
                className="w-full mt-2"
              />
            </div>
          )}
          {scenarioType === "output-reduction" && (
            <div className="input-field-group">
              <label htmlFor="scenario-output-reduction">Output reduction: {outputReductionPct}%</label>
              <input id="scenario-output-reduction"
                type="range"
                min="5"
                max="50"
                value={outputReductionPct}
                onChange={(event) => setOutputReductionPct(Number(event.target.value))}
                className="w-full mt-2"
              />
            </div>
          )}
          {scenarioType === "longcontext" && (
            <div className="input-field-group">
              <label htmlFor="scenario-context-threshold">Fresh input threshold (tokens)</label>
              <input id="scenario-context-threshold"
                type="number"
                min="0"
                value={contextThreshold}
                onChange={(event) => setContextThreshold(Number(event.target.value))}
                className="w-full bg-neutral-800 border border-neutral-700 rounded p-2 text-xs text-white"
              />
            </div>
          )}
          {scenarioType === "model-switch" && (
            <>
              <div className="input-field-group">
                <label htmlFor="scenario-target-model">Target model</label>
                <input id="scenario-target-model"
                  value={targetModel}
                  onChange={(event) => setTargetModel(event.target.value)}
                  placeholder="Required"
                  className="w-full bg-neutral-800 border border-neutral-700 rounded p-2 text-xs text-white"
                />
              </div>
              <div className="input-field-group">
                <label htmlFor="scenario-target-provider">Target provider (optional)</label>
                <input id="scenario-target-provider"
                  value={targetProvider}
                  onChange={(event) => setTargetProvider(event.target.value)}
                  placeholder="Use each selected request's provider"
                  className="w-full bg-neutral-800 border border-neutral-700 rounded p-2 text-xs text-white"
                />
              </div>
              <div className="input-field-group">
                <label htmlFor="scenario-target-route">Target route (optional)</label>
                <input id="scenario-target-route"
                  value={targetRoute}
                  onChange={(event) => setTargetRoute(event.target.value)}
                  placeholder="Use each selected request's route"
                  className="w-full bg-neutral-800 border border-neutral-700 rounded p-2 text-xs text-white"
                />
              </div>
            </>
          )}
          <div className="flex items-end">
            <button
              onClick={runScenario}
              disabled={loading}
              className="btn-primary"
            >
              Calculate scenario
            </button>
          </div>
        </div>

        {activeScenario ? (
          <div className="content-box-body" style={{ display: "grid", gap: 16 }}>
            <div className="text-xs text-neutral-300">
              Selected <strong>{activeScenario.request_count.toLocaleString()}</strong> request(s) from
              {" "}<strong>{activeScenario.query_filtered_request_count.toLocaleString()}</strong> shared-query request(s).
            </div>

            <div className="card-grid">
              <div>
                <span className="text-neutral-500">Modeled token change (baseline − projected)</span>
                <div className="text-base font-semibold text-neutral-100 mt-0.5">
                  {activeScenario.token_reduction === null
                    ? "Unknown"
                    : `${activeScenario.token_reduction.toLocaleString()} tokens (${activeScenario.token_reduction_pct ?? "Unknown"}%)`}
                </div>
                <div className="text-neutral-500">
                  Baseline {activeScenario.baseline_tokens.total_tokens === null
                    ? "unknown"
                    : activeScenario.baseline_tokens.total_tokens.toLocaleString()}
                  {" · "}projected {activeScenario.projected_tokens.total_tokens === null
                    ? "unknown"
                    : activeScenario.projected_tokens.total_tokens.toLocaleString()}
                </div>
              </div>
              <div>
                <span className="text-neutral-500">Modeled cost difference (baseline − projected)</span>
                <div className="text-base font-semibold text-neutral-100 mt-0.5">
                  {activeScenario.financials.projected_savings_usd === null
                    ? "Unknown"
                    : `${activeScenario.financials.projected_savings_usd >= 0 ? "+" : "−"}$${Math.abs(activeScenario.financials.projected_savings_usd).toFixed(8)}`}
                </div>
                <div className="text-neutral-500">
                  Baseline {activeScenario.financials.baseline_cost_usd === null
                    ? "unknown"
                    : `$${activeScenario.financials.baseline_cost_usd.toFixed(8)}`}
                  {" · "}projected {activeScenario.financials.projected_cost_usd === null
                    ? "unknown"
                    : `$${activeScenario.financials.projected_cost_usd.toFixed(8)}`}
                </div>
              </div>
              <div>
                <span className="text-neutral-500">Pricing coverage</span>
                <div className="text-neutral-200 mt-0.5">
                  {activeScenario.is_priced ? "Complete for selected requests" : "Incomplete or unknown"}
                </div>
                <div className="text-neutral-500">
                  Baseline {activeScenario.pricing_coverage.baseline_priced_requests.toLocaleString()}
                  {" · "}projected {activeScenario.pricing_coverage.projected_priced_requests.toLocaleString()} priced request(s)
                </div>
              </div>
            </div>

            <div className="text-xs text-neutral-400 space-y-1">
              <div>
                Unknown usage counters: {activeScenario.pricing_coverage.unknown_usage_counters.length
                  ? activeScenario.pricing_coverage.unknown_usage_counters.join(", ")
                  : "none"}
              </div>
              <div>
                Unknown-TTL cache-write tokens: {activeScenario.pricing_coverage.unknown_ttl_tokens === null
                  ? "unknown"
                  : activeScenario.pricing_coverage.unknown_ttl_tokens.toLocaleString()}
                {" · "}Unpriced tokens: {activeScenario.financials.unpriced_tokens === null
                  ? "unknown"
                  : activeScenario.financials.unpriced_tokens.toLocaleString()}
              </div>
            </div>

            <div>
              <h3 className="text-xs font-semibold text-neutral-200 mb-2">Effective pricing evidence</h3>
              {activeScenario.pricing_evidence.length ? (
                <ul className="space-y-2">
                  {activeScenario.pricing_evidence.map((evidence) => (
                    <li
                      key={`${evidence.role}-${evidence.provider}-${evidence.model}-${evidence.pricing_version_id}-${evidence.rate_id}-${evidence.requested_route}`}
                      className="text-[11px] text-neutral-400 border-l border-neutral-700 pl-3"
                    >
                      <div>
                        {evidence.role}: {evidence.provider || "unknown provider"} / {evidence.model || "unknown model"} /
                        {" "}{evidence.requested_route || "unknown route"} · {evidence.rate_status}
                        {evidence.reason ? ` (${evidence.reason})` : ""}
                      </div>
                      <div>
                        Schedule {evidence.pricing_version_id ?? "unknown"} · effective {evidence.effective_start ?? "unknown"}
                        {" – "}{evidence.effective_end ?? "open-ended"} · rate route {evidence.rate_route ?? "unmatched"}
                      </div>
                      <div>
                        Hash {evidence.pricing_hash ?? "unknown"} · verified {evidence.verified_on ?? "unknown"}
                        {" · "}{evidence.request_count.toLocaleString()} request(s)
                        {evidence.source_url ? (
                          <> · <a className="text-sky-400 underline" href={evidence.source_url} target="_blank" rel="noreferrer">source</a></>
                        ) : " · no source URL"}
                      </div>
                    </li>
                  ))}
                </ul>
              ) : (
                <div className="text-[11px] text-neutral-500">No pricing evidence was available for the selected requests.</div>
              )}
            </div>

            <div className="text-xs text-neutral-400">
              <span className="font-semibold text-neutral-300">Assumptions</span>
              <ul className="list-disc pl-4 mt-1 space-y-0.5">
                {activeScenario.assumptions.map((assumption, index) => (
                  <li key={index}>{assumption}</li>
                ))}
              </ul>
            </div>
            {activeScenario.warnings.length > 0 && (
              <div className="text-xs text-amber-300">
                <span className="font-semibold">Warnings</span>
                <ul className="list-disc pl-4 mt-1 space-y-0.5">
                  {activeScenario.warnings.map((warning, index) => (
                    <li key={index}>{warning}</li>
                  ))}
                </ul>
              </div>
            )}
            <div className="text-[11px] text-neutral-500 italic border-t border-neutral-900 pt-2">
              {activeScenario.disclaimer}
            </div>
          </div>
        ) : (
          <div className="content-box-body">
            Calculate a scenario to see request counts, effective pricing evidence, assumptions, and any unknown usage.
          </div>
        )}
      </section>
    </div>
  );
};

export default Efficiency;
