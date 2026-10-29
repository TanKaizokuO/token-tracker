import React, { useState, useEffect, useMemo, useCallback } from 'react';
import type { PageProps } from '../App';
import SubscriptionFees from '../components/SubscriptionFees';
import { ChartPanel } from '../components/ChartPanel';
import { formatInr, formatUsdAsInr, usdToInr } from '../lib/money';
import './Providers.css';

type CoverageStatus = 'exact' | 'partial' | 'unavailable';

function coverageStatus(value: unknown): CoverageStatus {
  return value === 'exact' || value === 'partial' || value === 'unavailable'
    ? value
    : 'unavailable';
}

function CoverageBadge({ status }: { status: CoverageStatus | null | undefined }) {
  if (!status || status === 'exact') return null;
  return (
    <span
      className="badge-tag"
      style={status === 'partial' ? { color: 'var(--accent-amber)', borderColor: 'var(--accent-amber)' } : undefined}
    >
      {status === 'partial' ? 'Partial' : 'Unavailable'}
    </span>
  );
}

interface ProviderRouteRow {
  provider: string;
  access_route: string;
  account_alias?: string | null;
  models: string[];
  request_count: number | null;
  total_tokens: number | null;
  fresh_input_tokens: number | null;
  cache_read_tokens: number | null;
  cache_write_tokens: number | null;
  output_tokens: number | null;
  total_tokens_status: CoverageStatus;
  token_fields_status: Record<string, CoverageStatus>;
  actual_api_spend_usd: number | null;
  api_equivalent_usd: number | null;
  api_equivalent_inr: number | null;
  subscription_fees_usd: number | null;
  cache_savings_usd: number | null;
  actual_api_spend_usd_status: CoverageStatus;
  api_equivalent_usd_status: CoverageStatus;
  api_equivalent_inr_status: CoverageStatus;
  subscription_fees_usd_status: CoverageStatus;
  cache_savings_usd_status: CoverageStatus;
  unpriced_request_count: number | null;
  is_verified_route: boolean;
}

interface FinancialCostSummary {
  actual_api_spend_usd: number | null;
  actual_api_spend_usd_status: CoverageStatus;
  actual_api_spend_inr_status: CoverageStatus;
  api_equivalent_usd_status: CoverageStatus;
  api_equivalent_inr_status: CoverageStatus;
  subscription_fees_usd_status: CoverageStatus;
  cache_savings_usd_status: CoverageStatus;
  actual_api_spend_inr: number | null;
  api_equivalent_usd: number | null;
  api_equivalent_inr: number | null;
  subscription_fees_usd: number | null;
  cache_savings_usd: number | null;
  unpriced_request_count: number | null;
  priced_request_count: number | null;
  fx_date: string | null;
  fx_rate_usd_to_inr: number | null;
  currency_basis: 'USD' | 'INR';
}

function nullableNumber(value: unknown): number | null {
  if (value === null || value === undefined || value === '') return null;
  const numeric = typeof value === 'number' ? value : Number(value);
  return Number.isFinite(numeric) ? numeric : null;
}

function formatCount(value: number | null): string {
  return value === null ? '—' : value.toLocaleString();
}

function formatMoney(value: number | null, currency: 'USD' | 'INR'): string {
  if (value === null) return 'Unavailable';
  return currency === 'USD' ? `$${value.toFixed(2)}` : formatInr(value);
}

interface QuotaCardItem {
  provider: string;
  access_route: string;
  is_available: boolean;
  official_timestamp: string | null;
  provenance: string;
  window_type?: string;
  window_reset_at?: string | null;
  limit_tokens?: number | null;
  used_tokens?: number | null;
  remaining_tokens?: number | null;
  limit_requests?: number | null;
  used_requests?: number | null;
  percent_used?: number | null;
  status?: string;
  account_alias?: string;
  units?: string;
  used_value?: number | null;
  limit_value?: number | null;
  remaining_value?: number | null;
  history?: Array<{
    quota_id: number; official_timestamp: string; provenance: string;
    window_reset_at: string | null; reported_status: string; status: string;
    used_value: number | null; limit_value: number | null;
    remaining_value: number | null; units: string; reported_percent_used: number | null;
  }>;
}

interface LiveActiveSession {
  session_id: string;
  origin_harness: 'claude-code' | 'omp';
  active_model: string;
  project_id?: string | null;
  last_observed_time: string;
  tokens_so_far: number;
  burn_rate_tpm?: number | null;
  estimated_value_usd?: number | null;
  last_sync_timestamp: string | null;
  value_coverage?: string;
  data_status?: string;
  status: 'active' | 'idle' | 'completed';
}

interface BurnRateData {
  declared_interval_minutes: number;
  current_burn_rate_tpm: number | null;
  current_burn_rate_cost_per_hour: number | null;
  typical_burn_rate_tpm: number | null;
  burn_status: 'normal' | 'elevated' | 'spike' | 'unavailable';
  active_sessions: LiveActiveSession[];
  recent_burn_series: Array<{
    timestamp: string; tpm: number | null; tokens: number | null;
    cost_usd: number | null; denominator_minutes: number; request_count: number;
    priced_requests: number; is_partial: boolean; is_active: boolean;
  }>;
}

interface BudgetRecord {
  id: string;
  name: string;
  period: 'daily' | 'monthly' | 'session' | 'project_model';
  target_type: 'tokens' | 'cost_usd' | 'cost_inr';
  threshold: number;
  scope?: {
    project_id?: string;
    model?: string;
  };
  current_value: number;
  percent_consumed: number;
  status: 'ok' | 'warning' | 'breached';
  notifications_enabled: boolean;
}

interface BudgetQueryResponse {
  error?: string;
  values?: {
    budgets?: BudgetRecord[];
  };
}

export default function Providers({ context }: PageProps) {
  const { query, setQuery, request, write } = context;

  // Local state for UI data
  const [loading, setLoading] = useState<boolean>(true);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  // Provider analytics state
  const [routeRows, setRouteRows] = useState<ProviderRouteRow[]>([]);
  const [financialSummary, setFinancialSummary] = useState<FinancialCostSummary | null>(null);
  const [quotas, setQuotas] = useState<QuotaCardItem[]>([]);
  const [burnRate, setBurnRate] = useState<BurnRateData | null>(null);
  const [budgets, setBudgets] = useState<BudgetRecord[]>([]);

  // Filter/Search inside the Provider table
  const [searchFilter, setSearchFilter] = useState<string>('');
  const [selectedCurrency, setSelectedCurrency] = useState<'USD' | 'INR'>('INR');

  // Budget form state
  const [newBudgetPeriod, setNewBudgetPeriod] = useState<'daily' | 'monthly' | 'session' | 'project_model'>('daily');
  const [newBudgetType, setNewBudgetType] = useState<'tokens' | 'cost_usd' | 'cost_inr'>('tokens');
  const [newBudgetThreshold, setNewBudgetThreshold] = useState<string>('500000');
  const [newBudgetScope, setNewBudgetScope] = useState<string>('');
  const [newBudgetScopeType, setNewBudgetScopeType] = useState<'project_id' | 'model'>('project_id');
  const [notificationConsent, setNotificationConsent] = useState<boolean>(false);
  const [budgetSuccessMsg, setBudgetSuccessMsg] = useState<string | null>(null);
  const [budgetErrorMsg, setBudgetErrorMsg] = useState<string | null>(null);

  // Fetch all provider metrics
  const loadData = useCallback(async () => {
    setLoading(true);
    setErrorMsg(null);
    try {
      // 1. Providers breakdown
      const provRes = await request('providers_breakdown').catch(() =>
        request('summary')
      );

      // Parse provider route rows from specialized response or fallback rows
      if (provRes?.rows && Array.isArray(provRes.rows) && provRes.rows.length > 0) {
        const parsedRows: ProviderRouteRow[] = (provRes.rows as Array<Record<string, unknown>>).map((r) => ({
          provider: String(r.provider || 'unknown'),
          access_route: String(r.access_route || 'direct_api'),
          account_alias: r.account_alias ? String(r.account_alias) : null,
          models: Array.isArray(r.models)
            ? (r.models as string[])
            : r.normalized_model
            ? [String(r.normalized_model)]
            : [],
          request_count: nullableNumber(r.request_count),
          total_tokens: nullableNumber(r.total_tokens),
          fresh_input_tokens: nullableNumber(r.fresh_input_tokens),
          cache_read_tokens: nullableNumber(r.cache_read_tokens),
          cache_write_tokens: nullableNumber(r.cache_write_tokens),
          total_tokens_status: coverageStatus(r.total_tokens_status),
          token_fields_status: (r.token_fields_status && typeof r.token_fields_status === 'object'
            ? r.token_fields_status as Record<string, CoverageStatus>
            : {}),
          output_tokens: nullableNumber(r.output_tokens),
          actual_api_spend_usd: nullableNumber(r.actual_api_spend_usd),
          api_equivalent_usd: nullableNumber(r.api_equivalent_usd),
          api_equivalent_inr: nullableNumber(r.api_equivalent_inr),
          subscription_fees_usd: nullableNumber(r.subscription_fees_usd),
          cache_savings_usd: nullableNumber(r.cache_savings_usd),
          actual_api_spend_usd_status: coverageStatus(r.actual_api_spend_usd_status),
          api_equivalent_usd_status: coverageStatus(r.api_equivalent_usd_status),
          api_equivalent_inr_status: coverageStatus(r.api_equivalent_inr_status),
          subscription_fees_usd_status: coverageStatus(r.subscription_fees_usd_status),
          cache_savings_usd_status: coverageStatus(r.cache_savings_usd_status),
          unpriced_request_count: nullableNumber(r.unpriced_request_count),
          is_verified_route: Boolean(r.is_verified_route ?? (r.access_route !== undefined)),
        }));
        setRouteRows(parsedRows);
      } else {
        setRouteRows([]);
      }

      const fxRatesAvailable: number[] = Array.isArray((provRes?.pricing_fx as any)?.fx_rates_available)
        ? (provRes.pricing_fx as any).fx_rates_available.map(nullableNumber).filter((rate: any): rate is number => rate !== null)
        : [];
      if (provRes?.values?.financial_summary && typeof provRes.values.financial_summary === 'object') {
        const fin = provRes.values.financial_summary as Record<string, unknown>;
        const candidateFxRate = nullableNumber(fin.fx_rate_usd_to_inr);
        const fxRate = candidateFxRate !== null && fxRatesAvailable.includes(candidateFxRate) ? candidateFxRate : null;
        const fxDate = fxRate !== null && typeof fin.fx_date === 'string' ? fin.fx_date : null;
        const hasDatedFx = fxRate !== null && fxDate !== null;
        setFinancialSummary({
          actual_api_spend_usd: nullableNumber(fin.actual_api_spend_usd),
          actual_api_spend_inr: hasDatedFx ? nullableNumber(fin.actual_api_spend_inr) : null,
          api_equivalent_usd: nullableNumber(fin.api_equivalent_usd),
          api_equivalent_inr: hasDatedFx ? nullableNumber(fin.api_equivalent_inr) : null,
          subscription_fees_usd: nullableNumber(fin.subscription_fees_usd),
          cache_savings_usd: nullableNumber(fin.cache_savings_usd),
          actual_api_spend_usd_status: coverageStatus(fin.actual_api_spend_usd_status),
          actual_api_spend_inr_status: hasDatedFx ? coverageStatus(fin.actual_api_spend_inr_status) : 'unavailable',
          api_equivalent_usd_status: coverageStatus(fin.api_equivalent_usd_status),
          api_equivalent_inr_status: hasDatedFx ? coverageStatus(fin.api_equivalent_inr_status) : 'unavailable',
          subscription_fees_usd_status: coverageStatus(fin.subscription_fees_usd_status),
          cache_savings_usd_status: coverageStatus(fin.cache_savings_usd_status),
          unpriced_request_count: nullableNumber(fin.unpriced_request_count),
          priced_request_count: nullableNumber(fin.priced_request_count),
          fx_date: hasDatedFx ? fxDate : null,
          fx_rate_usd_to_inr: hasDatedFx ? fxRate : null,
          currency_basis: selectedCurrency,
        });
      } else {
        setFinancialSummary({
          actual_api_spend_usd: null,
          actual_api_spend_inr: null,
          api_equivalent_usd: nullableNumber(provRes?.values?.api_equivalent_usd),
          api_equivalent_inr: null,
          subscription_fees_usd: nullableNumber(provRes?.values?.subscription_fees_usd),
          cache_savings_usd: nullableNumber(provRes?.values?.cache_savings_usd),
          actual_api_spend_usd_status: 'unavailable',
          actual_api_spend_inr_status: 'unavailable',
          api_equivalent_usd_status: coverageStatus(provRes?.values?.api_equivalent_usd_status),
          api_equivalent_inr_status: 'unavailable',
          subscription_fees_usd_status: coverageStatus(provRes?.values?.subscription_fees_usd_status),
          cache_savings_usd_status: coverageStatus(provRes?.values?.cache_savings_usd_status),
          unpriced_request_count: nullableNumber(provRes?.missingness?.unpriced_requests_count),
          priced_request_count: nullableNumber(provRes?.missingness?.priced_requests_count),
          fx_date: null,
          fx_rate_usd_to_inr: null,
          currency_basis: selectedCurrency,
        });
      }

      // 2. Official Provider Quotas (WP16 item 23)
      const quotaRes = await request('provider_quotas').catch(() => null);
      if (quotaRes?.values?.quotas && Array.isArray(quotaRes.values.quotas)) {
        setQuotas(quotaRes.values.quotas as QuotaCardItem[]);
      } else {
        // Known default provider cards displaying explicit "Quota unavailable" per contract
        setQuotas([
          {
            provider: 'anthropic',
            access_route: 'direct_api',
            is_available: false,
            official_timestamp: null,
            provenance: 'none',
            limit_tokens: null,
            used_tokens: null,
            percent_used: null,
          },
          {
            provider: 'openai',
            access_route: 'direct_api',
            is_available: false,
            official_timestamp: null,
            provenance: 'none',
            limit_tokens: null,
            used_tokens: null,
            percent_used: null,
          },
          {
            provider: 'bedrock',
            access_route: 'bedrock_cross_region',
            is_available: false,
            official_timestamp: null,
            provenance: 'none',
            limit_tokens: null,
            used_tokens: null,
            percent_used: null,
          },
        ]);
      }

      // 3. Burn rate and live active sessions (WP16 item 24 / item 47)
      const burnRes = await request('token_burn_rate').catch(() => null);
      if (burnRes?.values) {
        setBurnRate({
          declared_interval_minutes: Number(burnRes.values.declared_interval_minutes || 15),
          current_burn_rate_tpm: nullableNumber(burnRes.values.current_burn_rate_tpm),
          current_burn_rate_cost_per_hour: nullableNumber(burnRes.values.current_burn_rate_cost_per_hour),
          typical_burn_rate_tpm: nullableNumber(burnRes.values.typical_burn_rate_tpm),
          burn_status: (burnRes.values.burn_status as 'normal' | 'elevated' | 'spike' | 'unavailable') || 'normal',
          active_sessions: (burnRes.values.active_sessions as LiveActiveSession[]) || [],
          recent_burn_series: (burnRes.values.recent_burn_series as BurnRateData['recent_burn_series']) || [],
        });
      } else {
        setBurnRate({
          declared_interval_minutes: 15,
          current_burn_rate_tpm: null,
          current_burn_rate_cost_per_hour: null,
          typical_burn_rate_tpm: null,
          burn_status: 'unavailable',
          active_sessions: [],
          recent_burn_series: [],
        });
      }
      const liveRes = await request('live_sessions');
      setBurnRate(previous => previous ? {
        ...previous,
        active_sessions: (liveRes?.values?.active_sessions as LiveActiveSession[]) || [],
      } : previous);

      // 4. Usage Budgets (WP16 item 26)
      const budgetRes = await request<BudgetQueryResponse>('usage_budgets');
      if (budgetRes.error) {
        throw new Error(budgetRes.error);
      }
      setBudgets(budgetRes.values?.budgets ?? []);
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      setErrorMsg(msg || 'Error fetching provider analytics');
    } finally {
      setLoading(false);
    }
  }, [request, selectedCurrency]);

  useEffect(() => {
    loadData();
  }, [loadData, query]);

  // Handle setting a filter from clicking a provider/route
  const handleFilterClick = (field: 'provider' | 'route', value: string) => {
    setQuery((prev) => {
      const existing = prev.filters[field] || [];
      const updated = existing.includes(value)
        ? existing.filter((x) => x !== value)
        : [...existing, value];
      return {
        ...prev,
        filters: {
          ...prev.filters,
          [field]: updated.length > 0 ? updated : undefined,
        },
      };
    });
  };

  // Persist a new budget through the domain API
  const handleAddBudget = async (e: React.FormEvent) => {
    e.preventDefault();
    const thresholdNum = Number(newBudgetThreshold);
    if (!Number.isFinite(thresholdNum) || thresholdNum <= 0) {
      setBudgetErrorMsg('Threshold must be a positive number');
      return;
    }

    setBudgetErrorMsg(null);
    setBudgetSuccessMsg(null);
    const scopeValue = newBudgetScope.trim();

    try {
      await write('budgets', {
        name: `${newBudgetPeriod} ${newBudgetType.replace(/_/g, ' ')} budget`,
        period: newBudgetPeriod,
        target_type: newBudgetType,
        threshold: thresholdNum,
        scope: scopeValue ? { [newBudgetScopeType]: scopeValue } : {},
        notifications_enabled: notificationConsent,
      });
      await loadData();
      setBudgetSuccessMsg(`Budget rule saved for ${newBudgetPeriod} (${newBudgetType})`);
      setTimeout(() => setBudgetSuccessMsg(null), 3000);
    } catch (err: unknown) {
      setBudgetErrorMsg(err instanceof Error ? err.message : 'Failed to save budget rule');
    }
  };

  const handleDeleteBudget = async (budget: BudgetRecord) => {
    setBudgetErrorMsg(null);
    setBudgetSuccessMsg(null);
    try {
      await write(`budgets/${encodeURIComponent(budget.id)}`, null, 'DELETE');
      await loadData();
      setBudgetSuccessMsg(`Deleted ${budget.name}`);
      setTimeout(() => setBudgetSuccessMsg(null), 3000);
    } catch (err: unknown) {
      setBudgetErrorMsg(err instanceof Error ? err.message : 'Failed to delete budget rule');
    }
  };

  // Filtered rows
  const filteredRows = useMemo(() => {
    if (!searchFilter.trim()) return routeRows;
    const q = searchFilter.toLowerCase();
    return routeRows.filter(
      (r) =>
        r.provider.toLowerCase().includes(q) ||
        r.access_route.toLowerCase().includes(q) ||
        (r.account_alias && r.account_alias.toLowerCase().includes(q)) ||
        r.models.some((m) => m.toLowerCase().includes(q))
    );
  }, [routeRows, searchFilter]);

  return (
    <div className="provider-page">
      <SubscriptionFees write={write} />
      {/* Header */}
      <div className="page-header-row">
        <div className="page-title-block">
          <h1>
            <span>Providers & Limits</span>
            <span className="badge-tag live">
              <span className="pulse-dot" />
              Live Route Telemetry
            </span>
          </h1>
          <p>
            Provider and access-route attribution, quota limit snapshots, burn rate monitoring, and locally enforced budget guardrails.
          </p>
        </div>
        <div className="header-badges">
          <div className="badge-tag">
            <span>Currency:</span>
            <button
              type="button"
              className={`btn-secondary ${selectedCurrency === 'USD' ? 'active' : ''}`}
              style={{ padding: '2px 6px', fontSize: '11px', marginRight: '4px' }}
              onClick={() => setSelectedCurrency('USD')}
            >
              USD
            </button>
            <button
              type="button"
              className={`btn-secondary ${selectedCurrency === 'INR' ? 'active' : ''}`}
              style={{ padding: '2px 6px', fontSize: '11px' }}
              onClick={() => setSelectedCurrency('INR')}
            >
              INR
            </button>
          </div>
          <button type="button" className="btn-secondary" onClick={() => loadData()}>
            ↻ Refresh
          </button>
        </div>
      </div>

      {errorMsg && (
        <div className="overlap-warning-banner" style={{ borderColor: 'var(--accent-rose)' }}>
          <span className="overlap-icon">⚠️</span>
          <div className="overlap-content">
            <strong>Provider Telemetry Notice</strong>
            <span>{errorMsg}</span>
          </div>
        </div>
      )}

      {/* Financial Cost Separation Banner (WP16 item 22) */}
      <div className="content-section">
        <div className="section-header">
          <h2>Actual Spend vs API-Equivalent Cost</h2>
          <span className="fin-rate-note">
            {financialSummary?.fx_date && financialSummary.fx_rate_usd_to_inr !== null
              ? `Dated FX Rate: 1 USD = ${financialSummary.fx_rate_usd_to_inr.toFixed(2)} INR (Date: ${financialSummary.fx_date})`
              : 'Dated FX rate unavailable'}
          </span>
        </div>
        <div className="section-body">
          <div className="financial-distinction-bar">
            <div className="fin-item">
              <span className="fin-title">Actual API Spend</span>
              <span className="fin-val">
                {formatMoney(
                  selectedCurrency === 'USD' ? financialSummary?.actual_api_spend_usd ?? null : financialSummary?.actual_api_spend_inr ?? usdToInr(financialSummary?.actual_api_spend_usd),
                  selectedCurrency
                )} <CoverageBadge status={selectedCurrency === 'USD' ? financialSummary?.actual_api_spend_usd_status : financialSummary?.actual_api_spend_inr_status} />
              </span>
            </div>
            <div className="fin-item">
              <span className="fin-title">API-Equivalent Value</span>
              <span className="fin-val" style={{ color: 'var(--accent-cyan)' }}>
                {formatMoney(
                  selectedCurrency === 'USD' ? financialSummary?.api_equivalent_usd ?? null : financialSummary?.api_equivalent_inr ?? usdToInr(financialSummary?.api_equivalent_usd),
                  selectedCurrency
                )} <CoverageBadge status={selectedCurrency === 'USD' ? financialSummary?.api_equivalent_usd_status : financialSummary?.api_equivalent_inr_status} />
              </span>
            </div>
            <div className="fin-item">
              <span className="fin-title">Request-linked Subscription Fees</span>
              <span className="fin-val">
                {selectedCurrency === 'USD'
                  ? formatMoney(financialSummary?.subscription_fees_usd ?? null, 'USD')
                  : formatUsdAsInr(financialSummary?.subscription_fees_usd, 2, 'Unavailable')} <CoverageBadge status={financialSummary?.subscription_fees_usd_status} />
              </span>
            </div>
            <div className="fin-item">
              <span className="fin-title">Cache Savings Value</span>
              <span className="fin-val" style={{ color: 'var(--accent-emerald)' }}>
                {selectedCurrency === 'USD'
                  ? formatMoney(financialSummary?.cache_savings_usd ?? null, 'USD')
                  : formatUsdAsInr(financialSummary?.cache_savings_usd, 2, 'Unavailable')} <CoverageBadge status={financialSummary?.cache_savings_usd_status} />
              </span>
            </div>
            <div className="fin-item">
              <span className="fin-title">Unpriced Requests</span>
              <span className="fin-val" style={{ color: financialSummary?.unpriced_request_count !== null && financialSummary?.unpriced_request_count !== undefined && financialSummary.unpriced_request_count > 0 ? 'var(--accent-amber)' : 'inherit' }}>
                {formatCount(financialSummary?.unpriced_request_count ?? null)}
              </span>
            </div>
          </div>
          <p style={{ fontSize: '11px', color: 'var(--text-muted)' }}>
            * API-equivalent cost uses public API token pricing and is not direct subscription billing. INR values require a dated FX rate matching an available historical rate; missing values remain unavailable.
          </p>
        </div>
      </div>

      {/* Quota & Usage-Limit Monitor (WP16 item 23) */}
      <div className="content-section">
        <div className="section-header">
          <h2>Subscription Quotas & Usage Limits</h2>
          <span className="badge-tag">Official Provider Provenance Only</span>
        </div>
        <div className="section-body">
          <div className="quota-grid">
            {quotas.map((q, idx) => (
              <div key={idx} className="quota-card">
                <div className="quota-card-header">
                  <div>
                    <div className="quota-provider-title">{q.provider}</div>
                    <div className="quota-route-sub">{q.access_route}</div>
                  </div>
                  <span className="quota-provenance-tag">
                    {q.provenance === 'none' ? 'No Official Stream' : q.provenance}
                  </span>
                  <span>{q.account_alias ?? 'default'} · {q.window_type ?? 'unknown window'}</span>
                </div>

                {q.is_available ? (
                  <div className="quota-progress-container">
                    <div className="quota-stats-row">
                      <span>Consumed: {q.used_tokens?.toLocaleString() ?? '—'}</span>
                      <span>Limit: {q.limit_tokens?.toLocaleString() ?? '—'}</span>
                    </div>
                    <div className="quota-progress-bar" hidden={q.percent_used == null}>
                      <div
                        className={`quota-progress-fill ${
                          (q.percent_used ?? 0) > 85 ? 'fill-rose' : (q.percent_used ?? 0) > 65 ? 'fill-amber' : 'fill-emerald'
                        }`}
                        style={{ width: `${Math.min(100, Math.max(0, q.percent_used ?? 0))}%` }}
                      />
                    </div>
                    <div className="quota-stats-row">
                      <span>{q.percent_used == null ? 'Official percentage unavailable' : `${q.percent_used.toFixed(1)}% used`}</span>
                      <span>Reset: {q.window_reset_at ? new Date(q.window_reset_at).toLocaleTimeString() : 'N/A'}</span>
                    </div>
                  </div>
                ) : (
                  <div className="quota-unavailable-box">
                    <strong>Quota unavailable</strong>
                    <span>{q.status === 'stale' ? 'Official snapshot expired at its reset time' : 'Official quota unavailable for this route'}</span>
                    <span style={{ fontSize: '10px', marginTop: '4px' }}>
                      (Local token counts are not used to fabricate quota percentages)
                    </span>
                  </div>
                )}
                <p>Measured: {q.official_timestamp ?? 'Unavailable'} · Reset: {q.window_reset_at ?? 'Unavailable'}</p>
                <p>Official values: {formatCount(q.used_value ?? null)} used / {formatCount(q.limit_value ?? null)} limit · {formatCount(q.remaining_value ?? null)} remaining ({q.units ?? 'unknown units'})</p>
                <details>
                  <summary>Official snapshot history ({q.history?.length ?? 0})</summary>
                  <table className="data-table">
                    <thead><tr><th>Measured</th><th>Source</th><th>Reported status / current expiry</th><th>Used / limit / remaining</th><th>Official %</th><th>Reset</th></tr></thead>
                    <tbody>{q.history?.map(h => <tr key={h.quota_id}>
                      <td>{h.official_timestamp}</td><td>{h.provenance}</td>
                      <td>{h.reported_status} / {h.status}</td>
                      <td>{formatCount(h.used_value)} / {formatCount(h.limit_value)} / {formatCount(h.remaining_value)} {h.units}</td>
                      <td>{h.reported_percent_used ?? 'Unavailable'}</td><td>{h.window_reset_at ?? 'Unavailable'}</td>
                    </tr>)}</tbody>
                  </table>
                </details>
              </div>
            ))}
          </div>
        </div>
      </div>

      {/* Burn Rate & Live Active Sessions (WP16 item 24 / item 47) */}
      <div className="content-section">
        <div className="section-header">
          <h2>Token Burn-Rate & Active Sessions</h2>
          <span className="badge-tag live">
            <span className="pulse-dot" /> Declared Interval: {burnRate?.declared_interval_minutes ?? 15}m
          </span>
        </div>
        <div className="section-body">
          <ChartPanel title="Recent local burn" context={context}
            subtitle="Five-minute denominator per point; latest bucket partial/active. Empty or unknown counters have no invented rate. Values are INR at the dated FX rate; local observations are partial."
            empty={!burnRate?.recent_burn_series.length}
            option={{
              tooltip: { trigger: 'axis' },
              xAxis: { type: 'category', data: burnRate?.recent_burn_series.map(b => `${b.timestamp}${b.is_partial ? ' · partial' : ''}`) },
              yAxis: { type: 'value', name: 'tokens/minute (5m denominator)' },
              series: [{ type: 'line', name: 'Observed TPM', connectNulls: false, data: burnRate?.recent_burn_series.map(b => b.tpm) }],
            }} />
          <details><summary>Burn bucket evidence</summary><table className="data-table">
            <thead><tr><th>Start</th><th>Requests / priced</th><th>Tokens / denominator</th><th>Estimated INR</th><th>State</th></tr></thead>
            <tbody>{burnRate?.recent_burn_series.map(b => <tr key={b.timestamp}>
              <td>{b.timestamp}</td><td>{b.request_count} / {b.priced_requests}</td>
              <td>{formatCount(b.tokens)} / {b.denominator_minutes}m</td><td>{formatUsdAsInr(b.cost_usd, 2, 'Unavailable')}</td>
              <td>{b.is_partial ? 'Partial' : 'Closed bucket'} · {b.is_active ? 'Active' : 'Local observations only'}</td>
            </tr>)}</tbody></table></details>
          <p>Live status is heuristic relative to the latest local request, not proof a process is running. Last sync is the latest successful local source checkpoint for this harness; absent checkpoints stay unavailable. Values are USD and may cover only priced requests.</p>
          <div className="burn-live-container">
            <div className="burn-stats-panel">
              <div className="burn-stat-item">
                <span className="burn-stat-label">Current Burn Velocity</span>
                <span className="burn-stat-val">
                  {burnRate?.current_burn_rate_tpm !== null && burnRate?.current_burn_rate_tpm !== undefined
                    ? `${Math.round(burnRate.current_burn_rate_tpm).toLocaleString()} TPM`
                    : <span className="unavailable-metric">Unavailable (No Active Traffic)</span>}
                </span>
              </div>
              <div className="burn-stat-item">
                <span className="burn-stat-label">Burn Cost Velocity</span>
                <span className="burn-stat-val" style={{ color: 'var(--accent-amber)' }}>
                  {burnRate?.current_burn_rate_cost_per_hour !== null && burnRate?.current_burn_rate_cost_per_hour !== undefined
                    ? `${formatUsdAsInr(burnRate.current_burn_rate_cost_per_hour)}/hr`
                    : <span className="unavailable-metric">N/A</span>}
                </span>
              </div>
              <div className="burn-stat-item">
                <span className="burn-stat-label">Typical Baseline</span>
                <span className="burn-stat-val" style={{ fontSize: '14px', color: 'var(--text-secondary)' }}>
                  {burnRate?.typical_burn_rate_tpm !== null && burnRate?.typical_burn_rate_tpm !== undefined
                    ? `${Math.round(burnRate.typical_burn_rate_tpm).toLocaleString()} TPM`
                    : 'No Historical Baseline Established'}
                </span>
              </div>
              <div className="burn-stat-item">
                <span className="burn-stat-label">Burn Status</span>
                <span className={`budget-status-badge status-${burnRate?.burn_status === 'spike' ? 'breached' : burnRate?.burn_status === 'elevated' ? 'warning' : 'ok'}`}>
                  {burnRate?.burn_status ?? 'normal'}
                </span>
              </div>
            </div>

            <div className="active-sessions-table-wrapper">
              <table className="data-table">
                <thead>
                  <tr>
                    <th>Session ID</th>
                    <th>Harness</th>
                    <th>Active Model</th>
                    <th className="numeric-cell">Tokens So Far</th>
                    <th className="numeric-cell">Burn Rate</th>
                    <th>Last Activity</th>
                    <th>Estimated Value (INR)</th>
                    <th>Last Sync (harness source)</th>
                    <th>Status</th>
                  </tr>
                </thead>
                <tbody>
                  {burnRate?.active_sessions && burnRate.active_sessions.length > 0 ? (
                    burnRate.active_sessions.map((s, idx) => (
                      <tr key={idx}>
                        <td style={{ fontFamily: 'var(--font-mono)' }}>{s.session_id.slice(0, 14)}...</td>
                        <td>
                          <span className="badge-tag">{s.origin_harness}</span>
                        </td>
                        <td>{s.active_model}</td>
                        <td className="numeric-cell">{formatCount(s.tokens_so_far)}</td>
                        <td className="numeric-cell">
                          {s.burn_rate_tpm == null ? 'Unavailable' : `${Math.round(s.burn_rate_tpm)} TPM`}
                        </td>
                        <td>{s.last_observed_time || 'Unavailable'}</td>
                        <td>{formatUsdAsInr(s.estimated_value_usd, 2, 'Unavailable')} · {s.value_coverage ?? 'Coverage unavailable'}</td>
                        <td>{s.last_sync_timestamp ?? 'Unavailable'}</td>
                        <td>
                          <span className={`budget-status-badge status-${s.status === 'active' ? 'ok' : 'warning'}`}>
                            {s.status}
                          </span>
                        </td>
                      </tr>
                    ))
                  ) : (
                    <tr>
                      <td colSpan={9} style={{ textAlign: 'center', padding: '24px', color: 'var(--text-muted)' }}>
                        No active live sessions in the current declared interval.
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          </div>
        </div>
      </div>

      {/* Provider & Access Route Breakdown (WP16 item 21) */}
      <div className="content-section">
        <div className="section-header">
          <h2>Provider & Access-Route Breakdown</h2>
          <div className="section-actions">
            <input
              type="text"
              className="form-input"
              placeholder="Search route, provider, model..."
              value={searchFilter}
              onChange={(e) => setSearchFilter(e.target.value)}
              style={{ width: '220px', padding: '4px 8px' }}
            />
          </div>
        </div>
        <div className="section-body">
          <p style={{ fontSize: '11px', color: 'var(--text-secondary)', marginBottom: '12px' }}>
            * Provider route identity is maintained independently of model identity. A model accessed via multiple routes remains distinguishable. Click any provider or route tag to toggle global filters.
          </p>
          <div style={{ overflowX: 'auto' }}>
            <table className="data-table">
              <thead>
                <tr>
                  <th>Provider</th>
                  <th>Access Route</th>
                  <th>Observed Models</th>
                  <th className="numeric-cell">Requests</th>
                  <th className="numeric-cell">Fresh Input</th>
                  <th className="numeric-cell">Cache Read</th>
                  <th className="numeric-cell">Cache Write</th>
                  <th className="numeric-cell">Output</th>
                  <th className="numeric-cell">Total Tokens</th>
                  <th className="numeric-cell">API Equivalent</th>
                  <th className="numeric-cell">Unpriced</th>
                </tr>
              </thead>
              <tbody>
                {filteredRows.length > 0 ? (
                  filteredRows.map((r, idx) => (
                    <tr key={idx}>
                      <td>
                        <button
                          type="button"
                          className="badge-tag"
                          style={{ cursor: 'pointer', background: 'transparent' }}
                          onClick={() => handleFilterClick('provider', r.provider)}
                        >
                          {r.provider}
                        </button>
                      </td>
                      <td>
                        <button
                          type="button"
                          className="badge-tag"
                          style={{ cursor: 'pointer', fontFamily: 'var(--font-mono)' }}
                          onClick={() => handleFilterClick('route', r.access_route)}
                        >
                          {r.access_route}
                        </button>
                        {r.account_alias && (
                          <span style={{ fontSize: '10px', color: 'var(--text-muted)', marginLeft: '4px' }}>
                            ({r.account_alias})
                          </span>
                        )}
                      </td>
                      <td>
                        <div style={{ display: 'flex', flexWrap: 'wrap', gap: '4px', maxWidth: '240px' }}>
                          {r.models.map((m, mIdx) => (
                            <span key={mIdx} className="badge-tag" style={{ fontSize: '10px' }}>
                              {m}
                            </span>
                          ))}
                        </div>
                      </td>
                      <td className="numeric-cell">{formatCount(r.request_count)}</td>
                      <td className="numeric-cell">{formatCount(r.fresh_input_tokens)} <CoverageBadge status={r.token_fields_status.fresh_input_tokens} /></td>
                      <td className="numeric-cell">{formatCount(r.cache_read_tokens)} <CoverageBadge status={r.token_fields_status.cache_read_tokens} /></td>
                      <td className="numeric-cell">{formatCount(r.cache_write_tokens)} <CoverageBadge status={r.token_fields_status.cache_write_tokens} /></td>
                      <td className="numeric-cell">{formatCount(r.output_tokens)} <CoverageBadge status={r.token_fields_status.output_tokens} /></td>
                      <td className="numeric-cell" style={{ fontWeight: 700 }}>
                        {formatCount(r.total_tokens)} <CoverageBadge status={r.total_tokens_status} />
                      </td>
                      <td className="numeric-cell" style={{ color: 'var(--accent-cyan)' }}>
                        {selectedCurrency === 'USD'
                          ? formatMoney(r.api_equivalent_usd, 'USD')
                          : financialSummary?.fx_rate_usd_to_inr !== null && financialSummary?.fx_rate_usd_to_inr !== undefined
                            ? formatMoney(r.api_equivalent_inr, 'INR')
                            : 'Unavailable (no dated FX basis)'} <CoverageBadge status={selectedCurrency === 'USD' ? r.api_equivalent_usd_status : financialSummary?.fx_rate_usd_to_inr !== null && financialSummary?.fx_rate_usd_to_inr !== undefined ? r.api_equivalent_inr_status : 'unavailable'} />
                      </td>
                      <td className="numeric-cell" style={{ color: r.unpriced_request_count !== null && r.unpriced_request_count > 0 ? 'var(--accent-amber)' : 'inherit' }}>
                        {formatCount(r.unpriced_request_count)}
                      </td>
                    </tr>
                  ))
                ) : (
                  <tr>
                    <td colSpan={11} style={{ textAlign: 'center', padding: '24px', color: 'var(--text-muted)' }}>
                      {loading ? 'Loading provider routes...' : 'No route records match active filters.'}
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </div>
      </div>

      {/* Usage Budgets & Guardrails (WP16 item 26) */}
      <div className="content-section">
        <div className="section-header">
          <h2>Usage Budgets & Guardrails</h2>
          <span className="badge-tag">Locally Enforced Thresholds</span>
        </div>
        <div className="section-body">
          <div className="budget-controls-grid">
            {/* Existing budgets list */}
            <div className="budget-list">
              <h3 style={{ fontSize: '12px', color: 'var(--text-secondary)', textTransform: 'uppercase' }}>
                Active Budget Rules ({budgets.length})
              </h3>
              {budgets.length > 0 ? (
                budgets.map((b) => (
                  <div key={b.id} className="budget-item-card">
                    <div className="budget-header">
                      <span className="budget-title">
                        <span>{b.period.toUpperCase()}</span>
                        <span className="badge-tag">{b.target_type}</span>
                        {b.scope?.project_id && (
                          <span className="badge-tag">Project: {b.scope.project_id}</span>
                        )}
                        {b.scope?.model && <span className="badge-tag">Model: {b.scope.model}</span>}
                      </span>
                      <button
                        type="button"
                        className="btn-secondary budget-delete-button"
                        aria-label={`Delete ${b.name}`}
                        onClick={() => void handleDeleteBudget(b)}
                      >
                        Delete
                      </button>
                      <span className={`budget-status-badge status-${b.status}`}>
                        {b.status} ({b.percent_consumed.toFixed(1)}%)
                      </span>
                    </div>
                    <div className="quota-progress-bar">
                      <div
                        className={`quota-progress-fill ${
                          b.percent_consumed >= 100
                            ? 'fill-rose'
                            : b.percent_consumed >= 80
                            ? 'fill-amber'
                            : 'fill-emerald'
                        }`}
                        style={{ width: `${Math.min(100, Math.max(0, b.percent_consumed))}%` }}
                      />
                    </div>
                    <div className="quota-stats-row">
                      <span>Consumed: {b.current_value.toLocaleString()}</span>
                      <span>Threshold: {b.threshold.toLocaleString()}</span>
                      <span>
                        Notifications: {b.notifications_enabled ? 'Consent Granted' : 'Disabled'}
                      </span>
                    </div>
                    {b.period === 'session' && <p>No baseline: comparable task labels and adequate matched samples are not established. This threshold is an absolute guardrail, not a task efficiency comparison.</p>}
                  </div>
                ))
              ) : (
                <div className="quota-unavailable-box">
                  <strong>No budgets configured</strong>
                  <span>Create threshold rules below to monitor daily, monthly, or session usage.</span>
                </div>
              )}
            </div>

            {/* Create budget form */}
            <form className="budget-form-box" onSubmit={handleAddBudget}>
              <h3 style={{ fontSize: '13px', fontWeight: 600, color: '#fff' }}>Configure Budget Rule</h3>

              {budgetSuccessMsg && (
                <div style={{ padding: '6px 10px', background: 'rgba(16, 185, 129, 0.15)', color: '#34d399', borderRadius: '4px', fontSize: '11px' }}>
                  {budgetSuccessMsg}
                </div>
              )}
              {budgetErrorMsg && (
                <div role="alert" style={{ padding: '6px 10px', background: 'rgba(244, 63, 94, 0.15)', color: '#fb7185', borderRadius: '4px', fontSize: '11px' }}>
                  {budgetErrorMsg}
                </div>
              )}

              <div className="form-group">
                <label className="form-label" htmlFor="budget-period-select">Period</label>
                <select
                  id="budget-period-select"
                  className="form-select"
                  value={newBudgetPeriod}
                  onChange={(e) => {
                    const val = e.target.value;
                    if (val === 'daily' || val === 'monthly' || val === 'session' || val === 'project_model') {
                      setNewBudgetPeriod(val);
                    }
                  }}
                >
                  <option value="daily">Daily</option>
                  <option value="monthly">Monthly</option>
                  <option value="session">Per Session</option>
                  <option value="project_model">Project / Model</option>
                </select>
              </div>

              <div className="form-group">
                <label className="form-label" htmlFor="budget-target-select">Metric Target</label>
                <select
                  id="budget-target-select"
                  className="form-select"
                  value={newBudgetType}
                  onChange={(e) => {
                    const val = e.target.value;
                    if (val === 'tokens' || val === 'cost_usd' || val === 'cost_inr') {
                      setNewBudgetType(val);
                    }
                  }}
                >
                  <option value="tokens">Tokens Count</option>
                  <option value="cost_usd">API Equivalent (USD)</option>
                  <option value="cost_inr">API Equivalent (INR)</option>
                </select>
              </div>

              <div className="form-group">
                <label className="form-label" htmlFor="budget-threshold-input">Threshold Value</label>
                <input
                  id="budget-threshold-input"
                  type="number"
                  className="form-input"
                  value={newBudgetThreshold}
                  onChange={(e) => setNewBudgetThreshold(e.target.value)}
                  placeholder="e.g. 1000000"
                  required
                />
              </div>

              <div className="form-group">
                <label className="form-label" htmlFor="budget-scope-type">Scope kind</label>
                <select id="budget-scope-type" className="form-select" value={newBudgetScopeType} onChange={e => setNewBudgetScopeType(e.target.value as 'project_id' | 'model')}>
                  <option value="project_id">Project</option><option value="model">Model</option>
                </select>
                <label className="form-label" htmlFor="budget-scope-input">Optional {newBudgetScopeType === 'model' ? 'model identity' : 'project ID'}</label>
                <input
                  id="budget-scope-input"
                  type="text"
                  className="form-input"
                  value={newBudgetScope}
                  onChange={(e) => setNewBudgetScope(e.target.value)}
                  placeholder="e.g. my-project-id"
                />
              </div>

              <div className="consent-checkbox-row">
                <input
                  type="checkbox"
                  id="notifConsent"
                  checked={notificationConsent}
                  onChange={(e) => setNotificationConsent(e.target.checked)}
                />
                <label htmlFor="notifConsent">
                  Enable local notifications (Requires explicit consent. No external data telemetry sent).
                </label>
              </div>

              <button type="submit" className="btn-primary" style={{ marginTop: '8px' }}>
                Save Budget Rule
              </button>
            </form>
          </div>
        </div>
      </div>
    </div>
  );
}
