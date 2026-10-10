const title = document.getElementById('document-title');
const statusMessage = document.getElementById('detail-status');
const content = document.getElementById('document-content');
const technicalDataList = document.getElementById('technical-data');
const additionalSection = document.getElementById('additional-section');
const additionalFields = document.getElementById('additional-fields');
const addAdditionalFieldButton = document.getElementById('add-additional-field');
const historyList = document.getElementById('document-history');
const extractedText = document.getElementById('extracted-text');
const pdfPreview = document.getElementById('pdf-preview');
const openPdf = document.getElementById('open-pdf');
const previousButton = document.getElementById('previous-document');
const nextButton = document.getElementById('next-document');
const documentPosition = document.getElementById('document-position');
const editForm = document.getElementById('document-edit-form');
const templateSelect = document.getElementById('edit-template');
const periodFormatSelect = document.getElementById('edit-period-format');
const companyInput = document.getElementById('edit-company');
const documentTypeInput = document.getElementById('edit-document-type');
const emissionDateInput = document.getElementById('edit-emission-date');
const periodPreview = document.getElementById('period-preview');
const keyPreview = document.getElementById('current-key-preview');
const saveButton = document.getElementById('save-document-button');
const editStatus = document.getElementById('edit-status');
saveButton.disabled = true;
let currentFileId = null;
let fieldSuggestionState = { names: [], values: {} };
let initialDocumentState = null;

const canonicalizeState = (value) => {
  if (value === null || value === undefined) return '';
  if (typeof value === 'string') return value.trim();
  if (Array.isArray(value)) return value.map((item) => canonicalizeState(item));
  if (typeof value === 'object') {
    return Object.fromEntries(
      Object.entries(value)
        .sort(([left], [right]) => left.localeCompare(right))
        .map(([key, nestedValue]) => [key, canonicalizeState(nestedValue)]),
    );
  }
  return value;
};

const getCurrentDocumentState = () => ({
  template_name: templateSelect.value || '',
  period_format: periodFormatSelect.value || '',
  emitting_company: companyInput.value.trim(),
  document_type: documentTypeInput.value.trim(),
  emission_date: parseEmissionDateInput(emissionDateInput.value) || '',
  optional_fields: canonicalizeState(getAdditionalFieldEntries()),
});

const updateSaveButtonState = () => {
  if (!currentFileId || !initialDocumentState) {
    saveButton.disabled = true;
    return;
  }
  const currentState = canonicalizeState(getCurrentDocumentState());
  const baselineState = canonicalizeState(initialDocumentState);
  saveButton.disabled = JSON.stringify(currentState) === JSON.stringify(baselineState);
};

const openNeighbor = (neighborId) => {
  if (!neighborId) return;
  const params = new URLSearchParams(window.location.search);
  params.set('id', neighborId);
  window.location.href = `/document.html?${params.toString()}`;
};

const loadDocumentNavigation = async (fileId) => {
  previousButton.disabled = true;
  nextButton.disabled = true;
  const filters = new URLSearchParams(window.location.search);
  filters.delete('id');
  filters.delete('page');
  if (!filters.has('sort_by')) filters.set('sort_by', 'updated_at');
  if (!filters.has('sort_order')) filters.set('sort_order', 'desc');

  let previous = null;
  let page = 1;
  let total = 0;
  try {
    while (true) {
      const params = new URLSearchParams(filters);
      params.set('page', String(page));
      const response = await fetch(`/api/files?${params.toString()}`);
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const items = await response.json();
      total = Number(response.headers.get('X-Total-Count')) || total;
      const index = items.findIndex((item) => String(item.id) === fileId);

      if (index !== -1) {
        const preceding = index > 0 ? items[index - 1] : previous;
        let following = index + 1 < items.length ? items[index + 1] : null;
        const position = (page - 1) * 25 + index + 1;
        if (!following && position < total) {
          const nextParams = new URLSearchParams(filters);
          nextParams.set('page', String(page + 1));
          const nextResponse = await fetch(`/api/files?${nextParams.toString()}`);
          if (!nextResponse.ok) throw new Error(`HTTP ${nextResponse.status}`);
          following = (await nextResponse.json())[0] || null;
        }

        previousButton.disabled = !preceding;
        nextButton.disabled = !following;
        previousButton.onclick = () => openNeighbor(preceding?.id);
        nextButton.onclick = () => openNeighbor(following?.id);
        documentPosition.textContent = `${position} of ${total}`;
        return;
      }

      if (items.length < 25 || page * 25 >= total) break;
      previous = items[items.length - 1] || previous;
      page += 1;
    }
    documentPosition.textContent = 'Not in this list';
  } catch (error) {
    documentPosition.textContent = 'Navigation unavailable';
    console.error(error);
  }
};

const formatDate = (value) => {
  if (!value) return 'Not recorded';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('fr-FR');
};

const formatEmissionDateInput = (value) => {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value || '');
  return match ? `${match[3]}/${match[2]}/${match[1]}` : '';
};

const parseEmissionDateInput = (value) => {
  const match = /^(\d{2})\/(\d{2})\/(\d{4})$/.exec(value.trim());
  if (!match) return null;
  const [, day, month, year] = match;
  const isoDate = `${year}-${month}-${day}`;
  const parsedDate = new Date(`${isoDate}T00:00:00Z`);
  if (Number.isNaN(parsedDate.getTime()) || parsedDate.toISOString().slice(0, 10) !== isoDate) return null;
  return isoDate;
};

const addMetadata = (target, label, value) => {
  if (value === null || value === undefined || value === '') return;
  const term = document.createElement('dt');
  term.textContent = label;
  const description = document.createElement('dd');
  description.textContent = typeof value === 'object' ? JSON.stringify(value, null, 2) : String(value);
  target.append(term, description);
};

const getFieldValueSuggestions = (suggestions, label) => {
  const values = suggestions.values?.[label];
  return Array.isArray(values) ? values : [];
};

const buildAdditionalFieldRow = (label = '', value = '', suggestions = {}) => {
  const row = document.createElement('div');
  row.className = 'additional-field-row';

  const labelSelect = document.createElement('select');
  labelSelect.className = 'additional-field-label';
  labelSelect.setAttribute('aria-label', 'Additional field name');
  const labelValues = [...new Set([...(suggestions.names || []), label].filter((item) => item !== null && item !== undefined && String(item).trim() !== ''))];
  setSelectOptions(labelSelect, labelValues, label);

  const valueSelect = document.createElement('select');
  valueSelect.className = 'additional-field-value';
  valueSelect.setAttribute('aria-label', 'Additional field value');
  const valueValues = [...new Set([...getFieldValueSuggestions(suggestions, labelSelect.value), value].filter((item) => item !== null && item !== undefined && String(item).trim() !== ''))];
  setSelectOptions(valueSelect, valueValues, value);
  labelSelect.addEventListener('change', () => {
    setSelectOptions(valueSelect, getFieldValueSuggestions(suggestions, labelSelect.value), '');
  });

  const removeButton = document.createElement('button');
  removeButton.type = 'button';
  removeButton.className = 'secondary-btn small-btn';
  removeButton.textContent = 'Remove';
  removeButton.addEventListener('click', () => {
    row.remove();
    if (!additionalFields.querySelector('.additional-field-row')) {
      const empty = document.createElement('p');
      empty.className = 'additional-field-empty';
      empty.textContent = 'No custom fields yet.';
      additionalFields.append(empty);
    }
    updateSaveButtonState();
  });

  row.append(labelSelect, valueSelect, removeButton);
  return row;
};

const renderAdditionalFields = (fields, suggestions = {}) => {
  additionalFields.replaceChildren();
  const entries = Object.entries(fields || {});
  if (!entries.length) {
    const empty = document.createElement('p');
    empty.className = 'additional-field-empty';
    empty.textContent = 'No custom fields yet.';
    additionalFields.append(empty);
    return;
  }

  entries.forEach(([label, value]) => {
    additionalFields.append(buildAdditionalFieldRow(label, value, suggestions));
  });
};

const updateDocumentSuggestions = (options = {}) => {
  setSelectOptions(companyInput, options.companies || [], companyInput.value || '');
  setSelectOptions(documentTypeInput, options.document_types || [], documentTypeInput.value || '');

  const suggestionState = {
    names: options.optional_field_names || [],
    values: options.optional_field_values || {},
  };

  additionalFields.querySelectorAll('.additional-field-row').forEach((row) => {
    const labelSelect = row.querySelector('.additional-field-label');
    const valueSelect = row.querySelector('.additional-field-value');
    if (labelSelect) {
      const current = labelSelect.value || '';
      setSelectOptions(labelSelect, suggestionState.names, current);
    }
    if (valueSelect) {
      const current = valueSelect.value || '';
      setSelectOptions(valueSelect, getFieldValueSuggestions(suggestionState, labelSelect?.value || ''), current);
    }
  });

  return suggestionState;
};

const getAdditionalFieldEntries = () => {
  const fields = {};
  additionalFields.querySelectorAll('.additional-field-row').forEach((row) => {
    const labelSelect = row.querySelector('.additional-field-label');
    const valueSelect = row.querySelector('.additional-field-value');
    const label = (labelSelect?.value || '').trim();
    if (!label) return;
    fields[label] = valueSelect?.value ?? '';
  });
  return fields;
};

const setSelectOptions = (select, values, selectedValue) => {
  select.replaceChildren();
  const options = [...values];
  if (selectedValue && !options.includes(selectedValue)) options.unshift(selectedValue);
  if (!options.length) {
    const placeholder = document.createElement('option');
    placeholder.value = '';
    placeholder.textContent = 'Select...';
    select.append(placeholder);
    select.value = '';
    return;
  }
  options.forEach((value) => {
    const option = document.createElement('option');
    option.value = value;
    option.textContent = value;
    select.append(option);
  });
  select.value = selectedValue || options[0] || '';
};

const renderHistory = (events) => {
  historyList.replaceChildren();
  if (!events.length) {
    const empty = document.createElement('li');
    empty.className = 'history-empty';
    empty.textContent = 'No history has been recorded for this document.';
    historyList.append(empty);
    return;
  }

  [...events].reverse().forEach((event) => {
    const entry = document.createElement('li');
    entry.className = 'history-entry';
    const heading = document.createElement('div');
    heading.className = 'history-heading';
    const kind = document.createElement('strong');
    kind.textContent = String(event.type || 'Update').replaceAll('_', ' ');
    const timestamp = document.createElement('time');
    timestamp.textContent = formatDate(event.timestamp);
    heading.append(kind, timestamp);
    entry.append(heading);

    if (event.payload && Object.keys(event.payload).length) {
      const payload = document.createElement('pre');
      payload.textContent = JSON.stringify(event.payload, null, 2);
      entry.append(payload);
    }
    historyList.append(entry);
  });
};

const renderDocument = (documentData, fileId) => {
  currentFileId = fileId;
  title.textContent = documentData.current_key.split('/').pop() || 'Document';
  technicalDataList.replaceChildren();
  additionalFields.replaceChildren();
  templateSelect.value = documentData.template_name || '';
  periodFormatSelect.value = documentData.period_format || '';
  companyInput.value = documentData.emitting_company || '';
  documentTypeInput.value = documentData.document_type || '';
  emissionDateInput.value = formatEmissionDateInput(documentData.emission_date);
  periodPreview.textContent = documentData.period || '—';
  keyPreview.textContent = documentData.current_key || '—';

  renderAdditionalFields(documentData.optional_fields || {}, fieldSuggestionState);

  initialDocumentState = {
    template_name: templateSelect.value || '',
    period_format: periodFormatSelect.value || '',
    emitting_company: (companyInput.value || '').trim(),
    document_type: (documentTypeInput.value || '').trim(),
    emission_date: parseEmissionDateInput(emissionDateInput.value) || '',
    optional_fields: canonicalizeState(getAdditionalFieldEntries()),
  };
  updateSaveButtonState();

  const technicalMetadata = [
    ['Status', documentData.status],
    ['Confidence', documentData.confidence],
    ['Original key', documentData.original_key],
    ['Current key', documentData.current_key],
    ['SHA-256', documentData.id],
    ['Created', formatDate(documentData.created_at)],
    ['Updated', formatDate(documentData.updated_at)],
    ['Schema version', documentData.schema_version],
  ];
  technicalMetadata.forEach(([label, value]) => addMetadata(technicalDataList, label, value));

  renderHistory(Array.isArray(documentData.history) ? documentData.history : []);
  extractedText.textContent = documentData.extracted_text || 'No extracted text is available.';
  const pdfUrl = `/files/${encodeURIComponent(fileId)}/pdf`;
  pdfPreview.src = pdfUrl;
  openPdf.href = pdfUrl;
  content.hidden = false;
  statusMessage.textContent = '';
};

const saveDocument = async (event) => {
  event.preventDefault();
  if (!currentFileId || saveButton.disabled) return;
  const emissionDate = parseEmissionDateInput(emissionDateInput.value);
  emissionDateInput.setCustomValidity(emissionDate ? '' : 'Enter a valid date in dd/mm/yyyy format.');
  if (!editForm.reportValidity()) return;

  saveButton.disabled = true;
  editStatus.textContent = 'Recalculating document name and saving...';
  const payload = {
    template_name: templateSelect.value,
    period_format: periodFormatSelect.value,
    emitting_company: companyInput.value.trim(),
    document_type: documentTypeInput.value.trim(),
    emission_date: emissionDate,
    optional_fields: getAdditionalFieldEntries(),
  };
  try {
    const response = await fetch(`/api/files/${encodeURIComponent(currentFileId)}/edit`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail || `HTTP ${response.status}`);
    renderDocument(result, currentFileId);
    editStatus.textContent = 'Changes saved. The document is validated.';
    updateSaveButtonState();
    await loadDocumentNavigation(currentFileId);
  } catch (error) {
    editStatus.textContent = `Unable to save changes: ${error.message}`;
    console.error(error);
  } finally {
    saveButton.disabled = false;
  }
};

if (addAdditionalFieldButton) {
  addAdditionalFieldButton.addEventListener('click', () => {
    const emptyState = additionalFields.querySelector('.additional-field-empty');
    if (emptyState) emptyState.remove();
    additionalFields.append(buildAdditionalFieldRow('', '', fieldSuggestionState));
    updateSaveButtonState();
  });
}

editForm.addEventListener('input', updateSaveButtonState);
editForm.addEventListener('change', updateSaveButtonState);
additionalFields.addEventListener('input', updateSaveButtonState);
additionalFields.addEventListener('change', updateSaveButtonState);

const loadDocument = async () => {
  const fileId = new URLSearchParams(window.location.search).get('id');
  if (!fileId) {
    title.textContent = 'Document not specified';
    statusMessage.textContent = 'Return to the document list and select a document.';
    return;
  }

  try {
    const [documentResponse, optionsResponse] = await Promise.all([
      fetch(`/api/files/${encodeURIComponent(fileId)}`),
      fetch('/api/document-options'),
    ]);
    if (!documentResponse.ok) throw new Error(`HTTP ${documentResponse.status}`);
    if (!optionsResponse.ok) throw new Error(`Unable to load edit options (HTTP ${optionsResponse.status})`);
    const [documentData, options] = await Promise.all([
      documentResponse.json(),
      optionsResponse.json(),
    ]);
    fieldSuggestionState = {
      names: options.optional_field_names || [],
      values: options.optional_field_values || {},
    };
    setSelectOptions(templateSelect, options.templates || [], documentData.template_name);
    setSelectOptions(periodFormatSelect, options.period_formats || [], documentData.period_format);
    setSelectOptions(companyInput, options.companies || [], documentData.emitting_company);
    setSelectOptions(documentTypeInput, options.document_types || [], documentData.document_type);
    renderDocument(documentData, fileId);
    renderAdditionalFields(documentData.optional_fields || {}, fieldSuggestionState);
    updateSaveButtonState();
    await loadDocumentNavigation(fileId);
  } catch (error) {
    title.textContent = 'Unable to load document';
    statusMessage.textContent = 'The document could not be loaded. It may have been removed or the API may be unavailable.';
    console.error(error);
  }
};

editForm.addEventListener('submit', saveDocument);
loadDocument();