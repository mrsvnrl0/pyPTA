(() => {
'use strict';
const canvas=document.getElementById('pine-canvas'); if(!canvas)return;
const ctx=canvas.getContext('2d'), asset=document.getElementById('pine-asset');
const byId=id=>document.getElementById(id), number=v=>Number.isFinite(Number(v))&&v!==null&&v!==undefined?Number(v):null;
const quoteCurrency=()=>data?.symbol?.split('/')?.at(-1)||'USDC';
const money=v=>number(v)===null?'Pending':Number(v).toLocaleString(undefined,{maximumFractionDigits:6})+' '+quoteCurrency();
const stamp=v=>number(v)===null?'—':new Date(Number(v)).toISOString().replace('T',' ').slice(0,19)+' UTC';
let data=null,count=100,offset=0,busy=false,token=0,drag=null,geom=null;
const text=(id,value)=>{byId(id).textContent=value;};
function cells(id,values){const node=byId(id);node.replaceChildren();for(const [label,value] of values){const div=document.createElement('div'),small=document.createElement('small'),strong=document.createElement('strong');small.textContent=label;strong.textContent=String(value??'Pending');div.append(small,strong);node.append(div);}}
function current(){return data&&data.structure?.status==='ready'&&data.expires_ms>Date.now();}
function planCurrent(){return current()&&data.suggestion&&data.guide?.status==='qualified'&&data.guide.expires_ms>Date.now();}
function renderInfo(){
 const s=data?.structure||{},k=s.perky||{},g=data?.guide||{},p=data?.suggestion;
 const signal=p?{kind:p.signal_kind,side:p.side,score:p.score,score_parts:p.quality?.score_parts}:k.signal;
 const selectedSide=byId('pine-reach-side').value;const reachSide=selectedSide==='auto'?(p?.side||(s.regime==='bearish'?'short':'long')):selectedSide;
 window.pyptaReachability?.render(byId('pine-reachability'),data?.reachability?.[reachSide],money,stamp);
 const source=data?.input_source||{};text('pine-source',source.instrument?`Candle input: ${source.venue} · ${source.instrument} · native ${source.quote_asset||quoteCurrency()} prices and base-asset volume`:'Waiting for candle-source metadata.');
 text('pine-status',current()?`${data.symbol} · ${data.interval_ms/60000}M · Perky ${k.regime||'neutral'} · candle closed ${stamp(s.asof_ms)}`:data?.error||s.error||'Waiting for current completed candles. Previous drawings are reference only.');
 cells('pine-quality',[['Perky regime',k.regime||'Waiting'],['Current signal',signal?.kind||'None'],['Confluence score',signal?`${signal.score} / 10`:'—'],['Required',`${k.settings?.min_score??4} / 10`]]);
 text('pine-quality-note',signal?`Score components: ${Object.entries(signal.score_parts||{}).filter(([,v])=>v).map(([name,v])=>`${name.replaceAll('_',' ')} +${v}`).join(' · ')||'none'}. Score is not a probability of profit.`:'Signals use completed candles. Score components are contextual measurements, not probabilities.');
 const zones=byId('pine-zones');zones.replaceChildren();for(const z of (s.zones||[]).filter(item=>item.kind==='OB').slice(-20).reverse()){const tr=document.createElement('tr');for(const value of [z.side,money(z.low)+' – '+money(z.high),z.kind,stamp(z.known_ms),z.status]){const td=document.createElement('td');td.textContent=String(value);tr.append(td);}zones.append(tr);}
 const usable=planCurrent();cells('pine-plan',[['Entry · live quote',usable?money(p.entry_price):'Not qualified'],['Signal reference',usable?money(p.signal_entry_price):'Pending'],['Suggested S/L',usable?money(p.stop):'Pending'],['TP1',usable?money(p.tp1):'Pending'],['TP2',usable?money(p.tp2):'Pending'],['TP3 · final',usable?money(p.tp3):'Pending']]);
 text('pine-plan-status',usable?`${p.method} · score ${p.score}/10. TP3 distance ${Number(p.exit_distance_pct||0).toFixed(2)}% (minimum ${p.minimum_exit_distance_pct||0}%). Review quantity and execution price before saving.`:'No current qualified Perky trade plan.');
 const add=byId('pine-add-position');add.hidden=!usable;if(usable){add.href='/positions?'+new URLSearchParams({plan_asset:asset.value,plan_id:p.id||p.key});}else add.removeAttribute('href');
 const blockers=byId('pine-blockers');blockers.replaceChildren();for(const b of g.blockers||[]){const li=document.createElement('li');li.textContent=b.message||String(b);blockers.append(li);}
 const events=byId('pine-events');events.replaceChildren();for(const e of (k.events||[]).slice(-15).reverse()){const li=document.createElement('li');li.textContent=`${e.kind} · ${e.side} · ${money(e.level)} · ${e.structure||'sweep'} · confirmed ${stamp(e.known_ms)}`;events.append(li);}
}
byId('pine-reach-side').addEventListener('change',renderInfo);
function draw(){
 const box=canvas.getBoundingClientRect(),w=box.width,h=box.height,dpr=window.devicePixelRatio||1;if(!w||!h)return;
 canvas.width=Math.round(w*dpr);canvas.height=Math.round(h*dpr);ctx.setTransform(dpr,0,0,dpr,0,0);ctx.fillStyle='#0f141b';ctx.fillRect(0,0,w,h);
 const bars=data?.candles||[];offset=Math.min(offset,Math.max(0,bars.length-10));const end=bars.length-offset,start=Math.max(0,end-count),shown=bars.slice(start,end);
 if(!shown.length){ctx.fillStyle='#9dafc3';ctx.fillText('Waiting for candles…',24,40);geom=null;return;}
 const pad={l:15,r:90,t:28,b:35},pw=w-pad.l-pad.r,ph=h-pad.t-pad.b,lo=Math.min(...shown.map(b=>b.l)),hi=Math.max(...shown.map(b=>b.h)),gap=(hi-lo||hi*.01)*.09,min=lo-gap,max=hi+gap;
 const x=i=>pad.l+(i-start+.5)*pw/shown.length,y=v=>pad.t+(max-v)/(max-min)*ph,interval=data.interval_ms,first=bars[0].t,ix=t=>(t-first)/interval;
 geom={start,end,x,y,pw,pad,shown};ctx.font='11px system-ui';ctx.strokeStyle='#263342';ctx.fillStyle='#9dafc3';
 for(let n=0;n<6;n++){const v=min+(max-min)*n/5,yp=y(v);ctx.beginPath();ctx.moveTo(pad.l,yp);ctx.lineTo(w-pad.r,yp);ctx.stroke();ctx.fillText(v.toLocaleString(undefined,{maximumFractionDigits:v>=100?2:8})+' '+quoteCurrency(),w-pad.r+7,yp+4);}
 ctx.save();ctx.beginPath();ctx.rect(pad.l,pad.t,pw,ph);ctx.clip();
 for(const z of data.structure?.zones||[]){const left=x(ix(z.created_ms??z.known_ms)),right=x(ix(z.mitigated_ms??shown[shown.length-1].t+interval));if(right<0||left>w)continue;const bull=['bullish','long'].includes(z.side),color=z.status==='mitigated'?'#7c8593':z.status==='tested'?'#fa925b':bull?'#59d9bb':'#f7b64c';ctx.globalAlpha=z.status==='mitigated'?.09:.10+.025*(z.live_score??z.score??0);ctx.fillStyle=color;ctx.fillRect(left,y(z.high),right-left,y(z.low)-y(z.high));ctx.globalAlpha=1;ctx.strokeStyle=color;ctx.setLineDash(z.status==='mitigated'?[2,4]:[]);ctx.lineWidth=1;ctx.strokeRect(left,y(z.high),right-left,y(z.low)-y(z.high));ctx.setLineDash([]);}
 for(const e of data.structure?.perky?.events||[]){const t=e.time_ms??e.known_ms-interval+1,j=ix(t);if(j<start||j>=end||number(e.level)===null)continue;ctx.strokeStyle=['bullish','long'].includes(e.side)?'#59d9bb':'#f7b64c';ctx.fillStyle=ctx.strokeStyle;ctx.lineWidth=1;if(e.kind==='SWEEP'){ctx.beginPath();ctx.moveTo(x(j),y(e.level)-5);ctx.lineTo(x(j)+5,y(e.level));ctx.lineTo(x(j),y(e.level)+5);ctx.lineTo(x(j)-5,y(e.level));ctx.closePath();ctx.fill();}else{ctx.setLineDash(e.kind==='CHoCH'?[5,4]:[]);ctx.beginPath();ctx.moveTo(x(ix(e.pivot_ms??t)),y(e.level));ctx.lineTo(x(j),y(e.level));ctx.stroke();ctx.setLineDash([]);ctx.fillText(e.kind,x(j)+3,y(e.level)-5);}}
 ctx.lineWidth=1;const bw=Math.max(1,Math.min(12,pw/shown.length*.65));for(let i=start;i<end;i++){const b=bars[i];ctx.fillStyle=ctx.strokeStyle=b.c>=b.o?'#59d9bb':'#f7b64c';ctx.beginPath();ctx.moveTo(x(i),y(b.h));ctx.lineTo(x(i),y(b.l));ctx.stroke();ctx.fillRect(x(i)-bw/2,y(Math.max(b.o,b.c)),bw,Math.max(1,Math.abs(y(b.o)-y(b.c))));}
 const overlays=[];
 if(planCurrent()){const p=data.suggestion;overlays.push(['Entry',p.entry_price,'#e1e9f0'],['S/L',p.stop,'#ff7e85'],['TP1',p.tp1,'#7edcc2'],['TP2',p.tp2,'#b7a0fb'],['TP3',p.tp3,'#d8b25f']);}
 for(const [label,level,color] of overlays){if(number(level)===null)continue;ctx.strokeStyle=ctx.fillStyle=color;ctx.setLineDash([6,4]);ctx.beginPath();ctx.moveTo(pad.l,y(level));ctx.lineTo(w-pad.r,y(level));ctx.stroke();ctx.setLineDash([]);ctx.fillText(label+' '+money(level),pad.l+6,y(level)-5);}
 ctx.restore();ctx.fillStyle='#9dafc3';const ticks=Math.min(4,Math.max(2,Math.floor(pw/140)));for(let n=0;n<ticks;n++){const i=start+Math.floor((shown.length-1)*n/(ticks-1));ctx.textAlign=n===0?'left':n===ticks-1?'right':'center';ctx.fillText(new Date(bars[i].t).toISOString().slice(5,16).replace('T',' '),n===0?pad.l:n===ticks-1?w-pad.r:x(i),h-12);}ctx.textAlign='left';
 if(!current()){ctx.fillStyle='#edb966';ctx.fillText('REFERENCE · waiting for current data',20,18);}
}
async function refresh(){if(busy||document.hidden)return;busy=true;const requested=asset.value,revision=++token;const controller=new AbortController(),timeout=setTimeout(()=>controller.abort(),12000);try{const response=await fetch('/api/smc-chart/'+encodeURIComponent(requested),{cache:'no-store',signal:controller.signal});const body=await response.json();if(revision!==token||requested!==asset.value)return;data=body;renderInfo();draw();}catch(error){if(revision===token){if(data)data.expires_ms=0;renderInfo();text('pine-status','SMC data unavailable. Previous drawings are reference only.');draw();}}finally{clearTimeout(timeout);busy=false;}}
function zoom(delta){count=Math.max(20,Math.min(600,Math.round(count*delta)));draw();}
byId('pine-reset').addEventListener('click',()=>{offset=0;count=100;draw();});byId('pine-zoom-in').addEventListener('click',()=>zoom(.8));byId('pine-zoom-out').addEventListener('click',()=>zoom(1.25));
asset.addEventListener('change',()=>{token++;data=null;offset=0;renderInfo();draw();refresh();});
canvas.addEventListener('wheel',e=>{e.preventDefault();zoom(e.deltaY<0?.85:1.15);},{passive:false});
canvas.addEventListener('pointerdown',e=>{drag={x:e.clientX,offset};canvas.setPointerCapture(e.pointerId);});canvas.addEventListener('pointerup',()=>{drag=null;});canvas.addEventListener('pointercancel',()=>{drag=null;});
canvas.addEventListener('pointermove',e=>{if(!geom)return;if(drag){offset=Math.max(0,drag.offset+Math.round((e.clientX-drag.x)*count/geom.pw));draw();return;}const x=e.clientX-canvas.getBoundingClientRect().left,i=Math.floor((x-geom.pad.l)/geom.pw*geom.shown.length),b=geom.shown[i];if(b)text('pine-hover',`${stamp(b.t)} · O ${money(b.o)} · H ${money(b.h)} · L ${money(b.l)} · C ${money(b.c)} · Volume ${b.v}`);});
canvas.addEventListener('keydown',e=>{if(['ArrowLeft','ArrowRight','Home','+','-','='].includes(e.key)){e.preventDefault();if(e.key==='ArrowLeft')offset+=10;if(e.key==='ArrowRight')offset=Math.max(0,offset-10);if(e.key==='Home')offset=0;if(['+','='].includes(e.key))zoom(.8);if(e.key==='-')zoom(1.25);draw();}});
window.addEventListener('resize',draw);document.addEventListener('visibilitychange',()=>{if(!document.hidden)refresh();});
setInterval(()=>{if(!document.hidden)refresh();},15000);setInterval(()=>{if(data&&!document.hidden){renderInfo();draw();}},1000);refresh();
})();
