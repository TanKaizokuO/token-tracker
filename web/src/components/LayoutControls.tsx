import React, { useState, useEffect, useCallback, useMemo, useRef } from 'react';
import type { PageContext, QueryEnvelope } from '../App';
import './Interaction.css';

// ============================================================================
// Layout State Types & Actual API Contract (WP22 / WP08)
// ============================================================================

export interface WidgetLayoutItem {
  id: string;
  title: string;
  description?: string;
  category?: string;
  colSpan: number; // 1 to 12 (12-column grid)
  minColSpan?: number;
  maxColSpan?: number;
  order: number;
  visible: boolean;
  removable?: boolean;
}

export interface VersionedLayoutState {
  version: number; // e.g. 1
  page_name: string;
  widgets: WidgetLayoutItem[];
  global_query?: Partial<QueryEnvelope>;
  updated_at: string;
}

export interface ServerSavedLayout {
  layout_id: string;
  layout_name: string;
  is_default: boolean;
  page_name: string;
  layout_state: VersionedLayoutState;
  created_at?: string;
  updated_at?: string;
}

export interface LayoutControlsProps {
  pageName: string;
  context: PageContext;
  defaultWidgets: WidgetLayoutItem[];
  widgets: WidgetLayoutItem[];
  onWidgetsChange: (widgets: WidgetLayoutItem[]) => void;
  onLayoutLoaded?: (layout: ServerSavedLayout) => void;
}

export interface DashboardGridProps {
  widgets: WidgetLayoutItem[];
  onWidgetsChange: (widgets: WidgetLayoutItem[]) => void;
  renderWidget: (widgetId: string) => React.ReactNode;
  editable?: boolean;
}

function mergeSavedWidgets(
  defaultWidgets: WidgetLayoutItem[],
  savedWidgets: WidgetLayoutItem[]
): { widgets: WidgetLayoutItem[]; unknownIds: string[] } {
  const defaultsById = new Map(defaultWidgets.map((widget) => [widget.id, widget]));
  const unknownIds: string[] = [];
  const restored = savedWidgets.flatMap((savedWidget) => {
    const defaultWidget = defaultsById.get(savedWidget.id);
    if (!defaultWidget) {
      unknownIds.push(savedWidget.id);
      return [];
    }
    return [{ ...defaultWidget, ...savedWidget }];
  });
  const restoredIds = new Set(restored.map((widget) => widget.id));
  const nextOrder = restored.reduce((highest, widget) => Math.max(highest, widget.order), -1) + 1;
  const additions = defaultWidgets
    .filter((widget) => !restoredIds.has(widget.id))
    .map((widget, index) => ({ ...widget, visible: false, order: nextOrder + index }));

  return { widgets: [...restored, ...additions], unknownIds };
}

function restoreGlobalQuery(current: QueryEnvelope, saved: Partial<QueryEnvelope>): QueryEnvelope {
  return {
    ...current,
    ...saved,
    contract_version: '1',
    time_range: { ...current.time_range, ...saved.time_range },
    filters: saved.filters ? { ...saved.filters } : current.filters,
    search: saved.search,
    include_archive:
      typeof saved.include_archive === 'boolean' ? saved.include_archive : current.include_archive,
  };
}

// ============================================================================
// LayoutControls Component (Toolbar, Saved Layouts, & Widget Management)
// ============================================================================

export function LayoutControls({
  pageName,
  context,
  defaultWidgets,
  widgets,
  onWidgetsChange,
  onLayoutLoaded,
}: LayoutControlsProps) {
  const { write, query, setQuery, bootstrap } = context;

  // Server saved layouts list
  const [savedLayouts, setSavedLayouts] = useState<ServerSavedLayout[]>([]);
  const hasAppliedDefaultLayout = useRef(false);
  const [selectedLayoutId, setSelectedLayoutId] = useState<string>('');
  const [isLoading, setIsLoading] = useState<boolean>(false);
  const [serverError, setServerError] = useState<string | null>(null);
  const [restorationWarning, setRestorationWarning] = useState<string | null>(null);

  // Modals state
  const [showSaveModal, setShowSaveModal] = useState<boolean>(false);
  const [showManageModal, setShowManageModal] = useState<boolean>(false);
  const [showCatalogModal, setShowCatalogModal] = useState<boolean>(false);

  // Save Modal inputs
  const [newLayoutName, setNewLayoutName] = useState<string>('');
  const [isDefaultLayout, setIsDefaultLayout] = useState<boolean>(false);
  const [includeGlobalQuery, setIncludeGlobalQuery] = useState<boolean>(true);
  const [isSaving, setIsSaving] = useState<boolean>(false);

  // 1. Fetch saved layouts from server API (GET /api/v1/layouts)
  const fetchLayouts = useCallback(async () => {
    setIsLoading(true);
    setServerError(null);
    try {
      const res = await fetch('/api/v1/layouts', { credentials: 'same-origin' });
      if (!res.ok) {
        throw new Error(`Failed to fetch layouts: HTTP ${res.status}`);
      }
      const data = (await res.json()) as { layouts?: ServerSavedLayout[] };
      const allLayouts = data.layouts || [];

      // Filter for this page or global layouts
      const pageLayouts = allLayouts.filter(
        (l) => !l.page_name || l.page_name === pageName
      );
      setSavedLayouts(pageLayouts);

      // Apply the server default only on the first successful load. Later
      // refreshes must not override the layout the user just saved or selected.
      if (!hasAppliedDefaultLayout.current) {
        hasAppliedDefaultLayout.current = true;
        const defaultLayout = pageLayouts.find((layout) => layout.is_default);
        setSelectedLayoutId(defaultLayout?.layout_id || '');
        if (defaultLayout?.layout_state?.widgets) {
          const { widgets: merged, unknownIds } = mergeSavedWidgets(defaultWidgets, defaultLayout.layout_state.widgets);
          onWidgetsChange(merged);
          if (unknownIds.length > 0) {
            setRestorationWarning(`Restored default layout contains unsupported widget ID(s): ${unknownIds.join(', ')}. They were omitted.`);
          }
          const savedQuery = defaultLayout.layout_state.global_query;
          if (savedQuery) {
            setQuery((prev) => restoreGlobalQuery(prev, savedQuery));
          }
          if (onLayoutLoaded) {
            onLayoutLoaded(defaultLayout);
          }
        }
      }
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : 'Error loading layouts';
      setServerError(msg);
    } finally {
      setIsLoading(false);
    }
  }, [pageName, onWidgetsChange, onLayoutLoaded, setQuery, defaultWidgets]);

  // Load on mount and when bootstrap changes
  useEffect(() => {
    if (bootstrap.csrf_token || bootstrap.status === 'ok') {
      fetchLayouts();
    }
  }, [bootstrap.csrf_token, bootstrap.status, fetchLayouts]);

  // 2. Select and restore a saved layout
  const handleSelectLayout = (layoutId: string) => {
    setSelectedLayoutId(layoutId);
    if (!layoutId) {
      // Revert to factory default widgets
      onWidgetsChange(defaultWidgets);
      return;
    }

    const layout = savedLayouts.find((l) => l.layout_id === layoutId);
    if (layout && layout.layout_state?.widgets) {
      const { widgets: merged, unknownIds } = mergeSavedWidgets(defaultWidgets, layout.layout_state.widgets);
      onWidgetsChange(merged);
      if (unknownIds.length > 0) {
        setRestorationWarning(`Restored layout "${layout.layout_name}" contains unsupported widget ID(s): ${unknownIds.join(', ')}. They were omitted.`);
      } else {
        setRestorationWarning(null);
      }
      // Restore query snapshot if present in layout
      if (layout.layout_state.global_query) {
        setQuery((prev) => restoreGlobalQuery(prev, layout.layout_state.global_query!));
      }
      if (onLayoutLoaded) {
        onLayoutLoaded(layout);
      }
    } else {
      setRestorationWarning(null);
    }
  };

  // 3. Save current layout via actual API: POST /api/v1/layouts
  const handleSaveLayout = async (e: React.FormEvent) => {
    e.preventDefault();
    const trimmedName = newLayoutName.trim();
    if (!trimmedName) return;

    setIsSaving(true);
    setServerError(null);

    const layoutStatePayload: VersionedLayoutState = {
      version: 1,
      page_name: pageName,
      widgets: widgets,
      updated_at: new Date().toISOString(),
      ...(includeGlobalQuery ? { global_query: query } : {}),
    };

    const payload = {
      layout_id: selectedLayoutId || `layout_${pageName}_${Date.now()}`,
      layout_name: trimmedName,
      is_default: isDefaultLayout,
      page_name: pageName,
      layout_state: layoutStatePayload,
    };

    try {
      const savedRes = (await write('layouts', payload, 'POST')) as ServerSavedLayout;
      setShowSaveModal(false);
      setNewLayoutName('');
      await fetchLayouts();
      if (savedRes?.layout_id) {
        setSelectedLayoutId(savedRes.layout_id);
      }
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : 'Failed to save layout';
      setServerError(msg);
    } finally {
      setIsSaving(false);
    }
  };

  // 4. Delete saved layout via actual API: DELETE /api/v1/layouts/{layout_id}
  const handleDeleteLayout = async () => {
    if (!selectedLayoutId) return;
    const confirmDelete = window.confirm('Delete this saved layout configuration?');
    if (!confirmDelete) return;

    setServerError(null);
    try {
      await write(`layouts/${encodeURIComponent(selectedLayoutId)}`, null, 'DELETE');
      setSelectedLayoutId('');
      onWidgetsChange(defaultWidgets);
      await fetchLayouts();
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : 'Failed to delete layout';
      setServerError(msg);
    }
  };

  // 5. Toggle visibility of individual widget in manage drawer
  const handleToggleWidgetVisible = (widgetId: string, visible: boolean) => {
    const updated = widgets.map((w) => {
      if (w.id === widgetId) {
        return { ...w, visible };
      }
      return w;
    });
    onWidgetsChange(updated);
  };

  const currentLayout = savedLayouts.find((l) => l.layout_id === selectedLayoutId);

  return (
    <div className="tl-layout-toolbar" role="toolbar" aria-label="Dashboard layout controls">
      <div className="tl-layout-toolbar-left">
        <span className="tl-layout-selector-label">Layout:</span>

        {/* Saved Layouts Selector */}
        <select
          className="tl-layout-select"
          value={selectedLayoutId}
          onChange={(e) => handleSelectLayout(e.target.value)}
          aria-label="Select saved dashboard layout"
        >
          <option value="">Default Factory View</option>
          {savedLayouts.map((l) => (
            <option key={l.layout_id} value={l.layout_id}>
              {l.layout_name} {l.is_default ? '★ (Default)' : ''}
            </option>
          ))}
        </select>

        {currentLayout?.is_default && (
          <span className="tl-badge-default" title="This layout loads automatically on page open">
            ★ Default
          </span>
        )}

        {/* Save Current Layout Button */}
        <button
          type="button"
          className="tl-btn tl-btn-sm"
          onClick={() => {
            setNewLayoutName(currentLayout?.layout_name || '');
            setIsDefaultLayout(currentLayout?.is_default || false);
            setShowSaveModal(true);
          }}
          title="Save current widget configuration"
          aria-label="Save layout"
        >
          <span>💾</span>
          <span>Save Layout</span>
        </button>

        {/* Delete Layout Button (only for saved custom layouts) */}
        {selectedLayoutId && (
          <button
            type="button"
            className="tl-btn tl-btn-sm tl-btn-danger"
            onClick={handleDeleteLayout}
            title="Delete currently selected layout"
            aria-label="Delete layout"
          >
            Delete
          </button>
        )}
      </div>

      <div className="tl-layout-toolbar-right">
        {/* Dynamic Add Widget Button */}
        <button
          type="button"
          className="tl-btn tl-btn-sm tl-btn-primary"
          onClick={() => setShowCatalogModal(true)}
          title="Open widget catalog to add widgets to the dashboard"
          aria-label="Add widget"
        >
          <span>＋ Add Widget</span>
        </button>

        {/* Manage / Add Widgets Button */}
        <button
          type="button"
          className="tl-btn tl-btn-sm"
          onClick={() => setShowManageModal(true)}
          title="Add, remove, or restore hidden widgets"
          aria-label="Manage widgets"
        >
          <span>⊞</span>
          <span>
            Widgets ({widgets.filter((w) => w.visible).length}/{widgets.length})
          </span>
        </button>

        {/* Reset to Factory Defaults Button */}
        <button
          type="button"
          className="tl-btn tl-btn-sm"
          onClick={() => {
            setSelectedLayoutId('');
            onWidgetsChange(defaultWidgets);
          }}
          title="Reset to factory default arrangement"
          aria-label="Reset widgets"
        >
          Reset Defaults
        </button>

        {serverError && (
          <span className="tl-state-error-text" style={{ fontSize: 11 }}>
            {serverError}
          </span>
        )}
      </div>

      {/* Save Layout Modal */}
      {showSaveModal && (
        <div className="tl-modal-backdrop" role="dialog" aria-modal="true" aria-labelledby="save-layout-title">
          <div className="tl-modal">
            <div className="tl-modal-header">
              <span id="save-layout-title" className="tl-modal-title">
                Save Dashboard Layout
              </span>
              <button
                type="button"
                className="tl-btn tl-btn-sm tl-btn-icon"
                onClick={() => setShowSaveModal(false)}
                aria-label="Close modal"
              >
                ✕
              </button>
            </div>

            <form onSubmit={handleSaveLayout}>
              <div className="tl-modal-body">
                <div className="tl-form-group">
                  <label htmlFor="tl-layout-name-input" className="tl-form-label">
                    Layout Name:
                  </label>
                  <input
                    id="tl-layout-name-input"
                    type="text"
                    className="tl-input-text"
                    placeholder="e.g. Executive Overview or Token Focus"
                    value={newLayoutName}
                    onChange={(e) => setNewLayoutName(e.target.value)}
                    required
                    autoFocus
                  />
                </div>

                <label className="tl-checkbox-label">
                  <input
                    type="checkbox"
                    checked={isDefaultLayout}
                    onChange={(e) => setIsDefaultLayout(e.target.checked)}
                  />
                  <span>Set as default layout for this page</span>
                </label>

                <label className="tl-checkbox-label">
                  <input
                    type="checkbox"
                    checked={includeGlobalQuery}
                    onChange={(e) => setIncludeGlobalQuery(e.target.checked)}
                  />
                  <span>Include active query filters & time range in layout snapshot</span>
                </label>
              </div>

              <div className="tl-modal-footer">
                <button
                  type="button"
                  className="tl-btn"
                  onClick={() => setShowSaveModal(false)}
                  disabled={isSaving}
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  className="tl-btn tl-btn-primary"
                  disabled={isSaving || !newLayoutName.trim()}
                >
                  {isSaving ? 'Saving...' : 'Save Layout'}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      {/* Manage Widgets Modal */}
      {showManageModal && (
        <div className="tl-modal-backdrop" role="dialog" aria-modal="true" aria-labelledby="manage-widgets-title">
          <div className="tl-modal">
            <div className="tl-modal-header">
              <span id="manage-widgets-title" className="tl-modal-title">
                Manage Dashboard Widgets
              </span>
              <button
                type="button"
                className="tl-btn tl-btn-sm tl-btn-icon"
                onClick={() => setShowManageModal(false)}
                aria-label="Close modal"
              >
                ✕
              </button>
            </div>

            <div className="tl-modal-body">
              <div className="tl-widget-list">
                {widgets.map((w) => (
                  <div key={w.id} className="tl-widget-item">
                    <div className="tl-widget-item-info">
                      <span className="tl-widget-item-name">{w.title}</span>
                      {w.description && <span className="tl-widget-item-desc">{w.description}</span>}
                    </div>

                    <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                      <span className="tl-widget-span-badge">{w.colSpan}/12 cols</span>
                      <input
                        type="checkbox"
                        checked={w.visible}
                        onChange={(e) => handleToggleWidgetVisible(w.id, e.target.checked)}
                        aria-label={`Toggle visibility for ${w.title}`}
                      />
                    </div>
                  </div>
                ))}
              </div>
            </div>

            <div className="tl-modal-footer">
              <button
                type="button"
                className="tl-btn tl-btn-primary"
                onClick={() => setShowManageModal(false)}
              >
                Done
              </button>
            </div>
          </div>
        </div>
      )}
      {/* Restoration Warning Banner */}
      {restorationWarning && (
        <div
          role="alert"
          style={{
            width: '100%',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            padding: '6px 12px',
            backgroundColor: 'rgba(245, 158, 11, 0.15)',
            border: '1px solid #f59e0b',
            color: '#fbbf24',
            borderRadius: '4px',
            fontSize: '12px',
            marginTop: '6px',
          }}
        >
          <span>⚠️ {restorationWarning}</span>
          <button
            type="button"
            onClick={() => setRestorationWarning(null)}
            style={{
              background: 'none',
              border: 'none',
              color: '#fbbf24',
              cursor: 'pointer',
              fontWeight: 'bold',
              padding: '0 4px',
            }}
            aria-label="Dismiss warning"
          >
            ✕
          </button>
        </div>
      )}

      {/* Dynamic Widget Catalog Modal */}
      {showCatalogModal && (
        <div className="tl-modal-backdrop" role="dialog" aria-modal="true" aria-labelledby="catalog-title">
          <div className="tl-modal" style={{ maxWidth: 540 }}>
            <div className="tl-modal-header">
              <span id="catalog-title" className="tl-modal-title">
                Widget Catalog
              </span>
              <button
                type="button"
                className="tl-btn tl-btn-sm tl-btn-icon"
                onClick={() => setShowCatalogModal(false)}
                aria-label="Close catalog"
              >
                ✕
              </button>
            </div>
            <div className="tl-modal-body">
              <p style={{ fontSize: 12, color: 'var(--text-muted, #9ca3af)', marginBottom: 12 }}>
                Browse available dashboard widgets for this view. Active widgets are currently visible.
              </p>
              <div className="tl-widget-list">
                {defaultWidgets.map((catalogItem) => {
                  const currentItem = widgets.find((w) => w.id === catalogItem.id);
                  const isVisible = currentItem ? currentItem.visible : false;
                  return (
                    <div
                      key={catalogItem.id}
                      className="tl-widget-item"
                      style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', padding: '8px 10px' }}
                    >
                      <div className="tl-widget-item-info">
                        <span className="tl-widget-item-name" style={{ fontWeight: 600 }}>{catalogItem.title}</span>
                        {catalogItem.description && (
                          <span className="tl-widget-item-desc" style={{ display: 'block', fontSize: 11, color: '#9ca3af' }}>
                            {catalogItem.description}
                          </span>
                        )}
                      </div>
                      <div>
                        {isVisible ? (
                          <span className="tl-badge-default" style={{ fontSize: 11 }}>Active</span>
                        ) : (
                          <button
                            type="button"
                            className="tl-btn tl-btn-sm tl-btn-primary"
                            onClick={() => {
                              if (currentItem) {
                                handleToggleWidgetVisible(catalogItem.id, true);
                              } else {
                                onWidgetsChange([...widgets, { ...catalogItem, visible: true, order: widgets.length }]);
                              }
                            }}
                          >
                            ＋ Add
                          </button>
                        )}
                      </div>
                    </div>
                  );
                })}
              </div>
            </div>
            <div className="tl-modal-footer">
              <button type="button" className="tl-btn" onClick={() => setShowCatalogModal(false)}>
                Close
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

// ============================================================================
// DashboardGrid & DashboardWidget (Keyboard Accessible 12-Column Responsive Grid)
// ============================================================================

export function DashboardGrid({
  widgets,
  onWidgetsChange,
  renderWidget,
  editable = true,
}: DashboardGridProps) {
  // Sort visible widgets by order
  const sortedWidgets = useMemo(() => {
    return [...widgets]
      .filter((w) => w.visible)
      .sort((a, b) => a.order - b.order);
  }, [widgets]);

  // Accessible Reorder: Move widget earlier in the order
  const handleMoveEarlier = (widgetId: string) => {
    const idx = sortedWidgets.findIndex((w) => w.id === widgetId);
    if (idx <= 0) return;

    const current = sortedWidgets[idx];
    const prev = sortedWidgets[idx - 1];

    const updated = widgets.map((w) => {
      if (w.id === current.id) return { ...w, order: prev.order };
      if (w.id === prev.id) return { ...w, order: current.order };
      return w;
    });
    onWidgetsChange(updated);
  };

  // Accessible Reorder: Move widget later in the order
  const handleMoveLater = (widgetId: string) => {
    const idx = sortedWidgets.findIndex((w) => w.id === widgetId);
    if (idx < 0 || idx >= sortedWidgets.length - 1) return;

    const current = sortedWidgets[idx];
    const next = sortedWidgets[idx + 1];

    const updated = widgets.map((w) => {
      if (w.id === current.id) return { ...w, order: next.order };
      if (w.id === next.id) return { ...w, order: current.order };
      return w;
    });
    onWidgetsChange(updated);
  };

  // Accessible Resize: Expand column span (step: 3 -> 4 -> 6 -> 8 -> 12)
  const handleGrowSpan = (widgetId: string) => {
    const spanSteps = [3, 4, 6, 8, 12];
    const updated = widgets.map((w) => {
      if (w.id === widgetId) {
        const nextSpan = spanSteps.find((s) => s > w.colSpan) || 12;
        const bounded = Math.min(w.maxColSpan || 12, nextSpan);
        return { ...w, colSpan: bounded };
      }
      return w;
    });
    onWidgetsChange(updated);
  };

  // Accessible Resize: Shrink column span (step: 12 -> 8 -> 6 -> 4 -> 3)
  const handleShrinkSpan = (widgetId: string) => {
    const spanSteps = [12, 8, 6, 4, 3];
    const updated = widgets.map((w) => {
      if (w.id === widgetId) {
        const nextSpan = spanSteps.find((s) => s < w.colSpan) || 3;
        const bounded = Math.max(w.minColSpan || 3, nextSpan);
        return { ...w, colSpan: bounded };
      }
      return w;
    });
    onWidgetsChange(updated);
  };

  // Hide widget
  const handleHideWidget = (widgetId: string) => {
    const updated = widgets.map((w) => {
      if (w.id === widgetId) {
        return { ...w, visible: false };
      }
      return w;
    });
    onWidgetsChange(updated);
  };

  // Keyboard navigation handler for widget headers
  const handleWidgetKeyDown = (widget: WidgetLayoutItem, e: React.KeyboardEvent) => {
    if (!editable) return;

    if (e.key === 'ArrowLeft' || e.key === 'ArrowUp') {
      e.preventDefault();
      handleMoveEarlier(widget.id);
    } else if (e.key === 'ArrowRight' || e.key === 'ArrowDown') {
      e.preventDefault();
      handleMoveLater(widget.id);
    } else if (e.key === '+' || e.key === '=') {
      e.preventDefault();
      handleGrowSpan(widget.id);
    } else if (e.key === '-' || e.key === '_') {
      e.preventDefault();
      handleShrinkSpan(widget.id);
    } else if (e.key === 'Delete' || e.key === 'Backspace') {
      e.preventDefault();
      handleHideWidget(widget.id);
    }
  };

  return (
    <div className="tl-dashboard-grid" role="region" aria-label="Dashboard widget grid">
      {sortedWidgets.map((w, index) => {
        const spanClass = `tl-col-${Math.max(1, Math.min(12, w.colSpan))}`;
        const isFirst = index === 0;
        const isLast = index === sortedWidgets.length - 1;

        return (
          <div
            key={w.id}
            className={`tl-widget ${spanClass}`}
            role="region"
            aria-label={`${w.title} widget`}
          >
            {/* Widget Accessible Header */}
            <div
              className="tl-widget-header"
              tabIndex={editable ? 0 : undefined}
              onKeyDown={(e) => handleWidgetKeyDown(w, e)}
              aria-label={`${w.title} controls. Use arrow keys to reorder, plus/minus to resize.`}
            >
              <div className="tl-widget-title-area">
                <span className="tl-widget-title">{w.title}</span>
                <span className="tl-widget-span-badge" title="Column width in 12-column grid">
                  {w.colSpan}/12
                </span>
              </div>

              {editable && (
                <div className="tl-widget-controls">
                  {/* Reorder Buttons */}
                  <button
                    type="button"
                    className="tl-btn tl-btn-icon tl-btn-sm"
                    onClick={() => handleMoveEarlier(w.id)}
                    disabled={isFirst}
                    title="Move earlier (Arrow Left/Up)"
                    aria-label={`Move ${w.title} earlier`}
                  >
                    ◀
                  </button>
                  <button
                    type="button"
                    className="tl-btn tl-btn-icon tl-btn-sm"
                    onClick={() => handleMoveLater(w.id)}
                    disabled={isLast}
                    title="Move later (Arrow Right/Down)"
                    aria-label={`Move ${w.title} later`}
                  >
                    ▶
                  </button>

                  {/* Resize Buttons */}
                  <button
                    type="button"
                    className="tl-btn tl-btn-icon tl-btn-sm"
                    onClick={() => handleShrinkSpan(w.id)}
                    disabled={w.colSpan <= (w.minColSpan || 3)}
                    title="Shrink width (- key)"
                    aria-label={`Shrink ${w.title} width`}
                  >
                    −
                  </button>
                  <button
                    type="button"
                    className="tl-btn tl-btn-icon tl-btn-sm"
                    onClick={() => handleGrowSpan(w.id)}
                    disabled={w.colSpan >= (w.maxColSpan || 12)}
                    title="Expand width (+ key)"
                    aria-label={`Expand ${w.title} width`}
                  >
                    +
                  </button>

                  {/* Hide Button */}
                  {w.removable !== false && (
                    <button
                      type="button"
                      className="tl-btn tl-btn-icon tl-btn-sm tl-btn-danger"
                      onClick={() => handleHideWidget(w.id)}
                      title="Hide widget (can be restored in Manage Widgets)"
                      aria-label={`Hide ${w.title} widget`}
                    >
                      ×
                    </button>
                  )}
                </div>
              )}
            </div>

            {/* Rendered Widget Content */}
            <div className="tl-widget-content">{renderWidget(w.id)}</div>
          </div>
        );
      })}
    </div>
  );
}

export default LayoutControls;
