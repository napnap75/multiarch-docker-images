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
  endpoint: '/api/consistency/rebuild',
  method: 'POST',
  confirmation: 'Rebuild the database from sidecar files? Existing database records will be replaced.',
  getResult: (result) => `Database rebuilt from ${result.count} sidecar file${result.count === 1 ? '' : 's'}.`,
}));

checkButton.addEventListener('click', () => runAction({
  operation: document.getElementById('check-operation'),
  progress: document.getElementById('check-progress'),
  status: document.getElementById('check-status'),
  endpoint: '/api/consistency',
  method: 'GET',
  confirmation: 'Run a consistency check? This scans metadata and database records against storage.',
  getResult: (anomalies) => anomalies.length
    ? `Consistency check complete: ${anomalies.length} issue${anomalies.length === 1 ? '' : 's'} found.`
    : 'Consistency check complete: no issues found.',
}));