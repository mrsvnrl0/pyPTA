// Shared, escaped DOM rendering and expiration for structural assessments.
(() => {
'use strict';
const renders=new WeakMap();
function expire(){for(const node of document.querySelectorAll('[data-reachability-expires]')){if(Number(node.dataset.reachabilityExpires)>Date.now())continue;const label=node.querySelector('[data-reachability-label]');if(label)label.textContent='Assessment unavailable';const wait=node.querySelector('[data-reachability-wait]');if(wait){wait.hidden=false;if(!wait.textContent.trim())wait.textContent='Waiting for current evidence and a fresh price.';}const evidence=node.querySelector('.reachability-evidence');if(evidence)evidence.hidden=true;}}
function el(tag,text){const node=document.createElement(tag);if(text!==undefined)node.textContent=text;return node;}
function render(container,r,money,stamp){
 const signature=JSON.stringify(r);if(renders.has(container)&&renders.get(container)===signature){expire();return;}renders.set(container,signature);const previous=container.querySelector('details'),opened=previous?.open||false,focused=previous?.querySelector('summary')===document.activeElement;
 container.replaceChildren();if(!r){container.append(el('p','Waiting for a current target assessment.'));return;}if(r.mode==='off'){container.append(el('p','Reachability is off. Enable it in Settings.'));return;}
 const details=el('details');details.open=opened;details.className='target-reachability';details.dataset.reachabilityExpires=String(r.expires_ms||0);details.dataset.reachabilityMode=r.mode;
 const summary=el('summary'),strong=el('strong',`Target reachability (${r.mode})`),label=el('span',r.label||'Assessment unavailable');label.dataset.reachabilityLabel='';summary.append(strong,document.createTextNode(' · '),label);details.append(summary);
 details.append(el('p',`${r.scope||'Current target assessment'} · ${money(r.target)}. ${r.mode==='advisory'?'Advisory only; qualification and saved targets are unchanged. ':''}Structural evidence, without a calibrated target-hit probability.`));
 const wait=el('p',r.error||'Waiting for current evidence and a fresh price.');wait.dataset.reachabilityWait='';wait.hidden=r.status!=='unavailable'&&r.expires_ms>Date.now();details.append(wait);
 const evidence=el('div');evidence.className='reachability-evidence';const range=r.dealing_range;if(range)evidence.append(el('p',`Confirmed range ${money(range.low)} → ${money(range.high)} · equilibrium ${money(range.equilibrium)}.`));
 evidence.append(el('p',`${r.timeframe_minutes||'—'}M bias: ${r.bias||'unavailable'} · ${r.cycle||'Unconfirmed'} · target ${r.target_type||'unknown'}.`));
 const internal=r.draw?.internal;if(internal)evidence.append(el('p',`Nearest internal draw: ${internal.kind} ${money(internal.low)} → ${money(internal.high)}.`));
 const list=el('ul');for(const c of r.checks||[]){const item=el('li');item.className=c.status==='pass'?'pass':'wait';item.append(el('strong',`${c.label} · ${c.status}: `),document.createTextNode(c.detail+(c.required?'':' (Context only)')));list.append(item);}evidence.append(list);
 evidence.append(el('small',`Assessed ${stamp(r.observed_ms)} · higher-timeframe candle ${stamp(r.bias_asof_ms)}. Balance and ATR describe observed movement, not a deadline or guarantee.`));details.append(evidence);container.append(details);if(focused)summary.focus({preventScroll:true});expire();
}
window.pyptaReachability={render,expire};setInterval(expire,1000);expire();
})();
