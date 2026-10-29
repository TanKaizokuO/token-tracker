import React, { useState, useEffect, useMemo, useRef } from 'react';
import type { PageProps, QueryResponse } from '../App';
import * as echarts from 'echarts';
import './Measurement.css';
type CoverageStatus = 'exact' | 'partial' | 'unavailable';

function CoverageBadge({ status }: { status?: CoverageStatus }) {
  // Partial coverage renders no badge; only Unavailable is shown.
  if (status !== 'unavailable') return null;
  return <span className="measurement-badge">Unavailable</span>;
}

function formatCount(value: number | null | undefined): string {
  return value === null || value === undefined ? '—' : value.toLocaleString();
}

function sumMeasured(values: Array<number | null | undefined>): number | null {
  const observed = values.filter((value): value is number => value !== null && value !== undefined);
  return observed.length === 0 ? null : observed.reduce((sum, value) => sum + value, 0);
}



type GroupDimension = 'model' | 'project' | 'session' | 'day';

interface TokenMetricValues {
  total_tokens_status?: CoverageStatus;
  token_fields_status?: Record<string, CoverageStatus>;
  total_tokens?: number | null;
  has_unresolved_overlap?: boolean;
  fresh_input_tokens?: number | null;
  cache_read_tokens?: number | null;
  cache_write_5m_tokens?: number | null;
  cache_write_1h_tokens?: number | null;
  cache_write_unknown_ttl_tokens?: number | null;
  output_tokens?: number | null;
  reasoning_tokens?: number | null;
  reported_total_tokens?: number | null;
  cache_hit_rate?: number | null;
}

interface TokenBreakdownRow {
  group_key: string;
  total_tokens_status?: CoverageStatus;
  token_fields_status?: Record<string, CoverageStatus>;
  fresh_input_tokens?: number | null;
  cache_read_tokens?: number | null;
  cache_write_5m_tokens?: number | null;
  cache_write_1h_tokens?: number | null;
  cache_write_unknown_ttl_tokens?: number | null;
  output_tokens?: number | null;
  reasoning_tokens?: number | null;
  total_tokens?: number | null;
  reported_total_tokens?: number | null;
  cache_hit_rate?: number | null;
}

type TokenCounterField =
  | 'fresh_input_tokens'
  | 'cache_read_tokens'
  | 'cache_write_5m_tokens'
  | 'cache_write_1h_tokens'
  | 'cache_write_unknown_ttl_tokens'
  | 'output_tokens'
  | 'reasoning_tokens';

function aggregateFieldStatus(rows: TokenBreakdownRow[], field: TokenCounterField): CoverageStatus {
  if (rows.length === 0) return 'unavailable';
  let hasObserved = false;
  let hasMissing = false;
  for (const row of rows) {
    const status = row.token_fields_status?.[field]
      ?? (row[field] === null || row[field] === undefined ? 'unavailable' : 'exact');
    if (status !== 'unavailable') hasObserved = true;
    if (status !== 'exact') hasMissing = true;
  }
  return !hasObserved ? 'unavailable' : hasMissing ? 'partial' : 'exact';
}

interface TokensResponse extends Omit<QueryResponse, 'values' | 'rows' | 'missingness' | 'coverage'> {
  values?: TokenMetricValues;
  rows?: TokenBreakdownRow[];
  missingness?: {
    unpriced_requests_count?: number | null;
    unknown_ttl_write_requests_count?: number | null;
  };
  coverage?: {
    has_unresolved_overlap?: boolean;
  };
}
export default function TokensCache({ context }: PageProps) {
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);
  const [data, setData] = useState<TokensResponse | null>(null);
  const [groupDimension, setGroupDimension] = useState<GroupDimension>('model');

  // Rates are explicitly hypothetical: the token response has no effective rate-card fields.
  const [freshRate, setFreshRate] = useState<string>('');
  const [readRate, setReadRate] = useState<string>('');
  const [write5mRate, setWrite5mRate] = useState<string>('');
  const [write1hRate, setWrite1hRate] = useState<string>('');
  const [reuseCount, setReuseCount] = useState<number>(3);
  const [tokenCount, setTokenCount] = useState<number>(100000);

  // Chart DOM refs
  const breakdownChartRef = useRef<HTMLDivElement>(null);
  const breakdownChartInstance = useRef<echarts.ECharts | null>(null);

  const cacheTrendChartRef = useRef<HTMLDivElement>(null);
  const cacheTrendChartInstance = useRef<echarts.ECharts | null>(null);

  // Fetch data on query change or group dimension change
  useEffect(() => {
    let isCancelled = false;
    setLoading(true);
    setError(null);

    // Request tokens metric family with active group dimension
    context
      .request<TokensResponse>('tokens', {
        group_by: [groupDimension],
      })
      .then((res) => {
        if (!isCancelled) {
          setData(res);
          setLoading(false);
        }
      })
      .catch((err: unknown) => {
        if (!isCancelled) {
          const message = err instanceof Error ? err.message : 'Failed to fetch tokens and cache metrics';
          setError(message);
          setLoading(false);
        }
      });

    return () => {
      isCancelled = true;
    };
  }, [context, groupDimension]);

  // Aggregate counters from response
  const summaryValues = useMemo(() => {
    const vals = data?.values || {};
    const fresh = vals.fresh_input_tokens ?? null;
    const read = vals.cache_read_tokens ?? null;
    const write5m = vals.cache_write_5m_tokens ?? null;
    const write1h = vals.cache_write_1h_tokens ?? null;
    const writeUnk = vals.cache_write_unknown_ttl_tokens ?? null;
    const output = vals.output_tokens ?? null;
    const reasoning = vals.reasoning_tokens ?? null;
    const total = vals.total_tokens ?? null;
    const reportedTotal = vals.reported_total_tokens ?? null;
    const promptDenominator = sumMeasured([fresh, read]);
    const tokenRows = data?.rows ?? [];
    const freshStatus = aggregateFieldStatus(tokenRows, 'fresh_input_tokens');
    const readStatus = aggregateFieldStatus(tokenRows, 'cache_read_tokens');
    const write5mStatus = aggregateFieldStatus(tokenRows, 'cache_write_5m_tokens');
    const write1hStatus = aggregateFieldStatus(tokenRows, 'cache_write_1h_tokens');
    const writeUnkStatus = aggregateFieldStatus(tokenRows, 'cache_write_unknown_ttl_tokens');
    const outputStatus = aggregateFieldStatus(tokenRows, 'output_tokens');
    const reasoningStatus = aggregateFieldStatus(tokenRows, 'reasoning_tokens');
    const promptDenominatorStatus: CoverageStatus = promptDenominator === null
      ? 'unavailable'
      : freshStatus === 'exact' && readStatus === 'exact'
        ? 'exact'
        : 'partial';
    const cacheHitRate = vals.cache_hit_rate !== undefined && vals.cache_hit_rate !== null
      ? vals.cache_hit_rate * 100
      : null;

    return {
      fresh,
      freshStatus,
      read,
      readStatus,
      write5m,
      write5mStatus,
      write1h,
      write1hStatus,
      writeUnk,
      writeUnkStatus,
      output,
      outputStatus,
      reasoning,
      reasoningStatus,
      total,
      reportedTotal,
      promptDenominator,
      promptDenominatorStatus,
      cacheHitRate,
    };
  }, [data]);

  // Breakdown rows for table and chart
  const rows = useMemo(() => data?.rows ?? [], [data]);

  // Simulator rates are user-entered hypotheticals; no effective rates are returned by this metric.
  const simulatorState = useMemo(() => {
    const parsedRates = [freshRate, readRate, write5mRate, write1hRate].map(raw =>
      raw.trim() === '' ? null : Number(raw)
    );
    if (!parsedRates.every(rate => rate !== null && Number.isFinite(rate))) {
      return { result: null, error: null, missingRates: true };
    }

    const [fresh, read, write5m, write1h] = parsedRates as [number, number, number, number];
    if (fresh <= 0) {
      return { result: null, error: 'Fresh input rate must be greater than 0', missingRates: false };
    }
    if (read < 0 || read > fresh) {
      return { result: null, error: 'Read rate must be between 0 and fresh rate', missingRates: false };
    }
    if (write5m < fresh) {
      return { result: null, error: '5m write rate cannot be less than fresh rate', missingRates: false };
    }
    if (write1h < fresh) {
      return { result: null, error: '1h write rate cannot be less than fresh rate', missingRates: false };
    }
    if (reuseCount < 0 || tokenCount < 0) {
      return { result: null, error: 'Reuse count and token volume must be non-negative', missingRates: false };
    }

    const readDiscount = fresh - read;
    const write5mPremium = write5m - fresh;
    const write1hPremium = write1h - fresh;
    const beReuses5m = readDiscount > 0 ? write5mPremium / readDiscount : Infinity;
    const beReuses1h = readDiscount > 0 ? write1hPremium / readDiscount : Infinity;
    const unitsM = tokenCount / 1_000_000.0;
    const noCacheCost = unitsM * fresh * (1.0 + reuseCount);
    const cost5m = unitsM * (write5m + reuseCount * read);
    const cost1h = unitsM * (write1h + reuseCount * read);

    return {
      result: {
        beReuses5m: Number.isFinite(beReuses5m) ? beReuses5m : null,
        beReuses1h: Number.isFinite(beReuses1h) ? beReuses1h : null,
        noCacheCost,
        cost5m,
        savings5m: noCacheCost - cost5m,
        cost1h,
        savings1h: noCacheCost - cost1h,
      },
      error: null,
      missingRates: false,
    };
  }, [freshRate, readRate, write5mRate, write1hRate, reuseCount, tokenCount]);
  const simulatorResult = simulatorState.result;
  const simError = simulatorState.error;

  // Initialize and update Stacked Breakdown Chart
  useEffect(() => {
    if (!breakdownChartRef.current || !data) return;

    if (!breakdownChartInstance.current) {
      breakdownChartInstance.current = echarts.init(breakdownChartRef.current);
    }
    const chart = breakdownChartInstance.current;

    const categories = rows.map((r) => groupDimension === 'project' && r.group_key === 'unassigned_project' ? 'Unattributed' : r.group_key);

    const seriesFresh = rows.map((r) => r.fresh_input_tokens ?? null);
    const seriesRead = rows.map((r) => r.cache_read_tokens ?? null);
    const seriesW5m = rows.map((r) => r.cache_write_5m_tokens ?? null);
    const seriesW1h = rows.map((r) => r.cache_write_1h_tokens ?? null);
    const seriesWUnk = rows.map((r) => r.cache_write_unknown_ttl_tokens ?? null);
    const seriesOutput = rows.map((r) => r.output_tokens ?? null);

    const option: echarts.EChartsOption = {
      backgroundColor: 'transparent',
      tooltip: {
        trigger: 'axis',
        axisPointer: { type: 'shadow' },
        formatter: (params: any) => {
          let html = `<div style="font-family: monospace; font-size: 12px; padding: 4px;">`;
          html += `<strong>${params[0]?.axisValue}</strong><br/>`;
          let total = 0;
          let totalMissing = false;
          let totalObserved = 0;
          for (const item of params) {
            const value = item.value === null || item.value === undefined ? null : Number(item.value);
            const measured = value !== null && Number.isFinite(value) ? value : null;
            if (measured === null) {
              totalMissing = true;
            } else {
              total += measured;
              totalObserved += 1;
            }
            html += `<span style="display:inline-block;margin-right:5px;border-radius:10px;width:9px;height:9px;background-color:${item.color};"></span>`;
            html += `${item.seriesName}: <strong>${measured === null ? 'Unavailable' : measured.toLocaleString()}</strong><br/>`;
          }
          html += `<hr style="margin: 4px 0; border: none; border-top: 1px solid #444;" />`;
          const totalLabel = totalObserved === 0
            ? 'Unavailable'
            : totalMissing
              ? `${total.toLocaleString()} (Partial; counter missing)`
              : total.toLocaleString();
          html += `Total Disjoint Tokens: <strong>${totalLabel}</strong></div>`;
          return html;
        },
      },
      legend: {
        data: [
          'Fresh Input',
          'Cache Read',
          'Cache Write 5m',
          'Cache Write 1h',
          'Cache Write (Unknown TTL)',
          'Output',
        ],
        textStyle: { color: '#94a3b8' },
        top: 0,
      },
      grid: {
        left: '3%',
        right: '4%',
        bottom: '8%',
        top: '40px',
        containLabel: true,
      },
      xAxis: {
        type: 'category',
        data: categories,
        axisLine: { lineStyle: { color: '#334155' } },
        axisLabel: { color: '#94a3b8', interval: 0, rotate: categories.length > 8 ? 30 : 0 },
      },
      yAxis: {
        type: 'value',
        axisLine: { lineStyle: { color: '#334155' } },
        splitLine: { lineStyle: { color: '#1f293d' } },
        axisLabel: {
          color: '#94a3b8',
          formatter: (v: number) => {
            if (v >= 1e6) return `${(v / 1e6).toFixed(1)}M`;
            if (v >= 1e3) return `${(v / 1e3).toFixed(0)}k`;
            return String(v);
          },
        },
      },
      series: [
        {
          name: 'Fresh Input',
          type: 'bar',
          stack: 'disjoint',
          itemStyle: { color: '#38bdf8' },
          data: seriesFresh,
        },
        {
          name: 'Cache Read',
          type: 'bar',
          stack: 'disjoint',
          itemStyle: { color: '#10b981' },
          data: seriesRead,
        },
        {
          name: 'Cache Write 5m',
          type: 'bar',
          stack: 'disjoint',
          itemStyle: { color: '#f59e0b' },
          data: seriesW5m,
        },
        {
          name: 'Cache Write 1h',
          type: 'bar',
          stack: 'disjoint',
          itemStyle: { color: '#d97706' },
          data: seriesW1h,
        },
        {
          name: 'Cache Write (Unknown TTL)',
          type: 'bar',
          stack: 'disjoint',
          itemStyle: { color: '#9ca3af' },
          data: seriesWUnk,
        },
        {
          name: 'Output',
          type: 'bar',
          stack: 'disjoint',
          itemStyle: { color: '#a855f7' },
          data: seriesOutput,
        },
      ],
    };

    chart.setOption(option);

    // Click drill-down handler
    chart.off('click');
    chart.on('click', (params: any) => {
      const selectedName = params.name;
      if (selectedName && selectedName !== 'unknown') {
        const filterValue = groupDimension === 'project' && selectedName === 'Unattributed'
          ? 'unattributed'
          : selectedName;
        context.setQuery((prev) => ({
          ...prev,
          filters: {
            ...prev.filters,
            [groupDimension]: [filterValue],
          },
        }));
      }
    });

    const handleResize = () => chart.resize();
    window.addEventListener('resize', handleResize);
    return () => {
      window.removeEventListener('resize', handleResize);
    };
  }, [rows, groupDimension, data, context]);

  // Initialize Cache Hit Rate & Efficiency Chart
  useEffect(() => {
    if (!cacheTrendChartRef.current || !data) return;

    if (!cacheTrendChartInstance.current) {
      cacheTrendChartInstance.current = echarts.init(cacheTrendChartRef.current);
    }
    const chart = cacheTrendChartInstance.current;

    const categories = rows.map((r) => r.group_key);
    const hitRates = rows.map((r) =>
      r.cache_hit_rate === null || r.cache_hit_rate === undefined
        ? null
        : (r.cache_hit_rate * 100).toFixed(2)
    );

    const option: echarts.EChartsOption = {
      backgroundColor: 'transparent',
      tooltip: {
        trigger: 'axis',
        formatter: (params: any) => {
          const item = params[0];
          return `<div style="font-family: monospace; font-size: 12px;">
            ${item.axisValue}<br/>
            Hit Rate: <strong>${item.value === null || item.value === undefined ? 'Unavailable' : `${item.value}%`}</strong><br/>
            <span style="color:#64748b;font-size:11px;">Denominator: fresh_input + cache_read</span>
          </div>`;
        },
      },
      grid: {
        left: '3%',
        right: '4%',
        bottom: '8%',
        top: '20px',
        containLabel: true,
      },
      xAxis: {
        type: 'category',
        data: categories,
        axisLine: { lineStyle: { color: '#334155' } },
        axisLabel: { color: '#94a3b8' },
      },
      yAxis: {
        type: 'value',
        max: 100,
        min: 0,
        axisLine: { lineStyle: { color: '#334155' } },
        splitLine: { lineStyle: { color: '#1f293d' } },
        axisLabel: {
          color: '#94a3b8',
          formatter: '{value}%',
        },
      },
      series: [
        {
          name: 'Cache Hit Rate',
          type: 'line',
          smooth: true,
          showSymbol: true,
          symbolSize: 6,
          itemStyle: { color: '#10b981' },
          lineStyle: { width: 3 },
          areaStyle: {
            color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
              { offset: 0, color: 'rgba(16, 185, 129, 0.35)' },
              { offset: 1, color: 'rgba(16, 185, 129, 0.0)' },
            ]),
          },
          data: hitRates,
        },
      ],
    };

    chart.setOption(option);

    const handleResize = () => chart.resize();
    window.addEventListener('resize', handleResize);
    return () => {
      window.removeEventListener('resize', handleResize);
    };
  }, [rows, groupDimension, data]);

  // Clean up chart instances on unmount
  useEffect(() => {
    return () => {
      breakdownChartInstance.current?.dispose();
      breakdownChartInstance.current = null;
      cacheTrendChartInstance.current?.dispose();
      cacheTrendChartInstance.current = null;
    };
  }, []);

  return (
    <div className="measurement-page">
      {/* Page Header */}
      <div className="measurement-header">
        <div className="measurement-title-group">
          <h1>
            Tokens & Cache Breakdown
            <span className="measurement-badge">WP12 Semantic Analytics</span>
            {data?.values?.total_tokens_status === 'partial' && (
              <span className="measurement-badge warning">Partial Coverage</span>
            )}
            {data?.values?.total_tokens_status === 'unavailable' && (
              <span className="measurement-badge">Totals Unavailable</span>
            )}
            {data?.values?.has_unresolved_overlap && (
              <span className="measurement-badge warning" title="Overlapping records are unresolved and are not reconciled">
                Unresolved Overlap
              </span>
            )}
          </h1>
          <p>
            Disjoint billable-token counters, non-additive reasoning tracking, cache hit rates,
            and parameter-driven break-even economics.
          </p>
        </div>

        {/* Dimension Pill Selector */}
        <div className="control-pills">
          <button
            className={`pill-btn ${groupDimension === 'model' ? 'active' : ''}`}
            onClick={() => setGroupDimension('model')}
          >
            By Model
          </button>
          <button
            className={`pill-btn ${groupDimension === 'project' ? 'active' : ''}`}
            onClick={() => setGroupDimension('project')}
          >
            By Project
          </button>
          <button
            className={`pill-btn ${groupDimension === 'session' ? 'active' : ''}`}
            onClick={() => setGroupDimension('session')}
          >
            By Session
          </button>
          <button
            className={`pill-btn ${groupDimension === 'day' ? 'active' : ''}`}
            onClick={() => setGroupDimension('day')}
          >
            By Day
          </button>
        </div>
      </div>

      {/* Coverage & Provenance Banner */}
      <div className="coverage-banner">
        <div className="coverage-details">
          <div className="coverage-item">
            <span className="coverage-label">Harness Scope:</span>
            <span className="coverage-val">{context.query.harness_scope}</span>
          </div>
          <div className="coverage-item">
            <span className="coverage-label">Prompt Denominator:</span>
            <span className="coverage-val">
              {formatCount(summaryValues.promptDenominator)} tokens <CoverageBadge status={summaryValues.promptDenominatorStatus} />
            </span>
          </div>
          <div className="coverage-item">
            <span className="coverage-label">Unpriced Requests:</span>
            <span className="coverage-val">
              {formatCount(data?.missingness?.unpriced_requests_count)}
            </span>
          </div>
          <div className="coverage-item">
            <span className="coverage-label">Unknown TTL Write Requests:</span>
            <span className="coverage-val">
              {formatCount(data?.missingness?.unknown_ttl_write_requests_count)}
            </span>
          </div>
          <div className="coverage-item">
            <span className="coverage-label">Unknown TTL Write Tokens:</span>
            <span className="coverage-val">{formatCount(summaryValues.writeUnk)} <CoverageBadge status={summaryValues.writeUnkStatus} /></span>
          </div>
          <div className="coverage-item">
            <span className="coverage-label">Observed Effective Rates:</span>
            <span className="coverage-val">Unavailable</span>
          </div>
        </div>
        <div className="coverage-details">
          <span className="measurement-badge success">Formula: fresh + cache_read + writes + output</span>
        </div>
      </div>

      {/* KPI Cards Grid */}
      <div className="kpi-grid">
        <div className="kpi-card">
          <div className="kpi-label-row">
            <span className="kpi-label">Disjoint Billable Tokens</span>
            <span className="measurement-badge">Total</span>
          </div>
          <div className="kpi-value-row">
            <span className="kpi-value">{formatCount(summaryValues.total)}</span> <CoverageBadge status={data?.values?.total_tokens_status} />
            <span className="kpi-unit">tokens</span>
          </div>
          <span className="kpi-subtext">Sum of 6 mutually exclusive billable counters</span>
        </div>

        <div className="kpi-card">
          <div className="kpi-label-row">
            <span className="kpi-label">Cache Hit Rate</span>
            <span className="measurement-badge success">Observed</span>
          </div>
          <div className="kpi-value-row">
            <span className="kpi-value">
              {summaryValues.cacheHitRate !== null ? `${summaryValues.cacheHitRate.toFixed(1)}%` : '—'}
            </span> <CoverageBadge status={summaryValues.promptDenominatorStatus} />
            <span className="kpi-unit">ratio</span>
          </div>
          <span className="kpi-subtext">
            {formatCount(summaryValues.read)} read / {formatCount(summaryValues.promptDenominator)} eligible prompt
          </span>
        </div>

        <div className="kpi-card">
          <div className="kpi-label-row">
            <span className="kpi-label">Fresh Input Tokens</span>
            <span className="measurement-badge">Observed Tokens</span>
          </div>
          <div className="kpi-value-row">
            <span className="kpi-value">{formatCount(summaryValues.fresh)}</span> <CoverageBadge status={summaryValues.freshStatus} />
          </div>
          <span className="kpi-subtext">
            {summaryValues.promptDenominator === null || summaryValues.fresh === null
              ? 'Prompt denominator unavailable'
              : summaryValues.promptDenominator > 0
                ? `${((summaryValues.fresh / summaryValues.promptDenominator) * 100).toFixed(1)}% of prompt tokens`
                : 'No prompt tokens'}
          </span>
        </div>

        <div className="kpi-card">
          <div className="kpi-label-row">
            <span className="kpi-label">Reasoning Tokens (Subset)</span>
            <span className="measurement-badge purple">Reported</span>
          </div>
          <div className="kpi-value-row">
            <span className="kpi-value">{formatCount(summaryValues.reasoning)}</span> <CoverageBadge status={summaryValues.reasoningStatus} />
          </div>
          <span className="kpi-subtext">
            {summaryValues.reasoning === null || summaryValues.output === null
              ? 'Reasoning subset or output count unavailable'
              : summaryValues.output > 0
                ? `${((summaryValues.reasoning / summaryValues.output) * 100).toFixed(1)}% of ${formatCount(summaryValues.output)} output tokens`
                : 'No output tokens'}
          </span>
        </div>
      </div>

      {/* Main Charts Section */}
      <div className="section-card">
        <div className="section-header">
          <div>
            <h2 className="section-title">Disjoint Stacked Token Breakdown</h2>
            <p className="section-subtitle">
              Mutual exclusivity guaranteed: Fresh + Cache Read + 5m Write + 1h Write + Unknown TTL + Output.
              Click any bar to filter by {groupDimension}.
            </p>
          </div>
        </div>
        <div ref={breakdownChartRef} className="chart-container-large" />
      </div>

      {/* Cache Hit Rate Trend Chart */}
      <div className="section-card">
        <div className="section-header">
          <div>
            <h2 className="section-title">Cache Efficiency by {groupDimension.toUpperCase()}</h2>
            <p className="section-subtitle">
              Strict denominator: fresh_input_tokens + cache_read_tokens. Untimed and unpriced requests do not distort hit rate.
            </p>
          </div>
        </div>
        <div ref={cacheTrendChartRef} className="chart-container" />
      </div>

      {/* Detailed Breakdown Table */}
      <div className="section-card">
        <div className="section-header">
          <div>
            <h2 className="section-title">Granular Token Breakdown ({groupDimension})</h2>
            <p className="section-subtitle">
              Disjoint counters, reasoning subset, and reported totals per dimension slice.
            </p>
          </div>
        </div>

        <div className="data-table-container">
          <table className="data-table">
            <thead>
              <tr>
                <th>{groupDimension.toUpperCase()}</th>
                <th className="right-align">Total Billable</th>
                <th className="right-align">Fresh Input</th>
                <th className="right-align">Cache Read</th>
                <th className="right-align">5m Write</th>
                <th className="right-align">1h Write</th>
                <th className="right-align">Unknown TTL (unpriced)</th>
                <th className="right-align">Output</th>
                <th className="right-align">Reasoning (Sub)</th>
                <th className="right-align">Hit Rate</th>
              </tr>
            </thead>
            <tbody>
              {rows.length === 0 ? (
                <tr>
                  <td colSpan={10} style={{ textAlign: 'center', padding: '24px', color: 'var(--text-muted)' }}>
                    No token activity records found for this query filter.
                  </td>
                </tr>
              ) : (
                rows.map((row, idx) => {
                  const key = row.group_key || `row-${idx}`;
                  const groupFilterValue = groupDimension === 'project' && key === 'unassigned_project'
                    ? 'unattributed'
                    : key;
                  const fresh = row.fresh_input_tokens ?? null;
                  const read = row.cache_read_tokens ?? null;
                  const w5m = row.cache_write_5m_tokens ?? null;
                  const w1h = row.cache_write_1h_tokens ?? null;
                  const wUnk = row.cache_write_unknown_ttl_tokens ?? null;
                  const out = row.output_tokens ?? null;
                  const reasoning = row.reasoning_tokens ?? null;
                  const total = row.total_tokens;
                  const rate = row.cache_hit_rate;

                  return (
                    <tr key={key}>
                      <td className="mono-cell">
                        <button
                          style={{
                            background: 'none',
                            border: 'none',
                            color: 'var(--accent-cyan)',
                            cursor: 'pointer',
                            textAlign: 'left',
                            padding: 0,
                          }}
                          onClick={() => {
                            context.setQuery((prev) => ({
                              ...prev,
                              filters: {
                                ...prev.filters,
                                [groupDimension]: [groupFilterValue],
                              },
                            }));
                          }}
                        >
                          {key === 'unassigned_project' && groupDimension === 'project' ? 'Unattributed' : key}
                        </button>
                      </td>
                      <td className="mono-cell right-align" style={{ fontWeight: 600 }}>
                        {formatCount(total)} <CoverageBadge status={row.total_tokens_status} />
                      </td>
                      <td className="mono-cell right-align">{formatCount(fresh)} <CoverageBadge status={row.token_fields_status?.fresh_input_tokens} /></td>
                      <td className="mono-cell right-align" style={{ color: 'var(--accent-emerald)' }}>
                        {formatCount(read)} <CoverageBadge status={row.token_fields_status?.cache_read_tokens} />
                      </td>
                      <td className="mono-cell right-align">{formatCount(w5m)} <CoverageBadge status={row.token_fields_status?.cache_write_5m_tokens} /></td>
                      <td className="mono-cell right-align">{formatCount(w1h)} <CoverageBadge status={row.token_fields_status?.cache_write_1h_tokens} /></td>
                      <td className="mono-cell right-align" style={{ color: 'var(--text-muted)' }}>
                        {formatCount(wUnk)} <CoverageBadge status={row.token_fields_status?.cache_write_unknown_ttl_tokens} />
                      </td>
                      <td className="mono-cell right-align" style={{ color: 'var(--accent-purple)' }}>
                        {formatCount(out)} <CoverageBadge status={row.token_fields_status?.output_tokens} />
                      </td>
                      <td className="mono-cell right-align" style={{ color: 'var(--text-secondary)' }}>
                        {formatCount(reasoning)} <CoverageBadge status={row.token_fields_status?.reasoning_tokens} />
                      </td>
                      <td className="mono-cell right-align" style={{ fontWeight: 600 }}>
                        {rate !== null && rate !== undefined ? `${(rate * 100).toFixed(1)}%` : '—'}
                      </td>
                    </tr>
                  );
                })
              )}
            </tbody>
          </table>
        </div>
      </div>

      {/* Break-Even Simulator Section */}
      <div className="section-card">
        <div className="section-header">
          <div>
            <h2 className="section-title">
              Cache-Write Economics & Break-Even Simulator
              <span className="measurement-badge">Hypothetical Model</span>
            </h2>
            <p className="section-subtitle">
              No effective pricing rates are returned with this token response. Enter all four hypothetical rates to calculate;
              no observed spend or pricing attribution is inferred. Unknown-TTL writes and unknown rates remain unpriced.
              Reuse count (3) and block size (100,000 tokens) are editable simulation assumptions, not measured activity.
            </p>
          </div>
        </div>

        {/* Input Parameters Form */}
        <div className="simulator-grid">
          <div className="form-field">
            <label className="form-label">Hypothetical Fresh Input Rate ($ / 1M)</label>
            <input
              type="number"
              step="0.1"
              className="form-input"
              value={freshRate}
              onChange={(e) => setFreshRate(e.target.value)}
            />
          </div>

          <div className="form-field">
            <label className="form-label">Hypothetical Cache Read Rate ($ / 1M)</label>
            <input
              type="number"
              step="0.05"
              className="form-input"
              value={readRate}
              onChange={(e) => setReadRate(e.target.value)}
            />
          </div>

          <div className="form-field">
            <label className="form-label">Hypothetical 5m TTL Write Rate ($ / 1M)</label>
            <input
              type="number"
              step="0.1"
              className="form-input"
              value={write5mRate}
              onChange={(e) => setWrite5mRate(e.target.value)}
            />
          </div>

          <div className="form-field">
            <label className="form-label">Hypothetical 1h TTL Write Rate ($ / 1M)</label>
            <input
              type="number"
              step="0.1"
              className="form-input"
              value={write1hRate}
              onChange={(e) => setWrite1hRate(e.target.value)}
            />
          </div>

          <div className="form-field">
            <label className="form-label">Hypothetical Reuse Count</label>
            <input
              type="number"
              step="0.5"
              className="form-input"
              value={reuseCount}
              onChange={(e) => setReuseCount(parseFloat(e.target.value) || 0)}
            />
          </div>

          <div className="form-field">
            <label className="form-label">Hypothetical Prompt Block Size (Tokens)</label>
            <input
              type="number"
              step="10000"
              className="form-input"
              value={tokenCount}
              onChange={(e) => setTokenCount(parseInt(e.target.value, 10) || 0)}
            />
          </div>
        </div>
        {simulatorState.missingRates && (
          <div className="disclaimer-box">
            Enter four explicit hypothetical rates above; results are unavailable until all rates are provided.
          </div>
        )}

        {simError && (
          <div className="disclaimer-box" style={{ borderColor: 'var(--accent-rose)', color: 'var(--accent-rose)' }}>
            Validation Error: {simError}
          </div>
        )}

        {/* Simulator Projections Result */}
        {simulatorResult && (
          <div className="simulator-result-box">
            <div className="sim-metric-card">
              <div className="sim-metric-label">5m TTL Break-Even Threshold</div>
              <div className="sim-metric-val" style={{ color: 'var(--accent-cyan)' }}>
                {simulatorResult.beReuses5m !== null ? `${simulatorResult.beReuses5m.toFixed(2)} reuses` : 'N/A'}
              </div>
              <span className="kpi-subtext">Required prompt reads within 5-minute window</span>
            </div>

            <div className="sim-metric-card">
              <div className="sim-metric-label">1h TTL Break-Even Threshold</div>
              <div className="sim-metric-val" style={{ color: 'var(--accent-amber)' }}>
                {simulatorResult.beReuses1h !== null ? `${simulatorResult.beReuses1h.toFixed(2)} reuses` : 'N/A'}
              </div>
              <span className="kpi-subtext">Required prompt reads within 1-hour window</span>
            </div>

            <div className="sim-metric-card">
              <div className="sim-metric-label">Hypothetical 5m Net Benefit</div>
              <div
                className="sim-metric-val"
                style={{
                  color: simulatorResult.savings5m >= 0 ? 'var(--accent-emerald)' : 'var(--accent-rose)',
                }}
              >
                ${simulatorResult.savings5m.toFixed(4)}
              </div>
              <span className="kpi-subtext">
                vs ${simulatorResult.noCacheCost.toFixed(4)} baseline no-cache cost
              </span>
            </div>

            <div className="sim-metric-card">
              <div className="sim-metric-label">Hypothetical 1h Net Benefit</div>
              <div
                className="sim-metric-val"
                style={{
                  color: simulatorResult.savings1h >= 0 ? 'var(--accent-emerald)' : 'var(--accent-rose)',
                }}
              >
                ${simulatorResult.savings1h.toFixed(4)}
              </div>
              <span className="kpi-subtext">
                vs ${simulatorResult.noCacheCost.toFixed(4)} baseline no-cache cost
              </span>
            </div>
          </div>
        )}

        {/* Stated Assumptions & Policy Disclaimer */}
        <div className="disclaimer-box">
          <strong>Policy Simulator Disclaimer:</strong> Stated projections are mathematical models calculated
          from the declared rate cards and assumed reuse frequency. This simulator does not assert or prove historical
          cache eviction causality or guarantees of future provider cache hits. Tokens with unknown TTL are
          strictly excluded from priced calculations without being conflated with ₹0.00.
        </div>
      </div>
    </div>
  );
}
