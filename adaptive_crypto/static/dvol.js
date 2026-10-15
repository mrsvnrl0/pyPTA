(() => {
'use strict';
const canvas=document.getElementById('dvol-canvas'); if(!canvas)return;
const ctx=canvas.getContext('2d');
const asset=document.getElementById('dvol-asset');
const period=document.getElementById('dvol-period');
const byId=id=>document.getElementById(id);
const number=v=>Number.isFinite(Number(v))&&v!==null&&v!==undefined?Number(v):null;
const pct=v=>number(v)===null?'—':Number(v).toFixed(2)+'%';
const money=v=>number(v)===null?'—':Number(v).toLocaleString(undefined,{maximumFractionDigits:2})+' '+(data?.symbol?.split('/')[1]||'');
const stamp=v=>number(v)===null?'—':new Date(Number(v)).toISOString().replace('T',' ').slice(0,19)+' UTC';
const text=(id,value)=>{const node=byId(id);if(node)node.textContent=value;};
let data=null,busy=false,token=0;

function freshnessLabel(){
 if(!asset.value)return 'No enabled DVOL pairs';
 if(!data)return 'Loading…';
 if(data.supported===false)return 'Unsupported asset';
 if(data.status==='error')return 'Unavailable';
 if(data.stale)return 'Stale (last known)';
 if(data.fresh)return 'Fresh';
 if(data.status==='empty')return 'No data';
 return '—';
}

function freshnessClass(){
 if(!data)return '';
 if(data.supported===false)return 'unsupported';
 if(data.status==='error')return 'error';
 if(data.stale)return 'stale';
 return 'fresh';
}

function renderSummary(){
 if(!data){text('dvol-latest','—');text('dvol-price','—');text('dvol-freshness','—');text('dvol-status',asset.value?'Loading DVOL…':'No enabled DVOL pairs');return;}
 const latest=data.latest;
 text('dvol-latest',latest?pct(latest.c)+(latest.forming?' (forming)':''):'—');
 text('dvol-price',money(data.price));
 const fresh=byId('dvol-freshness');fresh.textContent=freshnessLabel();fresh.className='dvol-freshness '+freshnessClass();
 const move=data.expected_move||{};
 text('dvol-move-1d',money(move['1d']));
 text('dvol-move-7d',money(move['7d']));
 text('dvol-move-30d',money(move['30d']));
 text('dvol-move-error',data.expected_move_error||'');
 const status=byId('dvol-status');status.textContent=freshnessLabel();status.className='dvol-freshness '+freshnessClass();
 if(data.supported===false){text('dvol-hover',data.error||'DVOL is not published for this asset.');}
 else if(data.status==='error'){text('dvol-hover',data.error||'DVOL is unavailable.');}
 else if(data.status==='empty'){text('dvol-hover','No DVOL data for this period and resolution.');}
 else{text('dvol-hover','Hover over the chart to inspect a reading. The latest interval may still be forming.');}
}

function renderTable(){
 const rows=byId('dvol-rows');rows.replaceChildren();
 const series=data&&Array.isArray(data.series)?data.series:null;
 if(!series||!series.length)return;
 for(const point of series.slice(-30).reverse()){
  const tr=document.createElement('tr');
  const values=[stamp(point.t),pct(point.o),pct(point.h),pct(point.l),pct(point.c)];
  values.forEach((value,index)=>{const td=document.createElement('td');td.textContent=value;if(index===4&&point.forming!==false&&data.latest&&point.t===data.latest.t&&data.latest.forming)td.className='forming';tr.append(td);});
  rows.append(tr);
 }
}

function draw(){
 const box=canvas.getBoundingClientRect(),w=box.width,h=box.height,dpr=window.devicePixelRatio||1;if(!w||!h)return;
 canvas.width=Math.round(w*dpr);canvas.height=Math.round(h*dpr);ctx.setTransform(dpr,0,0,dpr,0,0);ctx.fillStyle='#0f141b';ctx.fillRect(0,0,w,h);
 const series=data&&data.supported&&data.status==='ok'&&Array.isArray(data.series)?data.series:[];
 if(!series.length){
  ctx.fillStyle='#9dafc3';ctx.font='13px system-ui';
  const message=!asset.value?'Add an enabled Deribit pair in DVOL Settings.':data?(data.supported===false?'DVOL is not published for this asset.':data.status==='error'?'DVOL unavailable.':'No DVOL data for this period.'):'Loading DVOL…';
  ctx.fillText(message,24,40);return;
 }
 const pad={l:46,r:18,t:20,b:30},pw=w-pad.l-pad.r,ph=h-pad.t-pad.b;
 const lo=Math.min(...series.map(p=>p.l)),hi=Math.max(...series.map(p=>p.h)),gap=(hi-lo||hi*.01)*.08,min=lo-gap,max=hi+gap;
 const x=i=>pad.l+(series.length===1?pw/2:i/(series.length-1)*pw),y=v=>pad.t+(max-v)/(max-min)*ph;
 ctx.font='11px system-ui';ctx.strokeStyle='#263342';ctx.fillStyle='#9dafc3';
 for(let n=0;n<5;n++){const v=min+(max-min)*n/4,yp=y(v);ctx.beginPath();ctx.moveTo(pad.l,yp);ctx.lineTo(w-pad.r,yp);ctx.stroke();ctx.fillText(v.toFixed(1)+'%',6,yp+4);}
 ctx.save();ctx.beginPath();ctx.rect(pad.l,pad.t,pw,ph);ctx.clip();
 ctx.fillStyle='rgba(128,216,178,.08)';ctx.beginPath();ctx.moveTo(x(0),y(series[0].h));for(let i=0;i<series.length;i++)ctx.lineTo(x(i),y(series[i].h));for(let i=series.length-1;i>=0;i--)ctx.lineTo(x(i),y(series[i].l));ctx.closePath();ctx.fill();
 ctx.strokeStyle='#80d8b2';ctx.lineWidth=2;ctx.beginPath();series.forEach((p,i)=>i?ctx.lineTo(x(i),y(p.c)):ctx.moveTo(x(i),y(p.c)));ctx.stroke();
 const latest=series[series.length-1];ctx.strokeStyle=ctx.fillStyle='#80d8b2';ctx.setLineDash([4,3]);ctx.beginPath();ctx.moveTo(pad.l,y(latest.c));ctx.lineTo(w-pad.r,y(latest.c));ctx.stroke();ctx.setLineDash([]);ctx.fillText('Latest '+pct(latest.c),w-pad.r-110,y(latest.c)-5);
 ctx.restore();ctx.fillStyle='#9dafc3';const ticks=Math.min(4,Math.max(2,Math.floor(pw/150)));for(let n=0;n<ticks;n++){const i=Math.floor((series.length-1)*n/(ticks-1));ctx.textAlign=n===0?'left':n===ticks-1?'right':'center';ctx.fillText(new Date(series[i].t).toISOString().slice(5,16).replace('T',' '),n===0?pad.l:n===ticks-1?w-pad.r:x(i),h-10);}ctx.textAlign='left';
}

async function refresh(){
 if(busy||!asset.value)return;busy=true;const requestedAsset=asset.value,requestedPeriod=period.value,revision=++token;
 const controller=new AbortController(),timeout=setTimeout(()=>controller.abort(),12000);
 try{
  const response=await fetch('/api/dvol?pair='+encodeURIComponent(requestedAsset)+'&period='+encodeURIComponent(requestedPeriod),{cache:'no-store',signal:controller.signal});
  const body=await response.json();
  if(!response.ok)throw new Error(body.error||"DVOL unavailable");
  if(revision!==token||requestedAsset!==asset.value||requestedPeriod!==period.value)return;
  data=body;renderSummary();renderTable();draw();
 }catch(error){
  if(revision===token){data={...(data||{supported:true,series:[]}),status:'error',fresh:false,stale:!!data?.series?.length,error:'DVOL data unavailable.',expected_move:{},expected_move_error:'Waiting for fresh DVOL and index data.'};renderSummary();renderTable();draw();}
 }finally{clearTimeout(timeout);busy=false;if(revision!==token)queueMicrotask(refresh);}
}

asset.addEventListener('change',()=>{token++;data=null;renderSummary();renderTable();draw();refresh();});
period.addEventListener('change',()=>{token++;data=null;renderSummary();renderTable();draw();refresh();});
canvas.addEventListener('pointermove',e=>{
 if(!data||!Array.isArray(data.series)||!data.series.length)return;
 const box=canvas.getBoundingClientRect(),w=box.width;if(!w)return;
 const pad={l:46,r:18};const pw=w-pad.l-pad.r;const series=data.series;
 const rel=(e.clientX-box.left-pad.l)/pw;
 if(rel<0||rel>1)return;
 const i=Math.round(rel*(series.length-1));const p=series[Math.max(0,Math.min(series.length-1,i))];
 text('dvol-hover',`${stamp(p.t)} · Open ${pct(p.o)} · High ${pct(p.h)} · Low ${pct(p.l)} · Close ${pct(p.c)}`);
});
window.addEventListener('resize',draw);
document.addEventListener('visibilitychange',()=>{if(!document.hidden)refresh();});
setInterval(()=>{if(!document.hidden)refresh();},30000);
renderSummary();renderTable();draw();refresh();
})();
