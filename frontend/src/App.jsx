import { useState, useEffect, useCallback } from 'react'
import { api, getToken, setToken, clearToken } from './api'
import './App.css'

// Client-side freshness verdict from FACTS (lag + expected cadence). The API
// reports facts; the UI applies the opinion (facts-not-opinions design).
function lagStatus(lagSeconds, expectedFrequency) {
  if (lagSeconds == null) return 'unknown'
  const day = 86400
  const thresholds = { daily: 2 * day, weekly: 9 * day, monthly: 40 * day }
  const limit = thresholds[expectedFrequency] ?? Infinity
  if (lagSeconds <= limit) return 'ok'
  if (lagSeconds <= limit * 3) return 'warn'
  return 'stale'
}

function fmtLag(s) {
  if (s == null) return '—'
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600)
  if (d > 0) return `${d}d ${h}h`
  const m = Math.floor((s % 3600) / 60)
  return h > 0 ? `${h}h ${m}m` : `${m}m`
}

function fmtTime(iso) {
  if (!iso) return '—'
  return new Date(iso).toLocaleString()
}

function KeyPrompt({ onSubmit }) {
  const [val, setVal] = useState('')
  return (
    <div className="key-prompt">
      <h2>Trading OS Health Console</h2>
      <p>Enter your API key to continue.</p>
      <input type="password" value={val} onChange={e => setVal(e.target.value)}
             placeholder="tos__..." onKeyDown={e => e.key === 'Enter' && val && onSubmit(val)} />
      <button onClick={() => val && onSubmit(val)}>Connect</button>
    </div>
  )
}

export default function App() {
  const [hasKey, setHasKey] = useState(!!getToken())
  const [ping, setPing] = useState(null)
  const [summary, setSummary] = useState(null)
  const [error, setError] = useState(null)
  const [lastRefresh, setLastRefresh] = useState(null)

  const load = useCallback(async () => {
    try {
      const [p, s] = await Promise.all([api.ping(), api.summary()])
      setPing(p); setSummary(s); setError(null)
      setLastRefresh(new Date())
    } catch (e) {
      if (e.message === 'unauthorized') { clearToken(); setHasKey(false); setError('Invalid API key.') }
      else setError(e.message)
    }
  }, [])

  useEffect(() => {
    if (!hasKey) return
    load()
    const id = setInterval(load, 30000)  // poll every 30s
    return () => clearInterval(id)
  }, [hasKey, load])

  if (!hasKey) {
    return <KeyPrompt onSubmit={k => { setToken(k); setHasKey(true) }} />
  }

  const sources = summary?.sources ?? []
  const jobs = summary?.recent_jobs ?? []
  const failures = summary?.recent_failures ?? []
  const dqFailures = summary?.recent_dq_failures ?? []

  return (
    <div className="console">
      <header className="banner">
        <div>
          <span className="title">Trading OS Health</span>
          {ping && <span className="meta">v{ping.version} · {ping.git_sha} · db {ping.db_connected ? 'up' : 'DOWN'}</span>}
        </div>
        <div className="refresh">
          Last refreshed: {lastRefresh ? lastRefresh.toLocaleTimeString() : '—'}
          <button onClick={load}>↻</button>
          <button onClick={() => { clearToken(); setHasKey(false) }}>log out</button>
        </div>
      </header>

      {error && <div className="alert error">Error: {error}</div>}

      {(failures.length > 0 || dqFailures.length > 0) && (
        <div className="alert warn">
          {failures.length > 0 && <div>⚠ {failures.length} failed job(s)</div>}
          {dqFailures.length > 0 && <div>⚠ {dqFailures.length} failed DQ check(s)</div>}
        </div>
      )}

      <section>
        <h3>Pipelines</h3>
        <table>
          <thead><tr><th></th><th>Source</th><th>Dataset</th><th>Kind</th><th>Last capture</th><th>Cadence</th><th>Lag</th><th>Status</th></tr></thead>
          <tbody>
            {sources.map(s => {
              const st = s.retired ? 'retired' : lagStatus(s.lag_seconds, s.expected_frequency)
              return (
                <tr key={`${s.name}/${s.dataset}`} className={s.retired ? 'retired-row' : ''}>
                  <td><span className={`dot ${st}`} />{s.critical && <span className="crit" title="critical pipeline">*</span>}</td>
                  <td>{s.name}</td><td>{s.dataset}</td><td>{s.kind}</td>
                  <td>{fmtTime(s.last_batch_at)}</td>
                  <td>{s.expected_frequency}</td>
                  <td>{fmtLag(s.lag_seconds)}</td>
                  <td>{s.retired ? 'retired' : (s.last_status ?? '—')}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </section>

      <section>
        <h3>Recent jobs</h3>
        <table>
          <thead><tr><th>Batch</th><th>Dataset</th><th>Status</th><th>Started</th><th>Rows out</th></tr></thead>
          <tbody>
            {jobs.map(j => (
              <tr key={j.batch_id}>
                <td>{j.batch_id}</td><td>{j.dataset}</td>
                <td className={j.status === 'failed' ? 'bad' : ''}>{j.status}</td>
                <td>{fmtTime(j.started_at)}</td>
                <td>{j.rows_out ?? '—'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
    </div>
  )
}
