import './StatusBar.css'

function StatusBar(props) {
  const { total, filtered, selected, favoritesCount, showFavoritesOnly, loading, exportStatus } = props

  return (
    <footer className="status-bar">
      <div className="status-left">
        {loading ? (
          <span className="status-loading">Loading...</span>
        ) : (
          <>
            <span className="status-count">{filtered} of {total} sessions</span>
            {selected > 0 && (
              <span className="status-selected">{selected} selected</span>
            )}
            {favoritesCount > 0 && (
              <span className="status-favs">
                {showFavoritesOnly ? '★ ' : '☆ '}{favoritesCount} favorites
              </span>
            )}
          </>
        )}
      </div>
      <div className="status-right">
        {exportStatus && (
          <span className={`status-export ${exportStatus.startsWith('✓') ? 'ok' : 'err'}`}>
            {exportStatus}
          </span>
        )}
      </div>
    </footer>
  )
}

export default StatusBar
