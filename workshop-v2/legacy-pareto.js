(() => {
 'use strict';
 const host=document.getElementById('legacy-pareto'),picker=document.getElementById('chart-dataset'),data=window.WORKSHOP_LEGACY_PARETO;
 if(!host||!picker||!data)return;
 const link=document.createElement('link');link.rel='stylesheet';link.href='legacy-pareto.css';document.head.append(link);
 const esc=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
 const fmt=(v,n=3)=>Number(v).toFixed(n).replace('.',',');
 const titles=['Исходный ReAct','Plan → execute','Индекс и описания','Журнал требований','Domain reviewer','Routing и бюджет','Журнал от варианта 5','Prompt от варианта 5'];
 let mode='tasks',selected=7;
 function front(points,x,y,minX){return points.filter(p=>!points.some(q=>q!==p&&(minX?q[x]<=p[x]:q[x]>=p[x])&&q[y]>=p[y]&&((minX?q[x]<p[x]:q[x]>p[x])||q[y]>p[y])));}
 function draw(){
  const pts=data.points,xKey=mode==='tasks'?'report_score':'avg_llm_calls',yKey=mode==='tasks'?'sql_score':'score',minX=mode!=='tasks',pf=front(pts,xKey,yKey,minX);
  const W=720,H=390,L=64,R=34,T=40,B=54;
  let lo=Math.min(...pts.map(p=>p[xKey])),hi=Math.max(...pts.map(p=>p[xKey])),span=hi-lo||1;lo=Math.max(0,lo-span*.16);hi=Math.min(minX?Infinity:1,hi+span*.16);
  let ylo=mode==='tasks'?0:.4,yhi=1;
  const x=v=>L+(v-lo)/(hi-lo)*(W-L-R),y=v=>H-B-(v-ylo)/(yhi-ylo)*(H-T-B);
  let svg=`<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${mode==='tasks'?'Оценка отчётов и SQL':'Общая оценка и вызовы модели'}"><text x="${L}" y="17" fill="#a8adba" font-size="12">${mode==='tasks'?'оценка SQL':'общая оценка'} ↑</text><text x="${W-R}" y="17" fill="#c8ff4d" text-anchor="end" font-size="12">${minX?'лучше ↖':'лучше ↗'}</text>`;
  for(let i=0;i<=4;i++){const v=ylo+(yhi-ylo)*i/4;svg+=`<line x1="${L}" x2="${W-R}" y1="${y(v)}" y2="${y(v)}" stroke="#303746"/><text x="${L-10}" y="${y(v)+4}" fill="#a8adba" text-anchor="end" font-size="12">${fmt(v,2)}</text>`;}
  for(let i=0;i<=4;i++){const v=lo+(hi-lo)*i/4;svg+=`<text x="${x(v)}" y="${H-31}" fill="#a8adba" text-anchor="middle" font-size="12">${fmt(v,minX?1:2)}</text>`;}
  svg+=`<text x="${(W+L-R)/2}" y="${H-8}" fill="#a8adba" text-anchor="middle" font-size="12">${minX?'LLM-вызовы на задачу':'оценка отчётов'} →</text><polyline points="${pf.slice().sort((a,b)=>a[xKey]-b[xKey]).map(p=>`${x(p[xKey])},${y(p[yKey])}`).join(' ')}" fill="none" stroke="#c8ff4d" stroke-width="2" stroke-dasharray="6 5"/>`;
  const occupied=[],truePoints=pts.map(p=>[x(p[xKey]),y(p[yKey])]);
  pts.forEach(p=>{const xx=x(p[xKey]),yy=y(p[yKey]);let px=xx+24,py=yy-22;
   for(let n=0;n<30&&(occupied.some(o=>Math.hypot(o[0]-px,o[1]-py)<35)||truePoints.some(o=>Math.hypot(o[0]-px,o[1]-py)<24));n++){let a=n*2.399;px=Math.max(L+15,Math.min(W-R-15,xx+Math.cos(a)*(35+Math.floor(n/5)*22)));py=Math.max(T+16,Math.min(H-B-14,yy+Math.sin(a)*(35+Math.floor(n/5)*22)));}
   occupied.push([px,py]);const color=p.number===selected?'#86a8ff':pf.includes(p)?'#c8ff4d':'#a8adba';
   svg+=`<line x1="${xx}" y1="${yy}" x2="${px}" y2="${py}" stroke="${color}" stroke-width="1.5" opacity=".8"/><circle cx="${xx}" cy="${yy}" r="5" fill="${color}"/><circle cx="${xx}" cy="${yy}" r="1.5" fill="#0d1018"/>`;
   svg+=`<g data-legacy-point="${p.number}" role="button" tabindex="0" aria-label="${p.number}: ${esc(titles[p.number])}"><circle cx="${px}" cy="${py}" r="${p.number===selected?16:13}" fill="#171b27" stroke="${color}" stroke-width="${p.number===selected?3:1.5}"/><text x="${px}" y="${py}" text-anchor="middle" dominant-baseline="central" fill="${color}" font-size="11">${p.number}</text></g>`;
  });svg+='</svg>';
  const p=pts.find(p=>p.number===selected),cfgChanges=Object.entries(p.config).filter(([k,v])=>k!=='name'&&JSON.stringify(v)!==JSON.stringify(pts[0].config[k]));
  host.innerHTML=`<div class="legacy-controls"><button data-legacy-mode="tasks" aria-pressed="${mode==='tasks'}">Качество отчётов / SQL</button><button data-legacy-mode="cost" aria-pressed="${mode==='cost'}">Общая оценка / вызовы</button></div><div class="arena-layout"><div>${svg}<div class="scatter-legend">${pts.map(p=>`<button data-legacy-point="${p.number}" aria-pressed="${p.number===selected}"><b>${p.number}</b><span>${esc(titles[p.number])}</span><small>${fmt(p[xKey],minX?1:3)} / ${fmt(p[yKey])}</small></button>`).join('')}</div></div><div class="legacy-selection"><p class="eyebrow">ВАРИАНТ ${p.number} · ${pf.includes(p)?'НА ПАРЕТО-ФРОНТЕ':'ДОМИНИРУЕТСЯ'}</p><h3>${esc(titles[p.number])}</h3><div class="legacy-metrics"><b>${fmt(p.report_score)}<small>отчёты</small></b><b>${fmt(p.sql_score)}<small>SQL</small></b><b>${fmt(p.avg_llm_calls,1)}<small>LLM-вызовы</small></b></div><p>Родитель: ${p.parent_number===null?'—':p.parent_number}. Оператор: <code>${esc(p.operator)}</code>.</p><details><summary>Изменения от исходного ReAct</summary>${cfgChanges.map(([k,v])=>`<p><code>${esc(k)}</code></p><pre>${esc(typeof v==='string'?v:JSON.stringify(v,null,2))}</pre>`).join('')||'<p>Исходная конфигурация.</p>'}</details><details><summary>Гипотеза и источник измерений</summary><p>${esc(p.hypothesis)}</p><code>${esc(data.source)}</code><p>SHA-256: <code>${data.sha256}</code></p><p>Это train, на котором искали настройки. Отдельную проверку новых задач нельзя заменить этим графиком.</p></details></div></div>`;
  host.querySelectorAll('[data-legacy-mode]').forEach(b=>b.onclick=()=>{mode=b.dataset.legacyMode;draw();});
  host.querySelectorAll('[data-legacy-point]').forEach(b=>{b.onclick=()=>{selected=Number(b.dataset.legacyPoint);draw();};b.onkeydown=e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();e.stopPropagation();selected=Number(b.dataset.legacyPoint);draw();}};});
  host.dataset.front=pf.map(p=>p.number).join(',');host.dataset.mode=mode;
 }
 function toggle(){const legacy=picker.value==='legacy';host.hidden=!legacy;['etl-pareto','etl-split-control','etl-cost-control'].forEach(id=>{const el=document.getElementById(id);if(el)el.hidden=legacy;});const caption=document.getElementById('arena-caption');if(caption)caption.hidden=legacy;if(legacy)draw();}
 picker.addEventListener('change',toggle);toggle();
 window.WorkshopLegacyPareto={front:(mode='tasks')=>front(data.points,mode==='tasks'?'report_score':'avg_llm_calls',mode==='tasks'?'sql_score':'score',mode!=='tasks').map(p=>p.number)};
})();
