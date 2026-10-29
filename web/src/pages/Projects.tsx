import React, { useState, useEffect } from 'react';
import './Explorers.css';
import { formatUsdAsInr, formatMoneyInr, formatUsdSecondary, formatInr } from '../lib/money';

type CoverageStatus = 'exact' | 'partial' | 'unavailable';

function CoverageBadge({ status }: { status?: CoverageStatus }) {
  // Partial coverage renders no badge; only Unavailable is shown.
  if (status !== 'unavailable') return null;
  return (
    <span className="badge-tag badge-gray">
      Unavailable
    </span>
  );
}

export interface ProjectRow {
  project_id: string;
  project_name: string;
  project_alias?: string | null;
  requests_count: number | null;
  sessions_count: number | null;
  active_days?: number | null;
  active_days_status?: CoverageStatus;
  active_days_count?: number | null;
  active_days_count_status?: CoverageStatus;
  total_tokens: number | null;
  total_tokens_status?: CoverageStatus;
  token_fields_status?: Record<string, CoverageStatus>;
  fresh_input_tokens: number | null;
  cache_read_tokens: number | null;
  cache_write_tokens: number | null;
  output_tokens: number | null;
  cache_hit_rate: number | null;
  api_equivalent_usd?: number | null;
  api_equivalent_usd_status?: CoverageStatus;
  api_equivalent_inr?: number | null;
  api_equivalent_inr_status?: CoverageStatus;
}

export interface SessionRow {
  session_id: string;
  origin_harness: string;
  started_at: string;
  duration_ms?: number | null;
  requests_count: number | null;
  total_tokens: number | null;
  total_tokens_status?: CoverageStatus;
  api_equivalent_usd?: number | null;
  api_equivalent_usd_status?: CoverageStatus;
  models_used: string[];
}

export interface ModelRow {
  model: string;
  display_name: string;
  provider: string;
  requests_count: number | null;
  total_tokens: number | null;
  total_tokens_status?: CoverageStatus;
  api_equivalent_usd?: number | null;
  api_equivalent_usd_status?: CoverageStatus;
}

export interface TimelineRow {
  bucket: string;
  total_tokens: number | null;
  requests_count: number | null;
  api_equivalent_usd?: number | null;
}

const SESSION_IDS_MISSING_TITLE = 'Session ids not recorded for this harness';

function formatCount(value: number | null | undefined): string {
  return value === null || value === undefined ? '—' : value.toLocaleString();
}

function formatCost(value: number | null | undefined): string {
  return formatUsdAsInr(value, 2, 'Unavailable');
}

function formatRate(value: number | null | undefined): string {
  return value === null || value === undefined ? '—' : `${(value * 100).toFixed(1)}%`;
}

function formatKilo(value: number | null): string {
  return value === null ? '—' : `${(value / 1000).toFixed(1)}k`;
}
function renderActiveDays(row: ProjectRow): string {
  if (row.active_days !== undefined && row.active_days !== null) {
    return formatCount(row.active_days);
  }
  if (row.active_days_count !== undefined && row.active_days_count !== null) {
    return formatCount(row.active_days_count);
  }
  return 'Unavailable';
}


export interface PageProps {
  context: {
    query: {
      contract_version: string;
      harness_scope: 'combined' | 'claude-code' | 'omp';
      time_range: {
        start?: string;
        end?: string;
        timezone: string;
        preset?: string;
      };
      filters: Record<string, string[] | undefined>;
      metric_id?: string;
      group_by?: string[];
      sort?: string | { field: string; order: 'asc' | 'desc' };
      pagination?: { limit: number; offset: number };
      search?: string;
      include_archive?: boolean;
    };
    setQuery: (updater: (prev: PageProps['context']['query']) => PageProps['context']['query']) => void;
    navigateToPage: (pageId: 'sessions' | 'models') => void;
    request: (metric_id: string, overrides?: Record<string, unknown>) => Promise<{
      rows?: unknown[];
      values?: Record<string, unknown>;
      eligible?: number | Record<string, unknown>;
      observed?: number | Record<string, unknown>;
      warnings?: string[];
    }>;
    write: (resource: string, payload: unknown, method?: string) => Promise<unknown>;
    bootstrap: unknown;
  };
}

type SortField = 'total_tokens' | 'api_equivalent_usd' | 'sessions_count' | 'requests_count' | 'project_name';

export default function Projects({ context }: PageProps) {
  const { query, setQuery, request } = context;

  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);
  const [projects, setProjects] = useState<ProjectRow[]>([]);
  const [loadedProjectContextKey, setLoadedProjectContextKey] = useState<string>('');
  const [searchFilter, setSearchFilter] = useState<string>('');
  const [sortField, setSortField] = useState<SortField>('total_tokens');
  const [sortOrder, setSortOrder] = useState<'asc' | 'desc'>('desc');
  const [pageOffset, setPageOffset] = useState<number>(0);
  const [pageSize, setPageSize] = useState<number>(15);

  // Selected project for detail drill-down
  const [selectedProject, setSelectedProject] = useState<ProjectRow | null>(null);
  const [loadedDetailContextKey, setLoadedDetailContextKey] = useState<string>('');
  const [detailLoading, setDetailLoading] = useState<boolean>(false);
  const [projectSessions, setProjectSessions] = useState<SessionRow[]>([]);
  const [projectModels, setProjectModels] = useState<ModelRow[]>([]);
  const [projectTimeline, setProjectTimeline] = useState<TimelineRow[]>([]);
  const projectContextKey = JSON.stringify([query.harness_scope, query.time_range, query.filters]);
  const selectedProjectFilterId = selectedProject
    ? selectedProject.project_id === 'unattributed' || !selectedProject.project_name
      ? 'unattributed'
      : selectedProject.project_id
    : null;
  const detailContextKey = JSON.stringify([
    selectedProjectFilterId,
    query.harness_scope,
    query.time_range,
    query.filters
  ]);

  // Load project rows
  useEffect(() => {
    setPageOffset(0);
  }, [query.harness_scope, query.time_range, query.filters]);

  useEffect(() => {
    let active = true;
    setLoading(true);
    setError(null);

    const fetchProjects = async () => {
      try {
        const res = await request('projects', {
          sort: { field: sortField, order: sortOrder },
          pagination: { limit: 1000, offset: 0 }
        });
        if (!active) return;
        const rawRows = (res.rows || []) as ProjectRow[];
        setLoadedProjectContextKey(projectContextKey);
        setProjects(rawRows);
        setSelectedProject(current => {
          if (!current) return current;
          return rawRows.find(project => project.project_id === current.project_id) ?? current;
        });
      } catch (err: unknown) {
        if (!active) return;
        const msg = err instanceof Error ? err.message : 'Failed to load projects';
        setError(msg);
      } finally {
        if (active) setLoading(false);
      }
    };

    fetchProjects();

    return () => {
      active = false;
    };
  }, [query.harness_scope, query.time_range, query.filters, sortField, sortOrder]);

  // Load drill-down evidence when project is selected
  useEffect(() => {
    const projectId = selectedProjectFilterId;
    if (!projectId) {
      setProjectSessions([]);
      setProjectModels([]);
      setProjectTimeline([]);
      setDetailLoading(false);
      setLoadedDetailContextKey(detailContextKey);
      return;
    }

    let active = true;
    setDetailLoading(true);

    const fetchProjectDetails = async () => {
      try {
        const activeProjectFilters = query.filters.project;
        const selectedProjectMatchesFilters = !activeProjectFilters?.length
          || activeProjectFilters.some(filter =>
            filter === projectId
            || filter === selectedProject?.project_name
            || filter === selectedProject?.project_alias
          );
        const selectedProjectFilter = selectedProjectMatchesFilters ? [projectId] : [];
        if (selectedProjectFilter.length === 0) {
          setLoadedDetailContextKey(detailContextKey);
          setProjectSessions([]);
          setProjectModels([]);
          setProjectTimeline([]);
          return;
        }

        const overrides: Record<string, unknown> = {
          filters: {
            ...query.filters,
            project: selectedProjectFilter
          }
        };

        const [sessRes, modRes, timeRes] = await Promise.all([
          request('sessions', {
            ...overrides,
            sort: { field: 'api_equivalent_usd', order: 'desc' },
            pagination: { limit: 5, offset: 0 }
          }).catch(() => ({ rows: [] })),
          request('models', overrides).catch(() => ({ rows: [] })),
          request('timeline', {
            ...overrides,
            group_by: ['day']
          }).catch(() => ({ rows: [] }))
        ]);

        if (!active) return;
        setProjectSessions((sessRes.rows || []) as SessionRow[]);
        setProjectModels((modRes.rows || []) as ModelRow[]);
        setProjectTimeline((timeRes.rows || []) as TimelineRow[]);
        setLoadedDetailContextKey(detailContextKey);
      } finally {
        if (active) setDetailLoading(false);
      }
    };

    fetchProjectDetails();

    return () => {
      active = false;
    };
  }, [selectedProjectFilterId, query.harness_scope, query.time_range, query.filters, detailContextKey]);

  const handleSort = (field: SortField) => {
    if (sortField === field) {
      setSortOrder(prev => (prev === 'asc' ? 'desc' : 'asc'));
    } else {
      setSortField(field);
      setSortOrder('desc');
      setPageOffset(0);
    }
    setPageOffset(0);
  };

  // Filter projects by search query
  const filteredProjects = projects.filter(p => {
    if (!searchFilter.trim()) return true;
    const q = searchFilter.toLowerCase();
    const nameMatch = (p.project_name || '').toLowerCase().includes(q);
    const idMatch = (p.project_id || '').toLowerCase().includes(q);
    const aliasMatch = (p.project_alias || '').toLowerCase().includes(q);
    return nameMatch || idMatch || aliasMatch;
  });

  const totalPages = Math.max(1, Math.ceil(filteredProjects.length / pageSize));
  const effectiveOffset = Math.max(0, Math.min(pageOffset, Math.floor(Math.max(0, filteredProjects.length - 1) / pageSize) * pageSize));
  const currentPage = Math.floor(effectiveOffset / pageSize) + 1;
  const pagedProjects = filteredProjects.slice(effectiveOffset, effectiveOffset + pageSize);
  const projectContextCurrent = !loading && loadedProjectContextKey === projectContextKey;
  const selectedProjectInContext = projectContextCurrent
    ? projects.find(project => project.project_id === selectedProject?.project_id)
    : undefined;
  const detailContextCurrent = !detailLoading && loadedDetailContextKey === detailContextKey;

  const applyProjectFilter = (project: ProjectRow | string) => {
    const projectId = typeof project === 'string' ? project : project.project_id;
    const projectName = typeof project === 'string' ? null : project.project_name;
    const projectFilterId = projectId === 'unattributed' || !projectName
      ? 'unattributed'
      : projectId;
    setQuery(prev => ({
      ...prev,
      filters: {
        ...prev.filters,
        project: [projectFilterId]
      }
    }));
  };

  return (
    <div className="page-container">
      {/* Title & Toolbar */}
      <div className="page-header-row">
        <div className="page-title-group">
          <h2>Project Usage Explorer</h2>
          <div className="page-subtitle">
            Attribution of token consumption, estimated costs, and session velocity across workspaces
          </div>
        </div>

        <div className="header-action-group">
          <span className="provenance-tag">Total Projects: {projects.length}</span>
          <span className="provenance-tag">Scope: {query.harness_scope}</span>
        </div>
      </div>

      {error && (
        <div className="banner-alert error">
          <span>Error loading projects: {error}</span>
        </div>
      )}

      {/* Control bar: search, sort summary, pagination */}
      <div className="control-toolbar">
        <div className="control-group">
          <div className="search-input-pill">
            <span style={{ color: '#64748b' }}>🔍</span>
            <input
              type="text"
              placeholder="Filter by project or repo..."
              value={searchFilter}
              onChange={e => {
                setSearchFilter(e.target.value);
                setPageOffset(0);
              }}
            />
          </div>
        </div>

        <div className="control-group">
          <span style={{ fontSize: '11px', color: '#94a3b8' }}>Sort by:</span>
          <button
            className={`btn-pill ${sortField === 'total_tokens' ? 'active' : ''}`}
            onClick={() => handleSort('total_tokens')}
          >
            Tokens {sortField === 'total_tokens' && (sortOrder === 'desc' ? '↓' : '↑')}
          </button>
          <button
            className={`btn-pill ${sortField === 'api_equivalent_usd' ? 'active' : ''}`}
            onClick={() => handleSort('api_equivalent_usd')}
          >
            Cost {sortField === 'api_equivalent_usd' && (sortOrder === 'desc' ? '↓' : '↑')}
          </button>
          <button
            className={`btn-pill ${sortField === 'sessions_count' ? 'active' : ''}`}
            onClick={() => handleSort('sessions_count')}
          >
            Sessions {sortField === 'sessions_count' && (sortOrder === 'desc' ? '↓' : '↑')}
          </button>
          <button
            className={`btn-pill ${sortField === 'requests_count' ? 'active' : ''}`}
            onClick={() => handleSort('requests_count')}
          >
            Requests {sortField === 'requests_count' && (sortOrder === 'desc' ? '↓' : '↑')}
          </button>
        </div>

        <div className="control-group">
          <label style={{ fontSize: '11px', color: '#94a3b8', display: 'flex', alignItems: 'center', gap: '4px' }}>
            Rows:
            <select
              aria-label="Rows per page"
              value={pageSize}
              onChange={e => {
                setPageSize(Number(e.target.value));
                setPageOffset(0);
              }}
              style={{ background: 'var(--bg-primary)', color: '#f8fafc', border: '1px solid var(--border-subtle)', borderRadius: '4px', padding: '2px 4px', fontSize: '11px' }}
            >
              <option value={10}>10</option>
              <option value={15}>15</option>
              <option value={25}>25</option>
              <option value={50}>50</option>
            </select>
          </label>
          <button
            className="btn-pill"
            disabled={effectiveOffset === 0}
            onClick={() => setPageOffset(prev => Math.max(0, prev - pageSize))}
            style={{ opacity: effectiveOffset === 0 ? 0.5 : 1 }}
            aria-label="Previous page"
          >
            ← Prev
          </button>
          <span style={{ fontSize: '11px', color: '#94a3b8' }}>
            {filteredProjects.length === 0 ? 0 : effectiveOffset + 1}–{Math.min(effectiveOffset + pageSize, filteredProjects.length)} of {filteredProjects.length}
          </span>
          <button
            className="btn-pill"
            disabled={effectiveOffset + pageSize >= filteredProjects.length}
            onClick={() => setPageOffset(prev => prev + pageSize)}
            style={{ opacity: effectiveOffset + pageSize >= filteredProjects.length ? 0.5 : 1 }}
            aria-label="Next page"
          >
            Next →
          </button>
        </div>
      </div>

      {/* Main Layout: Project Table + Side Detail Panel */}
      <div style={{ display: 'grid', gridTemplateColumns: selectedProject ? 'repeat(auto-fit, minmax(min(100%, 360px), 1fr))' : 'minmax(0, 1fr)', gap: '16px' }}>
        {/* Table View */}
        <div className="table-responsive-container">
          <table className="explorer-table">
            <thead>
              <tr>
                {([
                  ['project_name', 'Project Workspace'],
                  ['total_tokens', 'Total Tokens'],
                  ['api_equivalent_usd', 'Est. Cost (INR)'],
                  ['sessions_count', 'Sessions'],
                  ['requests_count', 'Requests']
                ] as const).map(([field, label]) => (
                  <th key={field} className={field === 'project_name' ? undefined : 'num-cell'} aria-sort={sortField === field ? (sortOrder === 'asc' ? 'ascending' : 'descending') : 'none'}>
                    <button className="btn-pill" onClick={() => handleSort(field)}>
                      {label} {sortField === field ? (sortOrder === 'asc' ? '↑' : '↓') : ''}
                    </button>
                  </th>
                ))}
                <th className="num-cell" title={`Distinct observed activity dates in ${query.time_range.timezone}; calculated before pagination`}>Active Days</th>
                <th className="num-cell">Cache Hit Rate</th>
                <th className="num-cell">Fresh / Cache Read / Out</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {loading && (
                <tr>
                  <td colSpan={9} style={{ textAlign: 'center', padding: '30px', color: '#94a3b8' }}>
                    Loading project workspaces…
                  </td>
                </tr>
              )}
              {!loading && pagedProjects.map(proj => {
                const isSelected = selectedProject?.project_id === proj.project_id;
                const isUnattributed = proj.project_id === 'unattributed' || !proj.project_name;
                const costStr = formatCost(proj.api_equivalent_usd);

                return (
                  <tr
                    key={proj.project_id}
                    className={isSelected ? 'selected' : ''}
                    onClick={() => setSelectedProject(isSelected ? null : proj)}
                  >
                    <td>
                      <div style={{ fontWeight: 600, color: isUnattributed ? '#f59e0b' : '#fff' }}>
                        {isUnattributed ? 'Unattributed Workspace' : proj.project_name}
                      </div>
                      <div style={{ fontSize: '10px', color: '#64748b', fontFamily: 'monospace' }}>
                        {proj.project_id} {proj.project_alias ? `(${proj.project_alias})` : ''}
                      </div>
                    </td>
                    <td className="num-cell" style={{ fontWeight: 600, color: '#38bdf8' }}>
                      <><span>{formatCount(proj.total_tokens)}</span><CoverageBadge status={proj.total_tokens_status} /></>
                    </td>
                    <td className="num-cell" style={{ color: '#10b981', fontWeight: 600 }}>
                      <><span>{costStr}</span><CoverageBadge status={proj.api_equivalent_usd_status} /></>
                    </td>
                    <td className="num-cell" title={proj.sessions_count === null ? SESSION_IDS_MISSING_TITLE : undefined}>{formatCount(proj.sessions_count)}</td>
                    <td className="num-cell">{formatCount(proj.requests_count)}</td>
                    <td className="num-cell">
                      {renderActiveDays(proj)}
                      <CoverageBadge status={proj.active_days_status ?? proj.active_days_count_status} />
                    </td>
                    <td className="num-cell">
                      <span className="badge-tag badge-blue">{formatRate(proj.cache_hit_rate)}</span>
                    </td>
                    <td className="num-cell" style={{ fontSize: '10px', color: '#94a3b8' }}>
                      {formatKilo(proj.fresh_input_tokens)} / {formatKilo(proj.cache_read_tokens)} / {formatKilo(proj.output_tokens)}
                    </td>
                    <td onClick={e => e.stopPropagation()}>
                      <button
                        className="btn-pill"
                        aria-label={`View ${isUnattributed ? 'Unattributed Workspace' : proj.project_name} details`}
                        aria-expanded={isSelected}
                        onClick={() => setSelectedProject(isSelected ? null : proj)}
                      >
                        Details
                      </button>
                      <button
                        className="btn-pill"
                        style={{ fontSize: '10px', padding: '2px 6px' }}
                        onClick={() => applyProjectFilter(proj)}
                        title="Filter entire dashboard to this project"
                      >
                        Filter
                      </button>
                    </td>
                  </tr>
                );
              })}

              {pagedProjects.length === 0 && !loading && (
                <tr>
                  <td colSpan={9} style={{ textAlign: 'center', padding: '30px', color: '#64748b' }}>
                    No projects found matching query and search filters.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
          {filteredProjects.length > pageSize && (
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', padding: '12px 8px', borderTop: '1px solid var(--border-subtle)' }}>
              <span style={{ fontSize: '11px', color: '#94a3b8' }}>
                Page {currentPage} of {totalPages} ({filteredProjects.length} projects)
              </span>
              <div style={{ display: 'flex', gap: '8px', alignItems: 'center' }}>
                <button
                  className="btn-pill"
                  disabled={effectiveOffset === 0}
                  onClick={() => setPageOffset(prev => Math.max(0, prev - pageSize))}
                  style={{ opacity: effectiveOffset === 0 ? 0.5 : 1 }}
                >
                  ← Prev
                </button>
                <button
                  className="btn-pill"
                  disabled={effectiveOffset + pageSize >= filteredProjects.length}
                  onClick={() => setPageOffset(prev => prev + pageSize)}
                  style={{ opacity: effectiveOffset + pageSize >= filteredProjects.length ? 0.5 : 1 }}
                >
                  Next →
                </button>
              </div>
            </div>
          )}

        {/* Project Detail Drawer */}
        {selectedProject && (
          <div className="detail-overlay-panel">
            <div className="detail-nav-bar">
              <div>
                <h3 style={{ fontSize: '14px', fontWeight: 700, color: '#fff' }}>
                  {selectedProject.project_name || 'Unattributed Workspace'}
                </h3>
                <div style={{ fontSize: '10px', color: '#64748b', fontFamily: 'monospace' }}>
                  ID: {selectedProject.project_id}
                </div>
              </div>
              <button
                className="btn-pill"
                onClick={() => setSelectedProject(null)}
                style={{ padding: '2px 8px' }}
              >
                ✕ Close
              </button>
            </div>

            {/* Quick Metrics */}
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '8px' }}>
              <div style={{ background: 'var(--bg-primary)', padding: '10px', borderRadius: '6px' }}>
                <div style={{ fontSize: '10px', color: '#94a3b8', textTransform: 'uppercase' }}>Total Consumption</div>
                <div style={{ fontSize: '16px', fontWeight: 700, color: '#38bdf8' }}>
                  {projectContextCurrent && selectedProjectInContext
                    ? <><span>{formatCount(selectedProjectInContext.total_tokens)}</span><CoverageBadge status={selectedProjectInContext.total_tokens_status} /></>
                    : projectContextCurrent ? 'Unavailable in current filter' : 'Refreshing current scope…'}
                </div>
              </div>
              <div style={{ background: 'var(--bg-primary)', padding: '10px', borderRadius: '6px' }}>
                <div style={{ fontSize: '10px', color: '#94a3b8', textTransform: 'uppercase' }}>Estimated Spend</div>
                <div style={{ fontSize: '16px', fontWeight: 700, color: '#10b981' }}>
                  {projectContextCurrent && selectedProjectInContext
                    ? <><span>{formatCost(selectedProjectInContext.api_equivalent_usd)}</span><CoverageBadge status={selectedProjectInContext.api_equivalent_usd_status} /></>
                    : projectContextCurrent ? 'Unavailable in current filter' : 'Refreshing current scope…'}
                </div>
              </div>
              <div style={{ background: 'var(--bg-primary)', padding: '10px', borderRadius: '6px' }}>
                <div style={{ fontSize: '10px', color: '#94a3b8', textTransform: 'uppercase' }}>Active Days</div>
                <div style={{ fontSize: '16px', fontWeight: 700 }}>
                  {projectContextCurrent && selectedProjectInContext
                    ? <>{renderActiveDays(selectedProjectInContext)} <CoverageBadge status={selectedProjectInContext.active_days_status ?? selectedProjectInContext.active_days_count_status} /></>
                    : projectContextCurrent ? 'Unavailable in current filter' : 'Refreshing current scope…'}
                </div>
                <div style={{ fontSize: '11px', color: '#94a3b8' }}>Distinct observed activity dates · {query.time_range.timezone} · full filtered project, not paged history</div>
              </div>
            </div>
            {projectContextCurrent && !selectedProjectInContext && (
              <div style={{ fontSize: '10px', color: '#f59e0b' }}>
                This project is outside the current shared filter context; scoped metrics are unavailable.
              </div>
            )}

            <div style={{ display: 'flex', gap: '8px' }}>
              <button
                className="btn-primary"
                style={{ width: '100%', fontSize: '11px', padding: '6px' }}
                onClick={() => applyProjectFilter(selectedProject)}
              >
                Apply Project to Global Filter
              </button>
            </div>

            {/* Model Mix in Project */}
            <div>
              <div style={{ fontSize: '11px', fontWeight: 600, color: '#94a3b8', marginBottom: '6px' }}>
                Model Mix in Project
              </div>
              {!detailContextCurrent ? (
                <div style={{ fontSize: '11px', color: '#64748b' }}>Refreshing detail evidence for current shared filters…</div>
              ) : (
                <div style={{ display: 'flex', flexDirection: 'column', gap: '4px' }}>
                  {projectModels.map(m => (
                    <div
                      key={m.model}
                      style={{
                        background: 'var(--bg-primary)',
                        padding: '6px 8px',
                        borderRadius: '4px',
                        display: 'flex',
                        justifyContent: 'space-between',
                        fontSize: '11px'
                      }}
                    >
                      <span style={{ color: '#f8fafc' }}>{m.display_name || m.model}</span>
                      <span style={{ color: '#38bdf8' }}>
                        {formatCount(m.total_tokens)} <CoverageBadge status={m.total_tokens_status} /> ({formatCount(m.requests_count)} reqs)
                      </span>
                    </div>
                  ))}
                  {projectModels.length === 0 && (
                    <div style={{ fontSize: '10px', color: '#64748b' }}>No model records in current window.</div>
                  )}
                </div>
              )}
            </div>

            {/* Top Expensive Sessions */}
            <div>
              <div style={{ fontSize: '11px', fontWeight: 600, color: '#94a3b8', marginBottom: '6px' }}>
                Top Expensive Sessions
              </div>
              {!detailContextCurrent ? (
                <div style={{ fontSize: '11px', color: '#64748b' }}>Refreshing detail evidence for current shared filters…</div>
              ) : (
                <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
                  {projectSessions.map(sess => (
                    <div
                      key={sess.session_id}
                      style={{
                        background: 'var(--bg-primary)',
                        padding: '8px 10px',
                        borderRadius: '4px',
                        border: '1px solid var(--border-subtle)',
                        fontSize: '11px'
                      }}
                    >
                      <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 2 }}>
                        <span style={{ fontFamily: 'monospace', color: '#f8fafc', fontSize: '10px' }}>
                          {sess.session_id.slice(0, 16)}...
                        </span>
                        <span style={{ color: '#10b981', fontWeight: 600 }}>
                          {formatCost(sess.api_equivalent_usd)} <CoverageBadge status={sess.api_equivalent_usd_status} />
                        </span>
                      </div>
                      <div style={{ display: 'flex', justifyContent: 'space-between', color: '#64748b', fontSize: '10px' }}>
                        <span>{sess.origin_harness} • {formatCount(sess.requests_count)} reqs</span>
                        <span>{formatCount(sess.total_tokens)} <CoverageBadge status={sess.total_tokens_status} /> tokens</span>
                      </div>
                      <button className="btn-pill" onClick={() => {
                        setQuery(prev => ({ ...prev, filters: { ...prev.filters, project: [selectedProjectFilterId!], session: [sess.session_id] } }));
                        context.navigateToPage('sessions');
                      }}>Inspect session</button>
                      </div>
                  ))}
                  {projectSessions.length === 0 && (
                    <div style={{ fontSize: '10px', color: '#64748b' }}>No session records.</div>
                  )}
                </div>
              )}
            </div>

            {/* Project Daily Velocity */}
            {detailContextCurrent && projectTimeline.length > 0 && (
              <div>
                <div style={{ fontSize: '11px', fontWeight: 600, color: '#94a3b8', marginBottom: '6px' }}>
                  Recent Daily Activity ({projectTimeline.length} returned buckets; not the active-day count)
                </div>
                <div style={{ display: 'flex', gap: '3px', alignItems: 'flex-end', height: '40px', background: 'var(--bg-primary)', padding: '4px', borderRadius: '4px' }}>
                  {projectTimeline.slice(-14).map(t => {
                    const maxTokens = Math.max(...projectTimeline.map(point => point.total_tokens ?? 0));
                    const heightPct = t.total_tokens == null || maxTokens === 0
                      ? 0
                      : Math.round((t.total_tokens / maxTokens) * 100);
                    return (
                      <div
                        key={t.bucket}
                        style={{
                          flex: 1,
                          height: `${heightPct}%`,
                          background: '#38bdf8',
                          borderRadius: '2px'
                        }}
                        title={`${t.bucket}: ${t.total_tokens == null ? 'Unavailable' : formatCount(t.total_tokens)} tokens`}
                      />
                    );
                  })}
                </div>
                <div style={{ fontSize: '11px', color: '#94a3b8', marginTop: '8px' }}>
                  {projectTimeline.slice(-14).map(point => (
                    <div key={point.bucket}>{point.bucket}: {formatCount(point.requests_count)} requests · {formatCount(point.total_tokens)} tokens · {formatCost(point.api_equivalent_usd)}</div>
                  ))}
                </div>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
