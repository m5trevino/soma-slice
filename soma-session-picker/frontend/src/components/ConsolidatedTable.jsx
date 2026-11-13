// Consolidated sessions table — the primary view. Shows one row per
// consolidated chain (a deduped, chronological merge of a conversation's
// generations), reusing the same row/checkbox/favorite styling as SessionTable.
function ConsolidatedRow({ s, selected, favorites, onToggleSelect, onToggleFavorite, onRowClick, activeRowId }) {
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
          onClick={(e) => { e.stopPropagation(); onToggleFavorite?.(s.session_id) }}
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
        <span className="session-id">{s.session_id}</span>
      </td>
      <td className="col-sets" title="unique chat sets after dedup">
        <span className="sets-value">{s.total_sets}</span>
      </td>
      <td className="col-chars">
        {(s.total_session_chars || 0).toLocaleString()}
      </td>
      <td className="col-source" title="original sessions merged into this chain">
        {s.source_count}
      </td>
      <td className="col-dropped" title="duplicate chunks dropped during consolidation">
        {s.dropped_chunks}
      </td>
      <td className="col-provider">{s.provider || '—'}</td>
    </tr>
  )
}

function ConsolidatedTable({ sessions, selected, favorites, loading, onToggleSelect, onToggleAll, onToggleFavorite, onRowClick, activeRowId }) {
  if (loading) {
    return (
      <div className="table-loading">
        <span className="spinner"></span>
        Loading consolidated sessions...
      </div>
    )
  }

  if (!sessions || sessions.length === 0) {
    return (
      <div className="table-empty">
        No consolidated sessions yet. Run the consolidator to build them into lineage/consolidated/.
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
          {sessions.length} consolidated · {selected.size > 0 && ` (${selected.size} selected)`}
        </label>
      </div>
      <div className="table-scroll">
        <table className="session-table">
          <thead>
            <tr>
              <th className="col-fav">★</th>
              <th className="col-check"></th>
              <th className="col-name">Consolidated Session</th>
              <th className="col-sets" title="unique chat sets">Sets</th>
              <th className="col-chars">Chars</th>
              <th className="col-source" title="original sessions merged">Source</th>
              <th className="col-dropped" title="duplicate chunks dropped">Dropped</th>
              <th className="col-provider">Provider</th>
            </tr>
          </thead>
          <tbody>
            {sessions.map((s) => (
              <ConsolidatedRow
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

export default ConsolidatedTable
