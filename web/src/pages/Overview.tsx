import React, { useState, useEffect, useRef, useCallback } from 'react';
import type { MetricRow, PageProps } from '../App';
import { DashboardGrid, LayoutControls, type WidgetLayoutItem } from '../components/LayoutControls';
import * as echarts from 'echarts';
import './Explorers.css';

const DEFAULT_OVERVIEW_WIDGETS: WidgetLayoutItem[] = [
  {
    id: 'metrics',
    title: 'Headline Metrics',
    description: 'Usage KPIs and equivalent-period comparisons',
    colSpan: 12,
    minColSpan: 6,
    order: 0,
    visible: true,
  },
  {
    id: 'timeline',
    title: 'Token Timeline',
    description: 'Interactive token consumption chart',
    colSpan: 12,
    minColSpan: 6,
    order: 1,
    visible: true,
  },
  {
    id: 'breakdown',
    title: 'Model & Harness Breakdown',
    description: 'Model ranking and harness comparison',
    colSpan: 12,
    minColSpan: 6,
    order: 2,
    visible: true,
  },
  {
    id: 'activity',
    title: 'Activity Calendar',
    description: 'Daily activity heatmap and date evidence',
    colSpan: 12,
    minColSpan: 6,
    order: 3,
    visible: true,
  },
];
function numericValue(value: unknown): number | null {
  if (value === null || value === undefined || value === '') return null;
  const numeric = typeof value === 'number' ? value : Number(value);
  return Number.isFinite(numeric) ? numeric : null;
}

function formatCount(value: unknown): string {
  const numeric = numericValue(value);
  return numeric === null ? '—' : numeric.toLocaleString();
}

function formatUsd(value: unknown, decimals = 3, missing = 'Unavailable'): string {
  const numeric = numericValue(value);
  return numeric === null ? missing : `$${numeric.toFixed(decimals)}`;
}

function formatRate(value: unknown): string {
  const numeric = numericValue(value);
  return numeric === null ? '—' : `${(numeric * 100).toFixed(1)}%`;
}

function sumMeasured(...values: unknown[]): number | null {
  const numericValues = values.map(numericValue);
  return numericValues.some(value => value === null)
    ? null
    : numericValues.reduce<number>((sum, value) => (sum ?? 0) + (value ?? 0), 0);
}

function heatmapValue(point: Record<string, unknown>, metric: 'tokens' | 'requests' | 'sessions' | 'cost' | 'activity'): number | null {
  const field = metric === 'tokens' ? 'total_tokens'
    : metric === 'requests' ? 'requests_count'
      : metric === 'sessions' ? 'sessions_count'
        : metric === 'activity' ? 'active_minutes' : 'api_equivalent_usd';
  return numericValue(point[field]);
}

function nextDate(date: string): string {
  const day = new Date(`${date}T00:00:00Z`);
  day.setUTCDate(day.getUTCDate() + 1);
  return day.toISOString().slice(0, 10);
}

function localDate(value: string, timezone: string, offsetMs = 0): string {
  const hasOffset = /[zZ]|[+-]\d\d:\d\d$/.test(value);
  const date = new Date(new Date(hasOffset ? value : `${value.slice(0, 19)}Z`).getTime() + offsetMs);
  if (!hasOffset) return date.toISOString().slice(0, 10);
  return new Intl.DateTimeFormat('en-CA', { timeZone: timezone, year: 'numeric', month: '2-digit', day: '2-digit' }).format(date);
}

export default function Overview({ context }: PageProps) {
  const { query, setQuery, request } = context;

  // State
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);
  const [summaryData, setSummaryData] = useState<any>(null);
  const [priorSummaryData, setPriorSummaryData] = useState<any>(null);
  const [timelineData, setTimelineData] = useState<any[]>([]);
  const [dailyData, setDailyData] = useState<MetricRow[]>([]);
  const [hourlyData, setHourlyData] = useState<MetricRow[]>([]);
  const [modelTimeline, setModelTimeline] = useState<MetricRow[]>([]);
  const [modelsData, setModelsData] = useState<any[]>([]);
  const [harnessData, setHarnessData] = useState<{ claude?: any; omp?: any }>({});

  // Timeline controls
  const [bucketGrain, setBucketGrain] = useState<'hour' | 'day' | 'week' | 'month'>('day');
  const [activeSeries, setActiveSeries] = useState<string>('all'); // 'all', 'fresh', 'cache_read', 'output', 'cache_write'

  // Model ranking metric
  const [modelRankMetric, setModelRankMetric] = useState<'total_tokens' | 'requests_count' | 'output_tokens' | 'sessions_count' | 'api_equivalent_usd'>('total_tokens');

  // Heatmap controls
  const [heatmapMetric, setHeatmapMetric] = useState<'tokens' | 'requests' | 'sessions' | 'cost' | 'activity'>('tokens');
  const [selectedDayEvidence, setSelectedDayEvidence] = useState<{
    date: string;
    loading: boolean;
    sessions: any[];
    projects: any[];
    models: any[];
    stats: any;
  } | null>(null);

  const timelineChartRef = useRef<HTMLDivElement>(null);
  const chartInstance = useRef<echarts.ECharts | null>(null);

  const [widgets, setWidgets] = useState<WidgetLayoutItem[]>(DEFAULT_OVERVIEW_WIDGETS);
  const handleWidgetsChange = useCallback((nextWidgets: WidgetLayoutItem[]) => {
    setWidgets(nextWidgets);
  }, []);
  // Helper to compute equivalent prior period
  const computePriorTimeRange = (currentRange: any) => {
    if (!currentRange.start || !currentRange.end) {
      return undefined;
    }
    try {
      const start = new Date(currentRange.start).getTime();
      const end = new Date(currentRange.end).getTime();
      const duration = end - start;
      if (duration <= 0) return undefined;
      const priorStart = new Date(start - duration).toISOString();
      const priorEnd = new Date(start).toISOString();
      return {
        start: priorStart,
        end: priorEnd,
        timezone: currentRange.timezone || 'UTC'
      };
    } catch {
      return undefined;
    }
  };

  // Main data fetch
  useEffect(() => {
    let isCancelled = false;
    setLoading(true);
    setError(null);

    const loadAll = async () => {
      try {
        // 1. Current summary
        const sumRes = await request('summary');
        if (isCancelled) return;
        setSummaryData(sumRes);

        // 2. Prior equivalent period summary
        const priorRange = computePriorTimeRange(query.time_range);
        if (priorRange) {
          try {
            const priorRes = await request('summary', { time_range: priorRange });
            if (!isCancelled) setPriorSummaryData(priorRes);
          } catch {
            if (!isCancelled) setPriorSummaryData(null);
          }
        } else {
          setPriorSummaryData(null);
        }

        // 3. Timeline
        const timeRes = await request('timeline', { group_by: [bucketGrain] });
        if (isCancelled) return;
        setTimelineData(timeRes.rows || []);
        const [dailyRes, hourlyRes, modelTimeRes] = await Promise.all([
          request('timeline', { group_by: ['day'] }),
          request('timeline', { group_by: ['hour'] }),
          request('timeline', { group_by: [bucketGrain, 'model'] }),
        ]);
        if (isCancelled) return;
        setDailyData(dailyRes.rows || []);
        setHourlyData(hourlyRes.rows || []);
        setModelTimeline(modelTimeRes.rows || []);

        // 4. Models
        const modRes = await request('models');
        if (isCancelled) return;
        setModelsData(modRes.rows || []);

        // 5. Harness comparison (Claude Code vs OMP) in combined scope
        if (query.harness_scope === 'combined') {
          const [claudeRes, ompRes] = await Promise.all([
            request('summary', { harness_scope: 'claude-code' }).catch(() => null),
            request('summary', { harness_scope: 'omp' }).catch(() => null)
          ]);
          if (!isCancelled) {
            setHarnessData({
              claude: claudeRes?.values,
              omp: ompRes?.values
            });
          }
        }
      } catch (err: any) {
        if (!isCancelled) setError(err.message || 'Failed to load overview data');
      } finally {
        if (!isCancelled) setLoading(false);
      }
    };

    loadAll();

    return () => {
      isCancelled = true;
    };
  }, [query.harness_scope, query.time_range, query.filters, bucketGrain]);

  // Render ECharts timeline
  useEffect(() => {
    if (!timelineChartRef.current || timelineData.length === 0) return;

    if (!chartInstance.current) {
      chartInstance.current = echarts.init(timelineChartRef.current);
    }

    const chart = chartInstance.current;
    const dates = timelineData.map((d: any) => d.bucket);

    const seriesConfig: any[] = [];

    const names = Array.from(new Set(modelTimeline.map(row => row.model))).filter(Boolean);
    const field = activeSeries === 'fresh' ? 'fresh_input_tokens'
      : activeSeries === 'cache_read' ? 'cache_read_tokens'
      : activeSeries === 'output' ? 'output_tokens' : 'total_tokens';
    for (const model of names) {
      seriesConfig.push({
        name: model,
        type: 'line',
        connectNulls: false,
        data: dates.map(date => {
          const row = modelTimeline.find(point => point.bucket === date && point.model === model);
          if (!row) return null;
          return activeSeries === 'cache_write'
            ? sumMeasured(row.cache_write_5m_tokens, row.cache_write_1h_tokens, row.cache_write_unknown_ttl_tokens)
            : numericValue(row[field]);
        }),
      });
    }

    const option: echarts.EChartsOption = {
      backgroundColor: 'transparent',
      tooltip: {
        trigger: 'axis',
        axisPointer: { type: 'shadow' },
        backgroundColor: '#1f293d',
        borderColor: '#334155',
        textStyle: { color: '#f8fafc', fontSize: 12 },
        formatter: (params: any) => {
          if (!Array.isArray(params) || params.length === 0) return '';
          const idx = params[0].dataIndex;
          const row = timelineData[idx];
          if (!row) return '';
          const totalTokens = formatCount(row.total_tokens);
          const reqCount = formatCount(row.requests_count);
          const topModel = row.top_model || 'Unknown';
          const unpricedCount = numericValue(row.unpriced_requests_count);
          const costStr = formatUsd(row.api_equivalent_usd, 4, 'Unpriced');
          const unpricedBadge = unpricedCount !== null && unpricedCount > 0 ? ` (${formatCount(unpricedCount)} unpriced reqs)` : '';

          let lines = `<strong>${row.bucket}</strong><br/>`;
          lines += `Total Tokens: <strong>${totalTokens}</strong><br/>`;
          lines += `Requests: <strong>${reqCount}</strong><br/>`;
          lines += `Dominant Model: <strong>${topModel}</strong><br/>`;
          lines += `Estimated Cost: <strong>${costStr}</strong>${unpricedBadge}<br/><hr style="border-color:#334155;margin:4px 0;"/>`;
          params.forEach((p: any) => {
            lines += `${p.marker} ${p.seriesName}: ${formatCount(p.value)}<br/>`;
          });
          return lines;
        }
      },
      legend: {
        textStyle: { color: '#94a3b8', fontSize: 11 },
        top: 0,
        right: 10
      },
      grid: {
        left: '2%',
        right: '2%',
        bottom: '8%',
        top: '12%',
        containLabel: true
      },
      xAxis: {
        type: 'category',
        data: dates,
        axisLine: { lineStyle: { color: '#334155' } },
        axisLabel: { color: '#94a3b8', fontSize: 10, rotate: dates.length > 20 ? 30 : 0 }
      },
      yAxis: {
        type: 'value',
        axisLine: { lineStyle: { color: '#334155' } },
        splitLine: { lineStyle: { color: 'rgba(51, 65, 85, 0.4)' } },
        axisLabel: {
          color: '#94a3b8',
          fontSize: 10,
          formatter: (v: number) => {
            if (v >= 1000000) return (v / 1000000).toFixed(1) + 'M';
            if (v >= 1000) return (v / 1000).toFixed(0) + 'k';
            return String(v);
          }
        }
      },
      series: seriesConfig
    };

    chart.setOption(option, true);
    const onPointClick = (params: { dataIndex: number; seriesName: string }) => {
      const point = modelTimeline.find(row => row.bucket === dates[params.dataIndex] && row.model === params.seriesName);
      if (!point) return;
      const { bucket_start: start, bucket_end: end, model } = point;
      if (typeof start === 'string' && typeof end === 'string' && model) {
        setQuery(prev => ({ ...prev, time_range: { ...prev.time_range, start, end, preset: 'custom' }, filters: { ...prev.filters, model: [model] } }));
      }
    };
    chart.on('click', onPointClick as any);

    const handleResize = () => chart.resize();
    window.addEventListener('resize', handleResize);

    return () => {
      chart.off('click', onPointClick);
      window.removeEventListener('resize', handleResize);
    };
  }, [timelineData, modelTimeline, activeSeries, setQuery]);

  // Clean chart on unmount
  useEffect(() => {
    return () => {
      if (chartInstance.current) {
        chartInstance.current.dispose();
        chartInstance.current = null;
      }
    };
  }, []);

  // Compute percentage changes
  const calcDelta = (currVal?: number, priorVal?: number) => {
    if (currVal === undefined || priorVal === undefined || priorVal === 0 || priorVal === null) {
      return null;
    }
    const pct = ((currVal - priorVal) / priorVal) * 100;
    return pct;
  };

  const values = summaryData?.values || {};
  const priorValues = priorSummaryData?.values || {};

  const dayBounds = (date: string) => {
    const timezone = query.time_range.timezone || 'UTC';
    const { start, end } = query.time_range;
    return {
      start: start && localDate(start, timezone) === date ? start : `${date}T00:00:00`,
      end: end && localDate(end, timezone, -1) === date ? end : `${nextDate(date)}T00:00:00`,
      timezone,
    };
  };

  // Handle Day selection in Heatmap/Calendar
  const handleSelectDay = async (dateStr: string) => {
    // 1. Update day evidence drawer
    setSelectedDayEvidence({
      date: dateStr,
      loading: true,
      sessions: [],
      projects: [],
      models: [],
      stats: null
    });

    try {
      const { start: dayStart, end: dayEnd, timezone: tz } = dayBounds(dateStr);

      const [sessRes, projRes, modRes, sumRes] = await Promise.all([
        request('sessions', { time_range: { start: dayStart, end: dayEnd, timezone: tz } }).catch(() => ({ rows: [] })),
        request('projects', { time_range: { start: dayStart, end: dayEnd, timezone: tz } }).catch(() => ({ rows: [] })),
        request('models', { time_range: { start: dayStart, end: dayEnd, timezone: tz } }).catch(() => ({ rows: [] })),
        request('summary', { time_range: { start: dayStart, end: dayEnd, timezone: tz } }).catch(() => ({ values: {} }))
      ]);

      setSelectedDayEvidence({
        date: dateStr,
        loading: false,
        sessions: sessRes.rows || [],
        projects: projRes.rows || [],
        models: modRes.rows || [],
        stats: sumRes.values || {}
      });
    } catch {
      setSelectedDayEvidence(prev => prev ? { ...prev, loading: false } : null);
    }
  };

  const applyDayToGlobalFilter = (dateStr: string) => {
    const { start: dayStart, end: dayEnd } = dayBounds(dateStr);
    setQuery((prev: any) => ({
      ...prev,
      time_range: {
        ...prev.time_range,
        start: dayStart,
        end: dayEnd,
        preset: 'custom'
      }
    }));
  };

  // Sort models by selected rank metric
  const sortedModels = [...modelsData].sort((a, b) => {
    const va = numericValue(a[modelRankMetric]) ?? Number.NEGATIVE_INFINITY;
    const vb = numericValue(b[modelRankMetric]) ?? Number.NEGATIVE_INFINITY;
    return vb - va;
  });

  const proportions = sortedModels.map(model => numericValue(model.proportions?.[modelRankMetric]));
  const timezone = query.time_range.timezone || 'UTC';
  const endDate = query.time_range.end ? localDate(query.time_range.end, timezone, -1) : localDate(new Date().toISOString(), timezone);
  const startDate = query.time_range.start ? localDate(query.time_range.start, timezone) : (() => {
    const d = new Date(`${endDate}T00:00:00Z`);
    d.setUTCFullYear(d.getUTCFullYear() - 1);
    d.setUTCDate(d.getUTCDate() + 1);
    return d.toISOString().slice(0, 10);
  })();
  const dailyByDate = new Map(dailyData.map(point => [point.bucket, point]));
  const calendarDays: MetricRow[] = [];
  for (let day = startDate; day <= endDate; day = nextDate(day)) {
    calendarDays.push(dailyByDate.get(day) || { bucket: day });
  }
  const heatmapMaxValue = calendarDays.reduce(
    (maximum, point) => Math.max(maximum, heatmapValue(point, heatmapMetric) ?? 0), 0
  );
  // Repeated DST hours remain distinct in the API. This display intentionally
  // combines them with all other occurrences of the same weekday and hour.
  const weekdayHours = Array.from({ length: 7 }, () => Array.from({ length: 24 }, () => [] as MetricRow[]));
  for (const point of hourlyData) {
    if (!point.bucket) continue;
    const date = point.bucket.slice(0, 10);
    const hour = Number(point.bucket.slice(11, 13));
    const weekday = new Date(`${date}T00:00:00Z`).getUTCDay();
    if (Number.isInteger(hour) && hour >= 0 && hour < 24) weekdayHours[weekday][hour].push(point);
  }
  const hourlyValue = (points: MetricRow[]): number | null => {
    // Distinct sessions cannot be summed across hours.
    if (!points.length || heatmapMetric === 'sessions') return null;
    const values = points.map(point => heatmapValue(point, heatmapMetric));
    return values.some(value => value === null) ? null : values.reduce<number>((sum, value) => sum + (value ?? 0), 0);
  };
  const hourlyMax = Math.max(0, ...weekdayHours.flatMap(day => day.map(points => hourlyValue(points) ?? 0)));

  const widgetContent: Record<string, React.ReactNode> = {
    metrics: (
      <>
      {/* 1. Headline Metrics Grid with Equivalent-Period Comparison */}
      <div className="overview-kpi-grid">
        {/* Total Tokens */}
        <div className="kpi-card">
          <div className="kpi-header">
            <span>Total Tokens</span>
            <span className="provenance-tag">Billable sum</span>
          </div>
          <div className="kpi-value-row">
            <span className="kpi-value">
              {formatCount(values.total_tokens)}
            </span>
            {(() => {
              const delta = calcDelta(values.total_tokens, priorValues.total_tokens);
              if (delta === null) return <span className="kpi-delta neutral">No baseline</span>;
              return (
                <span className={`kpi-delta ${delta >= 0 ? 'positive' : 'negative'}`}>
                  {delta >= 0 ? '↑' : '↓'} {Math.abs(delta).toFixed(1)}%
                </span>
              );
            })()}
          </div>
          <div className="kpi-footer">
            <span>Fresh: {formatCount(values.fresh_input_tokens)}</span>
            <span>Out: {formatCount(values.output_tokens)}</span>
          </div>
        </div>

        {/* Requests & Sessions */}
        <div className="kpi-card">
          <div className="kpi-header">
            <span>Requests & Sessions</span>
            <span className="provenance-tag">Canonical dedupe</span>
          </div>
          <div className="kpi-value-row">
            <span className="kpi-value">
              {formatCount(values.requests_count)}
            </span>
            {(() => {
              const delta = calcDelta(values.requests_count, priorValues.requests_count);
              if (delta === null) return <span className="kpi-delta neutral">No baseline</span>;
              return (
                <span className={`kpi-delta ${delta >= 0 ? 'positive' : 'negative'}`}>
                  {delta >= 0 ? '↑' : '↓'} {Math.abs(delta).toFixed(1)}%
                </span>
              );
            })()}
          </div>
          <div className="kpi-footer">
            <span>{formatCount(values.sessions_count)} sessions</span>
            <span>Archive records: {formatCount(values.archive_records_count)}</span>
          </div>
        </div>

        {/* Estimated Value (USD / INR) */}
        <div className="kpi-card">
          <div className="kpi-header">
            <span>API-Equivalent Value</span>
            <span className="provenance-tag">{Array.isArray(summaryData?.pricing_fx?.fx_rates_available) && summaryData.pricing_fx.fx_rates_available.length > 0 ? 'Historical FX rates available' : 'Historical FX unavailable'}</span>
          </div>
          <div className="kpi-value-row">
            <span className="kpi-value" style={{ color: '#10b981' }}>
              {formatUsd(values.api_equivalent_usd, 3, numericValue(summaryData?.missingness?.unpriced_requests_count) !== null && Number(summaryData.missingness.unpriced_requests_count) > 0 ? 'Unpriced' : 'Unavailable')}
            </span>
            {(() => {
              const delta = calcDelta(values.api_equivalent_usd, priorValues.api_equivalent_usd);
              if (delta === null) return <span className="kpi-delta neutral">No baseline</span>;
              return (
                <span className={`kpi-delta ${delta <= 0 ? 'positive' : 'negative'}`}>
                  {delta >= 0 ? '↑' : '↓'} {Math.abs(delta).toFixed(1)}%
                </span>
              );
            })()}
          </div>
          <div className="kpi-footer">
            <span>
              {Array.isArray(summaryData?.pricing_fx?.fx_rates_available) && summaryData.pricing_fx.fx_rates_available.length > 0 && numericValue(values.api_equivalent_inr) !== null
                ? `₹${Number(values.api_equivalent_inr).toFixed(2)}`
                : 'INR unavailable'}
            </span>
            <span>
              {numericValue(summaryData?.missingness?.unpriced_requests_count) !== null
                ? `Unpriced requests: ${formatCount(summaryData.missingness.unpriced_requests_count)}`
                : 'Pricing status unavailable'}
            </span>
          </div>
        </div>

        {/* Cache Hit Rate & Savings */}
        <div className="kpi-card">
          <div className="kpi-header">
            <span>Cache Effectiveness</span>
            <span className="provenance-tag">Read / (Fresh+Read)</span>
          </div>
          <div className="kpi-value-row">
            <span className="kpi-value" style={{ color: '#38bdf8' }}>
              {formatRate(values.cache_hit_rate)}
            </span>
            {(() => {
              const delta = calcDelta(values.cache_hit_rate, priorValues.cache_hit_rate);
              if (delta === null) return <span className="kpi-delta neutral">No baseline</span>;
              return (
                <span className={`kpi-delta ${delta >= 0 ? 'positive' : 'negative'}`}>
                  {delta >= 0 ? '↑' : '↓'} {Math.abs(delta).toFixed(1)}%
                </span>
              );
            })()}
          </div>
          <div className="kpi-footer">
            <span>Reads: {formatCount(values.cache_read_tokens)}</span>
            <span>
              {values.cache_savings_usd !== null && values.cache_savings_usd !== undefined
                ? `Save ${formatUsd(values.cache_savings_usd, 3)}`
                : 'Savings unavailable'}
            </span>
          </div>
        </div>

        {/* Generation Speed & Duration */}
        <div className="kpi-card">
          <div className="kpi-header">
            <span>Generation Speed & Latency</span>
            <span className="provenance-tag">Timed requests</span>
          </div>
          <div className="kpi-value-row">
            <span className="kpi-value" style={{ color: '#a855f7' }}>
              {numericValue(values.generation_speed_tps) !== null ? `${Number(values.generation_speed_tps).toFixed(1)} TPS` : '—'}
            </span>
          </div>
          <div className="kpi-footer">
            <span>
              TTFT: {numericValue(values.ttft_ms) !== null ? `${Math.round(Number(values.ttft_ms))}ms` : '—'}
            </span>
            <span>
              Eligible: {formatCount(summaryData?.eligible?.timed_requests)} reqs
            </span>
          </div>
          <div className="kpi-footer">
            <span>Mean recorded session span: {numericValue(values.session_duration_ms) === null ? 'Unavailable' : `${(Number(values.session_duration_ms) / 60000).toFixed(1)} min`}</span>
            <span>Recorded: {formatCount(summaryData?.missingness?.session_duration_observed_count)} / {formatCount(summaryData?.missingness?.session_duration_sessions_count)} sessions; not active time</span>
          </div>
        </div>
      </div>
      </>
    ),
    timeline: (
      <>
      {/* 2. Interactive Token Timeline with Grain & Series Selectors */}
      <div className="chart-container-box">
        <div className="chart-header">
          <div>
            <h3 style={{ fontSize: '13px', fontWeight: 600, color: '#fff' }}>
              Interactive Token Consumption Timeline
            </h3>
            <div style={{ fontSize: '11px', color: '#94a3b8' }}>
              Hourly, daily, weekly, or monthly bucket rollups with model dominance and pricing states
            </div>
          </div>

          <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
            {/* Grain Selector */}
            <div className="control-group">
              {(['hour', 'day', 'week', 'month'] as const).map(grain => (
                <button
                  key={grain}
                  className={`btn-pill ${bucketGrain === grain ? 'active' : ''}`}
                  onClick={() => setBucketGrain(grain)}
                >
                  {grain.toUpperCase()}
                </button>
              ))}
            </div>

            {/* Series Toggles */}
            <div className="control-group">
              <button
                className={`btn-pill ${activeSeries === 'all' ? 'active' : ''}`}
                onClick={() => setActiveSeries('all')}
              >
                Total
              </button>
              <button
                className={`btn-pill ${activeSeries === 'fresh' ? 'active' : ''}`}
                onClick={() => setActiveSeries('fresh')}
              >
                Fresh
              </button>
              <button
                className={`btn-pill ${activeSeries === 'cache_read' ? 'active' : ''}`}
                onClick={() => setActiveSeries('cache_read')}
              >
                Cache Read
              </button>
              <button
                className={`btn-pill ${activeSeries === 'cache_write' ? 'active' : ''}`}
                onClick={() => setActiveSeries('cache_write')}
              >
                Cache Write
              </button>
              <button
                className={`btn-pill ${activeSeries === 'output' ? 'active' : ''}`}
                onClick={() => setActiveSeries('output')}
              >
                Output
              </button>
            </div>
          </div>
        </div>

        {/* Chart Canvas */}
        <div ref={timelineChartRef} className="chart-canvas" />
        <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', marginTop: 8 }} aria-label="Timeline point drilldown">
          {modelTimeline.map(point => (
            <button key={`${point.bucket}-${point.model}`} className="btn-pill"
              onClick={() => {
                const { bucket_start: start, bucket_end: end, model } = point;
                if (typeof start !== 'string' || typeof end !== 'string' || !model) return;
                setQuery(prev => ({ ...prev, time_range: { ...prev.time_range, start, end, preset: 'custom' }, filters: { ...prev.filters, model: [model] } }));
              }}>
              {point.bucket} · {point.model} · {formatCount(point.total_tokens)} tokens · {formatCount(point.requests_count)} requests · {formatUsd(point.api_equivalent_usd)} · {String(point.total_tokens_status)}
            </button>
          ))}
        </div>
      </div>
      </>
    ),
    breakdown: (
      <>
      {/* 3. Model Distribution & Side-by-Side Harness Comparison Grid */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(360px, 1fr))', gap: '16px' }}>
        {/* Model Ranking & Distribution */}
        <div className="chart-container-box">
          <div className="chart-header">
            <div>
              <h3 style={{ fontSize: '13px', fontWeight: 600, color: '#fff' }}>Model Usage Distribution</h3>
              <div style={{ fontSize: '11px', color: '#94a3b8' }}>
                Ranked breakdown across canonical models and providers
              </div>
            </div>

            <div className="control-group">
              <select
                className="btn-pill"
                aria-label="Rank models by"
                value={modelRankMetric}
                onChange={e => setModelRankMetric(e.target.value as any)}
                style={{ outline: 'none' }}
              >
                <option value="total_tokens">Rank by Tokens</option>
                <option value="requests_count">Rank by Requests</option>
                <option value="output_tokens">Rank by Output</option>
                <option value="sessions_count">Rank by Sessions</option>
                <option value="api_equivalent_usd">Rank by Cost (USD)</option>
              </select>
            </div>
          </div>

          {proportions.length > 0 && proportions.every(value => value !== null) ? (
            <div role="img" aria-label={`Model distribution proportions for ${modelRankMetric}`} style={{
              width: 120, height: 120, borderRadius: '50%', margin: '12px auto',
              background: `conic-gradient(${(() => {
                let offset = 0;
                const colors = ['#38bdf8', '#a855f7', '#10b981', '#f59e0b', '#f472b6', '#fb7185'];
                return proportions.map((value, index) => {
                  const start = offset;
                  offset += (value ?? 0) * 100;
                  return `${colors[index % colors.length]} ${start}% ${offset}%`;
                }).join(', ');
              })()})`,
              display: 'grid', placeItems: 'center'
            }}>
              <div style={{ width: 65, height: 65, borderRadius: '50%', background: 'var(--bg-primary)' }} />
            </div>
          ) : <p>Distribution unavailable: selected metric has missing or partial coverage.</p>}
          <p style={{ fontSize: 10 }}>Session proportions count model-session associations; a session may appear under multiple models.</p>
          <div style={{ display: 'flex', flexDirection: 'column', gap: '10px', marginTop: '6px' }}>
            {sortedModels.map((m: any, idx: number) => {
              const val = numericValue(m[modelRankMetric]);
              const fraction = numericValue(m.proportions?.[modelRankMetric]);
              const share = fraction === null ? null : Math.round(fraction * 100);
              const unpricedRequests = numericValue(m.unpriced_requests);
              const costValue = numericValue(m.api_equivalent_usd);
              const costDisplay = costValue !== null
                ? formatUsd(costValue)
                : unpricedRequests !== null && unpricedRequests > 0
                  ? `Unpriced (${formatCount(unpricedRequests)} reqs)`
                  : 'Unavailable';

              return (
                <div
                  key={m.model + idx}
                  style={{
                    background: 'var(--bg-primary)',
                    padding: '10px 12px',
                    borderRadius: '6px',
                    border: '1px solid var(--border-subtle)'
                  }}
                >
                  <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: '4px' }}>
                    <span style={{ fontWeight: 600, color: '#f8fafc', fontSize: '12px' }}>
                      {idx + 1}. {m.display_name || m.model}
                      <span className="provenance-tag" style={{ marginLeft: 6 }}>
                        {m.provider}
                      </span>
                    </span>
                    <span style={{ color: '#38bdf8', fontWeight: 700, fontVariantNumeric: 'tabular-nums' }}>
                      {modelRankMetric === 'api_equivalent_usd' ? costDisplay : formatCount(val)}
                    </span>
                  </div>

                  {/* Progress bar */}
                  <div style={{ height: '5px', background: 'var(--bg-tertiary)', borderRadius: '3px', overflow: 'hidden' }}>
                    <div
                      style={{
                        width: `${share === null ? 0 : Math.min(100, share)}%`,
                        height: '100%',
                        background: 'linear-gradient(90deg, #38bdf8, #a855f7)'
                      }}
                    />
                  </div>

                  <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: '10px', color: '#64748b', marginTop: '4px' }}>
                    <span>{share === null ? 'Share unavailable (missing values)' : `${share}% of selected metric`} • {formatCount(m.requests_count)} reqs</span>
                    <span>Cost: {costDisplay} • TPS: {numericValue(m.speed_tps) === null ? '—' : formatCount(m.speed_tps)}</span>
                  </div>
                </div>
              );
            })}
            {sortedModels.length === 0 && (
              <div style={{ padding: '20px', textAlign: 'center', color: '#64748b', fontSize: '12px' }}>
                No model data matching active filters.
              </div>
            )}
          </div>
        </div>

        {/* Harness Comparison: Claude Code vs Oh My Pi */}
        <div className="chart-container-box">
          <div className="chart-header">
            <div>
              <h3 style={{ fontSize: '13px', fontWeight: 600, color: '#fff' }}>Harness Comparison</h3>
              <div style={{ fontSize: '11px', color: '#94a3b8' }}>
                Side-by-side performance under identical matched filters
              </div>
            </div>
            <span className="provenance-tag">WP17b Core</span>
          </div>

          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '10px', marginTop: '6px' }}>
            {/* Claude Code Column */}
            <div
              style={{
                background: 'var(--bg-primary)',
                padding: '12px',
                borderRadius: '6px',
                border: '1px solid var(--border-subtle)'
              }}
            >
              <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: '8px' }}>
                <span style={{ width: 8, height: 8, borderRadius: '50%', background: '#f59e0b' }} />
                <strong style={{ fontSize: '12px', color: '#fff' }}>Claude Code</strong>
              </div>
              <div style={{ display: 'flex', flexDirection: 'column', gap: '6px', fontSize: '11px' }}>
                <div>Tokens: <strong style={{ color: '#f8fafc' }}>{formatCount(harnessData.claude?.total_tokens)}</strong></div>
                <div>Requests: <strong style={{ color: '#f8fafc' }}>{formatCount(harnessData.claude?.requests_count)}</strong></div>
                <div title={harnessData.claude?.sessions_count === null ? 'Session ids not recorded for this harness' : undefined}>Sessions: <strong style={{ color: '#f8fafc' }}>{formatCount(harnessData.claude?.sessions_count)}</strong></div>
                <div>Cache Hit: <strong style={{ color: '#10b981' }}>{formatRate(harnessData.claude?.cache_hit_rate)}</strong></div>
                <div>Cost: <strong style={{ color: '#38bdf8' }}>{formatUsd(harnessData.claude?.api_equivalent_usd, 3)}</strong></div>
                <div>Mean recorded session span: {numericValue(harnessData.claude?.session_duration_ms) === null ? 'Unavailable' : `${(harnessData.claude.session_duration_ms / 60000).toFixed(1)} min`}</div>
                <div>TPS: <strong>{numericValue(harnessData.claude?.generation_speed_tps) === null ? '—' : formatCount(harnessData.claude?.generation_speed_tps)}</strong></div>
              </div>
            </div>

            {/* Oh My Pi Column */}
            <div
              style={{
                background: 'var(--bg-primary)',
                padding: '12px',
                borderRadius: '6px',
                border: '1px solid var(--border-subtle)'
              }}
            >
              <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: '8px' }}>
                <span style={{ width: 8, height: 8, borderRadius: '50%', background: '#38bdf8' }} />
                <strong style={{ fontSize: '12px', color: '#fff' }}>Oh My Pi (OMP)</strong>
              </div>
              <div style={{ display: 'flex', flexDirection: 'column', gap: '6px', fontSize: '11px' }}>
                <div>Tokens: <strong style={{ color: '#f8fafc' }}>{formatCount(harnessData.omp?.total_tokens)}</strong></div>
                <div>Requests: <strong style={{ color: '#f8fafc' }}>{formatCount(harnessData.omp?.requests_count)}</strong></div>
                <div title={harnessData.omp?.sessions_count === null ? 'Session ids not recorded for this harness' : undefined}>Sessions: <strong style={{ color: '#f8fafc' }}>{formatCount(harnessData.omp?.sessions_count)}</strong></div>
                <div>Cache Hit: <strong style={{ color: '#10b981' }}>{formatRate(harnessData.omp?.cache_hit_rate)}</strong></div>
                <div>Cost: <strong style={{ color: '#38bdf8' }}>{formatUsd(harnessData.omp?.api_equivalent_usd, 3)}</strong></div>
                <div>Mean recorded session span: {numericValue(harnessData.omp?.session_duration_ms) === null ? 'Unavailable' : `${(harnessData.omp.session_duration_ms / 60000).toFixed(1)} min`}</div>
                <div>TPS: <strong>{numericValue(harnessData.omp?.generation_speed_tps) === null ? '—' : formatCount(harnessData.omp?.generation_speed_tps)}</strong></div>
              </div>
            </div>
          </div>

          <div style={{ marginTop: '10px', fontSize: '11px', color: '#94a3b8', lineHeight: 1.4 }}>
            Unresolved overlap is not reconciled automatically; combined totals may include overlapping activity. Select one harness to isolate a source.
          </div>
        </div>
      </div>
      </>
    ),
    activity: (
      <>
      {/* 4. Activity Heatmap & Usage Calendar with Color Modes */}
      <div className="chart-container-box">
        <div className="chart-header">
          <div>
            <h3 style={{ fontSize: '13px', fontWeight: 600, color: '#fff' }}>Activity Calendar & Heatmap</h3>
            <div style={{ fontSize: '11px', color: '#94a3b8' }}>
              Select coloring mode to explore intensity. Click any date tile to set date global filter and inspect sessions, projects, and models.
            </div>
          </div>

          <div className="control-group">
            <span style={{ fontSize: '11px', color: '#94a3b8', marginRight: 4 }}>Coloring Mode:</span>
            {(['tokens', 'requests', 'sessions', 'activity', 'cost'] as const).map(mode => (
              <button
                key={mode}
                className={`btn-pill ${heatmapMetric === mode ? 'active' : ''}`}
                onClick={() => setHeatmapMetric(mode)}
              >
                {mode.replace('_', ' ').toUpperCase()}
              </button>
            ))}
          </div>
        </div>

        {/* Daily Calendar Tiles (Rolling window from timeline) */}
        <div style={{ marginTop: '10px' }}>
          <div style={{ fontSize: '11px', fontWeight: 600, color: '#94a3b8', marginBottom: 6 }}>
            Daily activity in the selected range. Missing dates are unknown, not zero; observed buckets may be partial. Active minutes union explicit request intervals only; session spans and duration without endpoints are not active time.
          </div>

          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(68px, 1fr))', gap: '6px' }}>
            {calendarDays.map(point => {
              const val = heatmapValue(point, heatmapMetric);
              const intensity = val === null || heatmapMaxValue === 0
                ? 0.15
                : Math.min(1, Math.max(0.15, val / heatmapMaxValue));
              const isSelected = selectedDayEvidence?.date === point.bucket;

              return (
                <button
                  type="button"
                  key={point.bucket}
                  className="calendar-day-tile"
                  onClick={() => { if (point.bucket) { handleSelectDay(point.bucket); applyDayToGlobalFilter(point.bucket); } }}
                  style={{
                    background: `rgba(56, 189, 248, ${intensity})`,
                    border: isSelected ? '2px solid #fff' : '1px solid var(--border-subtle)',
                    padding: '6px 4px',
                    display: 'flex',
                    flexDirection: 'column',
                    alignItems: 'center',
                    justifyContent: 'center',
                    gap: 2,
                    cursor: 'pointer'
                  }}
                  title={`${point.bucket}: ${val === null ? 'unavailable' : heatmapMetric === 'cost' ? formatUsd(val, 2) : formatCount(val)} (${heatmapMetric})`}
                >
                  <span style={{ fontSize: '9px', fontWeight: 700, color: '#fff' }}>
                    {point.bucket?.slice(5)}
                  </span>
                  <span style={{ fontSize: '9px', opacity: 0.9 }}>
                    {val === null ? '—' : heatmapMetric === 'cost' ? formatUsd(val, 2) : val > 1000 ? `${(val / 1000).toFixed(1)}k` : formatCount(val)}
                  </span>
                </button>
              );
            })}
          </div>
        </div>

        <div style={{ marginTop: 20 }}>
          <h4>Weekday / hour heatmap ({timezone})</h4>
          <p style={{ fontSize: 11, color: '#94a3b8' }}>
            Values combine observed hourly buckets across the selected range, including both offset-distinct repeated DST hours. Missing buckets are unknown. Session counts cannot be added across hours. Partial timing or pricing remains partial.
          </p>
          <div className="heatmap-grid" role="grid" aria-label="Weekday and hour activity">
            <span />
            {Array.from({ length: 24 }, (_, hour) => <span className="heatmap-label" key={hour}>{hour}</span>)}
            {weekdayHours.map((day, weekday) => (
              <React.Fragment key={weekday}>
                <span className="heatmap-label">{['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'][weekday]}</span>
                {day.map((points, hour) => {
                  const value = hourlyValue(points);
                  const statusField = heatmapMetric === 'activity' ? 'active_minutes_status' : heatmapMetric === 'cost' ? 'api_equivalent_usd_status' : heatmapMetric === 'tokens' ? 'total_tokens_status' : null;
                  const partial = statusField && points.some(point => point[statusField] !== 'exact');
                  const label = `${['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'][weekday]} ${String(hour).padStart(2, '0')}:00: ${value === null ? 'unavailable' : heatmapMetric === 'cost' ? formatUsd(value) : formatCount(value)} ${heatmapMetric}${partial ? ', partial coverage' : ''}`;
                  return <span key={hour} role="gridcell" tabIndex={0} aria-label={label} title={label} className="heatmap-cell"
                    style={{ background: value === null ? 'var(--bg-tertiary, #1f293d)' : `rgba(56, 189, 248, ${hourlyMax ? Math.max(0.15, value / hourlyMax) : 0.15})` }} />;
                })}
              </React.Fragment>
            ))}
          </div>
        </div>

        {/* Selected Day Evidence Drawer */}
        {selectedDayEvidence && (
          <div className="evidence-drawer">
            <div className="evidence-header">
              <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                <strong style={{ fontSize: '13px', color: '#fff' }}>
                  Evidence for {selectedDayEvidence.date}
                </strong>
                <button
                  className="btn-primary"
                  onClick={() => applyDayToGlobalFilter(selectedDayEvidence.date)}
                  style={{ padding: '3px 8px', fontSize: '10px' }}
                >
                  Apply Date to Global Filter
                </button>
              </div>

              <button
                className="btn-pill"
                onClick={() => setSelectedDayEvidence(null)}
                style={{ padding: '2px 8px' }}
              >
                ✕ Close
              </button>
            </div>

            {selectedDayEvidence.loading ? (
              <div style={{ padding: '16px', color: '#94a3b8', fontSize: '12px' }}>
                Fetching date evidence...
              </div>
            ) : (
              <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(260px, 1fr))', gap: '12px' }}>
                {/* Stats */}
                <div style={{ background: 'var(--bg-primary)', padding: '10px', borderRadius: '6px' }}>
                  <div style={{ fontSize: '11px', fontWeight: 600, color: '#94a3b8', marginBottom: 4 }}>Day Totals</div>
                  <div>Observed records only; coverage may be partial. No recorded activity does not prove zero usage.</div>
                  <div style={{ fontSize: '12px', color: '#f8fafc' }}>
                    Tokens: <strong>{formatCount(selectedDayEvidence.stats?.total_tokens)}</strong><br />
                    Requests: <strong>{formatCount(selectedDayEvidence.stats?.requests_count)}</strong><br />
                    Cost: <strong>{formatUsd(selectedDayEvidence.stats?.api_equivalent_usd, 3)}</strong><br />
                    Hit Rate: <strong>{formatRate(selectedDayEvidence.stats?.cache_hit_rate)}</strong>
                  </div>
                </div>

                {/* Models */}
                <div style={{ background: 'var(--bg-primary)', padding: '10px', borderRadius: '6px' }}>
                  <div style={{ fontSize: '11px', fontWeight: 600, color: '#94a3b8', marginBottom: 4 }}>Models Used</div>
                  <div style={{ fontSize: '11px', color: '#f8fafc' }}>
                    {selectedDayEvidence.models.length > 0 ? (
                      selectedDayEvidence.models.map((m: any) => (
                        <div key={m.model} style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 2 }}>
                          <span>{m.display_name || m.model}</span>
                          <span style={{ color: '#38bdf8' }}>{formatCount(m.requests_count)} reqs</span>
                        </div>
                      ))
                    ) : (
                      <span style={{ color: '#64748b' }}>No models recorded</span>
                    )}
                  </div>
                </div>

                {/* Projects & Sessions */}
                <div style={{ background: 'var(--bg-primary)', padding: '10px', borderRadius: '6px' }}>
                  <div style={{ fontSize: '11px', fontWeight: 600, color: '#94a3b8', marginBottom: 4 }}>Projects & Sessions</div>
                  <div style={{ fontSize: '11px', color: '#f8fafc' }}>
                    <div>Active Projects: <strong>{selectedDayEvidence.projects.length}</strong></div>
                    <div>Recorded Sessions: <strong>{selectedDayEvidence.sessions.length}</strong></div>
                    {selectedDayEvidence.projects.slice(0, 3).map((p: any) => (
                      <div key={p.project_id} style={{ color: '#94a3b8', fontSize: '10px' }}>
                        • {p.project_name || p.project_id}
                      </div>
                    ))}
                  </div>
                </div>
              </div>
            )}
          </div>
        )}
      </div>
      </>
    ),
  };

  return (
    <div className="page-container">
      {/* Header & Scope Status */}
      <div className="page-header-row">
        <div className="page-title-group">
          <h2>Unified Usage Overview</h2>
          <div className="page-subtitle">
            Cross-harness consumption, pricing coverage, and model efficiency across{' '}
            <span style={{ color: '#38bdf8', fontWeight: 600 }}>{query.harness_scope}</span> scope
          </div>
        </div>

        <div className="header-action-group">
          {values.has_unresolved_overlap && (
            <span className="badge-tag badge-yellow" title="Overlapping records detected across harnesses">
              ⚠ Unresolved Overlap
            </span>
          )}
          {values.total_tokens_status === 'partial' && (
            <span className="badge-tag badge-yellow">Partial Coverage</span>
          )}
          {values.total_tokens_status === 'exact' && (
            <span className="badge-tag badge-green">Exact Reconciled</span>
          )}
          <span className="provenance-tag">
            Timezone: {query.time_range.timezone || 'UTC'}
          </span>
        </div>
      </div>

      {/* Warning Alert if Overlap or Archive Coverage Exists */}
      {summaryData?.warnings && summaryData.warnings.length > 0 && (
        <div className="privacy-banner" style={{ borderColor: '#f59e0b' }}>
          <div>
            <strong>Notice:</strong> {summaryData.warnings.join(' • ')}
          </div>
        </div>
      )}
      <LayoutControls
        pageName="overview"
        context={context}
        defaultWidgets={DEFAULT_OVERVIEW_WIDGETS}
        widgets={widgets}
        onWidgetsChange={handleWidgetsChange}
      />
      <DashboardGrid
        widgets={widgets}
        onWidgetsChange={handleWidgetsChange}
        renderWidget={widgetId => widgetContent[widgetId] ?? null}
      />




    </div>
  );
}
