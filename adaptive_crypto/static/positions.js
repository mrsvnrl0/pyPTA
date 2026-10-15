const addDialog = document.getElementById('add-position-dialog');
document.getElementById('add-position').addEventListener('click', () => addDialog.showModal());
document.getElementById('cancel-add-position').addEventListener('click', () => {
  if (addDialog.querySelector('form').dataset.saving !== '1') addDialog.close();
});
addDialog.addEventListener('cancel', event => {
  if (addDialog.querySelector('form').dataset.saving === '1') event.preventDefault();
});

const positionType = addDialog.querySelector('[name="position_type"]');
const paperFollowup = addDialog.querySelector('[data-paper-followup]');
function syncPaperFollowup() {
  if (!paperFollowup || !positionType) return;
  paperFollowup.disabled = positionType.value !== 'spot';
  if (paperFollowup.disabled) paperFollowup.checked = false;
}
positionType?.addEventListener('change', syncPaperFollowup);
positionType?.form?.addEventListener('reset', () => setTimeout(syncPaperFollowup, 0));
syncPaperFollowup();

document.addEventListener('click', event => {
  const button = event.target.closest('[data-suggested-stop]');
  if (!button) return;
  const input = button.closest('form').querySelector('[name="stop_price"]');
  input.value = button.dataset.suggestedStop;
  input.dispatchEvent(new Event('input', {bubbles:true}));
  input.focus();
});

const pinePrefill = document.getElementById('pine-position-prefill');
if (pinePrefill) {
  const plan=JSON.parse(pinePrefill.textContent), form=addDialog.querySelector('form');
  for (const [key,value] of Object.entries({asset:plan.asset,entry:plan.entry_price,stop_price:plan.stop,position_type:plan.side==='short'?'margin_short':'spot',target_mode:'nn'})) {
    const field=form.elements.namedItem(key); if(field) { field.value=String(value);field.dispatchEvent(new Event('change',{bubbles:true})); }
  }
  const currency = form.querySelector('select[name="asset"]')?.selectedOptions[0]?.dataset.quote || plan.symbol?.split('/')?.at(-1);
  const level = value => value == null ? 'pending' : String(value) + (currency ? ' ' + currency : '');
  const note=document.createElement('p');note.className='notice';note.textContent='SMC Engine plan prefilled. Review the actual entry and quantity. NN monitoring remains active. Suggested TP1 '+level(plan.nearest_target)+'; TP2 '+level(plan.tp2)+'.';form.prepend(note);
  syncPaperFollowup(); addDialog.showModal();
}
