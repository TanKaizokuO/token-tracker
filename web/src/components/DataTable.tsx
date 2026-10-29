import React, { useState, useMemo, useCallback, useRef, useEffect } from 'react';
import './Interaction.css';
import { formatUsdAsInr } from '../lib/money';

// ============================================================================
// Column Definition & Table Types (WP22)
// ============================================================================

export interface ColumnDef<T = Record<string, unknown>> {
  id: string;
  header: string | React.ReactNode;
  accessor?: keyof T | ((row: T) => unknown);
  cell?: (value: unknown, row: T, index: number) => React.ReactNode;
  sortable?: boolean;
  filterable?: boolean;
  width?: string | number;
  minWidth?: string | number;
  align?: 'left' | 'center' | 'right';
  formatter?: (value: unknown, row: T) => string;
  defaultVisible?: boolean;
}

export interface SortState {
  columnId: string;
  direction: 'asc' | 'desc';
}

export interface DataTableProps<T = Record<string, unknown>> {
  data: T[];
  columns: ColumnDef<T>[];
  keyField: keyof T | ((row: T) => string | number);
  title?: string;
  subtitle?: string;
  searchPlaceholder?: string;
  defaultSort?: SortState;
  pageSizeOptions?: number[];
  defaultPageSize?: number;
  selectable?: boolean;
  selectedKeys?: Set<string | number>;
  onSelectionChange?: (selectedKeys: Set<string | number>, selectedRows: T[]) => void;
  onRowClick?: (row: T) => void;
  exportFileName?: string;
  actions?: React.ReactNode;
  emptyMessage?: string;
  loading?: boolean;
  compact?: boolean;
}

// ============================================================================
// CSV Export & Formula Injection Defense (WP22 Security Invariant)
// ============================================================================

/**
 * Sanitizes an individual cell value against CSV / Spreadsheet Formula Injection (CWE-1236).
 * Spreadsheets (Excel, Calc, Google Sheets) execute cells beginning with `=`, `+`, `-`, `@`,
 * tab (0x09), or carriage return (0x0D) as formulas or dynamic data exchange (DDE) commands.
 * Prepending a single quote `'` forces spreadsheet parsers to treat the content as literal text.
 */
export function sanitizeCsvCell(val: unknown): string {
  if (val === null || val === undefined) return '';

  let str: string;
  if (typeof val === 'object') {
    if (val instanceof Date) {
      str = val.toISOString();
    } else {
      str = JSON.stringify(val);
    }
  } else {
    str = String(val);
  }

  // Strip leading whitespace to test the true first operational character
  const trimmed = str.trimStart();
  const dangerousPrefixes = ['=', '+', '-', '@', '\t', '\r'];

  if (dangerousPrefixes.some((prefix) => trimmed.startsWith(prefix))) {
    // Prepend single quote to neutralize formula evaluation
    str = `'${str}`;
  }

  // Escape internal double quotes by doubling them, wrap in quotes if contains comma, quote, or newline
  if (str.includes('"') || str.includes(',') || str.includes('\n') || str.includes('\r')) {
    str = `"${str.replace(/"/g, '""')}"`;
  }

  return str;
}

/**
 * Exports specified dataset rows to a sanitized, downloadable CSV file.
 */
export function exportToCsv<T>(
  rows: T[],
  columns: ColumnDef<T>[],
  fileName: string = 'token_ledger_export.csv'
): void {
  if (!rows || rows.length === 0) return;

  const visibleCols = columns.filter((col) => col.defaultVisible !== false);

  // 1. Headers
  const headerRow = visibleCols
    .map((col) => {
      const headerText = typeof col.header === 'string' ? col.header : col.id;
      return sanitizeCsvCell(headerText);
    })
    .join(',');

  // 2. Data Rows
  const csvLines = [headerRow];

  for (const row of rows) {
    const rowRecord = row as Record<string, unknown>;
    const rowValues = visibleCols.map((col) => {
      let rawVal: unknown;
      if (typeof col.accessor === 'function') {
        rawVal = col.accessor(row);
      } else if (col.accessor) {
        rawVal = row[col.accessor];
      } else {
        rawVal = rowRecord[col.id];
      }

      if (col.formatter) {
        return sanitizeCsvCell(col.formatter(rawVal, row));
      }
      return sanitizeCsvCell(rawVal);
    });
    csvLines.push(rowValues.join(','));
  }

  const csvContent = '\uFEFF' + csvLines.join('\r\n'); // UTF-8 BOM for Excel
  const blob = new Blob([csvContent], { type: 'text/csv;charset=utf-8;' });
  const url = URL.createObjectURL(blob);

  const link = document.createElement('a');
  link.href = url;
  link.setAttribute('download', fileName.endsWith('.csv') ? fileName : `${fileName}.csv`);
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
  URL.revokeObjectURL(url);
}

// ============================================================================
// DataTable Component Implementation
// ============================================================================

export function DataTable<T = Record<string, unknown>>({
  data,
  columns,
  keyField,
  title,
  subtitle,
  searchPlaceholder = 'Search table...',
  defaultSort,
  pageSizeOptions = [10, 25, 50, 100],
  defaultPageSize = 25,
  selectable = false,
  selectedKeys: controlledSelectedKeys,
  onSelectionChange,
  onRowClick,
  exportFileName = 'token_ledger_data.csv',
  actions,
  emptyMessage = 'No matching records found.',
  loading = false,
  compact = false,
}: DataTableProps<T>) {
  // Row key resolver
  const getRowKey = useCallback(
    (row: T): string | number => {
      if (typeof keyField === 'function') return keyField(row);
      const rowRecord = row as Record<string, unknown>;
      return rowRecord[keyField as string] as string | number;
    },
    [keyField]
  );

  // 1. Column Visibility State
  const [columnVisibility, setColumnVisibility] = useState<Record<string, boolean>>(() => {
    const initial: Record<string, boolean> = {};
    for (const col of columns) {
      initial[col.id] = col.defaultVisible !== false;
    }
    return initial;
  });

  const [showColumnPicker, setShowColumnPicker] = useState<boolean>(false);
  const columnPickerRef = useRef<HTMLDivElement>(null);

  // Close column popover on outside click
  useEffect(() => {
    const handleClickOutside = (e: MouseEvent) => {
      if (columnPickerRef.current && !columnPickerRef.current.contains(e.target as Node)) {
        setShowColumnPicker(false);
      }
    };
    if (showColumnPicker) {
      document.addEventListener('mousedown', handleClickOutside);
    }
    return () => {
      document.removeEventListener('mousedown', handleClickOutside);
    };
  }, [showColumnPicker]);

  const visibleColumns = useMemo(() => {
    return columns.filter((col) => columnVisibility[col.id] !== false);
  }, [columns, columnVisibility]);

  // 2. Search State
  const [searchTerm, setSearchTerm] = useState<string>('');

  // 3. Sorting State
  const [sortState, setSortState] = useState<SortState | null>(defaultSort || null);

  const handleHeaderSort = (columnId: string, sortable?: boolean) => {
    if (sortable === false) return;

    setSortState((prev) => {
      if (!prev || prev.columnId !== columnId) {
        return { columnId, direction: 'asc' };
      }
      if (prev.direction === 'asc') {
        return { columnId, direction: 'desc' };
      }
      return null; // toggle back to unordered
    });
  };

  // 4. Filtering and Sorting Pipeline
  const filteredData = useMemo(() => {
    let result = [...data];

    // Search query filter
    const query = searchTerm.trim().toLowerCase();
    if (query) {
      result = result.filter((row) => {
        const rowRecord = row as Record<string, unknown>;
        return visibleColumns.some((col) => {
          let cellVal: unknown;
          if (typeof col.accessor === 'function') {
            cellVal = col.accessor(row);
          } else if (col.accessor) {
            cellVal = row[col.accessor];
          } else {
            cellVal = rowRecord[col.id];
          }

          if (cellVal === null || cellVal === undefined) return false;
          const strVal = typeof cellVal === 'object' ? JSON.stringify(cellVal) : String(cellVal);
          return strVal.toLowerCase().includes(query);
        });
      });
    }

    // Sort order
    if (sortState) {
      const activeCol = columns.find((c) => c.id === sortState.columnId);
      if (activeCol) {
        const factor = sortState.direction === 'asc' ? 1 : -1;
        result.sort((a, b) => {
          const aRec = a as Record<string, unknown>;
          const bRec = b as Record<string, unknown>;
          let valA: unknown;
          let valB: unknown;

          if (typeof activeCol.accessor === 'function') {
            valA = activeCol.accessor(a);
            valB = activeCol.accessor(b);
          } else if (activeCol.accessor) {
            valA = a[activeCol.accessor];
            valB = b[activeCol.accessor];
          } else {
            valA = aRec[activeCol.id];
            valB = bRec[activeCol.id];
          }

          if (valA === valB) return 0;
          if (valA === null || valA === undefined) return 1;
          if (valB === null || valB === undefined) return -1;

          if (typeof valA === 'number' && typeof valB === 'number') {
            return (valA - valB) * factor;
          }
          if (typeof valA === 'boolean' && typeof valB === 'boolean') {
            return (valA === valB ? 0 : valA ? 1 : -1) * factor;
          }

          return String(valA).localeCompare(String(valB), undefined, { numeric: true }) * factor;
        });
      }
    }

    return result;
  }, [data, searchTerm, sortState, visibleColumns, columns]);

  // 5. Pagination State
  const [pageSize, setPageSize] = useState<number>(defaultPageSize);
  const [currentPage, setCurrentPage] = useState<number>(1);

  // Reset page when search or data changes
  useEffect(() => {
    setCurrentPage(1);
  }, [searchTerm, pageSize]);

  const totalPages = Math.max(1, Math.ceil(filteredData.length / pageSize));
  const activePage = Math.min(currentPage, totalPages);

  const paginatedData = useMemo(() => {
    const startIndex = (activePage - 1) * pageSize;
    return filteredData.slice(startIndex, startIndex + pageSize);
  }, [filteredData, activePage, pageSize]);

  // 6. Selection Management
  const [internalSelectedKeys, setInternalSelectedKeys] = useState<Set<string | number>>(new Set());
  const selectedKeys = controlledSelectedKeys ?? internalSelectedKeys;

  const updateSelection = useCallback(
    (newKeys: Set<string | number>) => {
      if (!controlledSelectedKeys) {
        setInternalSelectedKeys(newKeys);
      }
      if (onSelectionChange) {
        const keyMap = new Set(newKeys);
        const selectedRows = data.filter((row) => keyMap.has(getRowKey(row)));
        onSelectionChange(newKeys, selectedRows);
      }
    },
    [controlledSelectedKeys, onSelectionChange, data, getRowKey]
  );

  const handleToggleRow = (rowKey: string | number, e: React.SyntheticEvent) => {
    e.stopPropagation();
    const next = new Set(selectedKeys);
    if (next.has(rowKey)) {
      next.delete(rowKey);
    } else {
      next.add(rowKey);
    }
    updateSelection(next);
  };

  const handleToggleAllVisible = (e: React.ChangeEvent<HTMLInputElement>) => {
    const next = new Set(selectedKeys);
    if (e.target.checked) {
      for (const row of paginatedData) {
        next.add(getRowKey(row));
      }
    } else {
      for (const row of paginatedData) {
        next.delete(getRowKey(row));
      }
    }
    updateSelection(next);
  };

  const selectAllCheckboxRef = useRef<HTMLInputElement>(null);
  const numVisibleSelected = useMemo(() => {
    return paginatedData.filter((r) => selectedKeys.has(getRowKey(r))).length;
  }, [paginatedData, selectedKeys, getRowKey]);

  useEffect(() => {
    if (selectAllCheckboxRef.current) {
      const allSelected = paginatedData.length > 0 && numVisibleSelected === paginatedData.length;
      const someSelected = numVisibleSelected > 0 && numVisibleSelected < paginatedData.length;
      selectAllCheckboxRef.current.checked = allSelected;
      selectAllCheckboxRef.current.indeterminate = someSelected;
    }
  }, [numVisibleSelected, paginatedData.length]);

  // 7. CSV Export Handler
  const handleExportCsv = (selectedOnly: boolean) => {
    let rowsToExport: T[];
    if (selectedOnly && selectedKeys.size > 0) {
      const keySet = new Set(selectedKeys);
      rowsToExport = data.filter((r) => keySet.has(getRowKey(r)));
    } else {
      rowsToExport = filteredData;
    }
    exportToCsv(rowsToExport, visibleColumns, exportFileName);
  };

  return (
    <div className={`tl-table-card ${compact ? 'is-compact' : ''}`}>
      {/* Top Header / Title / Action Toolbar */}
      {(title || subtitle || actions) && (
        <div className="tl-table-toolbar">
          <div className="tl-table-toolbar-left">
            {title && <span className="tl-modal-title">{title}</span>}
            {subtitle && <span className="tl-chart-subtitle">{subtitle}</span>}
          </div>
          <div className="tl-table-toolbar-right">{actions}</div>
        </div>
      )}

      {/* Filter / Search / Column Config / Export Toolbar */}
      <div className="tl-table-toolbar">
        <div className="tl-table-toolbar-left">
          {/* Search Box */}
          <div className="tl-table-search-box">
            <span className="tl-table-search-icon">🔍</span>
            <input
              type="text"
              className="tl-table-search-input"
              placeholder={searchPlaceholder}
              value={searchTerm}
              onChange={(e) => setSearchTerm(e.target.value)}
              aria-label="Filter rows"
            />
            {searchTerm && (
              <button
                type="button"
                className="tl-table-search-clear"
                onClick={() => setSearchTerm('')}
                aria-label="Clear search"
              >
                ✕
              </button>
            )}
          </div>

          {/* Quick Row Count Pill */}
          <span className="tl-widget-span-badge">
            {filteredData.length} {filteredData.length === 1 ? 'row' : 'rows'}
          </span>
        </div>

        <div className="tl-table-toolbar-right">
          {/* Column Visibility Selector */}
          <div style={{ position: 'relative' }} ref={columnPickerRef}>
            <button
              type="button"
              className="tl-btn tl-btn-sm"
              onClick={() => setShowColumnPicker((prev) => !prev)}
              aria-label="Toggle columns menu"
              aria-expanded={showColumnPicker}
            >
              <span>Columns ({visibleColumns.length}/{columns.length})</span>
              <span>▾</span>
            </button>

            {showColumnPicker && (
              <div className="tl-column-popover" role="menu">
                <div style={{ display: 'flex', justifyContent: 'space-between', paddingBottom: 6, borderBottom: '1px solid var(--tl-border-subtle)' }}>
                  <button
                    type="button"
                    className="tl-btn tl-btn-sm"
                    onClick={() => {
                      const all: Record<string, boolean> = {};
                      for (const col of columns) all[col.id] = true;
                      setColumnVisibility(all);
                    }}
                  >
                    Select All
                  </button>
                  <button
                    type="button"
                    className="tl-btn tl-btn-sm"
                    onClick={() => {
                      const reset: Record<string, boolean> = {};
                      for (const col of columns) reset[col.id] = col.defaultVisible !== false;
                      setColumnVisibility(reset);
                    }}
                  >
                    Reset
                  </button>
                </div>
                {columns.map((col) => (
                  <label key={col.id} className="tl-column-option">
                    <input
                      type="checkbox"
                      checked={columnVisibility[col.id] !== false}
                      onChange={(e) => {
                        setColumnVisibility((prev) => ({
                          ...prev,
                          [col.id]: e.target.checked,
                        }));
                      }}
                    />
                    <span>{typeof col.header === 'string' ? col.header : col.id}</span>
                  </label>
                ))}
              </div>
            )}
          </div>

          {/* CSV Export Button (Formula-Injection Protected) */}
          <button
            type="button"
            className="tl-btn tl-btn-sm"
            onClick={() => handleExportCsv(selectedKeys.size > 0)}
            title="Export sanitized CSV with formula injection defense"
            aria-label="Export CSV"
          >
            <span>📥</span>
            <span>
              {selectedKeys.size > 0 ? `Export Selected (${selectedKeys.size})` : 'Export CSV'}
            </span>
          </button>
        </div>
      </div>

      {/* Bulk Selection Notification Bar */}
      {selectable && selectedKeys.size > 0 && (
        <div className="tl-table-selection-bar">
          <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
            <span>
              <strong>{selectedKeys.size}</strong> item{selectedKeys.size === 1 ? '' : 's'} selected across all pages
            </span>
            {selectedKeys.size < filteredData.length && (
              <button
                type="button"
                className="tl-btn tl-btn-sm"
                onClick={() => {
                  const allKeys = new Set(filteredData.map(getRowKey));
                  updateSelection(allKeys);
                }}
              >
                Select all {filteredData.length} filtered items
              </button>
            )}
          </div>
          <button
            type="button"
            className="tl-btn tl-btn-sm tl-btn-danger"
            onClick={() => updateSelection(new Set())}
          >
            Clear Selection
          </button>
        </div>
      )}

      {/* Main Table Content */}
      <div className="tl-table-scroll-wrapper" role="region" aria-label="Data table">
        <table className="tl-table">
          <thead>
            <tr>
              {selectable && (
                <th style={{ width: 36, textAlign: 'center' }}>
                  <input
                    type="checkbox"
                    ref={selectAllCheckboxRef}
                    onChange={handleToggleAllVisible}
                    aria-label="Select all visible rows"
                  />
                </th>
              )}
              {visibleColumns.map((col) => {
                const isSorted = sortState?.columnId === col.id;
                const sortDir = isSorted ? sortState.direction : undefined;
                const isSortable = col.sortable !== false;

                return (
                  <th
                    key={col.id}
                    className={`${isSortable ? 'is-sortable' : ''} ${
                      col.align === 'right' ? 'align-right' : col.align === 'center' ? 'align-center' : ''
                    }`}
                    style={{ width: col.width, minWidth: col.minWidth }}
                    onClick={() => handleHeaderSort(col.id, isSortable)}
                    tabIndex={isSortable ? 0 : undefined}
                    onKeyDown={(e) => {
                      if (isSortable && (e.key === 'Enter' || e.key === ' ')) {
                        e.preventDefault();
                        handleHeaderSort(col.id, true);
                      }
                    }}
                    role={isSortable ? 'button' : undefined}
                    aria-sort={
                      isSorted ? (sortDir === 'asc' ? 'ascending' : 'descending') : undefined
                    }
                  >
                    <div className="tl-th-inner">
                      <span>{col.header}</span>
                      {isSortable && (
                        <span className="tl-sort-indicator" aria-hidden="true">
                          {isSorted ? (sortDir === 'asc' ? '▲' : '▼') : '↕'}
                        </span>
                      )}
                    </div>
                  </th>
                );
              })}
            </tr>
          </thead>
          <tbody>
            {loading ? (
              <tr>
                <td
                  colSpan={visibleColumns.length + (selectable ? 1 : 0)}
                  style={{ textAlign: 'center', padding: '40px 16px' }}
                >
                  <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 8 }}>
                    <div className="tl-spinner" />
                    <span className="tl-state-text">Loading dataset...</span>
                  </div>
                </td>
              </tr>
            ) : paginatedData.length === 0 ? (
              <tr>
                <td
                  colSpan={visibleColumns.length + (selectable ? 1 : 0)}
                  style={{ textAlign: 'center', padding: '40px 16px', color: 'var(--tl-text-muted)' }}
                >
                  {emptyMessage}
                </td>
              </tr>
            ) : (
              paginatedData.map((row, rowIdx) => {
                const key = getRowKey(row);
                const isSelected = selectedKeys.has(key);
                const rowRecord = row as Record<string, unknown>;

                return (
                  <tr
                    key={String(key)}
                    className={isSelected ? 'is-selected' : ''}
                    onClick={() => onRowClick && onRowClick(row)}
                    style={{ cursor: onRowClick ? 'pointer' : 'default' }}
                  >
                    {selectable && (
                      <td style={{ textAlign: 'center' }} onClick={(e) => e.stopPropagation()}>
                        <input
                          type="checkbox"
                          checked={isSelected}
                          onChange={(e) => handleToggleRow(key, e)}
                          aria-label={`Select row ${rowIdx + 1}`}
                        />
                      </td>
                    )}
                    {visibleColumns.map((col) => {
                      let cellVal: unknown;
                      if (typeof col.accessor === 'function') {
                        cellVal = col.accessor(row);
                      } else if (col.accessor) {
                        cellVal = row[col.accessor];
                      } else {
                        cellVal = rowRecord[col.id];
                      }

                      let renderedContent: React.ReactNode;
                      if (col.cell) {
                        renderedContent = col.cell(cellVal, row, rowIdx);
                      } else if (col.formatter) {
                        renderedContent = col.formatter(cellVal, row);
                      } else if (cellVal === null || cellVal === undefined) {
                        renderedContent = <span style={{ color: 'var(--tl-text-muted)' }}>-</span>;
                      } else if (typeof cellVal === 'boolean') {
                        renderedContent = cellVal ? 'Yes' : 'No';
                      } else if (typeof cellVal === 'number' && /_usd$/.test(String(col.accessor ?? col.id))) {
                        // USD cost fields display as INR (dated FX); CSV export keeps the raw columns.
                        renderedContent = formatUsdAsInr(cellVal, Math.abs(cellVal) > 0 && Math.abs(cellVal) < 0.01 ? 6 : 2);
                      } else {
                        renderedContent = String(cellVal);
                      }

                      return (
                        <td
                          key={col.id}
                          className={`${
                            col.align === 'right' ? 'align-right' : col.align === 'center' ? 'align-center' : ''
                          }`}
                        >
                          {renderedContent}
                        </td>
                      );
                    })}
                  </tr>
                );
              })
            )}
          </tbody>
        </table>
      </div>

      {/* Pagination Bar */}
      <div className="tl-pagination-bar">
        <div className="tl-pagination-info">
          <span>
            Showing{' '}
            <strong>
              {filteredData.length === 0 ? 0 : (activePage - 1) * pageSize + 1}–
              {Math.min(activePage * pageSize, filteredData.length)}
            </strong>{' '}
            of <strong>{filteredData.length}</strong> records
          </span>

          {/* Page Size Selector */}
          <div className="tl-page-size-selector">
            <span style={{ fontSize: 11, color: 'var(--tl-text-muted)' }}>Rows per page:</span>
            <select
              className="tl-layout-select"
              value={pageSize}
              onChange={(e) => setPageSize(Number(e.target.value))}
              aria-label="Rows per page"
            >
              {pageSizeOptions.map((opt) => (
                <option key={opt} value={opt}>
                  {opt}
                </option>
              ))}
            </select>
          </div>
        </div>

        {/* Navigation Buttons */}
        <div className="tl-pagination-nav">
          <button
            type="button"
            className="tl-btn tl-btn-sm tl-btn-icon"
            onClick={() => setCurrentPage(1)}
            disabled={activePage <= 1}
            title="First page"
            aria-label="First page"
          >
            ⏮
          </button>
          <button
            type="button"
            className="tl-btn tl-btn-sm tl-btn-icon"
            onClick={() => setCurrentPage((p) => Math.max(1, p - 1))}
            disabled={activePage <= 1}
            title="Previous page"
            aria-label="Previous page"
          >
            ◀
          </button>

          <span style={{ margin: '0 6px', fontSize: 12 }}>
            Page <strong>{activePage}</strong> of <strong>{totalPages}</strong>
          </span>

          <button
            type="button"
            className="tl-btn tl-btn-sm tl-btn-icon"
            onClick={() => setCurrentPage((p) => Math.min(totalPages, p + 1))}
            disabled={activePage >= totalPages}
            title="Next page"
            aria-label="Next page"
          >
            ▶
          </button>
          <button
            type="button"
            className="tl-btn tl-btn-sm tl-btn-icon"
            onClick={() => setCurrentPage(totalPages)}
            disabled={activePage >= totalPages}
            title="Last page"
            aria-label="Last page"
          >
            ⏭
          </button>
        </div>
      </div>
    </div>
  );
}

export default DataTable;
