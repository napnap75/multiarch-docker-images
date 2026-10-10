const form = document.getElementById('config-form');
const editor = document.getElementById('config-editor');
const saveButton = document.getElementById('save-config-button');
const status = document.getElementById('config-status');

const loadConfig = async () => {
  try {
    const response = await fetch('/api/config');
    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`);
    }

    const result = await response.json();
    editor.value = result.content;
    editor.disabled = false;
    saveButton.disabled = false;
    status.textContent = '';
  } catch (error) {
    status.textContent = 'Unable to load the configuration file.';
    console.error(error);
  }
};

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  saveButton.disabled = true;
  editor.disabled = true;
  status.textContent = 'Validating and saving configuration...';

  try {
    const response = await fetch('/api/config', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ content: editor.value }),
    });
    if (!response.ok) {
      const result = await response.json();
      throw new Error(result.detail || `HTTP ${response.status}`);
    }

    status.textContent = 'Configuration saved and reloaded. Reloading the page...';
    window.setTimeout(() => window.location.reload(), 600);
  } catch (error) {
    status.textContent = error.message || 'Unable to save the configuration file.';
    console.error(error);
    editor.disabled = false;
    saveButton.disabled = false;
  }
});

loadConfig();
