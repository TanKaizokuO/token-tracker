import React, { useCallback, useEffect, useMemo, useState } from 'react';
import type { PageProps, QueryResponse } from '../App';
import './Providers.css';
import { formatUsdAsInr, formatMoneyInr, formatUsdSecondary, formatInr } from '../lib/money';

type HarnessScope = 'combined' | 'claude-code' | 'omp';
const HARNESS_SCOPES: HarnessScope[] = ['claude-code', 'omp', 'combined'];

interface SummaryValues {
  total_tokens?: number | null;
  total_tokens_status?: 'exact' | 'partial';
  has_unresolved_overlap?: boolean;
  token_fields_status?: Record<string, string>;
  api_equivalent_usd_status?: string;
  api_equivalent_inr_status?: string;
  cache_savings_usd_status?: string;
  session_duration_ms?: number | null;
  fresh_input_tokens?: number | null;
  cache_read_tokens?: number | null;
  cache_write_5m_tokens?: number | null;
  cache_write_1h_tokens?: number | null;
  cache_write_unknown_ttl_tokens?: number | null;
  output_tokens?: number | null;
  reasoning_tokens?: number | null;
  requests_count?: number | null;
  sessions_count?: number | null;
  cache_hit_rate?: number | null;
  api_equivalent_usd?: number | null;
  api_equivalent_inr?: number | null;
  cache_savings_usd?: number | null;
}

interface Distribution {
  count: number; linked_sessions: number; basis: string; status: string; method: string;
  min: number | null; max: number | null; p50: number | null; p90: number | null; p95: number | null;
}

interface TimingSlice {
  counts: { total_requests: number; primary_basis_requests: number; eligible_generation_requests: number; eligible_ttft_requests: number; eligible_total_duration_requests: number };
  tps: { weighted: number | null; p95: number | null };
  distributions: { ttft_ms: { p50: number | null; p95: number | null }; duration_ms: { p95: number | null } };
}

interface TimelineRow {
  bucket: string; bucket_start: string; bucket_end: string; model: string | null;
  requests_count: number; sessions_count: number | null; total_tokens: number | null;
  total_tokens_status: string; api_equivalent_usd: number | null; api_equivalent_usd_status: string;
  active_minutes: number | null; active_minutes_status: string; active_minutes_basis: string; active_minutes_timed_requests: number;
}
type TimelineResponse = Omit<QueryResponse, 'rows'> & { rows?: TimelineRow[] };
interface PeriodMetrics {
  requests_count: number; total_tokens: number | null; api_equivalent_usd: number | null;
  cache_savings_usd: number | null; cache_hit_rate: number | null; priced_requests: number;
  coverage: Record<string, string>;
  outcomes: { completed_tasks?: number; total_tasks?: number; completion_rate?: number | null; adequate_sample?: boolean };
}
type ComparisonResponse = Omit<QueryResponse, 'values'> & {
  period_a?: { start: string; end: string; metrics: PeriodMetrics };
  period_b?: { start: string; end: string; metrics: PeriodMetrics };
  matched_status?: string; confounders?: string[];
};

interface TraceTool {
  tool_name: string; invocations_count: number; timed_invocations_count: number;
  total_duration_ms: number; output_size_observed_count: number; total_output_size_bytes: number;
  success_count: number; error_count: number; status_unobserved_count: number;
}
interface TraceEvidence {
  event_id?: string; canonical_id?: string; session_id?: string; event_name?: string;
  type?: string; evidence?: string; classification?: string;
}
type TraceResponse = Omit<QueryResponse, 'values'> & {
  event_scope?: string; session_level_events?: TraceEvidence[];
  values?: { tool_calls_count?: number; subagent_calls_count?: number; subagent_sessions_count?: number };
  tools_breakdown?: TraceTool[]; subagent_events?: TraceEvidence[];
  retry_loops?: { retry_requests_count?: number; retry_sequences_count?: number; suspected_waste_signals?: TraceEvidence[] };
  source_coverage?: Record<string, string>;
};


type SummaryResponse = Omit<QueryResponse, 'values' | 'missingness'> & {
  values?: SummaryValues;
  session_duration_distribution?: Distribution;
  missingness?: {
    unpriced_requests_count?: number;
    session_duration_observed_count?: number;
    session_duration_sessions_count?: number;
  };
};

interface SpeedValues {
  generation_speed_tps_weighted?: number | null;
  ttft_ms_median?: number | null;
  duration_ms_median?: number | null;
}

type SpeedResponse = Omit<QueryResponse, 'values' | 'eligible' | 'primary_timing_basis'> & {
  values?: SpeedValues;
  primary_timing_basis?: string;
  slices?: { model: Record<string, TimingSlice>; route: Record<string, TimingSlice>; model_route: Record<string, Record<string, TimingSlice>> };
  eligible?: {
    timed_generation_requests?: number;
    total_requests?: number;
    timed_duration_requests?: number;
    timed_ttft_requests?: number;
    timing_basis_used?: string;
  };
};

interface ModelTokenRow {
  group_key: string;
  model?: string;
  provider?: string;
  access_route?: string | null;
  raw_model?: string;
  pricing_version_id?: string | null;
  total_tokens?: number | null;
  requests_count?: number;
  sessions_count?: number | null;
  fresh_input_tokens?: number | null;
  cache_read_tokens?: number | null;
  cache_write_5m_tokens?: number | null;
  cache_write_1h_tokens?: number | null;
  cache_write_unknown_ttl_tokens?: number | null;
  output_tokens?: number | null;
  reasoning_tokens?: number | null;
  reported_total_tokens?: number | null;
  token_fields_status?: Record<string, string>;
  cache_hit_rate?: number | null;
  eligible_prompt_tokens?: number | null;
  priced_requests_count?: number;
  unpriced_requests_count?: number;
  api_equivalent_usd_status?: string;
  cache_savings_usd?: number | null;
  cache_savings_usd_status?: string;
  total_tokens_status?: 'exact' | 'partial' | 'unavailable';
  api_equivalent_usd?: number | null;
}

type TokensResponse = Omit<QueryResponse, 'rows'> & { rows?: ModelTokenRow[] };

interface HarnessOutcomeSlice {
  adequate_sample?: boolean;
  completed_tasks?: number;
  tokens_per_completed_task?: number | null;
  time_per_completed_task_seconds?: number | null;
  timed_completed_tasks?: number;
  retry_requests_per_completed_task?: number | null;
  retry_covered_completed_tasks?: number;
  cost_per_success_usd?: number | null;
  total_tasks?: number;
  user_owned_count?: number;
  inferred_count?: number;
  abstention_reason?: string | null;
}

interface RequestReliability {
  eligible_requests: number; status_covered_requests: number; unknown_status_requests: number; coverage: string;
  statuses: Record<string, { count: number; rate: number | null }>;
  failed_requests: { count: number; rate: number | null };
  retry_requests: { count: number | null; rate: number | null };
  retry_covered_requests: number; retry_unknown_requests: number;
}
type OutcomesResponse = Omit<QueryResponse, 'values'> & { values?: HarnessOutcomeSlice; request_reliability?: RequestReliability };

type ScopeMap<T> = Record<HarnessScope, T | undefined>;
type ModelByScope = Partial<Record<HarnessScope, ModelTokenRow>>;

function emptyScopeMap<T>(): ScopeMap<T> {
  return {
    'claude-code': undefined,
    omp: undefined,
    combined: undefined,
  };
}

function throwQueryError(response: { error?: string }): void {
  if (response.error) throw new Error(response.error);
}

const SESSION_IDS_MISSING_TITLE = 'Session ids not recorded for this harness';

function formatNumber(value: number | null | undefined, digits = 0): string {
  return value === null || value === undefined
    ? '—'
    : value.toLocaleString(undefined, { maximumFractionDigits: digits });
}

function formatCost(value: number | null | undefined): string {
  return formatUsdAsInr(value, 2, '—');
}

function formatRatio(value: number | null | undefined): string {
  return value === null || value === undefined ? '—' : `${(value * 100).toFixed(1)}%`;
}

function withStatus(value: string, status: string | undefined): string {
  return status && status !== 'exact' ? `${value} (${status})` : value;
}

function formatDelta(value: number | null): string {
  if (value === null) return '—';
  const formatted = value.toLocaleString(undefined, { maximumFractionDigits: 2 });
  return value > 0 ? `+${formatted}` : formatted;
}

function delta(left: number | null | undefined, right: number | null | undefined): number | null {
  return left === null || left === undefined || right === null || right === undefined ? null : right - left;
}

export default function Harness({ context }: PageProps) {
  const { request, setQuery, navigateToPage } = context;
  const [loading, setLoading] = useState(true);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [activeTab, setActiveTab] = useState<'side-by-side' | 'delta' | 'models' | 'trends'>('side-by-side');
  const [summaries, setSummaries] = useState<ScopeMap<SummaryValues>>(emptyScopeMap<SummaryValues>());
  const [speeds, setSpeeds] = useState<ScopeMap<SpeedResponse>>(emptyScopeMap<SpeedResponse>());
  const [modelsByScope, setModelsByScope] = useState<ScopeMap<ModelTokenRow[]>>(emptyScopeMap<ModelTokenRow[]>());
  const [outcomesByHarness, setOutcomesByHarness] = useState<ScopeMap<HarnessOutcomeSlice>>(emptyScopeMap());
  const [reliabilityByScope, setReliabilityByScope] = useState<ScopeMap<RequestReliability>>(emptyScopeMap());
  const [summaryMissingness, setSummaryMissingness] = useState<ScopeMap<SummaryResponse['missingness']>>(emptyScopeMap());
  const [daysByScope, setDaysByScope] = useState<ScopeMap<TimelineRow[]>>(emptyScopeMap());
  const [dayModelsByScope, setDayModelsByScope] = useState<ScopeMap<TimelineRow[]>>(emptyScopeMap());
  const [distributions, setDistributions] = useState<ScopeMap<Distribution>>(emptyScopeMap());
  const [comparisons, setComparisons] = useState<ScopeMap<ComparisonResponse>>(emptyScopeMap());
  const [traces, setTraces] = useState<ScopeMap<TraceResponse>>(emptyScopeMap());
  const [unpricedByScope, setUnpricedByScope] = useState<ScopeMap<number>>(emptyScopeMap<number>());

  const loadComparison = useCallback(async () => {
    setLoading(true);
    setErrorMessage(null);
    try {
      const scopeResults = await Promise.all(HARNESS_SCOPES.map(async (scope) => {
        const [summary, speed, tokens, days, dayModels, outcomes, trace] = await Promise.all([
          request<SummaryResponse>('summary', { harness_scope: scope }),
          request<SpeedResponse>('speed', { harness_scope: scope }),
          request<TokensResponse>('models', { harness_scope: scope, pagination: { limit: 1000, offset: 0 } }),
          request<TimelineResponse>('timeline', { harness_scope: scope, group_by: ['day'] }),
          request<TimelineResponse>('timeline', { harness_scope: scope, group_by: ['day', 'model'] }),
          request<OutcomesResponse>('outcomes', { harness_scope: scope }),
          request<TraceResponse>('traces', { harness_scope: scope }),
        ]);
        for (const result of [summary, speed, tokens, days, dayModels, outcomes, trace]) throwQueryError(result);
        const range = summary.query.time_range;
        const start = range.start && Date.parse(range.start);
        const end = range.end && Date.parse(range.end);
        let comparison: ComparisonResponse | undefined;
        if (typeof start === 'number' && typeof end === 'number' && Number.isFinite(start) && Number.isFinite(end) && end > start) {
          const timezone = range.timezone;
          comparison = await request<ComparisonResponse>('comparisons', {
            harness_scope: scope,
            comparison_periods: {
              period_a: { start: new Date(start - (end - start)).toISOString(), end: new Date(start).toISOString(), timezone },
              period_b: { start: new Date(start).toISOString(), end: new Date(end).toISOString(), timezone },
            },
          });
          throwQueryError(comparison);
        }
        return { scope, summary, speed, tokens, days, dayModels, outcomes, trace, comparison };
      }));

      const nextSummaries = emptyScopeMap<SummaryValues>();
      const nextSpeeds = emptyScopeMap<SpeedResponse>();
      const nextModels = emptyScopeMap<ModelTokenRow[]>();
      const nextUnpriced = emptyScopeMap<number>();
      const nextMissingness = emptyScopeMap<SummaryResponse['missingness']>();
      const nextDays = emptyScopeMap<TimelineRow[]>();
      const nextDayModels = emptyScopeMap<TimelineRow[]>();
      const nextDistributions = emptyScopeMap<Distribution>();
      const nextComparisons = emptyScopeMap<ComparisonResponse>();
      const nextOutcomes = emptyScopeMap<HarnessOutcomeSlice>();
      const nextTraces = emptyScopeMap<TraceResponse>();
      const nextReliability = emptyScopeMap<RequestReliability>();
      for (const result of scopeResults) {
        nextSummaries[result.scope] = result.summary.values;
        nextSpeeds[result.scope] = result.speed;
        nextModels[result.scope] = result.tokens.rows ?? [];
        nextUnpriced[result.scope] = result.summary.missingness?.unpriced_requests_count;
        nextMissingness[result.scope] = result.summary.missingness;
        nextDays[result.scope] = result.days.rows ?? [];
        nextDayModels[result.scope] = result.dayModels.rows ?? [];
        nextDistributions[result.scope] = result.summary.session_duration_distribution;
        nextComparisons[result.scope] = result.comparison;
        nextOutcomes[result.scope] = result.outcomes.values;
        nextReliability[result.scope] = result.outcomes.request_reliability;
        nextTraces[result.scope] = result.trace;
      }
      setReliabilityByScope(nextReliability);
      setSummaries(nextSummaries);
      setSpeeds(nextSpeeds);
      setModelsByScope(nextModels);
      setUnpricedByScope(nextUnpriced);
      setSummaryMissingness(nextMissingness);
      setDaysByScope(nextDays);
      setDayModelsByScope(nextDayModels);
      setDistributions(nextDistributions);
      setComparisons(nextComparisons);
      setOutcomesByHarness(nextOutcomes);
      setTraces(nextTraces);
    } catch (error: unknown) {
      setErrorMessage(error instanceof Error ? error.message : 'Failed to load harness comparison metrics');
    } finally {
      setLoading(false);
    }
  }, [request]);

  useEffect(() => {
    void loadComparison();
  }, [loadComparison, context.query]);

  const modelRows = useMemo(() => {
    const byModel = new Map<string, ModelByScope>();
    for (const scope of HARNESS_SCOPES) {
      for (const row of modelsByScope[scope] ?? []) {
        const key = JSON.stringify([row.model, row.provider, row.access_route, row.raw_model, row.pricing_version_id]);
        const grouped = byModel.get(key) ?? {};
        grouped[scope] = row;
        byModel.set(key, grouped);
      }
    }
    return Array.from(byModel, ([key, grouped]) => {
      const identity = grouped.combined ?? grouped['claude-code'] ?? grouped.omp!;
      return { key, model: identity.model ?? 'unknown', provider: identity.provider ?? 'unknown', route: identity.access_route, rawModel: identity.raw_model, version: identity.pricing_version_id, ...grouped };
    }).sort((left, right) => left.key.localeCompare(right.key));
  }, [modelsByScope]);

  const combined = summaries.combined;
  const hasUnresolvedOverlap = combined?.has_unresolved_overlap === true;

  const scopeLabels: Record<HarnessScope, string> = {
    'claude-code': 'Claude Code',
    omp: 'Oh My Pi',
    combined: 'Combined',
  };

  const inspectSessions = (scope: HarnessScope) => {
    setQuery((previous) => ({ ...previous, harness_scope: scope }));
    navigateToPage('sessions');
  };

  const inspectBucket = (scope: HarnessScope, row: TimelineRow) => {
    setQuery((previous) => ({
      ...previous, harness_scope: scope,
      time_range: { ...previous.time_range, start: row.bucket_start, end: row.bucket_end, preset: 'custom' },
      filters: row.model ? { ...previous.filters, model: [row.model] } : previous.filters,
    }));
    navigateToPage('sessions');
  };

  const deltaRows = [
    { label: 'Billable tokens', value: summaries['claude-code']?.total_tokens_status === 'exact' && summaries.omp?.total_tokens_status === 'exact' ? delta(summaries['claude-code']?.total_tokens, summaries.omp?.total_tokens) : null, format: formatDelta },
    { label: 'Requests', value: delta(summaries['claude-code']?.requests_count, summaries.omp?.requests_count), format: formatDelta },
    { label: 'Cache hit rate (percentage points)', value: delta(summaries['claude-code']?.cache_hit_rate, summaries.omp?.cache_hit_rate), format: (value: number | null) => value === null ? '—' : `${value > 0 ? '+' : ''}${(value * 100).toFixed(1)} pp` },
  ];

  const renderMetric = (label: string, value: string, title?: string) => (
    <div className="metric-row" key={label}>
      <span className="metric-name">{label}</span>
      <span className="metric-val" title={title}>{value}</span>
    </div>
  );

  return (
    <div className="harness-page">
      <div className="page-header-row">
        <div className="page-title-block">
          <h1>
            <span>Harness Comparison</span>
            <span className="badge-tag">Claude Code vs Oh My Pi vs Combined</span>
          </h1>
          <p>Compare the shared query engine's usage, cost, timing, and recorded task-outcome evidence across identical filters.</p>
        </div>
        <div className="header-badges">
          <div className="section-actions">
            <button type="button" className={`btn-secondary ${activeTab === 'side-by-side' ? 'active' : ''}`} onClick={() => setActiveTab('side-by-side')}>
              Side-by-Side
            </button>
            <button type="button" className={`btn-secondary ${activeTab === 'delta' ? 'active' : ''}`} onClick={() => setActiveTab('delta')}>
              Deltas & Differences
            </button>
            <button type="button" className={`btn-secondary ${activeTab === 'models' ? 'active' : ''}`} onClick={() => setActiveTab('models')}>
              Model Distribution
            </button>
            <button type="button" className={`btn-secondary ${activeTab === 'trends' ? 'active' : ''}`} onClick={() => setActiveTab('trends')}>
              Daily Trends
            </button>
            <button type="button" className="btn-secondary" onClick={() => void loadComparison()} disabled={loading}>
              {loading ? 'Refreshing…' : 'Refresh'}
            </button>
          </div>
        </div>
      </div>

      {hasUnresolvedOverlap && (
        <div className="overlap-warning-banner" role="status">
          <div className="overlap-icon">!</div>
          <div className="overlap-content">
            <strong>Combined token total is partial.</strong>
            <span>The query engine reports unresolved overlap for this selection; no overlap count or token difference is inferred here.</span>
          </div>
        </div>
      )}

      {errorMessage && <div className="error-banner" role="alert">{errorMessage}</div>}

      {activeTab === 'side-by-side' && (
        <div className="comparison-grid">
          {HARNESS_SCOPES.map((scope) => {
            const values = summaries[scope];
            const speedResponse = speeds[scope];
            const speed = speedResponse?.values;
            const timingBasis = speedResponse?.primary_timing_basis ?? speedResponse?.eligible?.timing_basis_used;
            const outcome = outcomesByHarness[scope];
            const trace = traces[scope];
            const reliability = reliabilityByScope[scope];
            return (
              <section className={`harness-column-card ${scope === 'combined' ? 'combined' : ''}`} key={scope}>
                <div className="harness-card-header">
                  <div className="harness-title">
                    {scopeLabels[scope]}
                    <span className="harness-badge">{scope}</span>
                  </div>
                  <button type="button" className="metric-evidence-link" onClick={() => inspectSessions(scope)}>
                    Inspect Sessions
                  </button>
                </div>
                <div className="metric-group">
                  <div className="metric-group-title">Usage</div>
                  {renderMetric('Billable tokens', formatNumber(values?.total_tokens))}
                  {renderMetric('Token status', values?.total_tokens_status ?? 'unavailable')}
                  {renderMetric('Requests', formatNumber(values?.requests_count))}
                  {renderMetric('Sessions', formatNumber(values?.sessions_count), values?.sessions_count === null ? SESSION_IDS_MISSING_TITLE : undefined)}
                  {renderMetric('Fresh input', withStatus(formatNumber(values?.fresh_input_tokens), values?.token_fields_status?.fresh_input_tokens))}
                  {renderMetric('Cache read', withStatus(formatNumber(values?.cache_read_tokens), values?.token_fields_status?.cache_read_tokens))}
                  {renderMetric('5m cache writes', withStatus(formatNumber(values?.cache_write_5m_tokens), values?.token_fields_status?.cache_write_5m_tokens))}
                  {renderMetric('1h cache writes', withStatus(formatNumber(values?.cache_write_1h_tokens), values?.token_fields_status?.cache_write_1h_tokens))}
                  {renderMetric('Unknown-TTL cache writes', withStatus(formatNumber(values?.cache_write_unknown_ttl_tokens), values?.token_fields_status?.cache_write_unknown_ttl_tokens))}
                  {renderMetric('Output tokens', withStatus(formatNumber(values?.output_tokens), values?.token_fields_status?.output_tokens))}
                  {renderMetric('Reasoning (subset of output)', withStatus(formatNumber(values?.reasoning_tokens), values?.token_fields_status?.reasoning_tokens))}
                  {renderMetric('Mean recorded session span (ms)', formatNumber(values?.session_duration_ms, 1))}
                  {renderMetric('Recorded spans / linked sessions', `${formatNumber(summaryMissingness[scope]?.session_duration_observed_count)} / ${formatNumber(summaryMissingness[scope]?.session_duration_sessions_count)} (not active time)`)}
                  {renderMetric('Stored span p50 / p90 / p95 (ms)', `${formatNumber(distributions[scope]?.p50)} / ${formatNumber(distributions[scope]?.p90)} / ${formatNumber(distributions[scope]?.p95)}`)}
                  {renderMetric('Span distribution coverage / basis', `${formatNumber(distributions[scope]?.count)} / ${formatNumber(distributions[scope]?.linked_sessions)} · ${distributions[scope]?.status ?? 'unavailable'} · ${distributions[scope]?.basis ?? 'unavailable'} · ${distributions[scope]?.method ?? 'unavailable'} (source spans may be inferred)`)}
                </div>
                <div className="metric-group">
                  <div className="metric-group-title">Cost & cache</div>
                  {renderMetric('Cache hit rate', withStatus(formatRatio(values?.cache_hit_rate), values?.token_fields_status?.fresh_input_tokens !== 'exact' || values?.token_fields_status?.cache_read_tokens !== 'exact' ? 'partial inputs' : 'exact'))}
                  {renderMetric('API-equivalent INR', withStatus(formatCost(values?.api_equivalent_usd), values?.api_equivalent_usd_status))}
                  {renderMetric('Cache savings INR', withStatus(formatCost(values?.cache_savings_usd), values?.cache_savings_usd_status))}
                  {renderMetric('Unpriced requests', formatNumber(unpricedByScope[scope]))}
                </div>
                <div className="metric-group">
                  <div className="metric-group-title">Timing (basis-dependent)</div>
                  {renderMetric('Weighted generation speed', speed?.generation_speed_tps_weighted == null ? '—' : `${formatNumber(speed.generation_speed_tps_weighted, 2)} tokens/sec`)}
                  {renderMetric('Median TTFT', speed?.ttft_ms_median == null ? '—' : `${formatNumber(speed.ttft_ms_median, 2)} ms`)}
                  {renderMetric('Median request duration', speed?.duration_ms_median == null ? '—' : `${formatNumber(speed.duration_ms_median, 2)} ms`)}
                  {renderMetric('Generation samples / requests', `${formatNumber(speedResponse?.eligible?.timed_generation_requests)} / ${formatNumber(speedResponse?.eligible?.total_requests)}`)}
                  {renderMetric('TTFT samples / requests', `${formatNumber(speedResponse?.eligible?.timed_ttft_requests)} / ${formatNumber(speedResponse?.eligible?.total_requests)}`)}
                  {renderMetric('Duration samples / requests', `${formatNumber(speedResponse?.eligible?.timed_duration_requests)} / ${formatNumber(speedResponse?.eligible?.total_requests)}`)}
                  {renderMetric('Timing basis', timingBasis ?? 'unavailable')}
                </div>
                <div className="metric-group">
                  <div className="metric-group-title">Recorded task outcomes</div>
                  {renderMetric('Outcome evidence', outcome?.adequate_sample ? 'Adequate descriptive sample; insufficient outcome evidence for matched ranking' : `Insufficient outcome evidence: ${outcome?.abstention_reason ?? 'No comparable completions'}`)}
                  {renderMetric('Completed / total tasks', `${formatNumber(outcome?.completed_tasks)} / ${formatNumber(outcome?.total_tasks)}`)}
                  {renderMetric('Manual / inferred outcomes', `${formatNumber(outcome?.user_owned_count)} / ${formatNumber(outcome?.inferred_count)}`)}
                  {renderMetric('Tokens / completed task', formatNumber(outcome?.tokens_per_completed_task))}
                  {renderMetric('Summed request seconds / completed task', formatNumber(outcome?.time_per_completed_task_seconds, 3))}
                  {renderMetric('Fully timed / completed tasks', `${formatNumber(outcome?.timed_completed_tasks)} / ${formatNumber(outcome?.completed_tasks)}`)}
                  {renderMetric('Retry requests / completed task', formatNumber(outcome?.retry_requests_per_completed_task, 2))}
                  {renderMetric('Retry-covered / completed tasks', `${formatNumber(outcome?.retry_covered_completed_tasks)} / ${formatNumber(outcome?.completed_tasks)}`)}
                  {renderMetric('Estimated API-equivalent INR / completion', formatCost(outcome?.cost_per_success_usd))}
                  <p>Session task units within these filters; request-duration sum is not wall-clock task elapsed time. Cost is a priced estimate, not billed spend or a quality score. Unknown duration, retry identity, or pricing withholds that measure. No ranking: task labels alone do not establish matched task/outcome identity across harnesses.</p>
                </div>
                <div className="metric-group">
                  <div className="metric-group-title">Canonical request reliability</div>
                  {renderMetric('Status-covered / eligible requests', `${formatNumber(reliability?.status_covered_requests)} / ${formatNumber(reliability?.eligible_requests)} · ${reliability?.coverage ?? 'unavailable'}`)}
                  {renderMetric('Unknown request statuses', formatNumber(reliability?.unknown_status_requests))}
                  {Object.entries(reliability?.statuses ?? {}).map(([status, value]) => renderMetric(`${status}: count / status-covered rate`, `${value.count} / ${formatRatio(value.rate)} (denominator ${reliability?.status_covered_requests})`))}
                  {renderMetric('All failed requests / status-covered rate', `${formatNumber(reliability?.failed_requests.count)} / ${formatRatio(reliability?.failed_requests.rate)}`)}
                  {renderMetric('Retry requests / retry-covered rate', `${formatNumber(reliability?.retry_requests.count)} / ${formatRatio(reliability?.retry_requests.rate)} (denominator ${formatNumber(reliability?.retry_covered_requests)}; unknown ${formatNumber(reliability?.retry_unknown_requests)})`)}
                  <p>Only explicit canonical request statuses enter failure rates. Tool failures and suspected retry signals below are not request failures. Unknown statuses are excluded, not successes.</p>
                  <div className="metric-group-title">Request-linked tools, reliability and subagents</div>
                  <p>Events are linked to filtered request IDs. Session-level events ({formatNumber(trace?.session_level_events?.length)}) are excluded: their request and period attribution is unknown. Observed signals are not proof of waste.</p>
                  {renderMetric('Tool calls / subagent events', `${formatNumber(trace?.values?.tool_calls_count)} / ${formatNumber(trace?.values?.subagent_calls_count)}`)}
                  {renderMetric('Linked subagent sessions', formatNumber(trace?.values?.subagent_sessions_count))}
                  {renderMetric('Requests with observed retry/error signals / suspected signals', `${formatNumber(trace?.retry_loops?.retry_requests_count)} / ${formatNumber(trace?.retry_loops?.retry_sequences_count)}`)}
                  {renderMetric('Tool / subagent source coverage', `${trace?.source_coverage?.tools ?? 'unavailable'} / ${trace?.source_coverage?.subagents ?? 'unavailable'}`)}
                  {(trace?.tools_breakdown ?? []).map((tool) => <div key={tool.tool_name}>
                    <strong>{tool.tool_name}</strong>
                    {renderMetric('Calls; success / errors / unknown', `${tool.invocations_count}; ${tool.success_count} / ${tool.error_count} / ${tool.status_unobserved_count}`)}
                    {renderMetric('Duration ms / timed calls', `${tool.timed_invocations_count ? formatNumber(tool.total_duration_ms, 2) : '—'} / ${tool.timed_invocations_count}`)}
                    {renderMetric('Output bytes / observed calls', `${tool.output_size_observed_count ? formatNumber(tool.total_output_size_bytes) : '—'} / ${tool.output_size_observed_count}`)}
                  </div>)}
                  {(trace?.retry_loops?.suspected_waste_signals ?? []).map((signal, index) => <p key={`${signal.event_id ?? signal.canonical_id}-${index}`}>{signal.classification}: {signal.evidence} · request {signal.canonical_id ?? '—'} · event {signal.event_id ?? '—'} · session {signal.session_id ?? '—'}</p>)}
                  {(trace?.subagent_events ?? []).map((event) => <p key={event.event_id}>Subagent {event.event_name} · event {event.event_id} · request {event.canonical_id ?? '—'} · session {event.session_id ?? '—'}</p>)}
                  <button type="button" className="metric-evidence-link" onClick={() => inspectSessions(scope)}>Inspect source sessions</button>
                </div>
                {loading && <div className="unavailable-metric">Loading metrics…</div>}
              </section>
            );
          })}
        </div>
      )}

      {activeTab === 'delta' && (
        <section className="content-section">
          <div className="section-header">
            <div>
              <h2 className="section-title">Oh My Pi − Claude Code</h2>
              <p className="section-subtitle">Descriptive differences are not matched-workload or causal comparisons. Token deltas require exact coverage; cache rates depend on observed inputs.</p>
            </div>
          </div>
          <div className="section-body">
            <div className="metric-group">
              {deltaRows.map((row) => renderMetric(row.label, row.format(row.value)))}
            </div>
          </div>
        </section>
      )}

      {activeTab === 'models' && (
        <section className="content-section">
          <div className="section-header">
            <div>
              <h2 className="section-title">Model usage by harness</h2>
              <p className="section-subtitle">Model, provider, route, raw identity and pricing version remain separate and aligned across harnesses. Up to 1,000 identity rows per harness are shown. Sessions are distinct linked sessions, not additive across identities. Missing fields are unavailable, not zero; cache denominator is fresh input plus reads. Fees are not allocated.</p>
            </div>
          </div>
          <div className="section-body">
            <div className="table-container">
              <table className="data-table">
                <thead><tr>
                  <th>Model / provider / route / harness</th><th>Requests / linked sessions</th><th>Billable tokens</th><th>Fresh input</th><th>Cache read</th><th>Writes 5m / 1h / unknown TTL</th><th>Output / reasoning subset</th><th>Reported total</th><th>Cache hit / eligible tokens</th><th>API-equivalent USD / priced requests</th><th>Cache savings USD</th>
                </tr></thead>
                <tbody>
                  {modelRows.length === 0 ? <tr><td colSpan={11}>No model-grouped token rows for this query.</td></tr> : modelRows.flatMap((row) => HARNESS_SCOPES.map((scope) => {
                    const item = row[scope];
                    const token = (field: keyof ModelTokenRow) => withStatus(formatNumber(item?.[field] as number | null | undefined), item?.token_fields_status?.[field]);
                    return <tr key={`${row.key}-${scope}`}>
                      <td><button type="button" className="metric-evidence-link" onClick={() => {
                        setQuery((previous) => ({ ...previous, harness_scope: scope, filters: { ...previous.filters, model: [row.model], provider: [row.provider], route: row.route ? [row.route] : undefined } }));
                        navigateToPage('sessions');
                      }}>{row.model} · {row.provider} · {row.route ?? 'unknown route'} · {scopeLabels[scope]}</button><small>Raw: {row.rawModel ?? 'unknown'} · price version: {row.version ?? 'unknown'}</small></td>
                      <td title={item?.sessions_count === null ? SESSION_IDS_MISSING_TITLE : undefined}>{item ? `${formatNumber(item.requests_count)} / ${formatNumber(item.sessions_count)}` : '—'}</td>
                      <td>{withStatus(formatNumber(item?.total_tokens), item?.total_tokens_status)}</td>
                      <td>{token('fresh_input_tokens')}</td><td>{token('cache_read_tokens')}</td>
                      <td>{token('cache_write_5m_tokens')} / {token('cache_write_1h_tokens')} / {token('cache_write_unknown_ttl_tokens')}</td>
                      <td>{token('output_tokens')} / {token('reasoning_tokens')}</td><td>{token('reported_total_tokens')}</td>
                      <td>{withStatus(formatRatio(item?.cache_hit_rate), item && (item.token_fields_status?.fresh_input_tokens !== 'exact' || item.token_fields_status?.cache_read_tokens !== 'exact') ? 'partial inputs' : undefined)} / {formatNumber(item?.eligible_prompt_tokens)}</td>
                      <td>{withStatus(formatCost(item?.api_equivalent_usd), item?.api_equivalent_usd_status)} / {formatNumber(item?.priced_requests_count)} of {formatNumber(item?.requests_count)}</td>
                      <td>{withStatus(formatCost(item?.cache_savings_usd), item?.cache_savings_usd_status)}</td>
                    </tr>;
                  }))}
                </tbody>
              </table>
            </div>
            <h3>Timing by model, route, and model / route</h3>
            <p>Selected timing basis per harness; samples and tails use nearest-rank percentiles. Full request counts include untimed and other-basis requests.</p>
            <div className="table-container"><table className="data-table">
              <thead><tr><th>Harness / dimension</th><th>Requests / primary basis</th><th>Generation / TTFT / duration samples</th><th>Weighted TPS / p95 TPS</th><th>TTFT p50 / p95 ms</th><th>Duration p95 ms</th><th>Basis</th></tr></thead>
              <tbody>{HARNESS_SCOPES.flatMap((scope) => {
                const slices = speeds[scope]?.slices;
                const rows: Array<[string, TimingSlice]> = [
                  ...Object.entries(slices?.model ?? {}).map(([key, item]): [string, TimingSlice] => [`model: ${key}`, item]),
                  ...Object.entries(slices?.route ?? {}).map(([key, item]): [string, TimingSlice] => [`route: ${key}`, item]),
                  ...Object.entries(slices?.model_route ?? {}).flatMap(([model, routes]) =>
                    Object.entries(routes).map(([route, item]): [string, TimingSlice] => [`model: ${model} / route: ${route}`, item])),
                ];
                return rows.map(([key, item]) =>
                  <tr key={`${scope}-${key}`}><td>{scopeLabels[scope]} · {key}</td>
                    <td>{item.counts.total_requests} / {item.counts.primary_basis_requests}</td>
                    <td>{item.counts.eligible_generation_requests} / {item.counts.eligible_ttft_requests} / {item.counts.eligible_total_duration_requests}</td>
                    <td>{formatNumber(item.tps.weighted, 2)} / {formatNumber(item.tps.p95, 2)}</td>
                    <td>{formatNumber(item.distributions.ttft_ms.p50, 2)} / {formatNumber(item.distributions.ttft_ms.p95, 2)}</td>
                    <td>{formatNumber(item.distributions.duration_ms.p95, 2)}</td><td>{speeds[scope]?.primary_timing_basis ?? 'unavailable'}</td></tr>);
              })}</tbody>
            </table></div>
          </div>
        </section>
      )}

      {activeTab === 'trends' && <section className="content-section">
        <div className="section-header"><div><h2 className="section-title">Daily observed usage and shares</h2>
          <p className="section-subtitle">Local days are clipped to the selected range; boundary days can be partial. Session counts are distinct within each bucket, not additive across days or models. Shares use combined canonical counts and are withheld for unresolved overlap; token shares also require exact coverage. Active minutes are unions of explicit request-event intervals, not session spans.</p></div></div>
        <div className="section-body"><div className="table-container"><table className="data-table">
          <thead><tr><th>Day / harness</th><th>Requests / combined share</th><th>Linked sessions / combined share</th><th>Billable tokens / combined share</th><th>API-equivalent INR</th><th>Active minutes / timed requests</th></tr></thead>
          <tbody>{HARNESS_SCOPES.flatMap((scope) => (daysByScope[scope] ?? []).map((row) => {
            const total = daysByScope.combined?.find((item) => item.bucket === row.bucket);
            const share = (value: number | null | undefined, denominator: number | null | undefined) =>
              hasUnresolvedOverlap || value == null || denominator == null || denominator <= 0 ? '—' : formatRatio(value / denominator);
            return <tr key={`${scope}-${row.bucket}`}><td><button type="button" className="metric-evidence-link" onClick={() => inspectBucket(scope, row)}>{row.bucket} · {scopeLabels[scope]}</button></td>
              <td>{formatNumber(row.requests_count)} / {share(row.requests_count, total?.requests_count)}</td>
              <td title={row.sessions_count === null ? SESSION_IDS_MISSING_TITLE : undefined}>{formatNumber(row.sessions_count)} / {share(row.sessions_count, total?.sessions_count)}</td>
              <td>{withStatus(formatNumber(row.total_tokens), row.total_tokens_status)} / {row.total_tokens_status === 'exact' && total?.total_tokens_status === 'exact' ? share(row.total_tokens, total.total_tokens) : '—'}</td>
              <td>{withStatus(formatCost(row.api_equivalent_usd), row.api_equivalent_usd_status)}</td>
              <td>{withStatus(formatNumber(row.active_minutes, 2), row.active_minutes_status)} / {formatNumber(row.active_minutes_timed_requests)} · {row.active_minutes_basis}</td></tr>;
          }))}</tbody>
        </table></div>
        <h3>Daily model mix</h3><p>Request share within each harness and day; model sessions overlap and are not additive. Links preserve the clipped day and model.</p>
        <div className="table-container"><table className="data-table"><thead><tr><th>Day / model / harness</th><th>Requests / harness-day share</th><th>Sessions</th><th>Tokens</th></tr></thead>
          <tbody>{HARNESS_SCOPES.flatMap((scope) => (dayModelsByScope[scope] ?? []).map((row) => {
            const total = daysByScope[scope]?.find((item) => item.bucket === row.bucket)?.requests_count;
            return <tr key={`${scope}-${row.bucket}-${row.model}`}><td>{row.model && row.model !== 'unknown' ? <button type="button" className="metric-evidence-link" onClick={() => inspectBucket(scope, row)}>{row.bucket} · {row.model} · {scopeLabels[scope]}</button> : `${row.bucket} · unknown model · ${scopeLabels[scope]} (model filter unavailable)`}</td>
              <td>{formatNumber(row.requests_count)} / {total ? formatRatio(row.requests_count / total) : '—'}</td><td title={row.sessions_count === null ? SESSION_IDS_MISSING_TITLE : undefined}>{formatNumber(row.sessions_count)}</td><td>{withStatus(formatNumber(row.total_tokens), row.total_tokens_status)}</td></tr>;
          }))}</tbody></table></div>
        <h3>Previous equivalent period</h3><p>Same elapsed duration, filters and harness scope. Partial figures are observed subsets, not complete totals; session outcomes are linked-session evidence. Model mix and DST differences are reported by the comparison analyzer; no causal claim is implied.</p>
        {HARNESS_SCOPES.map((scope) => {
          const comparison = comparisons[scope];
          return <div key={scope} className="metric-group"><strong>{scopeLabels[scope]}</strong>
            {comparison?.period_a && comparison.period_b ? <>
              {renderMetric('Previous [start, end)', `${comparison.period_a.start} – ${comparison.period_a.end}`)}
              {renderMetric('Selected [start, end)', `${comparison.period_b.start} – ${comparison.period_b.end}`)}
              {renderMetric('Requests previous → selected', `${formatNumber(comparison.period_a.metrics.requests_count)} → ${formatNumber(comparison.period_b.metrics.requests_count)}`)}
              {renderMetric('Billable tokens previous → selected', `${withStatus(formatNumber(comparison.period_a.metrics.total_tokens), comparison.period_a.metrics.coverage.total_tokens)} → ${withStatus(formatNumber(comparison.period_b.metrics.total_tokens), comparison.period_b.metrics.coverage.total_tokens)}`)}
              {renderMetric('API-equivalent INR previous → selected', `${withStatus(formatCost(comparison.period_a.metrics.api_equivalent_usd), comparison.period_a.metrics.coverage.api_equivalent_usd)} → ${withStatus(formatCost(comparison.period_b.metrics.api_equivalent_usd), comparison.period_b.metrics.coverage.api_equivalent_usd)}`)}
              {renderMetric('Cache savings INR previous → selected', `${withStatus(formatCost(comparison.period_a.metrics.cache_savings_usd), comparison.period_a.metrics.coverage.cache_savings_usd)} → ${withStatus(formatCost(comparison.period_b.metrics.cache_savings_usd), comparison.period_b.metrics.coverage.cache_savings_usd)}`)}
              {renderMetric('Cache hit rate previous → selected', `${withStatus(formatRatio(comparison.period_a.metrics.cache_hit_rate), comparison.period_a.metrics.coverage.cache_hit_rate)} → ${withStatus(formatRatio(comparison.period_b.metrics.cache_hit_rate), comparison.period_b.metrics.coverage.cache_hit_rate)}`)}
              {renderMetric('Priced requests previous → selected', `${formatNumber(comparison.period_a.metrics.priced_requests)} → ${formatNumber(comparison.period_b.metrics.priced_requests)}`)}
              {renderMetric('Completed / tasks previous → selected', `${formatNumber(comparison.period_a.metrics.outcomes.completed_tasks)} / ${formatNumber(comparison.period_a.metrics.outcomes.total_tasks)} → ${formatNumber(comparison.period_b.metrics.outcomes.completed_tasks)} / ${formatNumber(comparison.period_b.metrics.outcomes.total_tasks)}`)}
              {renderMetric('Outcome adequacy previous → selected', `${comparison.period_a.metrics.outcomes.adequate_sample ? 'adequate' : 'sparse'} → ${comparison.period_b.metrics.outcomes.adequate_sample ? 'adequate' : 'sparse'}`)}
              {renderMetric('Comparison status', comparison.matched_status ?? 'unavailable')}
              {(comparison.confounders ?? []).map((item) => <p key={item}>{item}</p>)}
            </> : <p>Unavailable without a bounded selected range.</p>}
          </div>;
        })}
        </div>
      </section>}
    </div>
  );
}
