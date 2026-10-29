import { MetricDefinitionsPanel } from './components/MetricDefinitions';
import React, { Suspense, useState, useEffect, useCallback, useMemo, useRef } from 'react';

// ============================================================================
// Types and Contracts (WP09 / WP17a)
// ============================================================================

export type PageId =
  | 'overview'
  | 'models'
  | 'tokens-cache'
  | 'speed'
  | 'providers'
  | 'projects'
  | 'sessions'
  | 'harness'
  | 'efficiency'
  | 'reports';

export interface PageMeta {
  id: PageId;
  title: string;
  shortcut: string;
  metricFamily: string;
  description: string;
}

export const PAGES: PageMeta[] = [
  { id: 'overview', title: 'Overview', shortcut: '1', metricFamily: 'summary', description: 'Unified usage, session counts, pricing coverage and harness breakdown' },
  { id: 'models', title: 'Models', shortcut: '2', metricFamily: 'models', description: 'Model distribution, rankings, cost per model and reliability' },
  { id: 'tokens-cache', title: 'Tokens & Cache', shortcut: '3', metricFamily: 'summary', description: 'Disjoint billable counters, cache hit rates and TTL economics' },
  { id: 'speed', title: 'Speed', shortcut: '4', metricFamily: 'summary', description: 'TPS generation speed, TTFT latency and timed eligibility' },
  { id: 'providers', title: 'Providers & Limits', shortcut: '5', metricFamily: 'summary', description: 'Access routes, account aliases, quotas and spend limits' },
  { id: 'projects', title: 'Projects', shortcut: '6', metricFamily: 'projects', description: 'Project usage exploration, repositories and cost attribution' },
  { id: 'sessions', title: 'Sessions', shortcut: '7', metricFamily: 'sessions', description: 'Session explorer, duration, execution timeline and request links' },
  { id: 'harness', title: 'Harness Comparison', shortcut: '8', metricFamily: 'summary/tokens/speed/outcomes', description: 'Claude Code vs Oh My Pi comparison from shared usage metrics and harness outcome evidence' },
  { id: 'efficiency', title: 'Efficiency', shortcut: '9', metricFamily: 'summary', description: 'Task outcomes, token savings simulator and compaction advice' },
  { id: 'reports', title: 'Reports & Settings', shortcut: '0', metricFamily: 'summary', description: 'Automated periodic reports, exports and local privacy configuration' },
];

export type PresetKey = 'today' | '7d' | '30d' | 'month-to-date' | 'all' | 'custom';

export interface TimeRange {
  start?: string;
  end?: string;
  timezone: string;
  preset?: PresetKey;
}

export interface QueryFilters {
  model?: string[];
  provider?: string[];
  project?: string[];
  repository?: string[];
  session?: string[];
  route?: string[];
  task?: string[];
  application?: string[];
  subscription?: string[];
  duration?: string[];
  [key: string]: string[] | string | undefined;
}

export const SUPPORTED_QUERY_FILTER_KEYS: Record<string, true> = {
  model: true,
  provider: true,
  project: true,
  repository: true,
  session: true,
  route: true,
  task: true,
  application: true,
  subscription: true,
  duration: true,
};

export function getUnsupportedQueryFilterKeys(filters: unknown): string[] {
  if (filters === undefined || filters === null) return [];
  if (typeof filters !== 'object' || Array.isArray(filters)) return ['<invalid filters object>'];
  return Object.keys(filters).filter(
    (key) => !Object.prototype.hasOwnProperty.call(SUPPORTED_QUERY_FILTER_KEYS, key)
  );
}

export interface ComparisonPeriod {
  start: string;
  end: string;
  timezone: string;
  label?: string;
}

export interface AnomalySettings {
  expected_anomalies?: Array<string | { id: string; notes?: string }>;
}

export interface ComparisonPeriods {
  period_a: ComparisonPeriod;
  period_b: ComparisonPeriod;
}

export interface QueryEnvelope {
  contract_version: '1';
  harness_scope: 'combined' | 'claude-code' | 'omp';
  time_range: TimeRange;
  filters: QueryFilters;
  metric_id?: string;
  group_by?: string[];
  sort?: string | { field: string; order: 'asc' | 'desc' };
  pagination?: {
    limit: number;
    offset: number;
  };
  search?: string;
  include_archive?: boolean;
  scenario_inputs?: Record<string, string | number>;
  history_window?: { start?: string; end?: string; timezone: string };
  threshold_multiplier?: number;
  anomaly_settings?: AnomalySettings;
  comparison_periods?: ComparisonPeriods;
}

export interface BootstrapData {
  status: string;
  csrf_token: string;
  version: string;
  schema_version: number;
  harness_options: ('combined' | 'claude-code' | 'omp')[];
  source_health?: {
    status: string;
    version: string;
  };
}

export type MetricFamily = 'summary' | 'timeline' | 'models' | 'projects' | 'sessions' | 'requests' | 'events' | 'coverage';

export interface MetricValues {
  [key: string]: unknown;
  total_tokens?: number;
  total_tokens_status?: 'exact' | 'partial';
  has_unresolved_overlap?: boolean;
  live_disjoint_tokens?: number;
  archive_tokens?: number;
  fresh_input_tokens?: number;
  cache_read_tokens?: number;
  cache_write_5m_tokens?: number;
  cache_write_1h_tokens?: number;
  cache_write_unknown_ttl_tokens?: number;
  output_tokens?: number;
  reasoning_tokens?: number;
  reported_total_tokens?: number;
  requests_count?: number;
  sessions_count?: number | null;
  cache_hit_rate?: number;
  api_equivalent_usd?: number;
  api_equivalent_inr?: number;
  actual_billed_amount?: number;
  cache_savings_usd?: number;
  generation_speed_tps?: number;
  ttft_ms?: number;
  session_duration_ms?: number;
  archive_records_count?: number;
  unpriced_requests_count?: number;
  unpriced_requests?: number;
}

export interface MetricRow {
  [key: string]: unknown;
  bucket?: string;
  timestamp?: string;
  model?: string;
  normalized_model?: string;
  display_name?: string;
  provider?: string;
  project_id?: string;
  project_name?: string;
  session_id?: string;
  request_timestamp?: string;
  canonical_id?: string;
  total_tokens?: number;
  requests_count?: number;
  sessions_count?: number | null;
  cost_usd?: number;
  api_equivalent_usd?: number;
  generation_speed_tps?: number;
  speed_tps?: number;
  fresh_input_tokens?: number;
  cache_read_tokens?: number;
  cache_write_5m_tokens?: number;
  cache_write_1h_tokens?: number;
  cache_write_unknown_ttl_tokens?: number;
  output_tokens?: number;
  unpriced_requests_count?: number;
  unpriced_requests?: number;
  top_model?: string;
}

export interface QueryMetadata {
  [key: string]: unknown;
  timed_requests?: number;
  timed_generation_requests?: number;
  eligible_generation_requests?: number;
  eligible_timing_requests_count?: number;
  unpriced_requests_count?: number;
  priced_requests_count?: number;
  unknown_ttl_write_requests_count?: number;
  timing_basis_used?: string;
}

export interface QueryResponse {
  contract_version: string;
  query: QueryEnvelope;
  metric_id: string;
  family?: MetricFamily;
  values?: MetricValues;
  rows?: MetricRow[];
  units?: Record<string, string>;
  metric_definition?: {
    id?: string;
    family?: string;
    version?: string;
    formula?: string;
    denominator?: string;
    counter_semantics?: string;
    speed_basis?: string;
    cost_basis?: string;
  };
  eligible?: QueryMetadata;
  observed?: QueryMetadata;
  missingness?: QueryMetadata;
  coverage?: QueryMetadata;
  pricing_fx?: {
    pricing_version?: string;
    default_target_currency?: string;
    fx_rate_applied?: number;
  };
  warnings?: string[];
  error?: string;
}

const EMPTY_WARNINGS: string[] = [];

export type WriteMethod = 'POST' | 'PUT' | 'DELETE' | 'PATCH';

export interface SavedView {
  view_id: string;
  view_name: string;
  is_default: boolean;
  page_name?: string;
  filter_state: Partial<QueryEnvelope>;
  layout_state?: Record<string, unknown>;
  created_at?: string;
  updated_at?: string;
}

export interface PageContext {
  query: QueryEnvelope;
  setQuery: (updater: QueryEnvelope | Partial<QueryEnvelope> | ((prev: QueryEnvelope) => QueryEnvelope)) => void;
  request: <TResponse = QueryResponse>(metric_id: string, overrides?: Partial<QueryEnvelope>) => Promise<TResponse>;
  navigateToPage: (pageId: PageId) => void;
  write: (resource: string, payload: unknown, method?: WriteMethod) => Promise<unknown>;
  bootstrap: BootstrapData;
}

export interface PageProps {
  context: PageContext;
}

export type PageComponent = React.ComponentType<PageProps>;

// All ten pages are real modules; lazy loading keeps the initial shell small.
export const pageRegistry: Record<PageId, PageComponent> = {
  overview: React.lazy(() => import('./pages/Overview')) as PageComponent,
  models: React.lazy(() => import('./pages/Models')) as PageComponent,
  'tokens-cache': React.lazy(() => import('./pages/TokensCache')) as PageComponent,
  speed: React.lazy(() => import('./pages/Speed')) as PageComponent,
  providers: React.lazy(() => import('./pages/Providers')) as PageComponent,
  projects: React.lazy(() => import('./pages/Projects')) as PageComponent,
  sessions: React.lazy(() => import('./pages/Sessions')) as PageComponent,
  harness: React.lazy(() => import('./pages/Harness')) as PageComponent,
  efficiency: React.lazy(() => import('./pages/Efficiency')) as PageComponent,
  reports: React.lazy(() => import('./pages/Reports')) as PageComponent,
};



// Helper to compute date range boundaries for presets
function computeTimeRange(preset: PresetKey): { start?: string; end?: string } {
  const now = new Date();
  const pad = (n: number) => String(n).padStart(2, '0');
  const toDateStr = (d: Date) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;

  if (preset === 'today') {
    const todayStr = toDateStr(now);
    const tomorrow = new Date(now.getTime() + 86400000);
    return { start: todayStr, end: toDateStr(tomorrow) };
  } else if (preset === '7d') {
    const past = new Date(now.getTime() - 7 * 86400000);
    const tomorrow = new Date(now.getTime() + 86400000);
    return { start: toDateStr(past), end: toDateStr(tomorrow) };
  } else if (preset === '30d') {
    const past = new Date(now.getTime() - 30 * 86400000);
    const tomorrow = new Date(now.getTime() + 86400000);
    return { start: toDateStr(past), end: toDateStr(tomorrow) };
  } else if (preset === 'month-to-date') {
    const startOfMonth = new Date(now.getFullYear(), now.getMonth(), 1);
    const tomorrow = new Date(now.getTime() + 86400000);
    return { start: toDateStr(startOfMonth), end: toDateStr(tomorrow) };
  } else if (preset === 'all') {
    return { start: undefined, end: undefined };
  }
  return {};
}

// Strips UI-only fields from QueryEnvelope before wire transmission
function sanitizeEnvelopeForWire(envelope: QueryEnvelope): Record<string, unknown> {
  const wireTimeRange: Record<string, unknown> = {
    timezone: envelope.time_range.timezone || 'UTC',
  };
  if (envelope.time_range.start) wireTimeRange.start = envelope.time_range.start;
  if (envelope.time_range.end) wireTimeRange.end = envelope.time_range.end;

  const wireFilters: Record<string, unknown> = {};
  for (const [key, val] of Object.entries(envelope.filters)) {
    if (Array.isArray(val) && val.length > 0) {
      wireFilters[key] = val;
    } else if (typeof val === 'string' && val.trim() !== '') {
      wireFilters[key] = [val.trim()];
    }
  }

  const payload: Record<string, unknown> = {
    contract_version: '1',
    harness_scope: envelope.harness_scope,
    time_range: wireTimeRange,
    filters: wireFilters,
  };

  if (envelope.metric_id) payload.metric_id = envelope.metric_id;
  if (envelope.group_by && envelope.group_by.length > 0) payload.group_by = envelope.group_by;
  if (envelope.sort) payload.sort = envelope.sort;
  if (envelope.pagination) payload.pagination = envelope.pagination;
  if (envelope.search) payload.search = envelope.search;
  if (typeof envelope.include_archive === 'boolean') payload.include_archive = envelope.include_archive;
  if (envelope.scenario_inputs) payload.scenario_inputs = envelope.scenario_inputs;
  if (envelope.history_window) payload.history_window = envelope.history_window;
  if (typeof envelope.threshold_multiplier === 'number') payload.threshold_multiplier = envelope.threshold_multiplier;
  if (envelope.anomaly_settings) payload.anomaly_settings = envelope.anomaly_settings;
  if (envelope.comparison_periods) payload.comparison_periods = envelope.comparison_periods;
  return payload;
}

// ============================================================================
// Shell Component
// ============================================================================

export default function App() {
  const [defaultLandingPage, setDefaultLandingPage] = useState<PageId>(() => {
    try {
      const saved = localStorage.getItem('token_ledger_default_page');
      if (saved && PAGES.some((p) => p.id === saved)) {
        return saved as PageId;
      }
    } catch {}
    return 'overview';
  });
  const [activePage, setActivePage] = useState<PageId>(() => {
    try {
      const saved = localStorage.getItem('token_ledger_default_page');
      if (saved && PAGES.some((p) => p.id === saved)) {
        return saved as PageId;
      }
    } catch {}
    return 'overview';
  });
  const [mobileNavigationOpen, setMobileNavigationOpen] = useState(false);
  const [showDefinitionsPanel, setShowDefinitionsPanel] = useState(false);
  const [isMobile, setIsMobile] = useState(() => window.matchMedia('(max-width: 768px)').matches);
  const navigationRef = useRef<HTMLElement>(null);
  const navigationTriggerRef = useRef<HTMLButtonElement>(null);
  const mainLayoutRef = useRef<HTMLDivElement>(null);

  const closeMobileNavigation = useCallback(() => {
    if (mainLayoutRef.current) mainLayoutRef.current.inert = false;
    setMobileNavigationOpen(false);
    navigationTriggerRef.current?.focus();
  }, []);

  useEffect(() => {
    const media = window.matchMedia('(max-width: 768px)');
    const update = () => {
      setIsMobile(media.matches);
      if (!media.matches) setMobileNavigationOpen(false);
    };
    media.addEventListener('change', update);
    return () => media.removeEventListener('change', update);
  }, []);

  useEffect(() => {
    const main = mainLayoutRef.current;
    if (!isMobile || !mobileNavigationOpen) return;
    if (main) main.inert = true;
    navigationRef.current?.querySelector<HTMLButtonElement>('.mobile-navigation-close')?.focus();
    const handleNavigationKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault();
        event.stopImmediatePropagation();
        closeMobileNavigation();
      } else if (event.key === 'Tab') {
        const controls = navigationRef.current?.querySelectorAll<HTMLElement>(
          'button:not([disabled]), select:not([disabled]), [href], [tabindex="0"]'
        );
        if (!controls?.length) return;
        const first = controls[0];
        const last = controls[controls.length - 1];
        if (event.shiftKey && document.activeElement === first) {
          event.preventDefault();
          last.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
          event.preventDefault();
          first.focus();
        }
      }
    };
    window.addEventListener('keydown', handleNavigationKey, true);
    return () => {
      if (main) main.inert = false;
      window.removeEventListener('keydown', handleNavigationKey, true);
    };
  }, [isMobile, mobileNavigationOpen, closeMobileNavigation]);

  const [bootstrap, setBootstrap] = useState<BootstrapData>({
    status: 'initializing',
    csrf_token: '',
    version: '1.0.0',
    schema_version: 0,
    harness_options: ['combined', 'claude-code', 'omp'],
  });
  const [bootstrapError, setBootstrapError] = useState<string | null>(null);
  const [bootstrapReady, setBootstrapReady] = useState(false);
  const handleSetDefaultLandingPage = useCallback((newPage: PageId) => {
    setDefaultLandingPage(newPage);
    try {
      localStorage.setItem('token_ledger_default_page', newPage);
    } catch {}
    void fetch('/api/v1/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': bootstrap.csrf_token },
      body: JSON.stringify({
        setting_key: 'default_landing_page',
        setting_value: newPage,
        description: 'Default dashboard landing view',
      }),
    }).catch(() => {});
  }, [bootstrap.csrf_token]);

  // Global Query Envelope State
  const [query, setQueryState] = useState<QueryEnvelope>({
    contract_version: '1',
    harness_scope: 'combined',
    time_range: {
      preset: '30d',
      timezone: 'UTC',
      ...computeTimeRange('30d'),
    },
    filters: {},
    pagination: { limit: 100, offset: 0 },
    include_archive: true,
  });

  const [savedViews, setSavedViews] = useState<SavedView[]>([]);
  const [savedViewsLoaded, setSavedViewsLoaded] = useState(false);
  const hasRestoredDefaultView = useRef(false);
  const [selectedViewId, setSelectedViewId] = useState<string>('');
  const [isSavingView, setIsSavingView] = useState(false);
  const [isDefaultView, setIsDefaultView] = useState(false);
  const [newViewName, setNewViewName] = useState('');

  // UI Drawer and Modal States
  const [showFiltersDrawer, setShowFiltersDrawer] = useState(false);
  const [showShortcutsModal, setShowShortcutsModal] = useState(false);
  const [globalError, setGlobalError] = useState<string | null>(null);
  const [savedViewWarning, setSavedViewWarning] = useState<string | null>(null);
  const [activeWarnings, setActiveWarnings] = useState<string[]>([]);
  const [hasUnresolvedOverlap, setHasUnresolvedOverlap] = useState(false);


  const queryRef = useRef(query);
  queryRef.current = query;
  const csrfTokenRef = useRef(bootstrap.csrf_token);


  const searchInputRef = useRef<HTMLInputElement>(null);
  // 1. Initial Bootstrap & CSRF handshake
  const fetchBootstrap = useCallback(async () => {
    try {
      setBootstrapError(null);
      const [bootRes, healthRes] = await Promise.all([
        fetch('/api/v1/bootstrap', { credentials: 'same-origin' }),
        fetch('/health', { credentials: 'same-origin' }).catch(() => null),
      ]);

      if (!bootRes.ok) {
        throw new Error(`Bootstrap failed: HTTP ${bootRes.status}`);
      }

      const bootData = (await bootRes.json()) as Partial<BootstrapData>;
      let healthData: { status: string; version: string } = { status: 'ok', version: '1.0.0' };
      if (healthRes && healthRes.ok) {
        healthData = (await healthRes.json()) as { status: string; version: string };
      }

      const csrfToken = bootData.csrf_token || '';
      csrfTokenRef.current = csrfToken;
      setBootstrap({
        status: bootData.status || 'ok',
        csrf_token: csrfToken,
        version: bootData.version || '1.0.0',
        schema_version: bootData.schema_version ?? 2,
        harness_options: bootData.harness_options || ['combined', 'claude-code', 'omp'],
        source_health: healthData,
      });
      setBootstrapReady(true);

    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : 'Failed to connect to local Token Ledger service';
      setBootstrapError(msg);
      setGlobalError(msg);
    }
  }, []);

  const loadSavedViews = useCallback(async () => {
    try {
      const res = await fetch('/api/v1/saved-views', { credentials: 'same-origin' });
      if (res.ok) {
        const data = (await res.json()) as { saved_views?: SavedView[] };
        setSavedViews(data.saved_views || []);
      }
    } catch {
      // Saved views fetch is non-fatal on cold start
    } finally {
      setSavedViewsLoaded(true);
    }
  }, []);

  useEffect(() => {
    fetchBootstrap();
    loadSavedViews();
  }, [fetchBootstrap, loadSavedViews]);

  // Query state setter
  const setQuery = useCallback(
    (updater: QueryEnvelope | Partial<QueryEnvelope> | ((prev: QueryEnvelope) => QueryEnvelope)) => {
      setQueryState((prev) => {
        if (typeof updater === 'function') {
          return updater(prev);
        }
        return {
          ...prev,
          ...updater,
          filters: { ...prev.filters, ...(updater.filters || {}) },
          time_range: { ...prev.time_range, ...(updater.time_range || {}) },
        };
      });
    },
    []
  );

  const request = useCallback(
    async <TResponse = QueryResponse,>(metric_id: string, overrides?: Partial<QueryEnvelope>): Promise<TResponse> => {
      const currentQuery = queryRef.current;
      const merged: QueryEnvelope = {
        ...currentQuery,
        ...overrides,
        time_range: { ...currentQuery.time_range, ...overrides?.time_range },
        filters: { ...currentQuery.filters, ...overrides?.filters },
        metric_id,
        contract_version: '1',
      };
      const unsupportedFilters = getUnsupportedQueryFilterKeys(merged.filters);
      if (unsupportedFilters.length > 0) {
        const message = `Query not sent: unsupported filter key(s): ${unsupportedFilters.join(', ')}.`;
        setSavedViewWarning(message);
        return {
          contract_version: '1',
          query: merged,
          metric_id,
          error: message,
          values: {},
          rows: [],
        } as TResponse;
      }
      const wirePayload = sanitizeEnvelopeForWire(merged);

      try {
        const res = await fetch('/api/v1/query', {
          method: 'POST',
          credentials: 'same-origin',
          headers: {
            'Content-Type': 'application/json',
            'X-CSRF-Token': csrfTokenRef.current,
          },
          body: JSON.stringify(wirePayload),
        });

        if (!res.ok) {
          const errData = (await res.json().catch(() => ({ error: `HTTP ${res.status}` }))) as { error?: string };
          throw new Error(errData.error || `Query failed with status ${res.status}`);
        }
        const data = (await res.json()) as QueryResponse;

        const warnings = data.warnings ?? EMPTY_WARNINGS;


        setActiveWarnings((previous) =>
          previous.length === warnings.length && previous.every((warning, index) => warning === warnings[index])
            ? previous
            : warnings
        );

        const overlap = Boolean(
          data.values?.has_unresolved_overlap || data.values?.total_tokens_status === 'partial'
        );
        setHasUnresolvedOverlap((previous) => (previous === overlap ? previous : overlap));
        setGlobalError(null);
        return data as TResponse;
      } catch (err: unknown) {
        const msg = err instanceof Error ? err.message : 'Query execution error';
        setGlobalError(msg);
        return {
          contract_version: '1',
          query: merged,
          metric_id,
          error: msg,
          values: {},
          rows: [],
        } as TResponse;
      }
    },
    []
  );

  // 4. API State-Changing Writer
  const write = useCallback(
    async (resource: string, payload: unknown, method: WriteMethod = 'POST'): Promise<unknown> => {
      const cleanResource = resource.startsWith('/') ? resource.slice(1) : resource;
      const res = await fetch(`/api/v1/${cleanResource}`, {
        method,
        credentials: 'same-origin',
        headers: {
          'Content-Type': 'application/json',
          'X-CSRF-Token': csrfTokenRef.current,
        },
        body: payload ? JSON.stringify(payload) : undefined,
      });


      if (!res.ok) {
        const errData = (await res.json().catch(() => ({ error: `HTTP ${res.status}` }))) as { error?: string };
        throw new Error(errData.error || `Write operation failed: HTTP ${res.status}`);
      }
      return await res.json();
    },
    []
  );



  // Current page metadata
  const currentMeta = useMemo(() => PAGES.find((p) => p.id === activePage) || PAGES[0], [activePage]);


  // PageContext object passed to slots
  const pageContext: PageContext = useMemo(
    () => ({
      query,
      setQuery,
      navigateToPage: setActivePage,
      request,
      write,
      bootstrap,
    }),
    [query, setQuery, setActivePage, request, write, bootstrap]
  );

  // 6. Keyboard Shortcuts Handler
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (isMobile && mobileNavigationOpen) return;
      const target = e.target as HTMLElement | null;
      if (target && (target.tagName === 'INPUT' || target.tagName === 'TEXTAREA' || target.tagName === 'SELECT')) {
        if (e.key === 'Escape') {
          target.blur();
        }
        return;
      }

      if (e.key === '?') {
        e.preventDefault();
        setShowShortcutsModal((prev) => !prev);
      } else if (e.key === '/') {
        e.preventDefault();
        searchInputRef.current?.focus();
      } else if (e.key === 'Escape') {
        setShowShortcutsModal(false);
        setShowFiltersDrawer(false);
        setIsSavingView(false);
      } else if (e.key >= '0' && e.key <= '9') {
        const matched = PAGES.find((p) => p.shortcut === e.key);
        if (matched) {
          e.preventDefault();
          setActivePage(matched.id);
        }
      }
    };

    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [isMobile, mobileNavigationOpen]);

  const handleSaveViewSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!newViewName.trim()) return;

    const unsupportedFilters = getUnsupportedQueryFilterKeys(query.filters);
    if (unsupportedFilters.length > 0) {
      setSavedViewWarning(
        `Saved view not saved: unsupported filter key(s): ${unsupportedFilters.join(', ')}.`
      );
      return;
    }

    try {
      const payload = {
        view_name: newViewName.trim(),
        is_default: isDefaultView,
        page_name: activePage,
        filter_state: {
          contract_version: query.contract_version,
          filters: query.filters,
          time_range: query.time_range,
          harness_scope: query.harness_scope,
          search: query.search,
          include_archive: query.include_archive,
        },
      };
      await write('saved-views', payload, 'POST');
      setNewViewName('');
      setIsSavingView(false);
      await loadSavedViews();
    } catch (err: unknown) {
      setGlobalError(`Failed to save view: ${err instanceof Error ? err.message : String(err)}`);
    }
  };

  const handleSelectSavedView = useCallback((viewId: string) => {
    if (!viewId) {
      setSelectedViewId('');
      setSavedViewWarning(null);
      setQuery(() => ({
        contract_version: '1',
        harness_scope: 'combined',
        time_range: {
          preset: '30d',
          timezone: 'UTC',
          ...computeTimeRange('30d'),
        },
        filters: {},
        pagination: { limit: 100, offset: 0 },
        include_archive: true,
      }));
      return;
    }

    const matched = savedViews.find((view) => view.view_id === viewId);
    if (!matched) return;

    const filterState = matched.filter_state || {};
    const unsupportedFilters = getUnsupportedQueryFilterKeys(filterState.filters);
    if (unsupportedFilters.length > 0) {
      setSavedViewWarning(
        `Saved view "${matched.view_name}" not applied: unsupported filter key(s): ` +
          `${unsupportedFilters.join(', ')}. No query was sent; the current query is unchanged.`
      );
      return;
    }

    setSelectedViewId(viewId);
    setSavedViewWarning(null);
    const validHarness =
      filterState.harness_scope === 'combined' ||
      filterState.harness_scope === 'claude-code' ||
      filterState.harness_scope === 'omp'
        ? filterState.harness_scope
        : undefined;

    setQuery((previous) => ({
      ...previous,
      contract_version: filterState.contract_version === '1' ? '1' : previous.contract_version,
      harness_scope: validHarness || previous.harness_scope,
      filters: filterState.filters ? { ...filterState.filters } : {},
      time_range: {
        ...previous.time_range,
        ...(filterState.time_range || {}),
      },
      search: filterState.search,
      include_archive:
        typeof filterState.include_archive === 'boolean'
          ? filterState.include_archive
          : previous.include_archive,
    }));

    if (matched.page_name && PAGES.some((page) => page.id === matched.page_name)) {
      setActivePage(matched.page_name as PageId);
    }
  }, [savedViews, setQuery]);

  useEffect(() => {
    if (!savedViewsLoaded || hasRestoredDefaultView.current) return;
    hasRestoredDefaultView.current = true;
    const defaultView = savedViews.find((view) => view.is_default);
    if (defaultView) handleSelectSavedView(defaultView.view_id);
  }, [savedViewsLoaded, savedViews, handleSelectSavedView]);

  const handlePresetChange = (preset: PresetKey) => {
    const computed = computeTimeRange(preset);
    setQuery({
      time_range: {
        ...query.time_range,
        preset,
        start: computed.start,
        end: computed.end,
      },
    });
  };

  const activeFilterCount = useMemo(() => {
    let count = 0;
    for (const val of Object.values(query.filters)) {
      if (Array.isArray(val) && val.length > 0) count++;
      else if (typeof val === 'string' && val.trim() !== '') count++;
    }
    if (query.search) count++;
    return count;
  }, [query.filters, query.search]);
  // Every route maps to a genuine lazily loaded page module.
  // Lazily loaded component for the active route.
  const SlotComponent = pageRegistry[activePage];

  return (
    <div className="app-container">
      {/* 1. Left Sidebar Navigation & Scope */}
      {isMobile && mobileNavigationOpen && (
        <div className="mobile-navigation-backdrop" onClick={closeMobileNavigation} aria-hidden="true" />
      )}
      <aside
        id="main-navigation"
        ref={navigationRef}
        className={`app-sidebar${mobileNavigationOpen ? ' open' : ''}`}
        role={isMobile && mobileNavigationOpen ? 'dialog' : undefined}
        aria-modal={isMobile && mobileNavigationOpen ? true : undefined}
        aria-label="Main Navigation"
      >
        <div className="sidebar-header">
          <div className="brand-title">
            <div className="brand-icon">TL</div>
            <span>Token Ledger</span>
          </div>
          <span className="brand-version">v{bootstrap.version}</span>
          <button
            type="button"
            className="btn-secondary mobile-navigation-close"
            onClick={closeMobileNavigation}
            aria-label="Close navigation"
          >
            Close
          </button>
        </div>

        {/* Persistent Scope Box (Harness Selector) */}
        <div className="sidebar-scope">
          <div className="scope-title">
            <span>Harness Scope</span>
            <span>{query.harness_scope}</span>
          </div>
          <div className="harness-toggle-group" role="tablist" aria-label="Harness Selector">
            <button
              type="button"
              role="tab"
              aria-selected={query.harness_scope === 'combined'}
              className={`harness-toggle-btn ${query.harness_scope === 'combined' ? 'active' : ''}`}
              onClick={() => setQuery({ harness_scope: 'combined' })}
              title="Combined Oh My Pi and Claude Code scope"
            >
              Combined
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={query.harness_scope === 'claude-code'}
              className={`harness-toggle-btn ${query.harness_scope === 'claude-code' ? 'active' : ''}`}
              onClick={() => setQuery({ harness_scope: 'claude-code' })}
              title="Claude Code live telemetry only"
            >
              Claude
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={query.harness_scope === 'omp'}
              className={`harness-toggle-btn ${query.harness_scope === 'omp' ? 'active' : ''}`}
              onClick={() => setQuery({ harness_scope: 'omp' })}
              title="Oh My Pi live logs only"
            >
              OMP
            </button>
          </div>
        </div>

        {/* Default Landing Page Selector */}
        <div className="sidebar-scope" style={{ marginTop: 0, paddingBottom: 6 }}>
          <div className="scope-title">
            <span>Default Landing</span>
          </div>
          <select
            className="reports-select"
            style={{ width: '100%', fontSize: '11px', padding: '4px 6px', background: 'var(--bg-card, #1e1e1e)', color: 'inherit', border: '1px solid var(--border-color, #333)', borderRadius: 4 }}
            value={defaultLandingPage}
            onChange={(e) => handleSetDefaultLandingPage(e.target.value as PageId)}
            aria-label="Select default landing page"
          >
            {PAGES.map((page) => (
              <option key={page.id} value={page.id}>
                {page.title} (Default)
              </option>
            ))}
          </select>
        </div>

        {/* 10 Navigation Items */}
        <nav className="sidebar-nav" role="navigation">
          <div className="nav-section-title">Views</div>
          {PAGES.map((page) => {
            const isActive = activePage === page.id;
            return (
              <button
                key={page.id}
                type="button"
                className={`nav-item ${isActive ? 'active' : ''}`}
                onClick={() => {
                  setActivePage(page.id);
                  if (isMobile) closeMobileNavigation();
                }}
                aria-current={isActive ? 'page' : undefined}
                title={page.description}
              >
                <div className="nav-item-left">
                  <span>{page.title}</span>
                </div>
                <span className="nav-shortcut-badge">{page.shortcut}</span>
              </button>
            );
          })}
        </nav>

        {/* Sidebar Footer with Source Health Indicator */}
        <div className="sidebar-footer">
          <div className="health-status-badge">
            <span
              className={`status-dot ${
                bootstrapError ? 'error' : hasUnresolvedOverlap ? 'warning' : ''
              }`}
            />
            <span>
              {bootstrapError
                ? 'Backend Disconnected'
                : `Loopback Synced (Schema v${bootstrap.schema_version})`}
            </span>
          </div>
          <button
            type="button"
            className="btn-secondary"
            style={{ width: '100%', textAlign: 'center' }}
            onClick={() => {
              if (isMobile) closeMobileNavigation();
              setShowShortcutsModal(true);
            }}
          >
            Keyboard Shortcuts (?)
          </button>
        </div>
      </aside>

      {/* 2. Main Layout Area */}
      <div className="app-main-layout" ref={mainLayoutRef}>
        {/* Topbar Header */}
        <header className="app-topbar">
          <div className="topbar-left">
            <button
              type="button"
              className="btn-secondary mobile-navigation-trigger"
              ref={navigationTriggerRef}
              aria-expanded={mobileNavigationOpen}
              aria-controls="main-navigation"
              onClick={() => setMobileNavigationOpen(true)}
            >
              Menu
            </button>
            <h1 className="current-page-heading">{currentMeta.title}</h1>
            <span style={{ fontSize: '11px', color: 'var(--text-muted)' }}>
              {currentMeta.description}
            </span>
          </div>

          <div className="topbar-right">
            {/* Saved Views Control */}
            <div className="saved-view-controls">
              <button
                type="button"
                className={`btn-secondary metric-definitions-toggle ${showDefinitionsPanel ? 'active' : ''}`}
                onClick={() => setShowDefinitionsPanel((prev) => !prev)}
                aria-expanded={showDefinitionsPanel}
                title="Toggle Metric Definitions Registry"
              >
                Definitions
              </button>
              
              <label htmlFor="saved-view-select" style={{ fontSize: '11px', color: 'var(--text-muted)' }}>
                View:
              </label>
              <select
                id="saved-view-select"
                className="saved-view-select"
                value={selectedViewId}
                onChange={(e) => handleSelectSavedView(e.target.value)}
              >
                <option value="">Default Query State</option>
                {savedViews.map((v) => (
                  <option key={v.view_id} value={v.view_id}>
                    {v.view_name} {v.page_name ? `(${v.page_name})` : ''}
                  </option>
                ))}
              </select>
              <button
                type="button"
                className="btn-secondary"
                onClick={() => {
                  setNewViewName('');
                  setIsDefaultView(false);
                  setIsSavingView(true);
                }}
                title="Save current filters as a named preset"
              >
                Save View
              </button>
            </div>
          </div>
        </header>

        {/* Global Filter Toolbar */}
        <div className="filter-toolbar">
          <div className="filter-group-primary">
            {/* Date Preset Pill */}
            <div className="filter-pill">
              <label htmlFor="filter-preset">Range</label>
              <select
                id="filter-preset"
                value={query.time_range.preset || '30d'}
                onChange={(e) => handlePresetChange(e.target.value as PresetKey)}
              >
                <option value="today">Today</option>
                <option value="7d">Last 7 Days</option>
                <option value="30d">Last 30 Days</option>
                <option value="month-to-date">Month to Date</option>
                <option value="all">All History</option>
                <option value="custom">Custom Boundary</option>
              </select>
            </div>

            {/* Custom Date Inputs if preset is custom */}
            {query.time_range.preset === 'custom' && (
              <>
                <div className="filter-pill">
                  <label htmlFor="custom-start">From</label>
                  <input
                    id="custom-start"
                    type={query.time_range.start?.includes('T') ? 'text' : 'date'}
                    value={query.time_range.start || ''}
                    onChange={(e) =>
                      setQuery({
                        time_range: { ...query.time_range, start: e.target.value || undefined },
                      })
                    }
                  />
                </div>
                <div className="filter-pill">
                  <label htmlFor="custom-end">To</label>
                  <input
                    id="custom-end"
                    type={query.time_range.end?.includes('T') ? 'text' : 'date'}
                    value={query.time_range.end || ''}
                    onChange={(e) =>
                      setQuery({
                        time_range: { ...query.time_range, end: e.target.value || undefined },
                      })
                    }
                  />
                </div>
              </>
            )}

            {/* Timezone Selector */}
            <div className="filter-pill">
              <label htmlFor="filter-tz">TZ</label>
              <select
                id="filter-tz"
                value={query.time_range.timezone}
                onChange={(e) =>
                  setQuery({
                    time_range: {
                      ...query.time_range,
                      timezone: e.target.value,
                    },
                  })
                }
              >
                <option value="UTC">UTC</option>
                <option value="America/New_York">New York (EST/EDT)</option>
                <option value="America/Los_Angeles">Los Angeles (PST/PDT)</option>
                <option value="Europe/London">London (GMT/BST)</option>
                <option value="Asia/Tokyo">Tokyo (JST)</option>
                <option value="Asia/Kolkata">India (IST)</option>
              </select>
            </div>

            {/* Search Substring Filter */}
            <div className="search-input-pill">
              <input
                ref={searchInputRef}
                type="text"
                aria-label="Search sessions and models"
                placeholder="Search sessions/models..."
                value={query.search || ''}
                onChange={(e) => setQuery({ search: e.target.value.trim() ? e.target.value : undefined })}
              />
              <span className="shortcut-hint">/</span>
            </div>
          </div>

          <div className="filter-actions">
            <button
              type="button"
              className={`btn-secondary ${showFiltersDrawer ? 'active' : ''}`}
              onClick={() => setShowFiltersDrawer((prev) => !prev)}
            >
              Filters {activeFilterCount > 0 ? `(${activeFilterCount})` : ''}
            </button>
            {activeFilterCount > 0 && (
              <button
                type="button"
                className="btn-secondary"
                style={{ color: 'var(--accent-rose)' }}
                onClick={() => {
                  setQuery((previous) => ({ ...previous, filters: {}, search: undefined }));
                  setSelectedViewId('');
                  setSavedViewWarning(null);
                }}
              >
                Clear
              </button>
            )}
          </div>
        </div>

        {/* Secondary Filter Drawer for deep dimensions */}
        {savedViewWarning && (
          <div className="banner-alert error" role="alert">
            <span>{savedViewWarning}</span>
          </div>
        )}
        {showFiltersDrawer && (
          <div className="secondary-filter-drawer">
            <div className="input-field-group">
              <label>Model</label>
              <input
                type="text"
                placeholder="e.g. claude-3-5-sonnet"
                value={(query.filters.model || []).join(', ')}
                onChange={(e) => {
                  const parts = e.target.value.split(',').map((value) => value.trim()).filter(Boolean);
                  setQuery({ filters: { ...query.filters, model: parts.length ? parts : undefined } });
                }}
              />
            </div>
            <div className="input-field-group">
              <label>Provider</label>
              <input
                type="text"
                placeholder="e.g. anthropic"
                value={(query.filters.provider || []).join(', ')}
                onChange={(e) => {
                  const parts = e.target.value.split(',').map((value) => value.trim()).filter(Boolean);
                  setQuery({ filters: { ...query.filters, provider: parts.length ? parts : undefined } });
                }}
              />
            </div>
            <div className="input-field-group">
              <label>Project</label>
              <input
                type="text"
                placeholder="Project name/ID or unattributed (NULL)"
                value={(query.filters.project || []).join(', ')}
                onChange={(e) => {
                  const parts = e.target.value.split(',').map((value) => value.trim()).filter(Boolean);
                  setQuery({ filters: { ...query.filters, project: parts.length ? parts : undefined } });
                }}
              />
            </div>
            <div className="input-field-group">
              <label>Session</label>
              <input
                type="text"
                placeholder="Session ID"
                value={(query.filters.session || []).join(', ')}
                onChange={(e) => {
                  const parts = e.target.value.split(',').map((value) => value.trim()).filter(Boolean);
                  setQuery({ filters: { ...query.filters, session: parts.length ? parts : undefined } });
                }}
              />
            </div>
            <div className="input-field-group">
              <label>Access route</label>
              <input
                type="text"
                placeholder="direct, bedrock, openrouter"
                value={(query.filters.route || []).join(', ')}
                onChange={(e) => {
                  const parts = e.target.value.split(',').map((value) => value.trim()).filter(Boolean);
                  setQuery({ filters: { ...query.filters, route: parts.length ? parts : undefined } });
                }}
              />
            </div>
            <div className="input-field-group">
              <label>Task</label>
              <input
                type="text"
                placeholder="debugging, refactoring, feature"
                value={(query.filters.task || []).join(', ')}
                onChange={(e) => {
                  const parts = e.target.value.split(',').map((value) => value.trim()).filter(Boolean);
                  setQuery({ filters: { ...query.filters, task: parts.length ? parts : undefined } });
                }}
              />
            </div>
            <div className="input-field-group">
              <label>Application</label>
              <input
                type="text"
                placeholder="e.g. claude-code, omp"
                value={(query.filters.application || []).join(', ')}
                onChange={(e) => {
                  const parts = e.target.value.split(',').map((value) => value.trim()).filter(Boolean);
                  setQuery({ filters: { ...query.filters, application: parts.length ? parts : undefined } });
                }}
              />
            </div>
            <div className="input-field-group">
              <label>Repository (project alias)</label>
              <input
                type="text"
                placeholder="e.g. token-ledger"
                value={(query.filters.repository || []).join(', ')}
                onChange={(e) => {
                  const parts = e.target.value.split(',').map((value) => value.trim()).filter(Boolean);
                  setQuery({ filters: { ...query.filters, repository: parts.length ? parts : undefined } });
                }}
              />
            </div>
            <div className="input-field-group">
              <label>Subscription fee fact</label>
              <select
                value={(query.filters.subscription || [])[0] || ''}
                onChange={(e) =>
                  setQuery({
                    filters: {
                      ...query.filters,
                      subscription: e.target.value ? [e.target.value] : undefined,
                    },
                  })
                }
              >
                <option value="">Any</option>
                <option value="recorded">Fee fact recorded</option>
                <option value="unrecorded">No fee fact (including unknown)</option>
              </select>
            </div>
            <div className="input-field-group">
              <label>Session duration</label>
              <select
                value={(query.filters.duration || [])[0] || ''}
                onChange={(e) =>
                  setQuery({
                    filters: {
                      ...query.filters,
                      duration: e.target.value ? [e.target.value] : undefined,
                    },
                  })
                }
              >
                <option value="">Any</option>
                <option value="under_1m">Under 1 minute</option>
                <option value="1m_to_5m">1–5 minutes</option>
                <option value="5m_to_30m">5–30 minutes</option>
                <option value="30m_plus">30 minutes or more</option>
                <option value="unknown">Unknown / no session duration</option>
              </select>
            </div>
            <p style={{ gridColumn: '1 / -1', color: 'var(--text-muted)', fontSize: '12px', margin: 0 }}>
              Repository means the project alias. Project <code>unattributed</code> selects NULL project IDs.
              Subscription filters require a recorded fee fact; duration uses session duration.
            </p>
          </div>
        )}

        {/* Global Error Banner */}
        {globalError && (
          <div className="banner-alert error" role="alert">
            <span>{globalError}</span>
            <button
              type="button"
              className="btn-secondary"
              onClick={() => {
                setGlobalError(null);
                fetchBootstrap();
              }}
            >
              Retry Connection
            </button>
          </div>
        )}

        {/* Unresolved Overlap & Warnings Banner */}
        {hasUnresolvedOverlap && (
          <div className="banner-alert warning" role="status">
            <span>
              <strong>Unresolved Overlap Notice:</strong> The active dataset contains overlapping
              or unpartitioned sessions across harnesses. Totals are marked as partial to prevent
              double-counting.
            </span>
          </div>
        )}
        {activeWarnings.length > 0 && !hasUnresolvedOverlap && (
          <div className="banner-alert warning" role="status">
            <span>Query warnings: {activeWarnings.join('; ')}</span>
          </div>
        )}

        {/* 3. Page Content Area & Registered Slot */}
        <div className="content-and-definitions-container">
          <main className="app-content" role="main">
          <Suspense
            fallback={
              <div role="status" style={{ color: 'var(--text-secondary)' }}>
                Loading {currentMeta.title}…
              </div>
            }
          >
            {bootstrapReady ? (
              <SlotComponent context={pageContext} />
            ) : (
              <div role="status" style={{ color: 'var(--text-secondary)' }}>
                {bootstrapError ?? 'Connecting to local Token Ledger service…'}
              </div>
            )}
          </Suspense>
        </main>
          {showDefinitionsPanel && (
            <MetricDefinitionsPanel
              activeFamily={currentMeta.metricFamily}
              pageTitle={currentMeta.title}
              onClose={() => setShowDefinitionsPanel(false)}
            />
          )}
        </div>
      </div>

      {/* 4. Modals (Save View & Keyboard Shortcuts) */}
      {isSavingView && (
        <div className="modal-overlay" role="dialog" aria-modal="true" aria-labelledby="save-view-title">
          <div className="modal-card">
            <div className="modal-header">
              <h3 id="save-view-title">Save Filter View</h3>
              <button
                type="button"
                className="btn-secondary"
                onClick={() => setIsSavingView(false)}
              >
                ✕
              </button>
            </div>
            <form onSubmit={handleSaveViewSubmit}>
              <div className="modal-body">
                <p style={{ color: 'var(--text-secondary)' }}>
                  Save your active harness ({query.harness_scope}), date range, and combined filters
                  as a named preset.
                </p>
                <div className="input-field-group">
                  <label htmlFor="new-view-name">View Name</label>
                  <input
                    id="new-view-name"
                    type="text"
                    required
                    autoFocus
                    placeholder="e.g. October Claude Work"
                    value={newViewName}
                    onChange={(e) => setNewViewName(e.target.value)}
                  />
                </div>
                <label className="default-view-checkbox">
                  <input
                    type="checkbox"
                    checked={isDefaultView}
                    onChange={(event) => setIsDefaultView(event.target.checked)}
                  />
                  <span>Open this view by default</span>
                </label>
              </div>
              <div className="modal-footer">
                <button
                  type="button"
                  className="btn-secondary"
                  onClick={() => setIsSavingView(false)}
                >
                  Cancel
                </button>
                <button type="submit" className="btn-primary">
                  Save Preset
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      {showShortcutsModal && (
        <div className="modal-overlay" role="dialog" aria-modal="true" aria-labelledby="shortcuts-title">
          <div className="modal-card">
            <div className="modal-header">
              <h3 id="shortcuts-title">Keyboard Navigation</h3>
              <button
                type="button"
                className="btn-secondary"
                onClick={() => setShowShortcutsModal(false)}
              >
                ✕
              </button>
            </div>
            <div className="modal-body">
              <div className="shortcuts-grid">
                {PAGES.map((p) => (
                  <div key={p.id} className="shortcut-entry">
                    <span>{p.title}</span>
                    <kbd>{p.shortcut}</kbd>
                  </div>
                ))}
                <div className="shortcut-entry">
                  <span>Focus Search</span>
                  <kbd>/</kbd>
                </div>
                <div className="shortcut-entry">
                  <span>Toggle Shortcuts</span>
                  <kbd>?</kbd>
                </div>
                <div className="shortcut-entry">
                  <span>Close Drawer/Modal</span>
                  <kbd>Esc</kbd>
                </div>
              </div>
            </div>
            <div className="modal-footer">
              <button
                type="button"
                className="btn-primary"
                onClick={() => setShowShortcutsModal(false)}
              >
                Done
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

