'use strict';
const editor = document.getElementById('settings-form');
const result = document.getElementById('settings-result');
const unsaved = document.getElementById('unsaved-status');
let savedStrategy = {};
try { savedStrategy = JSON.parse(document.getElementById('saved-strategy')?.textContent || '{}'); }
catch (_) { savedStrategy = {}; }
if (!savedStrategy || typeof savedStrategy !== 'object' || Array.isArray(savedStrategy)) savedStrategy = {};
function ruleInputs() {
  return editor ? Array.from(editor.querySelectorAll('[data-rule]')) : [];
}
let dirty = false;
let busy = false;
function markDirty(value = true) {
  dirty = value;
  unsaved.textContent = value ? 'Unsaved edits' : 'No unsaved edits';
}
editor.addEventListener('input', () => markDirty());
editor.addEventListener('change', () => markDirty());
window.addEventListener('beforeunload', event => {
  if (dirty || busy) { event.preventDefault(); event.returnValue = ''; }
});
const rows = document.getElementById('asset-rows');
let savedRows = rows.innerHTML;
document.getElementById('add-asset').addEventListener('click', () => {
  const row = rows.firstElementChild.cloneNode(true);
  row.querySelectorAll('input').forEach(input => { input.value = input.dataset.assetField === 'price_decimals' ? '3' : ''; });
  row.querySelector('select').value = 'true';
  row.querySelector('button').setAttribute('aria-label', 'Remove new currency pair');
  rows.append(row);
  row.querySelector('input').focus();
  markDirty();
});
rows.addEventListener('click', event => {
  const button = event.target.closest('.remove-asset');
  if (!button) return;
  if (rows.children.length === 1) { result.textContent = 'Keep at least one currency pair.'; return; }
  button.closest('.asset-settings-row').remove();
  markDirty();
});
editor.addEventListener('reset', () => {
  rows.innerHTML = savedRows;
  markDirty(false);
  result.textContent = 'Unsaved edits discarded.';
});
function readValue(input) {
  if (input.type === 'checkbox') return input.checked;
  if (input.dataset.kind === 'bool' || input.dataset.assetField === 'enabled') return input.value === 'true';
  if (['int', 'number'].includes(input.dataset.kind) || input.dataset.assetField === 'price_decimals') return Number(input.value);
  return input.dataset.assetField === 'symbol' || input.dataset.assetField === 'name' ? input.value.trim().toUpperCase() : input.value;
}
function settingsDocument() {
  return {
    assets: Array.from(rows.children, row => Object.fromEntries(Array.from(row.querySelectorAll('[data-asset-field]'), input => [input.dataset.assetField, readValue(input)]))),
    refresh_seconds: Number(document.getElementById('refresh-seconds').value),
    strategy: {...savedStrategy, ...Object.fromEntries(ruleInputs().map(input => [input.dataset.rule, readValue(input)])), nn_model_id:'parente_mlp_v1', strategy_model:'neural_network'},
  };
}
function lockControls() {
  busy = true;
  const controls = Array.from(document.querySelectorAll('input, select, textarea, button')).filter(control => !control.disabled);
  controls.forEach(control => { control.disabled = true; });
  return () => { busy = false; controls.forEach(control => { control.disabled = false; }); };
}
async function post(url, body) {
  const response = await fetch(url, {method: 'POST', headers: {'Content-Type': 'application/json', Accept: 'application/json'}, body: JSON.stringify(body)});
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || 'Unable to complete this request.');
  return data;
}
function showError(target, error) {
  target.classList.add('danger');
  target.textContent = error.name === 'TypeError' ? 'Connection interrupted. Reload to check whether the request completed before retrying.' : error.message;
}
editor.addEventListener('submit', async event => {
  event.preventDefault();
  if (busy) return;
  const payload = {settings: settingsDocument(), revision: editor.dataset.revision, csrf_token: editor.elements.csrf_token.value};
  const unlock = lockControls();
  result.classList.remove('danger');
  result.textContent = 'Validating and saving settings…';
  try {
    const data = await post(editor.action, payload);
    editor.dataset.revision = data.revision;
    editor.querySelectorAll('input').forEach(input => { input.defaultValue = input.value; if (input.type === 'checkbox') input.defaultChecked = input.checked; });
    editor.querySelectorAll('select').forEach(select => Array.from(select.options).forEach(option => { option.defaultSelected = option.selected; }));
    savedStrategy = payload.settings.strategy;
    const baseline = document.getElementById('saved-strategy');
    if (baseline) baseline.textContent = JSON.stringify(savedStrategy);
    // Capture the enabled controls after unlocking, so discard never restores disabled fields.
    markDirty(false);
    result.textContent = data.message;
  } catch (error) { showError(result, error); }
  finally { unlock(); if (!dirty) savedRows = rows.innerHTML; }
});
const purgeDialog = document.getElementById('purge-dialog');
document.getElementById('open-purge')?.addEventListener('click', () => {
  if (dirty) { result.textContent = 'Save or discard your edits before clearing the database.'; return; }
  purgeDialog.showModal();
});
document.getElementById('cancel-purge')?.addEventListener('click', () => purgeDialog.close());
purgeDialog?.addEventListener('cancel', event => { if (busy) event.preventDefault(); });
document.querySelectorAll('.maintenance-form').forEach(form => form.addEventListener('submit', async event => {
  event.preventDefault();
  if (busy) return;
  const status = (form.closest('dialog') || form.closest('.strategy-controls')).querySelector('.maintenance-result');
  status.classList.remove('danger');
  if (dirty) { status.textContent = 'Save or discard your edits before applying settings.'; return; }
  const payload = Object.fromEntries(new FormData(form));
  const unlock = lockControls();
  status.textContent = 'Waiting for the current scan or alert delivery to finish…';
  try {
    await post(form.action, payload);
    unlock();
    window.location.href = '/settings#settings-database';
    window.location.reload();
  } catch (error) { showError(status, error); }
  finally { unlock(); }
}));
document.getElementById('restart-form').addEventListener('submit', async event => {
  event.preventDefault();
  if (busy) return;
  const status = document.getElementById('restart-result');
  status.classList.remove('danger');
  if (dirty) { status.textContent = 'Save or discard your edits before restarting.'; return; }
  const form = event.currentTarget;
  const payload = Object.fromEntries(new FormData(form));
  const unlock = lockControls();
  status.textContent = 'Checking saved settings and waiting for scans and deliveries to finish…';
  try {
    const data = await post(form.action, payload);
    status.textContent = 'Restarting dashboard. This page will reconnect automatically…';
    for (let attempt = 0; attempt < 120; attempt++) {
      await new Promise(resolve => setTimeout(resolve, 1000));
      try {
        const response = await fetch('/api/dashboard/status', {cache: 'no-store', signal: AbortSignal.timeout(2000)});
        const current = await response.json();
        if (response.ok && !current.restarting && current.generation !== data.generation) {
          unlock();
          try { sessionStorage.setItem('dashboard-restarted', '1'); } catch (_) { /* Storage is optional. */ }
          window.location.reload();
          return;
        }
      } catch (_) { /* The listener closes briefly during restart. */ }
    }
    throw new Error('The dashboard has not reconnected yet. Check the server log, then reload this page.');
  } catch (error) { showError(status, error); }
  finally { unlock(); }
});
try {
  if (sessionStorage.getItem('dashboard-restarted')) {
    document.getElementById('restart-result').textContent = 'Dashboard restarted successfully.';
    sessionStorage.removeItem('dashboard-restarted');
  }
} catch (_) { /* Storage is optional. */ }
