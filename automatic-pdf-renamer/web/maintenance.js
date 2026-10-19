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

const trainModelButton = document.getElementById('train-model-button');
const modelTrainingOperation = document.getElementById('model-training-operation');
const modelTrainingProgress = document.getElementById('model-training-progress');
const modelsStatus = document.getElementById('models-status');
const modelsInventoryStatus = document.getElementById('models-inventory-status');

const sleep = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));

const pollModelTraining = async (jobId) => {
  while (true) {
    const response = await fetch(`/jobs/${encodeURIComponent(jobId)}`);
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail || `HTTP ${response.status}`);

    modelTrainingProgress.value = Number(result.progress || 0);
    modelsStatus.textContent = result.message || result.stage || 'Training models…';

    if (result.status === 'completed') {
      modelTrainingProgress.value = 100;
      modelsInventoryStatus.textContent = 'All models were replaced successfully. Previous model artifacts have been removed.';
      return;
    }
    if (result.status === 'failed') {
      throw new Error(result.message || 'Model retraining failed.');
    }
    await sleep(1000);
  }
};

trainModelButton.addEventListener('click', async () => {
  if (!window.confirm('Retrain all models from validated documents? The current models will be replaced after the complete training succeeds.')) return;

  trainModelButton.disabled = true;
  actionButtons.forEach((button) => { button.disabled = true; });
  modelTrainingOperation.hidden = false;
  modelTrainingProgress.value = 0;
  modelsInventoryStatus.textContent = '';
  modelsStatus.textContent = 'Starting full model retraining…';

  try {
    const response = await fetch('/models/train', { method: 'POST' });
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail || `HTTP ${response.status}`);
    await pollModelTraining(result.job_id);
    modelsStatus.textContent = 'Model retraining completed.';
  } catch (error) {
    modelsStatus.textContent = `Model retraining failed: ${error.message}`;
    modelsInventoryStatus.textContent = 'The previous model set should remain active if training failed before replacement.';
    console.error(error);
  } finally {
    trainModelButton.disabled = false;
    actionButtons.forEach((button) => { button.disabled = false; });
  }
});
