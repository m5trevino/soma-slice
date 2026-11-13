import { useState } from 'react'
import './SessionTable.css'

// One row of the table. Extracted so the flat list and the grouped tree render
// identical rows — a session looks the same whichever view you are in.
function SessionRow({ s, selected, favorites, onToggleSelect, onToggleFavorite, onRowClick, activeRowId }) {
  const isSel = selected.has(s.session_id)
  const isFav = favorites?.has(s.session_id)
  const isActive = activeRowId === s.session_id
  return (
    <tr
      className={`session-row ${isSel ? 'selected' : ''} ${isActive ? 'active' : ''}`}
      onClick={() => onRowClick?.(s.session_id)}
    >
      <td className="col-fav">
        <button
          className="fav-btn"
          onClick={(e) => {
            e.stopPropagation()
            onToggleFavorite?.(s.session_id)
          }}
          title={isFav ? 'Remove from favorites' : 'Add to favorites'}
          style={{ color: isFav ? '#f59e0b' : '#4b5563' }}
        >
          {isFav ? '★' : '☆'}
        </button>
      </td>
      <td className="col-check">
        <input
          type="checkbox"
          checked={isSel}
          onClick={(e) => e.stopPropagation()}
          onChange={() => onToggleSelect(s.session_id)}
        />
      </td>
      <td className="col-name" title={s.session_id}>
        <span className="session-id">{s.session_dir_name}</span>
      </td>
      <td className="col-sets" title={`${s.chat_set_count} chat sets (one user message + one agent reply) — ${s.chat_file_count || 0} files on disk`}>
        <span className="sets-value">{s.chat_set_count}</span>
      </td>
      <td className="col-score">
        {s.total_score.toLocaleString()}
      </td>
      <td className="col-chars">
        {(s.total_session_chars || s.combined_text_length || 0).toLocaleString()}
      </td>
      <td className="col-provider">{s.provider || '—'}</td>
      <td className="col-domain">{s.domain || '—'}</td>
      <td className="col-date">{s.date || '—'}</td>
      <td className="col-title" title={s.title || s.session_id}>{s.title || '—'}</td>
    </tr>
  )
}

const HEAD = (
  <tr>
    <th className="col-fav">★</th>
    <th className="col-check"></th>
    <th className="col-name">Session</th>
    <th className="col-sets" title="Chat sets — one user message + the agent's reply">Sets</th>
    <th className="col-score">Score</th>
    <th className="col-chars">Chars</th>
    <th className="col-provider">Provider</th>
    <th className="col-domain">Domain</th>
    <th className="col-date">Date</th>
    <th className="col-title">Title</th>
  </tr>
)

function SessionTable({ sessions, groups, grouped, selected, favorites, loading, onToggleSelect, onToggleAll, onToggleFavorite, onRowClick, activeRowId }) {
  const [hoveredRow, setHoveredRow] = useState(null)
  const [openGroups, setOpenGroups] = useState(() => new Set())

  if (loading) {
    return (
      <div className="table-loading">
        <span className="spinner"></span>
        Loading sessions...
      </div>
    )
  }

  const toggleGroup = (key) => {
    setOpenGroups((prev) => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
  }

  // ── Grouped view: one tree node per project ──────────────────────────
  if (grouped) {
    const gs = groups || []
    if (gs.length === 0) {
      return (
        <div className="table-empty">
          No sessions match your filters. Try widening them.
        </div>
      )
    }
    return (
      <div className="session-table-wrap">
        <div className="table-toolbar">
          <label className="select-all-label">
            {gs.length} projects · {gs.reduce((n, g) => n + g.session_count, 0)} sessions
            {selected.size > 0 && ` (${selected.size} selected)`}
          </label>
        </div>
        <div className="table-scroll">
          {gs.map((g) => {
            const open = openGroups.has(g.project_key)
            return (
              <div className="group" key={g.project_key}>
                <div
                  className={`group-head ${open ? 'open' : ''}`}
                  onClick={() => toggleGroup(g.project_key)}
                  role="button"
                  tabIndex={0}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter' || e.key === ' ') toggleGroup(g.project_key)
                  }}
                >
                  <span className="group-caret">▶</span>
                  <span className="group-name">{g.project_name}</span>
                  <span className="group-meta">
                    <span>{g.session_count} sessions</span>
                    <span className="group-sets">{g.total_sets.toLocaleString()} sets</span>
                    <span>{g.total_score.toLocaleString()} score</span>
                  </span>
                </div>
                {open && (
                  <div className="group-body">
                    <table className="session-table">
                      <thead>{HEAD}</thead>
                      <tbody>
                        {g.sessions.map((s) => (
                          <SessionRow
                            key={s.session_id}
                            s={s}
                            selected={selected}
                            favorites={favorites}
                            onToggleSelect={onToggleSelect}
                            onToggleFavorite={onToggleFavorite}
                            onRowClick={onRowClick}
                            activeRowId={activeRowId}
                          />
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </div>
            )
          })}
        </div>
      </div>
    )
  }

  if (sessions.length === 0) {
    return (
      <div className="table-empty">
        No sessions match your filters. Try widening them.
      </div>
    )
  }

  return (
    <div className="session-table-wrap">
      <div className="table-toolbar">
        <label className="select-all-label">
          <input
            type="checkbox"
            checked={sessions.length > 0 && selected.size === sessions.length}
            onChange={onToggleAll}
          />
          {selected.size > 0 && ` (${selected.size} selected)`}
        </label>
      </div>

      <div className="table-scroll">
        <table className="session-table">
          <thead>
            <tr>
              <th className="col-fav">★</th>
              <th className="col-check"></th>
              <th className="col-name">Session</th>
              <th className="col-sets" title="Chat sets — one user message + the agent's reply">Sets</th>
              <th className="col-score">Score</th>
              <th className="col-chars">Chars</th>
              <th className="col-provider">Provider</th>
              <th className="col-domain">Domain</th>
              <th className="col-date">Date</th>
              <th className="col-title">Title</th>
            </tr>
          </thead>
          <tbody>
            {sessions.map((s) => (
              <SessionRow
                key={s.session_id}
                s={s}
                selected={selected}
                favorites={favorites}
                onToggleSelect={onToggleSelect}
                onToggleFavorite={onToggleFavorite}
                onRowClick={onRowClick}
                activeRowId={activeRowId}
              />
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

export default SessionTable
