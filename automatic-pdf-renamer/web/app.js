const searchFilter = document.getElementById('search-filter');
const body = document.getElementById('documents-body');
const summaryCards = document.getElementById('summary-cards');
const refreshButton = document.getElementById('refresh-btn');
const rebuildButton = document.getElementById('rebuild-btn');
const rebuildStatus = document.getElementById('rebuild-status');
const listStatus = document.getElementById('list-status');
const loadMoreButton = document.getElementById('load-more-btn');
const loadSentinel = document.getElementById('load-sentinel');
const columnFilters = [...document.querySelectorAll('[data-filter]')];
const sortButtons = [...document.querySelectorAll('[data-sort]')];
let sortBy = 'updated_at';
let sortOrder = 'desc';
let page = 1;
let total = 0;
let hasMore = true;
let loading = false;
let reloadAfterRequest = false;
let loadedItems = [];

const formatUpdated = (value) => {
  if (!value) return '—';
  try {
    return new Date(value).toLocaleString('fr-FR');
  } catch {
    return value;
  }
};

const escapeHtml = (value) => String(value ?? '').replace(/[&<>"']/g, (character) => ({
  '&': '&amp;',
  '<': '&lt;',
  '>': '&gt;',
  '"': '&quot;',
  "'": '&#39;',
}[character]));

const getListParams = () => {
  const params = new URLSearchParams();
  columnFilters.forEach((filter) => {
    const value = filter.value.trim();
    if (value) params.set(filter.dataset.filter, value);
  });
  const search = searchFilter.value.trim();
  if (search) params.set('q', search);
  params.set('sort_by', sortBy);
  params.set('sort_order', sortOrder);
  return params;
};

const badgeClass = (status) => {
  const normalized = (status || 'unknown').trim().toLowerCase();
  if (normalized === 'processed' || normalized === 'validated') return normalized;
  if (normalized === 'not processed') return 'not processed';
  return 'unknown';
};

const renderSummary = (totals) => {
  summaryCards.innerHTML = `
    <article class="summary-card">
      <span class="label">Total</span>
      <span class="value">${totals.total}</span>
    </article>
    <article class="summary-card">
      <span class="label">Processed</span>
      <span class="value">${totals.processed}</span>
    </article>
    <article class="summary-card">
      <span class="label">Validated</span>
      <span class="value">${totals.validated}</span>
    </article>
    <article class="summary-card">
      <span class="label">Pending</span>
      <span class="value">${totals.pending}</span>
    </article>
  `;
};

const renderRows = (items, append) => {
  if (!items.length && !append) {
    body.innerHTML = '<tr><td colspan="8" class="empty-state">No documents found.</td></tr>';
    return;
  }

  const listParams = getListParams();
  const rows = items
    .map((item) => {
      const detailParams = new URLSearchParams(listParams);
      detailParams.set('id', item.id);
      return `
      <tr>
        <td><span class="badge ${escapeHtml(badgeClass(item.status))}">${escapeHtml(item.status || 'unknown')}</span></td>
        <td>
          <a class="document-link" href="/document.html?${detailParams.toString()}">${escapeHtml(item.current_key?.split('/').pop() || 'Untitled document')}</a>
          <div class="key-text">${escapeHtml(item.current_key || '—')}</div>
        </td>
        <td>${escapeHtml(item.template_name || '—')}</td>
        <td>${escapeHtml(item.emitting_company || '—')}</td>
        <td>${escapeHtml(item.document_type || '—')}</td>
        <td>${escapeHtml(item.period || '—')}</td>
        <td>${item.confidence == null ? '—' : `${(Number(item.confidence) * 100).toFixed(1)}%`}</td>
        <td>${escapeHtml(formatUpdated(item.updated_at))}</td>
      </tr>
    `;
    })
    .join('');
  if (append) {
    body.insertAdjacentHTML('beforeend', rows);
  } else {
    body.innerHTML = rows;
  }
};

const loadDocuments = async ({ append = false } = {}) => {
  if (loading) {
    if (!append) reloadAfterRequest = true;
    return;
  }
  if (append && !hasMore) return;
  loading = true;
  loadMoreButton.disabled = true;
  if (!append) {
    page = 1;
    total = 0;
    hasMore = true;
    loadedItems = [];
    listStatus.textContent = 'Loading documents...';
  } else {
    listStatus.textContent = `Loading more documents (${loadedItems.length} of ${total})...`;
  }

  const params = getListParams();
  params.set('page', String(page));

  try {
    const response = await fetch(`/api/files?${params.toString()}`);
    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`);
    }

    const items = await response.json();
    total = Number(response.headers.get('X-Total-Count')) || 0;
    const counts = JSON.parse(response.headers.get('X-Status-Counts') || '{}');
    renderSummary({
      total: counts.total ?? total,
      processed: counts.processed ?? 0,
      validated: counts.validated ?? 0,
      pending: counts.pending ?? 0,
    });
    renderRows(items, append);
    loadedItems = append ? loadedItems.concat(items) : items;
    page += 1;
    hasMore = loadedItems.length < total;
    listStatus.textContent = total
      ? `Showing ${loadedItems.length} of ${total} matching documents.`
      : 'No documents match these filters.';
    loadMoreButton.hidden = !hasMore;
  } catch (error) {
    if (!append) {
      body.innerHTML = '<tr><td colspan="8" class="empty-state">Unable to load documents. Check the API server.</td></tr>';
      summaryCards.innerHTML = '<article class="summary-card"><span class="label">Error</span><span class="value">!</span></article>';
    }
    listStatus.textContent = append
      ? `Unable to load more documents. ${loadedItems.length} of ${total} loaded.`
      : 'Unable to load documents. Check the API server.';
    console.error(error);
  } finally {
    loading = false;
    loadMoreButton.disabled = false;
    if (reloadAfterRequest) {
      reloadAfterRequest = false;
      loadDocuments();
    }
  }
};

const updateSortIndicators = () => {
  document.querySelectorAll('[data-sort-column]').forEach((header) => {
    const active = header.dataset.sortColumn === sortBy;
    header.setAttribute('aria-sort', active ? (sortOrder === 'asc' ? 'ascending' : 'descending') : 'none');
    const indicator = header.querySelector('.sort-button span');
    indicator.textContent = active ? (sortOrder === 'asc' ? '↑' : '↓') : '';
  });
};

const rebuildDatabase = async () => {
  if (!window.confirm('Rebuild the database from sidecar files? Existing database records will be replaced.')) return;

  rebuildButton.disabled = true;
  rebuildStatus.textContent = 'Rebuilding database from sidecar files…';
  try {
    const response = await fetch('/api/consistency/rebuild', { method: 'POST' });
    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`);
    }

    const result = await response.json();
    rebuildStatus.textContent = `Database rebuilt from ${result.count} sidecar file${result.count === 1 ? '' : 's'}.`;
    await loadDocuments();
  } catch (error) {
    rebuildStatus.textContent = 'Unable to rebuild the database. Check the API server and storage backend.';
    console.error(error);
  } finally {
    rebuildButton.disabled = false;
  }
};

columnFilters.forEach((filter) => filter.addEventListener('input', loadDocuments));
searchFilter.addEventListener('input', loadDocuments);
refreshButton.addEventListener('click', loadDocuments);
rebuildButton.addEventListener('click', rebuildDatabase);
loadMoreButton.addEventListener('click', () => loadDocuments({ append: true }));
sortButtons.forEach((button) => {
  button.addEventListener('click', () => {
    if (sortBy === button.dataset.sort) {
      sortOrder = sortOrder === 'asc' ? 'desc' : 'asc';
    } else {
      sortBy = button.dataset.sort;
      sortOrder = 'asc';
    }
    updateSortIndicators();
    loadDocuments();
  });
});

updateSortIndicators();
loadDocuments();

if ('IntersectionObserver' in window) {
  const loadObserver = new IntersectionObserver((entries) => {
    if (entries.some((entry) => entry.isIntersecting)) {
      loadDocuments({ append: true });
    }
  }, { rootMargin: '300px' });
  loadObserver.observe(loadSentinel);
}
