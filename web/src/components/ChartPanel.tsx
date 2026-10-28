import React, { useRef, useEffect, useState, useMemo, useCallback } from 'react';
import * as echarts from 'echarts';
import type { PageContext, QueryEnvelope, QueryFilters } from '../App';
import './Interaction.css';

// ============================================================================
// ChartPanel Interfaces & Types (WP22)
// ============================================================================

export interface ChartPanelProps {
  title: string;
  subtitle?: string;
  badge?: React.ReactNode;
  context: PageContext;
  option: echarts.EChartsOption;
  loading?: boolean;
  error?: string | null;
  empty?: boolean;
  emptyMessage?: string;
  height?: number | string;
  className?: string;

  /**
   * Primary filter dimension this chart binds to (e.g. 'model', 'provider', 'project', 'session', 'route', 'task', 'application').
   * Clicks and isolates directly modify this key in context.setQuery.
   */
  filterKey?: keyof QueryFilters;

  /** Custom click handler updating context.setQuery directly */
  onPointClick?: (
    params: echarts.ECElementEvent,
    setQuery: PageContext['setQuery'],
    currentQuery: QueryEnvelope
  ) => void;

  /** Custom legend toggle handler updating context.setQuery directly */
  onLegendSelectChanged?: (
    params: { name: string; selected: Record<string, boolean> },
    setQuery: PageContext['setQuery'],
    currentQuery: QueryEnvelope
  ) => void;

  /** Custom drilldown handler (e.g. timeline bar -> day zoom, or provider -> model breakdown) */
  onDrilldown?: (
    params: echarts.ECElementEvent,
    setQuery: PageContext['setQuery'],
    currentQuery: QueryEnvelope
  ) => void;

  /** Custom isolate handler */
  onIsolate?: (
    name: string,
    setQuery: PageContext['setQuery'],
    currentQuery: QueryEnvelope
  ) => void;

  actions?: React.ReactNode;
  showExportImage?: boolean;
  showFilterChips?: boolean;
  showResetAction?: boolean;
}

// ============================================================================
// Common Reset Filter Helper (WP22 Shared Utility)
// ============================================================================

/**
 * Resets query filters in the shared PageContext query envelope.
 * If keysToReset is omitted or empty, clears all filters.
 * If specific keys are passed (e.g. ['model', 'provider']), clears only those dimensions.
 */
export function resetQueryFilters(
  setQuery: PageContext['setQuery'],
  keysToReset?: (keyof QueryFilters)[]
): void {
  setQuery((prev: QueryEnvelope) => {
    if (!keysToReset || keysToReset.length === 0) {
      return {
        ...prev,
        filters: {},
        pagination: prev.pagination ? { ...prev.pagination, offset: 0 } : undefined,
      };
    }
    const updatedFilters: QueryFilters = { ...prev.filters };
    for (const key of keysToReset) {
      delete updatedFilters[key];
    }
    return {
      ...prev,
      filters: updatedFilters,
      pagination: prev.pagination ? { ...prev.pagination, offset: 0 } : undefined,
    };
  });
}

// ============================================================================
// ChartPanel Component Implementation
// ============================================================================

export function ChartPanel({
  title,
  subtitle,
  badge,
  context,
  option,
  loading = false,
  error = null,
  empty = false,
  emptyMessage = 'No chart data available for the active filters.',
  height = 320,
  className = '',
  filterKey,
  onPointClick,
  onLegendSelectChanged,
  onDrilldown,
  onIsolate,
  actions,
  showExportImage = true,
  showFilterChips = true,
  showResetAction = true,
}: ChartPanelProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartInstanceRef = useRef<echarts.ECharts | null>(null);

  const { query, setQuery } = context;

  // Active filters for this specific chart dimension
  const activeDimensionFilters: string[] = useMemo(() => {
    if (!filterKey) return [];
    const val = query.filters[filterKey];
    if (Array.isArray(val)) return val;
    if (typeof val === 'string' && val.trim()) return [val.trim()];
    return [];
  }, [query.filters, filterKey]);

  // Isolate callback
  const handleIsolate = useCallback(
    (targetName: string) => {
      if (!targetName) return;
      if (onIsolate) {
        onIsolate(targetName, setQuery, query);
        return;
      }
      if (filterKey) {
        setQuery((prev) => ({
          ...prev,
          filters: {
            ...prev.filters,
            [filterKey]: [targetName],
          },
          pagination: prev.pagination ? { ...prev.pagination, offset: 0 } : undefined,
        }));
      }
    },
    [onIsolate, filterKey, setQuery, query]
  );

  // Toggle filter callback
  const handleToggleFilter = useCallback(
    (targetName: string) => {
      if (!targetName || !filterKey) return;
      setQuery((prev) => {
        const currentFilters = prev.filters[filterKey];
        const currentList: string[] = Array.isArray(currentFilters)
          ? currentFilters
          : typeof currentFilters === 'string'
            ? [currentFilters]
            : [];
        const isSelected = currentList.includes(targetName);
        const nextList = isSelected
          ? currentList.filter((item) => item !== targetName)
          : [...currentList, targetName];

        return {
          ...prev,
          filters: {
            ...prev.filters,
            [filterKey]: nextList,
          },
          pagination: prev.pagination ? { ...prev.pagination, offset: 0 } : undefined,
        };
      });
    },
    [filterKey, setQuery]
  );

  // Drilldown callback
  const handleDrilldown = useCallback(
    (params: echarts.ECElementEvent) => {
      if (onDrilldown) {
        onDrilldown(params, setQuery, query);
        return;
      }

      // Built-in date drilldown: if the clicked name is an ISO date (YYYY-MM-DD)
      const clickedName = params.name;
      if (clickedName && /^\d{4}-\d{2}-\d{2}$/.test(clickedName)) {
        const nextDay = new Date(new Date(clickedName).getTime() + 86400000)
          .toISOString()
          .slice(0, 10);
        setQuery((prev) => ({
          ...prev,
          time_range: {
            ...prev.time_range,
            start: clickedName,
            end: nextDay,
            preset: 'custom',
          },
        }));
      }
    },
    [onDrilldown, setQuery, query]
  );

  // 1. Initialize ECharts & attach event listeners
  useEffect(() => {
    if (!containerRef.current) return;

    // Destroy prior instance if present
    if (chartInstanceRef.current && !chartInstanceRef.current.isDisposed()) {
      chartInstanceRef.current.dispose();
    }

    const chart = echarts.init(containerRef.current, undefined, {
      renderer: 'canvas',
    });
    chartInstanceRef.current = chart;

    // Handle clicks: updates shared query
    const handleClick = (params: echarts.ECElementEvent) => {
      if (onPointClick) {
        onPointClick(params, setQuery, query);
        return;
      }

      // Extract entity name from element
      let entityName = params.name;
      if (!entityName && typeof params.data === 'object' && params.data !== null && 'name' in params.data) {
        const itemObj = params.data as { name?: string };
        entityName = itemObj.name || '';
      } else if (!entityName && typeof params.data === 'string') {
        entityName = params.data;
      } else if (!entityName && params.seriesName) {
        entityName = params.seriesName;
      }

      if (entityName && filterKey) {
        handleToggleFilter(entityName);
      }
    };

    // Handle double-clicks: trigger drilldown
    const handleDblClick = (params: echarts.ECElementEvent) => {
      handleDrilldown(params);
    };

    // Handle legend changes
    const handleLegend = (params: unknown) => {
      const typedParams = params as { name: string; selected: Record<string, boolean> };
      if (onLegendSelectChanged) {
        onLegendSelectChanged(typedParams, setQuery, query);
      }
    };

    chart.on('click', handleClick);
    chart.on('dblclick', handleDblClick);
    chart.on('legendselectchanged', handleLegend);

    // Responsive ResizeObserver
    const resizeObserver = new ResizeObserver(() => {
      if (chart && !chart.isDisposed()) {
        chart.resize();
      }
    });
    resizeObserver.observe(containerRef.current);

    return () => {
      resizeObserver.disconnect();
      chart.off('click', handleClick);
      chart.off('dblclick', handleDblClick);
      chart.off('legendselectchanged', handleLegend);
      chart.dispose();
      chartInstanceRef.current = null;
    };
  }, [filterKey, handleToggleFilter, handleDrilldown, onPointClick, onLegendSelectChanged, setQuery, query]);

  // 2. Synchronize ECharts Options with Professional Operate Theme
  useEffect(() => {
    const chart = chartInstanceRef.current;
    if (!chart || chart.isDisposed()) return;

    // Merge default dark professional styling into provided options
    const defaultPalette = [
      '#38bdf8', // Cyan
      '#10b981', // Emerald
      '#f59e0b', // Amber
      '#f43f5e', // Rose
      '#a855f7', // Purple
      '#6366f1', // Indigo
      '#ec4899', // Pink
      '#14b8a6', // Teal
      '#06b6d4', // Sky
    ];

    const themedOption: echarts.EChartsOption = {
      backgroundColor: 'transparent',
      color: defaultPalette,
      textStyle: {
        fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif',
        color: '#94a3b8',
      },
      tooltip: {
        backgroundColor: '#111827',
        borderColor: '#334155',
        borderWidth: 1,
        textStyle: {
          color: '#f8fafc',
          fontSize: 12,
        },
        padding: [8, 12],
        ...(option.tooltip || {}),
      },
      legend: {
        textStyle: {
          color: '#94a3b8',
          fontSize: 11,
        },
        pageTextStyle: {
          color: '#94a3b8',
        },
        ...(option.legend || {}),
      },
      ...option,
    };

    chart.setOption(themedOption, true);
  }, [option]);

  // Export chart image
  const handleExportPng = () => {
    const chart = chartInstanceRef.current;
    if (!chart || chart.isDisposed()) return;

    const dataUrl = chart.getDataURL({
      type: 'png',
      pixelRatio: 2,
      backgroundColor: '#0a0e17',
    });

    const link = document.createElement('a');
    link.href = dataUrl;
    link.download = `${title.toLowerCase().replace(/[^a-z0-9]+/g, '_')}_chart.png`;
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
  };

  return (
    <div className={`tl-chart-card ${className}`}>
      {/* Chart Header */}
      <div className="tl-chart-header">
        <div className="tl-chart-title-area">
          <div className="tl-chart-title">
            <span>{title}</span>
            {badge}
          </div>
          {subtitle && <span className="tl-chart-subtitle">{subtitle}</span>}
        </div>

        <div className="tl-chart-actions">
          {actions}

          {/* Quick Filter Reset for this dimension */}
          {showResetAction && filterKey && activeDimensionFilters.length > 0 && (
            <button
              type="button"
              className="tl-btn tl-btn-sm tl-btn-danger"
              onClick={() => resetQueryFilters(setQuery, [filterKey])}
              title={`Reset active ${String(filterKey)} filter`}
              aria-label={`Reset ${String(filterKey)} filter`}
            >
              Reset Filter
            </button>
          )}

          {/* PNG Export Button */}
          {showExportImage && (
            <button
              type="button"
              className="tl-btn tl-btn-sm"
              onClick={handleExportPng}
              title="Export high-resolution PNG"
              aria-label="Export chart image"
            >
              <span>📷</span>
              <span>Export</span>
            </button>
          )}
        </div>
      </div>

      {/* Active Filter Chips Bar */}
      {showFilterChips && filterKey && activeDimensionFilters.length > 0 && (
        <div className="tl-filter-chips">
          <span style={{ fontSize: 11, color: 'var(--tl-text-muted)' }}>Filtered by {String(filterKey)}:</span>
          {activeDimensionFilters.map((fVal) => (
            <span key={fVal} className="tl-filter-chip">
              <span>{fVal}</span>
              <button
                type="button"
                className="tl-filter-chip-remove"
                onClick={() => handleToggleFilter(fVal)}
                aria-label={`Remove filter ${fVal}`}
              >
                ×
              </button>
              {activeDimensionFilters.length > 1 && (
                <button
                  type="button"
                  style={{
                    background: 'none',
                    border: 'none',
                    color: 'var(--tl-accent-cyan)',
                    fontSize: 10,
                    cursor: 'pointer',
                    textDecoration: 'underline',
                    padding: '0 2px',
                  }}
                  onClick={() => handleIsolate(fVal)}
                  title={`Isolate ${fVal}`}
                >
                  isolate
                </button>
              )}
            </span>
          ))}
          <button
            type="button"
            style={{
              background: 'none',
              border: 'none',
              color: 'var(--tl-accent-rose)',
              fontSize: 11,
              cursor: 'pointer',
              marginLeft: 4,
            }}
            onClick={() => resetQueryFilters(setQuery, [filterKey])}
          >
            Clear all
          </button>
        </div>
      )}

      {/* Canvas Area with Overlays */}
      <div
        className="tl-chart-canvas-container"
        style={{ height: typeof height === 'number' ? `${height}px` : height }}
      >
        <div ref={containerRef} className="tl-chart-canvas" />

        {/* Loading Overlay */}
        {loading && (
          <div className="tl-state-overlay">
            <div className="tl-spinner" />
            <span className="tl-state-text">Updating chart visualization...</span>
          </div>
        )}

        {/* Error Overlay */}
        {error && (
          <div className="tl-state-overlay">
            <span style={{ fontSize: 20 }}>⚠️</span>
            <span className="tl-state-error-text">{error}</span>
          </div>
        )}

        {/* Empty State Overlay */}
        {!loading && !error && empty && (
          <div className="tl-state-overlay">
            <span style={{ fontSize: 20, color: 'var(--tl-text-muted)' }}>📊</span>
            <span className="tl-state-text">{emptyMessage}</span>
            {activeDimensionFilters.length > 0 && filterKey && (
              <button
                type="button"
                className="tl-btn tl-btn-sm"
                onClick={() => resetQueryFilters(setQuery, [filterKey])}
              >
                Reset {String(filterKey)} Filter
              </button>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

export default ChartPanel;
