(() => {
'use strict';
const form=document.getElementById('dvol-settings-form');if(!form)return;
const rows=document.getElementById('dvol-pair-rows');
const outcome=document.getElementById('dvol-settings-result');
const availability=document.getElementById('dvol-pairs-availability');
const add=document.getElementById('dvol-add-pair');
const discard=document.getElementById('dvol-discard-pairs');
let revision=null,available=[],busy=false,dirty=false;
const text=(node,value,error=false)=>{node.textContent=value;node.classList.toggle('danger',error);};
const lock=value=>{busy=value;for(const control of form.querySelectorAll('input,select,button'))control.disabled=value;};
function row(pair={symbol:'',enabled:true}){
 const wrapper=document.createElement('div');wrapper.className='settings-grid';wrapper.style.alignItems='end';
 const pairLabel=document.createElement('label');pairLabel.textContent='Deribit pair';
 const input=document.createElement('input');input.type='text';input.required=true;input.maxLength=61;input.value=pair.symbol;input.dataset.dvolField='symbol';input.setAttribute('list','dvol-pair-suggestions');pairLabel.append(input);
 const stateLabel=document.createElement('label');stateLabel.textContent='Status';
 const select=document.createElement('select');select.dataset.dvolField='enabled';
 for(const [value,label] of [['true','Enabled'],['false','Disabled']]){const option=document.createElement('option');option.value=value;option.textContent=label;select.append(option);}
 select.value=String(pair.enabled);stateLabel.append(select);
 const remove=document.createElement('button');remove.type='button';remove.className='secondary';remove.textContent='Remove';remove.setAttribute('aria-label','Remove DVOL pair');remove.addEventListener('click',()=>{wrapper.remove();dirty=true;});
 wrapper.append(pairLabel,stateLabel,remove);rows.append(wrapper);
}
function render(document){rows.replaceChildren();for(const pair of document.pairs)row(pair);dirty=false;}
async function load(){
 if(busy)return;lock(true);text(outcome,'Loading DVOL settings…');
 try{
  const response=await fetch(form.action,{cache:'no-store',headers:{Accept:'application/json'},signal:AbortSignal.timeout(12000)});
  const body=await response.json();if(!response.ok)throw new Error(body.error||'DVOL settings unavailable.');
  revision=body.revision;available=body.available_pairs||[];render(body.settings);
  const suggestions=document.getElementById('dvol-pair-suggestions');suggestions.replaceChildren();for(const pair of available){const option=document.createElement('option');option.value=pair;suggestions.append(option);}
  text(availability,body.availability_error||('Available Deribit pairs: '+(available.join(', ')||'none')) ,!!body.availability_error);
  text(outcome,'DVOL settings loaded.');
 }catch(error){revision=null;text(outcome,error.message||'DVOL settings unavailable.',true);}
 finally{lock(false);form.querySelector('[type=submit]').disabled=revision===null;add.disabled=revision===null;}
}
add.addEventListener('click',()=>{if(busy)return;const used=new Set(Array.from(rows.querySelectorAll('[data-dvol-field=symbol]'),input=>input.value.trim().toUpperCase()));row({symbol:available.find(pair=>!used.has(pair))||'',enabled:true});dirty=true;});
discard.addEventListener('click',load);
form.addEventListener('input',()=>{dirty=true;});form.addEventListener('change',()=>{dirty=true;});
form.addEventListener('submit',async event=>{
 event.preventDefault();if(busy||revision===null)return;
 const settings={pairs:Array.from(rows.children,wrapper=>({symbol:wrapper.querySelector('[data-dvol-field=symbol]').value,enabled:wrapper.querySelector('[data-dvol-field=enabled]').value==='true'}))};
 lock(true);text(outcome,'Saving DVOL settings…');
 try{
  const response=await fetch(form.action,{method:'POST',headers:{'Content-Type':'application/json',Accept:'application/json'},body:JSON.stringify({settings,revision,csrf_token:form.elements.csrf_token.value}),signal:AbortSignal.timeout(20000)});
  const body=await response.json();if(!response.ok)throw new Error(body.error||'DVOL settings could not be saved.');
  revision=body.revision;render(body.settings);text(outcome,body.message);
 }catch(error){text(outcome,error.message||'DVOL settings could not be saved.',true);}
 finally{lock(false);}
});
window.addEventListener('beforeunload',event=>{if(dirty){event.preventDefault();event.returnValue='';}});
load();
})();
