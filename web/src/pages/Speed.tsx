import React, { useState, useEffect, useMemo, useRef } from 'react';
import type { PageProps, QueryResponse } from '../App';
import * as echarts from 'echarts';
import './Measurement.css';
import { formatUsdAsInr } from '../lib/money';

type SpeedSliceDimension =
  | 'model'
  | 'provider'
  | 'route'
  | 'task'
  | 'context_size'
  | 'output_size'
  | 'timing_basis';

interface LatencyDistribution {
  count: number;
  mean: number | null;
  min: number | null;
  max: number | null;
  p50: number | null;
  p90: number | null;
  p95: number | null;
  p99: number | null;
  method?: string;
}

interface SpeedSliceMetrics {
  minimum_samples?: number;
  comparison_status?: string;
  ttft_comparison_status?: string;
  inferred_label_count?: number;
  counts: {
    total_requests: number;
    eligible_generation_requests: number;
    eligible_ttft_requests: number;
    eligible_total_duration_requests: number;
  };
  tps: {
    weighted: number | null;
    mean: number | null;
    median: number | null;
    p90: number | null;
    p95: number | null;
  };
  distributions: {
    ttft_ms: LatencyDistribution;
    duration_ms: LatencyDistribution;
    generation_duration_ms: LatencyDistribution;
    queue_duration_ms: LatencyDistribution;
  };
}
interface SpeedPoint {
  canonical_id: string;
  session_id: string | null;
  request_timestamp: string;
  model: string | null;
  provider: string | null;
  access_route: string | null;
  origin_harness: string | null;
  output_tokens: number | null;
  input_volume_proxy: number | null;
  tps: number | null;
  ttft_ms: number | null;
  api_equivalent_usd: number | null;
  pricing_status: string;
}
interface SpeedBucket {
  start: string;
  end: string;
  weighted_tps: number | null;
  eligible_requests: number;
  total_requests: number;
}
interface SpeedExtra {
  timeline?: { grain: string; buckets: SpeedBucket[] };
  primary_timing_basis?: string;
  slices?: Partial<Record<SpeedSliceDimension, Record<string, SpeedSliceMetrics>>>;
  points?: SpeedPoint[];
  task_minimum_samples?: number;
  task_models?: Record<string, Record<string, SpeedSliceMetrics>>;
  anomaly_baseline?: {
    window: { start: string | null; end: string | null; days: number };
    method: string;
    minimum_samples: number;
    groups: Array<{ model: string; provider: string; access_route: string; origin_harness: string;
      tps_samples: number; ttft_samples: number; median_tps: number | null; median_ttft_ms: number | null }>;
  };
}


export default function Speed({ context }: PageProps) {
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);
  const [data, setData] = useState<QueryResponse | null>(null);
  const [selectedSlice, setSelectedSlice] = useState<SpeedSliceDimension>('model');
  const [grain, setGrain] = useState<'hour' | 'day' | 'week' | 'month'>('day');
  const [scatterAxis, setScatterAxis] = useState<'output_tokens' | 'input_volume_proxy'>('output_tokens');
  const [scatterMetric, setScatterMetric] = useState<'tps' | 'ttft_ms'>('tps');
  const [latencyBasis, setLatencyBasis] = useState<string | null>(null);

  // Chart DOM refs
  const tpsChartRef = useRef<HTMLDivElement>(null);
  const tpsChartInstance = useRef<echarts.ECharts | null>(null);

  const distributionChartRef = useRef<HTMLDivElement>(null);
  const distributionChartInstance = useRef<echarts.ECharts | null>(null);
  const timelineRef = useRef<HTMLDivElement>(null);
  const scatterRef = useRef<HTMLDivElement>(null);

  // Fetch speed data
  useEffect(() => {
    let isCancelled = false;
    setLoading(true);
    setError(null);

    context
      .request('speed', {
        metric_id: 'generation_speed_tps',
        group_by: [grain],
      })
      .then((res) => {
        if (!isCancelled) {
          setData(res);
          setLoading(false);
        }
      })
      .catch((err: Error) => {
        if (!isCancelled) {
          setError(err.message || 'Failed to fetch speed analytics');
          setLoading(false);
        }
      });

    return () => {
      isCancelled = true;
    };
  }, [context, grain]);

  // Extract core speed KPI values
  const kpiValues = useMemo(() => {
    const vals = (data?.values as Record<string, unknown>) || {};
    const eligible = (data?.eligible as Record<string, unknown>) || {};
    const observed = (data?.observed as Record<string, unknown>) || {};

    return {
      tpsWeighted: typeof vals.generation_speed_tps_weighted === 'number' ? vals.generation_speed_tps_weighted : null,
      tpsMean: typeof vals.generation_speed_tps_mean === 'number' ? vals.generation_speed_tps_mean : null,
      tpsMedian: typeof vals.generation_speed_tps_median === 'number' ? vals.generation_speed_tps_median : null,
      tpsP90: typeof vals.generation_speed_tps_p90 === 'number' ? vals.generation_speed_tps_p90 : null,
      tpsP95: typeof vals.generation_speed_tps_p95 === 'number' ? vals.generation_speed_tps_p95 : null,
      ttftMean: typeof vals.ttft_ms_mean === 'number' ? vals.ttft_ms_mean : null,
      ttftMedian: typeof vals.ttft_ms_median === 'number' ? vals.ttft_ms_median : null,
      ttftP90: typeof vals.ttft_ms_p90 === 'number' ? vals.ttft_ms_p90 : null,
      ttftP95: typeof vals.ttft_ms_p95 === 'number' ? vals.ttft_ms_p95 : null,
      totalRequests: typeof eligible.total_requests === 'number' ? eligible.total_requests : 0,
      timedGenerationRequests: typeof eligible.timed_generation_requests === 'number' ? eligible.timed_generation_requests : 0,
      timedTtftRequests: typeof eligible.timed_ttft_requests === 'number' ? eligible.timed_ttft_requests : 0,
      measuredCount: typeof observed.measured_timing === 'number' ? observed.measured_timing : 0,
      proxyCount: typeof observed.log_span_proxy_timing === 'number' ? observed.log_span_proxy_timing : 0,
      untimedCount: typeof observed.unobserved_timing === 'number' ? observed.unobserved_timing : 0,
    };
  }, [data]);

  // Extract active slices map
  const activeSlices = useMemo(() => {
    const rawSlices = (data as unknown as { slices?: Record<string, Record<string, SpeedSliceMetrics>> })?.slices;
    if (!rawSlices) return {};
    return rawSlices[selectedSlice] || {};
  }, [data, selectedSlice]);

  // Extract anomalies
  const anomalies = useMemo(() => {
    return (data as unknown as { anomalies?: Array<Record<string, unknown>> })?.anomalies || [];
  }, [data]);
  const speedData = data as (QueryResponse & SpeedExtra) | null;
  const buckets = speedData?.timeline?.buckets || [];
  const points = speedData?.points || [];
  const baseline = speedData?.anomaly_baseline;
  const timingSlices = speedData?.slices?.timing_basis || {};
  const stageBasis = latencyBasis && timingSlices[latencyBasis]
    ? latencyBasis : speedData?.primary_timing_basis === 'none' ? 'untimed' : speedData?.primary_timing_basis || 'untimed';
  const stageSlice = timingSlices[stageBasis];
  const latencyStages = [
    { id: 'ttft_ms', label: 'TTFT', source: 'Recorded first-output timing (ttft_ms)', eligibility: 'Nonnegative recorded TTFT' },
    { id: 'generation_duration_ms', label: 'Generation', source: 'Recorded generation span (generation_duration_ms)', eligibility: 'Positive recorded generation span; output-token availability is not required' },
    { id: 'duration_ms', label: 'Total duration', source: 'Recorded request span (duration_ms)', eligibility: 'Nonnegative recorded total span' },
    { id: 'queue_duration_ms', label: 'Queue / start', source: 'Explicit queue instrumentation (queue_ms), when supplied', eligibility: 'Nonnegative explicitly recorded queue span only' },
  ] as const;

  const selectBucket = (bucket: SpeedBucket) => {
    context.setQuery((prev) => {
      const start = prev.time_range.start && Date.parse(prev.time_range.start) > Date.parse(bucket.start)
        ? prev.time_range.start : bucket.start;
      const end = prev.time_range.end && Date.parse(prev.time_range.end) < Date.parse(bucket.end)
        ? prev.time_range.end : bucket.end;
      return { ...prev, time_range: { ...prev.time_range, start, end, preset: 'custom' } };
    });
  };

  const inspectPoint = (point: SpeedPoint) => {
    context.setQuery((prev) => ({
      ...prev, search: point.canonical_id,
      filters: point.session_id ? { ...prev.filters, session: [point.session_id] } : prev.filters,
    }));
    context.navigateToPage('sessions');
  };

  useEffect(() => {
    if (!timelineRef.current || !scatterRef.current || !data) return;
    const timelineChart = echarts.init(timelineRef.current);
    const scatterChart = echarts.init(scatterRef.current);
    timelineChart.setOption({
      tooltip: { trigger: 'axis' },
      xAxis: { type: 'category', data: buckets.map((b) => b.start), axisLabel: { color: '#94a3b8' } },
      yAxis: { type: 'value', name: 'TPS', axisLabel: { color: '#94a3b8' } },
      series: [{ type: 'line', connectNulls: false, data: buckets.map((b) => b.weighted_tps) }],
    });
    timelineChart.on('click', (event) => {
      const bucket = buckets[event.dataIndex];
      if (bucket) selectBucket(bucket);
    });
    const plotted = points.filter((point) => point[scatterAxis] != null && point[scatterMetric] != null);
    scatterChart.setOption({
      tooltip: { trigger: 'item' },
      xAxis: { type: 'value', name: scatterAxis === 'output_tokens' ? 'Output tokens' : 'Input volume proxy' },
      yAxis: { type: 'value', name: scatterMetric === 'tps' ? 'TPS' : 'TTFT (ms)' },
      series: [{ type: 'scatter', data: plotted.map((p) => [p[scatterAxis], p[scatterMetric]]) }],
    });
    scatterChart.on('click', (event) => {
      const point = plotted[event.dataIndex];
      if (point) inspectPoint(point);
    });
    const resize = () => { timelineChart.resize(); scatterChart.resize(); };
    window.addEventListener('resize', resize);
    return () => { window.removeEventListener('resize', resize); timelineChart.dispose(); scatterChart.dispose(); };
  }, [data, scatterAxis, scatterMetric, context]);


  // Initialize & update TPS Slices Comparison Chart
  useEffect(() => {
    if (!tpsChartRef.current || !data) return;

    if (!tpsChartInstance.current) {
      tpsChartInstance.current = echarts.init(tpsChartRef.current);
    }
    const chart = tpsChartInstance.current;

    const sliceKeys = Object.keys(activeSlices);
    const eligible = (key: string) => selectedSlice !== 'task' || activeSlices[key]?.comparison_status === 'eligible';
    const weightedData = sliceKeys.map((k) => eligible(k) ? activeSlices[k]?.tps?.weighted ?? null : null);
    const medianData = sliceKeys.map((k) => eligible(k) ? activeSlices[k]?.tps?.median ?? null : null);
    const meanData = sliceKeys.map((k) => eligible(k) ? activeSlices[k]?.tps?.mean ?? null : null);
    const p95Data = sliceKeys.map((k) => eligible(k) ? activeSlices[k]?.tps?.p95 ?? null : null);

    const option: echarts.EChartsOption = {
      backgroundColor: 'transparent',
      tooltip: {
        trigger: 'axis',
        axisPointer: { type: 'shadow' },
        formatter: (params: unknown) => {
          const items = params as Array<{ axisValue: string; color: string; seriesName: string; value: number | null }>;
          let html = `<div style="font-family: monospace; font-size: 12px;">`;
          html += `<strong>${items[0]?.axisValue}</strong><br/>`;
          for (const item of items) {
            html += `<span style="display:inline-block;margin-right:5px;border-radius:10px;width:9px;height:9px;background-color:${item.color};"></span>`;
            html += `${item.seriesName}: <strong>${item.value == null ? 'unavailable' : `${item.value.toFixed(1)} tps`}</strong><br/>`;
          }
          const sampleCount = activeSlices[items[0]?.axisValue]?.counts?.eligible_generation_requests ?? 0;
          html += `<span style="color:#64748b;font-size:11px;">Eligible Timed Requests: ${sampleCount}</span></div>`;
          return html;
        },
      },
      legend: {
        data: ['Weighted TPS', 'Median TPS', 'Mean TPS', 'P95 TPS'],
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
        data: sliceKeys,
        axisLine: { lineStyle: { color: '#334155' } },
        axisLabel: { color: '#94a3b8', interval: 0, rotate: sliceKeys.length > 6 ? 25 : 0 },
      },
      yAxis: {
        type: 'value',
        axisLine: { lineStyle: { color: '#334155' } },
        splitLine: { lineStyle: { color: '#1f293d' } },
        axisLabel: {
          color: '#94a3b8',
          formatter: '{value} tps',
        },
      },
      series: [
        {
          name: 'Weighted TPS',
          type: 'bar',
          itemStyle: { color: '#38bdf8' },
          data: weightedData,
        },
        {
          name: 'Median TPS',
          type: 'bar',
          itemStyle: { color: '#10b981' },
          data: medianData,
        },
        {
          name: 'Mean TPS',
          type: 'bar',
          itemStyle: { color: '#a855f7' },
          data: meanData,
        },
        {
          name: 'P95 TPS',
          type: 'bar',
          itemStyle: { color: '#f59e0b' },
          data: p95Data,
        },
      ],
    };

    chart.setOption(option);

    chart.off('click');
    chart.on('click', (params: unknown) => {
      const p = params as { name: string };
      if (p.name && ['model', 'provider', 'route', 'task'].includes(selectedSlice)) {
        context.setQuery((prev) => ({
          ...prev,
          filters: {
            ...prev.filters,
            [selectedSlice]: [p.name],
          },
        }));
      }
    });

    const handleResize = () => chart.resize();
    window.addEventListener('resize', handleResize);
    return () => {
      window.removeEventListener('resize', handleResize);
    };
  }, [activeSlices, selectedSlice, data, context]);

  // Initialize & update Latency Percentiles Distribution Chart (TTFT & Duration)
  useEffect(() => {
    if (!distributionChartRef.current || !data) return;

    if (!distributionChartInstance.current) {
      distributionChartInstance.current = echarts.init(distributionChartRef.current);
    }
    const chart = distributionChartInstance.current;

    const sliceKeys = Object.keys(activeSlices);
    const eligible = (key: string) => selectedSlice !== 'task' || activeSlices[key]?.ttft_comparison_status === 'eligible';
    const ttftP50 = sliceKeys.map((k) => eligible(k) ? activeSlices[k]?.distributions?.ttft_ms?.p50 ?? null : null);
    const ttftP90 = sliceKeys.map((k) => eligible(k) ? activeSlices[k]?.distributions?.ttft_ms?.p90 ?? null : null);
    const ttftP95 = sliceKeys.map((k) => eligible(k) ? activeSlices[k]?.distributions?.ttft_ms?.p95 ?? null : null);

    const option: echarts.EChartsOption = {
      backgroundColor: 'transparent',
      tooltip: {
        trigger: 'axis',
        axisPointer: { type: 'shadow' },
        formatter: (params: unknown) => {
          const items = params as Array<{ axisValue: string; color: string; seriesName: string; value: number | null }>;
          let html = `<div style="font-family: monospace; font-size: 12px;">`;
          html += `<strong>${items[0]?.axisValue}</strong> (TTFT Latency)<br/>`;
          for (const item of items) {
            html += `<span style="display:inline-block;margin-right:5px;border-radius:10px;width:9px;height:9px;background-color:${item.color};"></span>`;
            html += `${item.seriesName}: <strong>${item.value == null ? 'unavailable' : `${item.value.toFixed(1)} ms`}</strong><br/>`;
          }
          html += `<span style="color:#64748b;font-size:11px;">Percentile Method: nearest_rank</span></div>`;
          return html;
        },
      },
      legend: {
        data: ['TTFT P50 (Median)', 'TTFT P90', 'TTFT P95'],
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
        data: sliceKeys,
        axisLine: { lineStyle: { color: '#334155' } },
        axisLabel: { color: '#94a3b8' },
      },
      yAxis: {
        type: 'value',
        axisLine: { lineStyle: { color: '#334155' } },
        splitLine: { lineStyle: { color: '#1f293d' } },
        axisLabel: {
          color: '#94a3b8',
          formatter: '{value} ms',
        },
      },
      series: [
        {
          name: 'TTFT P50 (Median)',
          type: 'bar',
          itemStyle: { color: '#10b981' },
          data: ttftP50,
        },
        {
          name: 'TTFT P90',
          type: 'bar',
          itemStyle: { color: '#f59e0b' },
          data: ttftP90,
        },
        {
          name: 'TTFT P95',
          type: 'bar',
          itemStyle: { color: '#f43f5e' },
          data: ttftP95,
        },
      ],
    };

    chart.setOption(option);

    const handleResize = () => chart.resize();
    window.addEventListener('resize', handleResize);
    return () => {
      window.removeEventListener('resize', handleResize);
    };
  }, [activeSlices, data, selectedSlice]);

  // Clean up chart instances
  useEffect(() => {
    return () => {
      tpsChartInstance.current?.dispose();
      tpsChartInstance.current = null;
      distributionChartInstance.current?.dispose();
      distributionChartInstance.current = null;
    };
  }, []);

  return (
    <div className="measurement-page">
      {loading && <p role="status">Loading speed analytics…</p>}
      {error && <p role="alert">{error}</p>}
      {/* Header */}
      <div className="measurement-header">
        <div className="measurement-title-group">
          <h1>
            Speed & Latency Analytics
            <span className="measurement-badge">WP14 Performance</span>
            {kpiValues.proxyCount > 0 && kpiValues.measuredCount > 0 && (
              <span className="measurement-badge warning">Separated Timing Bases</span>
            )}
          </h1>
          <p>
            Weighted, arithmetic mean, and median generation throughput (TPS) with deterministic nearest-rank
            latency distributions and sample-count denominators.
          </p>
        </div>

        {/* Slice Selector */}
        <div className="control-pills">
          <button
            className={`pill-btn ${selectedSlice === 'model' ? 'active' : ''}`}
            onClick={() => setSelectedSlice('model')}
          >
            Model
          </button>
          <button
            className={`pill-btn ${selectedSlice === 'provider' ? 'active' : ''}`}
            onClick={() => setSelectedSlice('provider')}
          >
            Provider
          </button>
          <button
            className={`pill-btn ${selectedSlice === 'route' ? 'active' : ''}`}
            onClick={() => setSelectedSlice('route')}
          >
            Route
          </button>
          <button
            className={`pill-btn ${selectedSlice === 'task' ? 'active' : ''}`}
            onClick={() => setSelectedSlice('task')}
          >
            Task
          </button>
          <button
            className={`pill-btn ${selectedSlice === 'context_size' ? 'active' : ''}`}
            onClick={() => setSelectedSlice('context_size')}
          >
            Input Volume Proxy
          </button>
          <button
            className={`pill-btn ${selectedSlice === 'output_size' ? 'active' : ''}`}
            onClick={() => setSelectedSlice('output_size')}
          >
            Output Size
          </button>
          <button
            className={`pill-btn ${selectedSlice === 'timing_basis' ? 'active' : ''}`}
            onClick={() => setSelectedSlice('timing_basis')}
          >
            Timing Basis
          </button>
        </div>
      </div>

      {/* Provenance & Separation Banner */}
      <div className="coverage-banner">
        <div className="coverage-details">
          <div className="coverage-item">
            <span className="coverage-label">Primary Timing Basis:</span>
            <span className="coverage-val">
              {String((data as unknown as { primary_timing_basis?: string })?.primary_timing_basis || 'none')}
            </span>
          </div>
          <div className="coverage-item">
            <span className="coverage-label">Measured Requests:</span>
            <span className="coverage-val">{kpiValues.measuredCount.toLocaleString()}</span>
          </div>
          <div className="coverage-item">
            <span className="coverage-label">Proxy Timed Requests:</span>
            <span className="coverage-val">{kpiValues.proxyCount.toLocaleString()}</span>
          </div>
          <div className="coverage-item">
            <span className="coverage-label">Untimed Requests:</span>
            <span className="coverage-val">{kpiValues.untimedCount.toLocaleString()}</span>
          </div>
        </div>
        <div className="coverage-details">
          <span className="measurement-badge success">Measured & Proxy Never Pooled</span>
        </div>
      </div>

      {/* KPI Cards Grid */}
      <div className="kpi-grid">
        <div className="kpi-card">
          <div className="kpi-label-row">
            <span className="kpi-label">Weighted Generation TPS</span>
            <span className="measurement-badge">Tokens / Total Sec</span>
          </div>
          <div className="kpi-value-row">
            <span className="kpi-value">
              {kpiValues.tpsWeighted !== null ? kpiValues.tpsWeighted.toFixed(1) : '—'}
            </span>
            <span className="kpi-unit">tps</span>
          </div>
          <span className="kpi-subtext">
            Across {kpiValues.timedGenerationRequests.toLocaleString()} eligible requests
          </span>
        </div>

        <div className="kpi-card">
          <div className="kpi-label-row">
            <span className="kpi-label">Median & Mean TPS</span>
            <span className="measurement-badge">Per-Request</span>
          </div>
          <div className="kpi-value-row">
            <span className="kpi-value">
              {kpiValues.tpsMedian !== null ? kpiValues.tpsMedian.toFixed(1) : '—'}
            </span>
            <span className="kpi-unit">
              med / {kpiValues.tpsMean !== null ? kpiValues.tpsMean.toFixed(1) : '—'} avg
            </span>
          </div>
          <span className="kpi-subtext">
            P90: {kpiValues.tpsP90 !== null ? `${kpiValues.tpsP90.toFixed(1)} tps` : '—'} | P95:{' '}
            {kpiValues.tpsP95 !== null ? `${kpiValues.tpsP95.toFixed(1)} tps` : '—'}
          </span>
        </div>

        <div className="kpi-card">
          <div className="kpi-label-row">
            <span className="kpi-label">Time to First Token (TTFT)</span>
            <span className="measurement-badge">Median (P50)</span>
          </div>
          <div className="kpi-value-row">
            <span className="kpi-value">
              {kpiValues.ttftMedian !== null ? kpiValues.ttftMedian.toFixed(0) : '—'}
            </span>
            <span className="kpi-unit">ms</span>
          </div>
          <span className="kpi-subtext">
            Mean: {kpiValues.ttftMean !== null ? `${kpiValues.ttftMean.toFixed(0)} ms` : '—'} (
            {kpiValues.timedTtftRequests.toLocaleString()} timed)
          </span>
        </div>

        <div className="kpi-card">
          <div className="kpi-label-row">
            <span className="kpi-label">TTFT P90 / P95 Tail Latency</span>
            <span className="measurement-badge danger">High Percentiles</span>
          </div>
          <div className="kpi-value-row">
            <span className="kpi-value">
              {kpiValues.ttftP90 !== null ? kpiValues.ttftP90.toFixed(0) : '—'}
            </span>
            <span className="kpi-unit">
              p90 / {kpiValues.ttftP95 !== null ? `${kpiValues.ttftP95.toFixed(0)} ms` : '—'} p95
            </span>
          </div>
          <span className="kpi-subtext">Deterministic nearest-rank calculation</span>
        </div>
      </div>

      <div className="section-card">
        <h2>Speed timeline</h2>
        <label>Time grain{' '}
          <select value={grain} onChange={(event) => setGrain(event.target.value as typeof grain)}>
            <option value="hour">Hour</option><option value="day">Day</option>
            <option value="week">Week</option><option value="month">Month</option>
          </select>
        </label>
        <p>Weighted TPS by local time; gaps with no eligible timing remain unavailable. Select a point or bucket to narrow the shared date filter.</p>
        <div ref={timelineRef} className="chart-container" aria-label="Speed timeline chart" />
        <table className="data-table"><thead><tr><th>Bucket</th><th>Weighted TPS</th><th>Eligible / requests</th></tr></thead>
          <tbody>{buckets.map((bucket) => <tr key={bucket.start}>
            <td><button className="pill-btn" onClick={() => selectBucket(bucket)}>{bucket.start}</button></td>
            <td>{bucket.weighted_tps == null ? 'Unavailable' : bucket.weighted_tps.toFixed(1)}</td>
            <td>{bucket.eligible_requests} / {bucket.total_requests}</td>
          </tr>)}</tbody>
        </table>
      </div>
      <div className="section-card">
        <h2>Speed versus request size</h2>
        <label>Horizontal axis{' '}
          <select value={scatterAxis} onChange={(event) => setScatterAxis(event.target.value as typeof scatterAxis)}>
            <option value="output_tokens">Output tokens</option>
            <option value="input_volume_proxy">Input volume proxy</option>
          </select>
        </label>
        <label>Vertical axis{' '}
          <select value={scatterMetric} onChange={(event) => setScatterMetric(event.target.value as typeof scatterMetric)}>
            <option value="tps">Generation TPS</option><option value="ttft_ms">TTFT (ms)</option>
          </select>
        </label>
        <p>Measured context occupancy is unavailable in request data. Input volume is fresh input plus cache reads, not occupancy; missing components are unavailable. TTFT and TPS have independent samples. Association is not causation. Cost is API-equivalent when priced.</p>
        <div ref={scatterRef} className="chart-container" aria-label="Speed versus request size scatter chart" />
        <table className="data-table"><thead><tr><th>Request / session</th><th>Output</th><th>Input proxy</th><th>TPS</th><th>TTFT ms</th><th>API-equivalent INR</th></tr></thead>
          <tbody>{points.map((point) => <tr key={point.canonical_id}>
            <td><button className="pill-btn" onClick={() => inspectPoint(point)}>{point.canonical_id} / {point.session_id || 'unavailable'}</button></td>
            <td>{point.output_tokens ?? 'Unavailable'}</td><td>{point.input_volume_proxy ?? 'Unavailable'}</td>
            <td>{point.tps == null ? 'Unavailable' : point.tps.toFixed(1)}</td><td>{point.ttft_ms ?? 'Unavailable'}</td>
            <td>{formatUsdAsInr(point.api_equivalent_usd, 4, 'Unpriced')}</td>
          </tr>)}</tbody>
        </table>
      </div>
      <div className="section-card">
        <h2>Speed by task and model</h2>
        <p>Descriptive, not a ranking or causal comparison. At least {speedData?.task_minimum_samples ?? 5} eligible samples per model and metric; sparse metrics abstain. Models may differ in provider, route, harness and workload; use shared filters to narrow them. Inferred labels are counted.</p>
        <div className="data-table-container"><table className="data-table">
          <thead><tr><th>Task / model</th><th>Inferred labels</th><th>TPS samples / requests</th><th>Weighted / median / p90 / p95 TPS</th><th>TTFT samples / requests</th><th>Median / p90 / p95 TTFT ms</th></tr></thead>
          <tbody>{Object.entries(speedData?.task_models || {}).flatMap(([task, models]) =>
            Object.entries(models).map(([model, item]) => <tr key={`${task}/${model}`}>
              <td>{task !== 'unavailable' && model !== 'unknown' ? <button className="pill-btn" onClick={() => context.setQuery((prev) => ({ ...prev, filters: { ...prev.filters, task: [task], model: [model] } }))}>{task} / {model}</button> : `${task} / ${model}`}</td>
              <td>{item.inferred_label_count ?? 0}</td>
              <td>{item.counts.eligible_generation_requests} / {item.counts.total_requests}</td>
              <td>{item.comparison_status === 'eligible' ? [item.tps.weighted, item.tps.median, item.tps.p90, item.tps.p95].map((v) => v == null ? 'Unavailable' : v.toFixed(1)).join(' / ') : 'Insufficient samples'}</td>
              <td>{item.counts.eligible_ttft_requests} / {item.counts.total_requests}</td>
              <td>{item.ttft_comparison_status === 'eligible' ? [item.distributions.ttft_ms.p50, item.distributions.ttft_ms.p90, item.distributions.ttft_ms.p95].map((v) => v == null ? 'Unavailable' : v.toFixed(0)).join(' / ') : 'Insufficient samples'}</td>
            </tr>))}
          </tbody></table></div>
      </div>
      {/* Main TPS Slices Chart */}
      <div className="section-card">
        <div className="section-header">
          <div>
            <h2 className="section-title">Throughput by {selectedSlice.replace('_', ' ').toUpperCase()}</h2>
            <p className="section-subtitle">
              Comparison of Weighted TPS, Median TPS, Mean TPS, and P95 TPS across slices.
              Untimed requests are excluded from denominators.
            </p>
          </div>
        </div>
        <div ref={tpsChartRef} className="chart-container-large" />
      </div>

      <section className="section-card" aria-labelledby="latency-stage-heading">
        <div className="section-header">
          <div>
            <h2 id="latency-stage-heading" className="section-title">Latency stage breakdown</h2>
            <p className="section-subtitle">
              Independent sample sets, not an additive waterfall. Queue/start is never inferred by subtracting
              TTFT, generation or total spans. First-output, generation and wall-clock spans are not interchangeable.
            </p>
          </div>
          <label htmlFor="stage-source-basis">Stage source basis{' '}
            <select id="stage-source-basis" aria-label="Stage source basis" value={stageBasis} onChange={(event) => setLatencyBasis(event.target.value)}
              style={{ color: 'var(--text-primary)', background: 'var(--bg-panel)', padding: '8px', border: '1px solid var(--border-subtle)', borderRadius: '6px' }}>
              {Object.keys(timingSlices).length > 0
                ? Object.keys(timingSlices).map((basis) => <option key={basis} value={basis}>{basis}</option>)
                : <option value="untimed">untimed</option>}
            </select>
          </label>
        </div>
        <p>
          Source basis: <strong>{stageBasis}</strong>
          {stageBasis === 'measured' ? ' — source-recorded timing.' : stageBasis === 'untimed'
            ? ' — no supported timing basis.' : ' — log-span proxy, not measured stage instrumentation.'}
          {' '}{stageSlice?.counts.total_requests ?? 0} requests in this basis / {kpiValues.totalRequests} filtered requests.
          Each eligible count is independent of TPS eligibility and other stages. Percentiles: nearest rank.
        </p>
        <div className="data-table-container" tabIndex={0} role="region" aria-label="Scrollable latency stage statistics">
          <table className="data-table" aria-label="Latency stages">
            <thead><tr><th>Stage / source field</th><th>Source basis</th><th>Eligible / basis requests</th>
              <th>Missing / ineligible</th><th>Mean (ms)</th><th>P50 (ms)</th><th>P90 (ms)</th><th>P95 (ms)</th></tr></thead>
            <tbody>{latencyStages.map((stage) => {
              const distribution = stageSlice?.distributions[stage.id];
              const count = distribution?.count ?? 0;
              const total = stageSlice?.counts.total_requests ?? 0;
              const display = (value: number | null | undefined) => count > 0 && value != null ? value.toLocaleString(undefined, { maximumFractionDigits: 2 }) : 'Unavailable';
              return <tr key={stage.id} data-stage={stage.id}>
                <th scope="row">{stage.label}<br /><small>{stage.source}<br />{stage.eligibility}</small></th>
                <td>{count > 0 ? stageBasis : 'Unavailable — no eligible source span'}</td>
                <td>{count} / {total}</td><td>{total - count}</td>
                <td>{display(distribution?.mean)}</td><td>{display(distribution?.p50)}</td>
                <td>{display(distribution?.p90)}</td><td>{display(distribution?.p95)}</td>
              </tr>;
            })}</tbody>
          </table>
        </div>
      </section>

      {/* Distribution Chart */}
      <div className="section-card">
        <div className="section-header">
          <div>
            <h2 className="section-title">TTFT Latency Distributions (P50 / P90 / P95)</h2>
            <p className="section-subtitle">
              TTFT reflects the source's recorded first-output timing; first chunk, transport, last-token and wall-clock stages are not interchangeable. Unsupported stages remain unavailable.
            </p>
          </div>
        </div>
        <div ref={distributionChartRef} className="chart-container" />
      </div>

      {/* Slice Table */}
      <div className="section-card">
        <div className="section-header">
          <div>
            <h2 className="section-title">Performance Slices Table ({selectedSlice})</h2>
            <p className="section-subtitle">
              Sample counts, weighted TPS, medians, and distribution percentiles.
            </p>
            {selectedSlice === 'task' && <p>Descriptive samples only; comparison requires {speedData?.task_minimum_samples ?? 5} eligible requests. Inferred labels are marked; no model rankings are produced.</p>}
          </div>
        </div>

        <div className="data-table-container">
          <table className="data-table">
            <thead>
              <tr>
                <th>{selectedSlice.toUpperCase()}</th>
                <th className="right-align">Sample Count</th>
                <th className="right-align">Weighted TPS</th>
                <th className="right-align">Median TPS</th>
                <th className="right-align">Mean TPS</th>
                <th className="right-align">P90 TPS</th>
                <th className="right-align">P95 TPS</th>
                <th className="right-align">TTFT P50 (ms)</th>
                <th className="right-align">TTFT P90 (ms)</th>
                <th className="right-align">TTFT P95 (ms)</th>
              </tr>
            </thead>
            <tbody>
              {Object.keys(activeSlices).length === 0 ? (
                <tr>
                  <td colSpan={10} style={{ textAlign: 'center', padding: '24px', color: 'var(--text-muted)' }}>
                    No performance data available for this slice.
                  </td>
                </tr>
              ) : (
                Object.entries(activeSlices).map(([key, item]) => {
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
                            if (['model', 'provider', 'route', 'task'].includes(selectedSlice)) {
                              context.setQuery((prev) => ({
                                ...prev,
                                filters: {
                                  ...prev.filters,
                                  [selectedSlice]: [key],
                                },
                              }));
                            }
                          }}
                        >
                          {key}
                        </button>
                        {selectedSlice === 'task' && <> — {item.comparison_status === 'eligible' ? 'sample sufficient' : 'insufficient samples'}; inferred labels: {item.inferred_label_count ?? 0}</>}
                      </td>
                      <td className="mono-cell right-align">
                        {item.counts?.eligible_generation_requests?.toLocaleString() ?? 0}
                      </td>
                      <td className="mono-cell right-align" style={{ fontWeight: 600 }}>
                        {item.tps?.weighted !== null && item.tps?.weighted !== undefined
                          ? item.tps.weighted.toFixed(1)
                          : '—'}
                      </td>
                      <td className="mono-cell right-align" style={{ color: 'var(--accent-emerald)' }}>
                        {item.tps?.median !== null && item.tps?.median !== undefined
                          ? item.tps.median.toFixed(1)
                          : '—'}
                      </td>
                      <td className="mono-cell right-align">
                        {item.tps?.mean !== null && item.tps?.mean !== undefined
                          ? item.tps.mean.toFixed(1)
                          : '—'}
                      </td>
                      <td className="mono-cell right-align">
                        {item.tps?.p90 !== null && item.tps?.p90 !== undefined
                          ? item.tps.p90.toFixed(1)
                          : '—'}
                      </td>
                      <td className="mono-cell right-align" style={{ color: 'var(--accent-amber)' }}>
                        {item.tps?.p95 !== null && item.tps?.p95 !== undefined
                          ? item.tps.p95.toFixed(1)
                          : '—'}
                      </td>
                      <td className="mono-cell right-align">
                        {item.distributions?.ttft_ms?.p50 !== null && item.distributions?.ttft_ms?.p50 !== undefined
                          ? item.distributions.ttft_ms.p50.toFixed(0)
                          : '—'}
                      </td>
                      <td className="mono-cell right-align">
                        {item.distributions?.ttft_ms?.p90 !== null && item.distributions?.ttft_ms?.p90 !== undefined
                          ? item.distributions.ttft_ms.p90.toFixed(0)
                          : '—'}
                      </td>
                      <td className="mono-cell right-align" style={{ color: 'var(--accent-rose)' }}>
                        {item.distributions?.ttft_ms?.p95 !== null && item.distributions?.ttft_ms?.p95 !== undefined
                          ? item.distributions.ttft_ms.p95.toFixed(0)
                          : '—'}
                      </td>
                    </tr>
                  );
                })
              )}
            </tbody>
          </table>
        </div>
      </div>

      <div className="section-card">
        <h2>Speed anomalies</h2>
        <p>Independent preceding {baseline?.window.days ?? 30}-day window: {baseline?.window.start ?? 'unavailable'} to {baseline?.window.end ?? 'unavailable'} (exclusive).
          Nearest-rank median; at least {baseline?.minimum_samples ?? 5} samples per metric, matched by model, provider, account, route, harness, timing basis and task.
          TPS below half a baseline above 5 TPS with at least 20 output tokens, or TTFT above both 2.5 times baseline and 1000 ms.</p>
        {baseline?.groups.map((group, i) => <p key={i}>
          {group.model || 'unknown model'} / {group.provider || 'unknown provider'} / {group.access_route || 'unknown route'} / {group.origin_harness || 'unknown harness'}:
          TPS baseline {group.median_tps ?? 'unavailable'} ({group.tps_samples} samples);
          TTFT baseline {group.median_ttft_ms ?? 'unavailable'} ms ({group.ttft_samples} samples).
          {group.tps_samples < baseline.minimum_samples || group.ttft_samples < baseline.minimum_samples ? ' Sparse metric abstains.' : ''}
        </p>)}
        {anomalies.length === 0 && <p>No anomalies detected; sparse or unavailable baselines abstain.</p>}
        <div className="anomaly-list">
          {anomalies.map((a, i) => (
            <div key={i} className="anomaly-item">
              <div className="anomaly-desc">
                <span className="anomaly-title">
                  {a.anomaly_type === 'severe_tps_drop' ? 'Throughput Degradation' : 'TTFT Latency Spike'}: {String(a.model ?? 'unknown')}
                </span>
                <span className="anomaly-meta">
                  {String(a.request_timestamp ?? 'unavailable')} | {String(a.provider ?? 'unknown')} / {String(a.access_route ?? 'unknown')} / {String(a.origin_harness ?? 'unknown')} |
                  Request {String(a.canonical_id ?? 'unavailable')} / session {String(a.session_id ?? 'unavailable')} |
                  Observed {String(a.observed_value)} {a.anomaly_type === 'severe_tps_drop' ? 'TPS' : 'ms'}; baseline {String(a.baseline_value)};
                  threshold {String(a.threshold)}; samples {String(a.sample_count)}.
                </span>
              </div>
              <button className="pill-btn" onClick={() => {
                context.setQuery((prev) => ({
                  ...prev, search: String(a.canonical_id),
                  filters: a.session_id ? { ...prev.filters, session: [String(a.session_id)] } : prev.filters,
                }));
                context.navigateToPage('sessions');
              }}>Inspect</button>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
