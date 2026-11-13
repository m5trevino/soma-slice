import './SessionMetaPanel.css'

function SessionMetaPanel({ sessionId, metadata, loading, error }) {
  if (!sessionId) {
    return (
      <div className="meta-panel-empty">
        <p>Click a session row to view its extracted metadata.</p>
        <p className="meta-hint">Like Search Monkey III — domain, sentiment, topics, project context, anchors.</p>
      </div>
    )
  }

  if (loading) {
    return (
      <div className="meta-panel-loading">
        <span className="spinner"></span>
        Loading metadata...
      </div>
    )
  }

  if (error) {
    return (
      <div className="meta-panel-error">
        <p className="error-text">{error}</p>
        <p className="error-hint">This session may not have extraction data yet.</p>
      </div>
    )
  }

  if (!metadata) {
    return (
      <div className="meta-panel-empty">
        <p>No metadata found for this session.</p>
      </div>
    )
  }

  const md = metadata.metadata || {}
  const topSentiment = Object.entries(md.sentiment || {}).slice(0, 3)
  const topMaturity = Object.entries(md.idea_maturity || {}).slice(0, 3)
  const topContentType = Object.entries(md.content_type || {}).slice(0, 3)
  const topEmotional = Object.entries(md.emotional_tone || {}).slice(0, 3)
  const topSubDomain = Object.entries(md.sub_domain || {}).slice(0, 3)
  const topProjectContext = Object.entries(md.project_context || {}).slice(0, 10)
  const topTopics = Object.entries(md.topics || {}).slice(0, 15)

  return (
    <div className="meta-content">
      {/* Basic info */}
      <section className="meta-section">
        <h3>Overview</h3>
        <div className="meta-row">
          <span className="meta-label">Session</span>
          <span className="meta-value session-id" title={metadata.session_id || sessionId}>
            {(metadata.session_id || sessionId).split('/').pop()}
          </span>
        </div>
        <div className="meta-row">
          <span className="meta-label">Provider</span>
          <span className="meta-value">{metadata.provider || '—'}</span>
        </div>
        <div className="meta-row">
          <span className="meta-label">Sets</span>
          <span className="meta-value">{metadata.total_sets || '—'}</span>
        </div>
        <div className="meta-row">
          <span className="meta-label">Chars</span>
          <span className="meta-value">{(metadata.total_session_chars || 0).toLocaleString()}</span>
        </div>
        <div className="meta-row">
          <span className="meta-label">Files</span>
          <span className="meta-value">{metadata.file_count || '—'}</span>
        </div>
      </section>

      {/* Domain & Classification */}
      <section className="meta-section">
        <h3>Classification</h3>
        <div className="meta-row">
          <span className="meta-label">Domain</span>
          <span className="meta-value domain-badge">{md.domain || '—'}</span>
        </div>
        <div className="meta-row">
          <span className="meta-label">Sub-domain</span>
          <span className="meta-value subdomain-value">
            {topSubDomain.length > 0 ? (
              <span className="value-counts">
                {topSubDomain.map(([t, c]) => (
                  <span key={t} className="value-item" title={`${t}: ${c}`}>
                    {t} <span className="count">({c})</span>
                  </span>
                ))}
              </span>
            ) : '—'}
          </span>
        </div>
        <div className="meta-row">
          <span className="meta-label">Content Type</span>
          <span className="meta-value">
            {topContentType.length > 0 ? (
              <span className="value-counts">
                {topContentType.map(([t, c]) => (
                  <span key={t} className="value-item" title={`${t}: ${c}`}>
                    {t} <span className="count">({c})</span>
                  </span>
                ))}
              </span>
            ) : '—'}
          </span>
        </div>
      </section>

      {/* Sentiment & Tone */}
      <section className="meta-section">
        <h3>Sentiment & Tone</h3>
        <div className="meta-row">
          <span className="meta-label">Sentiment</span>
          <span className="meta-value">
            {topSentiment.length > 0 ? (
              <span className="value-counts">
                {topSentiment.map(([t, c]) => (
                  <span key={t} className="value-item" title={`${t}: ${c}`}>
                    {t} <span className="count">({c})</span>
                  </span>
                ))}
              </span>
            ) : '—'}
          </span>
        </div>
        <div className="meta-row">
          <span className="meta-label">Idea Maturity</span>
          <span className="meta-value">
            {topMaturity.length > 0 ? (
              <span className="value-counts">
                {topMaturity.map(([t, c]) => (
                  <span key={t} className="value-item" title={`${t}: ${c}`}>
                    {t} <span className="count">({c})</span>
                  </span>
                ))}
              </span>
            ) : '—'}
          </span>
        </div>
        <div className="meta-row">
          <span className="meta-label">Emotional Tone</span>
          <span className="meta-value">
            {topEmotional.length > 0 ? (
              <span className="value-counts">
                {topEmotional.map(([t, c]) => (
                  <span key={t} className="value-item" title={`${t}: ${c}`}>
                    {t} <span className="count">({c})</span>
                  </span>
                ))}
              </span>
            ) : '—'}
          </span>
        </div>
      </section>

      {/* Project Context */}
      <section className="meta-section">
        <h3>Project Context</h3>
        {topProjectContext.length > 0 ? (
          <div className="context-tags">
            {topProjectContext.map(([k, v]) => (
              <span key={k} className="context-tag" title={`${k}: ${v}`}>
                {k} <span className="tag-count">{v}</span>
              </span>
            ))}
          </div>
        ) : (
          <span className="meta-value">No project context extracted</span>
        )}
      </section>

      {/* Topics */}
      <section className="meta-section">
        <h3>Topics ({Object.keys(md.topics || {}).length})</h3>
        {topTopics.length > 0 ? (
          <div className="context-tags">
            {topTopics.map(([k, v]) => (
              <span key={k} className="context-tag" title={`${k}: ${v}`}>
                {k} <span className="tag-count">{v}</span>
              </span>
            ))}
          </div>
        ) : (
          <span className="meta-value">No topics extracted</span>
        )}
      </section>

      {/* Anchors */}
      <section className="meta-section">
        <h3>Anchors ({(metadata.anchors || []).length})</h3>
        {(metadata.anchors || []).length > 0 ? (
          <div className="anchors-list">
            {(metadata.anchors || []).slice(0, 20).map((a, i) => (
              <div key={i} className="anchor-item">
                <div className="anchor-set">Set #{a.set_number || '—'}</div>
                <div className="anchor-snippet" title={a.snippet}>
                  {a.snippet?.substring(0, 120)}{a.snippet?.length > 120 ? '...' : ''}
                </div>
              </div>
            ))}
            {(metadata.anchors || []).length > 20 && (
              <div className="anchor-more">... and {(metadata.anchors || []).length - 20} more</div>
            )}
          </div>
        ) : (
          <span className="meta-value">No anchors extracted</span>
        )}
      </section>

      {/* Tool Calls */}
      <section className="meta-section">
        <h3>Tool Calls / Questions ({(metadata.tool_calls || []).length})</h3>
        {(metadata.tool_calls || []).length > 0 ? (
          <div className="tool-calls-list">
            {metadata.tool_calls.map((tc, i) => (
              <div key={i} className="tool-call-item">
                <div className="tc-question">{tc.question}</div>
                <div className="tc-meta">
                  {tc.provider && <span className="tc-provider">{tc.provider}</span>}
                  {tc.context_set && <span className="tc-set">Set #{tc.context_set}</span>}
                </div>
              </div>
            ))}
          </div>
        ) : (
          <span className="meta-value">No tool calls</span>
        )}
      </section>
    </div>
  )
}

export default SessionMetaPanel
