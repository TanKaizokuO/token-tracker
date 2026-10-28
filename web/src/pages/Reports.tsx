import React, { useCallback, useEffect, useMemo, useState } from 'react';
import type { PageProps, QueryEnvelope, QueryResponse, SavedView } from '../App';
import './Reports.css';

type ReportFrequency = 'manual' | 'daily' | 'weekly' | 'monthly';
type ExportFormat = 'csv' | 'json' | 'html' | 'pdf' | 'png';
type SettingsTab = 'reports' | 'sources' | 'pricing' | 'privacy' | 'alerts' | 'views';
type BudgetPeriod = 'daily' | 'monthly' | 'session' | 'project_model';
type BudgetTarget = 'tokens' | 'cost_usd' | 'cost_inr';

interface ReportDefinition {
  item_id: string | null;
  item_type: 'report';
  name: string;
  frequency: ReportFrequency;
  filter_state: Record<string, unknown>;
  threshold_config: Record<string, unknown>;
  pricing_assumptions: Record<string, unknown>;
  project_id: string | null;
  account_alias: string;
  last_generated_at: string | null;
  created_at: string | null;
  updated_at: string | null;
}

interface ExportRecord {
  export_id: string;
  export_format: ExportFormat;
  sha256_hash: string;
  byte_size: number;
  query_filter: Record<string, unknown>;
  pricing_fx_assumptions: Record<string, unknown>;
  redaction_applied: boolean;
  created_at: string;
  session_id?: string | null;
  project_id?: string | null;
  account_alias: string;
}

interface SourceCheckpoint {
  source_adapter?: string | null;
  origin_harness?: string | null;
  account_alias?: string | null;
  file_locator?: string | null;
  last_scan_at?: string | null;
  last_success_at?: string | null;
  status?: string | null;
  error_count?: number | null;
  last_error?: string | null;
  file_size?: number | null;
  last_byte_offset?: number | null;
  last_line_index?: number | null;
  newest_record_timestamp?: string | null;
  interval_start?: string | null;
  interval_end?: string | null;
  discovered_count?: number | null;
  parsed_count?: number | null;
  unparseable_lines?: number | null;
  observations_count?: number | null;
  skipped_count?: number | null;
}

interface ArchivePartition {
  partition_id?: string;
  partition_type?: string;
  grain?: string;
  source_name?: string;
  timezone_name?: string;
  start_time?: string | null;
  end_time?: string | null;
  total_tokens?: number | null;
  total_cost_usd?: number | null;
  records_count?: number | null;
  pricing_status?: string | null;
  is_reconciled?: boolean;
  is_live_replaced?: boolean;
}

interface CoverageResponse extends Omit<QueryResponse, 'rows'> {
  checkpoints?: SourceCheckpoint[];
  rows?: ArchivePartition[];
}

interface TimelineBucketRow {
  period?: string;
  bucket_start?: string;
  bucket_end?: string;
  request_count?: number;
  total_tokens?: number;
  api_equivalent_usd?: number | null;
  api_equivalent_inr?: number | null;
  unpriced_requests_count?: number;
  pricing_status?: string | null;
  [key: string]: unknown;
}

interface CoverageTimelinePeriod {
  key: string;
  start: string | null;
  end: string | null;
  label: string;
  coverageType: 'full' | 'archive' | 'partial' | 'missing' | 'unavailable';
  coverageLabel: string;
  detailLevel: string;
  recordsCount: number | null;
  totalTokens: number | null;
  pricingStatus: string;
  isManual?: boolean;
  isUnpriced?: boolean;
  reconciliation: string;
  notes: string;
}


interface PricingFxEvidence {
  fx_rates_available?: number[];
  pricing_version_hashes?: string[];
  [key: string]: unknown;
}

interface SummaryEvidence extends Omit<QueryResponse, 'pricing_fx'> {
  pricing_fx?: PricingFxEvidence;
}

interface PrivacySettings {
  content_capture: boolean;
  content_fingerprints: boolean;
  git_linkage: boolean;
  notifications: boolean;
  policy_version?: string;
  opt_in_versions?: Record<string, string>;
  updated_at?: string | null;
}

interface ProjectPolicy {
  allowed_projects: string[] | null;
  excluded_projects: string[] | null;
  include_patterns: string[] | null;
  exclude_patterns: string[] | null;
}

interface Budget {
  id: string;
  name: string;
  period: BudgetPeriod;
  target_type: BudgetTarget;
  threshold: number;
  scope: Record<string, string>;
  notifications_enabled: boolean;
  project_id?: string | null;
  account_alias: string;
  created_at?: string;
  updated_at?: string;
}

interface SavedLayout {
  layout_id: string;
  layout_name: string;
  is_default: boolean;
  page_name?: string | null;
  layout_state: Record<string, unknown>;
  created_at?: string;
  updated_at?: string;
}

interface HealthResponse {
  status: string;
  version: string;
}

interface Feedback {
  kind: 'success' | 'error' | 'info';
  message: string;
}

const EXPORT_FORMATS: ExportFormat[] = ['csv', 'json', 'html', 'pdf', 'png'];
const SETTINGS_TABS: Array<{ id: SettingsTab; label: string }> = [
  { id: 'reports', label: 'Reports & exports' },
  { id: 'sources', label: 'Source health' },
  { id: 'pricing', label: 'Pricing & FX' },
  { id: 'privacy', label: 'Privacy & retention' },
  { id: 'alerts', label: 'Alerts' },
  { id: 'views', label: 'Saved views & layouts' },
];

const PRIVACY_OPTIONS: Array<{
  key: keyof Pick<PrivacySettings, 'content_capture' | 'content_fingerprints' | 'git_linkage' | 'notifications'>;
  title: string;
  description: string;
  consent: string;
}> = [
  {
    key: 'content_capture',
    title: 'Transcript and tool-content capture',
    description: 'Allows prompt/response text and tool input/output metadata to be captured. Secrets remain redacted, but this can add sensitive content to local storage.',
    consent: 'Enable transcript and tool-content capture? This may persist sensitive conversation or tool content locally. Source files remain read-only.',
  },
  {
    key: 'content_fingerprints',
    title: 'Content fingerprints',
    description: 'Allows SHA-256 content fingerprints for repetition analysis. Equal content produces equal hashes, which can reveal equality and may be vulnerable to dictionary attacks.',
    consent: 'Enable content fingerprints? Hashes can reveal that content is equal and may be vulnerable to dictionary attacks.',
  },
  {
    key: 'git_linkage',
    title: 'Local Git linkage',
    description: 'Allows local commit, branch, and diff-stat linkage to usage records. Git linkage can expose source-control metadata.',
    consent: 'Enable local Git linkage? This permits source-control metadata to be linked to usage records.',
  },
  {
    key: 'notifications',
    title: 'Local alert notifications',
    description: 'Consent gate for optional local notification hooks used by budget rules. Enabling consent does not itself send or schedule a notification.',
    consent: 'Enable consent for local alert notifications? This only opens the notification setting to eligible local rules.',
  },
];

const EMPTY_PROJECT_POLICY: ProjectPolicy = {
  allowed_projects: null,
  excluded_projects: null,
  include_patterns: null,
  exclude_patterns: null,
};

async function getJson<T>(path: string): Promise<T> {
  const response = await fetch(path, { credentials: 'same-origin' });
  const data = (await response.json().catch(() => ({}))) as { error?: string } & T;
  if (!response.ok) {
    throw new Error(data.error || `Request failed with HTTP ${response.status}`);
  }
  return data;
}

function dateLabel(value: string | null | undefined): string {
  if (!value) return 'Never';
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString();
}

function numberLabel(value: unknown): string {
  return typeof value === 'number' && Number.isFinite(value)
    ? new Intl.NumberFormat().format(value)
    : 'Unavailable';
}

function currencyLabel(value: unknown, currency: 'USD' | 'INR'): string {
  if (typeof value !== 'number' || !Number.isFinite(value)) return 'Unavailable';
  return new Intl.NumberFormat(undefined, { style: 'currency', currency, maximumFractionDigits: 4 }).format(value);
}

function querySnapshot(query: QueryEnvelope): Partial<QueryEnvelope> {
  return {
    contract_version: '1',
    harness_scope: query.harness_scope,
    time_range: {
      ...(query.time_range.start ? { start: query.time_range.start } : {}),
      ...(query.time_range.end ? { end: query.time_range.end } : {}),
      timezone: query.time_range.timezone || 'UTC',
    },
    filters: { ...query.filters },
    ...(query.search ? { search: query.search } : {}),
    ...(typeof query.include_archive === 'boolean' ? { include_archive: query.include_archive } : {}),
  };
}

function filterCount(filters: QueryEnvelope['filters']): number {
  return Object.values(filters).filter((value) =>
    Array.isArray(value) ? value.length > 0 : typeof value === 'string' && value.trim().length > 0
  ).length;
}

function textLines(value: string): string[] | null {
  const values = value.split('\n').map((entry) => entry.trim()).filter(Boolean);
  return values.length ? values : null;
}

function stateLines(values: string[] | null): string {
  return values?.join('\n') ?? '';
}

type PricingVersionEntry = {
  version_id: string; effective_start: string; effective_end: string | null;
  currency_code: string; pricing_hash: string; source_provenance: unknown;
  rates: Record<string, unknown>[]; model_mappings: Record<string, unknown>[];
};
type FxEntry = { base_currency: string; target_currency: string; rate_per_usd: number; effective_date: string; source_provenance: string };
type Recalculation = {
  mode: string; persisted: boolean; valuation?: Record<string, unknown>;
  valuations?: Record<string, unknown>[]; selected_count?: number;
  abstentions?: Array<{ canonical_id: string; missing_counters: string[] }>;
};

function PricingManager({ context, onPersist }: PageProps & { onPersist: () => void }) {
  const { query, write } = context;
  const [versions, setVersions] = useState<PricingVersionEntry[]>([]);
  const [fx, setFx] = useState<FxEntry[]>([]);
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');
  const [busy, setBusy] = useState(false);
  const [scope, setScope] = useState<'request' | 'filtered'>('request');
  const [canonicalId, setCanonicalId] = useState('');
  const [versionId, setVersionId] = useState('');
  const [preview, setPreview] = useState<Recalculation | null>(null);

  const load = useCallback(async () => {
    const [v, f] = await Promise.all([
      getJson<{ versions: PricingVersionEntry[] }>('/api/v1/pricing/versions'),
      getJson<{ fx_rates: FxEntry[] }>('/api/v1/pricing/fx'),
    ]);
    setVersions(v.versions);
    setFx(f.fx_rates);
  }, []);
  useEffect(() => { void load().catch((e: unknown) => setError(e instanceof Error ? e.message : 'Unable to load pricing')); }, [load]);
  useEffect(() => { setPreview(null); }, [query, scope, canonicalId, versionId]);

  async function submitVersion(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const text = (name: string) => String(form.get(name) || '').trim();
    const optionalNumber = (name: string) => text(name) === '' ? undefined : Number(text(name));
    setBusy(true); setError(''); setMessage('');
    try {
      const rate: Record<string, unknown> = {
        provider: text('provider'), model_identity: text('model'), access_route: text('route'),
        input_rate_per_m: Number(text('input')), output_rate_per_m: Number(text('output')),
        status: text('status'),
      };
      for (const [name, field] of [['read', 'cache_read_rate_per_m'], ['write5', 'cache_write_5m_rate_per_m'], ['write1h', 'cache_write_1h_rate_per_m']] as const) {
        const value = optionalNumber(name); if (value !== undefined) rate[field] = value;
      }
      if (text('source_url')) rate.source_url = text('source_url');
      if (text('verified_on')) rate.verified_on = text('verified_on');
      if (text('note')) rate.note = text('note');
      if (text('threshold')) {
        rate.is_tiered = true;
        rate.prompt_threshold = Number(text('threshold'));
        rate.tiered_rates_json = JSON.parse(text('tier_rates'));
      }
      const mappings = text('alias') ? [{ provider: text('provider'), access_route: text('route'),
        model_identity: text('model'), alias: text('alias'), ...(text('display') ? { display_name: text('display') } : {}) }] : [];
      await write('pricing/versions', {
        version_id: text('version'), effective_start: text('start'), effective_end: text('end') || null,
        currency_code: text('currency'), source_provenance: { source: text('source') },
        rates: [rate, ...JSON.parse(text('extra_rates') || '[]')],
        model_mappings: [...mappings, ...JSON.parse(text('extra_mappings') || '[]')],
      });
      await load(); setMessage('Immutable pricing version created.');
    } catch (e) { setError(e instanceof Error ? e.message : 'Unable to create pricing version'); }
    finally { setBusy(false); }
  }
  async function submitFx(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    setBusy(true); setError(''); setMessage('');
    try {
      await write('pricing/fx', { base_currency: form.get('base'), target_currency: form.get('target'),
        rate_per_usd: Number(form.get('rate')), effective_date: form.get('date'), source_provenance: form.get('source') });
      await load(); setMessage('Dated FX rate created.');
    } catch (e) { setError(e instanceof Error ? e.message : 'Unable to create FX rate'); }
    finally { setBusy(false); }
  }
  async function recalculate(persist: boolean) {
    if (persist && (!preview || !window.confirm('Persist these derived valuations for the current selection? Missing counters will be skipped. Original usage and billed/subscription facts remain unchanged.'))) return;
    setBusy(true); setError(''); setMessage('');
    if (!persist) setPreview(null);
    try {
      const selection = scope === 'request' ? { canonical_id: canonicalId.trim() } : { query: querySnapshot(query) };
      const result = await write('pricing/recalculate', { ...selection, pricing_version_id: versionId || null, persist }) as Recalculation;
      setPreview(result);
      if (persist) {
        setMessage('Eligible derived valuations persisted. The selection was evaluated again at persistence time.');
        onPersist();
      }
    } catch (e) {
      const detail = e instanceof Error ? e.message : 'Unable to recalculate';
      if (detail.includes('all six usage counters')) { setPreview(null); setMessage(`Abstained: ${detail}`); }
      else setError(detail);
    } finally { setBusy(false); }
  }

  return <div className="reports-stack">
    <section className="reports-card">
      <h2 className="reports-card-title">Pricing manager</h2>
      <p>Versions and mappings are immutable. Create a new version to make a correction. Non-USD cards require a dated direct currency-to-USD rate for valuation.</p>
      {error && <p role="alert" className="reports-banner error">{error}</p>}
      {message && <p role="status" className="reports-banner">{message}</p>}
      <form className="reports-policy-grid" onSubmit={submitVersion}>
        <label className="reports-form-group">Version ID<input name="version" className="reports-input" required /></label>
        <label className="reports-form-group">Effective start (ISO 8601 with timezone)<input name="start" className="reports-input" placeholder="2026-01-01T00:00:00Z" required /></label>
        <label className="reports-form-group">Exclusive effective end (optional)<input name="end" className="reports-input" placeholder="2027-01-01T00:00:00Z" /></label>
        <label className="reports-form-group">Source provenance<input name="source" className="reports-input" required /></label>
        <label className="reports-form-group">Provider<input name="provider" className="reports-input" required /></label>
        <label className="reports-form-group">Rate model identity<input name="model" className="reports-input" required /></label>
        <label className="reports-form-group">Access route<input name="route" className="reports-input" defaultValue="default" required /></label>
        <label className="reports-form-group">Currency<input name="currency" className="reports-input" defaultValue="USD" minLength={3} maxLength={3} required /></label>
        <label className="reports-form-group">Input rate per million<input name="input" className="reports-input" type="number" min="0" step="any" required /></label>
        <label className="reports-form-group">Output rate per million<input name="output" className="reports-input" type="number" min="0" step="any" required /></label>
        <label className="reports-form-group">Cache read rate per million<input name="read" className="reports-input" type="number" min="0" step="any" /></label>
        <label className="reports-form-group">Cache write 5m rate per million<input name="write5" className="reports-input" type="number" min="0" step="any" /></label>
        <label className="reports-form-group">Cache write 1h rate per million<input name="write1h" className="reports-input" type="number" min="0" step="any" /></label>
        <label className="reports-form-group">Status<select name="status" className="reports-input"><option value="unverified">Unverified</option><option value="estimated">Estimated</option><option value="verified">Verified</option></select></label>
        <label className="reports-form-group">Source URL<input name="source_url" className="reports-input" /></label>
        <label className="reports-form-group">Verified on<input name="verified_on" className="reports-input" type="date" /></label>
        <label className="reports-form-group">Rate note<input name="note" className="reports-input" /></label>
        <label className="reports-form-group">Tier prompt threshold<input name="threshold" className="reports-input" type="number" min="0" step="1" /></label>
        <label className="reports-form-group">Tier rates JSON (rate field names per million)<textarea name="tier_rates" className="reports-textarea" defaultValue="{}" /></label>
        <label className="reports-form-group">Model or reasoning alias<input name="alias" className="reports-input" /></label>
        <label className="reports-form-group">Alias display name<input name="display" className="reports-input" /></label>
        <label className="reports-form-group">Additional rates JSON array<textarea name="extra_rates" className="reports-textarea" defaultValue="[]" /></label>
        <label className="reports-form-group">Additional mappings JSON array<textarea name="extra_mappings" className="reports-textarea" defaultValue="[]" /></label>
        <button className="reports-btn" disabled={busy} type="submit">Create version</button>
      </form>
      <h3>Stored versions, rates and mappings</h3>
      {!versions.length && <p>No versions stored.</p>}
      {versions.map((v) => (
        <details key={v.version_id}>
          <summary>{v.version_id} · {v.effective_start} → {v.effective_end || 'open'} · {v.currency_code}</summary>
          <p>Digest: <code>{v.pricing_hash}</code></p>
          {Array.isArray(v.rates) && v.rates.length > 0 && (
            <div className="reports-table-wrap">
              <table className="reports-table">
                <thead>
                  <tr>
                    <th>Provider / Model</th>
                    <th>Route</th>
                    <th>Input / M</th>
                    <th>Output / M</th>
                    <th>Status</th>
                    <th>Source provenance</th>
                  </tr>
                </thead>
                <tbody>
                  {v.rates.map((rate, rIdx) => {
                    const isUnpriced = (rate.input_rate_per_m === 0 && rate.output_rate_per_m === 0) || rate.status === 'unpriced';
                    const isManual = rate.status === 'manual' || (typeof rate.source_provenance === 'string' && rate.source_provenance.includes('manual')) || (typeof rate.note === 'string' && rate.note.includes('manual'));
                    return (
                      <tr key={`${rate.provider}-${rate.model_identity}-${rIdx}`}>
                        <td><strong>{String(rate.model_identity || 'Unavailable')}</strong><span className="reports-cell-sub">{String(rate.provider || 'Unavailable')}</span></td>
                        <td>{String(rate.access_route || 'default')}</td>
                        <td>{numberLabel(rate.input_rate_per_m)} {v.currency_code}</td>
                        <td>{numberLabel(rate.output_rate_per_m)} {v.currency_code}</td>
                        <td>
                          {isUnpriced ? (
                            <span className="reports-status-pill danger">Unpriced</span>
                          ) : isManual ? (
                            <span className="reports-status-pill accent">Manual</span>
                          ) : rate.status === 'verified' ? (
                            <span className="reports-status-pill success">Verified</span>
                          ) : rate.status === 'estimated' ? (
                            <span className="reports-status-pill warning">Estimated</span>
                          ) : (
                            <span className="reports-status-pill muted">{String(rate.status || 'Unavailable')}</span>
                          )}
                        </td>
                        <td>{String(rate.source_url || rate.note || 'Recorded provenance')}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
          <pre>{JSON.stringify({ source_provenance: v.source_provenance, rates: v.rates, model_mappings: v.model_mappings }, null, 2)}</pre>
        </details>
      ))}
    </section>
    <section className="reports-card">
      <h2 className="reports-card-title">Dated FX</h2>
      <p>Rates are target units per base unit. Valuation uses direct card-currency/USD and USD/INR rates on or before the request date.</p>
      <form className="reports-policy-grid" onSubmit={submitFx}>
        <label className="reports-form-group">Base currency<input name="base" className="reports-input" defaultValue="USD" required /></label>
        <label className="reports-form-group">Target currency<input name="target" className="reports-input" defaultValue="INR" required /></label>
        <label className="reports-form-group">Target units per base<input name="rate" className="reports-input" type="number" min="0.00000001" step="any" required /></label>
        <label className="reports-form-group">Effective date<input name="date" className="reports-input" type="date" required /></label>
        <label className="reports-form-group">FX source<input name="source" className="reports-input" required /></label>
        <button className="reports-btn" disabled={busy} type="submit">Create FX rate</button>
      </form>
      <ul>{fx.map((item) => <li key={`${item.base_currency}-${item.target_currency}-${item.effective_date}`}>{item.effective_date}: {item.base_currency}/{item.target_currency} {item.rate_per_usd} · {item.source_provenance}</li>)}</ul>
    </section>
    <section className="reports-card">
      <h2 className="reports-card-title">Preview and recalculate</h2>
      <p>Historical selects the rate card effective at each request date; a selected version is an explicit what-if, even outside its effective window. Filtered scope uses the current shared query for canonical requests only, not archive aggregates. Preview never writes.</p>
      <label className="reports-form-group">Scope<select className="reports-input" value={scope} onChange={(e) => setScope(e.target.value as 'request' | 'filtered')}><option value="request">Single request</option><option value="filtered">Current filtered requests</option></select></label>
      {scope === 'request' && <label className="reports-form-group">Canonical request ID<input className="reports-input" value={canonicalId} onChange={(e) => setCanonicalId(e.target.value)} /></label>}
      <label className="reports-form-group">Valuation version<select className="reports-input" value={versionId} onChange={(e) => setVersionId(e.target.value)}><option value="">Historical at request date</option>{versions.map((v) => <option key={v.version_id} value={v.version_id}>{v.version_id}</option>)}</select></label>
      <button className="reports-btn" disabled={busy || (scope === 'request' && !canonicalId.trim())} onClick={() => void recalculate(false)}>Preview valuation</button>
      <button className="reports-btn" disabled={busy || !preview} onClick={() => void recalculate(true)}>Persist recalculation</button>
      {preview && <div><h3>{preview.persisted ? 'Persisted result' : 'Preview result'}</h3>
        {preview.selected_count !== undefined && <p>{preview.selected_count} selected · {preview.valuations?.length ?? 0} valued · {preview.abstentions?.length ?? 0} abstained</p>}
        {preview.abstentions?.map((a) => <p key={a.canonical_id}>Abstained {a.canonical_id}: missing {a.missing_counters.join(', ')}</p>)}
        {Array.isArray(preview.valuations) && preview.valuations.length > 0 && (
          <div className="reports-table-wrap">
            <table className="reports-table">
              <thead>
                <tr>
                  <th>Canonical ID</th>
                  <th>Pricing status</th>
                  <th>API USD</th>
                  <th>API INR</th>
                  <th>FX rate</th>
                </tr>
              </thead>
              <tbody>
                {preview.valuations.map((val, idx) => {
                  const pStatus = String(val.pricing_status || 'unpriced');
                  const isUnpriced = pStatus === 'unpriced';
                  const isManual = pStatus === 'manual';
                  return (
                    <tr key={String(val.canonical_id || idx)}>
                      <td><code>{String(val.canonical_id || 'Unavailable')}</code></td>
                      <td>
                        {isUnpriced ? (
                          <span className="reports-status-pill danger">Unpriced</span>
                        ) : isManual ? (
                          <span className="reports-status-pill accent">Manual</span>
                        ) : pStatus === 'priced' ? (
                          <span className="reports-status-pill success">Priced</span>
                        ) : (
                          <span className="reports-status-pill warning">{pStatus}</span>
                        )}
                      </td>
                      <td>{currencyLabel(val.api_equivalent_usd, 'USD')}</td>
                      <td>{currencyLabel(val.api_equivalent_inr, 'INR')}</td>
                      <td>{numberLabel(val.fx_rate_applied)}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
        <pre>{JSON.stringify(preview.valuation || preview.valuations, null, 2)}</pre>
      </div>}
    </section>
  </div>;
}

export default function Reports({ context }: PageProps) {
  const { query, setQuery, request, write, bootstrap } = context;
  const [activeTab, setActiveTab] = useState<SettingsTab>('reports');
  const [feedback, setFeedback] = useState<Feedback | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [reloadVersion, setReloadVersion] = useState(0);
  const [reports, setReports] = useState<ReportDefinition[]>([]);
  const [dueReports, setDueReports] = useState<ReportDefinition[]>([]);
  const [exports, setExports] = useState<ExportRecord[]>([]);
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [privacySettings, setPrivacySettings] = useState<PrivacySettings | null>(null);
  const [projectPolicy, setProjectPolicy] = useState<ProjectPolicy>(EMPTY_PROJECT_POLICY);
  const [retentionDays, setRetentionDays] = useState<number | null>(null);
  const [retentionInput, setRetentionInput] = useState('');
  const [retentionResult, setRetentionResult] = useState<Record<string, unknown> | null>(null);
  const [budgets, setBudgets] = useState<Budget[]>([]);
  const [savedViews, setSavedViews] = useState<SavedView[]>([]);
  const [savedLayouts, setSavedLayouts] = useState<SavedLayout[]>([]);
  const [coverage, setCoverage] = useState<CoverageResponse | null>(null);
  const [summary, setSummary] = useState<SummaryEvidence | null>(null);
  const [queryError, setQueryError] = useState<string | null>(null);
  const [timelineRows, setTimelineRows] = useState<TimelineBucketRow[]>([]);
  const [selectedFormats, setSelectedFormats] = useState<ExportFormat[]>(['json']);

  const [reportName, setReportName] = useState('');
  const [reportFrequency, setReportFrequency] = useState<ReportFrequency>('manual');
  const [reportAccountAlias, setReportAccountAlias] = useState('default');

  const [allowedProjectsInput, setAllowedProjectsInput] = useState('');
  const [excludedProjectsInput, setExcludedProjectsInput] = useState('');
  const [includePatternsInput, setIncludePatternsInput] = useState('');
  const [excludePatternsInput, setExcludePatternsInput] = useState('');

  const [budgetName, setBudgetName] = useState('');
  const [budgetPeriod, setBudgetPeriod] = useState<BudgetPeriod>('monthly');
  const [budgetTarget, setBudgetTarget] = useState<BudgetTarget>('tokens');
  const [budgetThreshold, setBudgetThreshold] = useState('');
  const [budgetProject, setBudgetProject] = useState('');
  const [budgetModel, setBudgetModel] = useState('');
  const [budgetNotifications, setBudgetNotifications] = useState(false);

  const [viewName, setViewName] = useState('');
  const [layoutName, setLayoutName] = useState('');
  const [layoutSection, setLayoutSection] = useState<SettingsTab>('reports');
  const [layoutDefault, setLayoutDefault] = useState(false);

  const currentFilterCount = useMemo(() => filterCount(query.filters), [query.filters]);
  const rangeLabel = `${query.time_range.start || 'Earliest'} → ${query.time_range.end || 'Latest'} (${query.time_range.timezone || 'UTC'})`;

  const loadResources = useCallback(async () => {
    setLoading(true);
    try {
      const [reportResponse, exportResponse, dueResponse, healthResponse, privacyResponse, policyResponse, retentionResponse, budgetResponse, viewResponse, layoutResponse] = await Promise.all([
        getJson<{ reports: ReportDefinition[] }>('/api/v1/reports/definitions'),
        getJson<{ exports: ExportRecord[] }>('/api/v1/exports?limit=100'),
        getJson<{ due_reports: ReportDefinition[] }>('/api/v1/reports/scheduler/due'),
        getJson<HealthResponse>('/api/v1/health'),
        getJson<{ settings: PrivacySettings }>('/api/v1/privacy'),
        getJson<{ policy: ProjectPolicy }>('/api/v1/privacy/projects'),
        getJson<{ retention_days: number | null }>('/api/v1/privacy/retention'),
        getJson<{ budgets: Budget[] }>('/api/v1/budgets'),
        getJson<{ saved_views: SavedView[] }>('/api/v1/saved-views'),
        getJson<{ layouts: SavedLayout[] }>('/api/v1/layouts'),
      ]);
      setReports(reportResponse.reports || []);
      setExports(exportResponse.exports || []);
      setDueReports(dueResponse.due_reports || []);
      setHealth(healthResponse);
      setPrivacySettings(privacyResponse.settings);
      setProjectPolicy(policyResponse.policy || EMPTY_PROJECT_POLICY);
      setAllowedProjectsInput(stateLines(policyResponse.policy?.allowed_projects ?? null));
      setExcludedProjectsInput(stateLines(policyResponse.policy?.excluded_projects ?? null));
      setIncludePatternsInput(stateLines(policyResponse.policy?.include_patterns ?? null));
      setExcludePatternsInput(stateLines(policyResponse.policy?.exclude_patterns ?? null));
      setRetentionDays(retentionResponse.retention_days);
      setRetentionInput(retentionResponse.retention_days == null ? '' : String(retentionResponse.retention_days));
      setBudgets(budgetResponse.budgets || []);
      setSavedViews(viewResponse.saved_views || []);
      setSavedLayouts((layoutResponse.layouts || []).filter((layout) => !layout.page_name || layout.page_name === 'reports'));
      setFeedback((current) => current?.kind === 'error' ? current : null);
    } catch (error) {
      setFeedback({ kind: 'error', message: error instanceof Error ? error.message : 'Unable to load Reports & Settings resources.' });
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadResources();
  }, [loadResources, reloadVersion]);

  useEffect(() => {
    let active = true;
    setQueryError(null);
    Promise.all([
      request('coverage'),
      request('summary'),
      request('timeline', { group_by: ['day'] }).catch(() => null),
    ]).then(([coverageResponse, summaryResponse, timelineResponse]) => {
      if (!active) return;
      if (coverageResponse.error || summaryResponse.error) {
        setQueryError(coverageResponse.error || summaryResponse.error || 'Unable to load shared query evidence.');
        setCoverage(null);
        setSummary(null);
        setTimelineRows([]);
        return;
      }
      setCoverage(coverageResponse as CoverageResponse);
      setSummary(summaryResponse as SummaryEvidence);
      if (timelineResponse && Array.isArray((timelineResponse as QueryResponse).rows)) {
        setTimelineRows((timelineResponse as QueryResponse).rows as TimelineBucketRow[]);
      } else {
        setTimelineRows([]);
      }
    }).catch((error: unknown) => {
      if (active) {
        setQueryError(error instanceof Error ? error.message : 'Unable to load shared query evidence.');
        setTimelineRows([]);
      }
    });
    return () => { active = false; };
  }, [query, request, reloadVersion]);

  const refresh = useCallback(() => setReloadVersion((version) => version + 1), []);

  async function handleCreateReport(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy('create-report');
    setFeedback(null);
    const snapshot = querySnapshot(query);
    const frequency = reportFrequency;
    try {
      await write('reports/definitions', {
        name: reportName.trim(),
        frequency,
        filter_state: {
          query: { ...snapshot, metric_id: 'summary' },
          schedule_timezone: snapshot.time_range?.timezone || 'UTC',
        },
        threshold_config: {},
        pricing_assumptions: {},
        project_id: null,
        account_alias: reportAccountAlias.trim() || 'default',
      }, 'POST');
      setReportName('');
      setFeedback({ kind: 'success', message: `Saved ${frequency} report with the current shared filters.` });
      await loadResources();
    } catch (error) {
      setFeedback({ kind: 'error', message: error instanceof Error ? error.message : 'Could not save report definition.' });
    } finally {
      setBusy(null);
    }
  }

  async function handleDeleteReport(report: ReportDefinition) {
    if (!report.item_id || !window.confirm(`Delete report “${report.name}”? This removes its definition; generated exports remain in the inventory.`)) return;
    setBusy(`delete-report-${report.item_id}`);
    setFeedback(null);
    try {
      await write(`reports/definitions/${encodeURIComponent(report.item_id)}`, {}, 'DELETE');
      setFeedback({ kind: 'success', message: `Deleted report “${report.name}”.` });
      await loadResources();
    } catch (error) {
      setFeedback({ kind: 'error', message: error instanceof Error ? error.message : 'Could not delete report.' });
    } finally {
      setBusy(null);
    }
  }

  async function handleGenerate(report: ReportDefinition) {
    if (!report.item_id || selectedFormats.length === 0) return;
    setBusy(`generate-${report.item_id}`);
    setFeedback(null);
    try {
      await write('reports/generate', { report_id: report.item_id, formats: selectedFormats }, 'POST');
      setFeedback({ kind: 'success', message: `Generated ${selectedFormats.join(', ').toUpperCase()} for “${report.name}”.` });
      await loadResources();
    } catch (error) {
      setFeedback({ kind: 'error', message: error instanceof Error ? error.message : 'Could not generate report.' });
    } finally {
      setBusy(null);
    }
  }

  async function handleRunDue() {
    setBusy('run-due');
    setFeedback(null);
    try {
      const response = await write('reports/scheduler/run-due', {}, 'POST') as { count?: number };
      setFeedback({ kind: 'success', message: `Local scheduler completed ${numberLabel(response.count ?? 0)} due report(s).` });
      await loadResources();
    } catch (error) {
      setFeedback({ kind: 'error', message: error instanceof Error ? error.message : 'Could not run due reports.' });
    } finally {
      setBusy(null);
    }
  }

  async function handlePrivacyChange(key: typeof PRIVACY_OPTIONS[number]['key'], enabled: boolean, consent: string) {
    if (enabled && !window.confirm(consent)) return;
    setBusy(`privacy-${key}`);
    setFeedback(null);
    try {
      const response = await write('privacy', { setting_key: key, opt_in_enabled: enabled }, 'POST') as { settings: PrivacySettings };
      setPrivacySettings(response.settings);
      setFeedback({ kind: 'success', message: `${key.replaceAll('_', ' ')} consent ${enabled ? 'enabled' : 'disabled'}.` });
    } catch (error) {
      setFeedback({ kind: 'error', message: error instanceof Error ? error.message : 'Could not update privacy consent.' });
    } finally {
      setBusy(null);
    }
  }

  async function handleSaveProjectPolicy(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy('project-policy');
    setFeedback(null);
    const payload: ProjectPolicy = {
      allowed_projects: textLines(allowedProjectsInput),
      excluded_projects: textLines(excludedProjectsInput),
      include_patterns: textLines(includePatternsInput),
      exclude_patterns: textLines(excludePatternsInput),
    };
    try {
      const response = await write('privacy/projects', payload, 'POST') as { policy: ProjectPolicy };
      setProjectPolicy(response.policy);
      setFeedback({ kind: 'success', message: 'Saved project inclusion and exclusion policy.' });
    } catch (error) {
      setFeedback({ kind: 'error', message: error instanceof Error ? error.message : 'Could not save project policy.' });
    } finally {
      setBusy(null);
    }
  }

  async function handleSaveRetention(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const days = Number(retentionInput);
    if (!Number.isInteger(days) || days < 1 || days > 36500) {
      setFeedback({ kind: 'error', message: 'Retention must be a whole number from 1 to 36,500 days.' });
      return;
    }
    setBusy('retention-save');
    setFeedback(null);
    try {
      const response = await write('privacy/retention', { retention_days: days }, 'POST') as { retention_days: number };
      setRetentionDays(response.retention_days);
      setFeedback({ kind: 'success', message: `Saved a ${response.retention_days}-day local retention period.` });
    } catch (error) {
      setFeedback({ kind: 'error', message: error instanceof Error ? error.message : 'Could not save retention period.' });
    } finally {
      setBusy(null);
    }
  }

  async function handleRunRetention() {
    if (!retentionDays || !window.confirm(`Run retention now? This permanently removes Token Ledger managed records and generated snapshots older than ${retentionDays} days. Source log files are read-only and will not be changed.`)) return;
    setBusy('retention-run');
    setFeedback(null);
    try {
      const response = await write('privacy/retention/run', {}, 'POST') as { result?: Record<string, unknown> };
      setRetentionResult(response.result || {});
      setFeedback({ kind: 'success', message: 'Retention completed for local managed data.' });
      await loadResources();
    } catch (error) {
      setFeedback({ kind: 'error', message: error instanceof Error ? error.message : 'Could not run retention.' });
    } finally {
      setBusy(null);
    }
  }

  async function handleCreateBudget(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy('budget-save');
    setFeedback(null);
    const scope: Record<string, string> = {};
    if (budgetProject.trim()) scope.project_id = budgetProject.trim();
    if (budgetModel.trim()) scope.model = budgetModel.trim();
    try {
      await write('budgets', {
        name: budgetName.trim(),
        period: budgetPeriod,
        target_type: budgetTarget,
        threshold: Number(budgetThreshold),
        scope,
        notifications_enabled: budgetNotifications,
        account_alias: 'default',
      }, 'POST');
      setBudgetName('');
      setBudgetThreshold('');
      setBudgetProject('');
      setBudgetModel('');
      setBudgetNotifications(false);
      setFeedback({ kind: 'success', message: 'Saved local budget rule.' });
      await loadResources();
    } catch (error) {
      setFeedback({ kind: 'error', message: error instanceof Error ? error.message : 'Could not save budget rule.' });
    } finally {
      setBusy(null);
    }
  }

  async function handleDeleteBudget(budget: Budget) {
    if (!window.confirm(`Delete budget rule “${budget.name}”?`)) return;
    setBusy(`delete-budget-${budget.id}`);
    setFeedback(null);
    try {
      await write(`budgets/${encodeURIComponent(budget.id)}`, {}, 'DELETE');
      setFeedback({ kind: 'success', message: `Deleted budget rule “${budget.name}”.` });
      await loadResources();
    } catch (error) {
      setFeedback({ kind: 'error', message: error instanceof Error ? error.message : 'Could not delete budget rule.' });
    } finally {
      setBusy(null);
    }
  }

  async function handleSaveView(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy('view-save');
    setFeedback(null);
    try {
      await write('saved-views', {
        view_name: viewName.trim(),
        is_default: false,
        page_name: 'reports',
        filter_state: querySnapshot(query),
      }, 'POST');
      setViewName('');
      setFeedback({ kind: 'success', message: 'Saved this shared filter state as a local view.' });
      await loadResources();
    } catch (error) {
      setFeedback({ kind: 'error', message: error instanceof Error ? error.message : 'Could not save view.' });
    } finally {
      setBusy(null);
    }
  }

  function handleApplyView(view: SavedView) {
    const state = view.filter_state || {};
    const harness = ['combined', 'claude-code', 'omp'].includes(String(state.harness_scope))
      ? state.harness_scope as QueryEnvelope['harness_scope']
      : query.harness_scope;
    setQuery((previous) => ({
      ...previous,
      contract_version: '1',
      harness_scope: harness,
      filters: { ...(state.filters || {}) },
      time_range: {
        timezone: state.time_range?.timezone || previous.time_range.timezone,
        ...(state.time_range?.start ? { start: state.time_range.start } : {}),
        ...(state.time_range?.end ? { end: state.time_range.end } : {}),
      },
      search: state.search,
      include_archive: typeof state.include_archive === 'boolean' ? state.include_archive : previous.include_archive,
    }));
    setFeedback({ kind: 'success', message: `Applied saved view “${view.view_name}” to the shared dashboard filters.` });
  }

  async function handleDeleteView(view: SavedView) {
    if (!window.confirm(`Delete saved view “${view.view_name}”?`)) return;
    setBusy(`delete-view-${view.view_id}`);
    setFeedback(null);
    try {
      await write(`saved-views/${encodeURIComponent(view.view_id)}`, {}, 'DELETE');
      setFeedback({ kind: 'success', message: `Deleted saved view “${view.view_name}”.` });
      await loadResources();
    } catch (error) {
      setFeedback({ kind: 'error', message: error instanceof Error ? error.message : 'Could not delete saved view.' });
    } finally {
      setBusy(null);
    }
  }

  async function handleSaveLayout(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy('layout-save');
    setFeedback(null);
    const snapshot = querySnapshot(query);
    try {
      await write('layouts', {
        layout_name: layoutName.trim(),
        is_default: layoutDefault,
        page_name: 'reports',
        layout_state: {
          version: 1,
          page_name: 'reports',
          updated_at: new Date().toISOString(),
          global_query: snapshot,
          widgets: [{ id: 'reports-workspace', title: 'Reports workspace', category: 'Reports', colSpan: 12, order: 0, visible: true, active_tab: layoutSection }],
        },
      }, 'POST');
      setLayoutName('');
      setFeedback({ kind: 'success', message: `Saved the Reports workspace to open on “${SETTINGS_TABS.find((tab) => tab.id === layoutSection)?.label}” with the current shared filters.` });
      await loadResources();
    } catch (error) {
      setFeedback({ kind: 'error', message: error instanceof Error ? error.message : 'Could not save workspace layout.' });
    } finally {
      setBusy(null);
    }
  }

  function handleApplyLayout(layout: SavedLayout) {
    const state = layout.layout_state || {};
    const widgets = Array.isArray(state.widgets) ? state.widgets as Array<Record<string, unknown>> : [];
    const tabId = widgets[0]?.active_tab;
    if (typeof tabId === 'string' && SETTINGS_TABS.some((tab) => tab.id === tabId)) {
      setActiveTab(tabId as SettingsTab);
    }
    const savedQuery = state.global_query as Partial<QueryEnvelope> | undefined;
    if (savedQuery) {
      setQuery((previous) => ({
        ...previous,
        ...savedQuery,
        contract_version: '1',
        filters: savedQuery.filters ? { ...savedQuery.filters } : previous.filters,
        time_range: { ...previous.time_range, ...savedQuery.time_range },
      }));
    }
    setFeedback({ kind: 'success', message: `Applied Reports workspace “${layout.layout_name}”.` });
  }

  async function handleDeleteLayout(layout: SavedLayout) {
    if (!window.confirm(`Delete saved workspace “${layout.layout_name}”?`)) return;
    setBusy(`delete-layout-${layout.layout_id}`);
    setFeedback(null);
    try {
      await write(`layouts/${encodeURIComponent(layout.layout_id)}`, {}, 'DELETE');
      setFeedback({ kind: 'success', message: `Deleted saved workspace “${layout.layout_name}”.` });
      await loadResources();
    } catch (error) {
      setFeedback({ kind: 'error', message: error instanceof Error ? error.message : 'Could not delete workspace.' });
    } finally {
      setBusy(null);
    }
  }

  const pricingFx = summary?.pricing_fx as PricingFxEvidence | undefined;
  const values = summary?.values || {};
  const missingness = summary?.missingness || {};
  const coverageValues = coverage?.values || {};
  const checkpoints = coverage?.checkpoints || [];
  const missingAdapters = [
    { name: 'claude_code', harness: 'claude-code' },
    { name: 'omp', harness: 'omp' },
  ].filter((adapter) => !checkpoints.some((checkpoint) =>
    checkpoint.origin_harness === adapter.harness
    || checkpoint.source_adapter === adapter.name
    || (adapter.name === 'omp' && checkpoint.origin_harness === 'oh-my-pi'),
  ));
  const archivePartitions = coverage?.rows || [];

  const coverageTimeline = useMemo<CoverageTimelinePeriod[]>(() => {
    if (queryError || !coverage) {
      return [];
    }

    const periods: CoverageTimelinePeriod[] = [];

    for (const partition of archivePartitions) {
      const isReplaced = Boolean(partition.is_live_replaced);
      const isReconciled = Boolean(partition.is_reconciled);
      const pricing = partition.pricing_status || 'Unavailable';
      const isUnpriced = pricing.toLowerCase() === 'unpriced';
      const isManual = pricing.toLowerCase() === 'manual';

      periods.push({
        key: `arch-${partition.partition_id || partition.start_time || Math.random()}`,
        start: partition.start_time || null,
        end: partition.end_time || null,
        label: partition.start_time ? `${dateLabel(partition.start_time)} → ${dateLabel(partition.end_time)}` : 'Archive interval unavailable',
        coverageType: isReplaced ? 'full' : isReconciled ? 'archive' : 'partial',
        coverageLabel: isReplaced ? 'Full request detail (live replaced)' : isReconciled ? 'Aggregate-only history (reconciled)' : 'Aggregate-only history (unreconciled)',
        detailLevel: isReplaced ? 'Full canonical request detail' : 'Aggregate only; request detail not implied',
        recordsCount: partition.records_count ?? null,
        totalTokens: partition.total_tokens ?? null,
        pricingStatus: isUnpriced ? 'Unpriced' : isManual ? 'Manual' : pricing,
        isManual,
        isUnpriced,
        reconciliation: isReplaced ? 'Live replaced' : isReconciled ? 'Reconciled' : 'Unreconciled',
        notes: isReplaced ? 'Replaced by verified live request-level coverage.' : 'Imported archive aggregate partition; raw request details are unavailable.',
      });
    }

    for (const row of timelineRows) {
      if (!row.request_count && !row.total_tokens) continue;
      const count = row.request_count ?? 0;
      const unpriced = row.unpriced_requests_count ?? 0;
      const isUnpriced = count > 0 && unpriced >= count;
      const isPartialPricing = count > 0 && unpriced > 0 && unpriced < count;
      const pricing = isUnpriced ? 'Unpriced' : isPartialPricing ? 'Partial priced' : 'Priced';

      periods.push({
        key: `live-${row.period || row.bucket_start || Math.random()}`,
        start: row.bucket_start || row.period || null,
        end: row.bucket_end || null,
        label: row.period ? row.period : row.bucket_start ? `${dateLabel(row.bucket_start)} → ${dateLabel(row.bucket_end)}` : 'Period unavailable',
        coverageType: 'full',
        coverageLabel: 'Full request detail (live)',
        detailLevel: 'Full canonical request detail',
        recordsCount: count,
        totalTokens: row.total_tokens ?? null,
        pricingStatus: pricing,
        isUnpriced,
        reconciliation: 'N/A (Canonical live)',
        notes: 'Observed from canonical ingestion with request-level events and timing.',
      });
    }

    if (periods.length === 0 && coverageValues.live_requests_count) {
      periods.push({
        key: 'live-overall-span',
        start: (coverageValues.live_earliest_utc as string | null) || null,
        end: (coverageValues.live_latest_utc as string | null) || null,
        label: `${dateLabel(String(coverageValues.live_earliest_utc || ''))} → ${dateLabel(String(coverageValues.live_latest_utc || ''))}`,
        coverageType: 'full',
        coverageLabel: 'Full request detail (live span)',
        detailLevel: 'Full canonical request detail',
        recordsCount: coverageValues.live_requests_count as number,
        totalTokens: null,
        pricingStatus: values.api_equivalent_usd != null ? 'Priced' : 'Unpriced',
        isUnpriced: values.api_equivalent_usd == null,
        reconciliation: 'N/A (Canonical live)',
        notes: 'Observed canonical live request span; full request records available.',
      });
    }

    periods.sort((a, b) => {
      const ta = a.start ? new Date(a.start).getTime() : 0;
      const tb = b.start ? new Date(b.start).getTime() : 0;
      return ta - tb;
    });

    const withGaps: CoverageTimelinePeriod[] = [];
    for (let i = 0; i < periods.length; i++) {
      if (i > 0) {
        const prev = periods[i - 1];
        const curr = periods[i];
        if (prev.end && curr.start) {
          const prevEndTime = new Date(prev.end).getTime();
          const currStartTime = new Date(curr.start).getTime();
          if (!Number.isNaN(prevEndTime) && !Number.isNaN(currStartTime) && currStartTime - prevEndTime > 25 * 3600 * 1000) {
            withGaps.push({
              key: `gap-${prev.end}-${curr.start}`,
              start: prev.end,
              end: curr.start,
              label: `${dateLabel(prev.end)} → ${dateLabel(curr.start)}`,
              coverageType: 'missing',
              coverageLabel: 'Missing data (interval gap)',
              detailLevel: 'Data gap (not a usage decline)',
              recordsCount: 0,
              totalTokens: 0,
              pricingStatus: 'Unavailable',
              reconciliation: 'None (Uncovered)',
              notes: 'Interval gap with no recorded observations. This reflects missing collection, not zero usage.',
            });
          }
        }
      }
      withGaps.push(periods[i]);
    }

    return withGaps;
  }, [coverage, queryError, archivePartitions, timelineRows, coverageValues, values]);

  return (
    <main className="reports-container">
      <header className="reports-header">
        <div>
          <p className="reports-kicker">LOCAL WORKSPACE / REPORTING</p>
          <h1 className="reports-header-title">Reports &amp; Settings</h1>
          <p className="reports-header-subtitle">Build redacted reports from the shared dashboard query, manage their local exports, and review real source and privacy state.</p>
        </div>
        <div className="reports-header-meta">
          <span className={`reports-status-pill ${health?.status === 'ok' ? 'success' : 'muted'}`}>
            {health ? `API ${health.status} · v${health.version}` : loading ? 'Checking local API…' : 'API unavailable'}
          </span>
          <span className="reports-status-pill muted">Schema {bootstrap.schema_version}</span>
          <button type="button" className="reports-btn reports-btn-secondary" onClick={refresh} disabled={loading || busy !== null}>Refresh</button>
        </div>
      </header>

      <section className="reports-context" aria-label="Current shared dashboard query">
        <div>
          <span className="reports-context-label">Shared dashboard context</span>
          <strong>{query.harness_scope}</strong>
        </div>
        <div>
          <span className="reports-context-label">Time range</span>
          <strong>{rangeLabel}</strong>
        </div>
        <div>
          <span className="reports-context-label">Active filters</span>
          <strong>{currentFilterCount}</strong>
        </div>
        <p>New report definitions and saved views snapshot this context. Creating them does not change the dashboard filters.</p>
      </section>

      {feedback && (
        <div className={`reports-banner ${feedback.kind}`} role={feedback.kind === 'error' ? 'alert' : 'status'}>
          <span>{feedback.message}</span>
          <button type="button" className="reports-dismiss" onClick={() => setFeedback(null)} aria-label="Dismiss message">×</button>
        </div>
      )}

      <nav className="reports-tabs" role="tablist" aria-label="Reports and settings sections">
        {SETTINGS_TABS.map((tab) => (
          <button key={tab.id} type="button" role="tab" aria-selected={activeTab === tab.id} className={`reports-tab-btn ${activeTab === tab.id ? 'active' : ''}`} onClick={() => setActiveTab(tab.id)}>
            {tab.label}
            {tab.id === 'reports' && reports.length > 0 && <span className="reports-tab-count">{reports.length}</span>}
            {tab.id === 'alerts' && budgets.length > 0 && <span className="reports-tab-count">{budgets.length}</span>}
          </button>
        ))}
      </nav>

      {loading && <p className="reports-loading" role="status">Loading local report and settings resources…</p>}

      {activeTab === 'reports' && (
        <div className="reports-stack">
          <section className="reports-card">
            <div className="reports-section-heading">
              <div>
                <p className="reports-eyebrow">DEFINITIONS</p>
                <h2 className="reports-card-title">Report schedule</h2>
                <p className="reports-card-description">Definitions run through the shared query engine. Daily, weekly, and monthly schedules are local; this page does not create a background worker.</p>
              </div>
              <button type="button" className="reports-btn" onClick={handleRunDue} disabled={busy !== null || dueReports.length === 0}>
                {busy === 'run-due' ? 'Running…' : `Run due locally${dueReports.length ? ` · ${dueReports.length}` : ''}`}
              </button>
            </div>
            {dueReports.length > 0 && (
              <p className="reports-notice">Due now: {dueReports.map((report) => report.name).join(' · ')}</p>
            )}
            <form className="reports-form reports-form-inline" onSubmit={handleCreateReport}>
              <label className="reports-form-group reports-grow">
                <span className="reports-form-label">Report name</span>
                <input className="reports-input" value={reportName} onChange={(event) => setReportName(event.target.value)} required maxLength={256} placeholder="e.g. Weekly usage review" />
              </label>
              <label className="reports-form-group">
                <span className="reports-form-label">Schedule</span>
                <select className="reports-select" value={reportFrequency} onChange={(event) => setReportFrequency(event.target.value as ReportFrequency)}>
                  <option value="manual">Manual</option>
                  <option value="daily">Daily</option>
                  <option value="weekly">Weekly</option>
                  <option value="monthly">Monthly</option>
                </select>
              </label>
              <label className="reports-form-group">
                <span className="reports-form-label">Account alias</span>
                <input className="reports-input" value={reportAccountAlias} onChange={(event) => setReportAccountAlias(event.target.value)} maxLength={128} />
              </label>
              <button type="submit" className="reports-btn" disabled={busy !== null || !reportName.trim()}>{busy === 'create-report' ? 'Saving…' : 'Save report'}</button>
            </form>

            <fieldset className="reports-format-picker">
              <legend>Formats for the next generation</legend>
              {EXPORT_FORMATS.map((format) => (
                <label key={format} className="reports-checkbox-label">
                  <input type="checkbox" checked={selectedFormats.includes(format)} onChange={(event) => setSelectedFormats((selected) => event.target.checked ? [...selected, format] : selected.filter((item) => item !== format))} />
                  <span>{format.toUpperCase()}</span>
                </label>
              ))}
              {selectedFormats.length === 0 && <span className="reports-inline-warning">Select at least one format before generating.</span>}
            </fieldset>

            <div className="reports-table-wrap">
              <table className="reports-table">
                <thead><tr><th>Definition</th><th>Schedule</th><th>Scope</th><th>Last generated</th><th>Actions</th></tr></thead>
                <tbody>
                  {reports.map((report) => (
                    <tr key={report.item_id || report.name}>
                      <td><strong>{report.name}</strong><span className="reports-cell-sub">{report.item_id || 'Unassigned ID'}</span></td>
                      <td><span className={`reports-status-pill ${report.frequency === 'manual' ? 'muted' : 'accent'}`}>{report.frequency}</span></td>
                      <td>{report.account_alias || 'default'}{report.project_id ? <span className="reports-cell-sub">Project {report.project_id}</span> : null}</td>
                      <td>{dateLabel(report.last_generated_at)}</td>
                      <td>
                        <div className="reports-actions">
                          <button type="button" className="reports-btn reports-btn-small" onClick={() => void handleGenerate(report)} disabled={busy !== null || selectedFormats.length === 0 || !report.item_id}>{busy === `generate-${report.item_id}` ? 'Generating…' : 'Generate'}</button>
                          <button type="button" className="reports-btn reports-btn-small reports-btn-danger" onClick={() => void handleDeleteReport(report)} disabled={busy !== null || !report.item_id}>Delete</button>
                        </div>
                      </td>
                    </tr>
                  ))}
                  {!reports.length && !loading && <tr><td colSpan={5} className="reports-empty">No saved report definitions yet. Create one above to store the current shared query.</td></tr>}
                </tbody>
              </table>
            </div>
          </section>

          <section className="reports-card">
            <div className="reports-section-heading">
              <div>
                <p className="reports-eyebrow">LOCAL FILE INVENTORY</p>
                <h2 className="reports-card-title">Generated exports <span className="reports-title-count">{exports.length}</span></h2>
                <p className="reports-card-description">Only files present in the server inventory are downloadable. Paths are intentionally not shown; downloads stream through the managed-root API.</p>
              </div>
            </div>
            <div className="reports-table-wrap">
              <table className="reports-table">
                <thead><tr><th>Created</th><th>Format</th><th>Size</th><th>Redaction</th><th>Download</th></tr></thead>
                <tbody>
                  {exports.map((item) => (
                    <tr key={item.export_id}>
                      <td>{dateLabel(item.created_at)}</td>
                      <td><span className="reports-format-tag">{item.export_format.toUpperCase()}</span></td>
                      <td>{numberLabel(item.byte_size)} bytes</td>
                      <td><span className={`reports-status-pill ${item.redaction_applied ? 'success' : 'warning'}`}>{item.redaction_applied ? 'Applied' : 'Not recorded'}</span></td>
                      <td><a className="reports-download" href={`/api/v1/exports/${encodeURIComponent(item.export_id)}/download`} download>Download {item.export_format.toUpperCase()}</a></td>
                    </tr>
                  ))}
                  {!exports.length && !loading && <tr><td colSpan={5} className="reports-empty">No generated exports in the local inventory.</td></tr>}
                </tbody>
              </table>
            </div>

            <h3 className="reports-subheading">Per-period coverage timeline</h3>
            <p className="reports-card-description">
              Distinguishes full request-level detail, aggregate-only history, partial coverage, and missing intervals.
              A data gap must not look like a usage decline. Archive request detail is not implied. Unknown coverage is shown unavailable.
            </p>
            <div className="reports-table-wrap">
              <table className="reports-table">
                <thead>
                  <tr>
                    <th>Period</th>
                    <th>Coverage type</th>
                    <th>Detail level</th>
                    <th>Records</th>
                    <th>Tokens</th>
                    <th>Pricing status</th>
                    <th>Reconciliation</th>
                    <th>Notes</th>
                  </tr>
                </thead>
                <tbody>
                  {coverageTimeline.map((item) => (
                    <tr key={item.key}>
                      <td><strong>{item.label}</strong></td>
                      <td>
                        <span className={`reports-status-pill ${
                          item.coverageType === 'full' ? 'success' :
                          item.coverageType === 'archive' ? 'accent' :
                          item.coverageType === 'partial' ? 'warning' :
                          item.coverageType === 'missing' ? 'danger' : 'muted'
                        }`}>
                          {item.coverageLabel}
                        </span>
                      </td>
                      <td>{item.detailLevel}</td>
                      <td>{item.recordsCount == null ? 'Unavailable' : numberLabel(item.recordsCount)}</td>
                      <td>{item.totalTokens == null ? 'Unavailable' : numberLabel(item.totalTokens)}</td>
                      <td>
                        {item.isUnpriced ? (
                          <span className="reports-status-pill danger">Unpriced</span>
                        ) : item.isManual ? (
                          <span className="reports-status-pill accent">Manual</span>
                        ) : item.pricingStatus === 'Unavailable' ? (
                          <span className="reports-status-pill muted">Unavailable</span>
                        ) : (
                          <span className="reports-status-pill success">{item.pricingStatus}</span>
                        )}
                      </td>
                      <td>{item.reconciliation}</td>
                      <td><span className="reports-cell-sub">{item.notes}</span></td>
                    </tr>
                  ))}
                  {!coverageTimeline.length && (
                    <tr>
                      <td colSpan={8} className="reports-empty">
                        {queryError || !coverage
                          ? 'Coverage unavailable: Unknown coverage is shown unavailable.'
                          : 'No historical coverage intervals observed for the current scope. Unknown coverage is shown unavailable.'}
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          </section>
        </div>
      )}

      {activeTab === 'sources' && (
        <div className="reports-stack">
          <section className="reports-card">
            <div className="reports-section-heading">
              <div><p className="reports-eyebrow">OBSERVED COVERAGE</p><h2 className="reports-card-title">Source health &amp; coverage</h2><p className="reports-card-description">These values come from the live coverage query. Paths and raw source errors are omitted from this view.</p></div>
              {queryError && <span className="reports-status-pill warning">Query unavailable</span>}
            </div>
            {queryError && <p className="reports-banner error">{queryError}</p>}
            <div className="reports-stat-grid">
              <div className="reports-stat"><span>API health</span><strong>{health?.status || 'Unavailable'}</strong><small>{health ? `Version ${health.version}` : 'Health endpoint not loaded'}</small></div>
              <div className="reports-stat"><span>Live requests</span><strong>{numberLabel(coverageValues.live_requests_count)}</strong><small>{coverageValues.live_earliest_utc ? `${dateLabel(String(coverageValues.live_earliest_utc))} → ${dateLabel(String(coverageValues.live_latest_utc || ''))}` : 'No observed live time span'}</small></div>
              <div className="reports-stat"><span>Source checkpoints</span><strong>{numberLabel(coverageValues.checkpoints_count)}</strong><small>Current local scan records</small></div>
              <div className="reports-stat"><span>Archive partitions</span><strong>{numberLabel(coverageValues.archive_partitions_count)}</strong><small>Distinct stored partitions; reconciliation is shown below</small></div>
            </div>
            <h3 className="reports-subheading">Source checkpoints</h3>
            <div className="reports-table-wrap">
              <table className="reports-table">
                <thead>
                  <tr>
                    <th>Adapter / Account</th>
                    <th>Scan timeline / Source</th>
                    <th>Files / Observations</th>
                    <th>Checkpoint state</th>
                    <th>Newest record</th>
                    <th>Error states / Unparseable</th>
                  </tr>
                </thead>
                <tbody>
                  {checkpoints.map((checkpoint, index) => {
                    const newest = checkpoint.newest_record_timestamp;
                    return (
                      <tr key={`${checkpoint.source_adapter}-${checkpoint.account_alias}-${index}`}>
                        <td>
                          <strong>{checkpoint.source_adapter || 'Unavailable'}</strong> ({checkpoint.origin_harness || 'Unavailable'})
                          <span className="reports-cell-sub">Account: {checkpoint.account_alias || 'default'}</span>
                          <span className="reports-cell-sub">File ID: {checkpoint.file_locator || 'Unavailable'}</span>
                        </td>
                        <td>
                          <span>Last scan: {checkpoint.last_scan_at ? dateLabel(checkpoint.last_scan_at) : 'Unavailable'}</span>
                          <span className="reports-cell-sub">Last success: {checkpoint.last_success_at ? dateLabel(checkpoint.last_success_at) : 'Unavailable'}</span>
                        </td>
                        <td>
                          <span>Discovered files: {numberLabel(checkpoint.discovered_count)}</span>
                          <span className="reports-cell-sub">Parsed observations: {numberLabel(checkpoint.parsed_count)}</span>
                          <span className="reports-cell-sub">Stored: {numberLabel(checkpoint.observations_count)} · Skipped: {numberLabel(checkpoint.skipped_count)}</span>
                          <span className="reports-cell-sub">Current file generation · Cache: Unavailable</span>
                        </td>
                        <td>
                          <span className={`reports-status-pill ${checkpoint.status === 'ok' || checkpoint.status === 'success' ? 'success' : checkpoint.status ? 'warning' : 'muted'}`}>
                            {checkpoint.status || 'Unavailable'}
                          </span>
                          <span className="reports-cell-sub">
                            Processed lines: {numberLabel(checkpoint.last_line_index)} · Processed bytes: {numberLabel(checkpoint.last_byte_offset)} B
                          </span>
                          <span className="reports-cell-sub">File size: {numberLabel(checkpoint.file_size)} B</span>
                        </td>
                        <td>
                          <span>{newest ? dateLabel(newest) : 'Unavailable'}</span>
                          <span className="reports-cell-sub">Source event time only; scan time is not event time.</span>
                        </td>
                        <td>
                          <span>Recorded errors: {numberLabel(checkpoint.error_count)}</span>
                          <span className="reports-cell-sub">Unparseable observations: {numberLabel(checkpoint.unparseable_lines)}</span>
                          {checkpoint.last_error && <span className="reports-cell-sub">Error recorded for this source; details omitted for privacy. Scan: {checkpoint.last_scan_at ? dateLabel(checkpoint.last_scan_at) : 'Unavailable'}</span>}
                        </td>
                      </tr>
                    );
                  })}
                  {missingAdapters.map((adapter) => (
                    <tr key={`missing-${adapter.name}`}>
                      <td><strong>{adapter.name}</strong> ({adapter.harness})</td>
                      <td colSpan={5} className="reports-empty">{coverage && !queryError ? 'No persisted source checkpoints. Discovery, parsed counts, errors and newest event time are unavailable; this does not establish that no source files exist.' : 'Source health unavailable; coverage has not loaded successfully.'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <h3 className="reports-subheading">Archive coverage</h3>
            <div className="reports-table-wrap">
              <table className="reports-table">
                <thead><tr><th>Source / grain</th><th>Period</th><th>Records</th><th>Tokens</th><th>Pricing</th><th>Reconciliation</th></tr></thead>
                <tbody>
                  {archivePartitions.map((partition, index) => {
                    const pricing = partition.pricing_status || 'Unavailable';
                    const isUnpriced = pricing.toLowerCase() === 'unpriced';
                    const isManual = pricing.toLowerCase() === 'manual';
                    return (
                      <tr key={partition.partition_id || `${partition.source_name}-${index}`}>
                        <td>{partition.source_name || partition.partition_type || 'Archive'}<span className="reports-cell-sub">{partition.grain || 'Unavailable'} · {partition.timezone_name || 'Unavailable'}</span></td>
                        <td>{dateLabel(partition.start_time)}<span className="reports-cell-sub">to {dateLabel(partition.end_time)}</span></td>
                        <td>{numberLabel(partition.records_count)}</td>
                        <td>{numberLabel(partition.total_tokens)}</td>
                        <td>
                          {isUnpriced ? (
                            <span className="reports-status-pill danger">Unpriced</span>
                          ) : isManual ? (
                            <span className="reports-status-pill accent">Manual</span>
                          ) : pricing === 'Unavailable' ? (
                            <span className="reports-status-pill muted">Unavailable</span>
                          ) : (
                            <span className="reports-status-pill success">{pricing}</span>
                          )}
                        </td>
                        <td>{partition.is_reconciled ? 'Reconciled' : 'Unreconciled'}{partition.is_live_replaced ? ' · live replaced' : ''}</td>
                      </tr>
                    );
                  })}
                  {!archivePartitions.length && !queryError && <tr><td colSpan={6} className="reports-empty">Coverage reports no archive partitions. Unknown coverage is shown unavailable.</td></tr>}
                </tbody>
              </table>
            </div>
          </section>
        </div>
      )}

      {activeTab === 'pricing' && (
        <div className="reports-stack">
        <section className="reports-card">
          <div className="reports-section-heading"><div><p className="reports-eyebrow">QUERY PROVENANCE</p><h2 className="reports-card-title">Pricing &amp; historical FX</h2><p className="reports-card-description">Current shared-query valuation and pricing/FX evidence from the canonical engine. Unknown prices are not presented as zero.</p></div></div>
          {queryError && <p className="reports-banner error">{queryError}</p>}
          <div className="reports-stat-grid">
            <div className="reports-stat">
              <span>API-equivalent value · USD</span>
              <strong>{currencyLabel(values.api_equivalent_usd, 'USD')}</strong>
              <small>{values.api_equivalent_usd != null ? <span className="reports-status-pill success">Priced</span> : <span className="reports-status-pill danger">Unpriced</span>}</small>
            </div>
            <div className="reports-stat">
              <span>API-equivalent value · INR</span>
              <strong>{currencyLabel(values.api_equivalent_inr, 'INR')}</strong>
              <small>{values.api_equivalent_inr != null ? 'Historical FX converted' : 'Unavailable when historical FX is missing'}</small>
            </div>
            <div className="reports-stat">
              <span>Priced request coverage</span>
              <strong>{values.requests_count === 0 ? 'No requests' : typeof missingness.priced_coverage_ratio === 'number' ? `${(missingness.priced_coverage_ratio * 100).toFixed(1)}%` : 'Unavailable'}</strong>
              <small>
                <span className="reports-status-pill success">{numberLabel(missingness.priced_requests_count)} priced</span>
                {' · '}
                <span className="reports-status-pill danger">{numberLabel(missingness.unpriced_requests_count)} unpriced</span>
              </small>
            </div>
            <div className="reports-stat">
              <span>Actual billed amount</span>
              <strong>{values.actual_billed_amount == null ? 'Unavailable' : numberLabel(values.actual_billed_amount)}</strong>
              <small>Shown in source-reported units; summary does not return a currency code</small>
            </div>
          </div>
          <div className="reports-evidence-grid">
            <div className="reports-evidence-card"><h3>Stored pricing versions</h3>
              {pricingFx?.pricing_version_hashes?.length ? <ul className="reports-evidence-list">{pricingFx.pricing_version_hashes.map((hash) => <li key={hash}><code>{hash}</code></li>)}</ul> : <p className="reports-empty-copy">No pricing version hashes were returned for this query.</p>}
            </div>
            <div className="reports-evidence-card"><h3>Stored FX rate values</h3>
              {pricingFx?.fx_rates_available?.length ? <ul className="reports-evidence-list">{pricingFx.fx_rates_available.map((rate, index) => <li key={`${rate}-${index}`}><code>{numberLabel(rate)}</code></li>)}</ul> : <p className="reports-empty-copy">No historical FX rate values were returned.</p>}
              <p className="reports-footnote">The query response provides rate values but not their currency labels or effective dates; no pair or date is inferred here.</p>
            </div>
          </div>
          {summary?.warnings?.length ? <div className="reports-warning-list"><h3>Query warnings</h3><ul>{summary.warnings.map((warning, index) => <li key={`${warning}-${index}`}>{warning}</li>)}</ul></div> : null}
        </section>
        <PricingManager context={context} onPersist={refresh} />
        </div>
      )}

      {activeTab === 'privacy' && (
        <div className="reports-stack">
          <section className="reports-card">
            <div className="reports-section-heading"><div><p className="reports-eyebrow">EXPLICIT CONSENT</p><h2 className="reports-card-title">Privacy controls</h2><p className="reports-card-description">All four opt-ins default off. Enabling one requires confirmation; disabling one takes effect immediately. Current policy version: {privacySettings?.policy_version || 'Unavailable'}.</p></div></div>
            <div className="reports-privacy-list">
              {PRIVACY_OPTIONS.map((option) => {
                const enabled = Boolean(privacySettings?.[option.key]);
                return <div className="reports-privacy-row" key={option.key}>
                  <div><h3>{option.title}</h3><p>{option.description}</p><span className={`reports-status-pill ${enabled ? 'warning' : 'muted'}`}>{enabled ? 'Opted in' : 'Off'}</span></div>
                  <label className="reports-switch"><input type="checkbox" checked={enabled} disabled={!privacySettings || busy !== null} onChange={(event) => void handlePrivacyChange(option.key, event.target.checked, option.consent)} /><span className="reports-switch-track" aria-hidden="true" /><span className="reports-sr-only">{enabled ? 'Disable' : 'Enable'} {option.title}</span></label>
                </div>;
              })}
            </div>
          </section>

          <section className="reports-card">
            <div className="reports-section-heading"><div><p className="reports-eyebrow">PROJECT POLICY</p><h2 className="reports-card-title">Inclusion &amp; exclusion</h2><p className="reports-card-description">Enter project IDs or patterns one per line. A blank allow-list means unrestricted inclusion; a blank exclusion list adds no exclusions.</p></div></div>
            <form className="reports-policy-grid" onSubmit={handleSaveProjectPolicy}>
              <label className="reports-form-group"><span className="reports-form-label">Allowed project IDs</span><textarea className="reports-textarea" rows={4} value={allowedProjectsInput} onChange={(event) => setAllowedProjectsInput(event.target.value)} /></label>
              <label className="reports-form-group"><span className="reports-form-label">Excluded project IDs</span><textarea className="reports-textarea" rows={4} value={excludedProjectsInput} onChange={(event) => setExcludedProjectsInput(event.target.value)} /></label>
              <label className="reports-form-group"><span className="reports-form-label">Include patterns</span><textarea className="reports-textarea" rows={4} value={includePatternsInput} onChange={(event) => setIncludePatternsInput(event.target.value)} /></label>
              <label className="reports-form-group"><span className="reports-form-label">Exclude patterns</span><textarea className="reports-textarea" rows={4} value={excludePatternsInput} onChange={(event) => setExcludePatternsInput(event.target.value)} /></label>
              <div className="reports-form-actions"><button className="reports-btn" type="submit" disabled={busy !== null}>{busy === 'project-policy' ? 'Saving…' : 'Save project policy'}</button><span>Project IDs in the current policy: {projectPolicy.allowed_projects?.length ?? 0} allow-listed · {projectPolicy.excluded_projects?.length ?? 0} excluded.</span></div>
            </form>
          </section>

          <section className="reports-card">
            <div className="reports-section-heading"><div><p className="reports-eyebrow">MANAGED DATA</p><h2 className="reports-card-title">Retention</h2><p className="reports-card-description">Retention applies to Token Ledger’s managed database records and generated snapshots, never raw source logs. Running it permanently deletes matching managed data.</p></div><span className="reports-status-pill muted">{retentionDays ? `${retentionDays} days configured` : 'Not configured'}</span></div>
            <form className="reports-form reports-form-inline" onSubmit={handleSaveRetention}>
              <label className="reports-form-group"><span className="reports-form-label">Keep data for (days)</span><input className="reports-input reports-number-input" type="number" min={1} max={36500} step={1} value={retentionInput} onChange={(event) => setRetentionInput(event.target.value)} required /></label>
              <button type="submit" className="reports-btn" disabled={busy !== null}>{busy === 'retention-save' ? 'Saving…' : 'Save retention period'}</button>
              <button type="button" className="reports-btn reports-btn-danger" onClick={() => void handleRunRetention()} disabled={busy !== null || !retentionDays}>{busy === 'retention-run' ? 'Running…' : 'Run retention now'}</button>
            </form>
            {retentionResult && <div className="reports-retention-result"><strong>Latest run result</strong>{Object.entries(retentionResult).filter(([key, value]) => key.startsWith('deleted_') && typeof value === 'number').map(([key, value]) => <span key={key}>{key.replaceAll('_', ' ')}: {numberLabel(value)}</span>)}</div>}
          </section>
        </div>
      )}

      {activeTab === 'alerts' && (
        <div className="reports-stack">
          <section className="reports-card">
            <div className="reports-section-heading"><div><p className="reports-eyebrow">LOCAL BUDGET RULES</p><h2 className="reports-card-title">Usage alerts</h2><p className="reports-card-description">Create supported daily, monthly, session, or project/model budgets. Rules save locally; notification delivery is not initiated by this screen.</p></div><span className={`reports-status-pill ${privacySettings?.notifications ? 'success' : 'muted'}`}>{privacySettings?.notifications ? 'Notifications consent on' : 'Notifications consent off'}</span></div>
            <form className="reports-form reports-budget-form" onSubmit={handleCreateBudget}>
              <label className="reports-form-group reports-grow"><span className="reports-form-label">Rule name</span><input className="reports-input" value={budgetName} onChange={(event) => setBudgetName(event.target.value)} required maxLength={256} placeholder="e.g. Monthly token cap" /></label>
              <label className="reports-form-group"><span className="reports-form-label">Period</span><select className="reports-select" value={budgetPeriod} onChange={(event) => setBudgetPeriod(event.target.value as BudgetPeriod)}><option value="daily">Daily</option><option value="monthly">Monthly</option><option value="session">Session</option><option value="project_model">Project / model</option></select></label>
              <label className="reports-form-group"><span className="reports-form-label">Measure</span><select className="reports-select" value={budgetTarget} onChange={(event) => setBudgetTarget(event.target.value as BudgetTarget)}><option value="tokens">Tokens</option><option value="cost_usd">Cost (USD)</option><option value="cost_inr">Cost (INR)</option></select></label>
              <label className="reports-form-group"><span className="reports-form-label">Threshold</span><input className="reports-input reports-number-input" type="number" min="0.000001" step="any" value={budgetThreshold} onChange={(event) => setBudgetThreshold(event.target.value)} required /></label>
              <label className="reports-form-group"><span className="reports-form-label">Project ID scope (optional)</span><input className="reports-input" value={budgetProject} onChange={(event) => setBudgetProject(event.target.value)} /></label>
              <label className="reports-form-group"><span className="reports-form-label">Model scope (optional)</span><input className="reports-input" value={budgetModel} onChange={(event) => setBudgetModel(event.target.value)} /></label>
              <label className="reports-checkbox-label reports-budget-notifications"><input type="checkbox" checked={budgetNotifications} disabled={!privacySettings?.notifications} onChange={(event) => setBudgetNotifications(event.target.checked)} /><span>Enable notifications for this rule</span></label>
              <div className="reports-form-actions"><button className="reports-btn" type="submit" disabled={busy !== null}>{busy === 'budget-save' ? 'Saving…' : 'Save budget rule'}</button>{!privacySettings?.notifications && <span>Enable the explicit notifications opt-in under Privacy &amp; retention first.</span>}</div>
            </form>
            <div className="reports-table-wrap">
              <table className="reports-table"><thead><tr><th>Rule</th><th>Period</th><th>Threshold</th><th>Scope</th><th>Notifications</th><th>Actions</th></tr></thead><tbody>
                {budgets.map((budget) => <tr key={budget.id}><td><strong>{budget.name}</strong><span className="reports-cell-sub">{budget.account_alias || 'default'}</span></td><td>{budget.period}</td><td>{numberLabel(budget.threshold)} {budget.target_type.replace('_', ' ')}</td><td>{Object.entries(budget.scope || {}).map(([key, value]) => `${key}: ${value}`).join(' · ') || 'All'}</td><td><span className={`reports-status-pill ${budget.notifications_enabled ? 'warning' : 'muted'}`}>{budget.notifications_enabled ? 'Enabled' : 'Off'}</span></td><td><button type="button" className="reports-btn reports-btn-small reports-btn-danger" onClick={() => void handleDeleteBudget(budget)} disabled={busy !== null}>Delete</button></td></tr>)}
                {!budgets.length && !loading && <tr><td colSpan={6} className="reports-empty">No local budget rules are configured.</td></tr>}
              </tbody></table>
            </div>
          </section>
        </div>
      )}

      {activeTab === 'views' && (
        <div className="reports-stack">
          <section className="reports-card">
            <div className="reports-section-heading"><div><p className="reports-eyebrow">SHARED QUERY STATE</p><h2 className="reports-card-title">Saved views</h2><p className="reports-card-description">Save, restore, and delete versioned filters through the local saved-view resource. Applying a view updates the shell’s shared query state.</p></div></div>
            <form className="reports-form reports-form-inline" onSubmit={handleSaveView}>
              <label className="reports-form-group reports-grow"><span className="reports-form-label">View name</span><input className="reports-input" value={viewName} onChange={(event) => setViewName(event.target.value)} required maxLength={256} placeholder="e.g. OMP this month" /></label>
              <button className="reports-btn" type="submit" disabled={busy !== null}>{busy === 'view-save' ? 'Saving…' : 'Save current filters'}</button>
            </form>
            <div className="reports-item-list">
              {savedViews.map((view) => <article className="reports-saved-item" key={view.view_id}><div><h3>{view.view_name}</h3><p>{view.page_name || 'All pages'}{view.is_default ? ' · default' : ''} · updated {dateLabel(view.updated_at)}</p></div><div className="reports-actions"><button className="reports-btn reports-btn-small" type="button" onClick={() => handleApplyView(view)}>Apply</button><button className="reports-btn reports-btn-small reports-btn-danger" type="button" onClick={() => void handleDeleteView(view)} disabled={busy !== null}>Delete</button></div></article>)}
              {!savedViews.length && !loading && <p className="reports-empty-copy">No saved views are stored locally.</p>}
            </div>
          </section>

          <section className="reports-card">
            <div className="reports-section-heading"><div><p className="reports-eyebrow">WORKSPACE PRESETS</p><h2 className="reports-card-title">Saved Reports layouts</h2><p className="reports-card-description">Store this page’s active section and shared filter snapshot with the existing versioned layouts resource.</p></div></div>
            <form className="reports-form reports-form-inline" onSubmit={handleSaveLayout}>
              <label className="reports-form-group reports-grow"><span className="reports-form-label">Layout name</span><input className="reports-input" value={layoutName} onChange={(event) => setLayoutName(event.target.value)} required maxLength={256} placeholder="e.g. Weekly operations review" /></label>
              <label className="reports-form-group"><span className="reports-form-label">Open Reports section</span><select className="reports-select" value={layoutSection} onChange={(event) => setLayoutSection(event.target.value as SettingsTab)}>{SETTINGS_TABS.map((tab) => <option key={tab.id} value={tab.id}>{tab.label}</option>)}</select></label>
              <label className="reports-checkbox-label"><input type="checkbox" checked={layoutDefault} onChange={(event) => setLayoutDefault(event.target.checked)} /><span>Set as default layout</span></label>
              <button className="reports-btn" type="submit" disabled={busy !== null}>{busy === 'layout-save' ? 'Saving…' : 'Save workspace'}</button>
            </form>
            <div className="reports-item-list">
              {savedLayouts.map((layout) => <article className="reports-saved-item" key={layout.layout_id}><div><h3>{layout.layout_name}</h3><p>{layout.page_name || 'All pages'}{layout.is_default ? ' · default' : ''} · updated {dateLabel(layout.updated_at)}</p></div><div className="reports-actions"><button className="reports-btn reports-btn-small" type="button" onClick={() => handleApplyLayout(layout)}>Apply</button><button className="reports-btn reports-btn-small reports-btn-danger" type="button" onClick={() => void handleDeleteLayout(layout)} disabled={busy !== null}>Delete</button></div></article>)}
              {!savedLayouts.length && !loading && <p className="reports-empty-copy">No Reports layouts are stored locally.</p>}
            </div>
          </section>
        </div>
      )}
    </main>
  );
}
