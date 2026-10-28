import React, { useEffect, useState } from 'react';

interface Outcome {
  outcome_id: string;
  entity_type: string;
  entity_id: string;
  account_alias: string;
  task_label: string | null;
  completion_rating: string | null;
  manual_rating: number | null;
  was_corrected: boolean | null;
  was_restarted: boolean | null;
  is_inferred: boolean;
  source: string;
}

export default function OutcomeEditor({ sessionId, requestIds, write }: {
  sessionId: string;
  requestIds: string[];
  write: (resource: string, payload: unknown, method?: 'POST' | 'PATCH') => Promise<unknown>;
}) {
  const [entityId, setEntityId] = useState(sessionId);
  const [records, setRecords] = useState<Outcome[]>([]);
  const [editing, setEditing] = useState<Outcome | null>(null);
  const [label, setLabel] = useState('');
  const [status, setStatus] = useState('');
  const [rating, setRating] = useState('');
  const [account, setAccount] = useState('default');
  const [corrected, setCorrected] = useState('unknown');
  const [restarted, setRestarted] = useState('unknown');
  const [message, setMessage] = useState('');
  const entityType = entityId === sessionId ? 'session' : 'request';

  const load = async () => {
    const response = await fetch(`/api/v1/outcomes?entity_type=${entityType}&entity_id=${encodeURIComponent(entityId)}&limit=100`, { credentials: 'same-origin' });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Could not load outcomes');
    setRecords(data.outcomes);
  };
  useEffect(() => {
    setEditing(null); setRecords([]); setLabel(''); setStatus(''); setRating(''); setAccount('default'); setCorrected('unknown'); setRestarted('unknown'); setMessage('');
    let active = true;
    fetch(`/api/v1/outcomes?entity_type=${entityType}&entity_id=${encodeURIComponent(entityId)}&limit=100`, { credentials: 'same-origin' })
      .then(async response => { const data = await response.json(); if (!response.ok) throw new Error(data.error || 'Could not load outcomes'); return data; })
      .then(data => { if (active) setRecords(data.outcomes); })
      .catch(error => { if (active) { setRecords([]); setMessage(error.message); } });
    return () => { active = false; };
  }, [entityId, entityType]);

  const edit = (record: Outcome) => {
    setEditing(record); setLabel(record.task_label ?? ''); setStatus(record.completion_rating ?? '');
    setRating(record.manual_rating == null ? '' : String(record.manual_rating)); setAccount(record.account_alias);
    setCorrected(record.was_corrected == null ? 'unknown' : String(record.was_corrected));
    setRestarted(record.was_restarted == null ? 'unknown' : String(record.was_restarted));
  };
  const save = async (event: React.FormEvent) => {
    event.preventDefault(); setMessage('');
    const payload: Record<string, unknown> = { task_label: label, completion_rating: status };
    payload.manual_rating = rating === '' ? null : Number(rating);
    if (corrected !== 'unknown') payload.was_corrected = corrected === 'true';
    if (restarted !== 'unknown') payload.was_restarted = restarted === 'true';
    if (!editing) Object.assign(payload, { entity_type: entityType, entity_id: entityId, account_alias: account });
    try {
      await write(editing ? `outcomes/${encodeURIComponent(editing.outcome_id)}` : 'outcomes', payload, editing ? 'PATCH' : 'POST');
      await load(); setMessage('Manual outcome saved.'); setEditing(null); setLabel(''); setStatus(''); setRating(''); setCorrected('unknown'); setRestarted('unknown');
    } catch (error) { setMessage(error instanceof Error ? error.message : 'Could not save outcome'); }
  };
  return <details>
    <summary>Manual task and outcome</summary>
    <p>Manual records are user-owned, not inferred. Session outcomes cover linked requests. The outcome query uses a request outcome as a session fallback when no session outcome exists. No rating is assumed.</p>
    <label>Outcome scope <select value={entityId} onChange={e => setEntityId(e.target.value)}><option value={sessionId}>Session: {sessionId}</option>{requestIds.map(id => <option key={id} value={id}>Request: {id}</option>)}</select></label>
    <ul>{records.map(record => <li key={record.outcome_id}>{record.task_label || 'Unlabeled'} · {record.completion_rating || 'Unknown'} · {record.is_inferred ? 'Inferred' : `Manual (${record.source})`} · rating {record.manual_rating == null ? 'unknown' : record.manual_rating} {!record.is_inferred && <button type="button" onClick={() => edit(record)}>Edit outcome</button>}</li>)}</ul>
    <form onSubmit={save}>
      <label>Account alias <input required maxLength={512} value={account} disabled={!!editing} onChange={e => setAccount(e.target.value)} /></label>
      <label>Task label <input required maxLength={512} value={label} onChange={e => setLabel(e.target.value)} placeholder="e.g. debugging" /></label>
      <label>Outcome status <select required value={status} onChange={e => setStatus(e.target.value)}><option value="">Choose status</option><option value="complete">Complete / success</option><option value="partial">Partial</option><option value="unsuccessful">Unsuccessful / failure</option><option value="abandoned">Abandoned</option></select></label>
      <label>Manual rating (optional, 0–10; blank means unknown) <input type="number" min="0" max="10" step="any" value={rating} onChange={e => setRating(e.target.value)} /></label>
      <label>Corrected <select value={corrected} onChange={e => setCorrected(e.target.value)}><option value="unknown" disabled={!!editing && editing.was_corrected != null}>Unknown</option><option value="true">Yes</option><option value="false">No</option></select></label>
      <label>Restarted <select value={restarted} onChange={e => setRestarted(e.target.value)}><option value="unknown" disabled={!!editing && editing.was_restarted != null}>Unknown</option><option value="true">Yes</option><option value="false">No</option></select></label>
      <button type="submit">{editing ? 'Update outcome' : 'Save outcome'}</button>{editing && <button type="button" onClick={() => { setEditing(null); setLabel(''); setStatus(''); setRating(''); setAccount('default'); setCorrected('unknown'); setRestarted('unknown'); }}>Cancel edit</button>}
    </form>
    {message && <p role="status">{message}</p>}
  </details>;
}
