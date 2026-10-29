import React, { useState, useEffect } from 'react';
import OutcomeEditor from '../components/OutcomeEditor';
import './Explorers.css';
import { formatUsdAsInr, formatMoneyInr, formatUsdSecondary, formatInr } from '../lib/money';
type CoverageStatus = 'exact' | 'partial' | 'unavailable';

function CoverageBadge({ status }: { status?: CoverageStatus }) {
  // Partial coverage renders no badge; only Unavailable is shown.
  if (status !== 'unavailable') return null;
  return <span className="badge-tag badge-gray">Unavailable</span>;
}

function formatCount(value: number | null | undefined): string {
  return value === null || value === undefined ? '—' : value.toLocaleString();
}

function formatCost(value: number | null | undefined, fractionDigits: number): string {
  return formatUsdAsInr(value, fractionDigits, 'Unpriced');
}

export interface SessionRecord {
  session_id: string;
  origin_harness: string;
  started_at: string | null;
  ended_at?: string | null;
  duration_ms?: number | null;
  status: string;
  is_subagent: boolean;
  parent_session_id?: string | null;
  project_name?: string | null;
  requests_count?: number | null;
  total_tokens?: number | null;
  total_tokens_status?: CoverageStatus;
  api_equivalent_usd?: number | null;
  api_equivalent_usd_status?: CoverageStatus;
  models_used: string[];
}

export interface RequestRecord {
  canonical_id: string;
  request_timestamp: string;
  provider: string;
  origin_harness: string;
  access_route: string;
  raw_model: string;
  normalized_model: string;
  session_id: string;
  project_id?: string | null;
  request_status: string;
  timing_basis: string;
  duration_ms?: number | null;
  ttft_ms?: number | null;
  generation_duration_ms?: number | null;
  generation_speed_tps?: number | null;
  is_unresolved_overlap: boolean;
  fresh_input_tokens?: number | null;
  cache_read_tokens?: number | null;
  cache_write_5m_tokens?: number | null;
  cache_write_1h_tokens?: number | null;
  cache_write_unknown_ttl_tokens?: number | null;
  output_tokens?: number | null;
  reasoning_tokens?: number | null;
  total_tokens?: number | null;
  pricing_status: string;
  api_equivalent_usd?: number | null;
  api_equivalent_inr?: number | null;
  cache_savings_usd?: number | null;
  valuation_pricing_version_id?: string | null;
  valuation_provenance?: Record<string, unknown> | null;
}

export interface EventRecord {
  event_id: string;
  session_id: string;
  canonical_id?: string | null;
  parent_event_id?: string | null;
  started_at?: string | null;
  ended_at?: string | null;
  duration_ms?: number | null;
  output_size_bytes?: number | null;
  event_kind: string;
  event_name: string;
  status?: string;
  details?: Record<string, unknown>;
}

interface PromptAttribution {
  prompt_id: string;
  session_id?: string | null;
  prompt_group?: string | null;
  requests_count: number;
  total_tokens: number;
}

interface SessionTreeNode {
  session_id: string;
  parent_session_id?: string | null;
  children_session_ids?: string[];
  session_name?: string;
  is_subagent?: boolean;
  exclusive_requests_count?: number;
  exclusive_tokens?: number;
  inclusive_requests_count?: number;
  inclusive_tokens?: number;
}

interface ToolStat {
  tool_name: string;
  invocations_count: number;
  timed_invocations_count?: number;
  total_duration_ms?: number | null;
  success_count?: number;
  error_count?: number;
  total_output_size_bytes?: number | null;
  output_size_observed_count?: number;
}

interface CompactionEvent {
  event_id: string;
  session_id?: string;
  timestamp?: string | null;
  before_tokens?: number | null;
  after_tokens?: number | null;
  reduction_tokens?: number | null;
  reduction_ratio?: number | null;
  turns_compacted?: number | null;
}

interface CompactionAnalysis {
  status?: string;
  compaction_events_count?: number;
  total_reduction_tokens?: number | null;
  events?: CompactionEvent[];
}

interface TraceAnalysisResponse {
  rows?: unknown[];
  values?: Record<string, unknown>;
  eligible?: number | Record<string, unknown>;
  observed?: number | Record<string, unknown>;
  warnings?: string[];
  events?: EventRecord[];
  event_counts_by_kind?: Record<string, number>;
  tools_breakdown?: ToolStat[];
  session_nodes?: SessionTreeNode[];
  prompt_groups?: PromptAttribution[];
  context_occupancy?: {
    window_size?: number | null;
    peak_occupancy_tokens?: number | null;
    peak_occupancy_ratio?: number | null;
    remaining_headroom_tokens?: number | null;
    cumulative_input_tokens?: number | null;
    per_turn_history?: Array<{
      session_id: string;
      turn_index: number;
      canonical_id?: string | null;
      timestamp?: string | null;
      reported_context_tokens: number;
      context_window_size?: number | null;
      occupancy_ratio?: number | null;
      remaining_tokens?: number | null;
      cumulative_input_proxy: number;
      input_proxy_growth_tokens: number;
    }>;
    unobserved_window_requests_count?: number;
  };
  event_scope?: string;
  session_level_event_scope?: string;
  compaction?: CompactionAnalysis;
  session_level_analysis?: { compaction?: CompactionAnalysis };
  retry_loops?: {
    retry_requests_count?: number;
    retry_tokens?: number;
    retry_sequences_count?: number;
    suspected_waste_signals?: Array<Record<string, unknown>>;
  };
  repetition_signals?: {
    opt_in_content_fingerprints?: boolean;
    repeated_reads_count?: number;
    signals?: Array<{
      path_alias: string;
      read_count: number;
      evidence_state: string;
      evidence_basis?: string;
      classification: string;
    }>;
  };
  subagent_events?: EventRecord[];
  mcp_events?: EventRecord[];
  skills_events?: EventRecord[];
  hooks_events?: EventRecord[];
  source_coverage?: Record<string, string>;
  missingness?: Record<string, number>;
}

type EventTreeNode = EventRecord & { children: EventTreeNode[] };

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
    request: (metric_id: string, overrides?: Record<string, unknown>) => Promise<TraceAnalysisResponse>;
    write: (resource: string, payload: unknown, method?: string) => Promise<unknown>;
    bootstrap: unknown;
  };
}

function isRequestRecord(value: unknown): value is RequestRecord {
  return typeof value === 'object' && value !== null
    && 'canonical_id' in value && typeof value.canonical_id === 'string'
    && 'session_id' in value && typeof value.session_id === 'string';
}

function isSessionRecord(value: unknown): value is SessionRecord {
  return typeof value === 'object' && value !== null
    && 'session_id' in value && typeof value.session_id === 'string'
    && 'origin_harness' in value && typeof value.origin_harness === 'string'
    && 'started_at' in value && (value.started_at === null || typeof value.started_at === 'string')
    && 'status' in value && typeof value.status === 'string'
    && 'is_subagent' in value && typeof value.is_subagent === 'boolean'
    && (!('requests_count' in value) || value.requests_count === null || value.requests_count === undefined || typeof value.requests_count === 'number')
    && (!('total_tokens' in value) || value.total_tokens === null || value.total_tokens === undefined || typeof value.total_tokens === 'number')
    && 'models_used' in value && Array.isArray(value.models_used)
    && value.models_used.every((model) => typeof model === 'string');
}
function buildEventTree(events: EventRecord[]): { roots: EventTreeNode[]; missingParentIds: string[] } {
  const nodes = events.map((event) => ({ ...event, children: [] as EventTreeNode[] }));
  const byId = new Map(nodes.map((node) => [node.event_id, node]));
  const roots: EventTreeNode[] = [];
  const missingParentIds: string[] = [];

  for (const node of nodes) {
    const parentId = node.parent_event_id;
    if (!parentId) {
      roots.push(node);
      continue;
    }
    const parent = byId.get(parentId);
    if (!parent || parent === node) {
      roots.push(node);
      if (!parent) missingParentIds.push(parentId);
      continue;
    }
    parent.children.push(node);
  }

  const reachable = new Set<string>();
  const markReachable = (node: EventTreeNode) => {
    if (reachable.has(node.event_id)) return;
    reachable.add(node.event_id);
    node.children.forEach(markReachable);
  };
  roots.forEach(markReachable);
  nodes.forEach((node) => {
    if (!reachable.has(node.event_id)) roots.push(node);
  });
  return { roots, missingParentIds };
}

function renderTraceEventNode(node: EventTreeNode, ancestorIds: ReadonlySet<string> = new Set()): React.ReactNode {
  if (ancestorIds.has(node.event_id)) {
    return <div key={`cycle-${node.event_id}`} style={{ color: '#fbbf24', fontSize: '10px' }}>Cyclic event parent link: {node.event_id}</div>;
  }
  const nextAncestors = new Set(ancestorIds);
  nextAncestors.add(node.event_id);
  const detailLabels: Record<string, string> = {
    tool_id: 'Tool call ID',
    tool_name: 'Tool',
    name: 'Source name',
    command_type: 'Command type',
    path: 'Path alias',
    before_tokens: 'Before tokens',
    after_tokens: 'After tokens',
    reduction_tokens: 'Reduction tokens',
    turns_compacted: 'Turns compacted',
    parent_tool_use_id: 'Parent tool-use ID',
    is_error: 'Source error flag',
    content_changed: 'Content changed evidence',
    changed_since_previous_read: 'Change since prior read',
    source_change_state: 'Source change state',
  };
  const visibleDetails = Object.entries(node.details ?? {}).filter(([key, value]) =>
    key in detailLabels && (typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean')
  );

  return (
    <details
      key={node.event_id}
      id={`event-ref-${node.event_id}`}
      style={{ padding: '5px 0' }}
    >
      <summary style={{ cursor: 'pointer', color: '#e2e8f0', fontSize: '11px' }}>
        <span style={{ color: '#38bdf8' }}>{node.event_kind}</span>
        {' · '}{node.event_name}
        <span style={{ color: '#94a3b8', marginLeft: 8 }}>
          {node.status || 'status unavailable'} · {formatCount(node.duration_ms)}ms
          {node.output_size_bytes === null || node.output_size_bytes === undefined
            ? '' : ` · ${formatCount(node.output_size_bytes)} output bytes`}
        </span>
        {node.children.length > 0 && <span style={{ color: '#64748b', marginLeft: 8 }}>({node.children.length} linked)</span>}
      </summary>
      <div style={{ margin: '6px 0 2px 12px', paddingLeft: 10, borderLeft: '1px solid var(--border-subtle)', fontSize: '10px', color: '#94a3b8' }}>
        <div>Event {node.event_id}{node.parent_event_id ? ` · parent ${node.parent_event_id}` : ''}</div>
        <div>{node.started_at || 'Time unavailable'}{node.canonical_id ? ` · request ${node.canonical_id}` : ''}</div>
        {visibleDetails.map(([key, value]) => (
          <div key={key}>{detailLabels[key]}: <strong style={{ color: '#cbd5e1' }}>{String(value)}</strong></div>
        ))}
        {node.children.map((child) => renderTraceEventNode(child, nextAncestors))}
      </div>
    </details>
  );
}

function TraceEventTree({ events, emptyLabel }: { events: EventRecord[]; emptyLabel: string }) {
  const { roots, missingParentIds } = buildEventTree(events);
  if (events.length === 0) {
    return <div style={{ padding: '10px 0', color: '#64748b', fontSize: '11px' }}>{emptyLabel}</div>;
  }
  return (
    <div style={{ maxHeight: 380, overflow: 'auto', paddingRight: 4 }}>
      {missingParentIds.length > 0 && (
        <div className="privacy-banner" style={{ marginBottom: 8 }}>
          {missingParentIds.length} event parent link(s) point outside this trace result.
        </div>
      )}
      {roots.map((node) => renderTraceEventNode(node))}
    </div>
  );
}

function SessionHierarchy({
  nodes,
  missingParents,
}: {
  nodes: SessionTreeNode[];
  missingParents: number;
}) {
  if (nodes.length === 0) {
    return <div style={{ padding: '10px 0', color: '#64748b', fontSize: '11px' }}>No session hierarchy is available in this trace scope.</div>;
  }
  const byId = new Map(nodes.map((node) => [node.session_id, node]));
  const roots = nodes.filter((node) => !node.parent_session_id || !byId.has(node.parent_session_id));
  const visited = new Set<string>();
  const renderNode = (node: SessionTreeNode): React.ReactNode => {
    if (visited.has(node.session_id)) return null;
    visited.add(node.session_id);
    const children = (node.children_session_ids ?? [])
      .map((id) => byId.get(id))
      .filter((child): child is SessionTreeNode => Boolean(child));
    return (
      <details key={node.session_id} style={{ padding: '5px 0' }}>
        <summary style={{ cursor: 'pointer', color: '#e2e8f0', fontSize: '11px' }}>
          {node.session_name || node.session_id}
          {node.is_subagent && <span className="badge-tag badge-purple" style={{ marginLeft: 6 }}>Subagent</span>}
          <span style={{ color: '#94a3b8', marginLeft: 8 }}>
            direct {formatCount(node.exclusive_tokens)} tokens / {formatCount(node.exclusive_requests_count)} req
            {' · '}inclusive {formatCount(node.inclusive_tokens)} tokens / {formatCount(node.inclusive_requests_count)} req
          </span>
        </summary>
        <div style={{ marginLeft: 12, paddingLeft: 10, borderLeft: '1px solid var(--border-subtle)' }}>
          {children.map(renderNode)}
        </div>
      </details>
    );
  };
  const treeRoots = roots.length > 0 ? roots : nodes;
  return (
    <div>
      {missingParents > 0 && (
        <div className="privacy-banner" style={{ marginBottom: 8 }}>
          {missingParents} parent session link(s) reference sessions outside this query result.
        </div>
      )}
      {treeRoots.map(renderNode)}
      {nodes.filter((node) => !visited.has(node.session_id)).map(renderNode)}
    </div>
  );
}

function ratioPercent(value?: number | null): string {
  return value === null || value === undefined ? '—' : `${(value * 100).toFixed(2)}%`;
}

function TraceAnalyticsPanels({ trace }: { trace: TraceAnalysisResponse | null }) {
  if (!trace) {
    return <div style={{ color: '#f59e0b', fontSize: '11px' }}>Trace analysis could not be loaded for this session.</div>;
  }
  const context = trace.context_occupancy;
  const turns = context?.per_turn_history ?? [];
  const prompts = trace.prompt_groups ?? [];
  const tools = trace.tools_breakdown ?? [];
  const nodes = trace.session_nodes ?? [];
  const missing = trace.missingness ?? {};
  const retrySignals = trace.retry_loops?.suspected_waste_signals ?? [];
  const repeatedReads = trace.repetition_signals?.signals ?? [];
  const coverage = trace.source_coverage ?? {};
  const sourceEventGroups = [
    ['Subagent events', trace.subagent_events ?? [], coverage.subagents],
    ['MCP events', trace.mcp_events ?? [], coverage.mcp_tools],
    ['Skill events', trace.skills_events ?? [], coverage.skills_and_hooks],
    ['Hook events', trace.hooks_events ?? [], coverage.skills_and_hooks],
  ] as const;

  return (
    <div style={{ maxHeight: 440, overflowY: 'auto', display: 'flex', flexDirection: 'column', gap: 6, paddingRight: 3 }}>
      <details>
        <summary style={{ cursor: 'pointer' }}>
          Context occupancy and input volume proxy ({turns.length} turns)
        </summary>
        <div style={{ padding: '8px 4px', fontSize: '10px', color: '#cbd5e1' }}>
          <div>
            Peak occupancy: {formatCount(context?.peak_occupancy_tokens)} tokens ({ratioPercent(context?.peak_occupancy_ratio)})
            {' · '}remaining: {formatCount(context?.remaining_headroom_tokens)}
            {' · '}window: {formatCount(context?.window_size)}
          </div>
          <div style={{ marginTop: 3 }}>
            Cumulative input volume proxy: {formatCount(context?.cumulative_input_tokens)} tokens. This is billed input accumulation (proxy), not measured context window occupancy.
          </div>
          {(context?.unobserved_window_requests_count ?? 0) > 0 && (
            <div style={{ color: '#fbbf24', marginTop: 3 }}>
              Context window unknown for {formatCount(context?.unobserved_window_requests_count)} request(s); occupancy and remaining capacity are unavailable for those turns.
            </div>
          )}
          <div style={{ overflowX: 'auto', marginTop: 8 }}>
            <table className="explorer-table">
              <thead><tr><th>Turn</th><th>Reported occupancy</th><th>Occupancy</th><th>Remaining</th><th>Input volume proxy</th><th>Proxy growth</th></tr></thead>
              <tbody>
                {turns.map((turn) => (
                  <tr key={`${turn.session_id}-${turn.turn_index}-${turn.canonical_id ?? ''}`}>
                    <td>{turn.turn_index + 1} · {turn.canonical_id || turn.session_id}</td>
                    <td className="num-cell">{formatCount(turn.reported_context_tokens)}</td>
                    <td className="num-cell">{ratioPercent(turn.occupancy_ratio)}</td>
                    <td className="num-cell">{formatCount(turn.remaining_tokens)}</td>
                    <td className="num-cell">{formatCount(turn.cumulative_input_proxy)}</td>
                    <td className="num-cell">{formatCount(turn.input_proxy_growth_tokens)}</td>
                  </tr>
                ))}
                {turns.length === 0 && <tr><td colSpan={6}>No per-turn context observations.</td></tr>}
              </tbody>
            </table>
          </div>
        </div>
      </details>

      <details>
        <summary style={{ cursor: 'pointer' }}>
          Prompt attribution ({prompts.length} linked prompts · {formatCount(missing.unlinked_prompt_requests_count)} unlinked requests)
        </summary>
        <div style={{ padding: '8px 4px', fontSize: '10px' }}>
          <div style={{ color: '#cbd5e1', marginBottom: 6 }}>
            Prompt rows are direct source-linked totals. The current trace records do not declare primary/follow-up prompt relationships or prompt-parent rollups, so those categories are not inferred.
            Unlinked prompt tokens: {formatCount(missing.unlinked_prompt_tokens)}.
          </div>
          <div style={{ overflowX: 'auto' }}>
            <table className="explorer-table">
              <thead><tr><th>Prompt ID</th><th>Source group</th><th>Session</th><th>Requests</th><th>Tokens</th></tr></thead>
              <tbody>
                {prompts.map((prompt) => (
                  <tr key={prompt.prompt_id}>
                    <td>{prompt.prompt_id}</td><td>{prompt.prompt_group || 'Unspecified'}</td>
                    <td>{prompt.session_id || 'Unlinked'}</td>
                    <td className="num-cell">{formatCount(prompt.requests_count)}</td>
                    <td className="num-cell">{formatCount(prompt.total_tokens)}</td>
                  </tr>
                ))}
                {prompts.length === 0 && <tr><td colSpan={5}>No linked prompt attribution in this scope.</td></tr>}
              </tbody>
            </table>
          </div>
        </div>
      </details>

      <details>
        <summary style={{ cursor: 'pointer' }}>Tool statistics ({tools.length} original tool names)</summary>
        <div style={{ padding: '8px 4px', overflowX: 'auto' }}>
          <table className="explorer-table">
            <thead><tr><th>Tool name</th><th>Calls</th><th>Timed</th><th>Total duration</th><th>Success / failure</th><th>Output bytes</th><th>Sizes observed</th></tr></thead>
            <tbody>
              {tools.map((tool) => (
                <tr key={`${tool.tool_name}-${tool.invocations_count}`}>
                  <td>{tool.tool_name}</td>
                  <td className="num-cell">{formatCount(tool.invocations_count)}</td>
                  <td className="num-cell">{formatCount(tool.timed_invocations_count)}</td>
                  <td className="num-cell">{formatCount(tool.total_duration_ms)}ms</td>
                  <td className="num-cell">{formatCount(tool.success_count)} / {formatCount(tool.error_count)}</td>
                  <td className="num-cell">{formatCount(tool.total_output_size_bytes)}</td>
                  <td className="num-cell">{formatCount(tool.output_size_observed_count)}</td>
                </tr>
              ))}
              {tools.length === 0 && <tr><td colSpan={7}>No tool calls were observed.</td></tr>}
            </tbody>
          </table>
          <div style={{ color: '#64748b', fontSize: '10px', marginTop: 5 }}>
            Output size is reported from source measurements; missing measurements are not treated as zero.
          </div>
        </div>
      </details>

      <details>
        <summary style={{ cursor: 'pointer' }}>
          Session and subagent hierarchy ({nodes.filter((node) => node.is_subagent).length} subagents)
        </summary>
        <div style={{ padding: '8px 4px', fontSize: '10px', color: '#94a3b8' }}>
          Inclusive totals include descendants; do not add them to exclusive totals.
          <SessionHierarchy nodes={nodes} missingParents={missing.missing_parent_sessions_count ?? 0} />
        </div>
      </details>

      {([
        {
          label: 'Filtered-request compaction',
          analysis: trace.compaction,
          scope: trace.event_scope,
          description: 'Only events linked to the selected requests. Compaction linked to excluded requests is not included.',
        },
        {
          label: 'Standalone session-level compaction',
          analysis: trace.session_level_analysis?.compaction,
          scope: trace.session_level_event_scope,
          description: 'Unlinked events from sessions associated with the selected requests, unfiltered by request attributes or time. These are not part of filtered-request totals.',
        },
      ] as const).map(({ label, analysis, scope, description }) => {
        const events = analysis?.events ?? [];
        const unknownReductions = events.filter((event) => event.reduction_tokens == null).length;
        return (
          <details key={label}>
            <summary style={{ cursor: 'pointer' }}>
              {label} ({formatCount(analysis?.compaction_events_count)} events)
            </summary>
            <div style={{ padding: '8px 4px', fontSize: '10px' }}>
              <p>{description} Scope: {scope ?? 'unavailable'}.</p>
              <p>
                Observed reduction subtotal: {events.some((event) => event.reduction_tokens != null) ? formatCount(analysis?.total_reduction_tokens) : '—'} tokens.
                {' '}Unknown reductions: {formatCount(unknownReductions)}. Missing measurements are not zero.
              </p>
              {events.map((event) => (
                <div key={event.event_id} style={{ padding: '6px 0', borderBottom: '1px solid var(--border-subtle)' }}>
                  <strong>{event.timestamp || event.event_id}</strong>
                  {' · '}before {formatCount(event.before_tokens)}
                  {' · '}after {formatCount(event.after_tokens)}
                  {' · '}reduction {formatCount(event.reduction_tokens)}
                  {' · '}turns compacted {formatCount(event.turns_compacted)}
                </div>
              ))}
              {events.length === 0 && (
                <div>No compaction event was observed in this scope. Source-missing before/after values remain unavailable.</div>
              )}
            </div>
          </details>
        );
      })}

      <details>
        <summary style={{ cursor: 'pointer' }}>
          Retry and loop evidence ({formatCount(trace.retry_loops?.retry_sequences_count)} suspected signals)
        </summary>
        <div style={{ padding: '8px 4px', fontSize: '10px' }}>
          <div style={{ color: '#cbd5e1', marginBottom: 6 }}>
            Request signals use retry_attempt &gt; 0 or status retry/error/rate_limit/timeout. Tool-loop signals require consecutive same-name tool calls in one session where the current call failed (error/timeout/failed). Signals are suspected evidence, not proven waste.
            Retry requests: {formatCount(trace.retry_loops?.retry_requests_count)} · retry tokens: {formatCount(trace.retry_loops?.retry_tokens)}.
          </div>
          {retrySignals.map((signal, index) => (
            <div key={`${String(signal.type)}-${String(signal.event_id ?? signal.canonical_id ?? index)}`} style={{ padding: '5px 0', borderBottom: '1px solid var(--border-subtle)', color: '#cbd5e1' }}>
              <strong>{String(signal.type ?? 'signal')}</strong>
              {' · '}{String(signal.evidence ?? 'Evidence unavailable')}
              {typeof signal.canonical_id === 'string' && <span> · request {signal.canonical_id}</span>}
              {typeof signal.event_id === 'string' && <span> · event {signal.event_id}</span>}
              {typeof signal.tokens === 'number' && <span> · {formatCount(signal.tokens)} tokens</span>}
              <span> · final outcome: {typeof signal.final_outcome === 'string' ? signal.final_outcome : 'Unknown (no linked final outcome)'}</span>
            </div>
          ))}
          {retrySignals.length === 0 && <div style={{ color: '#64748b' }}>No retry or loop signals were observed under these evidence rules.</div>}
        </div>
      </details>

      <details>
        <summary style={{ cursor: 'pointer' }}>
          Repeated reads ({formatCount(trace.repetition_signals?.repeated_reads_count)} suspected groups)
        </summary>
        <div style={{ padding: '8px 4px', fontSize: '10px' }}>
          <div style={{ color: '#cbd5e1', marginBottom: 6 }}>
            Content fingerprint opt-in: {trace.repetition_signals?.opt_in_content_fingerprints ? 'on' : 'off'}.
            Path aliases alone never establish identical contents; no file bodies are displayed.
          </div>
          {repeatedReads.map((signal) => (
            <div key={`${signal.path_alias}-${signal.read_count}`} style={{ padding: '5px 0', borderBottom: '1px solid var(--border-subtle)', color: '#cbd5e1' }}>
              {signal.path_alias} · {formatCount(signal.read_count)} reads · content {signal.evidence_state} ({signal.evidence_basis || 'unknown evidence'})
              {' · '}{signal.classification}
            </div>
          ))}
          {repeatedReads.length === 0 && <div style={{ color: '#64748b' }}>No repeated path aliases were observed.</div>}
          <div style={{ color: '#64748b', marginTop: 5 }}>Repeated reads can be necessary re-verification; they are not proof of waste.</div>
        </div>
      </details>

      <details>
        <summary style={{ cursor: 'pointer' }}>
          Source event classes ({Object.values(trace.event_counts_by_kind ?? {}).reduce((sum, count) => sum + count, 0)} events)
        </summary>
        <div style={{ padding: '8px 4px', fontSize: '10px' }}>
          <div style={{ color: '#94a3b8', marginBottom: 6 }}>
            Event kinds: {Object.entries(trace.event_counts_by_kind ?? {}).map(([kind, count]) => `${kind} ${count}`).join(' · ') || 'none'}
          </div>
          {sourceEventGroups.map(([label, events, status]) => (
            <details key={label} style={{ margin: '5px 0' }}>
              <summary style={{ cursor: 'pointer' }}>
                {label} ({events.length}) · {status || 'coverage unavailable'}
              </summary>
              <TraceEventTree events={events} emptyLabel={`${label} were not observed in this source trace.`} />
            </details>
          ))}
          {coverage.missing_ingestion_path && (
            <div style={{ color: '#64748b', marginTop: 6 }}>{coverage.missing_ingestion_path}</div>
          )}
        </div>
      </details>
    </div>
  );
}

type SortField = 'started_at' | 'total_tokens' | 'api_equivalent_usd' | 'duration_ms' | 'requests_count';

export default function Sessions({ context }: PageProps) {
  const { query, setQuery, request } = context;
  const sharedContextKey = JSON.stringify(query);

  // Session table state
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);
  const [sessions, setSessions] = useState<SessionRecord[]>([]);
  const [loadedSessionsContextKey, setLoadedSessionsContextKey] = useState<string | null>(null);
  const [loadedDetailContextKey, setLoadedDetailContextKey] = useState<string | null>(null);
  const [searchQuery, setSearchQuery] = useState<string>('');
  const [modelFilter, setModelFilter] = useState('');
  const [minimumDuration, setMinimumDuration] = useState('');
  const [maximumDuration, setMaximumDuration] = useState('');
  const [sortField, setSortField] = useState<SortField>('started_at');
  const [sortOrder, setSortOrder] = useState<'asc' | 'desc'>('desc');
  const [pageOffset, setPageOffset] = useState<number>(0);
  const pageSize = 15;

  // Selected session state
  const [selectedSession, setSelectedSession] = useState<SessionRecord | null>(null);
  const [sessionRequests, setSessionRequests] = useState<RequestRecord[]>([]);
  const [sessionEvents, setSessionEvents] = useState<EventRecord[]>([]);
  const [traceAnalysis, setTraceAnalysis] = useState<TraceAnalysisResponse | null>(null);
  const [detailLoading, setDetailLoading] = useState<boolean>(false);
  const selectedDetailContextKey = selectedSession
    ? JSON.stringify([sharedContextKey, selectedSession.session_id])
    : null;
  const sessionListIsCurrent = loadedSessionsContextKey === sharedContextKey;
  const selectedSessionIsCurrent = sessionListIsCurrent
    && selectedSession !== null
    && sessions.some((session) => session.session_id === selectedSession.session_id);
  const sessionDetailsAreCurrent = selectedSessionIsCurrent
    && loadedDetailContextKey === selectedDetailContextKey;

  // Individual Request Inspector state
  const [selectedRequestIdx, setSelectedRequestIdx] = useState<number | null>(null);
  const [activeTab, setActiveTab] = useState<'requests' | 'timeline' | 'analytics' | 'inspector'>('requests');
  const [pendingEventCanonicalId, setPendingEventCanonicalId] = useState<string | null>(null);

  // Load Sessions
  useEffect(() => {
    let active = true;
    setLoading(true);
    setError(null);

    const fetchSessions = async () => {
      try {
        const res = await request('sessions', {
          sort: { field: sortField, order: sortOrder }
        });
        if (!active) return;
        const rows = (res.rows || []).filter(isSessionRecord);
        setSessions(rows);
        setLoadedSessionsContextKey(sharedContextKey);
        setSelectedSession((current) =>
          current ? rows.find((session) => session.session_id === current.session_id) ?? null : null
        );
      } catch (err: unknown) {
        if (!active) return;
        const msg = err instanceof Error ? err.message : 'Failed to load sessions';
        setError(msg);
      } finally {
        if (active) setLoading(false);
      }
    };

    fetchSessions();

    return () => {
      active = false;
    };
  }, [sharedContextKey, sortField, sortOrder]);

  useEffect(() => {
    if (!query.search) return;
    let active = true;

    const openMatchingTrace = async () => {
      const requestResult = await request('requests', {
        search: query.search,
        pagination: { limit: 100, offset: 0 },
      });
      const matchingRequest = requestResult.rows?.find(isRequestRecord);
      if (!matchingRequest || !active) return;

      const sessionResult = await request('sessions', {
        filters: { ...query.filters, session: [matchingRequest.session_id] },
        pagination: { limit: 100, offset: 0 },
      });
      const matchingSession = sessionResult.rows?.find(isSessionRecord);
      if (!matchingSession || !active) return;

      setSearchQuery(matchingSession.session_id);
      setPageOffset(0);
      setSelectedSession(matchingSession);
      setActiveTab('inspector');
    };

    void openMatchingTrace();
    return () => {
      active = false;
    };
  }, [request, query.search, sharedContextKey]);
  // Load request rows and analyzer output through the shared traces query contract.
  useEffect(() => {
    if (!selectedSession) {
      setSessionRequests([]);
      setSessionEvents([]);
      setTraceAnalysis(null);
      setSelectedRequestIdx(null);
      setLoadedDetailContextKey(null);
      setDetailLoading(false);
      return;
    }

    const selectedSessionFilter = query.filters.session;
    if (selectedSessionFilter?.length && !selectedSessionFilter.includes(selectedSession.session_id)) {
      setSessionRequests([]);
      setSessionEvents([]);
      setTraceAnalysis(null);
      setSelectedRequestIdx(null);
      setLoadedDetailContextKey(selectedDetailContextKey);
      setDetailLoading(false);
      return;
    }
    let active = true;
    setDetailLoading(true);

    const fetchSessionDetails = async () => {
      try {
        const [reqRes, traceRes] = await Promise.all([
          request('requests', {
            filters: { ...query.filters, session: [selectedSession.session_id] },
            sort: { field: 'request_timestamp', order: 'asc' },
            pagination: { limit: 200, offset: 0 }
          }).catch(() => ({ rows: [] })),
          request('traces', {
            filters: { ...query.filters, session: [selectedSession.session_id] },
            pagination: { limit: 200, offset: 0 }
          }).catch(() => null)
        ]);

        if (!active) return;
        const reqRows = (reqRes.rows || []) as RequestRecord[];
        setSessionRequests(reqRows);
        setTraceAnalysis(traceRes);
        setSessionEvents(traceRes?.events || []);
        if (reqRows.length > 0) {
          const requestedIndex = reqRows.findIndex((row) => row.canonical_id === query.search);
          setSelectedRequestIdx(requestedIndex >= 0 ? requestedIndex : 0);
          if (query.search) setActiveTab('inspector');
        } else {
          setSelectedRequestIdx(null);
        }
        setLoadedDetailContextKey(selectedDetailContextKey);
      } finally {
        if (active) setDetailLoading(false);
      }
    };

    fetchSessionDetails();

    return () => {
      active = false;
    };
  }, [selectedSession, query.search, sharedContextKey, selectedDetailContextKey]);

  const handleSort = (field: SortField) => {
    if (sortField === field) {
      setSortOrder(prev => (prev === 'asc' ? 'desc' : 'asc'));
    } else {
      setSortField(field);
      setSortOrder('desc');
    }
  };

  const filteredSessions = (sessionListIsCurrent ? sessions : []).filter(s => {
    if (modelFilter && !s.models_used.includes(modelFilter)) return false;
    if (minimumDuration !== '' && (s.duration_ms == null || s.duration_ms < Number(minimumDuration) * 60000)) return false;
    if (maximumDuration !== '' && (s.duration_ms == null || s.duration_ms > Number(maximumDuration) * 60000)) return false;
    if (!searchQuery.trim()) return true;
    const q = searchQuery.toLowerCase();
    const idMatch = s.session_id.toLowerCase().includes(q);
    const projMatch = (s.project_name || '').toLowerCase().includes(q);
    const modelMatch = (s.models_used || []).some(m => m.toLowerCase().includes(q));
    const harnessMatch = s.origin_harness.toLowerCase().includes(q);
    return idMatch || projMatch || modelMatch || harnessMatch;
  });

  const pagedSessions = filteredSessions.slice(pageOffset, pageOffset + pageSize);

  const selectedRequest = selectedRequestIdx !== null && sessionRequests[selectedRequestIdx]
    ? sessionRequests[selectedRequestIdx]
    : null;

  const navigateRequest = (step: number) => {
    if (selectedRequestIdx === null) return;
    const newIdx = selectedRequestIdx + step;
    if (newIdx >= 0 && newIdx < sessionRequests.length) {
      setSelectedRequestIdx(newIdx);
      setActiveTab('inspector');
    }
  };

  const jumpToEvent = (canonicalId: string) => {
    setPendingEventCanonicalId(canonicalId);
    setActiveTab('timeline');
  };

  useEffect(() => {
    if (activeTab !== 'timeline' || pendingEventCanonicalId === null) return;
    const event = sessionEvents.find((candidate) => candidate.canonical_id === pendingEventCanonicalId);
    if (!event) {
      setPendingEventCanonicalId(null);
      return;
    }
    const eventElement = document.getElementById(`event-ref-${event.event_id}`) as HTMLDetailsElement | null;
    if (!eventElement) return;
    eventElement.open = true;
    eventElement.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    setPendingEventCanonicalId(null);
  }, [activeTab, pendingEventCanonicalId, sessionEvents]);

  return (
    <div className="page-container">
      {/* Title */}
      <div className="page-header-row">
        <div className="page-title-group">
          <h2>Advanced Session Explorer & Request Inspector</h2>
          <div className="page-subtitle">
            Searchable session index, chronological execution traces, and canonical request inspection
          </div>
        </div>

        <div className="header-action-group">
          <span className="provenance-tag">Total Sessions: {sessionListIsCurrent ? sessions.length.toLocaleString() : 'Refreshing…'}</span>
          <span className="provenance-tag">Scope: {query.harness_scope}</span>
        </div>
      </div>

      {error && (
        <div className="banner-alert error">
          <span>Error loading sessions: {error}</span>
        </div>
      )}

      {/* Control bar */}
      <div className="control-toolbar">
        <div className="control-group">
          <div className="search-input-pill">
            <span style={{ color: '#64748b' }}>🔍</span>
            <input
              type="text"
              aria-label="Search sessions"
              placeholder="Search session ID, project, model..."
              value={searchQuery}
              onChange={e => {
                setSearchQuery(e.target.value);
                setPageOffset(0);
              }}
            />
          </div>
        </div>
        <div className="control-group" style={{ flexWrap: 'wrap' }}>
          <label>Model <select aria-label="Filter sessions by model" value={modelFilter} onChange={event => { setModelFilter(event.target.value); setPageOffset(0); }}>
            <option value="">All models</option>
            {Array.from(new Set(sessions.flatMap(session => session.models_used))).sort().map(model => <option key={model} value={model}>{model}</option>)}
          </select></label>
          <label>Min minutes <input aria-label="Minimum session duration in minutes" type="number" min="0" step="any" value={minimumDuration} style={{ width: 72 }} onChange={event => { setMinimumDuration(event.target.value); setPageOffset(0); }} /></label>
          <label>Max minutes <input aria-label="Maximum session duration in minutes" type="number" min="0" step="any" value={maximumDuration} style={{ width: 72 }} onChange={event => { setMaximumDuration(event.target.value); setPageOffset(0); }} /></label>
        </div>

        <div className="control-group">
          <span style={{ fontSize: '11px', color: '#94a3b8' }}>Sort by:</span>
          <button
            className={`btn-pill ${sortField === 'started_at' ? 'active' : ''}`}
            onClick={() => handleSort('started_at')}
          >
            Recency {sortField === 'started_at' && (sortOrder === 'desc' ? '↓' : '↑')}
          </button>
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
            className={`btn-pill ${sortField === 'duration_ms' ? 'active' : ''}`}
            onClick={() => handleSort('duration_ms')}
          >
            Duration {sortField === 'duration_ms' && (sortOrder === 'desc' ? '↓' : '↑')}
          </button>
          <button
            className={`btn-pill ${sortField === 'requests_count' ? 'active' : ''}`}
            onClick={() => handleSort('requests_count')}
          >
            Requests {sortField === 'requests_count' && (sortOrder === 'desc' ? '↓' : '↑')}
          </button>
        </div>

        <div className="control-group">
          <button
            className="btn-pill"
            disabled={pageOffset === 0}
            onClick={() => setPageOffset(prev => Math.max(0, prev - pageSize))}
            style={{ opacity: pageOffset === 0 ? 0.5 : 1 }}
          >
            ← Prev
          </button>
          <span style={{ fontSize: '11px', color: '#94a3b8' }}>
            {filteredSessions.length === 0 ? 0 : pageOffset + 1}–{Math.min(pageOffset + pageSize, filteredSessions.length)} of {filteredSessions.length}
          </span>
          <button
            className="btn-pill"
            disabled={pageOffset + pageSize >= filteredSessions.length}
            onClick={() => setPageOffset(prev => prev + pageSize)}
            style={{ opacity: pageOffset + pageSize >= filteredSessions.length ? 0.5 : 1 }}
          >
            Next →
          </button>
        </div>
      </div>

      {/* Main Layout: Session Table + Detail Drawer */}
      <div style={{ display: 'grid', gridTemplateColumns: selectedSession ? 'repeat(auto-fit, minmax(min(100%, 440px), 1fr))' : 'minmax(0, 1fr)', gap: '16px', minWidth: 0 }}>
        {/* Sessions Table */}
        <div className="table-responsive-container">
          <table className="explorer-table">
            <thead>
              <tr>
                <th>Session &amp; Title</th>
                <th>Application Basis</th>
                <th>Project Workspace</th>
                <th>Primary Model</th>
                <th className="num-cell" onClick={() => handleSort('total_tokens')}>Total Tokens</th>
                <th className="num-cell">Cache</th>
                <th className="num-cell" onClick={() => handleSort('api_equivalent_usd')}>Est. Cost</th>
                <th className="num-cell" onClick={() => handleSort('duration_ms')}>Duration</th>
                <th className="num-cell" onClick={() => handleSort('requests_count')}>Reqs</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody>
              {pagedSessions.map(sess => {
                const isSelected = selectedSession?.session_id === sess.session_id;
                const durMin = sess.duration_ms === null || sess.duration_ms === undefined
                  ? '—'
                  : `${(sess.duration_ms / 60000).toFixed(1)}m`;
                const costStr = formatCost(sess.api_equivalent_usd, 3);

                return (
                  <tr
                    key={sess.session_id}
                    className={isSelected ? 'selected' : ''}
                    aria-selected={isSelected}
                  >
                    <td>
                      <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                        <button className="btn-pill" aria-label={`Inspect session ${sess.session_id}`} aria-expanded={isSelected} onClick={() => setSelectedSession(isSelected ? null : sess)} style={{ overflowWrap: 'anywhere', textAlign: 'left' }}>
                          {sess.session_id}
                        </button>
                        {sess.is_subagent && (
                          <span className="badge-tag badge-purple" title={`Child of ${sess.parent_session_id || 'parent'}`}>
                            Subagent
                          </span>
                        )}
                      </div>
                      <div style={{ fontSize: '10px', color: '#64748b', marginTop: 2 }}>
                        Title: <span style={{ color: '#94a3b8' }}>Unavailable (not projected)</span>
                      </div>
                      <div style={{ fontSize: '10px', color: '#64748b' }}>
                        {sess.started_at ? sess.started_at.slice(0, 16).replace('T', ' ') : '—'}
                      </div>
                    </td>
                    <td>
                      <span className="provenance-tag" style={{ fontSize: '10px' }} title="Application basis">
                        {sess.origin_harness || 'Unavailable'}
                      </span>
                    </td>
                    <td>
                      <span style={{ color: sess.project_name ? '#fff' : '#f59e0b', fontSize: '11px' }}>
                        {sess.project_name || 'Unattributed'}
                      </span>
                    </td>
                    <td>
                      {sess.models_used.length === 1 ? (
                        <span className="provenance-tag" style={{ fontSize: '10px', color: '#38bdf8' }}>
                          {sess.models_used[0]} <span style={{ color: '#94a3b8', fontSize: '9px' }}>(primary)</span>
                        </span>
                      ) : sess.models_used.length > 1 ? (
                        <div style={{ display: 'flex', gap: 4, flexWrap: 'wrap', alignItems: 'center' }}>
                          <span style={{ fontSize: '10px', color: '#94a3b8' }}>Unavailable (multi-model):</span>
                          {sess.models_used.map(m => (
                            <span key={m} className="provenance-tag" style={{ fontSize: '9px' }}>
                              {m.replace('claude-', '').replace('anthropic.', '')}
                            </span>
                          ))}
                        </div>
                      ) : (
                        <span style={{ fontSize: '10px', color: '#64748b' }}>Unavailable</span>
                      )}
                    </td>
                    <td className="num-cell" style={{ fontWeight: 600, color: '#38bdf8' }}>
                      {formatCount(sess.total_tokens)} <CoverageBadge status={sess.total_tokens_status} />
                    </td>
                    <td className="num-cell" style={{ fontSize: '11px', color: '#64748b' }}>
                      Unavailable
                    </td>
                    <td className="num-cell" style={{ color: '#10b981', fontWeight: 600 }}>
                      {costStr} <CoverageBadge status={sess.api_equivalent_usd_status} />
                    </td>
                    <td className="num-cell" style={{ color: '#94a3b8' }}>
                      {durMin}
                    </td>
                    <td className="num-cell">{formatCount(sess.requests_count)}</td>
                    <td>
                      <span className={`badge-tag ${sess.status === 'completed' ? 'badge-green' : 'badge-gray'}`}>
                        {sess.status || 'Unknown'}
                      </span>
                    </td>
                  </tr>
                );
              })}

              {pagedSessions.length === 0 && !loading && (
                <tr>
                  <td colSpan={10} style={{ textAlign: 'center', padding: '30px', color: '#64748b' }}>
                    No sessions found matching filters.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>

        {/* Detailed Session & Request Drawer */}
        {selectedSession && (
          <div className="detail-overlay-panel">
            {sessionDetailsAreCurrent ? (
              <>
            {/* Header */}
            <div className="detail-nav-bar">
              <div>
                <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
                  <h3 style={{ fontSize: '13px', fontWeight: 700, color: '#fff' }}>
                    Session {selectedSession.session_id.slice(0, 12)}...
                  </h3>
                  <span className="badge-tag badge-blue" title="Application basis">{selectedSession.origin_harness || 'Unavailable'} (application basis)</span>
                  {selectedSession.is_subagent && (
                    <span className="badge-tag badge-purple">Subagent</span>
                  )}
                </div>
                <div style={{ fontSize: '10px', color: '#94a3b8', marginTop: 2 }}>
                  Title: <strong style={{ color: '#cbd5e1' }}>Unavailable (not projected by session API)</strong>
                </div>
                <div style={{ fontSize: '10px', color: '#64748b', marginTop: 2 }}>
                  Started: {selectedSession.started_at || '—'} • Project: {selectedSession.project_name || 'Unattributed'}
                </div>
                <div style={{ fontSize: '10px', color: '#94a3b8', marginTop: 2 }}>
                  Primary model:{' '}
                  {selectedSession.models_used.length === 1 ? (
                    <strong style={{ color: '#38bdf8' }}>{selectedSession.models_used[0]} (single-model primary)</strong>
                  ) : selectedSession.models_used.length > 1 ? (
                    <span style={{ color: '#cbd5e1' }}>Unavailable (multi-model session: {selectedSession.models_used.join(', ')}; primary not projected)</span>
                  ) : (
                    <span style={{ color: '#64748b' }}>Unavailable</span>
                  )}
                </div>
              </div>
              <button
                className="btn-pill"
                onClick={() => setSelectedSession(null)}
                style={{ padding: '2px 8px' }}
              >
                ✕ Close
              </button>
            </div>

            {/* Quick Metrics Bar */}
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: '8px' }}>
              <div style={{ background: 'var(--bg-primary)', padding: '8px', borderRadius: '4px' }}>
                <div style={{ fontSize: '9px', color: '#94a3b8', textTransform: 'uppercase' }}>Tokens</div>
                <div style={{ fontSize: '14px', fontWeight: 700, color: '#38bdf8' }}>
                  {formatCount(selectedSession.total_tokens)} <CoverageBadge status={selectedSession.total_tokens_status} />
                </div>
              </div>
              <div style={{ background: 'var(--bg-primary)', padding: '8px', borderRadius: '4px' }}>
                <div style={{ fontSize: '9px', color: '#94a3b8', textTransform: 'uppercase' }}>Cache</div>
                <div style={{ fontSize: '12px', fontWeight: 600, color: '#94a3b8' }}>
                  Unavailable
                </div>
                <div style={{ fontSize: '8px', color: '#64748b' }}>Not projected</div>
              </div>
              <div style={{ background: 'var(--bg-primary)', padding: '8px', borderRadius: '4px' }}>
                <div style={{ fontSize: '9px', color: '#94a3b8', textTransform: 'uppercase' }}>Est. Cost</div>
                <div style={{ fontSize: '14px', fontWeight: 700, color: '#10b981' }}>
                  {formatCost(selectedSession.api_equivalent_usd, 3)} <CoverageBadge status={selectedSession.api_equivalent_usd_status} />
                </div>
              </div>
              <div style={{ background: 'var(--bg-primary)', padding: '8px', borderRadius: '4px' }}>
                <div style={{ fontSize: '9px', color: '#94a3b8', textTransform: 'uppercase' }}>Requests</div>
                <div style={{ fontSize: '14px', fontWeight: 700, color: '#f8fafc' }}>
                  {formatCount(selectedSession.requests_count)}
                </div>
              </div>
            </div>
            <OutcomeEditor key={selectedSession.session_id} sessionId={selectedSession.session_id} requestIds={sessionRequests.map(item => item.canonical_id)} write={context.write} />


            {/* Tabs */}
            <div style={{ display: 'flex', borderBottom: '1px solid var(--border-subtle)', gap: '4px', overflowX: 'auto' }}>
              <button
                className={`btn-pill ${activeTab === 'requests' ? 'active' : ''}`}
                onClick={() => setActiveTab('requests')}
              >
                Requests ({sessionRequests.length})
              </button>
              <button
                className={`btn-pill ${activeTab === 'timeline' ? 'active' : ''}`}
                onClick={() => setActiveTab('timeline')}
              >
                Trace Events ({sessionEvents.length})
              </button>
              <button
                className={`btn-pill ${activeTab === 'analytics' ? 'active' : ''}`}
                onClick={() => setActiveTab('analytics')}
              >
                Analysis
              </button>
              {selectedRequest && (
                <button
                  className={`btn-pill ${activeTab === 'inspector' ? 'active' : ''}`}
                  onClick={() => setActiveTab('inspector')}
                >
                  Request Inspector
                </button>
              )}
            </div>

            {detailLoading ? (
              <div style={{ padding: '20px', textAlign: 'center', color: '#64748b' }}>
                Loading session details...
              </div>
            ) : (
              <>
                {/* TAB 1: Requests List */}
                {activeTab === 'requests' && (
                  <div style={{ display: 'flex', flexDirection: 'column', gap: '6px', maxHeight: '420px', overflowY: 'auto' }}>
                    {sessionRequests.map((req, idx) => {
                      const isCurrent = selectedRequestIdx === idx;
                      return (
                        <div
                          key={req.canonical_id}
                          role="button"
                          tabIndex={0}
                          aria-label={`Inspect request ${idx + 1}`}
                          onKeyDown={event => {
                            if (event.key === 'Enter' || event.key === ' ') {
                              event.preventDefault();
                              setSelectedRequestIdx(idx);
                              setActiveTab('inspector');
                            }
                          }}
                          style={{
                            background: isCurrent ? 'rgba(56, 189, 248, 0.15)' : 'var(--bg-primary)',
                            border: isCurrent ? '1px solid #38bdf8' : '1px solid var(--border-subtle)',
                            borderRadius: '6px',
                            padding: '8px 10px',
                            cursor: 'pointer'
                          }}
                          onClick={() => {
                            setSelectedRequestIdx(idx);
                            setActiveTab('inspector');
                          }}
                        >
                          <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 2 }}>
                            <span style={{ fontSize: '11px', fontWeight: 600, color: '#fff' }}>
                              #{idx + 1} {req.normalized_model}
                            </span>
                            <span style={{ fontSize: '11px', color: '#10b981', fontWeight: 600 }}>
                              {formatCost(req.api_equivalent_usd, 4)}
                            </span>
                          </div>
                          <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: '10px', color: '#64748b' }}>
                            <span>
                              Tokens: <strong style={{ color: '#38bdf8' }}>{formatCount(req.total_tokens)}</strong>
                            </span>
                            <span>
                              TPS: {req.generation_speed_tps === null || req.generation_speed_tps === undefined ? '—' : `${req.generation_speed_tps} tps`} • Dur: {formatCount(req.duration_ms)}ms
                            </span>
                          </div>
                        </div>
                      );
                    })}
                    {sessionRequests.length === 0 && (
                      <div style={{ padding: '20px', textAlign: 'center', color: '#64748b', fontSize: '11px' }}>
                        No granular requests found (session may be archive partition).
                      </div>
                    )}
                  </div>
                )}

                {/* TAB 2: Parent-linked Trace Events */}
                {activeTab === 'timeline' && (
                  <div style={{ maxHeight: '420px', overflowY: 'auto' }}>
                    <TraceEventTree
                      events={sessionEvents}
                      emptyLabel="No execution trace events were observed for this session."
                    />
                  </div>
                )}

                {/* TAB 3: Existing analyzer outputs, expanded on demand */}
                {activeTab === 'analytics' && (
                  <TraceAnalyticsPanels trace={traceAnalysis} />
                )}

                {/* TAB 3: Individual Request Inspector with Prev/Next Navigation */}
                {activeTab === 'inspector' && selectedRequest && (
                  <div style={{ display: 'flex', flexDirection: 'column', gap: '10px' }}>
                    {/* Stepper Navigation */}
                    <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', background: 'var(--bg-primary)', padding: '6px 10px', borderRadius: '4px' }}>
                      <button
                        className="btn-pill"
                        disabled={selectedRequestIdx === 0}
                        onClick={() => navigateRequest(-1)}
                        style={{ opacity: selectedRequestIdx === 0 ? 0.4 : 1 }}
                      >
                        ← Prev Request
                      </button>

                      <span style={{ fontSize: '11px', fontWeight: 600, color: '#fff' }}>
                        Request {(selectedRequestIdx ?? 0) + 1} of {sessionRequests.length}
                      </span>

                      <button
                        className="btn-pill"
                        disabled={selectedRequestIdx === sessionRequests.length - 1}
                        onClick={() => navigateRequest(1)}
                        style={{ opacity: selectedRequestIdx === sessionRequests.length - 1 ? 0.4 : 1 }}
                      >
                        Next Request →
                      </button>
                    </div>

                    {/* Metadata Card */}
                    <div style={{ background: 'var(--bg-primary)', padding: '10px', borderRadius: '6px', fontSize: '11px' }}>
                      <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 4 }}>
                        <span style={{ color: '#94a3b8' }}>Canonical ID:</span>
                        <span style={{ fontFamily: 'monospace', color: '#fff', overflowWrap: 'anywhere', minWidth: 0 }}>
                          {selectedRequest.canonical_id}
                        </span>
                      </div>
                      <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 4 }}>
                        <span style={{ color: '#94a3b8' }}>Model & Route:</span>
                        <span style={{ color: '#38bdf8', fontWeight: 600 }}>
                          {selectedRequest.normalized_model} ({selectedRequest.provider})
                        </span>
                      </div>
                      <div>Status: <strong>{selectedRequest.request_status || 'Unknown (not reported)'}</strong></div>
                      <div>Application basis: {selectedRequest.origin_harness || 'Unavailable'} · Access route: {selectedRequest.access_route || 'Unavailable'}</div>
                      <div>Original model: {selectedRequest.raw_model || 'Unavailable'}</div>
                      <div>Request timestamp: {selectedRequest.request_timestamp || 'Unavailable'}</div>
                      <div>Reasoning setting: <strong style={{ color: '#cbd5e1' }}>Unavailable (not projected by request API)</strong></div>
                      <div>Pricing version: {selectedRequest.valuation_pricing_version_id || 'Unavailable'}</div>
                      <div>Overlap: {selectedRequest.is_unresolved_overlap ? 'Unresolved; totals may overlap' : 'No unresolved overlap recorded'}</div>
                      <details><summary>Valuation provenance</summary><pre style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{selectedRequest.valuation_provenance ? JSON.stringify(selectedRequest.valuation_provenance, null, 2) : 'Unavailable'}</pre></details>
                      <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 4 }}>
                        <span style={{ color: '#94a3b8' }}>Timing Basis:</span>
                        <span>{selectedRequest.timing_basis} • {formatCount(selectedRequest.duration_ms)}ms</span>
                      </div>
                      <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 4 }}>
                        <span style={{ color: '#94a3b8' }}>Generation Speed:</span>
                        <span style={{ color: '#a855f7', fontWeight: 600 }}>
                          {selectedRequest.generation_speed_tps === null || selectedRequest.generation_speed_tps === undefined
                            ? 'Untimed'
                            : `${selectedRequest.generation_speed_tps} TPS`}
                        </span>
                      </div>
                      <div style={{ display: 'flex', justifyContent: 'space-between' }}>
                        <span style={{ color: '#94a3b8' }}>Pricing Status:</span>
                        <span style={{ color: selectedRequest.pricing_status === 'exact' ? '#10b981' : '#f59e0b' }}>
                          {selectedRequest.pricing_status} ({formatCost(selectedRequest.api_equivalent_usd, 4)})
                        </span>
                      </div>
                    </div>

                    {/* Disjoint Billable Counters */}
                    <div style={{ background: 'var(--bg-primary)', padding: '10px', borderRadius: '6px' }}>
                      <div style={{ fontSize: '10px', fontWeight: 600, color: '#94a3b8', textTransform: 'uppercase', marginBottom: 6 }}>
                        Disjoint Token Breakdown
                      </div>
                      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '6px', fontSize: '11px' }}>
                        <div>Fresh Input: <strong>{formatCount(selectedRequest.fresh_input_tokens)}</strong></div>
                        <div>Cache Read: <strong style={{ color: '#10b981' }}>{formatCount(selectedRequest.cache_read_tokens)}</strong></div>
                        <div>Cache Write (5m): <strong>{formatCount(selectedRequest.cache_write_5m_tokens)}</strong></div>
                        <div>Cache Write (1h): <strong>{formatCount(selectedRequest.cache_write_1h_tokens)}</strong></div>
                        <div>Cache Write (Unknown TTL, unpriced): <strong>{formatCount(selectedRequest.cache_write_unknown_ttl_tokens)}</strong></div>
                        <div>Output: <strong style={{ color: '#a855f7' }}>{formatCount(selectedRequest.output_tokens)}</strong></div>
                        <div>Reasoning (Subset): <strong>{formatCount(selectedRequest.reasoning_tokens)}</strong></div>
                      </div>
                    </div>

                    {/* Linked Event Navigation */}
                    <button
                      className="btn-pill"
                      onClick={() => jumpToEvent(selectedRequest.canonical_id)}
                      style={{ textAlign: 'center', width: '100%', padding: '6px' }}
                    >
                      🔗 Jump to Linked Event in Trace Timeline
                    </button>

                    <div style={{ background: 'var(--bg-primary)', padding: '10px', borderRadius: '6px', fontSize: '11px', color: '#cbd5e1' }}>
                      <div style={{ fontSize: '10px', fontWeight: 600, color: '#94a3b8', textTransform: 'uppercase', marginBottom: 4 }}>
                        Body &amp; Transcript View
                      </div>
                      <div>Status: <span className="badge-tag badge-gray">Unavailable</span></div>
                      <div style={{ color: '#94a3b8', marginTop: 4 }}>
                        Prompt and response bodies and full transcripts remain unavailable because no content route is provided by the API. Raw contents are neither persisted nor retrieved.
                      </div>
                    </div>

                    <div className="privacy-banner">
                      Metadata-only by default. Prompt/response body and transcript views stay unavailable (no content route). No source files or transcripts are read by this inspector.
                    </div>
                  </div>
                )}
              </>
            )}
              </>
            ) : (
              <div style={{ padding: '20px', textAlign: 'center', color: '#64748b' }}>
                {sessionListIsCurrent
                  ? 'Refreshing session details for the active query context…'
                  : 'Refreshing sessions and details for the active harness, time range, and filters…'}
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
