import './FilterBar.css'

function FilterBar(props) {
  const {
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
    onRefresh,
  } = props

  const buildOption = (val) => (
    <option key={val} value={val}>{val}</option>
  )

  return (
    <div className="filter-bar">
      <div className="filter-row">
        {/* Project — primary axis for dev work */}
        {filterOptionsLoaded && projects.length > 0 && (
          <div className="filter-group filter-group-project">
            <label>Project</label>
            <select value={project} onChange={(e) => setProject(e.target.value)}>
              <option value="">All projects</option>
              {projects.map((p) => (
                <option key={p.project_key} value={p.project_key}>
                  {p.project_name} ({p.session_count})
                </option>
              ))}
            </select>
          </div>
        )}

        {/* Search */}
        <div className="filter-group filter-group-search">
          <label>Search</label>
          <input
            type="text"
            placeholder="Session name, domain, topic..."
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
        </div>

        {/* Domain */}
        {filterOptionsLoaded && domains.length > 0 && (
          <div className="filter-group">
            <label>Domain</label>
            <select value={domain} onChange={(e) => setDomain(e.target.value)}>
              <option value="">All</option>
              {domains.map(buildOption)}
            </select>
          </div>
        )}

        {/* Provider */}
        {filterOptionsLoaded && providers.length > 0 && (
          <div className="filter-group">
            <label>Provider</label>
            <select value={provider} onChange={(e) => setProvider(e.target.value)}>
              <option value="">All</option>
              {providers.map(buildOption)}
            </select>
          </div>
        )}

        {/* Model */}
        {filterOptionsLoaded && models.length > 0 && (
          <div className="filter-group">
            <label>Model</label>
            <select value={model} onChange={(e) => setModel(e.target.value)}>
              <option value="">All</option>
              {models.map((m) => (
                <option key={m} value={m} title={m}>{m}</option>
              ))}
            </select>
          </div>
        )}

        {/* Min Score */}
        <div className="filter-group">
          <label>Min Score</label>
          <select value={minScore} onChange={(e) => setMinScore(Number(e.target.value))}>
            <option value={0}>All</option>
            <option value={10000}>10k+</option>
            <option value={50000}>50k+</option>
            <option value={100000}>100k+</option>
            <option value={200000}>200k+</option>
            <option value={300000}>300k+</option>
            <option value={500000}>500k+</option>
          </select>
        </div>

        {/* Min Sets — logical exchanges (user message + reply), not files */}
        <div className="filter-group">
          <label>Min Sets</label>
          <select value={minSets} onChange={(e) => setMinSets(Number(e.target.value))}>
            <option value={0}>All</option>
            <option value={5}>5+</option>
            <option value={10}>10+</option>
            <option value={25}>25+</option>
            <option value={50}>50+</option>
            <option value={100}>100+</option>
            <option value={200}>200+</option>
          </select>
        </div>

        {/* Sort */}
        <div className="filter-group">
          <label>Sort</label>
          <select value={sort} onChange={(e) => setSort(e.target.value)}>
            <option value="sets">Sets</option>
            <option value="score">Score</option>
            <option value="date">Date</option>
            <option value="name">Name</option>
            <option value="provider">Provider</option>
            <option value="domain">Domain</option>
            <option value="functions">Functions</option>
          </select>
        </div>

        {/* Order */}
        <div className="filter-group">
          <label>Order</label>
          <select value={order} onChange={(e) => setOrder(e.target.value)}>
            <option value="desc">Desc</option>
            <option value="asc">Asc</option>
          </select>
        </div>

        {/* Limit */}
        <div className="filter-group">
          <label>Limit</label>
          <select value={limit} onChange={(e) => setLimit(Number(e.target.value))}>
            {[100, 200, 500, 1000, 2000, 4000, 5000].map((v) => (
              <option key={v} value={v}>{v}</option>
            ))}
          </select>
        </div>
        {/* Refresh */}
        <div className="filter-group filter-group-btn">
          <label>&nbsp;</label>
          <button className="btn-refresh" onClick={onRefresh}>
            Refresh
          </button>
        </div>
      </div>
    </div>
  )
}

export default FilterBar
