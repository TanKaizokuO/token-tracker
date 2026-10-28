import React, { useEffect, useState } from 'react';

type FeeInput = { provider: string; account_alias: string; access_route: string; paid_date: string; amount: string; currency: string; source: string };
type Fee = Omit<FeeInput, 'amount'> & { fee_id: string; amount: number };
const emptyFee: FeeInput = { provider: '', account_alias: '', access_route: '', paid_date: '', amount: '', currency: 'USD', source: '' };

export default function SubscriptionFees({ write }: { write: (resource: string, payload: unknown, method?: 'POST' | 'PATCH') => Promise<unknown> }) {
  const [fees, setFees] = useState<Fee[]>([]);
  const [offset, setOffset] = useState(0);
  const [total, setTotal] = useState<number | null>(null);
  const [form, setForm] = useState<FeeInput>(emptyFee);
  const [editing, setEditing] = useState<string | null>(null);
  const [message, setMessage] = useState('');
  const load = async (pageOffset: number) => {
    const response = await fetch(`/api/v1/subscription-fees?limit=50&offset=${pageOffset}`, { credentials: 'same-origin' });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Could not load subscription fees');
    setFees(data.subscription_fees);
    setTotal(data.total);
  };
  useEffect(() => {
    let active = true;
    setTotal(null);
    fetch(`/api/v1/subscription-fees?limit=50&offset=${offset}`, { credentials: 'same-origin' })
      .then(async response => { const data = await response.json(); if (!response.ok) throw new Error(data.error || 'Could not load subscription fees'); return data; })
      .then(data => { if (active) { setFees(data.subscription_fees); setTotal(data.total); } })
      .catch(error => { if (active) setMessage(error.message); });
    return () => { active = false; };
  }, [offset]);
  const change = (key: keyof FeeInput, value: string) => setForm(previous => ({ ...previous, [key]: value }));
  const save = async (event: React.FormEvent) => {
    event.preventDefault(); setMessage('');
    try {
      await write(editing ? `subscription-fees/${encodeURIComponent(editing)}` : 'subscription-fees', { ...form, amount: Number(form.amount) }, editing ? 'PATCH' : 'POST');
      await load(offset); setForm(emptyFee); setEditing(null); setMessage('Subscription fee saved.');
    } catch (error) { setMessage(error instanceof Error ? error.message : 'Could not save subscription fee'); }
  };
  return <section className="provider-section">
    <h2>Historical subscription fees</h2>
    <p>These account-level payments are an independent, non-additive all-history resource. Dashboard date, provider, account, route and other filters do not apply. They are not allocated to requests or included in request-linked fee totals, billed API spend, or API-equivalent values; no currency conversion is assumed.</p>
    {total === null ? <p>Fee list unavailable or loading.</p> : total === 0 ? <p>No historical fees recorded; total is unknown, not zero.</p> : <><p>Showing {offset + 1}–{offset + fees.length} of {total} recorded payments.</p><ul>{fees.map(fee => <li key={fee.fee_id}>{fee.paid_date} · {fee.provider} · {fee.account_alias} · {fee.access_route} · {fee.amount} {fee.currency} · {fee.source} <button type="button" onClick={() => { setEditing(fee.fee_id); setForm({ provider: fee.provider, account_alias: fee.account_alias, access_route: fee.access_route, paid_date: fee.paid_date, amount: String(fee.amount), currency: fee.currency, source: fee.source }); }}>Edit fee</button></li>)}</ul><button type="button" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - 50))}>Previous fees</button><button type="button" disabled={offset + fees.length >= total} onClick={() => setOffset(offset + 50)}>Next fees</button></>}
    <form onSubmit={save}>
      <label>Provider <input required maxLength={512} value={form.provider} onChange={e => change('provider', e.target.value)} /></label>
      <label>Account alias <input required maxLength={512} value={form.account_alias} onChange={e => change('account_alias', e.target.value)} /></label>
      <label>Access route <input required maxLength={512} value={form.access_route} onChange={e => change('access_route', e.target.value)} /></label>
      <label>Paid date <input required type="date" value={form.paid_date} onChange={e => change('paid_date', e.target.value)} /></label>
      <label>Amount <input required type="number" min="0" step="any" value={form.amount} onChange={e => change('amount', e.target.value)} /></label>
      <label>Currency <input required minLength={3} maxLength={3} pattern="[A-Za-z]{3}" value={form.currency} onChange={e => change('currency', e.target.value)} /></label>
      <label>Payment source <input required maxLength={512} value={form.source} onChange={e => change('source', e.target.value)} placeholder="e.g. invoice" /></label>
      <button type="submit">{editing ? 'Update fee' : 'Save fee'}</button>{editing && <button type="button" onClick={() => { setEditing(null); setForm(emptyFee); }}>Cancel edit</button>}
    </form>
    {message && <p role="status">{message}</p>}
  </section>;
}
