import { useSyncExternalStore } from 'react';

// Single money formatter for the dashboard. Costs are stored in USD by the API;
// the display currency is INR, converted with the dated USD->INR rate the API
// reports (historical_fx via /api/v1/pricing/fx or pricing_fx.fx_rate_applied).
// When no dated rate is known, INR shows as unavailable and the USD amount is
// kept alongside it so no value is silently dropped.

export const INR_UNAVAILABLE = 'INR unavailable';

let inrPerUsd: number | null = null;
const listeners = new Set<() => void>();

const toFiniteNumber = (value: unknown): number | null => {
  if (value === null || value === undefined || value === '') return null;
  const n = typeof value === 'number' ? value : Number(value);
  return Number.isFinite(n) ? n : null;
};

export function setInrPerUsd(rate: unknown): void {
  const n = toFiniteNumber(rate);
  const next = n !== null && n > 0 ? n : null;
  if (next === inrPerUsd) return;
  inrPerUsd = next;
  listeners.forEach((listener) => listener());
}

export function getInrPerUsd(): number | null {
  return inrPerUsd;
}

function subscribeInrPerUsd(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

// Re-renders the calling component whenever the dated FX rate changes.
export function useInrPerUsd(): number | null {
  return useSyncExternalStore(subscribeInrPerUsd, getInrPerUsd, getInrPerUsd);
}

const inrFormatters = new Map<number, Intl.NumberFormat>();
const usdFormatters = new Map<number, Intl.NumberFormat>();

function formatterFor(map: Map<number, Intl.NumberFormat>, currency: 'INR' | 'USD', digits: number): Intl.NumberFormat {
  let formatter = map.get(digits);
  if (!formatter) {
    formatter = new Intl.NumberFormat(currency === 'INR' ? 'en-IN' : 'en-US', {
      style: 'currency',
      currency,
      minimumFractionDigits: digits,
      maximumFractionDigits: digits,
    });
    map.set(digits, formatter);
  }
  return formatter;
}

// Format an amount already expressed in INR, e.g. 133783.96 -> "₹1,33,783.96".
export function formatInr(value: unknown, digits = 2, missing = INR_UNAVAILABLE): string {
  const n = toFiniteNumber(value);
  if (n === null) return missing;
  return formatterFor(inrFormatters, 'INR', digits).format(n);
}

// Format a USD amount as secondary text, e.g. 1406.18 -> "$1,406.18".
export function formatUsdSecondary(value: unknown, digits = 2): string {
  const n = toFiniteNumber(value);
  if (n === null) return '';
  return formatterFor(usdFormatters, 'USD', digits).format(n);
}

// Convert a USD amount with the active dated rate. Returns null when no rate or value.
export function usdToInr(usd: unknown): number | null {
  const n = toFiniteNumber(usd);
  const rate = inrPerUsd;
  return n === null || rate === null ? null : n * rate;
}

// Primary display for a USD cost: INR when a dated rate is known, otherwise
// "INR unavailable" plus the USD amount.
export function formatUsdAsInr(usd: unknown, digits = 2, missing = INR_UNAVAILABLE): string {
  const n = toFiniteNumber(usd);
  if (n === null) return missing;
  const inr = usdToInr(n);
  if (inr === null) return `${INR_UNAVAILABLE} (${formatUsdSecondary(n, digits)})`;
  return formatInr(inr, digits, missing);
}

// Prefer an INR value the API already computed with its dated FX; otherwise convert the USD amount.
export function formatMoneyInr(usd: unknown, apiInr?: unknown, digits = 2, missing = INR_UNAVAILABLE): string {
  const direct = toFiniteNumber(apiInr);
  if (direct !== null) return formatInr(direct, digits, missing);
  return formatUsdAsInr(usd, digits, missing);
}
