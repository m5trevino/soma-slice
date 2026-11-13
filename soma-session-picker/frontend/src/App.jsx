import { useState, useEffect, useCallback, useMemo, useRef } from 'react'
import './App.css'
import './components/FavoritesPanel.css'
import SessionTable from './components/SessionTable'
import ConsolidatedTable from './components/ConsolidatedTable'
import FilterBar from './components/FilterBar'
import StatusBar from './components/StatusBar'
import SessionMetaPanel from './components/SessionMetaPanel'

const API = '/api'

// Renders a metadata value that may be a plain string or a
// {label: count} histogram — avoids "Objects are not valid as a React child".
function topLabel(value) {
  if (!value) return ''
  if (typeof value === 'string') return value
  if (Array.isArray(value)) return String(value[0] ?? '')
  if (typeof value === 'object') {
    const top = Object.entries(value).sort((a, b) => (b[1] || 0) - (a[1] || 0))[0]
    return top ? top[0] : ''
  }
  return String(value)
}

function App() {
  const [sessions, setSessions] = useState([])
  const [groups, setGroups] = useState([])
  const [consolidated, setConsolidated] = useState([])
  const [consolidatedLoading, setConsolidatedLoading] = useState(false)
  // "consolidated" = deduped chains (primary); "flat"/"grouped" = old search.
  const [viewMode, setViewMode] = useState('consolidated')
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(false)
  const [selected, setSelected] = useState(new Set())
  const [exporting, setExporting] = useState(false)
  const [exportStatus, setExportStatus] = useState('')
  
  // Favorites — persistent across reloads
  const [favorites, setFavorites] = useState(() => {
    try {
      const stored = localStorage.getItem('soma_favorites')
      return stored ? new Set(JSON.parse(stored)) : new Set()
    } catch { return new Set() }
  })
  
  // Selected favorites for export (separate from the favorites collection)
  const [exportSelected, setExportSelected] = useState(new Set())
  
  const [allFavorites, setAllFavorites] = useState([])  // full session data for favs
  const [showFavoritesPanel, setShowFavoritesPanel] = useState(false)
  const [showStartPanel, setShowStartPanel] = useState(false)
  const [jsonPath, setJsonPath] = useState('')
  const [loadedSessions, setLoadedSessions] = useState([])
  const [loadError, setLoadError] = useState('')
  const [startMode, setStartMode] = useState('all')
  const [pickN, setPickN] = useState(1)
  const [selectFirstN, setSelectFirstN] = useState(0)

  // Current profile
  const [profile, setProfile] = useState(null)
  const [profileError, setProfileError] = useState('')
  const [profileLoading, setProfileLoading] = useState(false)

  // Selected session for detail panel
  const [selectedSessionId, setSelectedSessionId] = useState(null)
  const [sessionMeta, setSessionMeta] = useState(null)
  const [metaLoading, setMetaLoading] = useState(false)
  const [metaError, setMetaError] = useState('')
  const [panelOpen, setPanelOpen] = useState(true)

  // Filter state
  const [search, setSearch] = useState('')
  const [domain, setDomain] = useState('')
  const [provider, setProvider] = useState('')
  const [model, setModel] = useState('')
  const [project, setProject] = useState('')
  const [minScore, setMinScore] = useState(0)
  // Chat set count is the more informative axis for judging a session's
  // substance, so it leads by default.
  const [sort, setSort] = useState('sets')
  const [order, setOrder] = useState('desc')
  // The vault holds ~4,800 sessions. A 500-row ceiling silently cut the list
  // off partway down (at 53 chat sets in score order), so the default fetches
  // everything and the Limit control is an explicit narrowing tool.
  const [limit, setLimit] = useState(5000)
  const [minSets, setMinSets] = useState(0)

  // Filter options
  const [providers, setProviders] = useState([])
  const [domains, setDomains] = useState([])
  const [models, setModels] = useState([])
  const [projects, setProjects] = useState([])
  const [scoreRange, setScoreRange] = useState({ min: 0, max: 0 })
  const [filterOptionsLoaded, setFilterOptionsLoaded] = useState(false)

  // Persist favorites
  useEffect(() => {
    localStorage.setItem('soma_favorites', JSON.stringify([...favorites]))
  }, [favorites])

  // Fetch filter options (DB only — fast)
  useEffect(() => {
    fetch(`${API}/sessions/counts`)
      .then((r) => r.json())
      .then((d) => {
        setProviders(d.providers || [])
        setDomains(d.domains || [])
        setModels(d.models || [])
        setScoreRange(d.score_range || { min: 0, max: 0 })
        setFilterOptionsLoaded(true)
      })
      .catch(() => setFilterOptionsLoaded(true))

    // Projects are ranked by session count; only list ones with real volume
    // so the dropdown stays navigable.
    fetch(`${API}/projects?min_sessions=2&limit=300`)
      .then((r) => r.json())
      .then((d) => setProjects(d.projects || []))
      .catch(() => setProjects([]))
  }, [])

  // Fetch current TurnKey profile
  useEffect(() => {
    setProfileLoading(true)
    fetch(`${API}/profile`)
      .then((r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`)
        return r.json()
      })
      .then((d) => {
        setProfile(d)
        setProfileError('')
      })
      .catch((err) => {
        setProfile(null)
        setProfileError(err.message || 'Profile not available')
      })
      .finally(() => setProfileLoading(false))
  }, [])

  const fetchSessions = useCallback(async () => {
    setLoading(true)
    try {
      const params = new URLSearchParams({
        search: search || '',
        domain: domain || '',
        provider: provider || '',
        model: model || '',
        project: project || '',
        min_score: String(minScore),
        sort: sort,
        order: order,
        limit: '5000',
        min_sets: String(minSets),
      })
      // Fetch both shapes in parallel: the flat list for the table, and the
      // grouped tree so repeats of one project collapse under a single node.
      const [res, gres] = await Promise.all([
        fetch(`${API}/sessions?${params}`),
        fetch(`${API}/sessions/grouped?${params}`),
      ])
      const data = await res.json()
      const gdata = await gres.json().catch(() => ({ groups: [] }))
      setSessions(data.sessions || [])
      setTotal(data.total || 0)
      setGroups(gdata.groups || [])
    } catch (err) {
      console.error('Failed to fetch sessions:', err)
    } finally {
      setLoading(false)
    }
  }, [search, domain, provider, model, project, minScore, sort, order, limit, minSets])

  useEffect(() => {
    fetchSessions()
  }, [fetchSessions])

  // Fetch consolidated sessions (chains) — the primary view.
  useEffect(() => {
    setConsolidatedLoading(true)
    fetch(`${API}/consolidated`)
      .then((r) => r.json())
      .then((d) => setConsolidated(d.sessions || []))
      .catch(() => setConsolidated([]))
      .finally(() => setConsolidatedLoading(false))
  }, [])

  // Fetch metadata when a session is selected
  const fetchSessionMeta = useCallback(async (sessionId) => {
    if (!sessionId) {
      setSessionMeta(null)
      setMetaError('')
      return
    }
    setMetaLoading(true)
    setMetaError('')
    try {
      const res = await fetch(`${API}/session/${encodeURIComponent(sessionId)}/metadata`)
      if (!res.ok) {
        const errData = await res.json().catch(() => ({}))
        throw new Error(errData.error || `HTTP ${res.status}`)
      }
      const data = await res.json()
      setSessionMeta(data)
    } catch (err) {
      setMetaError(err.message)
      setSessionMeta(null)
    } finally {
      setMetaLoading(false)
    }
  }, [])

  // Selection management
  const toggleSelect = useCallback((sessionId) => {
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(sessionId)) next.delete(sessionId)
      else next.add(sessionId)
      return next
    })
  }, [])

  const toggleAll = useCallback(() => {
    if (selected.size === sessions.length) {
      setSelected(new Set())
    } else {
      setSelected(new Set(sessions.map((s) => s.session_id)))
    }
  }, [sessions])

  // Favorites — add selected to favorites
  const addSelectedToFavorites = useCallback(() => {
    if (selected.size === 0) return
    setFavorites((prev) => {
      const next = new Set(prev)
      selected.forEach(id => next.add(id))
      return next
    })
    setExportStatus(`Added ${selected.size} to favorites (${favorites.size + selected.size} total)`)
  }, [selected, favorites.size])

  // Favorites — remove from favorites
  const removeFavorite = useCallback((sessionId) => {
    setFavorites((prev) => {
      const next = new Set(prev)
      next.delete(sessionId)
      return next
    })
    setExportSelected((prev) => {
      const next = new Set(prev)
      next.delete(sessionId)
      return next
    })
  }, [])

  // Favorites — clear every favorite, and the export selection with it.
  // Without this the list is permanent: favorites live in localStorage and
  // the only per-row control was a single ✕.
  const clearFavorites = useCallback(() => {
    const n = favorites.size
    if (n === 0) return
    if (!window.confirm(`Clear all ${n} favorites? This cannot be undone.`)) return
    setFavorites(new Set())
    setExportSelected(new Set())
    setExportStatus(`Cleared ${n} favorites`)
  }, [favorites.size])

  // Favorites — remove just the ones checked for export
  const removeSelectedFavorites = useCallback(() => {
    const ids = new Set(exportSelected)
    if (ids.size === 0) return
    setFavorites((prev) => {
      const next = new Set(prev)
      ids.forEach((id) => next.delete(id))
      return next
    })
    setExportSelected(new Set())
    setExportStatus(`Removed ${ids.size} favorites`)
  }, [exportSelected])

  // Toggle favorite star on a session row
  const toggleFavorite = useCallback((sessionId) => {
    setFavorites((prev) => {
      const next = new Set(prev)
      if (next.has(sessionId)) next.delete(sessionId)
      else next.add(sessionId)
      return next
    })
  }, [])

  // Favorites — load full data for favorites
  const loadFavoritesData = useCallback(async () => {
    const favList = []
    const favIds = Array.from(favorites)
    if (favIds.length === 0) {
      setAllFavorites([])
      return
    }
    // Fetch metadata for each favorite (lazy-load from disk)
    const promises = favIds.map(async (sid) => {
      try {
        const res = await fetch(`${API}/session/${encodeURIComponent(sid)}/metadata`)
        if (res.ok) {
          const data = await res.json()
          favList.push({ ...data, _session_id: sid })
        }
      } catch (e) {
        console.error('Failed to load favorite:', sid, e)
      }
    })
    await Promise.all(promises)
    setAllFavorites(favList)
  }, [favorites])

  // Total chat sets across every favorite, so the panel header shows how much
  // conversation the current selection represents.
  const favTotalSets = useMemo(
    () => allFavorites.reduce((sum, f) => sum + (f.total_sets || 0), 0),
    [allFavorites]
  )

  // Load sessions from a JSON file
  const loadSessions = useCallback(async () => {
    if (!jsonPath.trim()) {
      setLoadError('Enter a JSON file path first')
      setLoadedSessions([])
      return
    }
    setLoadError('')
    try {
      const res = await fetch(`${API}/sessions/load?path=${encodeURIComponent(jsonPath.trim())}`)
      const data = await res.json()
      if (!res.ok || data.error) {
        setLoadError(data.error || `HTTP ${res.status}`)
        setLoadedSessions([])
        return
      }
      setLoadedSessions(data.sessions || [])
    } catch (err) {
      setLoadError(err.message || 'Failed to load sessions')
      setLoadedSessions([])
    }
  }, [jsonPath])

  // Launch — POST the start config to /api/launch
  const launchHandler = useCallback(async () => {
    if (!jsonPath.trim()) {
      setExportStatus('Enter a JSON file path first')
      return
    }
    try {
      const res = await fetch(`${API}/launch`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          json_path: jsonPath.trim(),
          mode: startMode,
          count: startMode === 'one' ? 1 : startMode === 'few' ? pickN : undefined,
        }),
      })
      const data = await res.json()
      if (!res.ok) {
        throw new Error(data.error || `HTTP ${res.status}`)
      }
      setExportStatus('✓ Launch submitted')
      setShowStartPanel(false)
    } catch (err) {
      setExportStatus(err.message || 'Launch failed')
    }
  }, [jsonPath, startMode, pickN])

  // Load favorites data when panel opens
  useEffect(() => {
    if (showFavoritesPanel) {
      loadFavoritesData()
    } else {
      setAllFavorites([])
      setExportSelected(new Set())
    }
  }, [showFavoritesPanel, loadFavoritesData])

  // Favorites — toggle individual favorite for export
  const toggleExportFavorite = useCallback((sessionId) => {
    setExportSelected((prev) => {
      const next = new Set(prev)
      if (next.has(sessionId)) next.delete(sessionId)
      else next.add(sessionId)
      return next
    })
  }, [])

  // Favorites — select all / none for export
  const toggleAllExport = useCallback(() => {
    if (exportSelected.size === allFavorites.length) {
      setExportSelected(new Set())
    } else {
      setExportSelected(new Set(allFavorites.map((f) => f.session_id || f._session_id)))
    }
  }, [allFavorites, exportSelected])

  // Favorites — export as lean JSON
  const exportFavorites = useCallback(async () => {
    if (favorites.size === 0) {
      setExportStatus('No favorites to export')
      return
    }
    setExporting(true)
    setExportStatus('Loading favorites metadata...')

    // Favorites are stored as session_ids, but the metadata blob is keyed by
    // directory name (_session_id). Comparing the two directly silently
    // produced an empty export whenever anything was checked, so both forms
    // are collected and matched.
    const dirOf = (sid) => (sid || '').split('/').pop()
    const wantedIds = exportSelected.size > 0 ? Array.from(exportSelected) : Array.from(favorites)
    const wantedDirs = new Set(wantedIds.map(dirOf).filter(Boolean))

    const favoritesList = []
    for (const fav of allFavorites) {
      if (fav.error) continue
      const sid = fav.session_id || fav._session_id

      // Favorites hold full session_ids ("acct/session.dir") while the
      // metadata blob is keyed by directory name ("session.dir"). Matching
      // only on directory keeps the two aligned in both modes.
      const include =
        exportSelected.size === 0
          ? wantedDirs.has(dirOf(sid)) || wantedDirs.has(dirOf(fav._session_id))
          : exportSelected.has(sid) ||
            exportSelected.has(fav._session_id) ||
            wantedDirs.has(dirOf(sid)) ||
            wantedDirs.has(dirOf(fav._session_id))

      if (!include) continue

      favoritesList.push({
        session_id: sid,
        provider: fav.provider || '',
        total_sets: fav.total_sets || 0,
        total_session_chars: fav.total_session_chars || 0,
        file_count: fav.file_count || 0,
        anchors: fav.anchors || [],
        tool_calls: fav.tool_calls || [],
        metadata: fav.metadata || {},
        json_file_list: fav.json_file_list || [],
      })
    }

    // Queue file carries only the exported session IDs.
    //
    // The previous form, `Array.from(exportSelected) || Array.from(favorites)`,
    // never fell back: an empty array is truthy in JavaScript, so exporting
    // with nothing checked wrote an empty queue.
    const selectedIds = wantedIds
    const queueData = {
      selected: selectedIds,
      count: selectedIds.length,
      timestamp: Date.now(),
    }
    try {
      await fetch(`${API}/select`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(queueData),
      })
    } catch (e) {
      console.error('Failed to update queue:', e)
    }

    // Download metadata JSON. The filename carries the time as well as the
    // date so a second export on the same day does not overwrite the first.
    const metadataBlob = new Blob(
      [JSON.stringify(favoritesList, null, 2)],
      { type: 'application/json' }
    )
    const metaUrl = URL.createObjectURL(metadataBlob)
    const stamp = new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-')
    const a = document.createElement('a')
    a.href = metaUrl
    a.download = `soma_favorites_${stamp}.json`
    a.click()
    URL.revokeObjectURL(metaUrl)
    setExportStatus(
      `✓ Exported ${favoritesList.length} of ${favorites.size} favorites`
    )
    setExporting(false)
  }, [favorites, allFavorites, exportSelected])

  // Consolidated export — write selected consolidated session paths to the queue.
  const exportConsolidated = useCallback(async () => {
    const paths = consolidated
      .filter((s) => selected.has(s.session_id))
      .map((s) => s.path)
    if (paths.length === 0) {
      setExportStatus('No consolidated sessions selected')
      return
    }
    try {
      await fetch(`${API}/select`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session_ids: paths }),
      })
      setExportStatus(`✓ Exported ${paths.length} consolidated session(s) to queue`)
    } catch (err) {
      setExportStatus(err.message || 'Export failed')
    }
  }, [consolidated, selected])

  // When a row is clicked, show metadata
  const handleRowClick = useCallback((sessionId) => {
    setSelectedSessionId((prev) => {
      if (prev === sessionId) {
        setPanelOpen(false)
        return null
      }
      setPanelOpen(true)
      fetchSessionMeta(sessionId)
      return sessionId
    })
  }, [fetchSessionMeta])

  const displayedSessions = useMemo(() => sessions, [sessions])

  const filterBarProps = {
    search, setSearch,
    domain, setDomain,
    provider, setProvider,
    model, setModel,
    project, setProject,
    minScore, setMinScore,
    sort, setSort,
    order, setOrder,
    limit, setLimit,
    minSets, setMinSets,
    providers, domains, models, projects,
    scoreRange, filterOptionsLoaded,
    onRefresh: fetchSessions,
  }

  return (
    <div className="app">
      <header className="header">
        <h1>Soma Session Picker</h1>
        <div className="header-actions">
          <button
            className="btn-start"
            onClick={() => setShowStartPanel(!showStartPanel)}
          >
            ▶ Start
          </button>
          <button
            className="btn-favorites"
            onClick={() => {
              setShowFavoritesPanel(!showFavoritesPanel)
              if (showFavoritesPanel) {
                setPanelOpen(true)
              }
            }}
            style={{ background: showFavoritesPanel ? '#f59e0b' : '#374151' }}
          >
            {showFavoritesPanel ? '✕ Close Favorites' : `★ Favorites (${favorites.size})`}
          </button>
          {selected.size > 0 && (
            <button
              className="btn-add-fav"
              onClick={addSelectedToFavorites}
              title={`Add ${selected.size} selected sessions to favorites`}
            >
              + Add {selected.size} to Favorites
            </button>
          )}
          {viewMode === 'consolidated' && selected.size > 0 && (
            <button
              className="btn-export-fav"
              onClick={exportConsolidated}
              title={`Write ${selected.size} consolidated session(s) to the Soma queue`}
            >
              Export {selected.size} Consolidated
            </button>
          )}
          {showFavoritesPanel && (
            <button
              className="btn-export-fav"
              onClick={exportFavorites}
              disabled={exporting || favorites.size === 0}
            >
              {exporting
                ? 'Exporting...'
                : exportSelected.size > 0
                  ? `Export ${exportSelected.size} Selected Favs`
                  : `Export ${favorites.size} Favorites`}
            </button>
          )}
          {favorites.size > 0 && (
            <button
              className="btn-clear-fav"
              onClick={clearFavorites}
              title="Clear all favorites"
            >
              Clear Favs
            </button>
          )}
        </div>
      </header>

      {/* Start Panel — fixed overlay */}
      {showStartPanel && (
        <div className="start-panel-overlay">
          <div className="start-panel">
            <div className="start-panel-header">
              <h2>Start Run</h2>
              <button className="start-panel-close" onClick={() => setShowStartPanel(false)} title="Close panel">
                ✕
              </button>
            </div>
            <div className="start-panel-body">
              {/* Current Profile Section */}
              <div className="start-field profile-section">
                <label>Current Profile</label>
                {profileLoading ? (
                  <div className="profile-loading">Loading profile…</div>
                ) : profileError ? (
                  <div className="profile-error" title={profileError}>
                    ⚠ {profileError}
                  </div>
                ) : profile ? (
                  <>
                    <div className="profile-name">{profile.name || 'Unnamed'}</div>
                    <div className="profile-models">
                      {profile.models.map((m, i) => (
                        <div key={i} className="profile-model-row">
                          <span className="profile-model-id">{m.modelId}</span>
                          <span className="profile-model-provider">{m.provider}</span>
                        </div>
                      ))}
                    </div>
                  </>
                ) : null}
              </div>
              {/* End Profile Section */}

              <div className="start-field">
                <label htmlFor="json-path">JSON file path</label>
                <input
                  id="json-path"
                  type="text"
                  placeholder="/path/to/soma_favorites.json"
                  value={jsonPath}
                  onChange={(e) => setJsonPath(e.target.value)}
                />
                <div style={{ marginTop: '8px' }}>
                  <button
                    className="btn-load-sessions"
                    onClick={loadSessions}
                    disabled={!jsonPath.trim()}
                  >
                    Load Sessions
                  </button>
                </div>
              </div>
              {loadError && (
                <div className="start-field">
                  <div style={{ color: '#f85149', fontSize: '13px', padding: '8px 12px', background: '#2d1616', borderRadius: '6px', border: '1px solid #f8514933' }}>
                    {loadError}
                  </div>
                </div>
              )}
              {loadedSessions.length > 0 && (
                <div className="start-field start-sessions-list">
                  <div className="start-sessions-list-header">
                    <label>Sessions ({loadedSessions.length})</label>
                    <span className="start-selected-count">{selected.size} selected</span>
                  </div>
                  <div className="start-sessions-list-controls">
                    <button
                      className="btn-select-all"
                      onClick={() => setSelected(new Set(loadedSessions.map((s) => s.session_id)))}
                    >
                      Select All
                    </button>
                    <button
                      className="btn-select-none"
                      onClick={() => setSelected(new Set())}
                    >
                      Select None
                    </button>
                    <div className="select-first-n">
                      <input
                        type="number"
                        min={0}
                        max={loadedSessions.length}
                        value={selectFirstN}
                        onChange={(e) => setSelectFirstN(Math.min(loadedSessions.length, Math.max(0, Number(e.target.value) || 0)))}
                        placeholder="First N"
                      />
                      <button
                        className="btn-select-first-n"
                        onClick={() => {
                          const n = Math.min(loadedSessions.length, Math.max(0, selectFirstN))
                          const firstN = new Set(loadedSessions.slice(0, n).map((s) => s.session_id))
                          setSelected(firstN)
                        }}
                      >
                        Check first N
                      </button>
                    </div>
                  </div>
                  <div className="start-sessions-list-body">
                    {loadedSessions.map((s) => (
                      <label key={s.session_id} className="start-session-row">
                        <input
                          type="checkbox"
                          checked={selected.has(s.session_id)}
                          onChange={() => toggleSelect(s.session_id)}
                        />
                        <span className="start-session-id">{s.session_id}</span>
                      </label>
                    ))}
                  </div>
                </div>
              )}
              <div className="start-field">
                <label>Run mode</label>
                <div className="radio-group">
                  <label className="radio-label">
                    <input
                      type="radio"
                      name="startMode"
                      value="all"
                      checked={startMode === 'all'}
                      onChange={(e) => setStartMode(e.target.value)}
                    />
                    All sessions
                  </label>
                  <label className="radio-label">
                    <input
                      type="radio"
                      name="startMode"
                      value="one"
                      checked={startMode === 'one'}
                      onChange={(e) => setStartMode(e.target.value)}
                    />
                    Just one
                  </label>
                  <label className="radio-label">
                    <input
                      type="radio"
                      name="startMode"
                      value="few"
                      checked={startMode === 'few'}
                      onChange={(e) => setStartMode(e.target.value)}
                    />
                    Pick N (1-3)
                  </label>
                </div>
              </div>
              <div className="start-field">
                <label htmlFor="pick-n" className={startMode === 'few' ? '' : 'disabled-label'}>
                  Number of sessions
                </label>
                <input
                  id="pick-n"
                  type="number"
                  min={1}
                  max={3}
                  value={pickN}
                  onChange={(e) => setPickN(Math.min(3, Math.max(1, Number(e.target.value) || 1)))}
                  disabled={startMode !== 'few'}
                />
              </div>
              <div className="start-field">
                <button
                  className="btn-launch"
                  onClick={() => {
                    if (!jsonPath.trim()) {
                      setExportStatus('Enter a JSON file path first')
                      return
                    }
                    launchHandler()
                  }}
                  disabled={!jsonPath.trim()}
                >
                  ▶ Launch
                </button>
              </div>
            </div>
          </div>
        </div>
      )}

      <FilterBar {...filterBarProps} />

      <div className="view-bar">
        <div className="rocker" role="tablist">
          <div className="rocker-slot">
            <div className={`rocker-knob${viewMode === 'flat' ? ' right' : ''}`}></div>
            <button
              className={viewMode === 'consolidated' ? 'on' : ''}
              onClick={() => setViewMode('consolidated')}
            >
              Consolidated
            </button>
            <button
              className={viewMode === 'grouped' ? 'on' : ''}
              onClick={() => setViewMode('grouped')}
            >
              By project
            </button>
            <button
              className={viewMode === 'flat' ? 'on' : ''}
              onClick={() => setViewMode('flat')}
            >
              All sessions
            </button>
          </div>
        </div>
        <span className="view-hint">
          {viewMode === 'consolidated'
            ? `${consolidated.length} consolidated sessions`
            : viewMode === 'grouped'
              ? `${groups.length} projects · ${groups.reduce((n, g) => n + g.session_count, 0)} sessions`
              : `${sessions.length} of ${total} sessions`}
        </span>
      </div>

      <div className="main-layout">
        <main className="main">
          {viewMode === 'consolidated' ? (
            <ConsolidatedTable
              sessions={consolidated}
              selected={selected}
              favorites={favorites}
              loading={consolidatedLoading}
              onToggleSelect={toggleSelect}
              onToggleAll={() => setSelected(selected.size === consolidated.length ? new Set() : new Set(consolidated.map(s => s.session_id)))}
              onToggleFavorite={toggleFavorite}
              onRowClick={handleRowClick}
              activeRowId={selectedSessionId}
            />
          ) : (
            <SessionTable
              sessions={displayedSessions}
              groups={groups}
              grouped={viewMode === 'grouped'}
              selected={selected}
              favorites={favorites}
              loading={loading}
              onToggleSelect={toggleSelect}
              onToggleAll={() => setSelected(selected.size === sessions.length ? new Set() : new Set(sessions.map(s => s.session_id)))}
              onToggleFavorite={toggleFavorite}
              onRowClick={handleRowClick}
              activeRowId={selectedSessionId}
            />
          )}
        </main>

        {/* Favorites panel — left sidebar */}
        {showFavoritesPanel && (
          <aside className="favorites-panel">
            <div className="favorites-panel-header">
              <span>Favorites ({allFavorites.length})</span>
              {favTotalSets > 0 && (
                <span
                  className="fav-header-sets"
                  title="Total chat sets across all favorites (one user message + one agent reply each)"
                >
                  {favTotalSets.toLocaleString()} sets
                </span>
              )}
              <button className="favorites-panel-close" onClick={() => setShowFavoritesPanel(false)} title="Close panel">✕</button>
            </div>
            {allFavorites.length > 0 && (
              <div className="favorites-panel-controls">
                <button
                  className="btn-select-all"
                  onClick={toggleAllExport}
                >
                  {exportSelected.size === allFavorites.length ? 'Select None' : 'Select All'}
                </button>
                {exportSelected.size > 0 && (
                  <>
                    <span className="fav-selected-count">
                      {exportSelected.size} selected for export
                    </span>
                    <button
                      className="btn-remove-selected"
                      onClick={removeSelectedFavorites}
                      title={`Remove these ${exportSelected.size} from favorites`}
                    >
                      Remove {exportSelected.size}
                    </button>
                  </>
                )}
                <button
                  className="btn-clear-favorites"
                  onClick={clearFavorites}
                  title="Clear every favorite"
                >
                  Clear All
                </button>
              </div>
            )}
            <div className="favorites-panel-body">
              {allFavorites.length === 0 ? (
                <p className="fav-empty">No favorites yet. Select sessions and click "Add to Favorites".</p>
              ) : (
                <div className="favorites-list">
                  {allFavorites.map((fav, i) => {
                    const sid = fav.session_id || fav._session_id
                    const checked = exportSelected.has(sid)
                    return (
                      <div key={i} className={`fav-item ${checked ? 'fav-item-checked' : ''}`}>
                        <label className="fav-item-checkbox">
                          <input
                            type="checkbox"
                            checked={checked}
                            onChange={() => toggleExportFavorite(sid)}
                          />
                          <span className="fav-item-id">{sid?.split('/').pop() || sid}</span>
                        </label>
                        <div className="fav-item-meta">
                          {/* Chat sets: one user message + one agent reply. */}
                          <span
                            className={`fav-sets${(fav.total_sets || 0) === 0 ? ' fav-sets-zero' : ''}`}
                            title={`${fav.total_sets || 0} chat sets — ${fav.file_count || 0} files on disk`}
                          >
                            {fav.total_sets || 0}
                          </span>
                          {fav.metadata?.domain && <span className="fav-badge">{topLabel(fav.metadata.domain)}</span>}
                          {fav.metadata?.sentiment && <span className="fav-badge fav-sentiment">{topLabel(fav.metadata.sentiment)}</span>}
                        </div>
                        <button
                          className="fav-remove-btn"
                          onClick={() => {
                            if (sid) {
                              removeFavorite(sid)
                              setExportSelected(prev => { const n = new Set(prev); n.delete(sid); return n })
                            }
                          }}
                          title="Remove from favorites"
                        >
                          ✕
                        </button>
                      </div>
                    )
                  })}
                </div>
              )}
            </div>
          </aside>
        )}

        {/* Metadata detail panel — right sidebar */}
        {panelOpen && (
          <aside className="meta-panel">
            <div className="meta-panel-header">
              <span>Session Details</span>
              <button className="meta-panel-close" onClick={() => setPanelOpen(false)} title="Close panel">✕</button>
            </div>
            <SessionMetaPanel
              sessionId={selectedSessionId}
              metadata={sessionMeta}
              loading={metaLoading}
              error={metaError}
            />
          </aside>
        )}
      </div>

      <StatusBar
        total={total}
        filtered={displayedSessions.length}
        selected={selected.size}
        favoritesCount={favorites.size}
        loading={loading}
        exportStatus={exportStatus}
      />
    </div>
  )
}

export default App
