import React, { useState, useEffect, useMemo } from 'react';
import type { PageProps, QueryResponse } from '../App';
import './Models.css';

// Exact six canonical display identities per Rebuild Plan
export const CANONICAL_DISPLAY_IDENTITIES = [
  'Opus 5.5',
  'Sonnet 5.5',
  'Haiku 5.5',
  'Sol 6.1',
  'Luna Max',
  'Gemini 3.8 Flash',
] as const;

type CanonicalModelName = typeof CANONICAL_DISPLAY_IDENTITIES[number];

type ActiveTab = 'profiles' | 'comparison' | 'tasks' | 'switching' | 'reliability';

interface ModelProfile {
  display_name: string;
  canonical_model_id: string;
  provider: string;
  is_canonical: boolean;
  raw_models_observed: string[];
  requests_count: number;
  sessions_count: number;
  tokens: {
    total_tokens: number;
    fresh_input_tokens: number;
    cache_read_tokens: number;
    cache_write_5m_tokens: number;
    cache_write_1h_tokens: number;
    cache_write_unknown_ttl_tokens: number;
    output_tokens: number;
    reasoning_tokens: number;
    reasoning_pct_of_output: number | null;
  };
  cache: {
    eligible_prompt_tokens: number;
    cache_read_tokens: number;
    cache_hit_rate: number | null;
    cache_savings_usd: number;
  };
  cost: {
    api_equivalent_usd: number | null;
    api_equivalent_inr: number | null;
    actual_billed_amount: number | null;
    priced_requests_count: number;
    unpriced_requests_count: number;
    cost_per_request_usd: number | null;
  };
  timing: {
    eligible_timing_requests_count: number;
    unobserved_timing_requests_count: number;
    sum_generation_seconds: number;
    weighted_tps: number | null;
    tps_distribution: {
      p50: number | null;
      p90: number | null;
      p95: number | null;
      mean: number | null;
    };
  };
  deep_ttft: {
    eligible_ttft_count: number;
    distribution_ms: {
      count: number;
      min: number | null;
      max: number | null;
      p50: number | null;
      p90: number | null;
      p95: number | null;
      mean: number | null;
    };
  };
  duration: {
    eligible_duration_count: number;
    distribution_ms: {
      p50: number | null;
      p95: number | null;
    };
  };
  reliability: {
    total_requests: number;
    known_status_requests: number;
    missing_status_requests: number;
    ok_count: number;
    error_count: number;
    interrupted_count: number;
    rate_limited_count: number;
    timeout_count: number;
    abnormal_termination_count: number;
    error_rate: number | null;
    retry_requests_count: number;
    retry_tokens: number;
    missing_status_caveat: string | null;
  };
  project_mix: Array<{
    project_id: string;
    requests_count: number;
    total_tokens: number;
    cost_usd: number | null;
  }>;
  session_trends: Array<{
    date: string;
    requests_count: number;
    total_tokens: number;
    cost_usd: number | null;
    output_tokens: number;
    cache_hit_rate: number | null;
  }>;
  session_mix?: Array<{session_id: string; requests_count: number; total_tokens: number}>;
  sample_counts: {
    requests: number;
    sessions: number;
    priced_requests: number;
    unpriced_requests: number;
    timed_requests: number;
    untimed_requests: number;
    ttft_samples: number;
  };
  missingness: {
    missing_pricing_count: number;
    missing_timing_count: number;
    missing_ttft_count: number;
    missing_status_count: number;
  };
}
interface ComparisonMetric {
  value: number | null;
  sample_count: number;
  denominator: number;
  basis: string;
  missing_count: number;
}
interface ComparisonRow {
  model_identity: string;
  sample_count: number;
  is_sparse: boolean;
  metrics: Record<string, ComparisonMetric>;
}
interface ModelResponse extends QueryResponse {
  comparison?: {
    task_cohorts: Array<{task_label: string; models: ComparisonRow[]}>;
    models_compared: ComparisonRow[];
    matched_filters: unknown;
  };
}

export default function Models({ context }: PageProps) {
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);
  const [data, setData] = useState<ModelResponse | null>(null);
  const [activeTab, setActiveTab] = useState<ActiveTab>('profiles');
  const [selectedModel, setSelectedModel] = useState<string>('Sonnet 5.5');
  const [comparisonTask, setComparisonTask] = useState('');
  const [selectedForCompare, setSelectedForCompare] = useState<string[]>([
    'Sonnet 5.5',
    'Opus 5.5',
  ]);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);

    // Request model_profiles family
    context
      .request('model_profiles')
      .then((res) => {
        if (!cancelled) {
          setData(res);
          setLoading(false);
        }
      })
      .catch((err) => {
        if (!cancelled) {
          // Attempt fallback to 'models' family if backend has models registered
          context
            .request('models')
            .then((resFallback) => {
              if (!cancelled) {
                setData(resFallback);
                setLoading(false);
              }
            })
            .catch((fallbackErr) => {
              if (!cancelled) {
                setError(err?.message || fallbackErr?.message || 'Failed to load model analytics');
                setLoading(false);
              }
            });
        }
      });

    return () => {
      cancelled = true;
    };
  }, [context.query]);

  // Extract profiles, ensuring all six canonical models exist
  const profiles: Record<string, ModelProfile> = useMemo(() => {
    const rawProfiles = (data as any)?.profiles || {};
    const result: Record<string, ModelProfile> = {};

    // First ensure all six canonical identities are present
    for (const name of CANONICAL_DISPLAY_IDENTITIES) {
      if (rawProfiles[name]) {
        result[name] = rawProfiles[name];
      } else {
        // Fallback initialized structure
        result[name] = {
          display_name: name,
          canonical_model_id: name.toLowerCase().replace(/[\s\.]+/g, '-'),
          provider: name.includes('Opus') || name.includes('Sonnet') || name.includes('Haiku')
            ? 'anthropic'
            : name.includes('Sol') || name.includes('Luna')
            ? 'openai'
            : 'google',
          is_canonical: true,
          raw_models_observed: [],
          requests_count: 0,
          sessions_count: 0,
          tokens: {
            total_tokens: 0,
            fresh_input_tokens: 0,
            cache_read_tokens: 0,
            cache_write_5m_tokens: 0,
            cache_write_1h_tokens: 0,
            cache_write_unknown_ttl_tokens: 0,
            output_tokens: 0,
            reasoning_tokens: 0,
            reasoning_pct_of_output: null,
          },
          cache: {
            eligible_prompt_tokens: 0,
            cache_read_tokens: 0,
            cache_hit_rate: null,
            cache_savings_usd: 0,
          },
          cost: {
            api_equivalent_usd: null,
            api_equivalent_inr: null,
            actual_billed_amount: null,
            priced_requests_count: 0,
            unpriced_requests_count: 0,
            cost_per_request_usd: null,
          },
          timing: {
            eligible_timing_requests_count: 0,
            unobserved_timing_requests_count: 0,
            sum_generation_seconds: 0,
            weighted_tps: null,
            tps_distribution: { p50: null, p90: null, p95: null, mean: null },
          },
          deep_ttft: {
            eligible_ttft_count: 0,
            distribution_ms: { count: 0, min: null, max: null, p50: null, p90: null, p95: null, mean: null },
          },
          duration: { eligible_duration_count: 0, distribution_ms: { p50: null, p95: null } },
          reliability: {
            total_requests: 0,
            known_status_requests: 0,
            missing_status_requests: 0,
            ok_count: 0,
            error_count: 0,
            interrupted_count: 0,
            rate_limited_count: 0,
            timeout_count: 0,
            abnormal_termination_count: 0,
            error_rate: null,
            retry_requests_count: 0,
            retry_tokens: 0,
            missing_status_caveat: null,
          },
          project_mix: [],
          session_trends: [],
          sample_counts: {
            requests: 0,
            sessions: 0,
            priced_requests: 0,
            unpriced_requests: 0,
            timed_requests: 0,
            untimed_requests: 0,
            ttft_samples: 0,
          },
          missingness: {
            missing_pricing_count: 0,
            missing_timing_count: 0,
            missing_ttft_count: 0,
            missing_status_count: 0,
          },
        };
      }
    }

    // Merge any unmatched models
    for (const [k, v] of Object.entries(rawProfiles)) {
      if (!result[k]) {
        result[k] = v as ModelProfile;
      }
    }

    return result;
  }, [data]);

  const activeProfile = profiles[selectedModel] || profiles['Sonnet 5.5'];

  // Toggle selection for comparison
  const toggleCompareModel = (modelName: string) => {
    setSelectedForCompare((prev) => {
      if (prev.includes(modelName)) {
        return prev.length > 1 ? prev.filter((m) => m !== modelName) : prev;
      } else {
        return [...prev, modelName];
      }
    });
  };

  const taskCohorts = data?.comparison?.task_cohorts || [];
  const cohort = taskCohorts.find((entry) => entry.task_label === comparisonTask);
  const comparisonRows: ComparisonRow[] = selectedForCompare.map((name) =>
    (comparisonTask ? cohort?.models : data?.comparison?.models_compared)?.find(
      (entry) => entry.model_identity === name) ||
    {model_identity: name, sample_count: 0, is_sparse: true, metrics: {}});
  const matchedAdequate = Boolean(comparisonTask) && selectedForCompare.length >= 2 &&
    comparisonRows.every((entry) => entry.sample_count >= 5);
  const outcomeAdequate = matchedAdequate && comparisonRows.every(
    (entry) => entry.metrics.outcome_rating?.sample_count >= 5);
  const comparisonMetrics = [
    ['tokens_per_request', 'Average tokens/request'],
    ['median_output', 'Median output tokens'],
    ['median_tps', 'Median TPS'],
    ['median_ttft_ms', 'Median TTFT (ms)'],
    ['p90_ttft_ms', 'p90 TTFT (ms)'],
    ['p95_ttft_ms', 'p95 TTFT (ms)'],
    ['median_duration_ms', 'Median latency (ms)'],
    ['p90_duration_ms', 'p90 latency (ms)'],
    ['p95_duration_ms', 'p95 latency (ms)'],
    ['cache_hit_rate', 'Cache read rate'],
    ['retry_error_rate', 'Error rate'],
    ['retry_rate', 'Retry rate'],
    ['cost_per_request_usd', 'Cost/request (USD)'],
    ['outcome_rating', 'Outcome rating'],
  ];
  const money = (value: number | null | undefined, symbol = '$', digits = 2) =>
    value == null ? 'Unavailable' : `${symbol}${value.toFixed(digits)}`;

  const taskSliceRows = (data as any)?.task_slices?.rows || [];
  const switchingEvents = (data as any)?.switching_timeline?.events || [];

  return (
    <div className="models-page">
      {/* Page Header */}
      <div className="models-header">
        <div>
          <h1>Model Profiles & Analytics</h1>
          <div className="models-header-meta">
            <span>WP13a/b Phase 1 Core</span>
            <span>•</span>
            <span>Scope: {context.query.harness_scope}</span>
            <span>•</span>
            <span>All 6 Canonical Display Identities Configured</span>
          </div>
        </div>
      </div>

      {/* Navigation Tabs */}
      <div className="models-nav-tabs">
        <button
          className={`models-tab-btn ${activeTab === 'profiles' ? 'active' : ''}`}
          onClick={() => setActiveTab('profiles')}
        >
          Model Profiles
        </button>
        <button
          className={`models-tab-btn ${activeTab === 'comparison' ? 'active' : ''}`}
          onClick={() => setActiveTab('comparison')}
        >
          Side-By-Side Comparison ({selectedForCompare.length})
        </button>
        <button
          className={`models-tab-btn ${activeTab === 'tasks' ? 'active' : ''}`}
          onClick={() => setActiveTab('tasks')}
        >
          Task Slices & Verbosity
        </button>
        <button
          className={`models-tab-btn ${activeTab === 'switching' ? 'active' : ''}`}
          onClick={() => setActiveTab('switching')}
        >
          Switching Timeline ({switchingEvents.length})
        </button>
        <button
          className={`models-tab-btn ${activeTab === 'reliability' ? 'active' : ''}`}
          onClick={() => setActiveTab('reliability')}
        >
          Reliability & Retries
        </button>
      </div>

      {loading && <div className="sparse-banner">Loading model analytics...</div>}
      {error && <div className="sparse-banner" style={{ borderColor: '#ef4444', color: '#fca5a5' }}>{error}</div>}

      {/* Tab 1: Model Profiles Grid & Drilldown */}
      {activeTab === 'profiles' && (
        <>
          <div className="canonical-models-grid">
            {CANONICAL_DISPLAY_IDENTITIES.map((name) => {
              const prof = profiles[name];
              const isSelected = selectedModel === name;
              const isUnobserved = prof.requests_count === 0;

              return (
                <div
                  key={name}
                  className={`model-card ${isSelected ? 'selected' : ''} ${isUnobserved ? 'unobserved' : ''}`}
                  onClick={() => setSelectedModel(name)}
                >
                  <div className="model-card-head">
                    <h3 className="model-card-title">{name}</h3>
                    <span className={`model-badge ${prof.provider}`}>
                      {prof.provider}
                    </span>
                  </div>

                  <div className="model-card-stats">
                    <div className="stat-item">
                      <span className="stat-label">Requests</span>
                      <span className="stat-val">{prof.requests_count.toLocaleString()}</span>
                    </div>
                    <div className="stat-item">
                      <span className="stat-label">Total Tokens</span>
                      <span className="stat-val">{prof.tokens.total_tokens.toLocaleString()}</span>
                    </div>
                    <div className="stat-item">
                      <span className="stat-label">Cost (USD)</span>
                      <span className="stat-val">{money(prof.cost.api_equivalent_usd)}</span>
                    </div>
                    <div className="stat-item">
                      <span className="stat-label">Speed</span>
                      <span className="stat-val">
                        {prof.timing.weighted_tps !== null ? `${prof.timing.weighted_tps} tps` : '—'}
                      </span>
                    </div>
                  </div>

                  {isUnobserved && (
                    <div className="unobserved-notice">
                      No requests observed in current filter range
                    </div>
                  )}
                </div>
              );
            })}
          </div>

          {/* Active Profile Detail */}
          {activeProfile && (
            <div className="profile-detail-section">
              <div className="profile-detail-header">
                <div>
                  <h2 style={{ margin: 0, color: '#f8fafc' }}>
                    {activeProfile.display_name} Details
                  </h2>
                  <span style={{ fontSize: '0.85rem', color: '#94a3b8' }}>
                    Canonical ID: {activeProfile.canonical_model_id} | Provider: {activeProfile.provider}
                  </span>
                </div>
                <div>
                  <button
                    className="models-tab-btn"
                    onClick={() => toggleCompareModel(activeProfile.display_name)}
                  >
                    {selectedForCompare.includes(activeProfile.display_name)
                      ? '✓ In Comparison'
                      : '+ Add to Compare'}
                  </button>
                </div>
              </div>

              {/* Metric Boxes */}
              <div className="detail-metric-grid">
                <div className="metric-box">
                  <span className="metric-box-title">Total Tokens</span>
                  <span className="metric-box-val">
                    {activeProfile.tokens.total_tokens.toLocaleString()}
                  </span>
                  <span className="metric-box-sub">
                    Fresh: {activeProfile.tokens.fresh_input_tokens.toLocaleString()} | Output: {activeProfile.tokens.output_tokens.toLocaleString()}
                  </span>
                </div>

                <div className="metric-box">
                  <span className="metric-box-title">Reasoning Tokens</span>
                  <span className="metric-box-val">
                    {activeProfile.tokens.reasoning_tokens.toLocaleString()}
                  </span>
                  <span className="metric-box-sub">
                    {activeProfile.tokens.reasoning_pct_of_output !== null
                      ? `${activeProfile.tokens.reasoning_pct_of_output}% of output (reported subset)`
                      : 'None reported'}
                  </span>
                </div>

                <div className="metric-box">
                  <span className="metric-box-title">Cache Hit Rate</span>
                  <span className="metric-box-val">
                    {activeProfile.cache.cache_hit_rate !== null
                      ? `${(activeProfile.cache.cache_hit_rate * 100).toFixed(1)}%`
                      : '—'}
                  </span>
                  <span className="metric-box-sub">
                    Read: {activeProfile.cache.cache_read_tokens.toLocaleString()} | Eligible: {activeProfile.cache.eligible_prompt_tokens.toLocaleString()}
                  </span>
                </div>

                <div className="metric-box">
                  <span className="metric-box-title">Cost & Valuation</span>
                  <span className="metric-box-val">
                    {money(activeProfile.cost.api_equivalent_usd)}
                  </span>
                  <span className="metric-box-sub">
                    {money(activeProfile.cost.api_equivalent_inr, '₹')} INR | Billed: {money(activeProfile.cost.actual_billed_amount)}
                  </span>
                </div>

                <div className="metric-box">
                  <span className="metric-box-title">Deep TTFT (p50 / p95)</span>
                  <span className="metric-box-val">
                    {activeProfile.deep_ttft.distribution_ms.p50 !== null
                      ? `${activeProfile.deep_ttft.distribution_ms.p50} ms`
                      : '—'}
                  </span>
                  <span className="metric-box-sub">
                    p95: {activeProfile.deep_ttft.distribution_ms.p95 !== null ? `${activeProfile.deep_ttft.distribution_ms.p95} ms` : '—'} | Samples: {activeProfile.deep_ttft.eligible_ttft_count}
                  </span>
                </div>

                <div className="metric-box">
                  <span className="metric-box-title">Throughput (TPS)</span>
                  <span className="metric-box-val">
                    {activeProfile.timing.weighted_tps !== null
                      ? `${activeProfile.timing.weighted_tps} tps`
                      : '—'}
                  </span>
                  <span className="metric-box-sub">
                    Median: {activeProfile.timing.tps_distribution.p50 !== null ? `${activeProfile.timing.tps_distribution.p50} tps` : '—'}
                  </span>
                </div>
              </div>

              {/* Sample Counts and Missingness Badges */}
              <div style={{ display: 'flex', gap: '0.75rem', flexWrap: 'wrap', fontSize: '0.8rem' }}>
                <span className="pill pill-neutral">
                  Requests: {activeProfile.sample_counts.requests} ({activeProfile.sample_counts.sessions} sessions)
                </span>
                <span className="pill pill-neutral">
                  Priced: {activeProfile.sample_counts.priced_requests} / Unpriced: {activeProfile.sample_counts.unpriced_requests}
                </span>
                <span className="pill pill-neutral">
                  Timed: {activeProfile.sample_counts.timed_requests} / Untimed: {activeProfile.sample_counts.untimed_requests}
                </span>
                <span className="pill pill-neutral">
                  TTFT Samples: {activeProfile.sample_counts.ttft_samples}
                </span>
              </div>

              {/* Project Mix Table */}
              <h3 style={{ margin: '1rem 0 0.5rem 0', color: '#f8fafc' }}>Project Distribution</h3>
              <div className="models-table-container">
                <table className="models-table">
                  <thead>
                    <tr>
                      <th>Project ID</th>
                      <th>Requests</th>
                      <th>Total Tokens</th>
                      <th>Estimated Cost (USD)</th>
                    </tr>
                  </thead>
                  <tbody>
                    {activeProfile.project_mix.length > 0 ? (
                      activeProfile.project_mix.map((p) => (
                        <tr key={p.project_id}>
                          <td>{p.project_id}</td>
                          <td>{p.requests_count}</td>
                          <td>{p.total_tokens.toLocaleString()}</td>
                          <td>{money(p.cost_usd, '$', 3)}</td>
                        </tr>
                      ))
                    ) : (
                      <tr>
                        <td colSpan={4} style={{ textAlign: 'center', color: '#64748b' }}>
                          No project associations for this model in current filter window
                        </td>
                      </tr>
                    )}
                  </tbody>
                </table>
              </div>
              <h3>Session Mix</h3>
              <div className="models-table-container"><table className="models-table">
                <thead><tr><th>Session</th><th>Requests</th><th>Tokens</th></tr></thead>
                <tbody>{(activeProfile.session_mix || []).map((session) => <tr key={session.session_id}>
                  <td>{session.session_id}</td><td>{session.requests_count}</td><td>{session.total_tokens.toLocaleString()}</td>
                </tr>)}</tbody>
              </table></div>
              {!activeProfile.session_mix?.length && <p>No observed sessions in the matched filter.</p>}
              <h3>Daily Session Trends</h3>
              <div className="models-table-container"><table className="models-table">
                <thead><tr><th>Date</th><th>Requests</th><th>Tokens</th><th>Output tokens</th><th>Estimated USD</th><th>Cache read rate</th></tr></thead>
                <tbody>{activeProfile.session_trends.map((point) => <tr key={point.date}>
                  <td>{point.date}</td><td>{point.requests_count}</td><td>{point.total_tokens.toLocaleString()}</td>
                  <td>{point.output_tokens}</td><td>{money(point.cost_usd)}</td>
                  <td>{point.cache_hit_rate == null ? 'Unavailable' : `${(point.cache_hit_rate * 100).toFixed(1)}%`}</td>
                </tr>)}</tbody>
              </table></div>
              {!activeProfile.session_trends.length && <p>No trend observations in the matched filter.</p>}
            </div>
          )}
        </>
      )}

      {/* Tab 2: Side-By-Side Comparison */}
      {activeTab === 'comparison' && (
        <div className="comparison-container">
          <div style={{ display: 'flex', gap: '0.5rem', flexWrap: 'wrap' }}>
            <span style={{ alignSelf: 'center', fontSize: '0.875rem', color: '#94a3b8' }}>Select Models:</span>
            {CANONICAL_DISPLAY_IDENTITIES.map((name) => (
              <button
                key={name}
                className={`models-tab-btn ${selectedForCompare.includes(name) ? 'active' : ''}`}
                onClick={() => toggleCompareModel(name)}
              >
                {selectedForCompare.includes(name) ? '✓ ' : '+ '}
                {name} ({profiles[name]?.requests_count || 0})
              </button>
            ))}
          </div>

          <label>Reliable matched task{' '}
            <select value={comparisonTask} onChange={(event) => setComparisonTask(event.target.value)}>
              <option value="">All observed requests (unmatched descriptive metrics)</option>
              {taskCohorts.map((task) => <option key={task.task_label} value={task.task_label}>{task.task_label}</option>)}
            </select>
          </label>
          <div className="sparse-banner">
            {matchedAdequate ? 'Matched task with adequate request samples.' : 'Unmatched or sparse task samples: effectiveness comparison unavailable.'}
            {' '}{outcomeAdequate ? 'Adequate distinct rated outcomes.' : 'Outcome comparison unavailable: requires at least 5 distinct manual outcomes per selected model.'}
          </div>
          <details><summary>Identical matched filters for every model</summary>
            <pre style={{whiteSpace: 'pre-wrap'}}>{JSON.stringify(data?.comparison?.matched_filters || context.query, null, 2)}</pre>
          </details>
          <div className="models-table-container"><table className="models-table">
            <thead><tr><th>Metric / eligible denominator</th>{comparisonRows.map((row) =>
              <th key={row.model_identity}>{row.model_identity}<br />{row.sample_count} cohort requests</th>)}</tr></thead>
            <tbody>{comparisonMetrics.map(([key, label]) => <tr key={key}>
              <th>{label}</th>{comparisonRows.map((row) => {
                const metric = row.metrics[key];
                const value = key === 'outcome_rating' && !outcomeAdequate ? null : metric?.value;
                return <td key={row.model_identity}>
                  <strong>{value == null ? 'Unavailable' : key.endsWith('_rate') ? `${(value * 100).toFixed(1)}%` : key === 'cost_per_request_usd' ? money(value, '$', 5) : value}</strong>
                  <div>n={metric?.sample_count || 0}; denominator={metric?.denominator || 0}</div>
                  <small>{metric?.basis || 'No eligible observations'}; missing={metric?.missing_count ?? row.sample_count}</small>
                </td>;
              })}
            </tr>)}</tbody>
          </table></div>
          <div className="disclaimer-text">Descriptive metrics do not rank task quality. Outcome comparison is gated on matched reliable task labels and adequate distinct outcomes.</div>
        </div>
      )}

      {/* Tab 3: Task Slices & Verbosity */}
      {activeTab === 'tasks' && (
        <div className="comparison-container">
          <div className="models-table-container">
            <table className="models-table">
              <thead>
                <tr>
                  <th>Model</th>
                  <th>Task Label</th>
                  <th>Source / Verification</th>
                  <th>Requests</th>
                  <th>Total Tokens</th>
                  <th>Output / Req</th>
                  <th>Output distribution (tokens)</th>
                  <th>Out / In Ratio</th>
                  <th>Reasoning Tokens</th>
                  <th>Effort Setting</th>
                  <th>Median Duration</th>
                  <th>Outcome Rating</th>
                </tr>
              </thead>
              <tbody>
                {taskSliceRows.length > 0 ? (
                  taskSliceRows.map((r: any, idx: number) => (
                    <tr key={idx}>
                      <td style={{ fontWeight: 600 }}>{r.model_identity}</td>
                      <td>{r.task_label}</td>
                      <td>
                        {r.is_reliable_manual_label ? (
                          <span className="pill pill-success">Manual Verified</span>
                        ) : (
                          <span className="pill pill-neutral">Inferred / Unverified</span>
                        )}
                      </td>
                      <td>{r.requests_count}</td>
                      <td>{r.total_tokens.toLocaleString()}</td>
                      <td>{r.output_per_request}</td>
                      <td>{r.output_distribution?.count ? <>
                        median {r.output_distribution.p50}; p90 {r.output_distribution.p90}; p95 {r.output_distribution.p95};
                        range {r.output_distribution.min}–{r.output_distribution.max}
                        <div>n={r.output_distribution.count}/{r.requests_count}; missing={r.output_missing_count}; nearest rank</div>
                      </> : 'Unavailable: no output observations'}</td>
                      <td>{r.output_to_input_ratio ?? '—'}</td>
                      <td>
                        {r.reasoning_tokens.toLocaleString()}
                        {r.reasoning_pct_of_output !== null && ` (${r.reasoning_pct_of_output}%)`}
                      </td>
                      <td>
                        {r.effort_setting === 'missing' ? (
                          <span style={{ color: '#94a3b8', fontStyle: 'italic' }}>missing</span>
                        ) : (
                          r.effort_setting
                        )}
                      </td>
                      <td>{r.median_duration_ms !== null ? `${r.median_duration_ms} ms` : '—'}</td>
                      <td>
                        {r.manual_outcome_rating !== null ? (
                          <span className="pill pill-success">★ {r.manual_outcome_rating}</span>
                        ) : (
                          <span style={{ color: '#64748b' }}>Unavailable</span>
                        )}
                      </td>
                    </tr>
                  ))
                ) : (
                  <tr>
                    <td colSpan={12} style={{ textAlign: 'center', color: '#64748b' }}>
                      No task slices recorded in current window
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
          <div className="disclaimer-text">
            Output size alone is not a quality measure. Shorter output does not imply superior work.
          </div>
        </div>
      )}

      {/* Tab 4: Switching Timeline */}
      {activeTab === 'switching' && (
        <div className="comparison-container">
          <div className="timeline-list">
            {switchingEvents.length > 0 ? (
              switchingEvents.map((evt: any, idx: number) => (
                <div key={idx} className="timeline-event-card">
                  <div className="timeline-switch">
                    <span>{evt.from_model}</span>
                    <span className="timeline-arrow">➔</span>
                    <span style={{ color: '#38bdf8' }}>{evt.to_model}</span>
                  </div>
                  <div className="timeline-details">
                    <span>Session: {evt.session_id}</span>
                    <span>•</span>
                    <span>Prior Status: {evt.prior_request_status}</span>
                    <span>•</span>
                    <span>Prior Cost: {evt.prior_request_cost_usd !== 'unknown' ? `$${evt.prior_request_cost_usd}` : 'unknown'}</span>
                    <span>•</span>
                    <span>Prior Duration: {evt.prior_request_duration_ms !== 'unknown' ? `${evt.prior_request_duration_ms} ms` : 'unknown'}</span>
                    <span>•</span>
                    <span>Time: {evt.switch_timestamp}</span>
                  </div>
                </div>
              ))
            ) : (
              <div style={{ padding: '2rem', textAlign: 'center', color: '#64748b' }}>
                No model switching events observed within sessions in current filter window
              </div>
            )}
          </div>
          <div className="disclaimer-text">
            Model switching patterns are descriptive and do not assert intent or causation.
          </div>
        </div>
      )}

      {/* Tab 5: Reliability & Retries */}
      {activeTab === 'reliability' && (
        <div className="comparison-container">
          <div className="models-table-container">
            <table className="models-table">
              <thead>
                <tr>
                  <th>Model</th>
                  <th>Total Requests</th>
                  <th>Known Status</th>
                  <th>OK</th>
                  <th>Error</th>
                  <th>Interrupted</th>
                  <th>Rate Limited</th>
                  <th>Timeout</th>
                  <th>Abnormal</th>
                  <th>Error Rate</th>
                  <th>Retries</th>
                  <th>Retry Tokens</th>
                </tr>
              </thead>
              <tbody>
                {Object.values(profiles)
                  .filter((p) => p.requests_count > 0)
                  .map((p) => {
                    const r = p.reliability;
                    return (
                      <tr key={p.display_name}>
                        <td style={{ fontWeight: 600 }}>{p.display_name}</td>
                        <td>{r.total_requests}</td>
                        <td>{r.known_status_requests}</td>
                        <td style={{ color: '#34d399' }}>{r.ok_count}</td>
                        <td style={{ color: r.error_count > 0 ? '#f87171' : 'inherit' }}>{r.error_count}</td>
                        <td>{r.interrupted_count}</td>
                        <td style={{ color: r.rate_limited_count > 0 ? '#fbbf24' : 'inherit' }}>{r.rate_limited_count}</td>
                        <td>{r.timeout_count}</td>
                        <td>{r.abnormal_termination_count}</td>
                        <td>
                          {r.error_rate !== null ? (
                            <span className={`pill ${r.error_rate > 0.1 ? 'pill-danger' : r.error_rate > 0 ? 'pill-warning' : 'pill-success'}`}>
                              {(r.error_rate * 100).toFixed(1)}%
                            </span>
                          ) : (
                            '—'
                          )}
                        </td>
                        <td>{r.retry_requests_count}</td>
                        <td>{r.retry_tokens.toLocaleString()}</td>
                      </tr>
                    );
                  })}
              </tbody>
            </table>
          </div>
          {Object.values(profiles).some((p) => p.reliability.missing_status_caveat) && (
            <div className="disclaimer-text">
              Caveat: Requests with unrecorded or missing status are strictly excluded from the error rate denominator.
            </div>
          )}
        </div>
      )}
    </div>
  );
}
