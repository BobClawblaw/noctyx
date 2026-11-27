/* Snapping grid dashboard (operator, 2026-09-26: panels must never overlap).
   Each panel occupies a column-span on a 12-col grid; dragging reorders the
   grid, resizing changes the span (snap) or the height (free px). NO
   meta-refresh — each panel polls its own endpoint on its own clock and
   repaints only its own DOM node. */
const VIEW=document.body.dataset.view||'main';
const LS='cm_layout_v4'+(VIEW=='main'?'':'_'+VIEW+'_v2')   /* bump when the panel set changes: a layout saved by an older page must not be applied */, LS_SEL='cm_sel_v1', LS_TOK='cm_token_v1';
let SEL=localStorage.getItem(LS_SEL)||null, OVR=null;
const LS_TAB='cm_dtab_v1',DTABS=['overview','llm','network','storage','gpu','health','backups'];
const BUILD=(window.NX_CFG||{}).build||'__BUILD__';                              /* server build stamp; see buildCheck() */
let DTAB=DTABS.includes(localStorage.getItem(LS_TAB))?localStorage.getItem(LS_TAB):'overview';
/* snapshot browser state: host, snapshot id, path; loaded on demand (int:0) */
const BR={host:null,snap:null,path:'/',snaps:[],data:null,find:null,busy:false};
async function browseTo(host,snap,path){
 BR.host=host;BR.snap=snap;BR.path=path||'/';BR.find=null;BR.busy=true;BR.data=null;
 repaint('browse',BR);
 try{
  if(!BR.snaps.length||BR.snaps[0]._host!==host){const r=await fetchJSON('/api/v1/snapshots/'+encodeURIComponent(host));
   BR.snaps=(r.snapshots||[]).map(x=>Object.assign({_host:host},x))}
  if(!BR.snap&&BR.snaps.length)BR.snap=BR.snaps[0].short_id;
  BR.data=await fetchJSON(`/api/v1/browse/${encodeURIComponent(host)}/${BR.snap}?path=${encodeURIComponent(BR.path)}`);
 }catch(e){BR.data={error:String(e.message||e)}}
 BR.busy=false;repaint('browse',BR);
 const el=$('p-browse');if(el&&el.classList.contains('collapsed'))toggleP('browse')}
async function browseFind(){const q=($('bq').value||'').trim();if(!q||!BR.host)return;
 BR.busy=true;BR.find={q};repaint('browse',BR);
 try{BR.find=Object.assign({q},await fetchJSON(`/api/v1/find/${encodeURIComponent(BR.host)}?q=${encodeURIComponent(q)}`))}
 catch(e){BR.find={q,error:String(e.message||e)}}
 BR.busy=false;repaint('browse',BR)}
function browseRestore(path,to){
 const what=to=='host'?`a fresh directory under ${(window.NX_CFG||{}).nodeLib||'/var/lib/noctyx'}/restore/ on ${BR.host}`:`the staging area on ${(window.NX_CFG||{}).nas||'nas'} (backups/restores/${BR.host}/<time>/)`;
 if(!confirm(`Restore ${path} from snapshot ${BR.snap} into ${what}?\nNothing on the live system is overwritten.`))return;
 act('restore',{host:BR.host,snapshot:BR.snap,path,to})}
/* ── navigation. One rule: a coloured "look at me" is a link to the panel where you
   act on it and watch the result. goTo() expands/scrolls/flashes the panel, selects
   the host and tab, and remembers the host so that panel highlights its rows.
   URL form (#host=..&panel=..&tab=..) works from /cluster and from a pasted link;
   a bare #host still selects a host (old links). ── */
let FOCUS={};
function hashOf(t){const q=[];for(const k of ['host','panel','tab'])if(t&&t[k])q.push(k+'='+encodeURIComponent(t[k]));return q.length?'#'+q.join('&'):''}
function parseHash(h){h=(h||'').replace(/^#/,'');if(!h)return {};if(!h.includes('='))return {host:decodeURIComponent(h)};
 const t={};for(const kv of h.split('&')){const i=kv.indexOf('=');if(i>0)t[kv.slice(0,i)]=decodeURIComponent(kv.slice(i+1))}return t}
function goTo(t){t=t||{};
 if(VIEW!='main'){location.href='/'+hashOf(t);return}
 if(t.tab&&DTABS.includes(t.tab)&&t.tab!=DTAB){DTAB=t.tab;try{localStorage.setItem(LS_TAB,t.tab)}catch(e){}}
 if(t.host&&t.host!=SEL)selectHost(t.host);else if(t.host&&t.tab)setTab(DTAB);
 const id=t.panel||(t.host?'detail':null);if(!id)return;
 const el=$('p-'+id),p=PANELS.find(x=>x.id==id);if(!el||!p)return;
 FOCUS={panel:id,host:t.host||null};
 if(el.classList.contains('collapsed'))toggleP(id);
 if(id!='detail'&&p._data!==undefined)repaint(id,p._data);          /* row highlight for the host */
 el.scrollIntoView({behavior:'smooth',block:'start'});
 el.classList.remove('flash');void el.offsetWidth;el.classList.add('flash');setTimeout(()=>el.classList.remove('flash'),2600);
 const row=el.querySelector('tr.hl');if(row)setTimeout(()=>row.scrollIntoView({block:'nearest'}),400);
 history.replaceState(null,'',location.pathname+hashOf({host:SEL,panel:id,tab:id=='detail'?DTAB:null}))}
const goAttr=t=>`class=go onclick="event.stopPropagation();goTo(${esc(JSON.stringify(t))})"`;
const goA=(t,cls,txt,title)=>`<a class="tag ${cls} go" href="/${hashOf(t)}" onclick="event.stopPropagation();goTo(${esc(JSON.stringify(t))});return false" title="${esc(title||'open')}">${txt}</a>`;
const hlRow=(panel,host)=>FOCUS.panel==panel&&FOCUS.host&&FOCUS.host==host?' hl':'';
/* where each alert is acted on */
function alertTarget(a){const n=a.name||'',h=a.host||null;
 if(/^Backup(Never|Stale|Late)$/.test(n))return {host:h,panel:'detail',tab:'backups',what:'Machine Report › backups'};
 if(n=='BackupJobFailed'||n=='JobDied')return {host:h,panel:'runs',what:'Runs'};
 if(n=='TickTimerInactive')return {panel:'schedule',what:'Schedule'};
 if(/^Nas|^RepoUnreadable/.test(n))return {panel:'storage',what:'Storage'};
 if(n=='RestEndpointDown'||n=='NoctyxMetricsDown')return {panel:'actions',what:'Console'};
 if(/^Host(HygieneWarning|SystemdUnitFailed|RootFilesystemFull)$/.test(n))return {host:h,panel:'hygiene',what:'Hygiene'};
 if(n=='NodeExporterDown')return {host:h,panel:'detail',tab:'health',what:'Machine Report › health'};
 if(/^LLM|Tensorfold|Mtplx/.test(n))return {host:h,panel:'detail',tab:'llm',what:'Machine Report › llm'};
 return h?{host:h,panel:'detail',tab:'overview',what:'Machine Report'}:{panel:'alerts',what:''}}
function setTab(t){DTAB=t;try{localStorage.setItem(LS_TAB,t)}catch(e){}
 const p=PANELS.find(x=>x.id=='detail');if(p._data)p._data._m={loading:true};repaint('detail',p._data);tick(p)}
const $=id=>document.getElementById(id);
const esc=t=>String(t??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
/* temperature unit: a display setting (Display settings › Units), °F unless changed. Data and alert
   thresholds stay in °C; only what is DISPLAYED converts. */
let TU=NXT.eff().tunit=='C'?'C':'F';
const tval=c=>TU=='F'?c*9/5+32:c;
const fmtT=(c,short)=>c==null||isNaN(c)?'—':Math.round(tval(+c))+'°'+(short?'':TU);
function setTU(u){TU=u;NXT.save({tunit:u});pushAccount();setRender();
 PANELS.forEach(p=>{if(p._data!==undefined&&p.id!='config')repaint(p.id,p._data)})}
const clamp=(v,a,b)=>Math.max(a,Math.min(b,v));
/* the subsystem a meter shows (data-k): themes may colour by it (Phosphor does); others ignore it */
function kOf(label){const l=String(label||'').toLowerCase().replace(/<[^>]+>/g,'');
 return /temp/.test(l)?'temp':/gpu/.test(l)?'gpu':/cpu|load/.test(l)?'cpu':/mem/.test(l)?'mem':/disk|root|stor/.test(l)?'disk':
  /net|fabric/.test(l)?'net':/power|watt|energy/.test(l)?'power':/tok|llm|request|generat|model/.test(l)?'llm':/health|backup/.test(l)?'ok':''}
/* LCD numerals draw an unlit "8" behind every digit, as a real segment display does */
const ghost=v=>String(v??'').replace(/[0-9]/g,'8').replace(/[^8:.\s/%-]/g,' ');
function bar(pct,neutral,k){const p=clamp(parseInt(pct)||0,0,100);
 return `<div class="bar ${neutral?'n':p>90?'b':p>75?'w':''}" data-k="${k||''}" style="--p:${Math.max(p,1)}"><i style="width:${p}%"></i></div>`}
const ageD=t=>t?(Date.now()-new Date(t))/864e5:null;
/* ALL times shown in UTC (the header clock is UTC): clients run mixed zones,
   and a bare 'HH:MM' that is sometimes Chicago and sometimes UTC is a lie */
function toEpoch(t){if(t==null||t==='')return null;if(typeof t=='number')return t;
 const s=String(t);return new Date(/[zZ]|[+-]\d\d:?\d\d$/.test(s)?s:s+'Z').getTime()/1000}
function fmtTs(t){const e=toEpoch(t);if(e==null||isNaN(e))return '—';
 return new Date(e*1000).toISOString().slice(5,16).replace('T',' ')+'Z'}
function agoS(t){const e=toEpoch(t);if(!e)return 'never';const s=Date.now()/1000-e;
 return s<0?'in '+fmtDur(-s):fmtDur(s)+' ago'}
function inS(t){const e=toEpoch(t);if(!e)return '—';const s=e-Date.now()/1000;
 return s<=60?'now':'in '+fmtDur(s)}
const HCLS={ok:'ok',late:'warn',stale:'bad',never:'bad'};
const pct=v=>v==null?'—':Math.round(v)+'%';
function fmtBits(b){if(b==null)return '—';return b>=1e9?(b/1e9).toFixed(b>=1e10?0:1)+' Gb/s':b>=1e6?(b/1e6).toFixed(b>=1e8?0:1)+' Mb/s':(b/1e3).toFixed(0)+' kb/s'}
function fmtK(v){if(v==null)return '—';v=+v;return v>=1e9?(v/1e9).toFixed(2)+' G':v>=1e6?(v/1e6).toFixed(v>=1e7?0:1)+' M':v>=1e3?(v/1e3).toFixed(v>=1e5?0:1)+' k':String(Math.round(v))}
function fmtBytes(b){if(b==null)return '—';const u=['B','KB','MB','GB','TB','PB'];let i=0;
 while(Math.abs(b)>=1000&&i<u.length-1){b/=1000;i++}return (b>=100||i==0?b.toFixed(0):b.toFixed(1))+' '+u[i]}
/* ── charts ──────────────────────────────────────────────────────────
   spark(): tiny inline trend. chart(): full component — hairline grid with tick
   labels, 2px line, 10% wash (≤2 series), ringed end-dot, legend with line
   keys (identity on the key, text in ink), crosshair + one tooltip for every
   series on hover. Series: [{name, pts:[[t,v]…], cls}] */
/* curve(): an SVG path through [x,y] points as a monotone cubic (Fritsch–Carlson, as d3's
   curveMonotoneX): smooth bends, but it never overshoots a sample — a 0→100% step stays inside
   0..100 and a flat run stays flat, so the curve can't invent a peak or a dip the data lacks. */
function curve(P){const n=P.length;if(!n)return '';
 const f=v=>v.toFixed(1);let d=`M${f(P[0][0])} ${f(P[0][1])}`;
 if(n<3)return n<2?d:d+` L${f(P[1][0])} ${f(P[1][1])}`;
 const h=[],k=[];for(let i=0;i<n-1;i++){h[i]=(P[i+1][0]-P[i][0])||1e-6;k[i]=(P[i+1][1]-P[i][1])/h[i]}
 const m=[k[0]];
 for(let i=1;i<n-1;i++){const p=(k[i-1]*h[i]+k[i]*h[i-1])/(h[i-1]+h[i]);
  m[i]=k[i-1]*k[i]<=0?0:(Math.sign(k[i-1])+Math.sign(k[i]))*Math.min(Math.abs(k[i-1]),Math.abs(k[i]),.5*Math.abs(p))}
 m[n-1]=k[n-2];
 for(let i=0;i<n-1;i++){const t=h[i]/3,[x0,y0]=P[i],[x1,y1]=P[i+1];
  d+=` C${f(x0+t)} ${f(y0+t*m[i])} ${f(x1-t)} ${f(y1-t*m[i+1])} ${f(x1)} ${f(y1)}`}
 return d}
function _lines(series,W,H,max){
 const ts=series.flatMap(s=>(s.pts||[]).map(p=>p[0]));if(!ts.length)return '';
 // end-stamp the path: a sliding window of the same length maps to the SAME point string
 // (x comes from the normalized span), so without this morph() compares identical attributes
 // and never repaints the trace — it freezes on screen (user, 2026-10-22: graphs not updating)
 const wk=`${Math.min(...ts)}-${Math.max(...ts)}`;
 const t0=Math.min(...ts),t1=Math.max(...ts)||t0+1;
 return series.map(s=>{const d=(s.pts||[]).filter(p=>p[1]!=null).map(p=>
   [(p[0]-t0)/((t1-t0)||1)*W,H-2-Math.min(p[1],max)/(max||1)*(H-4)]);
 return d.length?`<path class="${s.cls||''}" data-w="${wk}" d="${curve(d)}"/>`:''}).join('')}
function spark(series,o={}){const v=series.flatMap(s=>(s.pts||[]).map(p=>p[1])).filter(x=>x!=null);
 if(v.length<2)return '<svg class=spk></svg>';
 const max=o.max??(Math.max(...v)*1.15||1);
 return `<svg class=spk viewBox="0 0 70 18" preserveAspectRatio="none">${_lines(series,70,18,max)}</svg>`}
const CH={};let CHN=0;
function nice(v){if(!(v>0))return 1;const p=Math.pow(10,Math.floor(Math.log10(v))),m=v/p;return (m<=1?1:m<=2?2:m<=2.5?2.5:m<=5?5:10)*p}
const CG={W:300,H:84,X0:30,X1:294,Y0:8,Y1:66},CGC={W:300,H:40,X0:2,X1:298,Y0:3,Y1:37};   /* CGC: compact rows (/cluster) */
function _geo(series,o){const ts=series.flatMap(s=>(s.pts||[]).map(p=>p[0]));const t0=ts.length?Math.min(...ts):0,t1=ts.length?Math.max(...ts):1;
 const vs=series.flatMap(s=>(s.pts||[]).map(p=>p[1])).filter(x=>x!=null);const peak=vs.length?Math.max(...vs):0;
 return {t0,t1:t1>t0?t1:t0+1,peak,max:o.max??nice(peak*1.15||1),cg:o.compact?CGC:CG}}
const _px=(t,g)=>g.cg.X0+(t-g.t0)/(g.t1-g.t0)*(g.cg.X1-g.cg.X0),_py=(v,g)=>g.cg.Y1-Math.min(v,g.max)/g.max*(g.cg.Y1-g.cg.Y0);
function chart(title,series,fmt,o={}){
 series=(series||[]).map(s=>({...s,pts:(s.pts||[]).filter(p=>p&&p[1]!=null)}));
 const id='ch'+(++CHN),g=_geo(series,o),G=g.cg;CH[id]={series,fmt,g,title};
 const has=series.some(s=>s.pts.length);
 const ticks=[0,.5,1].map(f=>({y:G.Y1-f*(G.Y1-G.Y0),v:g.max*f}));
 const tk=v=>{const f=String(fmt(v)).replace(/<[^>]+>/g,'');const m=f.match(/^[↑↓]?\s*(-?[\d.,]+\s?[kMGT]?)/);return m?m[1].replace(/\s+$/,''):f.slice(0,6)};
 const grid=ticks.map(t=>`<line class="gl" x1="${G.X0}" x2="${G.X1}" y1="${t.y}" y2="${t.y}"/>${o.compact?'':`<text class="tk" x="${G.X0-4}" y="${t.y+2.5}">${esc(tk(t.v))}</text>`}`).join('');
 const xy=s=>curve(s.pts.map(p=>[_px(p[0],g),_py(p[1],g)]));
 const wash=series.length<=2?series.map(s=>{if(s.pts.length<2)return '';const d=xy(s);
   return `<path class="wash ${s.cls||''}" d="${d} L${_px(s.pts[s.pts.length-1][0],g).toFixed(1)} ${G.Y1} L${_px(s.pts[0][0],g).toFixed(1)} ${G.Y1} Z"/>`}).join(''):'';
 const lines=series.map(s=>s.pts.length?`<path class="ln ${s.cls||''}" d="${xy(s)}"/>`:'').join('');
 const dots=o.compact?'':series.map(s=>{const p=s.pts[s.pts.length-1];return p?`<circle class="dot ${s.cls||''}" cx="${_px(p[0],g).toFixed(1)}" cy="${_py(p[1],g).toFixed(1)}" r="3.5"/>`:''}).join('');
 const lastv=s=>{const p=s.pts[s.pts.length-1];return p?esc(fmt(p[1])):'—'};
 const legend=series.length>1?series.map(s=>`<span class=lg><i class="key ${s.cls||''}"></i>${esc(s.name||'')} <b>${lastv(s)}</b></span>`).join('')
  :`<span class=lg><b>${series.length?lastv(series[0]):'—'}</b></span>`;
 return `<div class=chart data-ch=${id}><div class=ch-h><span class=ch-t>${esc(title)}</span>${legend}<span class=ch-max>peak ${esc(fmt(g.peak))}</span></div>
  <svg viewBox="0 0 ${G.W} ${G.H}"${o.compact?' preserveAspectRatio="none" class=cmp':''}>${grid}${wash}${lines}${dots}<line class="xh" x1="0" x2="0" y1="${G.Y0}" y2="${G.Y1}" style="display:none"/>${series.map((s,i)=>`<circle class="hd" data-i="${i}" r="3" style="display:none"/>`).join('')}${o.compact?'':`<text class="ax" x="${G.X0}" y="79">−${esc(o.span||'1h')}</text><text class="ax r" x="${G.X1}" y="79">now</text>`}</svg>
  ${has?'':'<div class=ch-empty>no data yet</div>'}</div>`}
/* hover layer: crosshair snaps to the nearest sample, one tooltip lists every series */
let TIP=null;
function hideTip(){if(TIP)TIP.style.display='none';document.querySelectorAll('.chart svg .xh,.chart svg .hd').forEach(l=>l.style.display='none')}
function initTip(){
 TIP=document.createElement('div');TIP.className='tip';TIP.style.display='none';document.body.appendChild(TIP);
 document.addEventListener('pointermove',e=>{
  const svg=e.target&&e.target.closest?e.target.closest('.chart svg'):null;if(!svg){hideTip();return}
  const c=CH[svg.parentElement.dataset.ch];if(!c||!c.series.some(s=>s.pts.length)){hideTip();return}
  const G=c.g.cg,r=svg.getBoundingClientRect(),fx=(e.clientX-r.left)/r.width*G.W,f=clamp((fx-G.X0)/(G.X1-G.X0),0,1),t=c.g.t0+f*(c.g.t1-c.g.t0);
  const rows=c.series.map((s,i)=>{if(!s.pts.length)return null;let b=s.pts[0];for(const p of s.pts)if(Math.abs(p[0]-t)<Math.abs(b[0]-t))b=p;return {s,i,p:b}}).filter(Boolean);
  const tx=rows[0].p[0],x=_px(tx,c.g);
  const xh=svg.querySelector('.xh');xh.setAttribute('x1',x);xh.setAttribute('x2',x);xh.style.display='';
  svg.querySelectorAll('.hd').forEach(d=>d.style.display='none');
  rows.forEach(({i,p})=>{const d=svg.querySelector(`.hd[data-i="${i}"]`);if(d){d.setAttribute('cx',_px(p[0],c.g));d.setAttribute('cy',_py(p[1],c.g));d.style.display=''}});
  TIP.replaceChildren();const h=document.createElement('div');h.className='tip-t';h.textContent=fmtTs(tx);TIP.appendChild(h);
  for(const {s,p} of rows){const d=document.createElement('div');d.className='tip-r';const k=document.createElement('i');k.className='key '+(s.cls||'');
   const v=document.createElement('b');v.textContent=c.fmt(p[1]);const n=document.createElement('span');n.textContent=s.name||c.title;d.append(k,v,n);TIP.appendChild(d)}
  TIP.style.display='';const tw=TIP.offsetWidth,th=TIP.offsetHeight;let left=e.clientX+14,top=e.clientY+14;
  if(left+tw>innerWidth-8)left=e.clientX-tw-14;if(top+th>innerHeight-8)top=e.clientY-th-10;TIP.style.left=left+'px';TIP.style.top=top+'px'});
 document.addEventListener('pointerleave',hideTip);document.addEventListener('scroll',hideTip,true)}
/* stable colour per entity (a model keeps its slot when others come and go) */
const LS_SLOT='cm_slots_v1';let SLOTS=null;
function slotOf(name){if(!SLOTS){try{SLOTS=JSON.parse(localStorage.getItem(LS_SLOT))||{}}catch(e){SLOTS={}}}
 if(SLOTS[name]==null){const used=new Set(Object.values(SLOTS));let i=0;while(used.has(i)&&i<8)i++;SLOTS[name]=i%8;try{localStorage.setItem(LS_SLOT,JSON.stringify(SLOTS))}catch(e){}}
 return 'c'+SLOTS[name]}
function ring(pct,label,inner,cls,labHtml){const r=21,c=2*Math.PI*r,p=clamp(+pct||0,0,100);
 return `<div class=ring data-k="${kOf(label)}"><svg viewBox="0 0 50 50"><circle class="track" cx="25" cy="25" r="${r}"/><circle class="fill ${cls||''}" cx="25" cy="25" r="${r}" stroke-dasharray="${(p/100*c).toFixed(1)} ${c.toFixed(1)}" transform="rotate(-90 25 25)"/></svg><div class=rv>${inner}</div><div class=rl>${labHtml??esc(label)}</div></div>`}
function statTile(label,value,unit,sub,pts,o={}){return `<div class=stat data-k="${o.k||kOf(label)}"><div class=l>${esc(label)}</div><div class=v>${value}<span class=u>${esc(unit||'')}</span></div>${pts&&pts.length>2?spark([{pts,cls:'dim'}],o):''}<div class=s>${sub||''}</div></div>`}
function ago(ts){if(!ts)return 'never';const m=Date.now()/1000-ts;
 if(m<90)return Math.round(m)+'s ago';if(m<5400)return Math.round(m/60)+'m ago';return Math.round(m/3600)+'h ago'}

const PANELS=[
 {id:'cluster', title:'Cluster',        sp:12,h:330,int:15,render:rCluster,api:()=>fetchJSON('/api/v1/metrics')},
 {id:'fleet',   title:'Fleet',          sp:5,h:430,int:15,render:rFleet,  api:()=>Promise.all([fetchJSON('/api/v1/overview'),
   fetchJSON('/api/v1/metrics').catch(()=>null)]).then(([o,m])=>{o.metrics=m;return o})},
 {id:'detail',  title:'Machine Report', sp:4,h:430,int:15,render:rDetail, api:()=>SEL?Promise.all([
   fetchJSON('/api/v1/facts/'+encodeURIComponent(SEL)+'?age=60'),
   DTAB=='backups'?Promise.resolve(null):fetchJSON('/api/v1/metrics/'+encodeURIComponent(SEL)+'?tab='+DTAB).catch(e=>({error:String(e.message||e)}))
  ]).then(([f,m])=>{if(f)f._m=m;return f}):Promise.resolve(null)},
 {id:'storage', title:'Storage Tiers',  sp:3,h:300,int:30,render:rStorage,api:()=>Promise.all([fetchJSON('/api/v1/capacity'),fetchJSON('/api/v1/growth').catch(()=>null)]).then(([t,g])=>{t._growth=g&&g.growth;return t})},
 {id:'alerts',  title:'Alerts',         sp:5,h:220,int:20,render:rAlerts, api:()=>fetchJSON('/api/v1/alerts')},
 {id:'brief',   title:'Morning Brief',  sp:5,h:220,int:600,render:rBrief,  api:()=>fetchJSON('/api/v1/digest')},
 {id:'hygiene', title:'Hygiene',        sp:7,h:260,int:60,render:rHygiene,api:()=>fetchJSON('/api/v1/hygiene')},
 {id:'browse',  title:'Snapshot Browser',sp:7,h:380,int:0, render:rBrowse, api:null},
 {id:'schedule',title:'Schedule',       sp:7,h:330,int:30,render:rSched,  api:()=>fetchJSON('/api/v1/schedule')},
 {id:'runs',    title:'Runs',           sp:5,h:330,int:15,render:rRuns,   api:()=>fetchJSON('/api/v1/runs')},
 {id:'jobs',    title:'Jobs / Audit',   sp:3,h:340,int:20,render:rJobs,   api:()=>fetchJSON('/api/v1/audit?n=14')},
 {id:'network', title:'Network',        sp:3,h:250,int:60,render:rNet,    api:()=>fetchJSON('/api/v1/probes')},
 {id:'actions', title:'Console',        sp:5,h:340,int:0, render:rActions,api:null},
];

if(VIEW=='config'){
 PANELS.splice(0,PANELS.length,
  {id:'config',title:'Backup Config',sp:12,h:0,int:300,render:rConfig,api:()=>fetchJSON('/api/v1/policy')});
 document.getElementById('desk').classList.add('stack');
}
if(VIEW=='recipes'){
 PANELS.splice(0,PANELS.length,
  {id:'recipes',title:'Recipes',sp:12,h:0,int:20,render:rRecipes,api:()=>fetchJSON('/api/v1/recipes')});
 document.getElementById('desk').classList.add('stack');
}
if(VIEW=='machine'){
 MX={host:new URLSearchParams(location.search).get('host')||localStorage.getItem(LS_SEL)||'',job:null,hy:{}};
 PANELS.splice(0,PANELS.length,{id:'machine',title:'Machine',sp:12,h:0,int:15,render:rMachine,
  api:()=>MX.host?fetchJSON('/api/v1/machine/'+encodeURIComponent(MX.host)):fetchJSON('/api/v1/machines').then(d=>{MX.host=((d.machines||[])[0]||{}).name;return fetchJSON('/api/v1/machine/'+encodeURIComponent(MX.host))})});
 document.getElementById('desk').classList.add('stack');
 document.addEventListener('keydown',e=>{if(e.ctrlKey||e.metaKey||e.altKey||/INPUT|TEXTAREA|SELECT/.test((document.activeElement||{}).tagName||''))return;
  if(e.key=='ArrowLeft')mxStep(-1);else if(e.key=='ArrowRight')mxStep(1)});
}
if(VIEW=='machines'){
 PANELS.splice(0,PANELS.length,{id:'machines',title:'Machines',sp:12,h:0,int:20,render:rMachines,api:()=>fetchJSON('/api/v1/machines')});
 document.getElementById('desk').classList.add('stack');
}
if(VIEW=='heal'){
 PANELS.splice(0,PANELS.length,{id:'heal',title:'Self-repair',sp:12,h:0,int:20,render:rHeal,api:srApi});
 document.getElementById('desk').classList.add('stack');
}
if(VIEW=='about'){
 PANELS.splice(0,PANELS.length,{id:'about',title:'About',sp:12,h:0,int:3600,render:rAbout,api:()=>fetchJSON('/api/v1/about')});
 document.getElementById('desk').classList.add('stack');
}
/* /kiosk: the wall display — no chrome, big type, every source refreshed together every 10 s */
if(VIEW=='kiosk'){
 PANELS.splice(0,PANELS.length,{id:'kiosk',title:'Kiosk',sp:12,h:0,int:10,render:rKiosk,
  api:()=>Promise.all([fetchJSON('/api/v1/metrics'),fetchJSON('/api/v1/recipes').catch(()=>({})),fetchJSON('/api/v1/overview').catch(()=>({})),fetchJSON('/api/v1/alerts').catch(()=>({}))])
   .then(([m,r,o,a])=>({m,r,o,a}))});
 document.body.classList.add('kiosk');document.getElementById('desk').classList.add('stack');
}
/* rotation mode: the pager is armed after buildPanels() creates #b-kiosk (see kioskArm) */
function kioskArm(){
 if(VIEW!='kiosk'||kioskArm.on)return;
 const k=kioskCfg();
 /* the wall shows one display and stays on it (operator, 2026-10-22): rotation alternates
    views only when this browser asks for it explicitly with ?rotation=1 in the URL — a
    site setting alone can never make the wall page through screens */
 if(!new URLSearchParams(location.search).has('rotation')||new URLSearchParams(location.search).has('page')||!k.rotation||k.fit=='scroll')return;
 kioskArm.on=true;
 setInterval(()=>{if(kioskPages()<2)return;kioskPg=(kioskPg+1)%kioskPages();kioskRepaint();},20000);}
/* /cluster: only the systems. Both panels read the same fleet metrics call. */
if(VIEW=='cluster'){
 PANELS.splice(0,PANELS.length,
  {id:'cluster',title:'Cluster',sp:12,h:0,int:15,render:rControl,api:()=>fetchJSON('/api/v1/metrics')});
 document.getElementById('desk').classList.add('stack');
}
/* ── navigation: menus in the header, the page name, 1–4 to jump ── */
const PAGES={main:['/','Dashboard'],cluster:['/cluster','Cluster'],recipes:['/recipes','Recipes'],config:['/config','Configuration'],heal:['/heal','Self-repair'],machines:['/machines','Machines'],machine:['/machine','Machine'],about:['/about','About'],kiosk:['/kiosk','Kiosk']};
(function nav(){
 const name=(PAGES[VIEW]||PAGES.main)[1];const pn=$('hpage');if(pn)pn.textContent=name;document.title=`Noctyx · ${name}`;
 document.querySelectorAll('#nav .menu').forEach(m=>{
  if(m.querySelector(`a[data-v="${VIEW}"]`))m.classList.add('cur');
  m.querySelectorAll('a[data-v]').forEach(a=>{if(a.dataset.v==VIEW)a.classList.add('cur')});
  const b=m.querySelector('.menu-title'),pop=m.querySelector('.menu-pop');
  b.addEventListener('click',e=>{e.stopPropagation();const open=!m.classList.contains('open');closeMenus();if(open){m.classList.add('open');pop.classList.remove('hide');b.setAttribute('aria-expanded','true')}});
  m.addEventListener('mouseenter',()=>{if(document.querySelector('#nav .menu.open')&&!m.classList.contains('open'))b.click()});
 });
 document.addEventListener('click',closeMenus);
 document.addEventListener('keydown',e=>{
  if(e.key=='Escape'){closeMenus();setClose();if(document.activeElement&&document.activeElement.id=='askq')askClose();return}
  if(e.ctrlKey||e.metaKey||e.altKey||/INPUT|TEXTAREA|SELECT/.test((document.activeElement||{}).tagName||''))return;
  const go={'1':'/','2':'/cluster','3':'/recipes','4':'/config','5':'/kiosk','6':'/machines'}[e.key];if(go&&location.pathname!=go)location.href=go});
 if(VIEW=='config'&&location.hash)setTimeout(()=>{const el=document.getElementById(location.hash.slice(1));if(el)el.scrollIntoView({behavior:'smooth',block:'start'})},900);
})();
function closeMenus(){document.querySelectorAll('#nav .menu.open').forEach(m=>{m.classList.remove('open');m.querySelector('.menu-pop').classList.add('hide');m.querySelector('.menu-title').setAttribute('aria-expanded','false')})}

/* ── display settings (the gear): appearance, units, layout. This browser's, over the site's
   defaults (site config "console", saved by an operator with "save as site default"). ── */
var SITESET=null,ME=null;   // var: fetchJSON may run before this line, and a let would throw there
const LS_WHERE='nx_settings_where';
const whereSaved=()=>{try{return localStorage.getItem(LS_WHERE)||'browser'}catch(e){return 'browser'}};
async function loadMe(){try{ME=await fetchJSON('/api/v1/me')}catch(e){ME=null}
 const c=$('huser');if(c&&ME){
  c.innerHTML=ME.user?`<b>${esc(ME.user)}</b><span class=role>${esc(ME.role)}</span><button class=lnk onclick="signOut()">sign out</button>`
   :ME.mode=='open'?`<span class="hpill" title="anyone on this network may look and act; sign-in is optional">open access</span>${ME.accounts?'<a class=lnk href="/login?next='+encodeURIComponent(location.pathname)+'">sign in</a>':''}`:''}
 if(ME&&ME.user&&whereSaved()=='account'){try{const s=await fetchJSON('/api/v1/settings/mine');
   if(s.settings&&Object.keys(s.settings).length){localStorage.setItem('nx_settings_v1',JSON.stringify(s.settings));NXT.apply();
    const t=NXT.eff().tunit=='C'?'C':'F';if(t!=TU){TU=t;PANELS.forEach(p=>{if(p._data!==undefined&&p.id!='config')repaint(p.id,p._data)})}}}catch(e){}}
 setRender()}
async function signOut(){await fetch('/api/v1/logout',{method:'POST'});location.href=ME&&ME.mode=='accounts'?'/login':location.pathname}
async function pushAccount(){if(!(ME&&ME.user&&whereSaved()=='account'))return;
 const r=await fetch('/api/v1/settings/mine',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({settings:NXT.local()})});
 const m=$('setmsg');if(m&&!r.ok)m.textContent='✗ could not save to your account: '+((await r.json().catch(()=>({}))).error||r.status)}
function setWhere(w){try{localStorage.setItem(LS_WHERE,w)}catch(e){};if(w=='account')pushAccount();setRender()}
async function loadSiteSettings(){try{SITESET=await fetchJSON('/api/v1/console-settings');NXT.setSite(SITESET.settings||{});
 const t=NXT.eff().tunit=='C'?'C':'F';if(t!=TU){TU=t;PANELS.forEach(p=>{if(p._data!==undefined&&p.id!='config')repaint(p.id,p._data)})}}catch(e){}
 setRender()}
function setOpen(){$('setwrap').classList.remove('hide');setRender();loadSiteSettings()}
function setClose(){$('setwrap').classList.add('hide')}

/* ── Ask Noctyx (copilot.py): a conversation with the site's assistant, which looks things up with the
   console's own data before it answers. The LLM's text is built as text nodes, never HTML. ── */
const ASK={sid:null,rev:0,busy:false,timer:null,hi:null,draft:'',done:{}};
try{ASK.sid=sessionStorage.getItem('nx_ask_sid')}catch(e){}
const askEl=(tag,cls,text)=>{const e=document.createElement(tag);if(cls)e.className=cls;if(text!=null)e.textContent=text;return e};
function askInline(t){const f=document.createDocumentFragment();t.split(/(`[^`\n]+`|\*\*[^*\n]+\*\*)/).forEach(p=>{
 if(p.length>2&&p.startsWith('`')&&p.endsWith('`'))f.append(askEl('code',null,p.slice(1,-1)));
 else if(p.length>4&&p.startsWith('**')&&p.endsWith('**'))f.append(askEl('b',null,p.slice(2,-2)));else f.append(p)});return f}
function askRich(text){const f=document.createDocumentFragment();text.split(/```[a-z]*\n?([\s\S]*?)```/).forEach((p,k)=>{
 if(k%2)f.append(askEl('pre',null,p.replace(/\n$/,'')));else f.append(askInline(p.replace(/^\n|\n$/g,'')))});return f}
const ASK_HINTS=['How is the cluster right now?','Did anything fail in the last 24 hours?','Which GPU ran hottest today?','When will the backup storage fill up?'];
function askToggle(){const w=$('askwrap');if(w.classList.contains('hide'))askOpen();else askClose()}
function askOpen(){$('askwrap').classList.remove('hide');if(ASK.sid&&!ASK.rev)askPoll();else if(!ASK.sid)askEmpty();setTimeout(()=>$('askq').focus(),30)}
function askClose(){$('askwrap').classList.add('hide')}
function askEmpty(){const L=$('asklog');L.textContent='';const h=askEl('div','askhint');
 ASK_HINTS.forEach(t=>{const b=askEl('button',null,t);b.type='button';b.onclick=()=>askSend(t);h.append(b)});
 L.append(askEl('div','askmsg bot','Ask anything about this site: its machines, models, backups, alerts or storage. I look at the live data before I answer.'),h)}
function askNew(){ASK.sid=null;ASK.rev=0;try{sessionStorage.removeItem('nx_ask_sid')}catch(e){}$('askmodel').textContent='';askEmpty();$('askq').focus()}
function askItem(i){
 if(i.kind=='user')return askEl('div','askmsg user',i.text);
 if(i.kind=='assistant'){const d=askEl('div','askmsg bot');d.append(askRich(i.text));if(i.model)$('askmodel').textContent=i.model;return d}
 if(i.kind=='tool'){const d=askEl('details','asktool'+(i.ok?'':' bad'));const a=Object.entries(i.args||{}).map(([k,v])=>k+'='+JSON.stringify(v)).join(' ');
  d.append(askEl('summary',null,(i.tool=='offer_action'?(i.ok?'offered a button: ':'could not offer: '):(i.ok?'looked at ':'could not look at '))+i.tool+(a?' ('+a+')':'')),askEl('pre',null,i.out||''));return d}
 if(i.kind=='action'){const d=askEl('div','askact');if(i.why)d.append(askEl('div','askwhy',i.why));
  const b=askEl('button','act',i.label);b.title=`runs the console's ${i.action} action as you (${i.need}); nothing has run yet`;
  if(ASK.done[i.id]){b.disabled=true;b.textContent=i.label+' · '+ASK.done[i.id]}
  b.onclick=async()=>{if(!confirm(`${i.label}?\n\nOffered by the assistant${i.why?': '+i.why:''}\n\nIt runs as a job (Jobs / Audit shows it).`))return;
   b.disabled=true;b.textContent=i.label+' · starting…';const r=await act(i.action,i.params);
   ASK.done[i.id]=r&&r.error?'refused: '+r.error:'started';b.textContent=i.label+' · '+ASK.done[i.id];if(r&&r.error)b.disabled=false};
  d.append(b);return d}
 if(i.kind=='error')return askEl('div','askmsg err',i.text);
 return askEl('div','askmsg',i.text||'')}
function askRender(v){const L=$('asklog');if(!ASK.rev)L.textContent='';
 L.querySelectorAll('.askbusy').forEach(e=>e.remove());
 (v.items||[]).forEach(i=>{const old=L.querySelector(`[data-i="${i.id}"]`);const n=askItem(i);n.dataset.i=i.id;if(old)old.replaceWith(n);else L.append(n)});
 ASK.rev=Math.max(ASK.rev,v.rev||0);ASK.busy=!!v.busy;
 if(ASK.busy)L.append(askEl('div','askbusy','looking'));
 $('asknote').textContent=v.advice_only?'This model does not take tool calls: it answers without looking anything up.':'It looks things up but changes nothing: what it suggests, you do.';
 L.scrollTop=L.scrollHeight}
async function askPoll(){clearTimeout(ASK.timer);if(!ASK.sid)return;
 let v;try{const r=await fetch('/api/v1/ask/'+encodeURIComponent(ASK.sid)+'?since='+ASK.rev);v=await r.json();
  if(!r.ok){if(v.ended){ASK.sid=null;ASK.rev=0;ASK.busy=false;try{sessionStorage.removeItem('nx_ask_sid')}catch(e){}}
   $('asklog').append(askEl('div','askmsg err',v.ended?'This conversation has ended (the console restarted, or it sat unused for hours): your next question starts a new one.':v.error||('HTTP '+r.status)));return}}
 catch(e){ASK.timer=setTimeout(askPoll,2000);return}
 askRender(v);if(ASK.busy)ASK.timer=setTimeout(askPoll,800)}
async function askPrompt(){const L=$('asklog'),tok=localStorage.getItem(LS_TOK)||'';
 let d;try{const r=await fetch('/api/v1/ask-prompt?'+new URLSearchParams({sid:ASK.sid||'',tunit:TU,tz:askZone()}),{headers:{'Authorization':'Bearer '+tok}});d=await r.json()}catch(e){d={error:String(e)}}
 if(d.error){L.append(askEl('div','askmsg err',d.error));return}
 L.querySelectorAll('.askprompt').forEach(e=>e.remove());
 const x=askEl('details','asktool askprompt');x.open=true;
 x.append(askEl('summary',null,'What Ask is told'+(ASK.sid?' in this conversation':' in a new conversation')+': '+(d.custom?"this site's own prompt":'the built-in prompt')+(d.notes?", plus the site's notes":'')+' (Configure › Assistant LLM)'),askEl('pre',null,d.system));
 L.append(x);x.scrollIntoView({block:'start'})}
function askZone(){try{return Intl.DateTimeFormat().resolvedOptions().timeZone||''}catch(e){return ''}}
async function askSend(text){text=(text||'').trim();if(!text||ASK.busy)return;
 const L=$('asklog');if(!ASK.sid)L.textContent='';
 const tok=localStorage.getItem(LS_TOK)||'';
 let r,d;try{r=await fetch('/api/v1/ask',{method:'POST',headers:{'Content-Type':'application/json','Authorization':'Bearer '+tok},
  body:JSON.stringify({sid:ASK.sid,text,tunit:TU,tz:askZone()})});d=await r.json()}catch(e){d={error:String(e)}}
 if(!r||!r.ok){if(r&&r.status==409&&/ended/.test(d.error||'')){askNew();return askSend(text)}
  L.append(askEl('div','askmsg err',d.error||'the console did not answer'));return}
 if(d.sid!==ASK.sid){ASK.sid=d.sid;ASK.rev=0;try{sessionStorage.setItem('nx_ask_sid',d.sid)}catch(e){}}
 askRemember(text);ASK.busy=true;askSet('');askPoll()}
/* earlier questions, newest last, kept in this browser: Up/Down step through them like a shell, Enter asks the
   one shown again, Esc clears the box (Esc on an empty box closes the drawer) */
const ASK_HIST='nx_ask_hist',ASK_HIST_MAX=50;
function askHist(){try{const h=JSON.parse(localStorage.getItem(ASK_HIST)||'[]');return Array.isArray(h)?h:[]}catch(e){return []}}
function askRemember(t){const h=askHist().filter(x=>x!==t);h.push(t);try{localStorage.setItem(ASK_HIST,JSON.stringify(h.slice(-ASK_HIST_MAX)))}catch(e){}
 ASK.hi=null;ASK.draft=''}
function askSet(v){const q=$('askq');q.value=v;q.style.height='auto';q.style.height=Math.min(140,q.scrollHeight)+'px';
 q.setSelectionRange(v.length,v.length)}
function askStep(dir){const h=askHist();if(!h.length)return false;
 let i=ASK.hi==null?h.length:ASK.hi;if(ASK.hi==null)ASK.draft=$('askq').value;
 i=Math.max(0,Math.min(h.length,i+dir));if(i===(ASK.hi==null?h.length:ASK.hi))return false;
 ASK.hi=i==h.length?null:i;askSet(i==h.length?ASK.draft||'':h[i]);return true}
$('askform').addEventListener('submit',e=>{e.preventDefault();askSend($('askq').value)});
$('askq').addEventListener('keydown',e=>{const q=e.target;
 if(e.key=='Enter'&&!e.shiftKey){e.preventDefault();askSend(q.value);return}
 if(e.key=='Escape'&&q.value){e.preventDefault();e.stopPropagation();ASK.hi=null;ASK.draft='';askSet('');return}
 const atTop=!q.value.slice(0,q.selectionStart).includes('\n'),atEnd=!q.value.slice(q.selectionEnd).includes('\n');
 if(e.key=='ArrowUp'&&atTop&&!e.shiftKey&&askStep(-1))e.preventDefault();
 else if(e.key=='ArrowDown'&&atEnd&&!e.shiftKey&&askStep(1))e.preventDefault()});
$('askq').addEventListener('input',e=>{const t=e.target;ASK.hi=null;t.style.height='auto';t.style.height=Math.min(140,t.scrollHeight)+'px'});
const segH=(cur,opts,fn)=>`<div class=seg>${opts.map(([v,l])=>`<button type=button class="${v==cur?'on':''}" onclick="${fn}('${v}')">${l}</button>`).join('')}</div>`;
function setRender(){
 const P=$('setpanel');if(!P||$('setwrap').classList.contains('hide'))return;
 const s=NXT.eff(),L=NXT.local(),own=Object.keys(L).length>0,f=NXT.face();
 const sch=s.mode=='light'?'light':s.mode=='system'?(f.scheme):'dark';
 const cards=Object.entries(NXT.PRESETS).map(([id,p])=>{const x=NXT.derive(p[sch=='light'?'light':'dark']);
   return `<button type=button class="tcard${s.theme==id?' on':''}" onclick="setTheme('${id}')"><b>${esc(p.label)}</b><small>${esc(p.blurb)}</small>
    <span class=swatch>${p.style?[x.bg,...['cpu','gpu','mem','net','power','llm','temp'].map(k=>p.dark.k[k])].map(c=>`<i style="background:${c}"></i>`).join(''):['bg','panel','accent','accent2','ok','warn','bad'].map(k=>`<i style="background:${x[k]}"></i>`).join('')}</span></button>`}).join('')
  +`<button type=button class="tcard${s.theme=='custom'?' on':''}" onclick="setTheme('custom')"><b>Custom</b><small>Your nine colours; the rest is mixed from them</small>
    <span class=swatch>${['bg','panel','accent','ok','warn','bad'].map(k=>`<i style="background:${s.custom[k]}"></i>`).join('')}</span></button>`;
 const names={bg:'Background',panel:'Panels',text:'Text',muted:'Muted text',accent:'Accent',line:'Lines',ok:'Good',warn:'Warning',bad:'Bad'};
 const picks=NXT.BASE.map(k=>`<label><input type=color value="${esc(s.custom[k])}" oninput="setCustom('${k}',this.value)">${names[k]}</label>`).join('');
 const site=SITESET&&SITESET.settings&&Object.keys(SITESET.settings).length;
 const canSite=!ME||ME.mode!='accounts'||ME.role=='admin',acct=ME&&ME.user;
 P.innerHTML=`<h2 id=setTitle>Display settings <span class=sp></span><button class=hbtn onclick="setReset()" title="Back to the site's defaults (or the shipped look)">reset</button><button class=hbtn onclick="setClose()">close</button></h2>
 <p class=setnote>Applied as you change them. They change how the console looks, never what it measures or does.</p>
 <div class=setstore>${own?(acct&&whereSaved()=='account'?'Saved in <b>your account</b> ('+esc(ME.user)+')':'<b>This browser</b> has its own settings'):'Using <b>'+(site?'the site\'s defaults':'the shipped look')+'</b>'}
  <span class=hspacer></span>${canSite?'<button class=hbtn onclick="setSaveSite()" title="Every browser without its own settings gets these">save as site default</button>':''}${own?'<button class=hbtn onclick="setReset()">use site default</button>':''}</div>
 ${acct?`<div class=setrow><label>Keep my settings</label>${segH(whereSaved(),[['browser','In this browser'],['account','In my account']],'setWhere')}<div class=hint>In your account they follow you to any browser you sign in on.</div></div>`:''}
 <div id=setmsg class=setnote></div>
 <div class=setgrp><h3>Appearance</h3><p class=gnote>The colours of the console: page, panels, text, lines, accent and the good / warning / bad states.</p>
  <div class=setrow><label>Mode</label>${segH(s.mode,[['light','Light'],['dark','Dark'],['system','System']],'setMode')}<div class=hint>Light or dark, or whatever the operating system asks for (System follows it as it changes).</div></div>
  <div class=setrow><label>Theme</label><div class=tcards>${cards}</div><div class=hint>Each has a light and a dark face; the mode picks which. Custom uses the nine colours below.</div></div>
  <div class="setrow${s.theme=='custom'?'':' dim'}"><label>Custom colours</label><div class=cpick>${picks}</div><div class=hint>Only used by the Custom theme. Its mode comes from the background you pick.</div></div></div>
 ${fxGroup(s)}
 ${kioskGroup(s)}
 <div class=setgrp><h3>Units</h3>
  <div class=setrow><label>Temperature</label>${segH(TU,[['F','°F'],['C','°C']],'setTU')}<div class=hint>Readings and alert thresholds stay in °C underneath; only the display converts.</div></div></div>
 <div class=setgrp><h3>Layout</h3>
  <div class=setrow><label>Panels</label><div><button class=hbtn onclick="layoutReset()">reset panel layout</button></div><div class=hint>Puts every dashboard panel back where it started (this browser).</div></div></div>`}
function setMode(m){NXT.save({mode:m});pushAccount();setRender()}
function setFx(k,v){NXT.save({fx:{[k]:v}});pushAccount();setRender()}
function fxGroup(s){const on=NXT.face().style=='phosphor',fx=s.fx||NXT.FXDEF,
  row=(k,lab,opts,hint)=>`<div class=setrow><label>${lab}</label><div class=seg>${opts.map(([v,l])=>`<button type=button class="${fx[k]==v?'on':''}" onclick="setFx('${k}','${v}')">${l}</button>`).join('')}</div><div class=hint>${hint}</div></div>`,
  oo=[['off','Off'],['on','On']];
 return `<div class="setgrp${on?'':' dim'}"><h3>Phosphor effects</h3><p class=gnote>${on?'Each is a single still layer, or moves with the compositor alone (no repaint of the page): light on the GPU.':'For the Phosphor themes: pick one above to use them.'}</p>
  ${row('glow','Glow',[['off','Off'],['soft','Soft'],['normal','Normal'],['bright','Bright']],'How much the meters, traces and numerals bloom.')}
  ${row('scan','Scanlines',[['off','Off'],['kiosk','Kiosk'],['all','Everywhere']],'Fine horizontal lines, like a CRT.')}
  ${row('vignette','Vignette',oo,'Darker corners, like the curve of a tube.')}
  ${row('grain','Grain',oo,'A faint, still film grain over the glass.')}
  ${row('fringe','RGB fringe',oo,'A hint of colour separation on the big numerals.')}
  ${row('beam','Beam sweep',oo,'A soft band of light that rolls down the screen every few seconds.')}
  ${row('flicker','Flicker',oo,'A barely-there phosphor flicker. Both moving effects stop when the system asks for reduced motion.')}</div>`}
function setTheme(id){NXT.save({theme:id});pushAccount();setRender()}
function setKiosk(k,v){const val=(k=='rotation')?v=='true':(k=='fit'||k=='cols'?v:+v);NXT.save({kiosk:{[k]:val}});pushAccount();setRender();
 if(VIEW=='kiosk'){kioskPg=0;const p=PANELS.find(x=>x.id=='kiosk');if(p&&p._data!==undefined)repaint('kiosk',p._data)}}
function kioskGroup(s){const k=s.kiosk||NXT.DEF.kiosk,
 row=(kk,lab,opts,hint)=>`<div class=setrow><label>${lab}</label><div class=seg>${opts.map(([v,l])=>`<button type=button class=\"${String(k[kk])==String(v)?'on':''}\" onclick=\"setKiosk('${kk}','${v}')\">${l}</button>`).join('')}</div><div class=hint>${hint}</div></div>`,
 lv=[['0','Off'],['1','On'],['2','Detail']];
 return `<div class=setgrp><h3>Kiosk</h3><p class=gnote>How much the wall display (<code>/kiosk</code>) shows. Any browser can also override this with URL flags: <code>?page=1</code>, <code>?alerts=2</code>, <code>?rotation=1</code>, <code>?fit=scroll</code>.</p>
 ${row('alerts','Alerts',lv,'Detail: a full page of alert summaries and triage (?page=1). The nodes page never shows alerts.')}
 ${row('backups','Backups',lv,'Off: the ratio only. On: the count. Detail: a page of per-host last run, next due, window.')}
 ${row('rotation','Rotation',[['false','Off'],['true','On']],'Page through nodes → alerts → backups every 20 seconds. Only takes effect in a browser whose URL asks for it (?rotation=1): the wall never changes views on its own.')}
 ${row('dense','Dense header',[['false','Off'],['true','On']],'Five fleet numbers on the header line (adds LLMs serving); off keeps four with bigger type.')}
 ${row('cols','Grid columns',[['0','Auto'],['2','2'],['3','3'],['4','4'],['5','5']],'Force the node grid to a column count. Auto picks the most columns whose cards still fit the screen. Try 3 for taller cards on a wall with rows to spare.')}
 ${row('fit','Fit',[['shrink','Shrink'],['scroll','Scroll']],'Shrink: everything on one screen, the type gets small (the classic kiosk). Scroll: full-size type, the grid scrolls when it overflows.')}</div>`}
function setCustom(k,v){NXT.save({theme:'custom',custom:{[k]:v}});clearTimeout(setCustom._p);setCustom._p=setTimeout(pushAccount,600);
 /* re-render only the cards/swatches when the pickers settle: keep the open picker */
 clearTimeout(setCustom._t);setCustom._t=setTimeout(setRender,400)}
function setReset(){NXT.clearLocal();TU=NXT.eff().tunit=='C'?'C':'F';PANELS.forEach(p=>{if(p._data!==undefined&&p.id!='config')repaint(p.id,p._data)});setRender()}
async function setSaveSite(){const s=NXT.eff(),body={mode:s.mode,theme:s.theme,tunit:TU,custom:s.custom,fx:s.fx,kiosk:s.kiosk};
 const m=$('setmsg');if(m)m.textContent='saving as the site default…';
 const r=await act('setConsoleDefaults',{settings:body});
 if(m)m.textContent=r&&r.error?'✗ '+r.error:'✓ saved: browsers without their own settings use these';setTimeout(loadSiteSettings,2500)}
loadSiteSettings();loadMe();
if(location.hash=='#settings')setOpen();

/* ── About ── */
function rAbout(d){
 if(!d)return '<div class=empty>loading…</div>';
 const S=d.site||{},R=d.repo||'https://github.com/BobClawblaw/noctyx';
 const pill=(k,v,c)=>`<span class="apill ${c||''}"><span>${esc(k)}</span><b>${esc(v)}</b></span>`;
 return `<div class=about>
  <img class=abanner src="/assets/banner.svg" alt="Noctyx — the night-shift operator for your AI cluster">
  <div class=apills>${pill('version',d.version)}${pill('license','PolyForm NC 1.0')}${pill('python',d.python)}${pill('restic',d.restic)}${pill('status','alpha','warn')}</div>
  <div class=acols>
   <div class=acard><h3>What Noctyx does</h3>
    <div class=afeat><b>Deploy</b><span>Model recipes onto GPU slots: built while the current model serves, switched under the slot's GPU lock, rolled back automatically. A model swapped by hand is noticed. Public recipes for your hardware, ranked by measured speed.</span></div>
    <div class=afeat><b>Cache</b><span>One copy of every Hugging Face model on the cluster, served back as <code>HF_ENDPOINT</code>; keeps working when the Hub does not.</span></div>
    <div class=afeat><b>Protect</b><span>Nightly snapshots of every node, Docker volumes, consistent database dumps, retention, verification, a dual-arch recovery stick for bare-metal restores.</span></div>
    <div class=afeat><b>Watch</b><span>Live fleet: temperatures, power, tokens/s per model, alerts with LLM triage, a morning brief, hygiene with the space each fix frees. <b style="font:inherit;color:var(--fg)">Ask</b> answers questions from the cluster's own data.</span></div>
    <div class=afeat><b>Heal</b><span>Every upgrade checks itself and rolls back when it fails, one machine first. A repair loop every 5 minutes restarts what stopped and puts back what drifted, then says what it did; what it leaves to you comes with a diagnosis.</span></div></div>
   <div class=acard><h3>This site</h3>
    <div class=arow><span>head node</span><b>${esc(S.head||'?')}</b></div>
    <div class=arow><span>hosts</span><b>${esc(S.hosts)}</b></div>
    <div class=arow><span>GPU slots · recipes</span><b>${esc(S.slots)} · ${esc(S.recipes)}</b></div>
    <div class=arow><span>backup target</span><b>${esc(S.nas)}</b></div>
    <div class=arow><span>console build</span><b>${esc(d.build)}</b></div>
    <h3 style="margin-top:14px">Keys</h3>
    <div class=akeys><kbd>1</kbd> dashboard <kbd>2</kbd> cluster <kbd>3</kbd> recipes <kbd>4</kbd> configuration <kbd>5</kbd> kiosk <kbd>6</kbd> machines <kbd>Esc</kbd> close menus and the drawer · settings: the gear, or <code>/#settings</code></div></div>
   <div class=acard><h3>Documentation</h3>
    <div class=alinks><a href="${R}#readme" target=_blank rel=noopener>README and screenshots</a><a href="${R}/tree/main/docs/guide" target=_blank rel=noopener>Operator's guide</a>
     <a href="${R}/blob/main/docs/guide/install.md" target=_blank rel=noopener>Install a head node and agents</a><a href="${R}/blob/main/docs/guide/restore.md" target=_blank rel=noopener>Restore a file, a node, the head</a>
     <a href="${R}/blob/main/docs/guide/recipes.md" target=_blank rel=noopener>Deploy recipes</a><a href="${R}/blob/main/docs/guide/site-config.md" target=_blank rel=noopener>Site config reference</a>
     <a href="${R}/blob/main/docs/spec/recipe-deploy.md" target=_blank rel=noopener>Recipe deploy spec (draft)</a></div></div>
   <div class=acard><h3>License</h3>
    <p>Licensed under the <b>PolyForm Noncommercial License 1.0.0</b>.</p>
    <p class=mut>Free for personal, hobbyist, educational and noncommercial use. Nobody may sell Noctyx or
     paid support for it — donationware via Bitcoin only. The bundled fonts stay under the SIL Open Font License;
     the recipe deploy spec is CC-BY-4.0.</p>
    <div class=alinks><a href="${R}/blob/main/LICENSE" target=_blank rel=noopener>LICENSE</a><a href="/assets/fonts/LICENSES.txt">Font licenses</a></div></div>
   <div class=acard><h3>Support Noctyx</h3>
    <p class=mut>100% free donationware — no paid tiers or paid support. Voluntary Bitcoin tips keep it going.</p>
    <div class=adonate><img src="/assets/donate-qr.png" alt="QR code: the bitcoin address" width=164 height=164>
     <div><div class=k>Bitcoin</div><button class=addr title="copy" onclick="copyAddr(this,'${esc(d.donate)}')">${esc(d.donate)}</button><div class="mut copied"></div></div></div></div>
  </div></div>`}
function copyAddr(btn,a){const ok=()=>{btn.nextElementSibling.textContent='copied ✓';setTimeout(()=>btn.nextElementSibling.textContent='',1800)};
 try{navigator.clipboard.writeText(a).then(ok,()=>{})}catch(e){const r=document.createRange();r.selectNodeContents(btn);getSelection().removeAllRanges();getSelection().addRange(r)}}

/* ── Kiosk ── */
const lastV=s=>Array.isArray(s)&&s.length?+s[s.length-1][1]||0:0;
/* compact magnitude for the header vitals: no space before the unit, so the number and its
   suffix read as one token at kiosk distance. */
const kTok=v=>v>=1e9?(v/1e9).toFixed(2)+'G':v>=1e6?(v/1e6).toFixed(v>=1e7?0:1)+'M':v>=1e3?(v/1e3).toFixed(v>=1e5?0:1)+'k':String(Math.round(v));
/* the identity of the kiosk's current screen (fit hysteresis compares it): page + the
   settings that change how much room the grid gets. */
function pgKey(){const k=kioskCfg();return[kioskPage(),k.rotation?'r':'-',k.fit].join('/')}
/* the kiosk's info level: site default (console › settings › Kiosk), overridable per browser by
   localStorage and by the URL. ?alerts= ?backups= (2 adds a detail page),
   ?rotation=1 pages through views every 20 s, ?page=n pins one view (0 nodes · 1 alerts ·
   2 backups; views beyond the first only exist as their detail level allows). */
let kioskPg=0;
/* The plain /kiosk is the classic one-screen wall: shrink fit, no extra pages, nothing off the
   display. URL flags extend it per browser: ?page=n pins one screen, and ?alerts= ?backups=
   ?rotation= ?fit= opt into the extra content — every one of which forces a fit that
   keeps whatever it shows fully on screen (shrink) unless the browser explicitly asks for
   ?fit=scroll. The site's Kiosk settings hold the same switches for browsers that want them
   as the standing choice. (operator, 2026-10-22, after a scroll-default regression: a wall
   must never hide information below or beside its own edges.) */
const kioskCfg=()=>{
 const q=new URLSearchParams(location.search),k={...NXT.eff().kiosk};
 const has=['alerts','backups','rotation','page','fit','cols'].some(p=>q.has(p));
 for(const p of['alerts','backups']){const v=+q.get(p);if(q.get(p)!=null&&!isNaN(v))k[p]=v}
 if(q.get('rotation')!=null)k.rotation=q.get('rotation')!='0';
 if(q.get('dense')!=null)k.dense=q.get('dense')!='0';
 {const v=+q.get('cols');if(q.get('cols')!=null&&!isNaN(v)&&v>=1&&v<=8)k.cols=v}   /* grid columns: ?cols=3 */
 if(q.has('page')){k.rotation=false;k.fit='shrink'}           /* a pinned view: one screen, that view */
 else if(has||k.rotation)k.fit='shrink';                     /* extra content must still fit: shrink */
 if(q.get('fit')=='scroll'||q.get('fit')=='shrink')k.fit=q.get('fit');   /* an explicit choice wins */
 return k};
const kioskPages=()=>{const k=kioskCfg();return 1+(k.alerts>1?1:0)+(k.backups>1?1:0)};
const kioskPage=()=>{const q=new URLSearchParams(location.search);
 if(q.get('page')!=null)return Math.max(0,Math.min(kioskPages()-1,+q.get('page')||0));
 return kioskPg%kioskPages()};
/* the header row: brand + big numbers + clock. a=alerts, k=kiosk settings.
   dense (default) packs five KPIs; spare keeps the classic four with bigger type. */
function kioskTop(M,H,order,I,a,k,pg,pages){
 const tok=((M.fleet||{}).series||{}).tokens||{};
 const tokSeries=Object.values(tok).reduce((s,x)=>s.concat(x||[]),[]);
 const tps=Object.values(tok).reduce((s,x)=>s+lastV(x),0);
 /* the hour's peak of the fleet total: bucket the per-model series by timestamp (the scrape
    grid is shared, so points at the same minute sum cleanly; a straggler bucket of one model
    under-reads and can never win the max against a full bucket) */
 const byT={};for(const[t,v]of tokSeries)byT[t]=(byT[t]||0)+(+v||0);
 const peak=Object.values(byT).reduce((m,v)=>v>m?v:m,0);
 const gpuW=order.reduce((s,n)=>s+(+(H[n]||{}).gpu_power||0),0);
 /* fleet GPU-watts history for the header graph: the API's own fleet series when it carries one,
    else the sum of each node's power sparkline bucketed by timestamp */
 const gpw=((M.fleet||{}).series||{}).gpu_power||(()=>{const byT={};
  for(const n of order)for(const[t,v]of (((H[n]||{}).spark||{}).gpu_power||[]))byT[t]=(byT[t]||0)+(+v||0);
  return Object.entries(byT).map(([t,v])=>[+t,v]).sort((x,y)=>x[0]-y[0])})();
 const act=(a.active||[]).length,crit=(a.active||[]).filter(x=>x.severity=='critical').length;
 /* fleet thermal ceiling: the hottest GPU anywhere, and its trend. The tiles show each node's
    own temp; this is the one number that says "something is running hot" from across the room,
    and it pairs with watts as the two physical-risk readings on the wall. */
 const tmaxSpk=((M.fleet||{}).series||{}).gpu_temp_max||(()=>{const byT={};
  for(const n of order)for(const[t,v]of (((H[n]||{}).spark||{}).gpu_temp||[]))byT[t]=Math.max(byT[t]||0,+v||0);
  return Object.entries(byT).map(([t,v])=>[+t,v]).sort((x,y)=>x[0]-y[0])})();
 const tmaxNow=order.reduce((m,n)=>Math.max(m,+(H[n]||{}).gpu_temp||0),0)||
  (tmaxSpk.length?lastV(tmaxSpk):null);
 const tmaxCls=tmaxNow==null?'':tmaxNow>=85?'bad':tmaxNow>=80?'warn':'';
 /* fleet vitals: saturation and the day's totals. Queue depth is the only saturation signal the
    tiles do not carry — a per-node card shows what it serves, not what is waiting behind it. */
 const qNow=lastV(((M.fleet||{}).series||{}).waiting)||0;
 const kwh=I.gpu_wh_24h!=null?I.gpu_wh_24h/1000:null;
 const tok24=I.tokens_24h;
 const llmN=(I.llm_up!=null&&I.llm_total!=null)?`${Math.round(I.llm_up)}/${Math.round(I.llm_total)}`:null;
 const llmBad=llmN&&I.llm_up<I.llm_total;
 const now=new Date(),tm=now.toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'}).match(/^([\d:.\s]+)(.*)$/)||['',now.toTimeString().slice(0,5),''];
 const dim=pgOn=>k.dense?'':(pgOn?'':' dim'), dn=()=>k.dense?' kdense':'';
 return `<div class=ktop><a class=kbrand href="/"><span class=mark></span><span class=wordmark>Noct<b>yx</b></span></a>
 <div class="kpair${pg==0?'':' dim'}" data-k=toks>
  <div class=ktopg>${chart('', Object.entries(tok).map(([m,pts])=>({name:m,pts,cls:'t'})), v=>v.toFixed(0)+' tok/s', {span:'1h',compact:1})}</div>
  <div class="kbig${dn()}"><b data-ghost="${ghost(tps.toFixed(0))}">${tps.toFixed(0)}</b><span>tokens /<br>second</span></div>
 </div>
 <div class="kpair${pg==0?'':' dim'}" data-k=watts>
  <div class=ktopg>${chart('', [{name:'fleet',pts:gpw,cls:'w'}], v=>Math.round(v)+' W', {span:'1h',compact:1})}</div>
  <div class="kbig${dn()}"><b data-ghost="${ghost(Math.round(gpuW))}">${Math.round(gpuW)}</b><span>GPU<br>watts</span></div>
 </div>
 <div class="kpair${pg==0?'':' dim'}" data-k=temp>
  <div class=ktopg>${chart('', [{name:'hottest',pts:tmaxSpk,cls:'c1'}], v=>Math.round(v)+' °C', {span:'1h',compact:1})}</div>
  <div class="kbig${dn()}"><b class="${tmaxCls}" data-ghost="${ghost(Math.round(tmaxNow||0))}">${tmaxNow!=null?Math.round(tmaxNow):'—'}</b><span>GPU temp<br>max</span></div>
 </div>
 <div class="kvitals${pg==0?'':' dim'}">
  <div class=kvital><b class="${qNow>2?'bad':qNow>0?'warn':''}" data-ghost="${ghost(Math.round(qNow))}">${Math.round(qNow)}</b><span>queued</span></div>
  <div class=kvital><b data-ghost="${ghost(kwh!=null?kwh.toFixed(1):'—')}">${kwh!=null?kwh.toFixed(1):'—'}</b><span>kWh / 24h</span></div>
  <div class=kvital><b data-ghost="${ghost(tok24!=null?kTok(tok24):'—')}">${tok24!=null?kTok(tok24):'—'}</b><span>tok / 24h</span></div>
 </div>
 ${k.dense&&llmN?`<div class="kbig${dn()}${pg==0?'':' dim'}" data-k=ok><b class="${llmBad?'warn':'ok'}" data-ghost="${ghost(llmN)}">${llmN}</b><span>LLM serving</span></div>`:''}
  <div class=kclock><b data-ghost="${ghost(tm[1].trim())}">${esc(tm[1].trim())}${tm[2].trim()?`<small>${esc(tm[2].trim())}</small>`:''}</b><span>${now.toLocaleDateString([], {weekday:'short',month:'short',day:'numeric'})}</span>${pages>1?`<span class=kpg>${pg+1} / ${pages}</span>`:''}</div></div>`}
/* one node card: metrics, meters, and EVERY serving model with its rate (an idle head-server
   slot no longer masks the model actually running on the node) */
function kioskNode(n,h){const srvs=((h.llm||{}).servers||[]).filter(s=>s.model||s.spark);
 const mem=h.mem_total?Math.round(100*h.mem_used/h.mem_total):null,up=h.up!==0&&h.up!==false;
 const t=(v,w)=>v==null?'—':`<b class="${v>=w?'bad':v>=w-10?'warn':''}">${fmtT(v)}</b>`;
 const sp=h.spark||{};
 /* every node now carries its own throughput: the live tok/s for the models it serves plus the
    last-hour total, and the 30-min generation trace under the GPU/CPU one (user, 2026-10-22). */
 const gen=srvs.reduce((a,s)=>a+((s.spark&&s.spark.gen_tps&&s.spark.gen_tps.length)?lastV(s.spark.gen_tps):(s.gen_tps||0)),0);
 const tok1h=srvs.reduce((a,s)=>a+(s.tokens_1h||0),0);
 const req1h=srvs.reduce((a,s)=>a+(s.requests_1h||0),0);
 const genspk=srvs.map(s=>s.spark&&s.spark.gen_tps).filter(p=>p&&p.length>1);
 /* prefill is a separate phase from decode: prompt tokens per second of prompt-processing time.
    It gets its own trace per endpoint so a burst of prompt work is visible next to generation
    (user, 2026-10-22). Prefill tok/s is orders of magnitude above decode, so it is drawn on its
    own scale against the tile's max, not overlaid on the generation trace's scale. */
 const pfsrv=srvs.map(s=>({model:s.model,pts:((s.spark&&s.spark.prefill_tps)||[]).filter(p=>p[1]!=null)})).filter(x=>x.pts.length>1);
 /* the trace's prompt line is prompt INPUT, not prefill speed: prompt tokens per second
    across the endpoint's requests (0 when idle, a jump when big prompts land), on its own
    zero-based scale — prompt volume runs far above generation. Prefill speed is nearly
    constant per model, so as a line it only rode the top edge; it stays a number below. */
 const ptspk=srvs.map(s=>((s.spark&&s.spark.prompt_tps)||[]).filter(p=>p[1]!=null)).filter(p=>p.length>1);
 const pfNow=srvs.reduce((a,s)=>a+(s.prefill_tps||0),0);
 /* a worker node serves no endpoint of its own: it runs tensor-parallel work for a head, so it
    has no tok/s of its own to report. Show what it IS doing (worker for X, tp N) rather than a
    bare dash, which read as "idle" (user, 2026-10-22: worker nodes don't offer toks). */
 const topo=((h.llm||{}).topology||[]);
 const wk=topo.find(t=>t.role=='worker');
 const hasTok=tok1h>0||req1h>0||gen>0;
 /* prefill readout: per endpoint, because prefill is a property of the endpoint's own prompt
    processing, not of the node. A dash when the endpoint is idle — a 0 tok/s prefill figure is
    misleading, since prefill only exists while a prompt is being processed. */
 const pfLine=srvs.length&&pfsrv.length?`<div class=kllm><span class=mut>${esc(pfsrv.map(x=>x.model||'—').join(', '))}</span><b>${pfNow>0?fmtK(pfNow):'—'}<small> prefill tok/s</small></b>
   <span class=mut>${pfNow>0?'prompt processing':'idle'}</span></div>`:'';
 const tpsLine=srvs.length?`<div class=kllm><span class=mut>${esc(srvs.map(s=>s.model||'—').join(', '))}</span><b>${hasTok?gen.toFixed(0):'—'}<small> tok/s</small></b>
   <span class=mut>${hasTok?`${fmtK(tok1h)} tok · ${Math.round(req1h)} req / 1h`:'no traffic / 1h'}</span></div>`
   :wk?`<div class=kllm><span class=mut>worker → ${esc(wk.worker_for||'head')}${wk.tp?' · tp '+esc(wk.tp):''}</span><b>—<small> tok/s</small></b>
     <span class=mut>${esc(wk.model||'tensor-parallel')}</span></div>`
   :`<div class=kllm><span class=mut>no model served</span><b>—<small> tok/s</small></b><span class=mut>idle</span></div>`;
 /* the 30-min trace fills the tile's middle: a 300x100 user-space box mapped exactly onto the
    measured pixels (none-scaling strokes keep the hairlines crisp); flat lines sit 2px above
    their label so the two never collide. The data window slides one step every tick without the
    box's endpoints moving, so the <svg> carries the window stamp: without it morph() sees an
    identical subtree and never touches the points, and the trace freezes (user, 2026-10-22) */
 const tr=(sp.gpu_util&&sp.gpu_util.length>1)?`<div class=ktrace><svg class=kspark viewBox="0 0 300 100" preserveAspectRatio="none" aria-hidden="true" data-win="${sp.gpu_util[0][0]}|${sp.gpu_util.at(-1)[0]}|pt${ptspk.length}">
   <line class="grid" x1="0" y1="48.3" x2="300" y2="48.3"/><line class="grid" x1="0" y1="2.3" x2="300" y2="2.3"/>${_lines([{pts:sp.gpu_util,cls:'a'},{pts:sp.cpu,cls:'g'},...genspk.map(p=>({pts:p,cls:'t'}))],300,96,100)}${ptspk.length?_lines(ptspk.map(p=>({pts:p,cls:'p'})),300,96,Math.max(...ptspk.flat().map(p=>p[1]))||1):''}</svg>
  <i class=a style="top:calc(2px + .3em)">GPU</i><i class=g style="bottom:2px">CPU</i>${genspk.length?'<i class=t style="bottom:calc(2px + 1.4em)">GEN TOK/S</i>':''}${ptspk.length?'<i class=p style="top:calc(2px + 1.4em)">PROMPT TOK/S</i>':''}</div>`:'';
 /* space left on each of the node's own disks (user, 2026-10-27): one row per device, / first */
 const dk=(h.disks||[]).map(d=>{const p=d.size?100*(1-(d.avail||0)/d.size):null;
  return `<span title="${esc(d.mount)}">${esc(d.mount.replace(/^\/Volumes\//,''))}</span>${bar(p,false,'disk')}<b class="${p>90?'bad':p>75?'warn':''}">${fmtBytes(d.avail)}<small> free</small></b>`}).join('');
 const disks=dk?`<div class=kdisks data-k=disk>${dk}</div>`:'';
 return `<div class="knode ${up?'':'down'}"><div class=kh><span class="dot ${up?'ok':'bad'}"></span><b>${esc(n)}</b>
   ${h.load1!=null?`<span class=kload>load ${(+h.load1).toFixed(1)}</span>`:''}</div>
  <div class=kgrid><span>GPU</span><b>${h.gpu_util!=null?Math.round(h.gpu_util)+'%':'—'}</b><span>GPU temp</span>${t(h.gpu_temp,85)}
   <span>power</span><b>${h.gpu_power!=null?Math.round(h.gpu_power)+' W':'—'}</b><span>CPU temp</span>${t(h.sys_temp,90)}
   <span>CPU</span><b>${h.cpu!=null?Math.round(h.cpu)+'%':'—'}</b><span>memory</span><b>${mem!=null?mem+'%':'—'}</b></div>
  ${tr}
  <div class=kmeters><span>GPU</span>${bar(h.gpu_util,true,'gpu')}<span>CPU</span>${bar(h.cpu,true,'cpu')}
   <span>MEM</span>${bar(mem,true,'mem')}<span>TEMP</span>${bar(h.gpu_temp!=null?h.gpu_temp/95*100:0,false,'temp')}</div>
  ${disks}${tpsLine}${pfLine}</div>`}
/* the alerts page (alerts: 2): name + host + since + summary + the LLM triage, newest-severity first */
function kioskAlerts(a){const L=(a.active||[]).slice().sort((x,y)=>({critical:0,warning:1,info:2})[x.severity]-({critical:0,warning:1,info:2})[y.severity]);
 if(!L.length)return '<div class=kdetail><div class="kticker kempty"><b class=ok>✓</b><span class=mut>no active alerts</span></div></div>';
 return `<div class=kdetail>${L.map(x=>`<div class="kcard ${x.severity}"><div class=kh><span class="dot ${x.severity=='critical'?'bad':x.severity=='warning'?'warn':'info'}"></span><b>${esc(x.name)}</b><span class=mut>${esc(x.host||'fleet')}</span><span class=hspacer></span><span class=mut>since ${esc((x.since||'').slice(0,16).replace('T',' '))}</span></div>
  <div class=ksum>${esc(x.summary||'')}</div>${x.triage?`<div class=ktri>${esc(x.triage)}</div>`:''}</div>`).join('')}</div>`}
/* the backups page (backups: 2): one row per host/profile — last run, next due, health */
async function kioskBackups(){let S={rows:[]};try{S=await fetchJSON('/api/v1/schedule')}catch(e){}
 const ago=t=>{if(!t)return '—';const s=(Date.now()/1000-t)/3600;return s<1?Math.round(s*60)+' min':s<48?Math.round(s)+' h':Math.round(s/24)+' d'};
 const nxt=t=>t?new Date(t*1000).toLocaleString([], {weekday:'short',hour:'2-digit',minute:'2-digit'}):'—';
 return `<div class=kdetail>${(S.rows||[]).map(r=>`<div class="kcard ${r.health=='ok'?'info':r.failed_at?'critical':'warning'}"><div class=kh><span class="dot ${r.health=='ok'?'ok':r.failed_at?'bad':'warn'}"></span><b>${esc(r.host)}</b><span class=mut>${esc(r.profile)}</span></div>
  <div class=kgrid style="grid-template-columns:auto 1fr auto 1fr"><span>last</span><b>${ago(r.last_ts)}</b><span>next</span><b>${nxt(r.next_ts)}</b><span>every</span><b>${r.every?Math.round(r.every/3600)+' h':'—'}</b><span>window</span><b>${esc(r.window||'—')}</b></div>
  ${r.why?`<div class=ksum>${esc(r.why)}</div>`:''}</div>`).join('')}</div>`}
let _kbHtml='',_kbAt=0;
async function kioskBackupsCached(){if(Date.now()-_kbAt>60000){_kbAt=Date.now();_kbHtml=await kioskBackups()}return _kbHtml}
function rKiosk(d){
 if(!d||!d.m)return '<div class=empty>loading…</div>';
 return kioskDraw(d,()=>kioskFit())}
/* the rotation pager asks for a redraw with the new page; the panel loop repaints itself
   every int seconds — this only matters if that tick hasn't come round yet */
function kioskRepaint(){const p=PANELS.find(x=>x.id=='kiosk');if(p&&p._data!==undefined)repaint('kiosk',p._data)}
/* rKiosk stays synchronous for morph(); when a page needs an extra fetch (the backups rows),
   kioskDraw paints the rest now and patches the detail area in, then refits. */
function kioskDraw(d,done){
 const M=d.m,H=M.hosts||{},order=M.order||Object.keys(H),I=(M.fleet||{}).infra||{};
 const k=kioskCfg(),pages=kioskPages();let pg=Math.min(kioskPage(),pages-1);
 /* the recipe/slot row is gone (user, 2026-10-22): the wall is nodes only, and every node
    carries its own trace + throughput. Slot state still drives the per-node LLM readout via
    H[n].llm below, so nothing is lost — it just stops taking a third of the wall. */
 const top=kioskTop(M,H,order,I,d.a||{},k,pg,pages);
 let main='',extra=null;
 if(pg==1&&k.alerts>1)main=kioskAlerts(d.a||{});
 else if(pg==2&&k.backups>1){extra=kioskBackupsCached().then(html=>{const el=document.getElementById('kdetail');if(el)el.innerHTML=html;kioskFit()});
  main='<div class=kdetail id=kdetail></div>'}
 else{const nodes=order.map(n=>kioskNode(n,H[n]||{})).join('');
  main=`<div class=knodes>${nodes}</div>`}
 const out=`<div class="kiosk-wrap${k.fit=='scroll'?' kfit-scroll':''}" data-page="${pg}" data-pages="${pages}">${top}${main}</div>`;
 if(done)done();
 if(extra)extra.catch(()=>{});
 return out}

function llmStats(s){const ms=v=>v==null?'—':v<1?Math.round(v*1000)+'<span class=u> ms</span>':v.toFixed(1)+'<span class=u> s</span>';
 const kv=(k,v)=>`<div><span class=k>${k}</span><span class=v>${v}</span></div>`;
 return `<div class=llms>
  ${kv('running',`${s.running??0}${s.waiting?' <span class=warn>+'+s.waiting+'</span>':''}`)}
  ${kv('throughput',`${Math.round(s.gen_tps||0)}<span class=u> tok/s</span>`)}
  ${kv('decode',s.decode_tps!=null?`${Math.round(s.decode_tps)}<span class=u> tok/s</span>`:'—')}
  ${kv('prefill',s.prefill_tps!=null?`${esc(fmtK(s.prefill_tps))}<span class=u> tok/s</span>`:'—')}
  ${kv('prefill time',ms(s.prefill_p50))}
  ${kv('prefill / decode',s.prefill_share!=null?`${Math.round(s.prefill_share)}<span class=u>%</span> / ${Math.round(100-s.prefill_share)}<span class=u>%</span>`:'—')}
  ${kv('in → out',s.prompt_tok_p50!=null?`${esc(fmtK(s.prompt_tok_p50))} → ${esc(fmtK(s.gen_tok_p50||0))}`:'—')}
  ${kv('queue',ms(s.queue_p50))}
  ${kv('ttft p50 · p95',`${ms(s.ttft_p50)} · ${ms(s.ttft_p95)}`)}
  ${kv('e2e p50',ms(s.e2e_p50))}
  ${kv('inter-token',s.itl_p50!=null?Math.round(s.itl_p50*1000)+'<span class=u> ms</span>':'—')}
  ${kv('kv cache',`<span class="${s.kv_pct>90?'bad':s.kv_pct>70?'warn':''}">${s.kv_pct!=null?Math.round(s.kv_pct):'—'}<span class=u>%</span></span>`)}
  ${kv('last hour',`${Math.round(s.requests_1h||0)}<span class=u> req</span> · ${esc(fmtK(s.tokens_1h||0))}`)}
  ${kv('prefix hit',`${s.prefix_hit!=null?Math.round(s.prefix_hit):'—'}<span class=u>%</span>${(s.preempt_rate||0)>0?' <span class=warn>preempt</span>':''}`)}
 </div>`}
function rControl(m){
 if(!m)return '<div class=empty>loading…</div>';
 const H=m.hosts||{},ord=m.order||[],names=ord.filter(n=>H[n]).concat(Object.keys(H).filter(n=>!ord.includes(n)&&H[n].up!=null));
 const F=m.fleet||{},S=F.series||{},I=F.infra||{};
 let tps=0,run=0,wait=0,models=0,gpuW=0;
 names.forEach(n=>{const L=(H[n]||{}).llm||{};(L.servers||[]).filter(x=>x.model).forEach(x=>{tps+=x.gen_tps||0;run+=x.running||0;wait+=x.waiting||0;models++});gpuW+=(H[n]||{}).gpu_power||0});
 const tokAll={};Object.values(S.tokens||{}).forEach(pts=>pts.forEach(([t,v])=>{tokAll[t]=(tokAll[t]||0)+(v||0)}));
 const tokPts=Object.entries(tokAll).map(([t,v])=>[+t,v]).sort((a,b)=>a[0]-b[0]);
 const upN=names.filter(n=>H[n].up).length;
 const ms=v=>v==null?'—':v<1?Math.round(v*1000)+'<span class=u>ms</span>':v.toFixed(1)+'<span class=u>s</span>';
 const kv=(k,v)=>`<div><span class=k>${k}</span><span class=v>${v}</span></div>`;
 const head=`<div class=hero>
  <div class=hv data-ghost="${ghost(Math.round(tps))}">${Math.round(tps)}</div>
  <div class=hl>tokens / second across the fleet<br><b>${models}</b> model${models==1?'':'s'} serving on <b>${upN}</b> of ${names.length} nodes${I.llm_total&&I.llm_up<I.llm_total?` · <span class=bad>${I.llm_total-I.llm_up} endpoint down</span>`:''}</div>
  <div class=stats>
   ${statTile('requests',run,' running',`${wait} waiting`,S.requests)}
   ${statTile('gpu power',Math.round(gpuW),' W',I.gpu_wh_24h!=null?(I.gpu_wh_24h/1000).toFixed(1)+' kWh / 24 h':'',S.gpu_power)}
   ${statTile('generated, 24 h',esc(fmtK(I.tokens_24h)),' tok',`${Math.round(I.requests_24h||0)} requests`,tokPts)}
   ${statTile('network',esc(fmtBits((S.net_tx&&S.net_tx.length?S.net_tx[S.net_tx.length-1][1]:0))),'',`↓ ${esc(fmtBits(S.net_rx&&S.net_rx.length?S.net_rx[S.net_rx.length-1][1]:0))}`,S.net_tx)}
   ${statTile('health',`${I.backups_ok??'—'}<span class=u>/${I.backups_total??'—'}</span>`,'',`backups · ${I.alerts?'<a class="warn go" href="/#panel=alerts" onclick="event.stopPropagation()" style="color:var(--warn)">'+I.alerts+' alert'+(I.alerts>1?'s':'')+' →</a>':'no alerts'} · raid ${(I.raid||[]).filter(r=>r.array=='md2').map(r=>r.active+'/'+((I.raid_total||{})[r.array]||'?')).join('')||'—'}${(I.raid_sync||{}).md2!=null?' <span class=warn>'+Math.round(I.raid_sync.md2)+'%</span>':''}`)}
  </div></div>`;
 const rows=names.map(n=>{const M=H[n],L=M.llm||{},sv=(L.servers||[]).filter(x=>x.model),s0=sv[0],sp=M.spark||{};
  const memP=M.mem_total?100*M.mem_used/M.mem_total:0,state=M.up===0?'bad':(M.failed_units||M.reboot||(s0&&s0.kv_pct>90))?'warn':'ok';
  const gcls=v=>v>90?'bad':v>75?'warn':'';
  const tcls=v=>v==null?'':v>=88?'bad':v>=78?'warn':'',deg=v=>v!=null?fmtT(v,1):'';
  const ttl=`cpu/soc ${fmtT(M.sys_temp)} · gpu ${fmtT(M.gpu_temp)}${M.nvme_temp!=null?' · nvme '+fmtT(M.nvme_temp):''}`;
  const hy=M.hygiene||{};
  const flags=[M.failed_units?goA({host:n,panel:'hygiene'},'warn',M.failed_units+' failed','failed units — fix in Hygiene'):'',M.reboot?goA({host:n,panel:'hygiene'},'warn','reboot','reboot required — Hygiene'):'',M.up===0?goA({host:n,panel:'detail',tab:'health'},'bad','no metrics','exporter down — Machine Report › health'):'',L.up===0&&sv.length?goA({host:n,panel:'detail',tab:'llm'},'bad','llm down','Machine Report › llm'):'',hy.items?goA({host:n,panel:'hygiene'},hy.warnings?'warn':'',hy.items+' hyg',hy.items+' housekeeping findings — Hygiene'):''].join('');
  const stats=s0?`<div class=nst>
    ${kv('run · wait',`${s0.running??0} · ${s0.waiting??0}`)}
    ${kv('throughput',`${Math.round(s0.gen_tps||0)}<span class=u> tok/s</span>`)}
    ${kv('decode',s0.decode_tps!=null?`${Math.round(s0.decode_tps)}<span class=u> tok/s</span>`:'—')}
    ${kv('prefill',s0.prefill_tps!=null?`${esc(fmtK(s0.prefill_tps))}<span class=u> tok/s</span> · ${ms(s0.prefill_p50)}`:'—')}
    ${kv('ttft · e2e',`${ms(s0.ttft_p50)} · ${ms(s0.e2e_p50)}`)}
    ${kv('in → out',s0.prompt_tok_p50!=null?`${esc(fmtK(s0.prompt_tok_p50))} → ${esc(fmtK(s0.gen_tok_p50||0))}`:'—')}
    ${kv('queue · itl',`${ms(s0.queue_p50)} · ${s0.itl_p50!=null?Math.round(s0.itl_p50*1000)+'<span class=u>ms</span>':'—'}`)}
    ${kv('last hour',`${Math.round(s0.requests_1h||0)}<span class=u> req</span> · ${esc(fmtK(s0.tokens_1h||0))}`)}
    ${kv('prefix · kv',`${s0.prefix_hit!=null?Math.round(s0.prefix_hit):'—'}<span class=u>%</span> · <span class="${gcls(s0.kv_pct)}">${s0.kv_pct!=null?Math.round(s0.kv_pct):'—'}<span class=u>%</span></span>`)}
   </div>`
   :L.worker_for?`<div class="nst wk">
    <div class=full><span class=k>tp model</span><span class=v>${esc(L.worker_model||'—')}</span></div>
    ${kv('fabric',`↑${esc(fmtBits(M.fabric_tx))} ↓${esc(fmtBits(M.fabric_rx))}`)}
    ${kv('net',`↑${esc(fmtBits(M.net_tx))} ↓${esc(fmtBits(M.net_rx))}`)}
    ${kv('disk io',`${esc(fmtBytes(M.disk_io))}<span class=u>/s</span>`)}
    ${kv('root',`${Math.round(M.root_pct||0)}<span class=u>%</span>`)}
   </div>`
   :`<div class="nst wk">${kv('llm','none')}${kv('disk io',`${esc(fmtBytes(M.disk_io))}<span class=u>/s</span>`)}${kv('root',`${Math.round(M.root_pct||0)}<span class=u>%</span>`)}</div>`;
  return `<div class="nrow ${state}" onclick="location.href='/#'+encodeURIComponent('${esc(n)}')" title="open in the dashboard" style="cursor:pointer">
   <div class=nid><div class=nm><span class="dot ${M.up?'ok':''}"></span>${esc(n)}${flags?`<span class=fl>${flags}</span>`:''}<span class=tjob>${M.gpu_power!=null?Math.round(M.gpu_power)+' W':''}</span></div>
    <div class=md title="${esc(s0?s0.model:L.worker_model||'')}">${s0?esc(s0.model):L.worker_for?'worker for '+esc(L.worker_for):'—'}</div>
    <div class=md>load ${M.load1??'—'}${M.containers?' · '+M.containers+' ctr':''}</div></div>
   <div class=nrings title="${esc(ttl)}">
    ${ring(M.gpu_util,'gpu',`${M.gpu_util!=null?Math.round(M.gpu_util)+'%':'—'}`,'a',`gpu <b class="${tcls(M.gpu_temp)}">${deg(M.gpu_temp)}</b>`)}
    ${ring(M.cpu,'cpu',`${M.cpu!=null?Math.round(M.cpu)+'%':'—'}`,gcls(M.cpu)||'',`cpu <b class="${tcls(M.sys_temp)}">${deg(M.sys_temp)}</b>`)}
    ${ring(memP,'mem',`${M.mem_used!=null?Math.round(M.mem_used/1e9)+'G':'—'}`,'')}
    ${s0?ring(s0.kv_pct,'kv',`${s0.kv_pct!=null?Math.round(s0.kv_pct)+'%':'—'}`,gcls(s0.kv_pct)||'ok'):ring(M.root_pct,'root',`${M.root_pct!=null?Math.round(M.root_pct)+'%':'—'}`,gcls(M.root_pct)||'ok')}
   </div>
   ${stats}
   <div class=nch>
    ${chart('cpu · gpu',[{name:'cpu',pts:sp.cpu,cls:'c0'},{name:'gpu',pts:sp.gpu_util,cls:'a'}],pct,{max:100,span:'30m',compact:1})}
    ${s0?chart('tokens/s',[{pts:(s0.spark||{}).gen_tps,cls:slotOf(s0.model)}],v=>Math.round(v)+' tok/s',{span:'30m',compact:1})
        :chart('network',[{name:'↑',pts:sp.net_tx,cls:'g'},{name:'↓',pts:sp.net_rx,cls:'c0'}],fmtBits,{span:'30m',compact:1})}
    ${s0?chart('network',[{name:'↑',pts:sp.net_tx,cls:'g'},{name:'↓',pts:sp.net_rx,cls:'c0'}],fmtBits,{span:'30m',compact:1})
        :L.worker_for?chart('fabric (200G)',[{name:'↑',pts:sp.fabric_tx,cls:'g'},{name:'↓',pts:sp.fabric_rx,cls:'c0'}],fmtBits,{span:'30m',compact:1})
        :chart('memory',[{pts:sp.mem_used,cls:'c6'}],fmtBytes,{max:M.mem_total||undefined,span:'30m',compact:1})}
   </div></div>`}).join('');
 const roster=(F.roster||[]).map(r=>`<span><b>${esc(r.model||'?')}</b> ${esc(r.host)}${r.workers&&r.workers.length?'+'+esc(r.workers.join('+')):''}${r.tp?' · tp'+r.tp:''}${r.max_model_len?' · '+esc(fmtK(r.max_model_len))+' ctx':''}${r.kv_tokens!=null?' · '+esc(fmtK(r.kv_tokens))+' kv':''}${r.gpu_mem_util!=null?' · '+Math.round(r.gpu_mem_util*100)+'% gpu mem':''}${r.started_ts?' · up '+esc(fmtDur(Date.now()/1000-r.started_ts)):''}</span>`).join('');
 const hot=k=>names.filter(n=>H[n][k]!=null).sort((a,b)=>H[b][k]-H[a][k])[0];
 const hotS=[['sys_temp','cpu'],['gpu_temp','gpu'],['nvme_temp','nvme']].map(([k,l])=>{const n=hot(k);if(!n)return '';const v=H[n][k];
   return `${l} <b class="${v>=88?'bad':v>=78?'warn':''}">${fmtT(v,1)}</b> ${esc(n)}`}).filter(Boolean).join(' · ');
 const foot=`<div class=foot>${roster}${hotS?`<span>hottest ${hotS}</span>`:''}<span>${(window.NX_CFG||{}).nas||'nas'} <b>${I.nas_free!=null?esc(fmtBytes(I.nas_free)):'—'}</b> free · repo endpoint <b class="${I.rest_up?'ok':'bad'}">${I.rest_up?'up':'DOWN'}</b> · exporters <b>${I.exporters_up??'?'}/${I.exporters_total??'?'}</b> · llm endpoints <b>${I.llm_up??'?'}/${I.llm_total??'?'}</b> · backups <b>${I.backups_ok??'?'}/${I.backups_total??'?'}</b> · alerts <b class="${I.alerts?'warn':'ok'}">${I.alerts??0}</b></span></div>`;
 return `<div class=ctl>${head}${rows}${foot}</div>`}
function rNodes(m){
 if(!m)return '<div class=empty>loading…</div>';
 const H=m.hosts||{},order=m.order||(OVR&&OVR.hosts||[]).map(h=>h.name);
 const names=order.filter(n=>H[n]).concat(Object.keys(H).filter(n=>!order.includes(n)&&H[n].up!=null));
 return `<div class=tiles style="grid-template-columns:repeat(auto-fill,minmax(min(100%,340px),1fr))">`+names.map(n=>{
  const M=H[n],L=M.llm||{},sv=(L.servers||[]).filter(s=>s.model),s0=sv[0],sp=M.spark||{};
  const memP=M.mem_total?100*M.mem_used/M.mem_total:0;
  const state=M.up===0?'bad':(M.failed_units||M.reboot||(s0&&s0.kv_pct>90))?'warn':'ok';
  const gcls=v=>v>90?'bad':v>75?'warn':'';
  return `<div class="tile nc ${state}" onclick="location.href='/#'+encodeURIComponent('${esc(n)}')" title="open in the dashboard">
   <div class=nm><span class="dot ${M.up?'ok':''}"></span>${esc(n)}<span class=tjob>${M.gpu_power!=null?Math.round(M.gpu_power)+' W':''}</span></div>
   <div class=sub>${s0?'<b style="color:var(--fg)">'+esc(s0.model)+'</b>'+(L.up===0?' <span class="tag bad">down</span>':''):L.worker_for?'TP worker for <b>'+esc(L.worker_for)+'</b>'+(L.worker_model?' · '+esc(L.worker_model):''):'no LLM server'}${M.load1!=null?' · load '+M.load1:''}</div>
   <div class=rings>
    ${ring(M.gpu_util,'gpu',`${M.gpu_util!=null?Math.round(M.gpu_util)+'%':'—'}<small>${M.gpu_temp!=null?fmtT(M.gpu_temp):''}</small>`,'a')}
    ${ring(M.cpu,'cpu',`${M.cpu!=null?Math.round(M.cpu)+'%':'—'}<small>load ${M.load1??'—'}</small>`,gcls(M.cpu)||'')}
    ${ring(memP,'memory',`${M.mem_used!=null?Math.round(M.mem_used/1e9)+'G':'—'}<small>of ${Math.round((M.mem_total||0)/1e9)}G</small>`,'')}
    ${s0?ring(s0.kv_pct,'kv cache',`${s0.kv_pct!=null?Math.round(s0.kv_pct)+'%':'—'}<small>${s0.running??0} run</small>`,gcls(s0.kv_pct)||'ok')
        :ring(M.root_pct,'root',`${M.root_pct!=null?Math.round(M.root_pct)+'%':'—'}<small>${esc(fmtBytes(M.disk_io))}/s</small>`,gcls(M.root_pct)||'ok')}
   </div>
   ${sv.map(s=>llmStats(s)).join('')}${!sv.length&&L.worker_for?`<div class=mut style="font:10.5px var(--mono);margin:4px 0">serves half of ${esc(L.worker_model||'the model')} for ${esc(L.worker_for)} — request stats are on that node</div>`:''}
   <div class=charts>
    ${chart('cpu',[{pts:sp.cpu}],pct,{max:100,span:'30m'})}
    ${chart('gpu',[{pts:sp.gpu_util,cls:'a'}],pct,{max:100,span:'30m'})}
    ${chart('network',[{name:'↑',pts:sp.net_tx,cls:'g'},{name:'↓',pts:sp.net_rx,cls:'c0'}],fmtBits,{span:'30m'})}
    ${s0?chart('tokens/s',[{pts:(s0.spark||{}).gen_tps,cls:slotOf(s0.model)}],v=>Math.round(v)+' tok/s',{span:'30m'})
        :chart('requests',[{pts:[]}],v=>v,{span:'30m'})}
   </div>
   <div class=flags>${s0?`<span class=tag>root ${Math.round(M.root_pct||0)}%</span>`:''}${M.disk_io!=null?`<span class=tag>io ${esc(fmtBytes(M.disk_io))}/s</span>`:''}${M.failed_units?`<span class="tag warn">${M.failed_units} failed unit${M.failed_units>1?'s':''}</span>`:''}${M.reboot?'<span class="tag warn">reboot needed</span>':''}${M.containers?`<span class=tag>${M.containers} ctr</span>`:''}${s0&&s0.ttft_p50!=null?`<span class=tag>ttft ${s0.ttft_p50<1?Math.round(s0.ttft_p50*1000)+' ms':s0.ttft_p50.toFixed(1)+' s'}</span>`:''}</div>
  </div>`}).join('')+'</div>'}
function loadLayout(){try{return JSON.parse(localStorage.getItem(LS))||{}}catch(e){return {}}}
function saveLayout(){const L=Object.assign({},loadLayout());
 L.order=PANELS.map(p=>p.id);
 for(const p of PANELS){const el=$('p-'+p.id);if(!el)continue;
  const sp=parseInt(el.style.gridColumn.replace(/\D/g,''))||p.sp;
  // height is only rewritten while expanded — a collapsed panel (64px) must
  // not clobber the height it restores to when reopened
  const prev=L[p.id]||{};
  L[p.id]={sp, h: !p.h?0 : el.classList.contains('collapsed') ? (prev.h||p.h) : el.offsetHeight,
              c: el.classList.contains('collapsed')}}
 localStorage.setItem(LS,JSON.stringify(L))}
function layoutReset(){localStorage.removeItem(LS);location.reload()}

function applyGrid(el,sp){el.style.gridColumn=`span ${clamp(sp,1,12)}`}

function buildPanels(){
 const desk=$('desk'),L=loadLayout();desk.innerHTML='';
 const order=(L.order||[]).filter(id=>PANELS.some(p=>p.id==id));
 const list=order.map(id=>PANELS.find(p=>p.id==id))
  .concat(PANELS.filter(p=>!order.includes(p.id)));
 for(const p of list){
  const l=L[p.id]||{};
  const el=document.createElement('div');el.className='panel';el.id='p-'+p.id;
  applyGrid(el,l.sp||p.sp);
  if(p.h)el.style.height=Math.max(l.h||p.h,120)+'px';else{   /* clamp: a stale/tiny saved height must not hide the body */el.classList.add('auto');el.style.removeProperty('height');el.style.removeProperty('width')}
  if(l.c)el.classList.add('collapsed');
  el.innerHTML=`<div class=grip><span class=dots><i></i><i></i><i></i><i></i></span>
   <h2>${p.title}</h2><span class=st id="st-${p.id}"></span>
   <button class=mini title=collapse onclick="toggleP('${p.id}')">${l.c?'▸':'▾'}</button></div>
   <div class=pbody id="b-${p.id}"><div class=empty>loading…</div></div>`;
  desk.appendChild(el);
  dragify(el,el.querySelector('.grip'));
 }
}
function toggleP(id){const el=$('p-'+id);el.classList.toggle('collapsed');
 const c=el.classList.contains('collapsed');el.querySelector('.mini').textContent=c?'▸':'▾';
 const ph=PANELS.find(x=>x.id==id).h;el.style.height=c?'64px':ph?Math.max((loadLayout()[id]||{}).h||ph,120)+'px':'';
 if(!c){const p=PANELS.find(x=>x.id==id);if(p._data!==undefined)repaint(id,p._data);else tick(p)}
 saveLayout()}
/* drag to REORDER: the dragged panel ghosts; dropping over another panel
   inserts before/after it depending on cursor position. The grid re-snaps. */
function dragify(el,grip){
 grip.addEventListener('pointerdown',e=>{
  if(e.target.closest('button'))return;
  e.preventDefault();
  const desk=$('desk');
  el.classList.add('dragging');
  let target=null,before=false;
  try{grip.setPointerCapture(e.pointerId)}catch(_){}
  const clear=()=>{document.querySelectorAll('.drop-before,.drop-after')
   .forEach(n=>n.classList.remove('drop-before','drop-after'));target=null};
  const move=ev=>{
   clear();
   const under=document.elementFromPoint(ev.clientX,ev.clientY);
   const t=under&&under.closest?under.closest('.panel'):null;
   if(t&&t!=el){
    const r=t.getBoundingClientRect();
    before=(ev.clientX-r.left)<r.width/2;
    target=t;t.classList.add(before?'drop-before':'drop-after');
   }
  };
  const up=ev=>{
   grip.removeEventListener('pointermove',move);grip.removeEventListener('pointerup',up);
   grip.removeEventListener('pointercancel',up);
   el.classList.remove('dragging');
   if(target){
    // honor the before/after computed by the last move (already correct for
    // the CURRENT target; recomputing from pointerup coords can disagree)
    if(before)target.before(el);else target.after(el)}
   clear();
   // keep PANELS order in sync with DOM order, then persist
  PANELS.sort((a,b)=>{const A=$('p-'+a.id),B=$('p-'+b.id);
    return A&&B?(A.compareDocumentPosition(B)&Node.DOCUMENT_POSITION_FOLLOWING?-1:1):0});
   saveLayout()};
  grip.addEventListener('pointermove',move);grip.addEventListener('pointerup',up);grip.addEventListener('pointercancel',up);
 });
 /* resize (native corner, vertical only — width is the grid's to own):
    persist the new height */
 el.addEventListener('mouseup',()=>{
  if(el.classList.contains('collapsed'))return;
  saveLayout();
 });
}

async function fetchJSON(u,auth){const h={'Accept':'application/json'};if(auth)h.Authorization='Bearer '+(localStorage.getItem(LS_TOK)||'');
 const r=await fetch(u,{headers:h});
 buildCheck(r);
 if(r.status==401&&ME&&ME.mode=='accounts'){location.href='/login?next='+encodeURIComponent(location.pathname+location.hash);throw new Error('sign in')}
 if(!r.ok)throw new Error(u+' -> '+r.status);return r.json()}
/* the server stamps every response with its build; a page from an older build reloads itself once
   (otherwise a tab left open across a UI restart keeps running stale code with no hint) */
function buildCheck(r){const b=r.headers.get('X-CM-Build');if(!b||b==BUILD)return;
 let last='';try{last=sessionStorage.getItem('noctyx_reloaded_for')||''}catch(e){}
 if(last==b)return;                                   /* already reloaded for this build: don't loop */
 try{sessionStorage.setItem('noctyx_reloaded_for',b)}catch(e){}
 location.reload()}

async function tick(p){
 if(!p||p.int<=0)return;          /* a panel this page does not have (an action refreshes the dashboard's) */
 if(p.id=='schedule'&&EDIT)return;
 if(p.id=='recipes'&&(RCJOB&&!RCJOB.done||document.activeElement&&document.activeElement.closest&&document.activeElement.closest('#b-recipes')&&/INPUT|SELECT/.test(document.activeElement.tagName)))return;
 if(p.id=='config'&&(CFGDIRTY||(CFGJOB&&/running|starting/.test(CFGJOB.text.split('\n')[0]))))return;
 const el=$('p-'+p.id);if(!el||el.classList.contains('collapsed'))return;
 const body=$('b-'+p.id);
 const slow=(body&&p._data!==undefined)?setTimeout(()=>body.classList.add('stale'),1200):0;
 try{
  const data=await p.api();p._data=data;repaint(p.id,data);p._painted=true;
  const st=$('st-'+p.id);if(st&&data){st.textContent=ago(data.ts||Date.now()/1000);st.removeAttribute('title');st.classList.add('live')}
 }catch(e){if(body&&p._painted)body.classList.add('stale');
  /* the reason is one hover away: a renderer exception or a failed fetch, with where it happened */
  const why=String((e&&e.stack)||e).split('\n').slice(0,2).join(' ').slice(0,300);
  console.error('panel '+p.id+':',e);
  const st=$('st-'+p.id);if(st){st.textContent='err';st.title=why;st.classList.remove('live')}
  if(body&&!p._painted&&VIEW!='kiosk')body.innerHTML=`<div class="empty bad">panel failed: ${esc(why.split(' at ')[0])}<br><span class=mut>details on the “err” pill · retries in ${p.int}s</span></div>`}
 finally{clearTimeout(slow)}
}
/* in-place DOM patch: the renderer stays a pure string function, but the live tree is
   diffed against the new markup and only changed text/attributes are touched. No teardown,
   no re-layout of untouched charts, no flash — and scroll positions/focus survive a tick. */
function morph(a,b){
 if(a.nodeType!==b.nodeType||a.nodeName!==b.nodeName){a.replaceWith(b);return}
 if(a.nodeType===3||a.nodeType===8){if(a.data!==b.data)a.data=b.data;return}
 if(a.nodeType!==1)return;
 for(const at of Array.from(a.attributes))if(!b.hasAttribute(at.name))a.removeAttribute(at.name);
 for(const at of Array.from(b.attributes))if(a.getAttribute(at.name)!==at.value)a.setAttribute(at.name,at.value);
 const tn=a.nodeName;
 if((tn==='INPUT'||tn==='TEXTAREA'||tn==='SELECT')&&document.activeElement!==a&&a.value!==b.value)a.value=b.value;
 if(tn==='TEXTAREA'||tn==='SCRIPT'||tn==='STYLE'){if(a.textContent!==b.textContent)a.textContent=b.textContent;return}
 morphKids(a,b)}
function morphKids(a,b){const ac=a.childNodes,bc=Array.from(b.childNodes);
 for(let i=0;i<bc.length;i++){const x=ac[i],y=bc[i];
  if(!x){a.appendChild(y);continue}
  if(x.nodeType===1&&y.nodeType===1&&(x.id||y.id)&&x.id!==y.id){x.replaceWith(y);continue}
  morph(x,y)}
 while(ac.length>bc.length)a.removeChild(a.lastChild)}
function repaint(id,data){const p=PANELS.find(x=>x.id==id),b=$('b-'+id);if(!b||!p)return;
  /* the kiosk's fit lives in inline styles on the rendered tree, and morph() clears style
     attributes the fresh markup doesn't carry — snapshot them and put them back. But not
     across a page switch (rotation): the old page's column/row fit must not squash the new one. */
  const prevW=b.querySelector('.kiosk-wrap'),prevPg=prevW?prevW.dataset.page:null;
  const pageSw=id=='kiosk'&&prevPg!=null&&prevPg!=String(kioskPage());
  const fit=id=='kiosk'&&!pageSw?kioskFitSnap(b):null;
 if(id=='kiosk'&&_kfSaved)kfStyle(_kfSaved);    /* morph() can't restore <html>'s class: the step it
                                                   chose would evaporate on the next 10s refresh */
 const t=document.createElement('template');t.innerHTML=p.render(data)||'<div class=empty>no data</div>';
 morphKids(b,t.content);b.classList.remove('stale');
 /* morph matches children positionally: a kiosk page whose markup shifted (a page change, an
    alert cleared) is a different screen — swap wholesale, then let the fit re-solve */
 if(id=='kiosk'){const cur=b.firstElementChild,nw=t.content.firstElementChild;
  if(cur&&nw&&(cur.className!==nw.className||(cur.dataset.page||'')!==(nw.dataset.page||'')))b.replaceChildren(nw)}
 if(id=='kiosk'){kioskFitRestore(b,fit);kioskFit()}
 for(const k in CH)if(!document.querySelector(`[data-ch="${k}"]`))delete CH[k];
 if(id=='actions')wireActions()}
function kioskFitSnap(b){const w=b.querySelector('.kiosk-wrap'),n=b.querySelector('.knodes');
 return w||n?{wrap:w&&w.style.transform,boxW:w&&w.style.width,boxH:w&&w.style.height,nodes:n&&n.getAttribute('style'),cls:n&&n.className,page:w&&w.dataset.page,pages:w&&w.dataset.pages,kf:_kfSaved}:null}
function kioskFitRestore(b,f){if(!f)return;
 const w=b.querySelector('.kiosk-wrap'),n=b.querySelector('.knodes');
 if(w&&f.wrap!=null)w.style.transform=f.wrap;
 if(w&&f.boxW!=null)w.style.width=f.boxW;        /* the box goes with the transform: scale() on a */
 if(w&&f.boxH!=null)w.style.height=f.boxH;       /* 100vw wrap would paint a corner of the screen */
 if(w&&f.page!=null)w.dataset.page=f.page;
 if(w&&f.pages!=null)w.dataset.pages=f.pages;
 if(f.kf)kfStyle(f.kf);                          /* the step <html> lost with the old tree */
 if(n&&f.nodes!=null)n.setAttribute('style',f.nodes);
 if(n&&f.cls)n.className=f.cls}
/* /kiosk: everything on one screen, always. The wrap is exactly the viewport (body overflow:hidden)
   and kioskFit() packs the node grid into the height left under the header and the slots: for each
   (type step, column count) it measures the cards' intrinsic height in a hidden in-DOM clone of the
   wrap — the live grid can't be measured (its rows are stretched by the flex container) and a bare
   detached tree resolves no inherited --kf-h and none of the theme selectors — then keeps the
   roomiest step whose rows fit full-size, shares the leftover height between them, and falls back
   to scaling the whole wrap when even the tightest step overflows. The three type steps
   come from --kf-h: all the kiosk CSS above derives from it, and kfStyle steps the ROOT — morph()
   can patch neither body's class list nor <html>, and a root class inherits into the probe's clone
   too. One fit per frame; if the applied tree still overflows its rows (a late theme class, a font
   load, a repaint mid-solve), the settled check refits, guard-bounded to converge. */
const KF={1:.78,2:.62};
/* The wall is painted at scale S: the wrap is laid out in a box of viewport/S and
   transform:scale(S)d from the top left, so it always covers exactly the viewport — type,
   gaps and the rows the solver measured all scale together. S = T x shrink:
   - T, the type scale, grows the wall on viewports taller than 1080p (a 4K panel at 1x, a
     zoomed-out browser), where fixed-px type is tiny on the physical screen. The smaller
     dimension sets it: a wall's room is its height. ?kf= pins it per browser.
   - shrink (< 1) is the last resort when even the smallest type step overflows. It widens
     and heightens the box by the same factor, so the shrunk wall still fills the screen
     (scaling a viewport-sized box would leave it in the top-left corner). */
const kioskT=(w,h)=>{const q=new URLSearchParams(location.search);
 if(q.get('kf'))return Math.max(.5,Math.min(3,+q.get('kf')||1));
 return Math.max(1,Math.min(2.2,Math.min(w,h)/1080))};
function kioskBox(wrap,S){wrap.style.width=(innerWidth/S)+'px';wrap.style.height=(innerHeight/S)+'px';
 wrap.style.transformOrigin='top left';wrap.style.transform=S==1?'':`scale(${S.toFixed(4)})`}
let _kfSaved=0,_fitPend=0,_fitPass=0,_lastFit=null;
function kfStyle(s){_kfSaved=s;document.documentElement.classList.toggle('kiosk-1',s==1);document.documentElement.classList.toggle('kiosk-2',s==2)}
function kioskFit(){
if(_fitPend||!document.querySelector('.kiosk-wrap'))return;   /* not on /kiosk: nothing to size */
_fitPend=true;
/* the fit is inline styles on a tree we morph in place: re-solving it before the browser has
   painted the morphed tree compares a half-updated layout and writes back a different answer
   every few ticks — a 4px reflow of the whole wall, which reads as a flash. One settled paint
   of the new markup is enough; two rAFs guarantee the frame between them gets painted. */
requestAnimationFrame(()=>requestAnimationFrame(()=>{setTimeout(()=>{_fitPend=false;kioskFitNow();
 /* one follow-up pass in the same task settles the shrink↔scroll feedback: a fit that shrinks
    type also shrinks the content it then has to fit, so a single solve can land short */
 kioskFitNow()},32)}))}
function kioskFitNow(){
 const wrap=document.querySelector('.kiosk-wrap');if(!wrap)return;
 if(kioskCfg().fit=='scroll'&&!kioskCfg().rotation){kfStyle(0);      /* natural size: no box, no scale */
  wrap.style.width=wrap.style.height=wrap.style.transform='';return}
 const T=kioskT(innerWidth,innerHeight),W=innerWidth/T,H=innerHeight/T;
 kioskBox(wrap,T);
 const nodes=wrap.querySelector('.knodes');if(!nodes||!nodes.children.length){
  /* no node grid: a detail page (alerts/backups) in shrink mode — fill exactly the room the
     wrap has left (header and gaps already occupy their height in the 100vh column) */
  const det=wrap.querySelector('.kdetail');
  if(det){kfStyle(0);
   det.style.maxHeight=Math.max(120,wrap.clientHeight-det.offsetTop-Math.ceil(10*(document.documentElement.classList.contains('kiosk-2')?KF[2]:1)))+'px'}
  return}
 const n=nodes.children.length;if(!n){kfStyle(0);return}
 /* measure in a hidden clone of the whole wrap: the top row and the slot cards above the grid
    shrink with the step too, so they are part of the room. The clone must sit inside the DOM —
    a bare detached tree resolves no inherited custom properties (--kf-h) and none of the theme
    selectors that hang off body[data-view]/html[data-style]. The clone grid's box is measured,
    not assumed: 100vh on the wrap resolves inside the probe (overflow:hidden), then the grid's
    flex height falls out. Cards carry their style attribute through the clone — a previous
    fit's inline transform/position is cleared, and bar fills are stripped: they animate every
    tick and would jitter the measurement and re-trigger this fit forever. */
 const probe=document.createElement('div');
 probe.style.cssText=`position:fixed;visibility:hidden;left:-9999px;top:0;width:${W}px;height:${H}px;overflow:hidden`;
 document.body.appendChild(probe);
 const solve=s=>{
  kfStyle(s);
  probe.innerHTML='';
  const copy=wrap.cloneNode(true);
  copy.querySelectorAll('.kmeters .bar').forEach(b=>{b.className='barx';b.style.cssText='height:8px'});
  copy.querySelectorAll('.ktrace').forEach(s=>s.style.cssText='height:calc(64px*var(--kf-h,1))');   /* fixed, data-independent */
  copy.querySelectorAll('.kiosk-wrap,.knodes').forEach(e=>e.removeAttribute('style'));
  copy.querySelectorAll('.knodes>*').forEach(e=>{e.style.position='';e.style.transform=''});
  copy.style.width=W+'px';copy.style.height=H+'px';  /* the layout box at T (100vw is the viewport) */
  probe.appendChild(copy);
  const pn=copy.querySelector('.knodes');
  if(!pn||!pn.children.length)return null;
  /* an explicit column count (?cols=3 / the Kiosk setting) pins the grid: fewer, wider columns
     trade vertical space for horizontal — what a wall with rows to spare (or tall cards to
     read) wants. Clamped to the node count and to one card per column. */
  const want=kioskCfg().cols;
  let maxCols=Math.max(1,Math.min(n,(want>=1&&want<=8)?want:Math.floor((W+12)/262)));
  /* auto: the fewest columns that keep the same row count — 6 nodes in 4 columns is two
     rows with two holes; 3 columns is the same two rows with wider cards */
  if(!(want>=1&&want<=8))maxCols=Math.ceil(n/Math.ceil(n/maxCols));
  const kfh=s?KF[s]:1,bottom=Math.ceil(10*kfh);
  const fitc=(cols,rows)=>{const avail=pn.offsetHeight;
    const gw=pn.getBoundingClientRect().width;const colW=(gw-(cols-1)*12)/cols;
    const cells=[...pn.children];
    cells.forEach(c=>{c.style.position='absolute';c.style.width=colW+'px'});
    let cell=0;cells.forEach(c=>{cell=Math.max(cell,c.offsetHeight||c.scrollHeight)});
    cells.forEach(c=>{c.style.position='';c.style.width=''});
    if(!cell)return null;
    const nat=rows*cell+(rows-1)*12;
    if(nat<=avail+1)return {cols,rows,rh:(avail-(rows-1)*12)/rows,s,_slack:avail-rows*cell-(rows-1)*12,_cell:cell,_avail:avail};
    /* the widest we may go: shrink. The box grows by 1/scale, and only the grid needs the
       room — the header above it keeps its layout height — so H/(header+grid) fits it. */
    if(cols>=maxCols)return {cols,rows,rh:cell,s,scale:H/(H-avail+nat)};
    return null};
  for(let cols=maxCols;cols>=1;cols--){
   pn.style.gridTemplateColumns=`repeat(${cols},minmax(0,1fr))`;
   pn.style.gridAutoRows='';
   const m=fitc(cols,Math.ceil(n/cols));
   if(m)return m}
  return null};
 /* Type step: all three fit when the grid has rows to spare. Step 0 (kf=1) is the LARGEST
    type; it also demands the tallest rows, so the old first-wins scan from s=0 already
    picks the biggest font that fits — keep that. The mistake this fixes: with a pinned
    column count and a big screen (a 3x2 grid on a 2000x1010 wall), the rows were forced to
    the cards' natural height and the leftover just pooled into empty graph space — the wall
    looked sparse and "too small" (operator, 2026-10-23). At step 0, grow the rows instead:
    spread the slack across the rows, which stretches the trace (.ktrace flex:1) and every
    proportional size with it, and the wall fills the screen at the largest type.
    When no step fits, the one that paints largest after its shrink wins (step size x
    shrink): the full-size step overflows the most, so it is rarely that one. */
 let best=null;const eff=m=>(m.s?KF[m.s]:1)*(m.scale||1);
 for(const s of[0,1,2]){
  const m=solve(s);if(!m)continue;
  if(m.scale===undefined){if(!best||eff(m)>=eff(best))best=m;break}
  if(!best||eff(m)>eff(best))best=m}
 probe.remove();
 if(!best){kfStyle(0);return}
 /* a tick that re-solves the same page a pixel or two differently rewrites every card's
    height: the whole wall reflows and it reads as a flash (operator, 2026-10-22). Hysteresis:
    keep the last fit when the new one lands within 6px of it on the same step and columns. */
 best.vw=innerWidth;best.vh=innerHeight;
 if(_lastFit&&_lastFit.pg==pgKey()&&_lastFit.vw==innerWidth&&_lastFit.vh==innerHeight&&_lastFit.s==best.s&&_lastFit.cols==best.cols&&
    _lastFit.scale===best.scale&&Math.abs(_lastFit.rh-best.rh)<=6)best=_lastFit;
 _lastFit=best;
 kfStyle(best.s);                               /* settle the step before measuring the live tree */
 nodes.style.gridTemplateColumns=`repeat(${best.cols},minmax(0,1fr))`;
 /* shrunk: the enlarged box has room to spare below the measured rows; share it out */
 nodes.style.gridAutoRows=best.scale?'minmax(0,1fr)':best.rh.toFixed(1)+'px';
 const S=T*(best.scale?Math.min(1,Math.max(.3,best.scale)):1);
 kioskBox(wrap,S);
 /* settle: if the applied tree's content still overflows its rows (a late theme class, a font
    that loaded mid-solve, stretched heights lying to the probe), refit from the true state —
    guard-bounded, the fit converges in one or two extra passes */
 const box=nodes.firstElementChild.getBoundingClientRect().height/S;
 if(++_fitPass<5&&[...nodes.children].some(c=>c.scrollHeight>box+2))kioskFit();else _fitPass=0}
if(VIEW=='kiosk'){addEventListener('resize',kioskFit);new MutationObserver(kioskFit).observe($('desk'),{childList:true,subtree:true})}

/* ── renderers (pure: data -> html) ── */
/* live tile body from Prometheus (fleet_metrics) */
function tileLive(M,up){const sp=M.spark||{};
 const memP=M.mem_total?100*M.mem_used/M.mem_total:0;
 const fl=[M.up===0?'<span class="tag bad">exporter down</span>':'',
  M.failed_units?`<span class="tag warn" title="failed systemd units — see Health tab">${M.failed_units} failed unit${M.failed_units>1?'s':''}</span>`:'',
  M.reboot?'<span class="tag warn" title="/var/run/reboot-required">reboot needed</span>':'',
  M.containers?`<span class=tag title="running Docker containers">${M.containers} ctr</span>`:''].join('');
 return `<div class=mgrid>
  <div class=mcell data-k=cpu><span>cpu</span><span class=mv>${pct(M.cpu)} · load ${M.load1??'—'}</span>${spark([{pts:sp.cpu}],{max:100})}</div>
  <div class=mcell data-k=gpu><span>gpu</span><span class=mv>${pct(M.gpu_util)}${M.gpu_temp!=null?' · '+fmtT(M.gpu_temp):''}${M.gpu_power!=null?' · '+Math.round(M.gpu_power)+'W':''}</span>${spark([{pts:sp.gpu_util,cls:'a'}],{max:100})}</div>
  <div class=mcell data-k=net><span>net</span><span class=mv>↑${fmtBits(M.net_tx)} ↓${fmtBits(M.net_rx)}</span>${spark([{pts:sp.net_tx,cls:'g'},{pts:sp.net_rx,cls:'c0'}])}</div>
 </div>
 <div class=gauges>
  <div class=gauge data-k=mem><span>mem</span>${bar(memP,true,'mem')}<span>${M.mem_used!=null?Math.round(M.mem_used/1e9)+'G':'—'}</span></div>
  <div class=gauge data-k=disk><span>disk</span>${bar(M.root_pct,false,'disk')}<span>${pct(M.root_pct)}</span></div>
 </div>
 ${llmLine(M.llm)}
 <div class=flags><span class=tnext>up ${esc(up.trim().slice(0,12))}</span>${fl}</div>`}
function llmLine(L){if(!L)return '';
 const sv=(L.servers||[]).filter(s=>s.model);
 if(!sv.length)return L.worker_for?`<div class=mcell><span>llm</span><span class="mv wk">TP worker for ${esc(L.worker_for)} · ${esc(L.worker_model||'')}</span><span></span></div>`:'';
 return sv.map(s=>`<div class=mcell data-k=llm><span>llm</span><span class=mv title="${esc(s.model)}">${esc(s.model)} · ${s.running??0} run${s.waiting?' / '+s.waiting+' wait':''} · ${Math.round(s.gen_tps||0)} tok/s${s.kv_pct!=null?' · kv '+Math.round(s.kv_pct)+'%':''}</span>${spark([{pts:(s.spark||{}).gen_tps,cls:'a'}])}</div>`).join('')}
/* the host's primary scheduled profile (system if scheduled) */
function schedRow(s,host){const rs=((s&&s.schedule&&s.schedule.rows)||[]).filter(r=>r.host==host&&r.enabled);
 return rs.find(r=>r.profile=='system')||rs[0]||null}
function rpoBadge(r,sn,h){
 if(!r&&h&&h.monitored_only)return `<span class=snapbadge title="monitored only: metrics and facts, no backup by design (site config backup:false)">monitored</span>`;
 const go=h&&h.name?`onclick="event.stopPropagation();goTo(${esc(JSON.stringify({host:h.name,panel:'detail',tab:'backups'}))})"`:'';
 if(!r){const n=(sn&&sn.count)||0;return `<span class="snapbadge go ${n?'warn':'bad'}" title="not scheduled — open backups" ${go}>unscheduled</span>`}
 const cls=HCLS[r.health]||'warn';
 const txt=r.last_ts?`${esc(r.profile)} ${fmtDur(Date.now()/1000-r.last_ts).split(' ')[0]}`:`no ${esc(r.profile)}`;
 return `<span class="snapbadge go ${cls}" title="${esc(r.profile)}: last ${esc(fmtTs(r.last_ts))} · ${esc(r.health)} · every ${esc(fmtDur(r.every))} — open backups" ${go}>${txt}</span>`}
function rFleet(s){
 if(!s)return OVR?'':'<div class=empty>loading…</div>';
 OVR=s;
 if(SEL&&!(s.hosts||[]).some(h=>h.name==SEL)){SEL=null;localStorage.removeItem(LS_SEL);history.replaceState(null,'',location.pathname);tick(PANELS.find(x=>x.id=='detail'))}
 $('hsrv').textContent=`${s.server} · ${s.hosts.length} nodes`;
 $('hauth').textContent=s.demo?'demo · sample data':s.auth_required?'auth on':'dev mode';
 $('hauth').className='hpill'+(s.auth_required?'':' warn');
 const rows=((s.schedule&&s.schedule.rows)||[]).filter(r=>r.enabled),good=rows.filter(r=>r.health=='ok').length;
 $('hrpo').innerHTML=rows.length?`backups ${good}/${rows.length}<span class=hlong> on schedule</span>`:'no schedule';
 $('hrpo').className='hpill go '+(rows.length&&good==rows.length?'ok':good<rows.length/2?'bad':'warn');
 return `<div class=tiles>`+s.hosts.map(h=>{
  const f=(s.facts&&s.facts[h.name])||{},sn=(s.snapshots&&s.snapshots.hosts||{})[h.name]||{};
  const M=((s.metrics&&s.metrics.hosts)||{})[h.name];
  const n=sn.count||0,last=sn.last||'';
  const ok=!f.error,up=(f.uptime_lines&&f.uptime_lines[0]||'').replace('up','')||'—';
  const memP=f.mem_total_gb?100*f.mem_used_gb/f.mem_total_gb:0;
  const rootd=(f.disks||[]).filter(d=>d.mnt=='/');
  const diskP=rootd.length?parseInt(rootd[0].pct)||0:0;
  const g=f.gpu_stats,gg=g&&g.temp_c!=null?g:null,tmp=gg?clamp(Math.round(gg.temp_c*100/95),0,100):0;
  return `<div class="tile${SEL==h.name?' sel':''}${runningOn(h.name)?' busy':''}" onclick="selectHost('${esc(h.name)}')">
   ${rpoBadge(schedRow(s,h.name),sn,h)}
   <div class=nm><span class="dot ${ok?'ok':''}"></span>${esc(h.name)}<span class="tjob go" data-jb="${esc(h.name)}" title="open Runs" onclick="event.stopPropagation();goTo(${esc(JSON.stringify({host:h.name,panel:'runs'}))})">${jobBadge(h.name)}</span></div>
   <div class=sub>${esc(f.address||'')} · ${esc((f.os_lines&&f.os_lines[0]||'—').replace('Ubuntu ','Ubu '))} · ${esc(h.arch)}</div>
   ${M&&M.up!=null?tileLive(M,up):`<div class=mrow>
    <div class=metric><span class=v>${esc(up.slice(0,8))}</span><span class=u>up</span></div>
    <div class=metric><span class=v>${g&&g.util_pct!=null?esc(g.util_pct)+'%':'—'}</span><span class=u>gpu</span></div>
   </div>
   <div class=gauges>
    <div class=gauge data-k=mem><span>mem</span>${bar(memP,true,'mem')}<span>${f.mem_used_gb??'—'}G</span></div>
    <div class=gauge data-k=disk><span>disk</span>${bar(diskP,false,'disk')}<span>${diskP||'—'}%</span></div>
    <div class=gauge><span>gpu</span>${bar(tmp)}<span>${gg?fmtT(gg.temp_c):'—'}</span></div>
   </div><div class=tnext>live metrics unavailable — SSH snapshot</div>`}
   <div class=tline data-jl="${esc(h.name)}">${jobLine(h.name)||nextLine(schedRow(s,h.name),f)}</div></div>`}).join('')+`</div>`;
}
/* ── live job state (client mirror of the server's job table) ──
   act() registers jobs; pollAll() refreshes running ones every 2 s; boot
   re-adopts in-flight jobs so a page reload mid-backup still shows them. */
const JOBS={};
function actHost(a,p){return (p.host||'') || (['backup','verify'].includes(a)?'*':'')}
function runningOn(host){return Object.values(JOBS).some(j=>!j.done&&j.running&&(j.params.host===host||(j.params.host===undefined&&['backup','verify'].includes(j.action))))}
function jobFor(host){
 const mine=Object.values(JOBS).filter(j=>j.params.host===host||(j.params.host===undefined&&['backup','verify','copy','prune'].includes(j.action)));
 mine.sort((a,b)=>(b.started_ts||0)-(a.started_ts||0));return mine[0]}
function jobBadge(host){const j=jobFor(host);if(!j)return '';
 if(!j.done&&j.running)return `<span class=spin>◌</span>`;
 if(j.done){const o=jobOutcome(j);return o.cls=='ok'?`<span class=okj>✓</span>`:o.cls=='warn'?`<span class=qj>–</span>`:`<span class=badj>✗</span>`}
 return `<span class=qj>…</span>`}
function nextLine(r,f){
 if(f&&f.agent_busy)return `<div class="jline run">job running on host${f.progress?' · '+esc(f.progress.uploaded_mb)+' MB up':''}</div>`;
 if(!r)return '';
 return `<div class=tnext>${r.due?'<span class=warn>due — next tick</span>':'next '+esc(r.profile)+' '+esc(fmtTs(r.next_ts))}</div>`}
/* what a finished job MEANT: a SKIP for busy is not a failure */
function jobOutcome(j){const out=j.output||'';
 if(j.rc===0)return {cls:'ok',txt:'ok'};
 if(/SKIP .*(busy|already running)/.test(out))return {cls:'warn',txt:'busy — skipped'};
 if(/SKIP .*(floor|probe failed|unmeasurable)/.test(out))return {cls:'warn',txt:'skipped — network'};
 if(j.rc===-2)return {cls:'warn',txt:'lost track (UI restart)'};
 return {cls:'bad',txt:'rc='+j.rc}}
function jobLine(host){const j=jobFor(host);if(!j)return '';
 const el=j.started?` · ${fmtDur((j.done?(j._end||Date.now()/1000):Date.now()/1000)-j.started_ts)}`:'';
 if(!j.done&&j.running)return `<div class="jline run">${esc(j.action)} running${el}</div>`;
 if(j.done){const last=(j.output||'').trim().split('\n').filter(Boolean).pop()||'',o=jobOutcome(j);
  return `<div class="jline ${o.cls}">${esc(j.action)} ${esc(o.txt)}${el}${last?' · '+esc(last.slice(0,60)):''}</div>`}
 const qb=j.queued_behind;
 return `<div class="jline warn">${esc(j.action)} queued${qb?' behind '+esc(qb.action)+(qb.host?' '+esc(qb.host):''):''}</div>`}
function fmtDur(s){s=Math.max(0,Math.round(s));
 return s<60?s+'s':s<3600?Math.floor(s/60)+'m '+String(s%60).padStart(2,'0')+'s'
  :s<172800?Math.floor(s/3600)+'h '+String(Math.floor(s%3600/60)).padStart(2,'0')+'m'
  :Math.floor(s/86400)+'d '+Math.floor(s%86400/3600)+'h'}
function refreshJobDom(){
 // patch badges/lines in place — no full re-render, no flicker
 document.querySelectorAll('[data-jb]').forEach(el=>{el.innerHTML=jobBadge(el.dataset.jb)});
 document.querySelectorAll('[data-jl]').forEach(el=>{el.innerHTML=jobLine(el.dataset.jl)});
 const out=$('cout'),act=Object.values(JOBS).find(j=>!j.done);
 if(out&&act){out.textContent=`$ ${(act.argv||[]).join(' ')}\n…running\n`+
   ((act.output||'').trim().split('\n').slice(-12).join('\n'))}
 // while a job is active on the selected machine, keep its report live
 if(SEL&&jobFor(SEL)&&!jobFor(SEL).done)repaint('detail',PANELS.find(p=>p.id=='detail')._data)}
async function pollAll(){
 const ids=Object.keys(JOBS).filter(id=>!JOBS[id].done);
 let dirty=false,hyDone=false;
 for(const id of ids){try{const j=await fetchJSON('/api/v1/job/'+id);
  const was=j.done;Object.assign(JOBS[id],j);dirty=true;
  if(was){JOBS[id]._end=Date.now()/1000;
   tick(PANELS.find(x=>x.id=='jobs'));tick(PANELS.find(x=>x.id=='fleet'));
   tick(PANELS.find(x=>x.id=='storage'));tick(PANELS.find(x=>x.id=='network'));
   tick(PANELS.find(x=>x.id=='schedule'));tick(PANELS.find(x=>x.id=='runs'));
   if(j.action=='hygiene'||j.action=='hygieneFix')hyDone=true}}
  catch(e){}}
 if(ids.length||dirty)refreshJobDom();
 if(hySync())hyRepaint();
 if(hyDone)hyRefresh()}
/* re-adopt in-flight jobs after a reload, so a running backup is still visible */
async function adoptRunning(){
 try{const js=await fetchJSON('/api/v1/jobs?active=1');
  for(const j of js){if(!JOBS[j.id])JOBS[j.id]=Object.assign({started_ts:Date.now()/1000},j);
   else Object.assign(JOBS[j.id],j)}
  refreshJobDom()}catch(e){}}
function selectHost(n){SEL=n;localStorage.setItem(LS_SEL,n);
 history.replaceState(null,'',location.pathname+hashOf({host:n}));
 if(OVR)repaint('fleet',OVR);
 tick(PANELS.find(x=>x.id=='detail'))}
function rDetail(f){
 if(!SEL)return '<div class=empty>click a machine in the Fleet panel →</div>';
 if(!f)return '<div class=empty>select a machine</div>';
 if(f.error)return `<div class="empty bad">facts unavailable: ${esc(f.error)}</div>`;
 const o=OVR||{snapshots:{hosts:{}},hosts:[]},
  h=(o.hosts||[]).find(x=>x.name==SEL)||{name:SEL},
  sn=(o.snapshots&&o.snapshots.hosts||{})[SEL]||{},g=f.gpu_stats;
 // monitored only (a Mac, or backup:false): its own backups are reported, never run from here
 const vs=o.versions||{},CMARK={ok:'ok',upgrade:'warn',behind:'bad',missing:'bad',ahead:'mut',unknown:'mut',skipped:'mut'};
 const comps=((vs.hosts||{})[SEL]||[]).map(r=>`<tr><td>${esc(r.component)}</td><td>${esc(r.installed||'—')}</td><td>${esc(r.pinned||'—')}</td><td>${esc(r.latest||'—')}</td>
  <td class=${CMARK[r.state]||'mut'}>${esc(r.detail)}${r.fix||r.state=='skipped'?`<div class=compbtns>${compBtns(SEL,r)}</div>`:''}</td></tr>`).join('');
 const mon=h.os=='macos'||!!h.monitored_only,tm=f.timemachine||{},tmOld=mon&&h.os=='macos'&&(!tm.latest_ts||Date.now()/1000-tm.latest_ts>2*86400);
 const rows=(f.last_jobs||[]).map(j=>
  `<tr><td>${esc(fmtTs(j.ts))}</td><td>${esc(j.event||'')}</td><td class="${(j.rc??0)==0?'ok':'bad'}">${j.rc??''}</td><td>${esc(j.snapshot||'')}</td></tr>`).join('');
 const srows=((o.schedule&&o.schedule.rows)||[]).filter(r=>r.host==SEL);
 const bk=srows.map(r=>r.enabled?
  `<tr><td>${esc(r.profile)}${r.override?'<span class="tag info">override</span>':''}</td>
   <td class="${HCLS[r.health]||''}">${r.last_ts?esc(agoS(r.last_ts)):'never'}</td>
   <td>${esc(fmtDur(r.every))}${r.window?' · '+esc(r.window):''}</td>
   <td>${r.due?'<span class=warn>due now</span>':esc(fmtTs(r.next_ts))}</td>
   <td><span class="tag ${HCLS[r.health]||''}">${esc(r.health)}</span></td></tr>`
  :`<tr><td>${esc(r.profile)}<span class="tag info">override</span></td><td colspan=4 class=mut>disabled for this host</td></tr>`).join('');
 const other=Object.entries(sn.profiles||{}).filter(([p])=>!srows.some(r=>r.profile==p))
  .map(([p,v])=>`<tr><td class=mut>${esc(p)}</td><td class=mut>${esc(agoS(v.last_ts))}</td><td class=mut colspan=3>${v.count} snapshot${v.count==1?'':'s'} · not scheduled</td></tr>`).join('');
 const snaps=(sn.recent||[]).slice().reverse().map(r=>
  `<tr class="snaprow${BR.snap==r.id?' sel':''}" onclick="browseTo('${esc(SEL)}','${esc(r.id)}','/')" title="browse this snapshot"><td>${esc(r.id)}</td><td>${esc(fmtTs(r.ts))}</td><td>${esc(r.profile)}</td>
   <td>${r.recorded===false?'<span class="tag warn" title="in the repo, but no backup run was seen to finish: its driver died (UI restart / lost ssh). The data is real; check it with verify.">no run record</span>':'<span class=ok>✓</span>'}</td></tr>`).join('');
 const pr=f.progress,act=f.agent_busy
  ?`<div class=sline><span class="tag warn">running</span> job holds the agent lock on this host${pr?` · <b>${esc(pr.profile)}</b> ${esc(fmtDur(+pr.elapsed||0))} · uploaded <b>${esc(pr.uploaded_mb)}</b> MB · scanned <b>${esc(pr.scanned_mb)}</b> MB · at ${esc(fmtTs(pr.at))}`:''}</div>`
  :`<div class=sline><span class="tag ok">idle</span> no Noctyx job on this host</div>`;
 const blog=(f.backuplog||[]).length?`<pre class=jl-t>${esc(f.backuplog.join('\n'))}</pre>
  <div class=mut style="font:10.5px var(--mono);margin-top:3px">last-backup.log · updated ${esc(agoS(f.backuplog_mtime))} — whoever started the job (tick, UI or CLI)</div>`:'';
 const disks=(f.disks||[]).map(d=>
  `<tr><td>${esc(d.mnt)}</td><td class=mut>${esc(d.dev.replace('/dev/',''))}</td><td>${d.used_gb}/${d.size_gb} GB</td><td style=width:34%>${bar(d.pct)}</td></tr>`).join('');
 const devs=(f.blockdevs||[]).map(d=>
  `<tr><td>${esc(d.name)}</td><td>${d.size_gb} GB</td><td class=mut>${esc(d.serial)}</td></tr>`).join('');
 const jj=jobFor(SEL);
 const jtail=jj?((jj.output||'').trim().split('\n').filter(Boolean).slice(-6).join('\n')):'';
 return `<div class=rep>
 <div class=hero><span class=big>${esc(SEL)}</span>
  <span class=mut>${esc(f.address||'')}${f.ip&&f.ip!=f.address?' · '+esc(f.ip):''}</span>
  <a class=lnk href="/machine?host=${esc(encodeURIComponent(SEL))}" title="everything about ${esc(SEL)} on one page">open full page →</a></div>
 ${mon?'':`<div style=margin-top:8px>
  <button class=act onclick="act('backupHost',{host:'${esc(SEL)}'})">backup now</button>
  <button class=act onclick="act('verifyHost',{host:'${esc(SEL)}'})">verify now</button></div>`}
 ${jj?`<div class="jlive ${jj.done?(jj.rc===0?'ok':'bad'):'run'}" id=jlive>
  <div class=jl-h>${esc(jj.action)} <span class=jl-d>${fmtDur((jj.done?(jj._end||Date.now()/1000):Date.now()/1000)-(jj.started_ts||0))}</span>
   ${jj.done?(jj.rc===0?'· done':'· rc='+jj.rc):'· running…'}</div>
  ${jtail?`<pre class=jl-t>${esc(jtail)}</pre>`:''}</div>`:''}
 <div class=mut style="font:11px var(--mono);margin-top:6px">${esc((f.os_lines||[]).join(' · '))}</div>
 <div class=dtabs>${DTABS.map(t=>`<button class="dtab${t==DTAB?' on':''}" onclick="setTab('${t}')">${t}</button>`).join('')}</div>
 ${DTAB=='overview'?`
  <h3>Vitals</h3>
  <div class=cards>
   <div class=card data-k=cpu><div class=l>load</div><div class=v>${esc((f.loadavg||'—').split(' ')[0])}</div><div class=s>${esc(f.loadavg||'')}</div></div>
   <div class=card data-k=mem><div class=l>memory</div><div class=v>${f.mem_used_gb??'—'}<span class=u>/${f.mem_total_gb??'—'}G</span></div>${bar(f.mem_total_gb?100*f.mem_used_gb/f.mem_total_gb:0,true,'mem')}</div>
   <div class=card data-k=cpu><div class=l>cpu</div><div class=v>${esc((f.cpu&&f.cpu[0])||'—')}</div><div class=s>${esc(((f.cpu&&f.cpu[1])||'').slice(0,22))}</div></div>
   <div class=card><div class=l>uptime</div><div class=v style=font-size:13px>${esc((f.uptime_lines&&f.uptime_lines[0]||'—'))}</div></div>
   ${g&&g.temp_c!=null?`<div class=card data-k=gpu><div class=l>gpu</div><div class=v>${Math.round(tval(g.temp_c))}<span class=u>°${TU}</span></div><div class=s>${g.util_pct??'—'}% · ${g.mem_used_mib??'—'} MiB</div></div>`:''}
   ${mon?`<div class=card data-k=${tmOld?'warn':'ok'}><div class=l>time machine</div><div class=v style=font-size:13px>${tm.latest_ts?esc(fmtTs(tm.latest_ts)):(tm.dest?'no backup yet':'off')}</div><div class=s>${esc(tm.dest||'no destination')}${tm.running=='1'?' · backing up now':''}</div></div>`
   :`<div class=card data-k=ok><div class=l>snapshots</div><div class=v>${sn.count||0}</div><div class=s>${sn.last_ts?'last '+esc(fmtTs(sn.last_ts)):'none'}${sn.stale?' · stale':''}</div></div>`}
  </div>
  ${(f.gpu&&f.gpu[0])?`<div class=mut style="font:11px var(--mono);margin-top:8px">${esc(f.gpu.join(' / '))}</div>`:''}
  ${tabOverview(f._m)}
  <h3>Filesystems</h3>
  <table><tr><th>mount</th><th>dev</th><th>used</th><th></th></tr>${disks||'<tr><td class=mut>no data</td></tr>'}</table>
  <h3>Components</h3>
  <table><tr><th>component</th><th>installed</th><th>pin</th><th>latest</th><th></th></tr>${comps||'<tr><td class=mut colspan=5>not checked yet (noctyx versions)</td></tr>'}</table>
  ${vs.ts?`<div class=mut style="font:10.5px var(--mono)">checked ${esc(fmtTs(vs.ts))} · nightly, or <code>noctyx versions --host ${esc(SEL)}</code></div>`:''}
  ${mon?`<h3>Backups</h3>
  <div class=mut style="font:11.5px var(--mono)">monitored only: ${h.os=='macos'?'Time Machine backs this Mac up'+(tm.dest?' to '+esc(tm.dest):' (no destination set)')+(tm.latest_ts?', last '+esc(fmtTs(tm.latest_ts)):''):'not backed up by Noctyx (backup: false, or no repository)'}</div>`
  :`<h3>Noctyx agent</h3>
  <div class=mut style="font:11.5px var(--mono)">${f.agent&&f.agent[0]?'agent '+esc(f.agent[0])+' · profiles '+esc(f.agent[1]||'—'):'<span class=bad>not installed</span>'}
   ${h.used_gb?` · pinned source ${h.used_gb} GB`:''}</div>`}`
 :DTAB=='llm'?tabLLM(f._m)
 :DTAB=='network'?tabNetwork(f._m)
 :DTAB=='storage'?tabStorage(f._m)+`<h3>Block devices (inventory)</h3>
  <table><tr><th>name</th><th>size</th><th>serial</th></tr>${devs||'<tr><td class=mut>no data</td></tr>'}</table>`
 :DTAB=='gpu'?tabGpu(f._m,f)
 :DTAB=='health'?tabHealth(f._m)
 :`
  <h3>Backups</h3>
  <table><tr><th>profile</th><th>last</th><th>every</th><th>next</th><th></th></tr>${bk+other||'<tr><td class=mut>not scheduled — see Schedule panel</td></tr>'}</table>
  <h3>Agent activity</h3>
  ${act}${blog}
  <h3>Snapshots</h3>
  <table><tr><th>id</th><th>when</th><th>profile</th><th></th></tr>${snaps||'<tr><td class=mut>none</td></tr>'}</table>
  <div class=mut style="font:10.5px var(--mono)">click a snapshot to browse its files · <button class=lnk onclick="browseTo('${esc(SEL)}',null,'/')">browse latest</button></div>
  <h3>Recent agent jobs</h3>
  <table><tr><th>when</th><th>event</th><th>rc</th><th>snapshot</th></tr>${rows||'<tr><td class=mut>none recorded</td></tr>'}</table>`}
 </div>`;
}
/* ── report tabs fed by /api/v1/metrics/<host>?tab=… (Prometheus) ── */
function mGuard(m){if(!m||m.loading)return '<div class=empty>loading…</div>';
 if(m.error)return `<div class="empty warn">live metrics unavailable: ${esc(m.error)}</div>`;return ''}
/* ── Cluster: every machine on one screen (Prometheus fleet metrics) ── */
function rCluster(m){
 if(!m)return '<div class=empty>loading…</div>';
 const H=m.hosts||{},ord=m.order||(OVR&&OVR.hosts||[]).map(h=>h.name),names=ord.filter(n=>H[n]).concat(Object.keys(H).filter(n=>!ord.includes(n)));
 let tps=0,run=0,wait=0,models=0,gpuW=0;
 const rows=names.map(n=>{const M=H[n]||{},L=M.llm||{},sv=(L.servers||[]).filter(s=>s.model);
  sv.forEach(s=>{tps+=s.gen_tps||0;run+=s.running||0;wait+=s.waiting||0;models++});gpuW+=M.gpu_power||0;
  const llm=sv.length?sv.map(s=>`<div class=md title="${esc(s.model)}">${esc(s.model)}${L.up===0?' <span class="tag bad">down</span>':''}</div>`).join('')
   :L.worker_for?`<div class="md wk">worker for ${esc(L.worker_for)}</div>`:'<div class="md wk">—</div>';
  const s0=sv[0]||{},sp=M.spark||{};
  const cell=(v,f,cls)=>`<td class="n ${cls||''}">${v==null?'<span class=u>—</span>':f(v)}</td>`;
  const ms=v=>v==null?'—':v<1?Math.round(v*1000)+'<span class=u>ms</span>':v.toFixed(1)+'<span class=u>s</span>';   /* null-safe: engines without latency histograms (tensorfold) */
  return `<tr><td><span class="dot ${M.up?'ok':''}"></span> <span class=hn>${esc(n)}</span></td><td>${llm}</td>
   <td class=n data-k=llm>${sv.length?`${s0.running??0}<span class=u> run</span>${s0.waiting?' <span class=warn>'+s0.waiting+' wait</span>':''} · ${Math.round(s0.gen_tps||0)}<span class=u> tok/s</span> ${spark([{pts:(s0.spark||{}).gen_tps,cls:'a'}])}${s0.decode_tps!=null?`<br><span class=u>decode</span> ${Math.round(s0.decode_tps)}<span class=u> tok/s per req</span>`:''}`:'<span class=u>—</span>'}</td>
   <td class="n lp">${sv.length?(s0.prefill_tps!=null?`${esc(fmtK(s0.prefill_tps))}<span class=u> tok/s</span>${s0.prefill_p50!=null?` · ${ms(s0.prefill_p50)}<span class=u> p50</span>`:''}${s0.prefill_share!=null?`<br><span class=u>${Math.round(s0.prefill_share)}% of busy time</span>`:''}`:'<span class=u>idle</span>'):'<span class=u>—</span>'}</td>
   <td class="n lp">${sv.length?`<span class="${s0.kv_pct>90?'bad':s0.kv_pct>70?'warn':''}">kv ${Math.round(s0.kv_pct||0)}%</span>${s0.ttft_p50!=null?' · ttft '+ms(s0.ttft_p50):''}${s0.queue_p50!=null&&s0.queue_p50>0.05?`<br><span class=u>queue</span> ${ms(s0.queue_p50)}`:''}`:'<span class=u>—</span>'}</td>
   <td class=n data-k=gpu>${M.gpu_util!=null?`${Math.round(M.gpu_util)}% · <span class="${M.gpu_temp>85?'bad':M.gpu_temp>75?'warn':''}">${fmtT(M.gpu_temp)}</span> · ${Math.round(M.gpu_power)}W`:'<span class=u>—</span>'}</td>
   <td class="n lp2" data-k=cpu>${M.cpu!=null?Math.round(M.cpu)+'%':'—'} ${spark([{pts:sp.cpu}],{max:100})}</td>
   <td class="n lp2">${M.mem_used!=null?Math.round(M.mem_used/1e9)+'<span class=u>/'+Math.round((M.mem_total||0)/1e9)+'G</span>':'<span class=u>—</span>'}</td>
   <td class=n data-k=net>↑${esc(fmtBits(M.net_tx))} ↓${esc(fmtBits(M.net_rx))}${M.fabric_tx!=null?`<br><span class=u>fabric</span> ↑${esc(fmtBits(M.fabric_tx))} ↓${esc(fmtBits(M.fabric_rx))}`:''}</td>
   <td class="n lp"><span class="${M.root_pct>90?'bad':M.root_pct>80?'warn':''}">${M.root_pct!=null?Math.round(M.root_pct)+'%':'—'}</span>${M.disk_io!=null?' · '+esc(fmtBytes(M.disk_io))+'<span class=u>/s</span>':''}</td>
   <td>${M.failed_units?goA({host:n,panel:'hygiene'},'warn',M.failed_units+' failed','failed units — fix in Hygiene'):''}${M.reboot?goA({host:n,panel:'hygiene'},'warn','reboot','reboot required — Hygiene'):''}${M.up===0?goA({host:n,panel:'detail',tab:'health'},'bad','no metrics','exporter down — Machine Report › health'):''}</td></tr>`}).join('');
 const F=m.fleet||{},S=F.series||{},I=F.infra||{};
 const tokSeries=Object.entries(S.tokens||{}).sort().map(([mo,pts])=>({name:mo.length>18?mo.slice(0,16)+'…':mo,pts,cls:slotOf(mo)}));
 const tokAll=Object.values(S.tokens||{}).reduce((acc,pts)=>{pts.forEach(([t,v])=>{acc[t]=(acc[t]||0)+(v||0)});return acc},{});
 const tokPts=Object.entries(tokAll).map(([t,v])=>[+t,v]).sort((a,b)=>a[0]-b[0]);
 const roster=(F.roster||[]).map(r=>`<tr><td><b>${esc(r.model||'?')}</b></td><td>${esc(r.host)}${r.workers&&r.workers.length?' <span class=u>+ '+esc(r.workers.join(', '))+'</span>':''}</td>
   <td class=n>${r.tp?'tp'+r.tp:'—'}</td><td class=n>${r.max_model_len?esc(fmtK(r.max_model_len))+'<span class=u> ctx</span>':'—'}</td>
   <td class=n>${r.kv_tokens!=null?esc(fmtK(r.kv_tokens))+'<span class=u> kv tok</span>':'—'}</td>
   <td class=n>${r.gpu_mem_util!=null?Math.round(r.gpu_mem_util*100)+'<span class=u>% gpu mem</span>':'—'}</td>
   <td class=n>${r.started_ts?'<span class=u>up</span> '+esc(fmtDur(Date.now()/1000-r.started_ts)):'—'}</td></tr>`).join('');
 const raid=(I.raid||[]).filter(r=>r.array=='md2'||(I.raid||[]).length==1).map(r=>{const t=(I.raid_total||{})[r.array],sy=(I.raid_sync||{})[r.array];
   return `${esc(r.level)} <b class="${r.active<t?'bad':''}">${r.active}/${t}</b>${sy!=null?' <span class=warn>rebuilding '+sy.toFixed(1)+'%</span>':''}`}).join(' · ');
 const upN=names.filter(n=>H[n].up).length;
 return `<div class=hero>
  <div class=hv data-ghost="${ghost(Math.round(tps))}">${Math.round(tps)}</div>
  <div class=hl>tokens / second across the fleet<br><b>${models}</b> model${models==1?'':'s'} serving on <b>${upN}</b> of ${names.length} nodes${I.llm_total&&I.llm_up<I.llm_total?` · <span class=bad>${I.llm_total-I.llm_up} endpoint down</span>`:''}</div>
  <div class=stats>
   ${statTile('requests',run,' running',`${wait} waiting`,S.requests)}
   ${statTile('gpu power',Math.round(gpuW),' W',I.gpu_wh_24h!=null?(I.gpu_wh_24h/1000).toFixed(1)+' kWh in 24 h':'',S.gpu_power)}
   ${statTile('generated, 24 h',esc(fmtK(I.tokens_24h)),' tok',`${Math.round(I.requests_24h||0)} requests`,tokPts)}
   ${statTile('network',esc(fmtBits((S.net_tx&&S.net_tx.length?S.net_tx[S.net_tx.length-1][1]:0))),'',`↓ ${esc(fmtBits(S.net_rx&&S.net_rx.length?S.net_rx[S.net_rx.length-1][1]:0))}`,S.net_tx)}
   ${statTile('health',`${I.backups_ok??'?'}<span class=u>/${I.backups_total??'?'}</span>`,'',`backups on schedule · ${I.alerts?'<span class=warn>'+I.alerts+' alert'+(I.alerts>1?'s':'')+'</span>':'no alerts'}`)}
  </div></div>
 <div class=clwrap><div>
  <table class=cl><tr><th>node</th><th>model</th><th>llm</th><th class=lp>prefill</th><th class=lp>cache · latency</th><th>gpu</th><th class=lp2>cpu</th><th class=lp2>mem</th><th>net</th><th class=lp>root · io</th><th></th></tr>${rows}</table>
  <h3>Models</h3>
  <table class="cl roster"><tr><th>model</th><th>on</th><th class=n>tp</th><th class=n>context</th><th class=n>kv capacity</th><th class=n>gpu mem</th><th class=n>served</th></tr>${roster||'<tr><td class=mut colspan=7>no model serving</td></tr>'}</table>
  <div class=strip>
   <span>${(window.NX_CFG||{}).nas||'nas'} <b>${I.nas_free!=null?esc(fmtBytes(I.nas_free)):'—'}</b> free${I.nas_used!=null?' · '+esc(fmtBytes(I.nas_used))+' used':''}</span>
   <span>raid ${raid||'—'}</span>
   <span>repo endpoint <b class="${I.rest_up?'ok':'bad'}">${I.rest_up?'up':'DOWN'}</b></span>
   <span>exporters <b>${I.exporters_up??'?'}/${I.exporters_total??'?'}</b></span>
   <span>backups on schedule <b class="${I.backups_ok==I.backups_total?'ok':'warn'}">${I.backups_ok??'?'}/${I.backups_total??'?'}</b></span>
   <span>alerts <b class="${I.alerts?'warn':'ok'}">${I.alerts??0}</b></span>
  </div></div>
  <div class=charts>
   ${chart('tokens/s by model',tokSeries.length?tokSeries:[{pts:[]}],v=>Math.round(v)+' tok/s')}
   ${chart('requests',[{name:'run',pts:S.requests,cls:'c2'},{name:'wait',pts:S.waiting,cls:'c4'}],v=>Math.round(v))}
   ${chart('gpu power (fleet)',[{pts:S.gpu_power,cls:'a'}],v=>Math.round(v)+' W')}
   ${chart('network (fleet)',[{name:'↑',pts:S.net_tx,cls:'g'},{name:'↓',pts:S.net_rx,cls:'c0'}],fmtBits)}
  </div></div>
 <div class=mut style="font:10.5px var(--mono);margin-top:6px">llm = requests running (+waiting) · tok/s = generated tokens per second averaged over the last minute (idle time included) · decode = tokens/s a single request streams at during decode · prefill = prompt tokens processed per second of prefill, median prefill time, share of busy time spent prefilling · kv = KV-cache use · ttft = median time to first token (5 min) · fabric = the 200G links (tensor-parallel traffic) · kv capacity = tokens the KV cache can hold at once</div>`}
function tabLLM(m){const g=mGuard(m);if(g)return g;
 const S=m.series||{},N=m.now||{},topo=m.topology||[];
 const models=Object.keys(S.gen_tps||{});
 const head=topo.map(t=>t.role=='worker'?`<div class=sline><span class="tag info">TP worker</span> ${esc(t.engine)} <b>${esc(t.model||'')}</b> — headless rank of <b>${esc(t.worker_for||'?')}</b> (tp=${esc(t.tp)}); metrics live on that node</div>`
  :`<div class=sline><span class="tag ${(m.up||{})[String(t.port)]===0?'bad':'ok'}">${esc(t.engine)}</span> <b>${esc(t.model||'')}</b> on :${esc(t.port)}${t.tp>1?' · tensor-parallel '+esc(t.tp):''}${t.container?' · '+esc(t.container):''}</div>`).join('');
 if(!models.length)return head||'<div class=empty>no LLM server on this node</div>';
 const nm=k=>(N[k]||{});
 const cards=models.map(mo=>`<h3>${esc(mo)}</h3><div class=cards>
  <div class=card data-k=llm><div class=l>running</div><div class=v>${nm('running')[mo]??'—'}</div><div class=s>${nm('waiting')[mo]??0} waiting</div></div>
  <div class=card data-k=llm><div class=l>throughput</div><div class=v>${Math.round(nm('gen_tps')[mo]||0)}<span class=u> tok/s</span></div><div class=s>1-min average, idle included</div></div>
  <div class=card><div class=l>decode speed</div><div class=v>${nm('decode_tps')[mo]!=null?Math.round(nm('decode_tps')[mo]):'—'}<span class=u> tok/s</span></div><div class=s>per request, while streaming</div></div>
  <div class=card><div class=l>prefill</div><div class=v>${nm('prefill_tps')[mo]!=null?esc(fmtK(nm('prefill_tps')[mo])):'—'}<span class=u> tok/s</span></div><div class=s>${nm('prefill_p50')[mo]!=null?nm('prefill_p50')[mo].toFixed(2)+' s p50 · ':''}${nm('prefill_share')[mo]!=null?Math.round(nm('prefill_share')[mo])+'% of busy time':''}</div></div>
  <div class=card><div class=l>per request</div><div class=v>${nm('prompt_tok_p50')[mo]!=null?esc(fmtK(nm('prompt_tok_p50')[mo])):'—'}<span class=u> in</span></div><div class=s>→ ${nm('gen_tok_p50')[mo]!=null?esc(fmtK(nm('gen_tok_p50')[mo])):'—'} out (p50) · queue ${nm('queue_p50')[mo]!=null?Math.round(nm('queue_p50')[mo]*1000)+' ms':'—'}</div></div>
  <div class=card><div class=l>kv cache</div><div class=v>${nm('kv_pct')[mo]!=null?Math.round(nm('kv_pct')[mo]):'—'}<span class=u>%</span></div>${bar(nm('kv_pct')[mo])}</div>
  <div class=card><div class=l>ttft p50</div><div class=v>${nm('ttft_p50')[mo]!=null?(nm('ttft_p50')[mo]<1?Math.round(nm('ttft_p50')[mo]*1000)+'<span class=u> ms</span>':nm('ttft_p50')[mo].toFixed(1)+'<span class=u> s</span>'):'—'}</div><div class=s>p95 ${nm('ttft_p95')[mo]!=null?nm('ttft_p95')[mo].toFixed(2)+' s':'—'}</div></div>
  <div class=card><div class=l>e2e p50</div><div class=v>${nm('e2e_p50')[mo]!=null?nm('e2e_p50')[mo].toFixed(1)+'<span class=u> s</span>':'—'}</div><div class=s>itl ${nm('itl_p50')[mo]!=null?Math.round(nm('itl_p50')[mo]*1000)+' ms':'—'}</div></div>
  <div class=card><div class=l>last hour</div><div class=v>${Math.round(nm('requests_1h')[mo]||0)}<span class=u> req</span></div><div class=s>${esc(fmtK(nm('tokens_1h')[mo]||0))} tokens</div></div>
  <div class=card><div class=l>prefix cache</div><div class=v>${nm('prefix_hit')[mo]!=null?Math.round(nm('prefix_hit')[mo]):'—'}<span class=u>% hit</span></div><div class=s>${(nm('preempt_rate')[mo]||0)>0?'<span class=warn>'+(nm('preempt_rate')[mo]*60).toFixed(1)+' preempt/min</span>':'no preemptions'}</div></div></div>
  <div class=charts>
   ${chart('throughput (1 m avg)',[{name:'generated',pts:(S.gen_tps||{})[mo],cls:'c1'},{name:'prompt',pts:(S.prompt_tps||{})[mo],cls:'c0'}],v=>Math.round(v)+' tok/s')}
   ${chart('speed per phase',[{name:'decode',pts:(S.decode_tps||{})[mo],cls:'c1'},{name:'prefill ÷100',pts:((S.prefill_tps||{})[mo]||[]).map(p=>[p[0],p[1]==null?null:p[1]/100]),cls:'c0'}],v=>Math.round(v)+' tok/s')}
   ${chart('prefill share of busy time',[{pts:(S.prefill_share||{})[mo],cls:'c3'}],pct,{max:100})}
   ${chart('prefill / queue time p50',[{name:'prefill',pts:(S.prefill_p50||{})[mo],cls:'c3'},{name:'queue',pts:(S.queue_p50||{})[mo],cls:'c4'}],v=>v.toFixed(2)+' s')}
   ${chart('requests',[{name:'run',pts:(S.running||{})[mo],cls:'c2'},{name:'wait',pts:(S.waiting||{})[mo],cls:'c4'}],v=>Math.round(v))}
   ${chart('kv cache',[{pts:(S.kv_pct||{})[mo]}],pct,{max:100})}
   ${chart('ttft',[{name:'p50',pts:(S.ttft_p50||{})[mo],cls:'c0'},{name:'p95',pts:(S.ttft_p95||{})[mo],cls:'c7'}],v=>v.toFixed(2)+' s')}
   ${chart('e2e latency p50',[{pts:(S.e2e_p50||{})[mo]}],v=>v.toFixed(1)+' s')}
   ${chart('prefix-cache hit',[{pts:(S.prefix_hit||{})[mo],cls:'g'}],pct,{max:100})}</div>`).join('');
 return head+cards}
function tabOverview(m){
 if(!m||m.loading||m.error)return mGuard(m);const S=m.series||{};
 return `<h3>Last hour</h3><div class=charts>
  ${chart('cpu',[{pts:S.cpu}],pct,{max:100})}
  ${chart('memory',[{pts:S.mem_used}],fmtBytes,{max:m.mem_total||undefined})}
  ${chart('network',[{name:'↑',pts:S.net_tx,cls:'g'},{name:'↓',pts:S.net_rx,cls:'c0'}],fmtBits)}
  ${chart('disk i/o',[{pts:S.disk_io}],b=>fmtBytes(b)+'/s')}
  ${chart('gpu',[{pts:S.gpu_util,cls:'a'}],pct,{max:100})}
  ${chart('load (1m)',[{pts:S.load1}],v=>v.toFixed(2))}</div>`}
function tabNetwork(m){const g=mGuard(m);if(g)return g;
 const rows=(m.nics||[]).map(n=>{const st=n.operstate;
  return `<tr><td>${esc(n.device)}</td>
   <td class="${st=='up'?'ok':st=='down'?'mut':''}">${esc(st||'—')}</td>
   <td>${n.speed_bps?esc(fmtBits(n.speed_bps)):'—'}</td><td class=mut>${esc(n.duplex||'')}</td>
   <td class="${n.errors_1h?'bad':''}">${n.errors_1h??'—'}</td><td class="${n.drops_1h?'warn':''}">${n.drops_1h!=null?Math.round(n.drops_1h):'—'}</td>
   <td>${spark([{pts:n.tx,cls:'g'},{pts:n.rx,cls:'c0'}])}</td><td class=mut>${esc(n.address||'')}</td></tr>`}).join('');
 const busy=(m.nics||[]).filter(n=>n.operstate=='up').map(n=>
  chart(n.device,[{name:'↑',pts:n.tx,cls:'g'},{name:'↓',pts:n.rx,cls:'c0'}],fmtBits)).join('');
 return `<h3>Interfaces</h3>
 <table><tr><th>nic</th><th>state</th><th>link</th><th>duplex</th><th>err 1h</th><th>drop 1h</th><th>↑/↓ 1h</th><th>mac</th></tr>${rows||'<tr><td class=mut>none</td></tr>'}</table>
 <div class=mut style="font:10.5px var(--mono);margin-top:4px">link = negotiated speed · a gigabit port showing 100 Mb/s, or rising err/drop, is a cable or port fault</div>
 <h3>Traffic (last hour)</h3><div class=charts>${busy||'<div class=mut>no interface up</div>'}</div>`}
function tabStorage(m){const g=mGuard(m);if(g)return g;
 const fs=(m.filesystems||[]).map(x=>{const used=x.size&&x.avail!=null?100*(1-x.avail/x.size):null,
   ino=x.files?100*(1-(x.files_free||0)/x.files):null;
  return `<tr><td>${esc(x.mount)}</td><td class=mut>${esc((x.device||'').replace('/dev/',''))} · ${esc(x.fstype||'')}</td>
   <td>${esc(fmtBytes(x.size-(x.avail||0)))} / ${esc(fmtBytes(x.size))}</td><td style=width:26%>${bar(used)}</td>
   <td class="${ino>90?'bad':ino>75?'warn':'mut'}">${ino!=null?Math.round(ino)+'% ino':''}</td></tr>`}).join('');
 const nv=(m.nvme||[]).map(d=>`<tr><td>${esc(d.device)}</td><td class=mut>${esc(d.model||'')}</td>
   <td class="${(d.wear||0)>=0.8?'bad':(d.wear||0)>=0.5?'warn':''}">${d.wear!=null?Math.round(d.wear*100)+'%':'—'}</td>
   <td>${fmtT(d.temp)}</td><td class="${d.media_errors?'bad':''}">${d.media_errors??'—'}</td>
   <td class="${d.critical?'bad':'ok'}">${d.critical?'WARN '+d.critical:'ok'}</td>
   <td>${d.spare!=null?Math.round(d.spare*100)+'%':'—'}</td><td>${d.hours!=null?Math.round(d.hours).toLocaleString()+' h':'—'}</td>
   <td class=mut>${d.unsafe??''}</td></tr>`).join('');
 const io=Object.entries(m.io||{}).map(([d,pts])=>chart(d,[{pts}],b=>fmtBytes(b)+'/s')).join('');
 return `<h3>Filesystems</h3>
 <table><tr><th>mount</th><th>dev</th><th>used</th><th></th><th>inodes</th></tr>${fs||'<tr><td class=mut>none</td></tr>'}</table>
 <h3>NVMe health</h3>
 <table><tr><th>dev</th><th>model</th><th>wear</th><th>temp</th><th>media err</th><th>critical</th><th>spare</th><th>powered</th><th>unsafe off</th></tr>${nv||'<tr><td class=mut>no NVMe data (nvme-cli collector)</td></tr>'}</table>
 <h3>Disk I/O (last hour)</h3><div class=charts>${io||'<div class=mut>no data</div>'}</div>`}
function tabGpu(m,f){const g=mGuard(m);if(g)return g;
 const S=m.series||{},gpus=(m.info||[]);
 if(!gpus.length)return '<div class=empty>no NVIDIA GPU reported</div>';
 return gpus.map(i=>{const k=i.gpu,mu=(m.mem_used||{})[k],mt=(m.mem_total||{})[k];
  return `<h3>GPU ${esc(k)} · ${esc(i.name||'')}</h3>
  <div class=kv><span class=k>driver</span><span>${esc(i.driver||'—')}</span>
   <span class=k>memory</span><span>${mu!=null&&mt?esc(fmtBytes(mu))+' / '+esc(fmtBytes(mt)):
    'unified memory — compute processes hold '+esc(fmtBytes(m.proc_mem))}</span></div>
  <div class=charts>
   ${chart('utilisation',[{pts:(S.util||{})[k],cls:'a'}],pct,{max:100})}
   ${chart('temperature',[{pts:((S.temp||{})[k]||[]).map(([t,v])=>[t,v==null?v:tval(v)]),cls:'w'}],v=>Math.round(v)+'°'+TU)}
   ${chart('power',[{pts:(S.power||{})[k]}],v=>Math.round(v)+' W')}
   ${chart('SM clock',[{pts:(S.clock||{})[k],cls:'g'}],v=>Math.round(v)+' MHz')}</div>`}).join('')}
function tabHealth(m){const g=mGuard(m);if(g)return g;
 const off=m.time_offset_s!=null?Math.abs(m.time_offset_s)*1000:null;
 const cs=(m.containers||[]).map(c=>`<tr><td>${esc(c.name)}</td><td class=mut>${esc(c.image)}</td></tr>`).join('');
 return `<h3>System</h3><div class=kv>
  <span class=k>exporter</span><span class="${m.exporter_up?'ok':'bad'}">${m.exporter_up?'up':'DOWN'}${m.textfile_age_s!=null?` · gpu/docker collector ran ${Math.round(m.textfile_age_s)}s ago`:''}</span>
  <span class=k>failed units</span><span class="${(m.failed_units||[]).length?'warn':'ok'}">${(m.failed_units||[]).length?esc(m.failed_units.join(', '))+` <span class=golnk onclick="goTo(${esc(JSON.stringify({host:SEL,panel:'hygiene'}))})">fix in Hygiene →</span>`:'none'}</span>
  <span class=k>reboot</span><span class="${m.reboot?'warn':'ok'}">${m.reboot?`required (/var/run/reboot-required) <span class=golnk onclick="goTo(${esc(JSON.stringify({host:SEL,panel:'hygiene'}))})">Hygiene →</span>`:'not required'}${rbEligible(SEL)?' '+rbBtn(SEL,'lnk'):''}</span>
  <span class=k>updates</span><span>${m.upgrades||0} pending${m.autoremove?` · ${m.autoremove} autoremovable`:''}</span>
  <span class=k>clock</span><span class="${m.time_synced?'ok':'warn'}">${m.time_synced?'synchronised':'NOT synchronised'}${off!=null?` · offset ${off.toFixed(off<1?2:0)} ms`:''}</span>
  <span class=k>booted</span><span>${m.boot_ts?esc(fmtTs(m.boot_ts))+' ('+esc(agoS(m.boot_ts))+')':'—'}</span></div>
 <h3>Containers (${(m.containers||[]).length})</h3>
 <table><tr><th>name</th><th>image</th></tr>${cs||'<tr><td class=mut>none running</td></tr>'}</table>${sigHealth(m)}`}
/* the early warnings' sources for this machine: sshd logins, its RoCE ports, its NFS mounts (rules.yml "signals") */
function sigHealth(m){
 const S=m.ssh||{},n=v=>v==null?'—':Math.round(v),FS={1:'down',2:'init',3:'armed',4:'active',5:'active defer'};
 const bps=v=>v==null?'—':fmtBytes(v)+'/s';
 const ssh=S.known==null?'':`<h3>SSH</h3><div class=kv>
  <span class=k>last 24 h</span><span class="${(S.failed_24h||0)+(S.invalid_24h||0)>50?'warn':''}">${n(S.accepted_24h)} logins · ${n(S.failed_24h)} failed · ${n(S.invalid_24h)} for unknown users</span>
  <span class=k>addresses</span><span>${n(S.known)} a login came from${(S.new||[]).length?` · <span class=warn>new this week: ${S.new.map(x=>esc(x.user+' from '+x.addr+' '+agoS(x.ts))).join('; ')}</span>`:''}</span></div>`;
 const fab=(m.fabric||[]).length?`<h3>RoCE fabric</h3><table><tr><th>port</th><th>state</th><th>rate</th><th>in / out</th><th>errors 1 h</th><th>retransmits</th></tr>${m.fabric.map(f=>`<tr><td>${esc(f.device)}</td><td class="${f.state==4?'ok':'mut'}">${esc(FS[f.state]||f.state)}</td><td>${f.state==4?esc(fmtBytes(f.rate*8).replace('B','b'))+'/s':'—'}</td><td>${f.state==4?esc(bps(f.rx_bps))+' / '+esc(bps(f.tx_bps)):'—'}</td><td class="${f.errors_1h>0?'warn':''}">${n(f.errors_1h)}</td><td class="${f.retrans_s>1?'warn':''}">${f.retrans_s==null?'—':esc(f.retrans_s.toFixed(2))+'/s'}</td></tr>`).join('')}</table>`:'';
 const nfs=(m.nfs||[]).length||m.nfs_running_s?`<h3>NFS mounts</h3>${m.nfs_running_s?`<div class=bad>the probe has been waiting ${esc(fmtDur(m.nfs_running_s))} for a mount (hung?)</div>`:''}<table><tr><th>mount</th><th>write</th><th>read</th><th>4 KiB round trip</th></tr>${(m.nfs||[]).map(x=>`<tr><td title="${esc(x.source||'')}">${esc(x.mountpoint)}</td>${x.ok?`<td>${esc(bps(x.write))}</td><td>${esc(bps(x.read))}</td><td>${esc(((x.small||0)*1000).toFixed(1))} ms</td>`:'<td colspan=3 class=bad>the probe failed</td>'}</tr>`).join('')}</table>${m.nfs_probe_ts?`<div class=mut>probed ${esc(agoS(m.nfs_probe_ts))}, every 5 min</div>`:''}`:'';
 return ssh+fab+nfs}
/* ── schedule: what runs when, and why; inline editing via schedule-set ── */
let EDIT=null;
function rSched(d){
 if(!d)return '<div class=empty>loading…</div>';
 if(d.error)return `<div class="empty bad">${esc(d.error)}</div>`;
 const t=d.tick||{},h=(d.spindle&&d.spindle.holder)||{};
 const head=`<div class=sline>tick every <b>${esc(d.tick_minutes)}m</b> · next ${esc((t.next||'—').replace(/^\w+ /,'').replace(/:\d\d UTC$/,'Z'))}
  ${t.running?'· <span class=warn>tick running</span>':''}${t.timer&&t.timer!='active'?' · <span class=bad>timer '+esc(t.timer)+'</span>':''}
  · ${esc(d.spindle&&d.spindle.name)}: ${h.what?'<span class=warn>'+esc(h.what)+'</span> since '+esc(fmtTs(h.since)):'<span class=ok>free</span>'}
  · failed runs retry after ${esc(d.retry_after)}</div>`;
 const edRow=(host,prof,cur)=>`<tr class=ed><td colspan=7>
  <label>every</label><input id=ed-every value="${esc(cur.every||'')}" placeholder="24h">
  <label>window</label><input id=ed-window value="${esc(cur.window===null?'any':(cur.window||''))}" placeholder="01:00-07:00">
  <button class=act onclick="saveSched('${esc(host)}','${esc(prof)}','set')">save</button>
  <button class=act onclick="saveSched('${esc(host)}','${esc(prof)}','disable')">disable</button>
  ${host&&cur.override?`<button class=act onclick="saveSched('${esc(host)}','${esc(prof)}','reset')">reset to default</button>`:''}
  <button class=lnk onclick="EDIT=null;repaint('schedule',PANELS.find(p=>p.id=='schedule')._data)">cancel</button>
  <div class=hint>every: 15m–90d (e.g. 6h, 1d, 2d) · window: HH:MM-HH:MM in UTC (when a backup may START; may wrap midnight) or "any".
  With a window and every ≥ 1d the host runs once per window. ${host?'Blank fields keep the default.':'Applies to every host without an override.'}</div></td></tr>`;
 const isEd=(host,prof)=>EDIT&&EDIT.host===host&&EDIT.profile===prof;
 let rows=Object.entries(d.defaults||{}).map(([prof,r])=>
  `<tr><td><b>all hosts</b></td><td>${esc(prof)}</td><td colspan=2 class=mut>fleet default</td><td>${esc(r.every)}</td><td>${esc(r.window||'any')}</td>
   <td><button class=lnk onclick="editSched('','${esc(prof)}')">edit</button></td></tr>`
  +(isEd('',prof)?edRow('',prof,r):'')).join('');
 rows+=(d.rows||[]).map(r=>{
  const main=r.enabled?
   `<tr class="${hlRow('schedule',r.host)}"><td><span class=golnk onclick="goTo(${esc(JSON.stringify({host:r.host,panel:'detail',tab:'backups'}))})">${esc(r.host)}</span></td><td>${esc(r.profile)}${r.override?'<span class="tag info">override</span>':''}</td>
    <td class="${HCLS[r.health]||''}">${r.last_ts?esc(agoS(r.last_ts)):'never'}</td>
    <td>${r.due?'<span class=warn>due now</span>':esc(fmtTs(r.next_ts))}</td>
    <td>${esc(fmtDur(r.every))}</td><td>${esc(r.window||'any')}</td>
    <td><button class=lnk onclick="editSched('${esc(r.host)}','${esc(r.profile)}')">edit</button></td></tr>
    ${r.failed_at?`<tr><td></td><td colspan=6 class=warn style="font-size:11px">last attempt failed ${esc(agoS(r.failed_at))} — ${esc(r.why)}</td></tr>`:''}`
   :`<tr><td>${esc(r.host)}</td><td>${esc(r.profile)}<span class="tag info">override</span></td><td colspan=4 class=mut>disabled for this host</td>
    <td><button class=lnk onclick="editSched('${esc(r.host)}','${esc(r.profile)}')">edit</button></td></tr>`;
  return main+(isEd(r.host,r.profile)?edRow(r.host,r.profile,{override:r.override,window:''}):'')}).join('');
 return head+`<table><tr><th>host</th><th>profile</th><th>last</th><th>next</th><th>every</th><th>window</th><th></th></tr>${rows}</table>`}
function editSched(host,profile){EDIT={host,profile};repaint('schedule',PANELS.find(p=>p.id=='schedule')._data)}
async function saveSched(host,profile,mode){
 const p={profile,mode};if(host)p.host=host;
 if(mode=='set'){const e=($('ed-every').value||'').trim(),w=($('ed-window').value||'').trim();
  if(e)p.every=e;if(w)p.window=w;
  if(!e&&!w){alert('give an interval and/or a window');return}}
 if(mode=='disable'&&!confirm(`Stop scheduling ${profile} ${host?'on '+host:'for ALL hosts'}?`))return;
 EDIT=null;await act('setSchedule',p);
 setTimeout(()=>{tick(PANELS.find(x=>x.id=='schedule'));tick(PANELS.find(x=>x.id=='fleet'))},1500)}
/* ── config (/config): what the backups cover — docker volumes, database dumps,
   extra excludes, retention. Saved through `noctyx policy-set` (validates,
   writes the site config, audits) which then pushes the result to every host. ── */
let CFGDIRTY=false,CFGJOB=null;
const lines=v=>(v||'').split('\n').map(x=>x.trim()).filter(Boolean);
function cfgDirty(){CFGDIRTY=true;const b=$('cf-state');if(b){b.textContent='unsaved changes';b.className='warn'}}
/* tri-state as a segmented control: the fleet default, or on / off for this host (data-v holds it) */
function triSel(id,val,eff){const v=val===true?'on':val===false?'off':'';
 return `<div class=seg id="${id}" data-v="${v}">${[['',`default (${eff?'on':'off'})`],['on','on'],['off','off']].map(([k,l])=>`<button type=button class="${k==v?'on':''}" onclick="segPick(this,'${k}')">${l}</button>`).join('')}</div>`}
function segPick(btn,v){const s=btn.parentElement;s.dataset.v=v;s.querySelectorAll('button').forEach(b=>b.classList.toggle('on',b===btn));cfgDirty()}
/* ── /machines: every machine on one page — worst first, each saying why it needs a look, with what
   it serves, its vitals, storage (and when it fills), backups, the early warnings, updates and alerts. ── */
function mxBar(pct,warn=85,bad=92){const c=pct>=bad?'bad':pct>=warn?'warn':'';return `<span class="mxbarm ${c}"><i style="width:${Math.max(0,Math.min(100,pct||0))}%"></i></span>`}
function mxCard(m){
 const st={ok:'ok',warn:'warn',bad:'bad'}[m.status],dot=`<span class="dot ${st=='ok'?'ok':st=='warn'?'warn':'bad'}"></span>`;
 const go=`goMx('${esc(m.name)}')`;
 const why=m.why.length?`<div class=mxwhy>${m.why.map(w=>`<span class="${w.level}">${esc(w.text)}</span>`).join('')}</div>`
  :'<div class="mxwhy ok">✓ all good</div>';
 const mem=m.mem_total?m.mem_used/m.mem_total*100:null;
 const rows=[];
 rows.push(['vitals',m.up===0?'<span class=bad>no metrics</span>':`CPU ${m.cpu!=null?Math.round(m.cpu)+'%':'—'}${m.load!=null?' · load '+m.load.toFixed(1):''} · mem ${mxBar(mem,90,97)}${m.mem_total?esc(fmtBytes(m.mem_used))+' / '+esc(fmtBytes(m.mem_total)):'—'}${m.sys_temp!=null?' · '+esc(fmtT(m.sys_temp)):''}${m.nvme_temp!=null?' · NVMe '+esc(fmtT(m.nvme_temp)):''}`]);
 if(m.gpu_util!=null||m.gpu_name)rows.push(['gpu',`${esc(m.gpu_name||'GPU')} · ${m.gpu_util!=null?Math.round(m.gpu_util)+'%':'—'}${m.gpu_temp!=null?' · '+esc(fmtT(m.gpu_temp)):''}${m.gpu_power!=null?' · '+Math.round(m.gpu_power)+' W':''}`]);
 rows.push(['serves',m.serves.length?m.serves.map(x=>x.worker_for?`${esc(x.model||'?')} <span class=mut>(worker for ${esc(x.worker_for)})</span>`:x.unscraped?`${esc(x.model||'?')} <span class=mut>(no metrics)</span>`:`<b>${esc(x.model||'?')}</b> · ${x.tps!=null?x.tps.toFixed(1)+' tok/s':'—'} · ${Math.round(x.running||0)} running${x.waiting?` · <span class=warn>${Math.round(x.waiting)} waiting</span>`:''}${x.kv_pct!=null?` · <span class="${x.kv_pct>90?'bad':x.kv_pct>70?'warn':''}">KV ${Math.round(x.kv_pct)}%</span>`:''}`).join('<br>'):'<span class=mut>no model</span>']);
 const grow=m.root_use_per_day>0?` · +${esc(fmtBytes(m.root_use_per_day))}/day${m.root_full_days!=null?(m.root_full_days>365?' · not filling up soon':` → full in <span class="${m.root_full_days<14?'warn':''}">${m.root_full_days<1?'<1':Math.round(m.root_full_days)} d</span>`):''}`:m.root_use_per_day<0?' · shrinking':'';
 const other=(m.disks||[]).filter(d=>d.mnt!='/').map(d=>`${esc(d.mnt)} ${esc(d.pct)}`).join(' · ');
 rows.push(['storage',`/ ${mxBar(m.root_pct)}${m.root_pct!=null?Math.round(m.root_pct)+'%':'—'}${m.root_free!=null?' · '+esc(fmtBytes(m.root_free))+' free':''}${grow}${other?`<br><span class=mut>${other}</span>`:''}`]);
 rows.push(['backups',m.backups.length?m.backups.map(b=>`<span class="${b.health=='ok'&&!b.failed_at?'ok':b.health=='late'||b.failed_at?'warn':'bad'}">${esc(b.profile)} ${esc(b.health)}</span> ${b.last_ts?esc(agoS(b.last_ts)):'never'}${b.failed_at?' · <span class=warn>last attempt failed</span>':''}`).join(' · ')+(m.snapshots!=null?` <span class=mut>· ${m.snapshots} snapshots</span>`:''):'<span class=mut>not backed up (monitored only)</span>']);
 const F=m.fabric;
 if(F)rows.push(['fabric',`${F.active.length?F.active.map(p=>esc(fmtBytes(p.rate*8).replace('B','b'))+'/s').join(' + ')+' up':'<span class=warn>no port up</span>'} <span class=mut>(${F.active.length}/${F.ports} ports)</span> · ${m.fabric_rx!=null?'in '+esc(fmtBytes(m.fabric_rx))+'/s · ':''}<span class="${F.errors_1h>0?'warn':''}">${F.errors_1h!=null?Math.round(F.errors_1h):'—'} errors/h</span>${F.retrans>1?` · <span class=warn>${F.retrans.toFixed(1)} retrans/s</span>`:''}`]);
 if((m.nfs||[]).length||m.nfs_hung_s)rows.push(['nfs',(m.nfs_hung_s?`<span class=bad>a mount hangs (${esc(fmtDur(m.nfs_hung_s))})</span> `:'')+(m.nfs||[]).map(x=>x.ok?`${esc(x.mount)} · write ${esc(fmtBytes(x.write))}/s · read ${esc(fmtBytes(x.read))}/s`:`<span class=bad>${esc(x.mount)}: probe fails</span>`).join('<br>')]);
 const S=m.ssh||{};
 if(S.ok_24h!=null||S.failed_24h!=null)rows.push(['ssh 24 h',`${Math.round(S.ok_24h||0)} logins · <span class="${(S.failed_24h||0)>20?'warn':''}">${Math.round(S.failed_24h||0)} failed</span>${(S.new||[]).length?` · <span class=warn>new: ${S.new.map(x=>esc(x.user+' from '+x.addr)).join(', ')}</span>`:''}`]);
 const upd=[...(m.behind||[]).map(x=>`<span class=warn>${esc(x)}</span>`),...(m.newer||[]).map(x=>`<span class=mut>${esc(x)} out</span>`)];
 const hy=m.hygiene||{};
 rows.push(['system',`${esc(m.os_line||m.os||'')} · ${esc(m.arch||'')}${m.uptime?' · '+esc(m.uptime.replace(/^up /,'up ')):''}${m.reboot?' · <span class=warn>reboot required</span>':''}${m.failed_units?` · <span class=warn>${m.failed_units} failed unit(s)</span>`:''}${m.containers!=null?' · '+Math.round(m.containers)+(Math.round(m.containers)==1?' container':' containers'):''}${hy.items?` · ${hy.items} hygiene item(s)`:''}${upd.length?'<br>'+upd.join(' · '):''}`]);
 if((m.repairs||[]).length)rows.push(['self-repair',m.repairs.map(r=>`<span class="${r.state=='needs_person'?'bad':'warn'}">${esc(r.what)}</span> <span class=mut>${esc(r.why||'')}</span>`).join('<br>')]);
 if((m.alerts||[]).length)rows.push(['alerts',m.alerts.map(a=>`<span class="${a.severity=='critical'?'bad':'warn'}">${esc(a.name)}</span> ${esc(a.summary||'')}`).join('<br>')]);
 return `<div class="mxc ${st}"><div class=mxh>${dot}<b class=go onclick="${go}" title="open everything about ${esc(m.name)}">${esc(m.name)}</b><span class=mut>${esc((m.role||[]).join(' · '))}</span><span class=hspacer></span><a class=lnk href="/machine?host=${esc(encodeURIComponent(m.name))}">open →</a></div>
  ${why}<div class=mxs>${rows.map(([k,v])=>`<span>${esc(k)}</span><span>${v}</span>`).join('')}</div></div>`}
/* ── /machine: one machine, everything at once, with its actions. ‹ › or ← → walk the machines. ── */
var MX=MX||{host:null,job:null,hy:{}};   // var: the /machine view sets it up before this line runs
function mxStep(d){const p=PANELS.find(x=>x.id=='machine'),ms=((p&&p._data)||{}).machines||[];if(!ms.length)return;
 const i=ms.findIndex(m=>m.name==MX.host);mxGo(ms[(i+d+ms.length)%ms.length].name)}
function mxGo(n){if(!n||n==MX.host)return;MX.host=n;history.replaceState(null,'','/machine?host='+encodeURIComponent(n));
 const p=PANELS.find(x=>x.id=='machine');if(p){p._data=undefined;repaint('machine',{loading:true});tick(p)}}
async function mxReload(){const p=PANELS.find(x=>x.id=='machine');if(p){const d=await p.api().catch(e=>({error:String(e)}));p._data=d;repaint('machine',d)}}
async function mxAct(action,params,what,btn){if(btn){btn.disabled=true}
 const r=await act(action,params);if(!r||r.error){if(btn)btn.disabled=false;if(r&&r.error)alert(r.error);return}
 MX.job={id:r.id,what,st:'queued'};mxJobPaint();mxJobPoll()}
async function mxJobPoll(){const j=MX.job;if(!j)return;
 for(;;){await new Promise(z=>setTimeout(z,1500));if(MX.job!==j)return;let s;try{s=await fetchJSON('/api/v1/job/'+j.id)}catch(e){continue}
  j.st=s.done?(s.rc===0?'done':'failed (rc '+s.rc+')'):s.running?'running':'queued';j.last=((s.output||'').trim().split('\n').filter(Boolean).pop()||'').slice(0,140);mxJobPaint();
  if(s.done){setTimeout(mxReload,800);return}}}
function mxJobPaint(){const e=$('m1job');if(!e||!MX.job)return;const j=MX.job;
 e.innerHTML=`<span class="tag ${/done/.test(j.st)?'ok':/failed/.test(j.st)?'bad':'warn'}">${esc(j.st)}</span> ${esc(j.what)} <span class=mut>${esc(j.last||'')}</span>`}
function mxAsk(n){askOpen();$('askq').value=`How is ${n} doing, and is there anything I should do about it?`}
function mxSec(id,title,body){return `<div class=m1sec id="mx-${id}"><h3>${esc(title)}</h3>${body}</div>`}
function mxWhyAct(w,n,S,canBackup){const t=w.text;
 if(/backup/.test(t)&&canBackup)return [`backups`,`<button class=silbtn onclick="event.stopPropagation();mxAct('backupHost',{host:'${esc(n)}'},'backup ${esc(n)}',this)">backup now</button>`];
 if(/reboot required/.test(t))return [`health`,rbEligible(n)?rbBtn(n,'silbtn'):''];
 if(/^behind/.test(t))return [`components`,''];
 if(/hygiene|failed unit/.test(t))return [`hygiene`,''];
 if(/needs a person/.test(t))return [`repair`,`<button class=silbtn onclick="event.stopPropagation();mxAct('healNow',{},'repair now',this)">repair now</button>`];
 if(/^alert/.test(t))return [`alerts`,''];
 if(/root disk/.test(t))return [`storage`,''];
 if(/ssh|fabric|NFS/.test(t))return [`health`,''];
 return [`alerts`,'']}
function mxScroll(id){const e=$('mx-'+id);if(e)e.scrollIntoView({behavior:'smooth',block:'start'})}
function rMachine(d){
 if(!d||d.loading)return '<div class=empty>loading…</div>';
 if(d.error)return `<div class="empty bad">${esc(d.error)}</div>`;
 const n=d.host,S=d.summary||{},F=d.facts||{},T=d.tabs||{},H=d.host_cfg||{};
 SEL=n;OVR=OVR||{hosts:[H]};if(!(OVR.hosts||[]).some(x=>x.name==n))OVR.hosts=[...(OVR.hosts||[]),H];
 const canBackup=!H.monitored_only&&H.os!='macos';
 const dot=st=>st=='ok'?'ok':st=='warn'?'warn':'bad';
 const nav=`<div class=m1nav><button class="act arrow" onclick="mxStep(-1)" title="previous machine (←)">‹</button>
  <select onchange="mxGo(this.value)">${(d.machines||[]).map(m=>`<option value="${esc(m.name)}"${m.name==n?' selected':''}>${m.status=='ok'?'●':m.status=='warn'?'▲':'■'} ${esc(m.name)}</option>`).join('')}</select>
  <button class="act arrow" onclick="mxStep(1)" title="next machine (→)">›</button>
  <span class=mut>${(d.machines||[]).map(m=>`<span class="dot ${dot(m.status)} go" title="${esc(m.name)}" onclick="mxGo('${esc(m.name)}')"></span>`).join(' ')}</span>
  <span class=hspacer></span><span class=mut>← → to walk · updated ${esc(agoS(d.ts))}</span></div>`;
 const why=(S.why||[]).length?(S.why).map(w=>{const [sec,btn]=mxWhyAct(w,n,S,canBackup);return `<span class="chip ${w.level}" onclick="mxScroll('${sec}')" title="show the details">${esc(w.text)}${btn}</span>`}).join('')
  :'<span class=ok>✓ all good</span>';
 const head=`<div class=m1head><span class="dot ${dot(S.status)}"></span><span class=big>${esc(n)}</span>
  <span class=mut>${esc((S.role||[]).join(' · '))}</span><span class=mut>${esc(F.address||'')}${F.ip&&F.ip!=F.address?' · '+esc(F.ip):''}</span></div>
  <div class=m1why>${why}</div>
  <div class=m1acts>${canBackup?`<button class=act onclick="mxAct('backupHost',{host:'${esc(n)}'},'backup ${esc(n)}',this)">backup now</button>
   <button class=act onclick="mxAct('verifyHost',{host:'${esc(n)}'},'verify ${esc(n)}',this)">verify now</button>
   <button class=act onclick="if(confirm('Take a checkpoint of ${esc(n)} now (a system snapshot)?'))mxAct('takeCheckpoint',{host:'${esc(n)}'},'checkpoint ${esc(n)}',this)">checkpoint now</button>`:''}
   <button class=act onclick="mxAct('healNow',{},'repair now',this)" title="run noctyx heal now">repair now</button>
   ${rbEligible(n)?rbBtn(n):''}
   <button class=act onclick="mxAsk('${esc(n)}')">ask about it</button>
   <a class=lnk href="/heal">self-repair log →</a>${canBackup?` <a class=lnk href="/#host=${esc(encodeURIComponent(n))}&panel=browse">snapshots →</a>`:''}
   <span id=m1job class=m1job></span></div>`;
 // storage: the summary's forecast over the report's filesystems
 const grow=S.root_use_per_day>0?`root disk: +${esc(fmtBytes(S.root_use_per_day))}/day over the last week${S.root_full_days!=null?(S.root_full_days>365?', not filling up soon':`, <span class="${S.root_full_days<14?'warn':''}">full in about ${Math.round(S.root_full_days)} days</span>`):''}`:'root disk: not growing';
 // backups
 const brows=(d.schedule||[]).map(r=>`<tr><td>${esc(r.profile)}</td><td class="${HCLS[r.health]||''}">${r.last_ts?esc(agoS(r.last_ts)):'never'}</td><td>${esc(fmtDur(r.every))}${r.window?' · '+esc(r.window):''}</td><td>${r.due?'<span class=warn>due now</span>':esc(fmtTs(r.next_ts))}</td><td><span class="tag ${HCLS[r.health]||''}">${esc(r.health)}</span>${r.failed_at?' <span class=warn>last attempt failed</span>':''}</td></tr>`).join('');
 const snaps=(d.snapshots.recent||[]).map(r=>`<tr><td><code>${esc(r.id)}</code></td><td>${esc(fmtTs(r.ts))}</td><td>${esc(r.profile)}</td></tr>`).join('');
 const jobs=(F.last_jobs||[]).slice(0,6).map(j=>`<tr><td>${esc(fmtTs(j.ts))}</td><td>${esc(j.event||'')}</td><td class="${(j.rc??0)==0?'ok':'bad'}">${j.rc??''}</td><td>${esc(j.snapshot||'')}</td></tr>`).join('');
 const busy=F.agent_busy?`<div class=sline><span class="tag warn">running</span> a job holds the agent${F.progress?` · ${esc(F.progress.profile)} ${esc(fmtDur(+F.progress.elapsed||0))} · uploaded ${esc(F.progress.uploaded_mb)} MB`:''}</div>`:'';
 const backups=canBackup?`${busy}<table><tr><th>profile</th><th>last</th><th>every</th><th>next</th><th>health</th></tr>${brows||'<tr><td colspan=5 class=mut>not scheduled</td></tr>'}</table>
  <h3>Recent snapshots (${d.snapshots.count??0})</h3><table>${snaps||'<tr><td class=mut>none</td></tr>'}</table>
  ${jobs?`<h3>Agent jobs</h3><table><tr><th>when</th><th>job</th><th>rc</th><th>snapshot</th></tr>${jobs}</table>`:''}
  <div class=mut style="font:11px var(--mono)">agent ${esc((F.agent||[])[0]||'—')} · profiles ${esc((F.agent||[])[1]||'—')}</div>`
  :`<div class=mut>monitored only: Noctyx does not back this machine up${H.os=='macos'?' (Time Machine does)':''}.</div>`;
 // components
 const CMARK={ok:'ok',upgrade:'warn',behind:'bad',missing:'bad',ahead:'mut',unknown:'mut',skipped:'mut'};
 const comps=(d.versions||[]).map(r=>`<tr><td>${esc(r.component)}</td><td>${esc(r.installed||'—')}</td><td>${esc(r.pinned||'—')}</td><td>${esc(r.latest||'—')}</td><td class=${CMARK[r.state]||'mut'}>${esc(r.detail)}${r.fix||r.state=='skipped'?`<div class=compbtns>${compBtns(n,r)}</div>`:''}</td></tr>`).join('');
 // hygiene
 const hyItems=((d.hygiene||{}).items||[]).filter(i=>!i.fixed);
 const hy=hyItems.map(i=>{const e=MX.hy[i.id];const btn=i.tier&&i.tier!='auto'?(e?`<span class=mut>${esc(e)}</span>`:`<button class=silbtn onclick="if(confirm('${esc((i.fix||'fix').replace(/'/g,''))} on ${esc(n)}?')){MX.hy['${esc(i.id)}']='sent';mxAct('hygieneFix',{host:'${esc(n)}',items:['${esc(i.id)}']},'hygiene fix ${esc(i.id)} on ${esc(n)}',this)}">${esc(i.fix||'fix')}${i.bytes?' (frees '+esc(fmtBytes(i.bytes))+')':''}</button>`):`<span class=mut>${i.tier=='auto'?'fixed by the daily run':'report only'}</span>`;
   return `<tr><td class="${i.severity=='warning'?'warn':''}">${esc(i.title)}<div class=mut>${esc(i.detail||'')}</div></td><td>${btn}</td></tr>`}).join('');
 // alerts and self-repair
 const al=(d.alerts||[]).map(a=>`<tr><td class="${a.severity=='critical'?'bad':a.severity=='warning'?'warn':''}">${esc(a.name)}<div class=mut>${esc(a.summary||'')}</div>${a.triage?`<div class=srnote>${esc(a.triage)}</div>`:''}${a.selfrepair?`<div class=srnote>self-repair: ${esc(a.selfrepair.why||a.selfrepair.how||'')}</div>${diagNote(a.selfrepair.diagnosis)}`:''}</td><td class=nowrap>${esc(agoS(a.since))}</td>
  <td class=nowrap>${a.fingerprint?`<button class=silbtn onclick="silenceAlert('${esc(a.fingerprint)}',60,this).then(mxReload)">silence 1 h</button> <button class=silbtn onclick="silenceAlert('${esc(a.fingerprint)}',1440,this).then(mxReload)">24 h</button>`:''}${a.fix?`<div class=compbtns>${compBtns(n,a.fix)}</div>`:''}</td></tr>`).join('');
 const rp=(S.repairs||[]).map(r=>`<tr><td class="${r.state=='needs_person'?'bad':'warn'}">${esc(r.what)}<div class=mut>${esc(r.why||'')}</div>${diagNote(r.diagnosis)}</td><td><button class=silbtn onclick="mxAct('healNow',{},'repair now',this)">repair now</button></td></tr>`).join('');
 // changes
 const CH={'in effect':'ok',undone:'mut',rolled_back:'warn',rollback_failed:'bad',refused:'mut'};
 const ch=(d.changes||[]).map(r=>`<tr><td class=nowrap>${esc(agoS(r.ts_epoch||r.ts))}</td><td>${esc(r.what)}</td><td><span class="tag ${CH[r.state]||''}">${esc(r.state)}</span></td><td>${r.revertible?`<button class=silbtn onclick="srRevert('${esc(r.id)}','${esc(String(r.what).replace(/'/g,''))}',${['prometheus','alertmanager'].includes(r.component)},this).then(()=>setTimeout(mxReload,4000))">undo</button>`:''}</td></tr>`).join('');
 // system
 const devs=(F.blockdevs||[]).map(b=>`${esc(b.name)} ${b.size_gb} GB <span class=mut>${esc(b.serial||'')}</span>`).join(' · ');
 const sys=`<div class=kv><span class=k>os</span><span>${esc((F.os_lines||[]).join(' · ')||S.os_line||'')}</span>
  <span class=k>cpu</span><span>${esc((F.cpu||[]).join(' · '))}</span>
  <span class=k>memory</span><span>${F.mem_used_gb??'—'} / ${F.mem_total_gb??'—'} GB</span>
  <span class=k>uptime</span><span>${esc((F.uptime_lines||[]).join(' · '))}</span>
  <span class=k>addresses</span><span>${esc(F.ip||F.address||'')}</span>
  ${(F.gpu||[]).length?`<span class=k>gpu</span><span>${esc(F.gpu.join(' / '))}</span>`:''}
  ${devs?`<span class=k>disks</span><span>${devs}</span>`:''}</div>`;
 const gpu=(T.gpu&&(T.gpu.info||[]).length)?tabGpu(T.gpu,F):(S.gpu_util!=null?`<div class=kv><span class=k>${esc(S.gpu_name||'gpu')}</span><span>${Math.round(S.gpu_util)}% · ${esc(fmtT(S.gpu_temp))} · ${S.gpu_power!=null?Math.round(S.gpu_power)+' W':'—'}</span></div>`:'<div class=empty>no GPU reported</div>');
 const secs=[
  mxSec('vitals','Vitals (last hour)',tabOverview(T.overview)),
  mxSec('llm','LLM',tabLLM(T.llm)),
  mxSec('gpu','GPU',gpu),
  mxSec('storage','Storage',`<div class=sline>${grow}</div>`+tabStorage(T.storage)),
  mxSec('network','Network',tabNetwork(T.network)),
  mxSec('health','Health, fabric, NFS, ssh',tabHealth(T.health)),
  mxSec('backups','Backups',backups),
  mxSec('components','Components',`<table><tr><th>component</th><th>installed</th><th>pin</th><th>latest</th><th></th></tr>${comps||'<tr><td class=mut colspan=5>not checked yet</td></tr>'}</table>${d.versions_ts?`<div class=mut style="font:10.5px var(--mono)">checked ${esc(agoS(d.versions_ts))}</div>`:''}`),
  mxSec('hygiene','Hygiene',hy?`<table>${hy}</table>`:'<div class=ok>nothing open</div>'),
  mxSec('alerts','Alerts',al?`<table>${al}</table>`:'<div class=ok>none firing</div>'),
  mxSec('repair','Self-repair',rp?`<table>${rp}</table>`:'<div class=ok>nothing open</div>'),
  mxSec('changes','Changes Noctyx made',ch?`<table>${ch}</table>`:'<div class=mut>none recorded</div>'),
  mxSec('system','System',sys)];
 setTimeout(mxJobPaint,0);
 return `<div class=m1>${nav}${head}<div class=m1grid>${secs.join('')}</div>${sshlSec(n)}</div>`}
/* SSH logins: the machine's own log (sshlog.py), read when its page opens, as one table. Its state lives here,
   not in the page's data, so the 15 s refresh keeps the filters, the sort and the rows shown. */
const SSHL={host:null,days:7,head:false,res:'all',q:'',sort:'ts',dir:-1,n:100,view:'events',d:null,busy:false,err:null};
const SSHRES={accepted:'ok',failed:'bad','gave up':'warn',invalid:'bad','too many':'bad'};
function sshlPaint(){const p=PANELS.find(x=>x.id=='machine');if(p&&p._data)repaint('machine',p._data)}
async function sshlLoad(refresh){const h=SSHL.host;SSHL.busy=true;sshlPaint();
 try{const r=await fetch(`/api/v1/ssh-log/${encodeURIComponent(h)}?days=${SSHL.days}${SSHL.head?'&head=1':''}${refresh?'&refresh=1':''}`,{headers:{Accept:'application/json'}});
  const d=await r.json().catch(()=>({error:'HTTP '+r.status}));
  if(SSHL.host==h){if(r.ok&&!d.error){SSHL.d=d;SSHL.err=null}else SSHL.err=d.error||('HTTP '+r.status)}}
 catch(e){if(SSHL.host==h)SSHL.err=String(e)}
 if(SSHL.host==h){SSHL.busy=false;sshlPaint()}}
function sshlSet(k,v){SSHL[k]=v;if(k=='days'||k=='head'){sshlLoad()}else{if(k!='n')SSHL.n=100;sshlPaint()}}
function sshlSort(k){SSHL.dir=SSHL.sort==k?-SSHL.dir:(['ts','refused','accepted'].includes(k)?-1:1);SSHL.sort=k;sshlPaint()}
const sshTs=t=>new Date(t*1000).toISOString().slice(0,19).replace('T',' ')+'Z';
function sshlRows(){const d=SSHL.d,q=SSHL.q.trim().toLowerCase();
 return (d.events||[]).filter(e=>(SSHL.res=='all'||(SSHL.res=='accepted')==(e.result=='accepted'))
  &&(!q||[e.user,e.addr,(d.names||{})[e.addr],e.method,e.result,e.key,e.key_name].join(' ').toLowerCase().includes(q)))}
function sshlTh(k,l){const on=SSHL.sort==k;return `<th class=sortable onclick="sshlSort('${k}')">${esc(l)}${on?(SSHL.dir<0?' ▾':' ▴'):''}</th>`}
function sshlSorted(rows,key){const k=SSHL.sort,dir=SSHL.dir;
 return rows.slice().sort((a,b)=>{const x=key(a,k),y=key(b,k);return (x<y?-1:x>y?1:0)*dir||(b.ts||b.last)-(a.ts||a.last)})}
function sshlSec(n){
 if(SSHL.host!=n){Object.assign(SSHL,{host:n,d:null,err:null,n:100,q:''});setTimeout(()=>sshlLoad(),0)}
 const d=SSHL.d,b=(k,v,l)=>`<button class="${SSHL[k]==v?'on':''}" onclick="sshlSet('${k}',${typeof v=='string'?`'${v}'`:v})">${l}</button>`;
 const ctl=`<div class=sshctl><span class=seg>${b('days',1,'24 h')}${b('days',7,'7 days')}${b('days',30,'30 days')}</span>
  <span class=seg>${b('res','all','all')}${b('res','accepted','accepted')}${b('res','refused','refused')}</span>
  <span class=seg>${b('view','events','each login')}${b('view','sources','by address')}</span>
  <label class=mut title="Noctyx's checks, backups and sessions from the head all log in with the head's key"><input type=checkbox ${SSHL.head?'checked':''} onchange="sshlSet('head',this.checked)"> the head's key too</label>
  <input class=sshq type=search placeholder="filter: user, address, key…" value="${esc(SSHL.q)}" oninput="sshlSet('q',this.value)">
  <span class=hspacer></span><button class=act onclick="sshlLoad(1)" ${SSHL.busy?'disabled':''}>${SSHL.busy?'reading the log…':'read again'}</button></div>`;
 let body;
 if(SSHL.err)body=`<div class="empty bad">${esc(SSHL.err)}</div>`;
 else if(!d)body=`<div class=empty>reading ${esc(n)}'s log…</div>`;
 else{const S=d.summary||{},rows=sshlRows(),nm=a=>(d.names||{})[a],ok=rows.filter(e=>e.result=='accepted').length;
  const line=`<div class=sline><b class=ok>${ok}</b> accepted · <b class="${rows.length-ok?'bad':''}">${rows.length-ok}</b> refused${SSHL.q||SSHL.res!='all'?' (filtered)':''}
   ${S.head_key?` · <span class=mut>${S.head_key} by the head's key ${SSHL.head?'included':'not shown'}</span>`:''}
   <span class=mut> · last ${d.days==1?'24 h':d.days+' days'}, read ${esc(agoS(d.ts))} in ${d.seconds}s${d.truncated?' · <span class=warn>only the newest lines were read</span>':''}${d.matching>d.shown?` · <span class=warn>newest ${d.shown} of ${d.matching}</span>`:''}</span></div>`;
  const who=a=>`${esc(a)}${nm(a)?` <span class=tag>${esc(nm(a))}</span>`:''}`;
  if(SSHL.view=='sources'){const m={};
   for(const e of rows){const s=m[e.addr]||(m[e.addr]={addr:e.addr,accepted:0,refused:0,first:e.ts,last:e.ts,users:{},keys:{}});
    s[e.result=='accepted'?'accepted':'refused']++;s.first=Math.min(s.first,e.ts);s.last=Math.max(s.last,e.ts);
    s.users[e.user]=1;if(e.key_name||e.key)s.keys[e.key_name||e.key.split(' ').pop().slice(0,16)+'…']=1}
   const key=(s,k)=>k=='ts'?s.last:k=='addr'?s.addr:k=='user'?Object.keys(s.users).join():k=='key'?Object.keys(s.keys).join():s[k]??'';
   const all=sshlSorted(Object.values(m),key);
   body=line+`<div class=sshwrap><table class=sshtab><tr>${sshlTh('addr','from')}${sshlTh('accepted','accepted')}${sshlTh('refused','refused')}${sshlTh('user','users')}${sshlTh('key','keys')}<th>first</th>${sshlTh('ts','last')}</tr>
    ${all.slice(0,SSHL.n).map(s=>`<tr class=go onclick="sshlSet('view','events');sshlSet('q','${esc(s.addr)}')" title="its logins"><td class=nowrap>${who(s.addr)}</td><td class=ok>${s.accepted||''}</td><td class="${s.refused?'bad':''}">${s.refused||''}</td>
     <td>${esc(Object.keys(s.users).join(', '))}</td><td>${esc(Object.keys(s.keys).join(', '))}</td><td class=nowrap>${esc(sshTs(s.first))}</td><td class=nowrap>${esc(sshTs(s.last))}</td></tr>`).join('')
     ||'<tr><td colspan=7 class=mut>no logins</td></tr>'}</table></div>${all.length>SSHL.n?`<button class=act onclick="sshlSet('n',${SSHL.n+200})">${all.length-SSHL.n} more</button>`:''}`}
  else{const key=(e,k)=>k=='key'?(e.key_name||e.key||''):k=='addr'?e.addr:(e[k]??'');
   const all=sshlSorted(rows,key);
   body=line+`<div class=sshwrap><table class=sshtab><tr>${sshlTh('ts','when (UTC)')}${sshlTh('result','result')}${sshlTh('user','user')}${sshlTh('addr','from')}${sshlTh('method','how')}${sshlTh('key','key')}</tr>
    ${all.slice(0,SSHL.n).map(e=>`<tr><td class=nowrap>${esc(sshTs(e.ts))}</td><td class=nowrap><span class="tag ${SSHRES[e.result]||''}">${esc(e.result)}</span></td>
     <td>${esc(e.user)}${e.invalid_user||e.result=='invalid'?' <span class="tag bad">no such user</span>':''}</td><td class=nowrap>${who(e.addr)}<span class=mut>:${e.port}</span></td>
     <td>${esc(e.method)}</td><td title="${esc(e.key||'')}">${e.key_name?esc(e.key_name):e.key?`<code>${esc(e.key.split(' ').pop().slice(0,20))}…</code>`:''}${e.head_key?' <span class=tag>head</span>':''}</td></tr>`).join('')
     ||`<tr><td colspan=6 class=mut>no logins${SSHL.q||SSHL.res!='all'?' match':''}</td></tr>`}</table></div>${all.length>SSHL.n?`<button class=act onclick="sshlSet('n',${SSHL.n+200})">${Math.min(200,all.length-SSHL.n)} more (${all.length-SSHL.n} left)</button>`:''}`}}
 return `<div class="m1sec m1wide" id="mx-sshlog"><h3>SSH logins</h3>${ctl}${body}</div>`}
function goMx(n){location.href='/machine?host='+encodeURIComponent(n)}
function rMachines(d){
 if(!d)return '<div class=empty>loading…</div>';
 if(d.error)return `<div class="empty bad">${esc(d.error)}</div>`;
 const F=d.fleet||{};
 const bar=`<div class=mxbar><span><b>${F.up}</b>/${F.machines} up</span>
  <span class="${F.bad?'bad':F.warn?'warn':'ok'}"><b>${F.bad+F.warn}</b> need${F.bad+F.warn==1?'s':''} a look</span>
  <span><b>${F.backups_ok??'—'}</b>/${F.backups_total??'—'} backups on schedule</span>
  <span class="${F.alerts?'warn':''}"><b>${F.alerts}</b> alert${F.alerts==1?'':'s'}</span>
  <span><b>${(F.tps||0).toFixed(0)}</b> tok/s now</span>
  <span><b>${F.repairs_24h??0}</b> self-repairs in 24 h</span><span class=hspacer></span><span class=mut>updated ${esc(agoS(d.ts))}</span></div>`;
 return `<div class=mx>${bar}<div class=mxgrid>${(d.machines||[]).map(mxCard).join('')}</div></div>`}
/* ── /heal: the self-repair loop (heal.py). What it found, each decision and why, every command it ran
   with its output: heal-log.jsonl, newest run first. ── */
const SRF={host:'',check:'',all:false,q:'',open:new Set()};
const SRST={repaired:'ok',failed:'bad',partial:'warn',needs_person:'bad',waiting:'warn',shadow:'warn',would:'info'};
const SRWORD={repaired:'repaired',failed:'failed',partial:'did what it could',needs_person:'needs a person',waiting:'waiting',shadow:'shadow',would:'would repair'};
function srApi(){const q=new URLSearchParams({n:'60'});if(SRF.host)q.set('host',SRF.host);if(SRF.check)q.set('check',SRF.check);
 if(SRF.all)q.set('all','1');if(SRF.q)q.set('q',SRF.q);
 return Promise.all([fetchJSON('/api/v1/heal'),fetchJSON('/api/v1/heal/log?'+q),fetchJSON('/api/v1/changes').catch(()=>({})),
  fetchJSON('/api/v1/self-checkpoints').catch(()=>({}))]).then(([s,l,c,sc])=>{s._log=l.runs||[];s._changes=c.changes||[];s._self=sc;return s})}
/* checkpoints: an ssh and a restic listing per machine, so only on request */
let SRCP=null;
async function srCheckpoints(refresh){SRCP={loading:true};srReload();
 SRCP=await fetchJSON('/api/v1/checkpoints'+(refresh?'?refresh=1':'')).catch(e=>({error:String(e)}));srReload()}
async function srRevert(id,what,data,btn){
 const keep=data?!confirm(`Undo change ${id}?\n\n${what}\n\nOK: also put back the data it kept (what was recorded since is moved aside).\nCancel: choose to keep today's data instead.`):false;
 if(keep&&!confirm(`Undo change ${id}, keeping today's data (the binaries and drop-in go back)?`))return;
 if(!data&&!confirm(`Undo change ${id}?\n\n${what}\n\nWhat ran before it goes back; it is checked, and put back as it is now if it does not work.`))return;
 btn.disabled=true;btn.textContent='undoing…';const r=await act('revertChange',{id,keep_data:keep});
 if(r&&r.error){btn.disabled=false;btn.textContent='undo';alert(r.error)}setTimeout(srReload,4000)}
async function srCheckpointNow(host,btn){if(!confirm(`Take a checkpoint of ${host} now (a system snapshot)?`))return;
 btn.disabled=true;btn.textContent='taking…';const r=await act('takeCheckpoint',{host});if(r&&r.error){btn.disabled=false;alert(r.error)}}
async function srReload(){const p=PANELS.find(x=>x.id=='heal');if(!p)return;const d=await p.api().catch(e=>({error:String(e)}));p._data=d;repaint('heal',d)}
function srSet(k,v){SRF[k]=v;srReload()}
function srToggle(id,open){if(open)SRF.open.add(id);else SRF.open.delete(id)}
async function srUnfreeze(btn){if(!confirm('Let self-repair act again? It froze because several repairs in a row failed.'))return;
 btn.disabled=true;const r=await act('healUnfreeze');if(r&&r.error){btn.disabled=false;alert(r.error)}setTimeout(srReload,2500)}
function srStep(r,s,i){const id=r.run+':'+i,k=s.kind;
 const kc={found:'warn',repair:'info',result:SRST[s.outcome]||'',decide:SRST[s.state]||'',error:'bad',freeze:'bad',cmd:'',change:'',note:'',skip:'warn'}[k]||'';
 const who=[s.host,s.check].filter(Boolean).map(esc).join(' · ');
 const body=k=='cmd'?`<details${SRF.open.has(id)?' open':''} ontoggle="srToggle('${esc(id)}',this.open)"><summary><code>${esc(s.text.length>160?s.text.slice(0,160)+'…':s.text)}</code> <span class="${s.rc===0?'ok':'bad'}">rc ${esc(s.rc)}</span></summary>${s.text.length>160?`<pre>${esc(s.text)}</pre>`:''}<pre>${esc(s.out||'(no output)')}</pre></details>`
  :esc(s.text)+(s.seconds!=null?` <span class=mut>(${esc(s.seconds)} s)</span>`:'');
 return `<tr><td>+${esc(Number(s.t).toFixed(1))}s</td><td class=k><span class="tag ${kc}">${esc(k=='result'?SRWORD[s.outcome]||k:k=='decide'?SRWORD[s.state]||k:k)}</span></td><td class=k>${who}</td><td>${body}</td></tr>`}
function srRun(r){const S=r.summary||{},id=r.run;
 const chips=S.skipped?`<span class="tag warn">${esc(S.skipped)}</span>`:['repaired','failed','partial','needs_person','waiting','shadow'].filter(k=>S[k]).map(k=>`<span class="tag ${SRST[k]}">${S[k]} ${esc(SRWORD[k])}</span>`).join('')||(S.found?`<span class="tag info">${S.found} found</span>`:'<span class="tag ok">all as expected</span>');
 return `<details class=srrun${SRF.open.has(id)?' open':''} ontoggle="srToggle('${esc(id)}',this.open)"><summary><b>${esc(fmtTs(r.ts_epoch))}</b><span class=mut>${esc(agoS(r.ts_epoch))}</span><span>${esc(r.trigger||'')}${r.host?' · '+esc(r.host):''}${r.dry?' · dry run':''}</span><span class=mut>${esc(r.seconds??'?')} s</span>${chips}</summary>
  <table class=srsteps>${(r.steps||[]).map((s,i)=>srStep(r,s,i)).join('')||'<tr><td></td><td colspan=3 class=mut>no steps</td></tr>'}
  <tr><td></td><td colspan=3 class=mut>checks: ${Object.entries(r.checks||{}).map(([k,v])=>esc(k)+' '+esc(v)+'s').join(' · ')||'—'}</td></tr></table></details>`}
const CHST={'in effect':'ok',undone:'mut',rolled_back:'warn',rollback_failed:'bad',refused:'mut','under a newer change':'mut'};
function srChanges(d){
 const rows=(d._changes||[]).slice(0,30).map(r=>{const cp=r.checkpoint||{},data=['prometheus','alertmanager'].includes(r.component);
  const cps=cp.snapshot?`<span title="${esc(cp.taken?'taken for it':'a snapshot from '+Math.round((cp.age||0)/60)+' min before')}">${esc(cp.snapshot)}</span>`:cp.error||cp.skipped?`<span class=mut>${esc(cp.error||cp.skipped)}</span>`:'<span class=mut>—</span>';
  return `<tr><td class="mut nowrap">${esc(agoS(r.ts_epoch||r.ts))}</td><td class=nowrap><b>${esc(r.host)}</b></td><td>${esc(r.what)}${(r.problems||[]).length?`<div class=mut>${esc(r.problems.join('; '))}</div>`:''}</td>
   <td><span class="tag ${CHST[r.state]||(String(r.state).startsWith('undid')?'info':'')}">${esc(r.state)}</span></td><td class=nowrap>${cps}</td>
   <td class=nowrap>${r.revertible?`<button class=silbtn onclick="srRevert('${esc(r.id)}','${esc(String(r.what).replace(/'/g,''))}',${data},this)" title="undo it: what ran before goes back (checked, and put back as it is now if that does not work)">undo</button>`:''}</td></tr>`}).join('');
 return `<div><h4>Changes</h4><p class="mut" style="margin:0 0 4px">What Noctyx changed on each machine, newest first, with the checkpoint it waited for. <b>undo</b> puts back what ran before the newest change in effect (<code>noctyx revert ID</code>).</p>
  ${rows?`<table class=srt><tr><th>when</th><th>machine</th><th>change</th><th>state</th><th>checkpoint</th><th></th></tr>${rows}</table>`:'<div class=mut>none recorded yet</div>'}</div>`}
function srCpSection(d){
 let body;
 if(!SRCP)body=`<button class=act onclick="srCheckpoints(false)">show checkpoints</button> <span class=mut>(asks every machine's repository)</span>`;
 else if(SRCP.loading)body='<div class=mut>asking each machine…</div>';
 else if(SRCP.error)body=`<div class=bad>${esc(SRCP.error)}</div>`;
 else body=Object.entries(SRCP.hosts||{}).map(([h,rows])=>`<div style="margin:6px 0"><b>${esc(h)}</b> <button class=silbtn onclick="srCheckpointNow('${esc(h)}',this)">checkpoint now</button>
   ${Array.isArray(rows)?(rows.length?`<table class=srt>${rows.slice(0,8).map(r=>`<tr><td class=nowrap><code>${esc(r.id)}</code></td><td class="mut nowrap">${esc(fmtTs(r.time))}</td><td>before ${esc((r.reasons||[]).join(', ')||'?')}${(r.changes||[]).length?' · '+r.changes.map(x=>esc(x.id+' '+x.what)).join('; '):''}</td></tr>`).join('')}</table>`:'<span class=mut>none yet</span>'):`<span class=bad>${esc(rows.error)}</span>`}</div>`).join('')
   +`<div class=mut>listed ${esc(agoS(SRCP.ts))} · <a class=lnk onclick="srCheckpoints(true)">ask again</a> · put a path back: Snapshot Browser, or <code>noctyx restore --host H --snapshot ID --path …</code></div>`;
 return `<div><h4>Checkpoints</h4><p class=mut style="margin:0 0 4px">Restic snapshots of a machine kept before Noctyx or apt changed it (a scheduled one from the last hours counts).</p>${body}</div>`}
function srSelf(S){
 const cur=S.current||{},g=cur.git||{},R=S.rollback||{};
 const rows=(S.checkpoints||[]).slice(0,6).map(x=>{const i=x.identity||{},gi=i.git||{};return `<tr><td class="mut nowrap">${esc(fmtTs(x.ts))}</td><td>${esc(i.version||'')} <code>${esc((gi.sha||'').slice(0,12))}</code>${gi.dirty?' <span class=warn>dirty</span>':''}</td><td>${esc(x.note||'')}</td></tr>`}).join('');
 return `<div><h4>Noctyx itself</h4><p class=mut style="margin:0 0 4px">Runs ${esc(cur.version||'?')} ${g.sha?'<code>'+esc(g.sha.slice(0,12))+'</code> on '+esc(g.branch||''):''} (${esc(cur.mode||'?')}). A self-checkpoint is kept when it starts on new code.</p>
  ${rows?`<table class=srt>${rows}</table>`:'<div class=mut>none yet</div>'}
  <div class=mut style="margin-top:4px">${R.target?`Going back to ${esc(R.target)}: <code>sudo noctyx self-rollback --yes</code> on the head (it restarts this console, so it is not a button).`:esc((R.problems||[]).join(' '))}</div></div>`}
function rHeal(d){
 if(!d)return '<div class=empty>loading…</div>';
 if(d.error)return `<div class="empty bad">${esc(d.error)}</div>`;
 const P=d.policy||{},U=P.upgrades||{},fr=d.frozen,pb=P.playbooks||{};
 const bar=`<div class=srbar><span class="tag ${P.auto?'ok':'warn'}">${P.auto?'repairs on':'report only'}</span>
  ${fr?`<span class="tag bad">frozen</span><span class=bad>${esc(fr.why)}</span><button class=act onclick="srUnfreeze(this)">unfreeze</button>`:''}
  <span>${esc(d.actions_1h||0)} of ${esc(P.budget_per_hour)} repairs this hour</span>
  <span>automatic upgrades ${U.auto?'<b>on</b>, '+esc(U.window):'off'}</span>
  <span class=mut>last run ${esc(agoS(d.ts))}${d.skipped?' · '+esc(d.skipped):''}</span><span class=hspacer></span>
  <button class=act onclick="healNow(this);setTimeout(srReload,4000)" title="run noctyx heal now">repair now</button><a class=lnk href="/config#sec-heal">policy →</a></div>`;
 const probs=(d.problems||[]).map(p=>`<tr><td class=nowrap><b>${esc(p.host)}</b></td><td>${esc(p.what)}${p.detail?`<div class=mut>${esc(p.detail)}</div>`:''}${diagNote(p.diagnosis)}</td><td><span class="tag ${SRST[p.state]||(p.needs_person?'bad':'warn')}">${esc(SRWORD[p.state]||(p.needs_person?'needs a person':'next try'))}</span></td><td>${esc(p.why||p.how||'')}</td><td class=mut>${esc(agoS(p.since))}</td></tr>`).join('');
 const reps=(d.repairs||[]).slice(-12).reverse().map(r=>`<tr><td class="mut nowrap">${esc(agoS(r.ts_epoch||r.ts))}</td><td class=nowrap><b>${esc(r.host)}</b></td><td>${esc(r.how)}</td><td class="${r.ok?'ok':r.ok===null?'warn':'bad'}">${r.ok?'✓':r.ok===null?'·':'✗'} ${esc(r.detail||'')}</td></tr>`).join('');
 const pbs=Object.entries(d.playbooks||{}).map(([k,t])=>`<span title="${esc(t)}" class="${pb[k]=='off'?'mut':pb[k]=='shadow'?'warn':''}">${esc(k)}: ${esc(pb[k]||'on')}</span>`).join('');
 const opt=(v,l,cur)=>`<option value="${esc(v)}"${v==cur?' selected':''}>${esc(l)}</option>`;
 const filt=`<div class=srf><select onchange="srSet('host',this.value)">${opt('','every machine',SRF.host)}${(d.hosts||[]).map(h=>opt(h,h,SRF.host)).join('')}</select>
  <select onchange="srSet('check',this.value)">${opt('','every playbook',SRF.check)}${Object.keys(d.playbooks||{}).map(k=>opt(k,k,SRF.check)).join('')}</select>
  <label><input type=checkbox${SRF.all?' checked':''} onchange="srSet('all',this.checked)"> also the runs that found nothing</label>
  <input type=search placeholder="search the log" value="${esc(SRF.q)}" onchange="srSet('q',this.value.trim())"></div>`;
 const log=(d._log||[]).map(srRun).join('')||`<div class=mut>${SRF.all||SRF.host||SRF.check||SRF.q?'no run matches':'no run found anything yet (tick “also the runs that found nothing” to see every run)'}</div>`;
 return `<div class=sr>${bar}
  <div><h4>Open problems</h4>${probs?`<table class=srt><tr><th>machine</th><th>what</th><th>state</th><th>why</th><th>since</th></tr>${probs}</table>`:'<div class=ok>none: everything Noctyx manages is as expected</div>'}</div>
  <div><h4>Repairs, last 24 h</h4>${reps?`<table class=srt>${reps}</table>`:'<div class=mut>none</div>'}</div>
  <div><h4>Playbooks</h4><div class=srpb>${pbs}</div></div>
  ${srChanges(d)}
  ${srCpSection(d)}
  ${srSelf(d._self||{})}
  <div><h4>Log</h4>${filt}<div class=sr style="gap:6px;margin-top:8px">${log}</div></div></div>`}
function rConfig(d){
 if(!d)return '<div class=empty>loading…</div>';
 if(d.error)return `<div class="empty bad">${esc(d.error)}</div>`;
 const P=d.policy||{},R=d.retention||{},HP=P.hosts||{};
 const dbChips=n=>{const D=(d.dumps||{})[n]||{},last={};((D.last||{}).results||[]).forEach(r=>last[r.id]=r);
  if(D.error)return `<span class=bad>scan failed: ${esc(D.error)}</span>`;
  if(!D.discovered)return '<span class=mut>not scanned yet — Apply</span>';
  if(!D.discovered.length)return '<span class=mut>none found</span>';
  const ts=(D.last||{}).ts;
  return D.discovered.map(x=>{const r=last[x.id]||{},nm=x.id.replace(/^[a-z]+:/,'');
   const st=r.ok===true?`<span class=ok>✓ ${r.bytes?esc(fmtBytes(r.bytes)):'saved'}</span>`:r.ok===false?`<span class=bad title="${esc(r.error||'')}">✗ failed</span>`:r.skipped?'<span class=mut>skipped</span>':'<span class=mut>not run</span>';
   return `<label class=dbc title="${esc(x.label)}${r.error?' — '+esc(r.error):''}"><input type=checkbox data-host="${esc(n)}" data-db="${esc(x.id)}"${x.enabled?' checked':''} onchange="cfgDirty()"><b>${esc(nm)}</b> <span class=mut>${esc(x.kind)}</span> ${st}</label>`}).join('')
   +(ts?`<span class="mut dbts">last run ${esc(agoS(ts))}</span>`:'')};
 const rows=(d.hosts||[]).map(n=>{const o=HP[n]||{},e=(d.effective||{})[n]||{},fe={docker_volumes:P.docker_volumes,db_dumps:P.db_dumps};
  return `<tr><td><b>${esc(n)}</b><div class=mut style="font-size:10px">${esc(((d.profiles||{})[n]||[]).join(' · '))}</div></td>
   <td>${triSel('cf-dv-'+n,o.docker_volumes,fe.docker_volumes)}</td>
   <td>${triSel('cf-db-'+n,o.db_dumps,fe.db_dumps)}</td>
   <td class=dbs>${dbChips(n)}</td>
   <td><textarea id="cf-ex-${esc(n)}" rows=2 placeholder="host-only patterns" oninput="cfgDirty()">${esc((o.exclude||[]).join('\n'))}</textarea></td></tr>`}).join('');
 const state=CFGDIRTY?'<span id=cf-state class=warn>unsaved changes</span>':`<span id=cf-state class=mut>${d.stored?'saved in the site config':'defaults (nothing saved yet)'}</span>`;
 return `<div class=cfg>
 <div class=cfgbar>${state}<span class=hspacer></span>
  <button class=act onclick="cfgSave()">save &amp; apply</button>
  <button class=act onclick="cfgRun('policyApply',{},'apply')" title="push the saved policy to every host again and rescan databases">re-apply</button>
  <button class=act onclick="cfgRun('dumpsNow',{},'dump')" title="dump every enabled database now, without a backup">dump databases now</button>
  <button class=lnk onclick="CFGDIRTY=false;tick(PANELS.find(x=>x.id=='config'))">discard / reload</button></div>
 <div class=cfgsec id=sec-backups><h4>All hosts</h4><p class=gnote>What every host's backup covers, unless a host below says otherwise.</p>
  <div class=setrow><label>Docker volumes</label><div><input type=checkbox class=sw id=cf-dv${P.docker_volumes?' checked':''} onchange="cfgDirty()"></div>
   <div class=hint>Back up /var/lib/docker/volumes. Image layers and container filesystems stay excluded: they are rebuilt, not restored.</div></div>
  <div class=setrow><label>Database dumps</label><div><input type=checkbox class=sw id=cf-db${P.db_dumps?' checked':''} onchange="cfgDirty()"></div>
   <div class=hint>Before each <b>system</b> backup, dump every database found (postgres, mariadb/mysql, redis/valkey SAVE, sqlite) into ${(window.NX_CFG||{}).nodeLib||'/var/lib/noctyx'}/dumps, so the snapshot holds a consistent copy.</div></div>
  <div class=setrow><label>Extra excludes</label><textarea id=cf-ex rows=3 oninput="cfgDirty()" placeholder="/root/scratch/**&#10;!/var/cache/apt/archives">${esc((P.exclude||[]).join('\n'))}</textarea>
   <div class=hint>One restic pattern per line, on every host. <code>!pattern</code> re-includes something the baseline excludes.</div></div>
  <div class=setrow><label>Extra SQLite files</label><textarea id=cf-sq rows=2 oninput="cfgDirty()" placeholder="/opt/app/data/app.db">${esc((P.sqlite||[]).join('\n'))}</textarea>
   <div class=hint>Copied safely (online backup API) before each backup. Gitea's and Grafana's are found automatically.</div></div></div>
 <div class=cfgsec><h4>Per host</h4>
  <table class=cfgt><tr><th>host</th><th>docker volumes</th><th>db dumps</th><th>databases <span class=mut>(untick to skip)</span></th><th>extra excludes</th></tr>${rows}</table>
  ${(d.not_covered||[]).length?`<div class=hint>not backed up by Noctyx: ${esc(d.not_covered.join(', '))} (no restic profile; see the site config)</div>`:''}</div>
 <div class=cfgsec id=sec-retention><h4>Retention</h4><p class=gnote>Snapshots kept per host and profile. The monthly prune applies it, gated by a repository check; the weekly run is a dry run you can read in Runs.</p>
  <div class=setrow><label>Daily</label><div><input id=cf-rd type=number min=0 max=999 value="${esc(R.daily??'')}" oninput="cfgDirty()"></div><div class=hint>The newest snapshot of each of this many days.</div></div>
  <div class=setrow><label>Weekly</label><div><input id=cf-rw type=number min=0 max=999 value="${esc(R.weekly??'')}" oninput="cfgDirty()"></div><div class=hint>Then one a week for this many weeks.</div></div>
  <div class=setrow><label>Monthly</label><div><input id=cf-rm type=number min=0 max=999 value="${esc(R.monthly??'')}" oninput="cfgDirty()"></div><div class=hint>Then one a month for this many months. How often backups run: the <a class=lnk href="/#panel=schedule">Schedule panel</a>.</div></div></div>
 ${hfSection(d.hf||{})}
 ${targetsSection(d.targets||{})}
 ${alertSection(d.alerting||{})}
 ${asSection(d.assistant||{})}
 ${srSection(d.heal||{})}
 ${cpSection(d.checkpoints||{})}
 <div class=cfgsec><details><summary>baseline exclusions — always applied (exclusions-common.conf; a profile with its own list in the site's etc dir uses that instead)</summary>
  <div class=cfgcols><pre class=cfgpre>${esc(d.baseline&&d.baseline.common||'')}</pre>${Object.entries((d.baseline||{}).profiles||{}).map(([k,v])=>`<pre class=cfgpre><b>${esc(k)}</b>\n${esc(v)}</pre>`).join('')}</div></details></div>
 <pre id=cf-out class="cfgpre${CFGJOB?'':' hide'}">${esc(CFGJOB?CFGJOB.text:'')}</pre>
 </div>`}
function hfSection(F){
 const S=F.status||{},T=S.totals||{},HS=S.hosts||{},ex=new Set(F.exclude_hosts||[]);
 const hosts=(F.sources||[]).map(n=>{const h=HS[n]||{};
  const last=h.error?`<span class=bad title="${esc(h.error)}">failed</span>`:h.ts?`${esc(agoS(h.ts))} · +${h.new_blobs||0} files (${esc(fmtBytes(h.new_bytes||0))})${h.bad_hash?` · <span class=bad>${h.bad_hash} bad hash</span>`:''}`:'<span class=mut>never</span>';
  return `<label class=dbc title="harvest model files from ${esc(n)}"><input type=checkbox data-hfhost="${esc(n)}"${ex.has(n)?'':' checked'} onchange="cfgDirty()"><b>${esc(n)}</b> <span class=mut>${last}</span></label>`}).join('');
 return `<div class=cfgsec id=sec-mirror><h4>Hugging Face model mirror</h4><p class=gnote>One copy of every model on the cluster, on ${(window.NX_CFG||{}).nas||'nas'}. Hosts download from it instead of huggingface.co; anything it lacks passes through, and when huggingface.co is unreachable the stored copy is served.</p>
  <div class=setrow><label>Status</label><div class=hfline>server <b class="${F.service=='active'?'ok':'bad'}">${esc(F.service||'?')}</b> · store <b>${T.repos??0}</b> repos, <b>${T.blobs??0}</b> files, <b>${esc(fmtBytes(T.bytes||0))}</b>${S.ts?` · harvested ${esc(agoS(S.ts))}`:''}${F.harvesting?' · <span class=warn>harvest running</span>':''}
   <button class=act onclick="cfgRun('hfHarvest',{},'harvest')" title="copy new model files from the hosts now (yields to backups)">harvest now</button></div></div>
  <div class=hftbox><div class=hftbar><input class=hftq type=search placeholder="filter: repo, owner…" value="${esc(HFT.q)}" oninput="HFT.q=this.value;hftPaint()"><span id=hft-bar>${hftBar()}</span></div>
   <div id=hft>${hftBody()}</div></div>
  <div class=setrow><label>Endpoint</label><div><code>HF_ENDPOINT=${esc(F.endpoint||'')}</code></div><div class=hint>Per process: <code>export HF_ENDPOINT=…</code>; a container: <code>-e HF_ENDPOINT=…</code>. Recipe deploys use it by themselves.</div></div>
  <div class=setrow><label>Nightly harvest</label><div><input type=checkbox class=sw id=cf-hf${F.harvest?' checked':''} onchange="cfgDirty()"></div><div class=hint>After the backups: copies only files the store lacks, checks each against its hash, pauses in the backup window.</div></div>
  <div class=setrow><label>Write-through cache</label><div><input type=checkbox class=sw id=cf-hfwt${F.write_through?' checked':''} onchange="cfgDirty()"></div><div class=hint>On a cache miss, stream weights to the node and atomically cache verified blobs into the mirror store immediately (restart <code>noctyx-hf-mirror</code> after changing).</div></div>
  <div class=setrow><label>Harvest from</label><div class=dbs>${hosts}</div><div class=hint>Untick a host to leave its caches out.</div></div>
  <div class=setrow><label>Bandwidth limit</label><div><input id=cf-hfbw type=number min=0 max=10000 value="${esc(F.bwlimit_mb??0)}" oninput="cfgDirty()"> MB/s</div><div class=hint>Per copy; 0 = no limit.</div></div></div>`}
/* What the mirror holds, as a tree (cache/tree.py): kind › owner › repo › revision › folders › files, sizes at
   every level. Its own state and its own box: a toggle repaints only #hft, never the settings form around it. */
const HFT={d:null,busy:false,err:null,open:new Set(),q:'',sort:'size',only:'all',asked:false};
async function hftLoad(refresh){HFT.busy=HFT.asked=true;hftPaint();
 try{const r=await fetch('/api/v1/hf-mirror/tree'+(refresh?'?refresh=1':''),{headers:{Accept:'application/json'}});
  const d=await r.json().catch(()=>({error:'HTTP '+r.status}));if(r.ok&&!d.error){HFT.d=d;HFT.err=null}else HFT.err=d.error||('HTTP '+r.status)}
 catch(e){HFT.err=String(e)}
 HFT.busy=false;hftPaint()}
function hftPaint(){const b=$('hft'),t=$('hft-bar');if(b)b.innerHTML=hftBody();if(t)t.innerHTML=hftBar()}
function hftTog(k,also){if(HFT.open.has(k)){HFT.open.delete(k)}else{HFT.open.add(k);if(also)HFT.open.add(also)}hftPaint()}
function hftAll(on){HFT.open.clear();if(on&&HFT.d)for(const r of HFT.d.repos){HFT.open.add('k:'+r.kind);HFT.open.add('o:'+r.kind+'/'+r.owner)}hftPaint()}
function hftRepos(){const q=HFT.q.trim().toLowerCase();
 return ((HFT.d||{}).repos||[]).filter(r=>(HFT.only=='all'||r.state!='complete')&&(!q||r.id.toLowerCase().includes(q)))}
function hftBar(){const d=HFT.d;if(!d)return '';const bad=d.repos.filter(r=>r.state!='complete').length,b=(k,v,l)=>`<button class="${HFT[k]==v?'on':''}" onclick="hftSet('${k}','${v}')">${l}</button>`;
 return `<span class=seg>${b('sort','size','largest')}${b('sort','name','name')}${b('sort','updated','newest')}</span>
  <span class=seg>${b('only','all','all '+d.repos.length)}${b('only','gaps',`incomplete ${bad}`)}</span>
  <button class=lnk onclick="hftAll(1)">open all</button><button class=lnk onclick="hftAll(0)">close all</button>
  <span class=mut>${esc(fmtBytes(d.bytes))} in ${d.blobs} files · scanned ${esc(agoS(d.ts))} (${d.seconds}s)</span>
  <button class=act onclick="hftLoad(1)" ${HFT.busy?'disabled':''}>${HFT.busy?'scanning…':'rescan'}</button>`}
function hftSet(k,v){HFT[k]=v;hftPaint()}
function hftCmp(a,b){return HFT.sort=='name'?a.name.localeCompare(b.name,undefined,{numeric:true,sensitivity:'base'})
 :HFT.sort=='updated'?(b.updated||0)-(a.updated||0):(b.bytes-a.bytes)}
function hftBody(){
 if(!HFT.asked)setTimeout(()=>hftLoad(),0);
 if(HFT.err)return `<div class="empty bad">${esc(HFT.err)}</div>`;
 if(!HFT.d)return `<div class=empty>reading the store…</div>`;
 const repos=hftRepos(),filt=!!HFT.q.trim()||HFT.only!='all',out=[];
 if(!repos.length)return `<div class=empty>${filt?'no repo matches':'the store is empty'}</div>`;
 const kinds={};for(const r of repos){const k=kinds[r.kind]||(kinds[r.kind]={name:r.kind,bytes:0,updated:0,owners:{}});
  const o=k.owners[r.owner]||(k.owners[r.owner]={name:r.owner||'(no owner)',key:r.owner,bytes:0,updated:0,repos:[]});
  o.repos.push(r);o.bytes+=r.bytes;k.bytes+=r.bytes;o.updated=Math.max(o.updated,r.updated);k.updated=Math.max(k.updated,r.updated)}
 const bar=(v,max)=>`<span class=hftbarv><i style="width:${max?Math.max(1,Math.round(100*v/max)):0}%"></i></span>`;
 const row=(depth,key,label,bytes,max,meta,cls,also)=>out.push(`<tr class="${cls||''}"><td style="padding-left:${6+depth*18}px" class=hftn>${key?`<span class="hftc go" onclick="hftTog('${esc(key)}'${also?`,'${esc(also)}'`:''})">${HFT.open.has(key)||(filt&&/^[ko]:/.test(key))?'▾':'▸'}</span>`:'<span class=hftc></span>'}${label}</td><td class=num>${bytes==null?'':esc(fmtBytes(bytes))}</td><td>${max!=null?bar(bytes||0,max):''}</td><td class=hftm>${meta||''}</td></tr>`);
 const isOpen=k=>HFT.open.has(k)||(filt&&/^[ko]:/.test(k));
 const KIND={models:'Models',datasets:'Datasets',spaces:'Spaces'};
 const ks=Object.values(kinds).sort(hftCmp),kmax=Math.max(...ks.map(k=>k.bytes));
 for(const k of ks){const kk='k:'+k.name,os=Object.values(k.owners).sort(hftCmp),n=os.reduce((a,o)=>a+o.repos.length,0);
  row(0,kk,`<b>${esc(KIND[k.name]||k.name)}</b>`,k.bytes,kmax,`${n} repo${n==1?'':'s'} · ${os.length} owner${os.length==1?'':'s'}`,'hftk');
  if(!isOpen(kk))continue;const omax=Math.max(...os.map(o=>o.bytes));
  for(const o of os){const ok='o:'+k.name+'/'+o.key;
   row(1,ok,`<b>${esc(o.name)}</b>`,o.bytes,omax,`${o.repos.length} repo${o.repos.length==1?'':'s'}${o.repos.some(r=>r.state!='complete')?' · <span class=warn>has gaps</span>':''}`);
   if(!isOpen(ok))continue;const rs=o.repos.slice().sort(hftCmp),rmax=Math.max(...rs.map(r=>r.bytes));
   for(const r of rs){const rk='r:'+r.dir,one=r.snapshots.length==1?'s:'+r.dir+'/'+r.snapshots[0].commit:null;
    const gap=r.snapshots.reduce((a,s)=>a+s.missing.length,0),exp=r.snapshots.reduce((a,s)=>a+s.expected,0);
    const hosts=Object.keys(r.seen_on||{}).length?Object.entries(r.seen_on).map(([h,t])=>`<span class=tag title="seen there ${esc(agoS(t))}">${esc(h)}</span>`).join(' '):(r.from||[]).map(h=>`<span class=tag title="its refs came from there">${esc(h)}</span>`).join(' ');
    const refs=Object.entries(r.refs||{}).map(([n,c])=>`${esc(n)} → <code>${esc(c.slice(0,7))}</code>`).join(', ');
    row(2,rk,`<a class=lnk href="https://huggingface.co/${r.kind=='models'?'':r.kind+'/'}${esc(r.id)}" target=_blank rel=noopener title="on huggingface.co">${esc(r.name)}</a>`,r.bytes,rmax,
     `${r.state=='complete'?'<span class=ok>✓ complete</span>':r.state=='empty'?'<span class=mut>no files</span>':`<span class=warn title="the mirror passes these through to huggingface.co">missing ${gap} of ${exp} weight files</span>`}
      · ${r.blobs} file${r.blobs==1?'':'s'}${r.snapshots.length>1?` · ${r.snapshots.length} revisions`:''}${refs?' · '+refs:''}${hosts?' · '+hosts:''}${r.updated?` · <span class=mut>${esc(agoS(r.updated))}</span>`:''}`,'hftr',one);
    if(!isOpen(rk))continue;
    for(const s of r.snapshots){const sk='s:'+r.dir+'/'+s.commit;
     row(3,sk,`<code>${esc(s.commit.slice(0,12))}</code>${s.refs.length?' '+s.refs.map(x=>`<span class=tag>${esc(x)}</span>`).join(' '):''}`,s.bytes,null,
      `${s.files.length} file${s.files.length==1?'':'s'}${s.missing.length?` · <span class=warn>${s.missing.length} not on the NAS</span>`:''}`);
     if(!isOpen(sk))continue;hftFiles(out,row,sk,s,4)}}}}
 return `<div class=hftwrap><table class=hfttab><tr><th>name</th><th class=num>size</th><th></th><th></th></tr>${out.join('')}</table></div>`}
function hftFiles(out,row,sk,s,depth){
 // a revision's files as folders: each folder's size is what it holds; files the revision should have but the
 // store lacks are listed in their folder, marked
 const root={dirs:{},files:[],bytes:0};
 const put=(rel,size,missing)=>{const parts=rel.split('/');let n=root;n.bytes+=size||0;
  for(const p of parts.slice(0,-1)){n=n.dirs[p]||(n.dirs[p]={dirs:{},files:[],bytes:0});n.bytes+=size||0}
  n.files.push({name:parts[parts.length-1],size,missing})};
 for(const [rel,size] of s.files)put(rel,size,false);for(const rel of s.missing)put(rel,null,true);
 const nat=(a,b)=>a.localeCompare(b,undefined,{numeric:true});
 const walk=(n,path,d)=>{
  for(const [name,c] of Object.entries(n.dirs).sort((a,b)=>nat(a[0],b[0]))){const k='f:'+sk.slice(2)+'/'+path+name;
   const cnt=(function all(x){return x.files.length+Object.values(x.dirs).reduce((t,y)=>t+all(y),0)})(c);
   row(d,k,`📁 ${esc(name)}/`,c.bytes,null,`<span class=mut>${cnt} file${cnt==1?'':'s'}${Object.keys(c.dirs).length?` · ${Object.keys(c.dirs).length} folder${Object.keys(c.dirs).length==1?'':'s'}`:''}</span>`);
   if(HFT.open.has(k))walk(c,path+name+'/',d+1)}
  for(const f of n.files.sort((a,b)=>nat(a.name,b.name)))row(d,null,f.missing?`<span class=warn>${esc(f.name)}</span>`:esc(f.name),f.missing?null:f.size,null,
   f.missing?'<span class=warn title="the mirror fetches it from huggingface.co when asked; offline, it is not there">not on the NAS</span>':'',f.missing?'hftmiss':'')};
 walk(root,'',depth)}
function targetsSection(TG){
 const items=TG.items||{};
 const rows=Object.entries(items).map(([n,t])=>`<tr><td><b>${esc(n)}</b></td><td><code>${esc(t.kind)}</code></td><td><code>${esc(t.uri||'')}</code></td>
  <td>${t.ready?'<span class=ok>✓ ready</span>':`<span class=warn title="${esc(t.detail||'')}">⚠ ${esc(t.detail||'not ready')}</span>`}</td>
  <td class=nowrap><button class=act onclick="cfgRun('copyTarget',{target:'${esc(n)}',dry_run:true},'dry-run copy to ${esc(n)}')">dry run</button>
   <button class=act onclick="cfgRun('copyTarget',{target:'${esc(n)}'},'copy to ${esc(n)}')"${t.ready?'':' disabled'}>copy now</button></td></tr>`).join('');
 return `<div class=cfgsec id=sec-targets><h4>Secondary backup targets (3-2-1 copy)</h4>
  <p class=gnote>Copy primary restic snapshots off-site or to a second disk (<code>local</code>, <code>rest</code>, <code>s3</code>, <code>b2</code>, <code>sftp</code>). Secrets stay in root-only <code>&lt;etc&gt;/target-creds/&lt;name&gt;.env</code> files, never in <code>site.json</code>.</p>
  ${rows?`<table class=cfgt><tr><th>target</th><th>kind</th><th>location</th><th>status</th><th></th></tr>${rows}</table>`
   :'<div class=hint>No secondary targets configured yet. Add a <code>"backup_targets"</code> block to <code>site.json</code> and run <code>noctyx copy --target NAME</code>.</div>'}</div>`}
let AL_DRAFT=null;
function alState(){if(!AL_DRAFT){const d=(PANELS.find(x=>x.id=='config')||{})._data||{};const c=(d.alerting&&d.alerting.config)||{};
  AL_DRAFT={repeat_interval:c.repeat_interval||'12h',channels:JSON.parse(JSON.stringify(c.channels||{}))}}return AL_DRAFT}
function alertSection(AL){
 const cfg=AL.config||{},chans=AL_DRAFT?AL_DRAFT.channels:(cfg.channels||{}),rep=AL_DRAFT?AL_DRAFT.repeat_interval:(cfg.repeat_interval||'12h');
 const kinds=AL.channels||['ntfy','pushover','slack','telegram','webhook','email'];
 const sevs=AL.severities||['all','warning','critical'];
 const dest=c=>c.url||c.smarthost||c.webhook_url_file||c.user_key_file||(c.chat_id!=null?'chat '+c.chat_id:'');
 const rows=Object.entries(chans).map(([n,c])=>`<tr><td><b>${esc(n)}</b></td><td><code>${esc(c.kind)}</code></td><td>${esc(c.min_severity||'warning')}</td>
  <td><code>${esc(dest(c))}</code></td><td><button class=lnk onclick="alDel('${esc(n)}')">remove</button></td></tr>`).join('');
 return `<div class=cfgsec id=sec-alerting><h4>External alert routing</h4>
  <p class=gnote>Deliver firing alerts to phones, chat, or email alongside the Noctyx console webhook (Linux systemd or macOS launchd Alertmanager). Secrets are referenced by root-only file paths.</p>
  <div class=setrow><label>Repeat interval</label><div><input id=al-rep value="${esc(rep)}" style="width:90px" placeholder="12h" oninput="alState().repeat_interval=this.value.trim();cfgDirty()"></div><div class=hint>How often still-firing alerts re-notify (e.g. <code>4h</code>, <code>12h</code>, <code>1d</code>).</div></div>
  <div class=setrow><label>Channels</label><div>${rows?`<table class=cfgt><tr><th>name</th><th>kind</th><th>severity</th><th>destination / secret file</th><th></th></tr>${rows}</table>`:'<div class=mut>console webhook only — add a phone or chat channel below</div>'}</div></div>
  <div class=setrow><label>Add channel</label><div class=hfline style="flex-wrap:wrap;gap:6px">
   <input id=al-name placeholder="name (e.g. phone)" style="width:130px">
   <select id=al-kind onchange="alKindUI()">${kinds.map(k=>`<option value="${esc(k)}">${esc(k)}</option>`).join('')}</select>
   <select id=al-sev>${sevs.map(s=>`<option value="${esc(s)}"${s=='warning'?' selected':''}>${esc(s)}</option>`).join('')}</select>
   <input id=al-a placeholder="https://ntfy.sh/topic" style="width:220px">
   <input id=al-b placeholder="optional token file (/etc/noctyx/alert-keys/...)" style="width:240px">
   <input id=al-c placeholder="" style="width:160px;display:none">
   <button class=act onclick="alAdd()">add</button></div></div>
  <div class=setrow><label></label><div class=hfline>
   <button class=act onclick="alSave()">save alerting</button>
   <button class=act onclick="cfgRun('applyAlerting',{},'apply alerting')" title="render alertmanager.yml and reload Alertmanager">apply &amp; reload</button>
   <button class=act onclick="cfgRun('testAlerting',{},'test alert')" title="send a synthetic NoctyxTestAlert through Alertmanager">send test alert</button></div></div></div>`}
function alKindUI(){const k=($('al-kind')||{}).value||'ntfy',a=$('al-a'),b=$('al-b'),c=$('al-c');if(!a||!b||!c)return;
 c.style.display='none';
 if(k=='ntfy'||k=='webhook'){a.placeholder=k=='ntfy'?'https://ntfy.sh/topic':'https://hooks.example/alert';b.placeholder='optional token file (/etc/noctyx/alert-keys/...)';}
 else if(k=='pushover'){a.placeholder='user_key_file (/etc/noctyx/alert-keys/po.user)';b.placeholder='token_file (/etc/noctyx/alert-keys/po.tok)';}
 else if(k=='slack'){a.placeholder='webhook_url_file (/etc/noctyx/alert-keys/slack.url)';b.placeholder='optional #channel';}
 else if(k=='telegram'){a.placeholder='bot_token_file (/etc/noctyx/alert-keys/tg.tok)';b.placeholder='chat_id (integer)';}
 else if(k=='email'){a.placeholder='smarthost (smtp.example.com:587)';b.placeholder='from@example.com';c.placeholder='to@example.com';c.style.display='';}}
function alAdd(){const st=alState(),n=($('al-name').value||'').trim().toLowerCase(),k=$('al-kind').value,sev=$('al-sev').value,
 a=($('al-a').value||'').trim(),b=($('al-b').value||'').trim(),c=($('al-c').value||'').trim();
 if(!n||!a){alert('enter a channel name and destination/file');return}
 const ent={kind:k,enabled:true,min_severity:sev,send_resolved:true};
 if(k=='ntfy'||k=='webhook'){ent.url=a;if(b)ent.token_file=b;}
 else if(k=='pushover'){ent.user_key_file=a;ent.token_file=b;}
 else if(k=='slack'){ent.webhook_url_file=a;if(b)ent.channel=b;}
 else if(k=='telegram'){ent.bot_token_file=a;ent.chat_id=Number(b);}
 else if(k=='email'){ent.smarthost=a;ent.from=b;ent.to=c;}
 st.channels[n]=ent;cfgDirty();const p=PANELS.find(x=>x.id=='config');if(p&&p._data)repaint('config',p._data)}
function alDel(name){const st=alState();delete st.channels[name];cfgDirty();const p=PANELS.find(x=>x.id=='config');if(p&&p._data)repaint('config',p._data)}
function alSave(){const st=alState();st.repeat_interval=($('al-rep')||{}).value?.trim()||st.repeat_interval||'12h';
 cfgRun('setAlerting',{alerting:st},'save alerting').then(()=>{AL_DRAFT=null})}
function asSection(A){
 const eps=(A.endpoints||[]).map((e,i)=>`<option value="${i}">${esc(e.host)} · ${esc(e.model||'?')} · ${esc(e.engine||'')} — ${esc(e.url)}</option>`).join('');
 return `<div class=cfgsec id=sec-assistant><h4>Assistant LLM</h4><p class=gnote>The model Noctyx asks to explain findings, triage alerts and write the morning brief. Text only: it never runs anything.</p>
  ${asHealth(A)}
  <div class=setrow><label>Use the assistant</label><div><input type=checkbox class=sw id=as-on${A.enabled===true?' checked':''} onchange="cfgDirty()"></div></div>
  <div class=setrow><label>Endpoint</label><div><input id=as-url class=asin value="${esc(A.url||'')}" placeholder="http://host:port/v1" oninput="cfgDirty()">
    ${eps?`<select id=as-pick onchange="asPick(this.value)" style="margin-top:6px"><option value="">or pick one Noctyx sees serving…</option>${eps}</select>`:''}</div>
   <div class=hint>An OpenAI-compatible base URL, ending in /v1.</div></div>
  <div class=setrow><label>Model</label><div><input id=as-model class=asin value="${esc(A.model||'')}" placeholder="(as served)" oninput="cfgDirty()" list=as-models><datalist id=as-models></datalist></div><div class=hint>The served model id; empty = the endpoint's first. <b>Test</b> lists what it serves.</div></div>
  <div class=setrow><label>Timeout</label><div><input id=as-to type=number min=5 max=600 value="${esc(A.timeout_seconds??60)}" oninput="cfgDirty()"> s</div></div>
  <div class=setrow><label>Fallback</label><div><input type=checkbox class=sw id=as-fb${A.fallback!==false?' checked':''} onchange="cfgDirty()"></div><div class=hint>When this one does not answer, ask any other model Noctyx sees serving.</div></div>
  <div class=setrow><label>API key</label><div class=hfline><b class="${A.has_key?'ok':'mut'}">${A.has_key?'set':'none'}</b>
   <input id=as-key type=password autocomplete=off placeholder="new key">
   <button class=act onclick="asKey(false)">set key</button>${A.has_key?'<button class=lnk onclick="asKey(true)">clear key</button>':''}</div><div class=hint>Stored root-only on the head node; never shown or logged.</div></div>
  <div class=setrow><label>Ask: site notes</label><div><textarea id=as-notes class=asin rows=4 maxlength=4000 placeholder="e.g. gpu-03 is a test box: its alerts can wait. Backups to the NAS pause on Sundays." oninput="cfgDirty()">${esc(A.ask_notes||'')}</textarea></div>
   <div class=hint>Added to the end of Ask Noctyx's system prompt, under “Notes from this site's admins”. At most 4,000 characters.</div></div>
  <div class=setrow><label>Ask: system prompt</label><div><textarea id=as-prompt class=asin rows=8 maxlength=16000 placeholder="(empty: the built-in prompt, below)" oninput="cfgDirty()" >${esc(A.ask_prompt||'')}</textarea>
    <div class=hfline style="margin-top:6px"><button class=lnk onclick="asBuiltin()" title="put the built-in prompt above, to edit">start from the built-in</button><button class=lnk onclick="$('as-prompt').value='';cfgDirty()">use the built-in</button></div>
    <details style="margin-top:6px"><summary class=mut>the built-in prompt</summary><pre class=asbuiltin>${esc(A.ask_builtin||'')}</pre></details></div>
   <div class=hint>Your own replaces the built-in one. Noctyx fills in ${(A.ask_fields||[]).map(f=>'<code>{'+esc(f)+'}</code>').join(' ')}; write a literal brace as <code>{{</code> or <code>}}</code>. It changes only what Ask is told: its tools stay read-only and your role's. A new conversation uses it.</div></div>
  <div class=setrow><label></label><div class=hfline><button class=act onclick="asTest()" title="one short request with the values above, before saving">test</button>
   <button class=act onclick="asSave()">save assistant</button> <span id=as-res class=mut></span></div></div></div>`}
function srSection(H){
 const P=H.policy||{},U=P.upgrades||{},pb=P.playbooks||{};
 const rows=Object.entries(H.playbooks||{}).map(([k,x])=>`<tr><td><b>${esc(k)}</b></td><td><select id="sr-pb-${esc(k)}" onchange="cfgDirty()">${['on','shadow','off'].map(m=>`<option${(pb[k]||x.default)==m?' selected':''}>${m}</option>`).join('')}</select></td><td class=mut>${esc(x.does)}</td></tr>`).join('');
 const slots=(H.slots||[]).map(n=>`<label class=dbc><input type=checkbox data-srslot="${esc(n)}"${(P.restart_slots||[]).includes(n)?' checked':''} onchange="cfgDirty()"><b>${esc(n)}</b></label>`).join('')||'<span class=mut>no recipe slots</span>';
 const comps=(H.upgradable||[]).map(n=>`<label class=dbc><input type=checkbox data-srcomp="${esc(n)}"${(U.components||[]).includes(n)?' checked':''} onchange="cfgDirty()"><b>${esc(n)}</b></label>`).join('');
 return `<div class=cfgsec id=sec-heal><h4>Self-repair</h4><p class=gnote>What <code>noctyx heal</code> may repair on its own: every 5 minutes, and at once when an alert it has a playbook for fires. Each repair is checked and bounded; the <a class=lnk href="/heal">Self-repair page</a> shows every run, decision and command.</p>
  <div class=setrow><label>Repair on its own</label><div><input type=checkbox class=sw id=sr-auto${P.auto!==false?' checked':''} onchange="cfgDirty()"></div><div class=hint>Off: it only reports what it would do.</div></div>
  <div class=setrow><label>Repairs per hour</label><div><input id=sr-budget type=number min=1 max=100 value="${esc(P.budget_per_hour??10)}" oninput="cfgDirty()"></div><div class=hint>Across the fleet. More wait for the next hour.</div></div>
  <div class=setrow><label>Freeze after</label><div><input id=sr-freeze type=number min=1 max=20 value="${esc(P.freeze_after??3)}" oninput="cfgDirty()"> failed repairs in a row</div><div class=hint>Then no repair runs until someone unfreezes it (the Self-repair page).</div></div>
  <div class=setrow><label>Disk full at</label><div><input id=sr-disk type=number min=50 max=99 value="${esc(P.disk_pct??92)}" oninput="cfgDirty()"> %</div><div class=hint>A root filesystem over this: the journal is vacuumed to 1 GiB and apt's package cache cleared. Docker is never pruned (a prune removes stopped containers someone means to start again); what is still full goes to a person with its largest directories.</div></div>
  <div class=setrow><label>Playbooks</label><div class=srscroll><table class=cfgt><tr><th>playbook</th><th>mode</th><th>what it does</th></tr>${rows}</table></div><div class=hint><b>shadow</b>: logs what it would do and leaves it to a person; <b>off</b>: not even checked.</div></div>
  <div class=setrow><label>Units it may restart</label><div><textarea id=sr-units rows=3 oninput="cfgDirty()" placeholder="nginx.service&#10;myapp-*.service">${esc((P.restart_units||[]).join('\n'))}</textarea></div><div class=hint>One unit name or pattern per line. A failed unit not listed is left to a person (with its last log lines). Never restarted, whatever is listed: ${(H.never_units||[]).map(x=>'<code>'+esc(x)+'</code>').join(' ')}.</div></div>
  <div class=setrow><label>Model servers it may restart</label><div class=dbs>${slots}</div><div class=hint>A ticked slot that stops answering for 15 minutes is started again with <code>noctyx recipe-start</code>, but only when Noctyx has seen that slot's own unit serve in the last 7 days. Stop a slot through Noctyx (Recipes, or <code>noctyx recipe-stop</code>) and it stays stopped.</div></div>
  <div class=setrow><label>Automatic upgrades</label><div><input type=checkbox class=sw id=sr-up${U.auto?' checked':''} onchange="cfgDirty()"></div><div class=hint><b>Off by default.</b> On: a component older than Noctyx's pin is upgraded inside the window, one machine per run, nodes before the head, as a change that checks itself and rolls back. A release that fails anywhere is not tried again on its own.</div></div>
  <div class=setrow><label>Upgrade window</label><div><input id=sr-win value="${esc(U.window||'03:00-05:00')}" placeholder="03:00-05:00" oninput="cfgDirty()"></div><div class=hint>HH:MM-HH:MM, the head's local time.</div></div>
  <div class=setrow><label>Upgrade</label><div class=dbs>${comps}</div></div>
  <div class=setrow><label>Between machines</label><div><input id=sr-soak type=number min=0 max=10080 value="${esc(U.soak_minutes??60)}" oninput="cfgDirty()"> min</div><div class=hint>After one machine's upgrade, the next waits this long.</div></div>
  <div class=setrow><label>Major versions</label><div><input type=checkbox class=sw id=sr-major${U.major?' checked':''} onchange="cfgDirty()"></div><div class=hint>Off: a pin in a new major version (Prometheus 2 → 3) waits for a person.</div></div>
  <div class=setrow><label></label><div class=hfline><button class=act onclick="srSave()">save self-repair</button></div></div></div>`}
function cpSection(K){
 const P=K.policy||{};
 const hist=(K.history||[]).slice(0,15).map(h=>`<tr><td class="mut nowrap">${esc(fmtTs(h.ts))}</td><td>${h.why.startsWith('before')?esc(h.why.replace(/-/g,' ')):`before a <b>${esc(h.why)}</b> change`}</td><td class=nowrap><button class=lnk onclick="cpDiff('${esc(h.name)}')">what restoring changes</button> <button class=silbtn onclick="cpRestore('${esc(h.name)}')">restore</button></td></tr>`).join('');
 return `<div class=cfgsec id=sec-checkpoints><h4>Checkpoints and history</h4><p class=gnote>A checkpoint is a restic snapshot of a machine kept before something changes it, to go back to (<a class=lnk href="/heal">Self-repair › Changes</a> undoes a change).</p>
  <div class=setrow><label>Before Noctyx changes</label><div><input type=checkbox class=sw id=cp-ch${P.before_changes!==false?' checked':''} onchange="cfgDirty()"></div><div class=hint>An upgrade or undo waits for a checkpoint of its machine.</div></div>
  <div class=setrow><label>Before apt</label><div><input type=checkbox class=sw id=cp-apt${P.before_apt!==false?' checked':''} onchange="cfgDirty()"></div><div class=hint>A hook in each node's apt takes one before packages change (a failed one never stops apt), and marks when apt finished.</div></div>
  <div class=setrow><label>A snapshot counts for</label><div><input id=cp-age type=number min=0 max=168 value="${esc(P.max_age_hours??6)}" oninput="cfgDirty()"> h</div><div class=hint>A system snapshot this recent is the checkpoint as it is; else a new one is taken (an incremental: minutes).</div></div>
  <div class=setrow><label>Wait at most</label><div><input id=cp-to type=number min=1 max=240 value="${esc(P.timeout_minutes??45)}" oninput="cfgDirty()"> min, apt <input id=cp-ato type=number min=1 max=120 value="${esc(P.apt_timeout_minutes??20)}" oninput="cfgDirty()"> min</div></div>
  <div class=setrow><label>Required</label><div><input type=checkbox class=sw id=cp-req${P.require?' checked':''} onchange="cfgDirty()"></div><div class=hint>On: a change whose checkpoint cannot be made does not run. Off: it runs and says why there is none.</div></div>
  <div class=setrow><label></label><div class=hfline><button class=act onclick="cpSave()">save checkpoints</button></div></div>
  <div class=setrow><label>Site config history</label><div>${hist?`<table class=srt>${hist}</table>`:'<span class=mut>a copy is kept before each rewrite; none yet</span>'}<pre id=cp-diff class="cfgpre hide"></pre></div><div class=hint>The last 50 copies. Restoring keeps the current file first.</div></div></div>`}
function cpSave(){cfgRun('setCheckpoints',{checkpoints:{before_changes:$('cp-ch').checked,before_apt:$('cp-apt').checked,max_age_hours:Number($('cp-age').value),
 timeout_minutes:Number($('cp-to').value),apt_timeout_minutes:Number($('cp-ato').value),require:$('cp-req').checked}},'save checkpoints')}
async function cpDiff(name){const o=$('cp-diff');o.classList.remove('hide');o.textContent='…';
 const j=await fetchJSON('/api/v1/config-history?diff='+encodeURIComponent(name)).catch(e=>({error:String(e)}));o.textContent=j.error||j.diff||'(the same as now)'}
function cpRestore(name){if(!confirm(`Put the site config back as it was (${name})? The current one is kept first.`))return;cfgRun('configRestore',{name},'restore the site config')}
function srVals(){const H=(PANELS.find(x=>x.id=='config')._data||{}).heal||{},pb={};
 Object.keys(H.playbooks||{}).forEach(k=>{const e=$('sr-pb-'+k);if(e)pb[k]=e.value});
 const n=id=>Number($(id).value);
 return {auto:$('sr-auto').checked,budget_per_hour:n('sr-budget'),freeze_after:n('sr-freeze'),disk_pct:n('sr-disk'),playbooks:pb,
  restart_units:lines($('sr-units').value),restart_slots:[...document.querySelectorAll('input[data-srslot]')].filter(c=>c.checked).map(c=>c.dataset.srslot),
  upgrades:{auto:$('sr-up').checked,window:$('sr-win').value.trim(),components:[...document.querySelectorAll('input[data-srcomp]')].filter(c=>c.checked).map(c=>c.dataset.srcomp),
   soak_minutes:n('sr-soak'),major:$('sr-major').checked}}}
function srSave(){const v=srVals();
 if(v.upgrades.auto&&!confirm('Turn on automatic upgrades? Noctyx then upgrades '+(v.upgrades.components.join(', ')||'nothing')+' on its own inside '+v.upgrades.window+'.'))return;
 cfgRun('setHeal',{heal:v},'save self-repair')}
function asPick(i){const A=(PANELS.find(x=>x.id=='config')._data||{}).assistant||{},e=(A.endpoints||[])[i];if(!e)return;
 $('as-url').value=e.url;$('as-model').value=e.model||'';cfgDirty()}
function asVals(){return {enabled:$('as-on').checked,url:$('as-url').value.trim(),model:$('as-model').value.trim(),
 timeout_seconds:Number($('as-to').value)||60,fallback:$('as-fb').checked,ask_notes:$('as-notes').value,ask_prompt:$('as-prompt').value}}
/* the assistant's diagnosis of a problem self-repair leaves to a person: a likely cause and a next step, advice only */
function diagNote(d){return d&&d.text?`<div class=srnote title="written by ${esc(d.model||'the assistant')} from this machine's logs, Noctyx's changes and the audit trail; nothing in it is run"><b>likely cause</b> <span class=mut>(${esc(d.model||'assistant')}, ${esc(agoS(d.ts))})</span><br>${esc(d.text).replace(/\n/g,'<br>')}</div>`:''}
/* who is on a slot's GPU: the processes using it, and who holds its lock or waits for it */
function gpuUse(u){if(!u)return '';if(u.error)return `<div class=mut style="font-size:11px">GPU: could not read (${esc(u.error)})</div>`;
 const pr=p=>`${esc((p.cmd||'').slice(0,60))} <span class=mut>pid ${esc(p.pid)}${p.seconds!=null?' · '+esc(fmtDur(p.seconds)):''}${p.user?' · '+esc(p.user):''}</span>`;
 const g=(u.gpu||[]).map(p=>`<span title="${esc(p.cmd||'')}">${esc(((p.cmd||'').split(' ')[0]||'').split('/').pop())} ${esc((p.mib/1024).toFixed(1))} GiB</span>`).join(' · ')||'idle';
 const l=u.holder?`<span class=warn>lock held</span> by ${pr(u.holder)}`:u.lock=='free'?'lock free':'no lock file yet';
 const w=(u.waiting||[]).length?`<div class=warn style="font-size:11px">${u.waiting.length} waiting for the lock: ${u.waiting.map(pr).join('; ')}</div>`:'';
 return `<div class=mut style="font-size:11px" title="${esc(u.path||'')}">GPU ${g} · ${l}</div>${w}`}
/* a slot's answer check: do its model's replies to the fixed prompts still match the recorded ones */
function ansLine(n,a){if(!a||!a.state)return '';
 if(a.state=='changed'||a.state=='wrong')return `<div class=drift title="${esc(a.detail||'')}">⚠ answers ${esc(a.state)} ${esc(agoS(a.ts))}: ${esc((a.prompts||[]).length)} fixed prompt${(a.prompts||[]).length==1?'':'s'} <span class=mut>· <code>noctyx slot-check --slot ${esc(n)}</code></span></div>`;
 if(a.state=='ok'||a.state=='recorded')return `<div class=mut style="font-size:11px" title="a few fixed prompts, asked greedily, compared with the replies recorded for this deployment">answers ${a.state=='ok'?'<span class=ok>as recorded</span>':'recorded'} · checked ${esc(agoS(a.ts))}</div>`;
 return `<div class=mut style="font-size:11px">answers not checked: ${esc(a.state)} ${esc(a.detail||'')}</div>`}
/* which endpoint answered the assistant's last request, and how many of its endpoints answer now */
function asHealth(A){const h=A.health||{};if(!A.enabled||(!h.asked&&h.up==null))return '';
 const now=h.up==null?'':h.up===0?`<b class=bad>no endpoint answers now</b>`:h.preferred===false?`<b class=warn>its own endpoint does not answer</b>: ${esc(h.up)} fallback${h.up==1?'':'s'} do`:`<b class=ok>${esc(h.up)} of ${esc(h.total)} endpoints answer</b>`;
 const last=!h.asked?'':h.answered?`last answer ${esc(agoS(h.asked))} from <b>${esc(h.model||'?')}</b> (${esc(h.answered)})${h.fallback?' <span class=warn>a fallback</span>':''}`:`<span class=bad>last request ${esc(agoS(h.asked))} got no answer</span>: ${esc(h.error||'')}`;
 return `<div class=setrow><label>Now</label><div>${[now,last].filter(Boolean).join(' · ')}</div></div>`}
function asBuiltin(){const A=(PANELS.find(x=>x.id=='config')._data||{}).assistant||{};$('as-prompt').value=A.ask_builtin||'';cfgDirty()}
/* downloads need an operator. Signed in (or open access): the link just works. Token mode: a browser
   link cannot carry the bearer token, so ask for a 60 s signed ticket and follow the link with it. */
function dlGo(ev,a){
 if(!ME||ME.mode!='token')return true;
 ev.preventDefault();const tok=localStorage.getItem(LS_TOK)||'';
 fetch('/api/v1/download-ticket',{method:'POST',headers:{'Content-Type':'application/json','Authorization':'Bearer '+tok},
  body:JSON.stringify({host:a.dataset.h,snapshot:a.dataset.s,path:a.dataset.p,dir:!!a.dataset.d})})
  .then(r=>r.json().then(j=>{if(r.ok&&j.t)location.href=a.href+'&t='+encodeURIComponent(j.t);else alert('download refused: '+(j.error||r.status)+(tok?'':' (set the token first)'))}));
 return false}
async function asPost(path,body){const tok=localStorage.getItem(LS_TOK)||'';
 const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json','Authorization':'Bearer '+tok},body:JSON.stringify(body)});
 return r.json().catch(()=>({error:'bad response'}))}
async function asTest(){const v=asVals(),o=$('as-res');o.className='mut';o.textContent='testing…';
 const j=await asPost('/api/v1/assistant-test',v).catch(e=>({error:String(e)}));
 const dl=$('as-models');if(dl&&j.models)dl.innerHTML=j.models.map(m=>`<option value="${esc(m)}">`).join('');
 o.className=j.ok?'ok':'bad';
 o.textContent=j.ok?`✓ ${j.model} answered in ${j.ms} ms: “${j.text}”${j.models?' · serves '+j.models.join(', '):''}`:`✗ ${j.error||'no answer'}${j.models?' · serves '+j.models.join(', '):''}`}
function asSave(){const v=asVals();if(!v.url&&!v.fallback){alert('set an endpoint, or allow the fallback');return}
 cfgRun('setAssistant',{assistant:v},'save assistant')}
async function asKey(clear){const k=($('as-key')||{}).value||'';if(!clear&&k.trim().length<8){alert('the key is shorter than 8 characters');return}
 if(clear&&!confirm('Remove the assistant API key?'))return;
 const j=await asPost('/api/v1/assistant-key',{key:clear?null:k.trim()}).catch(e=>({error:String(e)}));
 $('as-key').value='';const o=$('as-res');o.className=j.ok?'ok':'bad';o.textContent=j.ok?(clear?'key removed':'key saved'):'✗ '+(j.error||'failed');
 const p=PANELS.find(x=>x.id=='config');const data=await p.api().catch(()=>null);if(data){p._data=data;if(!CFGDIRTY)repaint('config',data)}}
function cfgCollect(){
 const d=PANELS.find(x=>x.id=='config')._data||{},P=d.policy||{},HP=P.hosts||{};
 const tri=v=>v=='on'?true:v=='off'?false:undefined;
 const pol={docker_volumes:$('cf-dv').checked,db_dumps:$('cf-db').checked,exclude:lines($('cf-ex').value),
  sqlite:lines($('cf-sq').value),skip_dumps:P.skip_dumps||[],hosts:{}};
 for(const n of d.hosts||[]){const o={},old=HP[n]||{};
  const dv=tri($(`cf-dv-${n}`).dataset.v),db=tri($(`cf-db-${n}`).dataset.v);if(dv!==undefined)o.docker_volumes=dv;if(db!==undefined)o.db_dumps=db;
  const ex=lines($(`cf-ex-${n}`).value);if(ex.length)o.exclude=ex;
  const seen=new Set(),skip=[];document.querySelectorAll(`input[data-host="${CSS.escape(n)}"][data-db]`).forEach(c=>{seen.add(c.dataset.db);if(!c.checked)skip.push(c.dataset.db)});
  (old.skip_dumps||[]).forEach(x=>{if(!seen.has(x))skip.push(x)});   /* keep skips for databases not running right now */
  if(skip.length)o.skip_dumps=skip;if((old.sqlite||[]).length)o.sqlite=old.sqlite;
  if(Object.keys(o).length)pol.hosts[n]=o}
 const num=id=>{const v=$(id).value.trim();return v===''?null:Number(v)};
 const ret={daily:num('cf-rd'),weekly:num('cf-rw'),monthly:num('cf-rm')};
 const F=d.hf||{},bw=Number(($('cf-hfbw')||{}).value||0);
 const hf=$('cf-hf')?{harvest:$('cf-hf').checked,write_through:!!($('cf-hfwt')&&$('cf-hfwt').checked),bwlimit_mb:Number.isInteger(bw)?bw:0,
  exclude_hosts:[...document.querySelectorAll('input[data-hfhost]')].filter(c=>!c.checked).map(c=>c.dataset.hfhost)}:undefined;
 return {policy:pol,retention:Object.values(ret).every(v=>Number.isInteger(v))?ret:null,hf_mirror:hf}}
async function cfgSave(){
 const b=cfgCollect();
 if(b.retention===null){alert('retention: daily, weekly and monthly must be whole numbers');return}
 const old=(PANELS.find(x=>x.id=='config')._data||{}).retention||{};
 if(['daily','weekly','monthly'].some(k=>b.retention[k]<(old[k]||0))&&!confirm('Retention goes DOWN: the next prune deletes snapshots that are kept today. Save?'))return;
 await cfgRun('setPolicy',b,'save')}
async function cfgRun(action,params,what){
 const out=$('cf-out');CFGJOB={text:what+': starting…'};if(out){out.classList.remove('hide');out.textContent=CFGJOB.text}
 const tok=localStorage.getItem(LS_TOK)||'';
 let r;try{r=await fetch('/api/v1/action',{method:'POST',headers:{'Content-Type':'application/json','Authorization':'Bearer '+tok},body:JSON.stringify({action,params})})}
 catch(e){CFGJOB.text=what+': '+e;if(out)out.textContent=CFGJOB.text;return}
 const j=await r.json().catch(()=>({error:'bad response'}));
 if(!r.ok||!j.id){CFGJOB.text=`${what} refused: ${j.error||r.status}`;if(out){out.textContent=CFGJOB.text;out.classList.add('bad')}return}
 if(out)out.classList.remove('bad');
 for(;;){await new Promise(z=>setTimeout(z,1200));
  let s;try{s=await fetchJSON('/api/v1/job/'+j.id)}catch(e){continue}
  CFGJOB.text=`${what}${s.done?(s.rc===0?' — done':' — FAILED (rc='+s.rc+')'):' — running…'}\n${(s.output||'').trim()}`;
  const o=$('cf-out');if(o){o.textContent=CFGJOB.text;o.classList.toggle('bad',s.done&&s.rc!==0)}
  if(s.done){if(s.rc===0&&(action=='setPolicy'||action=='setAssistant'||action=='setAlerting'||action=='setHeal'||action=='setCheckpoints'||action=='configRestore'))CFGDIRTY=false;
   const p=PANELS.find(x=>x.id=='config');const data=await p.api().catch(()=>null);if(data){p._data=data;if(!CFGDIRTY)repaint('config',data)}
   return}}}
/* ── recipes (/recipes): each GPU slot serves one recipe; deploy builds the next one while the
   current keeps serving, then switches under the slot's GPU lock and switches back on failure. ── */
let RCJOB=null;const RSEL={};
const rcLabel=r=>!r?'nothing':r.adopted?r.name:`${r.recipe}@${(r.sha||'').slice(0,12)}`;
function rRecipes(d){
 if(!d)return '<div class=empty>loading…</div>';
 if(d.error)return `<div class="empty bad">${esc(d.error)}</div>`;
 const cards=Object.entries(d.slots||{}).map(([n,s])=>{const L=s.live||{},c=s.current,pv=s.previous,sel=RSEL[n]||{};
  const opts=(s.recipes||[]).map(r=>`<option${(sel.recipe||'')==r?' selected':''}>${esc(r)}</option>`).join('');
  const prep=Object.entries(s.prepared||{}).map(([r,x])=>`${esc(r)}@${esc((x.sha||'').slice(0,12))} <span class=mut>${esc(agoS(x.prepared_ts))}</span>`).join(' · ');
  const hist=(s.history||[]).map(h=>`<tr><td>${esc(agoS(h.ts))}</td><td>${esc(h.event)}</td><td>${esc(h.to||'')}</td><td class="${h.rc===0?'ok':'bad'}">${h.rc===0?'ok':'rc '+esc(h.rc)}${h.rolled_back?' · rolled back':''}${h.down_s!=null?' · down '+esc(h.down_s)+'s':''}</td></tr>`).join('');
  return `<div class=rslot>
  <div class=rh><span class="dot ${L.ok?'ok':'bad'}"></span><b>${esc(n)}</b><span class=mut>${esc(s.head)}${s.worker?' + '+esc(s.worker):''} · :${esc(s.port)}</span><span class=hspacer></span>${s.busy?`<span class="tag warn" title="${esc(s.busy)}">busy</span>`:''}</div>
  <div>deployment <b>${esc(rcLabel(c))}</b>${c&&c.deployed_ts?` <span class=mut>${esc(agoS(c.deployed_ts))}</span>`:''}${c&&c.adopted?' <span class=mut>(adopted: from before Noctyx; '+esc(c.unit)+')</span>':''}${c&&c.state=='stopped'?' <span class=warn>stopped</span>':''}</div>
  <div class=rm>${L.ok?`<span class=ok>serving</span> ${esc((L.models||[]).join(', '))}${L.connections!=null?` <span class=mut>· ${esc(L.connections)} connected</span>`:''}`:`<span class=bad>not serving</span> <span class=mut>${esc(L.error||L.status||'')}</span>`}</div>
  ${s.drift?`<div class=drift>⚠ ${esc(s.drift)}${L.ok?` <span class=mut>· swapped by hand? <code>noctyx recipe-adopt --slot ${esc(n)} --unit UNIT</code> records it</span>`:''}</div>`:s.unrecorded?`<div class=mut style="font-size:11px">no record of which models this serves, so a swap goes unnoticed: <code>noctyx recipe-adopt --slot ${esc(n)}</code> records what answers now</div>`:''}
  ${gpuUse(s.gpu_use)}
  ${ansLine(n,s.answers)}
  ${s.deploy===false?`<div class=mut style="font-size:11px">watch-only: Noctyx reports this slot and never switches it</div>`:''}
  ${prep?`<div class=mut style="font-size:11px">prepared: ${prep}</div>`:''}
  <div class=ract>
   <select id="rs-r-${esc(n)}" onchange="RSEL['${esc(n)}']={...(RSEL['${esc(n)}']||{}),recipe:this.value}">${opts}</select>
   <input id="rs-f-${esc(n)}" placeholder="ref (catalog's)" value="${esc(sel.ref||'')}" oninput="RSEL['${esc(n)}']={...(RSEL['${esc(n)}']||{}),ref:this.value}" title="branch, tag or commit; empty = the catalog's ref">
   <button class=act onclick="rcDeploy('${esc(n)}',false)" title="build + download while the current one serves, then switch">deploy</button>
   <button class=act onclick="rcDeploy('${esc(n)}',true)" title="check out, build and download only — nothing stops">prepare</button>
  </div>
  <div class=ract>
   <button class=act onclick="rcOp('recipeStop','${esc(n)}')"${L.ok?'':' disabled'}>stop</button>
   <button class=act onclick="rcOp('recipeStart','${esc(n)}')"${L.ok||!c?' disabled':''}>start</button>
   <button class=act onclick="rcOp('recipeRollback','${esc(n)}')"${pv?'':' disabled'} title="${pv?'switch back to '+esc(rcLabel(pv)):'nothing to roll back to'}">roll back${pv?' → '+esc(rcLabel(pv)):''}</button>
  </div>
  ${hist?`<details><summary>history</summary><table>${hist}</table></details>`:''}
 </div>`}).join('');
 const C=d.catalog||{};
 const crow=Object.entries(C).map(([k,v])=>`<tr><td><b>${esc(k)}</b></td><td>${esc(v.kind)}</td><td>${esc(v.ref||'main')}</td><td>${esc((v.slots||[]).join(', '))}</td><td class=mut>${esc(v.repo)}</td><td><button class=lnk onclick="rcCatRemove('${esc(k)}')">remove</button></td></tr>`).join('');
 const slotBoxes=Object.keys(d.slots||{}).map(n=>`<label><input type=checkbox name=rcs value="${esc(n)}"> ${esc(n)}</label>`).join(' ');
 return `<div class=cfg>
 <div class=rslots>${cards}</div>
 <pre id=rc-out class="cfgpre${RCJOB?'':' hide'}${RCJOB&&RCJOB.done&&RCJOB.rc!==0?' bad':''}">${esc(RCJOB?RCJOB.text:'')}</pre>
 ${rcDiscover(d)}
 <div class="cfgsec rcat"><h4>Catalog <span class=mut>— recipe repos and the slots each may run on (site config › recipes)</span></h4>
  <table>${crow}</table>
  <div class=radd><input name=name id=rca-n placeholder="name" size=18><input name=repo id=rca-r placeholder="https://github.com/…/recipe.git">
   <select id=rca-k>${(d.kinds||[]).map(k=>`<option>${esc(k)}</option>`).join('')}</select><input id=rca-f placeholder="ref" value="main" size=10> ${slotBoxes}
   <button class=act onclick="rcCatAdd()">add</button></div>
  <div class=hint><b>spark</b>: a TP-pair recipe run with <code>python3 -m spark up|down</code>; the slot's link settings are written into its <code>cluster.toml</code>. <b>systemd</b>: a one-GPU recipe with <code>setup.sh</code> + <code>serve.sh</code>. Models download through the Hugging Face mirror on ${(window.NX_CFG||{}).nas||'nas'}.</div></div>
 </div>`}
let RDSORT='fastest',RDALL=false;
function rcDiscover(d){
 const D=d.discover||{},have=new Set(Object.values(d.catalog||{}).map(v=>(v.repo||'').toLowerCase().replace(/(\.git)?\/*$/,'')));
 const rank=x=>{const sp=(x.speed||{}).tps;return RDSORT=='newest'?[0,x.pushed_at||'']:RDSORT=='stars'?[x.stars||0,x.pushed_at||'']:[sp?2:x.claimed_tps?1:0,sp||x.claimed_tps||0,x.pushed_at||'']};
 const cmp=(a,b)=>{const A=rank(a),B=rank(b);for(let i=0;i<A.length;i++){if(A[i]<B[i])return 1;if(A[i]>B[i])return -1}return 0};
 const items=(D.items||[]).filter(x=>RDALL||(x.slots||[]).length).sort(cmp);
 const hwl={gb10:'GB10',rtx5090:'RTX 5090',mac:'Mac'};
 const rows=items.map(x=>{const sp=x.speed,inCat=x.in_catalog||have.has(('https://github.com/'+x.repo).toLowerCase());
  const speed=sp&&sp.tps?`<b>${esc(sp.tps)}</b> tok/s <span class="tag ok" title="${esc(sp.source)} · ${esc(sp.hw||'')}">measured</span>${sp.peak?` <span class=mut>peak ${esc(Math.round(sp.peak))} @${esc(sp.streams)}</span>`:''}`
   :x.claimed_tps?`${esc(x.claimed_tps)} tok/s <span class="tag warn" title="the first tok/s figure in its README: unverified, may be an aggregate">claim</span>`:'<span class=mut>—</span>';
  const fit=(x.slots||[]).length?`<span class=ok>→ ${esc(x.slots.join(', '))}</span> <span class=mut>${esc(x.kind)}</span>`:`<span class=mut>${esc(x.why||'')}</span>`;
  const add=inCat?'<span class=mut>in catalog</span>':(x.slots||[]).length?`<button class=lnk onclick="rcAddFound('${esc(x.repo)}')">add</button>`:'';
  return `<tr><td><a class=lnk href="${esc(x.url)}" target=_blank rel=noopener>${esc(x.repo)}</a>${x.fork?' <span class=mut>fork</span>':''}<div class=mut style="font:10.5px var(--sans);white-space:normal;max-width:520px">${esc(x.description||'')}</div></td>
   <td>${esc(hwl[x.hw]||x.hw)}${x.units>1?' ×'+esc(x.units):''}</td><td>${speed}</td><td class=mut>${esc((x.pushed_at||'').slice(0,10))}</td><td class=mut>★${esc(x.stars||0)}</td><td>${fit}</td><td>${add}</td></tr>`}).join('');
 return `<div class="cfgsec rcat"><h4>Discover on GitHub <span class=mut>— public recipes for this cluster's hardware; speed is measured (our harness, tensorfold.dev) where anyone measured it</span></h4>
  <div class=radd>sort <select onchange="RDSORT=this.value;repaint('recipes',PANELS.find(x=>x.id=='recipes')._data)">${['fastest','newest','stars'].map(s=>`<option${s==RDSORT?' selected':''}>${s}</option>`).join('')}</select>
   <label><input type=checkbox${RDALL?' checked':''} onchange="RDALL=this.checked;repaint('recipes',PANELS.find(x=>x.id=='recipes')._data)"> include ones Noctyx cannot deploy here</label>
   <span class=mut>${D.ts?'searched '+esc(agoS(D.ts))+' · '+esc((D.items||[]).length)+' found':'not searched yet'}</span>
   <button class=act onclick="rcRun('recipeDiscover',{},'search GitHub')">search now</button></div>
  ${rows?`<table>${rows}</table>`:'<div class=empty>nothing yet: search now</div>'}
  <div class=hint>“claim” is the first tok/s figure in a README: unverified, often an aggregate over many streams. Adding a recipe changes nothing that runs: deploy it from its slot above (it builds first, while the current one keeps serving).${(D.items||[]).some(x=>x.units==4)?' Recipes for 4 Sparks would need one slot across both pairs.':''}</div></div>`}
function rcAddFound(full){
 const d=PANELS.find(x=>x.id=='recipes')._data||{},x=((d.discover||{}).items||[]).find(i=>i.repo==full);if(!x)return;
 let name=full.split('/')[1].toLowerCase().replace(/[^a-z0-9.-]+/g,'-').replace(/^[^a-z0-9]+/,'').slice(0,48);
 if((d.catalog||{})[name])name=(full.split('/')[0].toLowerCase().replace(/[^a-z0-9-]+/g,'')+'-'+name).slice(0,48);
 name=prompt(`Catalog name for ${full}?\n(${x.kind}, slots ${x.slots.join(', ')}, branch ${x.branch})`+(x.kind=='script'?'\n\nstart.sh recipe: it reads the slot\'s script_env (WORKER, PORT) from .env — check its README for other settings it needs.':''),name);
 if(!name)return;
 rcRun('recipeCatalog',{name:name.trim(),entry:{repo:x.clone||('https://github.com/'+full+'.git'),kind:x.kind,ref:x.branch||'main',slots:x.slots}},'catalog add '+name)}
function rcDeploy(n,prep){
 const d=PANELS.find(x=>x.id=='recipes')._data||{},s=(d.slots||{})[n]||{},L=s.live||{};
 const recipe=($('rs-r-'+n)||{}).value,ref=(($('rs-f-'+n)||{}).value||'').trim();
 if(!recipe)return;
 if(!prep&&!confirm(`Deploy ${recipe}${ref?' @ '+ref:''} to ${n} (${s.head}${s.worker?' + '+s.worker:''})?\n\n`
   +(L.ok?`${(L.models||[]).join(', ')} keeps serving while the new one is checked out, built and downloaded, then STOPS for the switch.`:'Nothing is serving there now.')
   +`\nIf the new one does not come up, ${n} switches back to ${rcLabel(s.current)}.`))return;
 rcRun('recipeDeploy',{slot:n,recipe,...(ref?{ref}:{}),...(prep?{prepare_only:true}:{})},(prep?'prepare ':'deploy ')+recipe+' → '+n)}
function rcOp(action,n){
 const d=PANELS.find(x=>x.id=='recipes')._data||{},s=(d.slots||{})[n]||{},L=s.live||{};
 const what={recipeStop:'stop',recipeStart:'start',recipeRollback:'roll back'}[action];
 const m={recipeStop:`Stop ${n}? ${(L.models||[]).join(', ')||'it'} goes offline until you start or deploy again.`,
  recipeStart:`Start ${rcLabel(s.current)} on ${n}?`,
  recipeRollback:`Roll ${n} back to ${rcLabel(s.previous)}?${L.ok?'\n'+(L.models||[]).join(', ')+' stops for the switch.':''}`}[action];
 if(confirm(m))rcRun(action,{slot:n},what+' '+n)}
function rcCatAdd(){
 const slots=[...document.querySelectorAll('input[name=rcs]:checked')].map(x=>x.value);
 const entry={repo:$('rca-r').value.trim(),kind:$('rca-k').value,ref:$('rca-f').value.trim()||'main',slots};
 rcRun('recipeCatalog',{name:$('rca-n').value.trim(),entry},'catalog add')}
function rcCatRemove(k){if(confirm(`Remove ${k} from the catalog? (Nothing is stopped; refused while it is deployed.)`))rcRun('recipeCatalog',{name:k,remove:true},'catalog remove')}
async function rcRun(action,params,what){
 RCJOB={text:what+': starting…',done:false};const show=()=>{const o=$('rc-out');if(o){o.classList.remove('hide');o.textContent=RCJOB.text;o.classList.toggle('bad',RCJOB.done&&RCJOB.rc!==0)}};show();
 const tok=localStorage.getItem(LS_TOK)||'';
 let r;try{r=await fetch('/api/v1/action',{method:'POST',headers:{'Content-Type':'application/json','Authorization':'Bearer '+tok},body:JSON.stringify({action,params})})}
 catch(e){Object.assign(RCJOB,{text:what+': '+e,done:true,rc:1});show();return}
 const j=await r.json().catch(()=>({error:'bad response'}));
 if(!r.ok||!j.id){Object.assign(RCJOB,{text:`${what} refused: ${j.error||r.status}`,done:true,rc:1});show();return}
 for(;;){await new Promise(z=>setTimeout(z,1500));
  let s;try{s=await fetchJSON('/api/v1/job/'+j.id)}catch(e){continue}
  Object.assign(RCJOB,{done:!!s.done,rc:s.rc,text:`${what}${s.done?(s.rc===0?' — done':' — FAILED (rc='+s.rc+')'):' — running…'}\n${(s.output||'').trim()}`});show();
  if(s.done){const p=PANELS.find(x=>x.id=='recipes');const data=await p.api().catch(()=>null);if(data){p._data=data;repaint('recipes',data)}return}}}
function rBrowse(b){
 if(!b||!b.host)return '<div class=empty>select a snapshot: open a machine → Backups tab → click a snapshot (or “browse” below)</div>';
 const snapOpts=(b.snaps||[]).map(s=>`<option value="${esc(s.short_id)}"${s.short_id==b.snap?' selected':''}>${esc(fmtTs(s.ts))} · ${esc(s.profile)} · ${esc(s.short_id)}</option>`).join('');
 const crumbs=['/'].concat(b.path.split('/').filter(Boolean)).map((seg,i,arr)=>{
  const to=i==0?'/':'/'+arr.slice(1,i+1).join('/');
  return `${i?'<span class=sep>/</span>':''}<button onclick="browseTo('${esc(b.host)}','${esc(b.snap)}','${esc(to)}')">${esc(seg)}</button>`}).join('');
 const head=`<div class=brow><b>${esc(b.host)}</b>
  <select onchange="browseTo('${esc(b.host)}',this.value,'/')">${snapOpts}</select>
  <input id=bq placeholder="find files across all snapshots, e.g. *.conf or */notes.md" value="${esc((b.find&&b.find.q)||'')}" onkeydown="if(event.key=='Enter')browseFind()">
  <button class=act onclick="browseFind()">find</button></div>`;
 if(b.busy)return head+'<div class=empty>loading…</div>';
 if(b.find){const f=b.find;
  if(f.error)return head+`<div class="empty bad">${esc(f.error)}</div>`;
  const rows=(f.matches||[]).map(m=>{const dir=m.path.replace(/\/[^/]*$/,'')||'/';
   return `<tr><td class=mut>${esc(m.snapshot)}</td><td>${esc(m.path)}</td><td>${m.type=='file'?esc(fmtBytes(m.size)):esc(m.type||'')}</td><td class=mut>${esc(fmtTs(m.mtime))}</td>
    <td class=acts><button class=lnk onclick="browseTo('${esc(b.host)}','${esc(m.snapshot)}','${esc(dir)}')">open</button>
    ${m.type=='file'?`<a class=lnk href="/api/v1/download/${encodeURIComponent(b.host)}/${esc(m.snapshot)}?path=${encodeURIComponent(m.path)}" data-h="${esc(b.host)}" data-s="${esc(m.snapshot)}" data-p="${esc(m.path)}" onclick="return dlGo(event,this)">download</a>`:''}</td></tr>`}).join('');
  return head+`<div class=sline>${(f.matches||[]).length} match${(f.matches||[]).length==1?'':'es'} for <b>${esc(f.q)}</b>${f.truncated?' (first 200)':''} · <button class=lnk onclick="BR.find=null;repaint('browse',BR)">back to browsing</button></div>
   <table><tr><th>snap</th><th>path</th><th>size</th><th>modified</th><th></th></tr>${rows||'<tr><td class=mut colspan=5>nothing matched</td></tr>'}</table>`}
 const d=b.data;if(!d)return head+'<div class=empty>no data</div>';
 if(d.error)return head+`<div class="empty bad">${esc(d.error)}</div>`;
 const rows=(d.entries||[]).map(e=>{const full=(b.path.replace(/\/$/,'')||'')+'/'+e.name;
  const dl=`/api/v1/download/${encodeURIComponent(b.host)}/${esc(b.snap)}?path=${encodeURIComponent(full)}${e.type=='dir'?'&dir=1':''}`;
  return `<tr class="${e.type=='dir'?'dir':''}"><td ${e.type=='dir'?`onclick="browseTo('${esc(b.host)}','${esc(b.snap)}','${esc(full)}')"`:''}>${e.type=='dir'?'▸ ':''}${esc(e.name)}${e.type=='symlink'?' <span class=mut>→ '+esc(e.target||'')+'</span>':''}</td>
   <td>${e.type=='file'?esc(fmtBytes(e.size)):'<span class=mut>'+esc(e.type)+'</span>'}</td><td class=mut>${esc(fmtTs(e.mtime))}</td><td class=mut>${esc(e.mode||'')}</td>
   <td class=acts>${e.type!='symlink'?`<a class=lnk href="${dl}" data-h="${esc(b.host)}" data-s="${esc(b.snap)}" data-p="${esc(full)}" data-d="${e.type=='dir'?1:''}" onclick="return dlGo(event,this)">${e.type=='dir'?'tar':'download'}</a>`:''}
    <button class=lnk onclick="browseRestore('${esc(full)}','staging')">→ staging</button>
    <button class=lnk onclick="browseRestore('${esc(full)}','host')">→ host</button></td></tr>`}).join('');
 const up=b.path!='/'?`<tr class=dir><td onclick="browseTo('${esc(b.host)}','${esc(b.snap)}','${esc(b.path.replace(/\/[^/]*\/?$/,'')||'/')}')">▴ ..</td><td colspan=4></td></tr>`:'';
 return head+`<div class=crumb>${crumbs}</div>
  <table><tr><th>name</th><th>size</th><th>modified</th><th>mode</th><th></th></tr>${up}${rows||'<tr><td class=mut colspan=5>empty directory</td></tr>'}</table>
  <div class=mut style="font:10.5px var(--mono);margin-top:6px">download = the file (dirs as .tar) · → staging = restore onto ${(window.NX_CFG||{}).nas||'nas'} under backups/restores/${esc(b.host)}/&lt;time&gt;/ · → host = restore into ${(window.NX_CFG||{}).nodeLib||'/var/lib/noctyx'}/restore/ on the machine. Nothing is ever overwritten from here.</div>`}
/* ── hygiene: per-host housekeeping findings; manual-tier items get a fix button ── */
/* hygiene fix feedback. Every click has a visible life: sending → queued → running → ✓/✗.
   State lives here (key host\0id) and the row is re-rendered from it; when the job ends the
   report is re-fetched and the entry settles against it (item fixed → gone, else ✗ + reason). */
const HY={};let HYSCAN=null;const hyKey=(h,id)=>h+'\0'+id;
function hyRepaint(){const p=PANELS.find(x=>x.id=='hygiene');if(p&&p._data!==undefined)repaint('hygiene',p._data)}
function hyErr(t){try{t=JSON.parse(t).error||t}catch(e){}return /bad token/.test(t)?'rejected: set the UI token in Console first':'rejected: '+t}
async function hySend(host,ids){for(const id of ids)HY[hyKey(host,id)]={st:'sending'};hyRepaint();
 const r=await act('hygieneFix',{host,items:ids});
 for(const id of ids)HY[hyKey(host,id)]=r.error?{st:'failed',msg:hyErr(r.error)}:{st:'queued',job:r.id};hyRepaint()}
function hyFix(btn){const {h,id,what}=btn.dataset;if(confirm(`${what} on ${h}?`))hySend(h,[id])}
function hyFixAll(btn){const jobs=JSON.parse(btn.dataset.jobs);const n=jobs.reduce((a,[,ids])=>a+ids.length,0);
 const fb=Number(btn.dataset.bytes||0);
 if(confirm(`Apply all ${n} space-reclaim fixes (package removal, old kernels, journal vacuum, docker prune) on every host?${fb?`\n\nFrees about ${fmtBytes(fb)}.`:''}`))for(const [h,ids] of jobs)hySend(h,ids)}
async function hyScan(){if(HYSCAN&&!['failed'].includes(HYSCAN.st))return;HYSCAN={st:'sending'};hyRepaint();
 const r=await act('hygiene');HYSCAN=r.error?{st:'failed',msg:hyErr(r.error)}:{st:'queued',job:r.id};hyRepaint()}
function hySync(){let ch=false;const upd=e=>{if(!e||!e.job||e.st=='done'||e.st=='failed')return;const j=JOBS[e.job];if(!j)return;
  const st=j.done?'done':j.running?'running':'queued';if(e.st!=st){e.st=st;ch=true;if(j.done){e.rc=j.rc;e.msg=(j.output||'').trim().split('\n').filter(Boolean).pop()||''}}};
 for(const k in HY)upd(HY[k]);upd(HYSCAN);return ch}
async function hyRefresh(){const p=PANELS.find(x=>x.id=='hygiene');if(!p)return;await tick(p);
 const H=(p._data&&p._data.hosts)||{};
 for(const k in HY){const e=HY[k];if(e.st!='done')continue;const [h,id]=k.split('\0');const it=((H[h]||{}).items||[]).find(i=>i.id==id);
  if(!it||it.fixed)delete HY[k];else{e.st='failed';e.msg=(e.rc===0?'ran, but not fixed':'failed (rc='+e.rc+')')+(e.msg?' · '+e.msg.slice(0,90):'')}}
 if(HYSCAN&&HYSCAN.st=='done')HYSCAN=HYSCAN.rc===0?null:{st:'failed',msg:'scan failed (rc='+HYSCAN.rc+')'+(HYSCAN.msg?' · '+HYSCAN.msg.slice(0,90):'')};
 hyRepaint()}
function hyCell(n,i){const e=HY[hyKey(n,i.id)],what=i.fix||'fix',
 btn=(label)=>`<button class="${label=='retry'?'lnk':'act'}" data-h="${esc(n)}" data-id="${esc(i.id)}" data-what="${esc(what)}: ${esc(i.title)}" onclick="hyFix(this)">${esc(label)}</button>`;
 if(e)switch(e.st){
  case 'sending':return '<span class=qj>…</span> <span class=mut>sending</span>';
  case 'queued':return '<span class=qj>…</span> <span class=mut>queued behind another job</span>';
  case 'running':return `<span class=spin>◌</span> <span class=mut>${esc(what)}…</span>`;
  case 'done':return '<span class=spin>◌</span> <span class=mut>checking…</span>';
  default:return `<span class=badj>✗</span> <span class=warn style="font:10.5px var(--mono);white-space:normal">${esc(e.msg||'failed')}</span> ${btn('retry')}`}
 if(i.fixed)return '<span class=okj>✓ fixed</span>';
 if(i.id=='reboot-needed'&&rbEligible(n))return rbBtn(n);
 if(i.tier=='manual')return btn(what);
 if(i.tier=='auto')return '<span class=mut>auto-fixes at 06:45</span>';
 return '<span class=mut>report only</span>'}
function rHygiene(d){
 if(!d)return '<div class=empty>loading…</div>';
 const H=d.hosts||{},names=Object.keys(H);
 if(!names.length)return `<div class=empty>no report yet — <button class=lnk onclick="hyScan()">run hygiene now</button></div>`;
 let rows='',open=0;
 // what the open space fixes would free (measured by each host's agent the way the fix works; ~ = estimate)
 const freeOf=items=>items.filter(i=>!i.fixed&&i.bytes>0).reduce((a,i)=>a+i.bytes,0);
 const approxOf=items=>items.some(i=>!i.fixed&&i.bytes>0&&i.bytes_approx);
 const frees=i=>i.bytes>0&&!i.fixed?` <span class="tag ok" title="${i.bytes_approx?'an estimate: ':''}what this fix frees on disk">frees ${i.bytes_approx?'~':''}${esc(fmtBytes(i.bytes))}</span>`:'';
 let fleet=0,fleetApprox=false;
 for(const n of names){const items=H[n].items||[];open+=items.filter(i=>!i.fixed).length;
  const hf=freeOf(items),ha=approxOf(items);fleet+=hf;fleetApprox=fleetApprox||ha;
  const hcell=`${esc(n)}${hf?`<div class=mut style="font:10px var(--mono)">${ha?'~':''}${esc(fmtBytes(hf))} reclaimable</div>`:''}`;
  if(H[n].error){rows+=`<tr><td>${esc(n)}</td><td colspan=3 class=bad>${esc(H[n].error)}</td></tr>`;continue}
  if(!items.length){rows+=`<tr class="${hlRow('hygiene',n)}"><td>${esc(n)}</td><td colspan=3 class=mut>clean${FOCUS.panel=='hygiene'&&FOCUS.host==n?' — nothing to fix here; the finding may be older than the last scan':''}</td></tr>`;continue}
  rows+=items.map((i,k)=>`<tr class="${i.fixed?'mut':''}${hlRow('hygiene',n)}"><td>${k?'':hcell}</td><td><span class="tag ${i.severity=='warning'&&!i.fixed?'warn':''}">${esc(i.severity)}</span></td>
   <td><b>${esc(i.title)}</b>${frees(i)}${i.detail?`<div class=mut style="font:10.5px var(--mono);white-space:normal">${esc(i.detail)}</div>`:''}
    ${i.fixed?'':i.explain?`<div class=xpl style="font:11.5px/1.5 var(--sans);color:var(--fg);margin-top:6px;white-space:normal;border-left:2px solid var(--line);padding-left:8px">${esc(i.explain)}<div class=mut style="font:10px var(--mono);margin-top:2px">— ${esc(i.explain_model||'assistant')}</div></div>`
     :`<button class=lnk data-h="${esc(n)}" data-id="${esc(i.id)}" onclick="explainItem(this.dataset.h,this.dataset.id,this)">explain</button>`}</td>
   <td class=acts>${hyCell(n,i)}</td></tr>`).join('')}
 // bulk = space reclaim only; unit restarts/clears (items with a named fix) are always one click each
 const bulk=names.map(n=>[n,(H[n].items||[]).filter(i=>!i.fixed&&i.tier=='manual'&&!i.fix&&!HY[hyKey(n,i.id)]).map(i=>i.id)]).filter(([,ids])=>ids.length);
 const nb=bulk.reduce((a,[,ids])=>a+ids.length,0);
 const nbBytes=bulk.reduce((a,[n,ids])=>a+(H[n].items||[]).filter(i=>ids.includes(i.id)&&i.bytes>0).reduce((s,i)=>s+i.bytes,0),0);
 const scan=!HYSCAN?`<button class=lnk onclick="hyScan()">re-scan</button>`
  :HYSCAN.st=='failed'?`<span class=badj>✗</span> <span class=warn>${esc(HYSCAN.msg)}</span> <button class=lnk onclick="hyScan()">retry</button>`
  :HYSCAN.st=='running'?`<span class=spin>◌</span> scanning every host…`:HYSCAN.st=='done'?`<span class=spin>◌</span> reading report…`:`<span class=qj>…</span> scan ${HYSCAN.st=='sending'?'sending':'queued'}`;
 return `<div class=sline><b>${open}</b> open item${open==1?'':'s'}${fleet?` · <b class=ok>${fleetApprox?'~':''}${esc(fmtBytes(fleet))}</b> can be freed`:''} · report ${esc(agoS(d.ts))} · ${scan}${nb?` · <button class=lnk data-jobs='${esc(JSON.stringify(bulk))}' data-bytes="${nbBytes}" onclick="hyFixAll(this)">fix all ${nb} space items${nbBytes?` (frees ${fleetApprox?'~':''}${esc(fmtBytes(nbBytes))})`:''}</button>`:''}</div>
  <table><tr><th>host</th><th></th><th>finding</th><th></th></tr>${rows}</table>`}
function rBrief(d){
 if(!d)return '<div class=empty>loading…</div>';
 if(!d.text)return `<div class="empty warn">no brief: ${esc(d.error||'assistant unavailable')} · <button class=lnk onclick="refreshBrief()">retry</button></div>`;
 return `<div style="font:12.5px/1.55 var(--sans);white-space:pre-wrap">${esc(d.text)}</div>
  <div class=mut style="font:10.5px var(--mono);margin-top:8px">written ${esc(agoS(d.ts))} by ${esc(d.model||'the assistant')} from live data · <button class=lnk onclick="refreshBrief()">refresh</button>${d.stale_error?' · <span class=warn>refresh failed: '+esc(d.stale_error)+'</span>':''}</div>`}
async function refreshBrief(){const p=PANELS.find(x=>x.id=='brief');const b=$('b-brief');if(b)b.classList.add('stale');
 try{p._data=await fetchJSON('/api/v1/digest?refresh=1',true);repaint('brief',p._data)}catch(e){if(b)b.classList.remove('stale')}}
async function explainItem(host,id,btn){btn.disabled=true;btn.textContent='asking…';
 const tok=localStorage.getItem(LS_TOK)||'';
 try{const r=await fetch('/api/v1/explain',{method:'POST',headers:{'Content-Type':'application/json','Authorization':'Bearer '+tok},body:JSON.stringify({kind:'hygiene',host,id})});
  const d=await r.json();const box=btn.closest('td').querySelector('.xpl')||btn.closest('td').appendChild(Object.assign(document.createElement('div'),{className:'xpl'}));
  box.style.cssText='font:11.5px/1.5 var(--sans);color:var(--fg);margin-top:6px;white-space:normal;border-left:2px solid var(--line);padding-left:8px';
  box.textContent=d.text||('assistant: '+(d.error||'no answer'));
  if(d.model){const m=document.createElement('div');m.className='mut';m.style.cssText='font:10px var(--mono);margin-top:2px';m.textContent='— '+d.model;box.appendChild(m)}
  btn.remove()}catch(e){btn.disabled=false;btn.textContent='explain'}}
/* dismissing one alert: an Alertmanager silence is the mechanism. The server rebuilds the
   matchers from the alert's own stable labels, so two alerts that share an alertname and host
   (agent carries two ComponentUpgradeAvailable, node_exporter and prometheus) stay
   independently dismissible instead of both vanishing on one click. */
async function silenceAlert(fp,mins,btn){
 const was=btn.textContent;btn.disabled=true;btn.textContent='…';
 try{
  const r=await fetch('/api/v1/alert-silence',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({fingerprint:fp,minutes:mins})});
  const d=await r.json().catch(()=>({}));
  if(!r.ok||d.error)throw new Error(d.error||('http '+r.status));
  const p=PANELS.find(x=>x.id=='alerts');if(p){const data=await p.api();p._data=data;repaint('alerts',data)}
 }catch(e){btn.disabled=false;btn.textContent=was;btn.title='silence failed: '+e.message;btn.classList.add('bad')}
}
async function unsilenceAlert(id,btn){
 const was=btn.textContent;btn.disabled=true;btn.textContent='…';
 try{
  const r=await fetch('/api/v1/alert-unsilence',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:id})});
  const d=await r.json().catch(()=>({}));
  if(!r.ok||d.error)throw new Error(d.error||('http '+r.status));
  const p=PANELS.find(x=>x.id=='alerts');if(p){const data=await p.api();p._data=data;repaint('alerts',data)}
 }catch(e){btn.disabled=false;btn.textContent=was;btn.title='unsilence failed: '+e.message;btn.classList.add('bad')}
}
/* a component's buttons (alerts and the Machine Report): Noctyx's own upgrade where it has one, or skip the release */
function compBtns(host,f){if(!f)return '';const x=f.fix||{},h=esc(JSON.stringify(host)),c=esc(JSON.stringify(f.component));let b='';
 if(x.argv)b+=`<button class=silbtn onclick="event.stopPropagation();compUpgrade(${h},${c},${esc(JSON.stringify(x.what||''))},this)" title="${esc(x.what||'')}">upgrade</button>`;
 if(x.change&&f.hosts>1)b+=`<button class=silbtn onclick="event.stopPropagation();compEverywhere(${c},${f.hosts},this)" title="every machine that needs it: one first, watched 5 min, then the rest one at a time; stops at the first failure">upgrade all ${f.hosts}</button>`;
 else if(x.pin)b+=`<button class=silbtn onclick="event.stopPropagation();compPin(${c},${esc(JSON.stringify(x.pin))},this)" title="${esc(x.why||'')}">take ${esc(x.pin)}</button>`;
 else if(x.why)b+=`<span class=mut style="font:10px var(--mono)" title="${esc(x.why)}">no upgrade here ⓘ</span>`;
 if(f.state=='upgrade'&&f.latest)b+=`<button class=silbtn onclick="event.stopPropagation();compSkip(${h},${c},${esc(JSON.stringify(f.latest))},false,this)" title="stay on ${esc(f.installed||'this version')}: no alert for ${esc(f.latest)}; a later release alerts again">skip ${esc(f.latest)}</button>`;
 if(f.state=='skipped')b+=`<button class=silbtn onclick="event.stopPropagation();compSkip(${h},${c},'',true,this)" title="alert on this release again">unskip</button>`;
 const n=f.notes;   /* what the release changes, from its release notes: the assistant's summary, or the notes' first lines */
 if(n&&(n.summary||n.excerpt))b+=`<details class=relnotes onclick="event.stopPropagation()"><summary>what ${esc(f.latest||'it')} changes</summary><div>${esc(n.summary||n.excerpt).replace(/\n/g,'<br>')}</div><div class=mut>${n.summary?'summary by '+esc(n.model||'the assistant')+' of ':''}<a class=lnk href="${esc(n.url||'#')}" target=_blank rel=noopener>the release notes</a>${n.published?' · '+esc(n.published):''}</div></details>`;
 return b}
async function compUpgrade(host,component,what,btn){
 if(!confirm(`${what}?\n\nRuns as a job (Jobs / Audit shows it), then checks ${host}'s versions again: the alert clears when it is current.`))return;
 if(btn){btn.disabled=true;btn.textContent='upgrading…'}
 const r=await act('upgradeComponent',{host,component});if(r&&r.error&&btn){btn.disabled=false;btn.textContent='upgrade';alert(r.error)}}
async function compEverywhere(component,n,btn){
 if(!confirm(`Upgrade ${component} on all ${n} machines that need it?\n\nOne goes first and is watched for 5 minutes, then the rest one at a time. Each is checked and rolled back if it does not work, and the rollout stops at the first failure.`))return;
 if(btn){btn.disabled=true;btn.textContent='upgrading…'}
 const r=await act('upgradeEverywhere',{component});if(r&&r.error&&btn){btn.disabled=false;btn.textContent='upgrade all';alert(r.error)}}
async function healNow(btn){if(btn){btn.disabled=true;btn.textContent='repairing…'}
 const r=await act('healNow');if(r&&r.error&&btn){btn.disabled=false;btn.textContent='repair now';alert(r.error)}}
async function compPin(component,release,btn){
 if(!confirm(`Move this site's pin of ${component} to ${release}?\n\nNoctyx reads the release's SHA-256 sums from its own sums file and keeps them with the pin. Nothing is installed yet: the row then reads "behind" and its upgrade button installs ${release}, checked and rolled back if it does not work.`))return;
 if(btn){btn.disabled=true;btn.textContent='pinning…'}
 const r=await act('pinRelease',{component,release});if(r&&r.error&&btn){btn.disabled=false;btn.textContent='take '+release;alert(r.error)}}
async function compSkip(host,component,release,undo,btn){
 if(!undo&&!confirm(`Skip ${component} ${release} on ${host}?\n\nThe alert resolves and stays quiet until a newer release than ${release} appears.`))return;
 if(btn){btn.disabled=true;btn.textContent=undo?'…':'skipping…'}
 const r=await act('skipRelease',undo?{host,component,undo:true}:{host,component,release});
 if(r&&r.error&&btn){btn.disabled=false;btn.textContent=undo?'unskip':'skip '+release;alert(r.error)}}
function rAlerts(d){
 if(!d)return '<div class=empty>loading…</div>';
 const act=d.active||[],rec=d.recent||[];
 const pill=$('halert');if(pill){const c=act.filter(a=>a.severity=='critical').length;
  pill.textContent=act.length?`${act.length} alert${act.length==1?'':'s'}`:'no alerts';
  pill.className='hpill go '+(c?'bad':act.length?'warn':'ok')}
 const rows=act.map(a=>{const t=alertTarget(a);const fp=a.fingerprint||'';const comp=(a.labels||{}).component||'';
  const sil=fp?`<button class=silbtn onclick="event.stopPropagation();silenceAlert('${fp}',60,this)" title="silence this one alert for 1 hour">1h</button><button class=silbtn onclick="event.stopPropagation();silenceAlert('${fp}',1440,this)" title="silence this one alert for 24 hours">24h</button><button class=silbtn onclick="event.stopPropagation();silenceAlert('${fp}',10080,this)" title="silence this one alert for 7 days">7d</button>`:'';
  return `<tr ${goAttr({host:t.host,panel:t.panel,tab:t.tab})} title="open ${esc(t.what||'')}"><td><span class="tag ${a.severity=='critical'?'bad':'warn'}">${esc(a.severity||'')}</span></td>
  <td class=wrapn>${esc(a.name||'')}${comp?`<div class=mut style="font:10px var(--mono)">${esc(comp)}</div>`:''}</td><td>${esc(a.host||'')}</td><td class=wrap>${esc(a.summary||'')}${a.fix?`<div class=compbtns>${compBtns(a.host,a.fix)}</div>`:''}${a.heal?`<div class=compbtns><button class=silbtn onclick="event.stopPropagation();healNow(this)" title="run noctyx heal now: it tries the safe repairs again">repair now</button></div>`:''}${a.selfrepair?`<div class=srnote>self-repair: <span class="${SRST[a.selfrepair.state]||''}">${esc(SRWORD[a.selfrepair.state]||a.selfrepair.state||'')}</span> — ${esc(a.selfrepair.why||a.selfrepair.how||'')} <a class=lnk href="/heal" onclick="event.stopPropagation()">log →</a></div>${diagNote(a.selfrepair.diagnosis)}`:''}${a.triage?`<div style="font:11.5px/1.5 var(--sans);color:var(--fg);margin-top:4px;white-space:normal;border-left:2px solid var(--line);padding-left:8px">${esc(a.triage)}<span class=mut style="font:10px var(--mono)"> — ${esc(a.triage_model||'assistant')}</span></div>`:''}</td><td class=mut>${esc(agoS(a.since))}</td><td class="golnk wrapn">${t.what?esc(t.what)+' →':''}</td><td class=btncol>${sil}</td></tr>`}).join('');
 const sl=(d.silenced||[]).map(s=>{const m=(s.matchers||[]).map(x=>esc(x.name)+'='+esc(x.value)).join(' · ');
  const until=s.ends?fmtTs(new Date(s.ends).getTime()/1000):'—';
  return `<tr><td class=mut style="font:10.5px var(--mono)">${m}</td><td class=mut>${esc(until)}</td><td class=mut>${esc(s.by||'')}</td>
   <td class=nowrap><button class=silbtn onclick="unsilenceAlert('${esc(s.id)}',this)" title="end this silence now: the alert fires again if it is still true">unsilence</button></td></tr>`}).join('');
 const hist=rec.slice(0,8).map(r=>`<tr><td class="${r.status=='resolved'?'ok':'warn'}">${esc(r.status||'')}</td>
  <td>${esc(r.name||'')}</td><td>${esc(r.host||'')}</td><td class=mut>${esc(fmtTs(r.ts))}</td></tr>`).join('');
 return (d.error?`<div class="empty warn">${esc(d.error)}</div>`:'')+
  (rows?`<table><tr><th>sev</th><th>alert</th><th>host</th><th>what</th><th>since</th><th>act in</th><th>silence</th></tr>${rows}</table>`
       :'<div class=sline><span class="tag ok">quiet</span> nothing firing</div>')+
  (sl?`<h3>Silenced</h3><table><tr><th>matches</th><th>until</th><th>by</th><th></th></tr>${sl}</table>`:'')+
  (hist?`<h3>Recent</h3><table><tr><th>status</th><th>alert</th><th>host</th><th>when</th></tr>${hist}</table>`:'')}
function rRuns(d){
 const rs=(d&&d.runs)||[];
 if(!rs.length)return '<div class=empty>no detached runs yet</div>';
 const cls={running:'warn',ok:'ok',failed:'bad',died:'bad'};
 return `<table><tr><th>started</th><th>job</th><th>state</th><th>took</th><th>last line</th></tr>`+rs.map(r=>
  `<tr class="${r.host?'go':''}${hlRow('runs',r.host)}" title="${esc(r.unit)}${r.host?' — open the machine':''}"${r.host?` onclick="goTo(${esc(JSON.stringify({host:r.host,panel:'detail',tab:'backups'}))})"`:''}><td>${esc(fmtTs(r.started_ts))}</td><td>${esc(r.cmd)}${r.host?' '+esc(r.host):''}</td>
   <td class="${cls[r.state]||''}">${esc(r.state)}${r.rc!=null&&r.rc!==0?' rc='+r.rc:''}</td>
   <td>${r.started_ts?esc(fmtDur((r.ended_ts||Date.now()/1000)-r.started_ts)):''}</td>
   <td class=mut style="max-width:260px;overflow:hidden;text-overflow:ellipsis">${esc(r.last)}</td></tr>`).join('')+'</table>'+
  (rs.some(r=>r.state=='died')?`<div class=mut style="font:10.5px var(--mono);margin-top:6px">died = the unit ended without an exit record (killed or crashed); its log is in ${(window.NX_CFG||{}).jobLogs||'/var/log/noctyx/jobs'}/</div>`:'')}
function rStorage(tiers){
 const g=(tiers&&tiers._growth)||null;
 const gl=!g?'':g.rate_per_day==null?`<div class=mut style="font:10.5px var(--mono);margin-top:6px">growth: ${g.reset?'reset by a clean-up '+esc(ago(g.reset))+'; ':''}collecting (${g.hours||0} h of history; needs 2 h)</div>`
  :`<div class=sline style="margin-top:6px">${(window.NX_CFG||{}).nas||'nas'} ${g.rate_per_day>=0?'growing':'shrinking'} <b>${esc(fmtBytes(Math.abs(g.rate_per_day)))}/day</b> (over ${g.hours>48?Math.round(g.hours/24)+' days':g.hours+' h'}${g.reset?' since a clean-up':''}${g.settling?', still settling':''})${g.days_to_full!=null?` · full in <b class="${g.days_to_full<30?'bad':g.days_to_full<180?'warn':''}">${g.days_to_full>3650?'>10 years':Math.round(g.days_to_full)+' days'}</b> at this rate`:''}</div>`;
 return `<table><tr><th>tier</th><th>free</th><th>used</th><th style=width:30%></th></tr>`+
 (tiers||[]).map(t=>{const tot=t.free_gb+t.used_gb||1;
  const rp=t.repos_gb,cm=rp?Object.values(rp).reduce((a,b)=>a+b,0):null;
  return `<tr><td>${t.mounted?esc(t.tier):'<span class=bad>'+esc(t.tier)+' ↓</span>'}</td><td>${t.free_gb}G</td><td>${t.used_gb}G</td><td>${bar(100*t.used_gb/tot)}</td></tr>`+
   (rp?`<tr><td class=mut>&nbsp; backups</td><td></td><td>${cm.toFixed(0)}G</td><td class="mut wrapn" style="font:10.5px var(--mono)">${Object.entries(rp).map(([k,v])=>esc(k)+' '+v.toFixed(0)+'G').join(' · ')}</td></tr>`+
    `<tr><td class=mut>&nbsp; other data</td><td></td><td>${Math.max(0,t.used_gb-cm).toFixed(0)}G</td><td class="mut wrapn" style="font:10.5px var(--mono)">not Noctyx${t.repos_ts?' · sized '+esc(ago(t.repos_ts)):''}</td></tr>`:'')}).join('')+'</table>'+gl}
function rJobs(rows){
 return `<table><tr><th>when</th><th>host</th><th>event</th><th>rc</th></tr>`+
 (rows||[]).map(a=>`<tr><td>${esc(fmtTs(a.ts))}</td><td>${esc(a.host||'')}</td><td>${esc(a.event||'')}</td><td class="${(a.rc??0)==0?'ok':'bad'}">${a.rc??''}</td></tr>`).join('')+'</table>'}
function rNet(s){
 const mb=(s&&s.mbit)||{},at=(s&&s.at)||{},ks=Object.keys(mb);
 return ks.length?`<table><tr><th>host</th><th>Mbit/s</th><th>measured</th></tr>`+ks.map(k=>
  `<tr><td>${esc(k)}</td><td class="${mb[k]>200?'ok':'warn'}">${mb[k].toFixed(0)}</td><td class=mut>${at[k]?ago(at[k]):'—'}</td></tr>`).join('')+
  `</table><div class=mut style="font:11px var(--mono);margin-top:8px">measured before each backup · “probe all” in Console refreshes every host</div>`
  :'<div class=empty>never probed — press “probe all” in Console</div>'}
function rActions(){
 return `<input class=pw type=password id=token placeholder="UI token (kept in this browser)" value="${esc(localStorage.getItem(LS_TOK)||'')}">
 <div style=height:8px></div>
 <button class=act onclick="act('status')">status</button>
 <button class=act onclick="act('probe')">probe all</button>
 <button class=act onclick="act('snapshots')">snapshots</button>
 <button class=act onclick="act('schedule')">schedule</button>
 <button class=act onclick="act('capacity')">capacity</button>
 <button class=act onclick="act('backup')">backup ALL</button>
 <button class=act onclick="act('verify')">verify ALL</button>
 <button class=act onclick="act('copy')">copy → NAS</button>
 <button class=act onclick="act('prune')">prune (dry)</button>
 <button class=act onclick="act('nasRest')">repo endpoint restart</button>
 <button class=act onclick="act('directory')">regen directory</button>
 <pre id=cout>idle</pre>`}
function wireActions(){const t=$('token');
 if(t&&!t.__wired){t.__wired=1;t.addEventListener('input',()=>localStorage.setItem(LS_TOK,t.value))}}

/* ── reboot: plan → confirm → job. The orchestrator re-checks every blocker; a head-node reboot
   takes this server down, so the page shows a banner, waits for it to answer again, re-adopts jobs. ── */
let RB=null;
function rbEligible(n){const h=((OVR&&OVR.hosts)||[]).find(x=>x.name==n);return !!h&&!h.monitored_only}
function rbBtn(n,cls){return `<button class="${cls||'act'}" data-h="${esc(n)}" onclick="rebootHost(this.dataset.h,this)">reboot…</button>`}
async function rebootHost(n,btn){
 if(btn){btn.disabled=true;btn.textContent='checking…'}
 let p;try{p=await fetchJSON('/api/v1/reboot-plan/'+encodeURIComponent(n))}catch(e){p={allowed:false,blockers:['plan failed: '+e]}}
 if(btn){btn.disabled=false;btn.textContent='reboot…'}
 if(!p.allowed){alert(`Can't reboot ${n} now:\n\n`+(p.blockers||[]).map(b=>' • '+b).join('\n'));return}
 const imp=(p.impact||[]).length?'\n\nGoes down with it:\n'+p.impact.map(x=>' • '+x).join('\n'):'\n\nNothing Noctyx knows of is serving from it.';
 const w=(p.warnings||[]).length?'\n\nNote:\n'+p.warnings.map(x=>' • '+x).join('\n'):'';
 const how=p.head?'This is the head node: it reboots in 20 s and this page reconnects when it is back.'
  :'It reboots in 10 s; Noctyx waits for it, checks its models and rescans Hygiene (progress in Runs).';
 if(!confirm(`Reboot ${n}?${imp}${w}\n\n${how}`))return;
 const r=await act('reboot',{host:n});
 if(r&&r.error){alert('reboot rejected: '+r.error);return}
 if(r&&r.id&&p.head){RB={host:n,job:r.id,t0:Date.now(),st:'scheduled'};rbBanner();rbWatch()}}
function rbBanner(){let b=$('rbban');if(!RB){if(b)b.remove();return}
 if(!b){b=document.createElement('div');b.id='rbban';document.body.appendChild(b)}
 const s=Math.round((Date.now()-RB.t0)/1000),x=` <button class=lnk onclick="RB=null;rbBanner()">dismiss</button>`;
 b.innerHTML={scheduled:`<span class=spin>◌</span> ${esc(RB.host)} reboots in a few seconds…`,
  down:`<span class=spin>◌</span> ${esc(RB.host)} is rebooting — this page reconnects when it is back (${s}s)`,
  back:`<span class=okj>✓</span> ${esc(RB.host)} is back after ${s}s — the reboot job is finishing (models, Hygiene) in Runs`,
  failed:`<span class=badj>✗</span> ${esc(RB.msg||'reboot did not start')}${x}`,
  nodown:`<span class=warn>${esc(RB.host)} has not gone down after ${s}s — check Runs</span>${x}`}[RB.st]}
async function rbWatch(){
 while(RB&&['scheduled','down'].includes(RB.st)){
  await new Promise(r=>setTimeout(r,3000));
  let up=false,j=null;
  try{const c=new AbortController(),t=setTimeout(()=>c.abort(),2500);
   const r=await fetch('/api/v1/job/'+RB.job,{signal:c.signal});clearTimeout(t);
   up=r.ok||r.status==404;if(r.ok)j=await r.json()}catch(e){up=false}
  if(!RB)return;
  if(RB.st=='scheduled'){
   if(j&&j.done&&j.rc!==0){RB.st='failed';RB.msg=`reboot of ${RB.host} refused or failed (rc=${j.rc}): `+((j.output||'').trim().split('\n').pop()||'')}
   else if(!up)RB.st='down';
   else if(Date.now()-RB.t0>120000)RB.st='nodown'}
  else if(up){RB.st='back';rbBanner();adoptRunning();for(const p of PANELS)tick(p);
   setTimeout(()=>{if(RB&&RB.st=='back'){RB=null;rbBanner()}},20000);return}
  rbBanner()}}

/* ── actions + job polling (own loop, own DOM) ── */
async function act(a,params){
 let p=params||{};
 if((a=='backupHost'||a=='verifyHost')&&!p.host){p={host:SEL};if(!p.host){alert('select a machine first');return}}
 if(['backup','verify'].includes(a)&&!confirm(a+' ALL — run now?'))return;
 const tok=localStorage.getItem(LS_TOK)||'';
 const out=$('cout');if(out)out.textContent='triggering…';
 try{
  const r=await fetch('/api/v1/action',{method:'POST',
   headers:{'Content-Type':'application/json','Authorization':'Bearer '+tok},
   body:JSON.stringify({action:a,params:p})});
  if(!r.ok){const t=await r.text();if(out)out.textContent='rejected: '+t;return {error:t}}
  const {id}=await r.json();
  JOBS[id]={id,action:a,params:p,argv:[],output:'',done:false,running:false,started_ts:Date.now()/1000};
  refreshJobDom();
  if(SEL)tick(PANELS.find(x=>x.id=='detail'));
  return {id}
 }catch(e){if(out)out.textContent='error: '+e;return {error:String(e)}}
}

/* ── boot ── */
function tickClock(){$('hclock').textContent=new Date().toISOString().slice(0,16).replace('T',' ')+'Z'}
const H0=parseHash(location.hash);
if(VIEW=='main'&&H0.host){SEL=H0.host;localStorage.setItem(LS_SEL,SEL)}
if(VIEW=='main'&&H0.tab&&DTABS.includes(H0.tab)){DTAB=H0.tab;try{localStorage.setItem(LS_TAB,DTAB)}catch(e){}}
buildPanels();initTip();
if(VIEW=='main')repaint('actions',null);
for(const p of PANELS)if(!p.api&&p.id!='actions')repaint(p.id,p._data);   /* on-demand panels (Snapshot Browser) show their empty state, not “loading…” */
tickClock();setInterval(tickClock,1000);
for(const p of PANELS){tick(p);if(p.int>0)setInterval(()=>tick(p),p.int*1000)}
kioskArm();                                        /* /kiosk rotation pager, once the panels exist */
async function kioskArmAfterSite(){                /* site defaults arrive async: re-arm once they do */
 if(VIEW=='kiosk'){await loadSiteSettings();kioskArm()}}
kioskArmAfterSite();
setInterval(pollAll,2000);
adoptRunning();
if(VIEW=='main'&&H0.panel)setTimeout(()=>goTo(H0),250);      /* a link from /cluster or an alert: land on the panel */
addEventListener('hashchange',()=>goTo(parseHash(location.hash)));
addEventListener('resize',()=>saveLayout());
/* the header drops whole items until it fits (see header.top.f1…f5); re-fit when its text or the window changes */
const FIT_STEPS=5;
function fitHeader(){const h=document.querySelector('header.top');if(!h)return;
 for(let i=1;i<=FIT_STEPS;i++)h.classList.remove('f'+i);
 for(let i=1;i<=FIT_STEPS&&h.scrollWidth>h.clientWidth+1;i++)h.classList.add('f'+i)}
addEventListener('resize',fitHeader);
new MutationObserver(fitHeader).observe(document.querySelector('header.top'),{childList:true,characterData:true,subtree:true});
fitHeader();
