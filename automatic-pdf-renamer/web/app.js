const searchFilter = document.getElementById('search-filter');
const body = document.getElementById('documents-body');
const summaryCards = document.getElementById('summary-cards');
const refreshButton = document.getElementById('refresh-btn');
const listStatus = document.getElementById('list-status');
const loadMoreButton = document.getElementById('load-more-btn');
const loadSentinel = document.getElementById('load-sentinel');
let columnFilters = [];
const tableHead = document.getElementById('documents-head');
const columnOptions = document.getElementById('column-options');
const selectedIds = new Set();
let allFilteredSelection = false;
let availableAdditionalFields = [];
const defaultColumns = ['status', 'current_key', 'emitting_company', 'document_type', 'period', 'updated_at'];
const columnDefinitions = {
  status: { label: 'Status', filter: 'status', sortable: true },
  current_key: { label: 'Document', filter: 'current_key', sortable: true },
  emitting_company: { label: 'Company', filter: 'emitting_company', sortable: true },
  document_type: { label: 'Document type', filter: 'document_type', sortable: true },
  period: { label: 'Period', filter: 'period', sortable: true },
  updated_at: { label: 'Last modified', filter: 'updated_at', sortable: true },
  template_name: { label: 'Template', filter: 'template', sortable: true },
  emission_date: { label: 'Emission date', filter: 'emission_date', sortable: false, inputType: 'date' },
  confidence: { label: 'Confidence', filter: 'confidence', sortable: true },
};
let visibleColumns = (() => {
  try { const saved = JSON.parse(localStorage.getItem('pdf-renamer-columns') || 'null'); return Array.isArray(saved) ? saved : [...defaultColumns]; }
  catch { return [...defaultColumns]; }
})();
const selectedCount = () => allFilteredSelection ? total : selectedIds.size;
const updateSelectionUi = () => {
  document.getElementById('selection-count').textContent = allFilteredSelection
    ? `All ${total} filtered documents selected`
    : `${selectedIds.size} selected`;
  document.getElementById('bulk-edit-btn').disabled = selectedCount() === 0;
  document.getElementById('select-page').checked = loadedItems.length > 0 && loadedItems.every((item) => selectedIds.has(item.id));
};
const renderColumnPicker = () => {
  const options = [...Object.entries(columnDefinitions).map(([key, value]) => [key, value.label]),
    ...availableAdditionalFields.map((key) => [`additional:${key}`, key])];
  columnOptions.innerHTML = options.map(([key, label]) =>
    `<label><input type="checkbox" data-column-option="${escapeHtml(key)}" ${visibleColumns.includes(key) ? 'checked' : ''}/> ${escapeHtml(label)}</label>`
  ).join('');
  columnOptions.querySelectorAll('[data-column-option]').forEach((control) => control.addEventListener('change', () => {
    const key = control.dataset.columnOption;
    visibleColumns = control.checked ? [...visibleColumns, key] : visibleColumns.filter((item) => item !== key);
    if (!visibleColumns.length) { visibleColumns = [...defaultColumns]; }
    localStorage.setItem('pdf-renamer-columns', JSON.stringify(visibleColumns));
    renderTableHead();
    updateSortIndicators();
    renderRows(loadedItems, false);
  }));
};
const renderTableHead = () => {
  const header = (key) => {
    const def = columnDefinitions[key] || { label: key.replace(/^additional:/, ''), filter: `additional_field__${key.slice(11)}`, sortable: false };
    return `<th scope="col" ${def.sortable ? `data-sort-column="${key}"` : ''}>${def.sortable ? `<button class="sort-button" data-sort="${key}">${escapeHtml(def.label)} <span></span></button>` : escapeHtml(def.label)}</th>`;
  };
  const previousFilterValues = Object.fromEntries(columnFilters.map((filter) => [filter.dataset.filter, filter.value]));
  const filters = visibleColumns.map((key) => {
    const def = columnDefinitions[key] || (key.startsWith('additional:') ? { label: key.slice(11), filter: `additional_field__${key.slice(11)}`, inputType: 'search' } : null);
    if (!def || !def.filter) return '<th></th>';
    const filterId = `column-filter-${def.filter}`;
    if (def.filter === 'status') return `<th><label class="visually-hidden" for="${filterId}">Filter status</label><select id="${filterId}" data-filter="status"><option value="">All</option><option value="processed">Processed</option><option value="validated">Validated</option><option value="not processed">Not processed</option><option value="unknown">Unknown</option></select></th>`;
    const inputType = def.inputType || (def.filter === 'updated_at' ? 'date' : 'search');
    return `<th><label class="visually-hidden" for="${filterId}">Filter ${escapeHtml(def.label)}</label><input id="${filterId}" data-filter="${def.filter}" type="${inputType}" placeholder="${escapeHtml(def.label)}" value="${escapeHtml(previousFilterValues[def.filter] || '')}" /></th>`;
  }).join('');
  tableHead.innerHTML = `<tr><th><input type="checkbox" id="select-page-head" aria-label="Select loaded rows"/></th>${visibleColumns.map(header).join('')}</tr><tr class="column-filters"><th></th>${filters}</tr>`;
  columnFilters = [...tableHead.querySelectorAll('[data-filter]')];
  columnFilters.forEach((filter) => filter.addEventListener('input', () => { selectedIds.clear(); allFilteredSelection = false; updateSelectionUi(); loadDocuments(); }));
  tableHead.querySelectorAll('[data-sort]').forEach((button) => button.addEventListener('click', () => {
    if (sortBy === button.dataset.sort) sortOrder = sortOrder === 'asc' ? 'desc' : 'asc';
    else { sortBy = button.dataset.sort; sortOrder = 'asc'; }
    updateSortIndicators(); loadDocuments();
  }));
  const selectHead = tableHead.querySelector('#select-page-head');
  selectHead.addEventListener('change', () => selectLoadedRows(selectHead.checked));
};
let sortButtons = [];
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
    body.innerHTML = `<tr><td colspan="${visibleColumns.length + 1}" class="empty-state">No documents found.</td></tr>`;
    return;
  }
  const listParams = getListParams();
  const rows = items.map((item) => {
    const detailParams = new URLSearchParams(listParams);
    detailParams.set('id', item.id);
    const cells = visibleColumns.map((key) => {
      if (key.startsWith('additional:')) return `<td>${escapeHtml(item.optional_fields?.[key.slice(11)] ?? '—')}</td>`;
      if (key === 'status') return `<td><span class="badge ${escapeHtml(badgeClass(item.status))}">${escapeHtml(item.status || 'unknown')}</span></td>`;
      if (key === 'current_key') return `<td><a class="document-link" href="/document.html?${detailParams.toString()}">${escapeHtml(item.current_key?.split('/').pop() || 'Untitled document')}</a><div class="key-text">${escapeHtml(item.current_key || '—')}</div></td>`;
      if (key === 'updated_at') return `<td>${escapeHtml(formatUpdated(item.updated_at))}</td>`;
      if (key === 'confidence') return `<td>${item.confidence == null ? '—' : `${(Number(item.confidence) * 100).toFixed(1)}%`}</td>`;
      return `<td>${escapeHtml(item[key] ?? '—')}</td>`;
    }).join('');
    return `<tr><td><input type="checkbox" data-select-id="${escapeHtml(item.id)}" aria-label="Select document" ${selectedIds.has(item.id) ? 'checked' : ''}/></td>${cells}</tr>`;
  }).join('');
  if (append) body.insertAdjacentHTML('beforeend', rows); else body.innerHTML = rows;
  body.querySelectorAll('[data-select-id]').forEach((control) => control.addEventListener('change', () => {
    if (control.checked) selectedIds.add(control.dataset.selectId); else selectedIds.delete(control.dataset.selectId);
    allFilteredSelection = false; updateSelectionUi();
  }));
  updateSelectionUi();
};
const selectLoadedRows = (checked) => {
  if (checked) loadedItems.forEach((item) => selectedIds.add(item.id));
  else loadedItems.forEach((item) => selectedIds.delete(item.id));
  allFilteredSelection = false;
  renderRows(loadedItems, false);
  updateSelectionUi();
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
    const response = await fetch(`/files?${params.toString()}`);
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
    availableAdditionalFields = [...new Set(loadedItems.flatMap((item) => Object.keys(item.optional_fields || {})))];
    renderColumnPicker();
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
  sortButtons = [...document.querySelectorAll('[data-sort]')];
  document.querySelectorAll('[data-sort-column]').forEach((header) => {
    const active = header.dataset.sortColumn === sortBy;
    header.setAttribute('aria-sort', active ? (sortOrder === 'asc' ? 'ascending' : 'descending') : 'none');
    const indicator = header.querySelector('.sort-button span');
    indicator.textContent = active ? (sortOrder === 'asc' ? '↑' : '↓') : '';
  });
};

renderColumnPicker();
renderTableHead();
fetch('/document-options').then((response) => response.ok ? response.json() : null).then((options) => {
  if (options && Array.isArray(options.optional_field_names)) {
    availableAdditionalFields = [...new Set([...availableAdditionalFields, ...options.optional_field_names])];
    renderColumnPicker();
  }
}).catch(() => {});
searchFilter.addEventListener('input', () => {
  selectedIds.clear(); allFilteredSelection = false; updateSelectionUi(); loadDocuments();
});
refreshButton.addEventListener('click', loadDocuments);
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

document.getElementById('select-page').addEventListener('change', (event) => selectLoadedRows(event.target.checked));
document.getElementById('select-filtered-btn').addEventListener('click', () => {
  allFilteredSelection = true;
  selectedIds.clear();
  updateSelectionUi();
});
document.getElementById('clear-selection-btn').addEventListener('click', () => {
  selectedIds.clear(); allFilteredSelection = false; renderRows(loadedItems, false); updateSelectionUi();
});
document.getElementById('bulk-edit-btn').addEventListener('click', () => {
  document.getElementById('bulk-edit-form').hidden = false;
  document.getElementById('bulk-scope').textContent = allFilteredSelection
    ? `This will apply to all ${total} documents matching the active filters.`
    : `This will apply to ${selectedIds.size} selected documents.`;
});
document.getElementById('cancel-bulk-edit').addEventListener('click', () => { document.getElementById('bulk-edit-form').hidden = true; });
document.getElementById('bulk-edit-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const changes = {};
  const company = document.getElementById('bulk-company').value.trim();
  const type = document.getElementById('bulk-type').value.trim();
  if (company) changes.emitting_company = company;
  if (type) changes.document_type = type;
  const rawFields = document.getElementById('bulk-fields').value.trim();
  if (rawFields) {
    try { changes.additional_fields = JSON.parse(rawFields); }
    catch { document.getElementById('bulk-result').textContent = 'Additional fields must be valid JSON.'; return; }
  }
  if (!Object.keys(changes).length) { document.getElementById('bulk-result').textContent = 'Choose at least one change.'; return; }
  const payload = allFilteredSelection
    ? { all_matching: true, filters: Object.fromEntries(getListParams()), changes }
    : { ids: [...selectedIds], changes };
  const result = document.getElementById('bulk-result');
  result.textContent = 'Applying changes…';
  try {
    const response = await fetch('/files/bulk-edit', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`);
    result.textContent = `${data.succeeded} succeeded, ${data.failed} failed out of ${data.requested}.`;
    selectedIds.clear(); allFilteredSelection = false;
    await loadDocuments();
  } catch (error) { result.textContent = `Bulk edit failed: ${error.message}`; }
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
