const rebuildButton = document.getElementById('rebuild-button');
const checkButton = document.getElementById('check-button');
const actionButtons = [rebuildButton, checkButton];

const runAction = async ({ operation, progress, status, endpoint, method, confirmation, getResult }) => {
  if (!window.confirm(confirmation)) return;

  actionButtons.forEach((actionButton) => { actionButton.disabled = true; });
  operation.hidden = false;
  progress.hidden = false;
  progress.removeAttribute('value');
  status.textContent = 'Running...';

  try {
    const response = await fetch(endpoint, { method });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const result = await response.json();
    progress.value = 1;
    status.textContent = getResult(result);
  } catch (error) {
    progress.hidden = true;
    status.textContent = 'The action failed. Check the API server and storage backend.';
    console.error(error);
  } finally {
    actionButtons.forEach((actionButton) => { actionButton.disabled = false; });
  }
};

rebuildButton.addEventListener('click', () => runAction({
  operation: document.getElementById('rebuild-operation'),
  progress: document.getElementById('rebuild-progress'),
  status: document.getElementById('rebuild-status'),
  endpoint: '/consistency/rebuild',
  method: 'POST',
  confirmation: 'Rebuild the database from sidecar files? Existing database records will be replaced.',
  getResult: (result) => `Database rebuilt from ${result.count} sidecar file${result.count === 1 ? '' : 's'}.`,
}));

checkButton.addEventListener('click', () => runAction({
  operation: document.getElementById('check-operation'),
  progress: document.getElementById('check-progress'),
  status: document.getElementById('check-status'),
  endpoint: '/consistency',
  method: 'GET',
  confirmation: 'Run a consistency check? This scans metadata and database records against storage.',
  getResult: (anomalies) => anomalies.length
    ? `Consistency check complete: ${anomalies.length} issue${anomalies.length === 1 ? '' : 's'} found.`
    : 'Consistency check complete: no issues found.',
}));

const modelsList = document.getElementById('models-list');
const modelsStatus = document.getElementById('models-status');
const refreshModelsButton = document.getElementById('refresh-models');
const trainModelForm = document.getElementById('train-model-form');
const modelTypeSelect = document.getElementById('model-type');
const trainModelButton = document.getElementById('train-model-button');

const escapeHtml = (value) => String(value).replace(/[&<>"']/g, (character) => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
}[character]));

const loadModels = async () => {
  refreshModelsButton.disabled = true;
  modelsStatus.textContent = 'Loading model inventory…';
  try {
    const response = await fetch('/models');
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail || `HTTP ${response.status}`);
    const models = result.models || [];
    modelsList.innerHTML = models.map((model) => {
      const versions = model.versions || [];
      const activeLabel = model.has_active_model
        ? `Active version: ${escapeHtml(model.active_version)}`
        : 'No active version';
      const versionContent = versions.length
        ? versions.map((item) => `
            <div class="model-version">
              <span><code>${escapeHtml(item.version)}</code>${item.active ? ' <span class="model-active-tag">Active</span>' : ''}</span>
              ${item.active ? '' : `<button class="secondary-btn activate-model" type="button" data-model="${escapeHtml(model.name)}" data-version="${escapeHtml(item.version)}">Activate</button>`}
            </div>
          `).join('')
        : '<p class="model-empty">No trained versions yet.</p>';
      return `
        <article class="model-card">
          <div class="model-card-heading">
            <div><h4>${escapeHtml(model.name)}</h4><p>${activeLabel}</p></div>
            <span class="model-count">${versions.length} version${versions.length === 1 ? '' : 's'}</span>
          </div>
          <div class="model-versions">${versionContent}</div>
        </article>
      `;
    }).join('');
    if (!models.length) modelsList.innerHTML = '<p>No model types found.</p>';
    modelsStatus.textContent = `Found ${models.length} model type${models.length === 1 ? '' : 's'}. Model directory: ${result.model_dir}`;
  } catch (error) {
    modelsStatus.textContent = `Could not load models: ${error.message}`;
  } finally {
    refreshModelsButton.disabled = false;
  }
};

trainModelForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  const modelName = modelTypeSelect.value;
  if (!window.confirm(`Train a new ${modelName} model version from validated document records?`)) return;

  trainModelButton.disabled = true;
  refreshModelsButton.disabled = true;
  modelsStatus.textContent = 'Training model… This may take a while.';
  try {
    const response = await fetch('/models/train', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ model_name: modelName }),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail || `HTTP ${response.status}`);
    modelsStatus.textContent = `Trained ${result.name} version ${result.version} from ${result.samples} examples. Activate it below when ready.`;
    await loadModels();
  } catch (error) {
    modelsStatus.textContent = `Training failed: ${error.message}`;
  } finally {
    trainModelButton.disabled = false;
    refreshModelsButton.disabled = false;
  }
});

modelsList.addEventListener('click', async (event) => {
  const button = event.target.closest('.activate-model');
  if (!button) return;
  const { model, version } = button.dataset;
  if (!window.confirm(`Activate ${model} version ${version} for runtime inference?`)) return;
  button.disabled = true;
  modelsStatus.textContent = `Activating ${model} version ${version}…`;
  try {
    const response = await fetch(`/models/${encodeURIComponent(model)}/${encodeURIComponent(version)}/activate`, {
      method: 'POST',
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail || `HTTP ${response.status}`);
    modelsStatus.textContent = `${result.name} version ${result.active_version} is now active.`;
    await loadModels();
  } catch (error) {
    modelsStatus.textContent = `Activation failed: ${error.message}`;
    button.disabled = false;
  }
});

refreshModelsButton.addEventListener('click', loadModels);
loadModels();
