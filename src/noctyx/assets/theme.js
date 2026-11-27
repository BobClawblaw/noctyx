/* ── appearance: themes, modes, custom colours ────────────────────────────────
   The page draws only from the CSS custom properties below (:root), so a theme is a set of values
   for them, written onto <html>. Each preset has a dark and a light face; the mode (dark, light,
   system) picks one. Custom is nine colours; every other shade is mixed from them. Settings are
   this browser's (localStorage), over the site's defaults (site config "console", cached here so
   the first paint is already right). Runs in <head>: no flash of the wrong colours. */
const NXT=(()=>{
 const rgb=h=>{const m=/^#([0-9a-f]{6})$/i.exec(String(h||'').trim());if(!m)return null;const n=parseInt(m[1],16);return[n>>16&255,n>>8&255,n&255]};
 const hex=c=>'#'+c.map(v=>Math.max(0,Math.min(255,Math.round(v))).toString(16).padStart(2,'0')).join('');
 const mix=(a,b,t)=>{const x=rgb(a),y=rgb(b);return x&&y?hex(x.map((v,i)=>v+(y[i]-v)*t)):a};
 const lum=h=>{const c=rgb(h);if(!c)return 0;const l=v=>{v/=255;return v<=.03928?v/12.92:((v+.055)/1.055)**2.4};return .2126*l(c[0])+.7152*l(c[1])+.0722*l(c[2])};
 const BASE=['bg','panel','text','muted','accent','line','ok','warn','bad'];
 const PHL={bg:'#e9eef1',panel:'#f8fafb',bg2:'#f1f5f7',bg3:'#e3eaee',bg0:'#dde5ea',line:'#c3d0d8',lineSoft:'#d5dfe5',text:'#0c1a22',muted:'#4a6170',faint:'#7d919e',ok:'#0f9d58',warn:'#c77700',bad:'#d1293d',info:'#1f5fe0'};
 const PH=(label,blurb,d)=>({label,blurb,style:'phosphor',dark:d,light:{...PHL,accent:mix(d.accent,'#000000',.4),accentDim:mix(d.accent,'#000000',.55),accent2:mix(d.accent2,'#000000',.4)}});
 const KK=['cpu','gpu','temp','mem','net','disk','power','llm','ok','alert'];
 const PRESETS={
  noctyx:{label:'Noctyx',blurb:'Night console — navy and neon cyan',
   dark:{bg:'#070913',panel:'#0d1020',bg2:'#121629',bg3:'#191e36',bg0:'#05060d',line:'#242a47',lineSoft:'#1a1f38',text:'#e2e8f6',muted:'#a9c0cc',faint:'#9db4c2',accent:'#3fe6ff',accentDim:'#1f93b3',accent2:'#c3a6ff',ok:'#2ecc8f',warn:'#f0b429',bad:'#ef5a5a',info:'#58a6ff'},
   light:{bg:'#eef1f8',panel:'#ffffff',bg2:'#f5f7fc',bg3:'#e7ebf5',bg0:'#e3e8f2',line:'#cfd6e6',lineSoft:'#e0e5f0',text:'#121729',muted:'#525d78',faint:'#8790a6',accent:'#0a9cc0',accentDim:'#077a96',accent2:'#8a3fe0',ok:'#1b9460',warn:'#a87a08',bad:'#d13c3c',info:'#2f6fd6'}},
  phosphor:{label:'Phosphor',blurb:'Instrument panel — LED meters, scope traces, LCD numerals',style:'phosphor',
   dark:{k:{cpu:'#3dff8f',gpu:'#3b8cff',temp:'#ffb020',mem:'#c3a6ff',net:'#22d3ee',disk:'#2ee6a6',power:'#ffd60a',llm:'#6ff5d2',ok:'#3dff8f',alert:'#ff4d5e'},bg:'#04070a',panel:'#0a1015',bg2:'#0d151b',bg3:'#132029',bg0:'#020406',line:'#1a2a35',lineSoft:'#12202a',text:'#e4f1f7',muted:'#a9c0cc',faint:'#9db4c2',accent:'#1fd1ff',accentDim:'#118aab',accent2:'#3dff8f',ok:'#3dff8f',warn:'#ffb020',bad:'#ff4d5e',info:'#3b8cff'},
   light:{...PHL,accent:'#0a8fc0',accentDim:'#086f95',accent2:'#0f9d58'}},
  p1green:PH('Phosphor · P1 Green','Green-phosphor CRT: lime, mint and teal on black',
   {bg:'#030806',panel:'#07100c',bg2:'#0a1510',bg3:'#0f1f17',bg0:'#010402',line:'#173525',lineSoft:'#0f2419',text:'#ddf5e5',muted:'#a5d2b2',faint:'#93c4a2',accent:'#33ff77',accentDim:'#1a9944',accent2:'#b6ff4d',ok:'#33ff77',warn:'#e8ff4d',bad:'#ff5c5c',info:'#4dffc3',
    k:{cpu:'#33ff77',gpu:'#4dffc3',temp:'#e8ff4d',mem:'#9dff4d',net:'#22e6a8',disk:'#7dffa8',power:'#d4ff33',llm:'#00ffb3',ok:'#33ff77',alert:'#ff5c5c'}}),
  p3amber:PH('Phosphor · P3 Amber','Amber terminal: gold, orange and honey',
   {bg:'#090704',panel:'#110d08',bg2:'#16110a',bg3:'#1f180e',bg0:'#040302',line:'#3a2b17',lineSoft:'#2a1f11',text:'#f6ead8',muted:'#dcc0a0',faint:'#c4a885',accent:'#ffb000',accentDim:'#a87100',accent2:'#ffd27a',ok:'#ffc94d',warn:'#ff8a1f',bad:'#ff4a3d',info:'#ffd27a',
    k:{cpu:'#ffb000',gpu:'#ffd27a',temp:'#ff8a1f',mem:'#ff9e5e',net:'#ffe08a',disk:'#ffc94d',power:'#fff07a',llm:'#ff7a3d',ok:'#ffc94d',alert:'#ff4a3d'}}),
  nixie:PH('Phosphor · Nixie','Neon tubes behind smoked glass, green indicator lamps',
   {bg:'#080707',panel:'#100e0d',bg2:'#151210',bg3:'#1d1815',bg0:'#040303',line:'#33261f',lineSoft:'#251c17',text:'#f2e8e1',muted:'#d4b8a8',faint:'#bfa090',accent:'#ff6a1a',accentDim:'#b2440a',accent2:'#ffa15c',ok:'#6fe36f',warn:'#ffb347',bad:'#ff3b30',info:'#ff9a5c',
    k:{cpu:'#ff6a1a',gpu:'#ff9a3d',temp:'#ffc04d',mem:'#ff4f6e',net:'#ffb37a',disk:'#ff8c42',power:'#ffd166',llm:'#ff5a36',ok:'#6fe36f',alert:'#ff3b30'}}),
  synthwave:PH('Phosphor · Synthwave','Magenta, violet and cyan on midnight purple',
   {bg:'#0a0812',panel:'#110e1d',bg2:'#161226',bg3:'#1e1832',bg0:'#05040a',line:'#30264c',lineSoft:'#221b38',text:'#efe9fb',muted:'#c6badf',faint:'#ab9fd0',accent:'#ff2fd0',accentDim:'#a3188a',accent2:'#2de2ff',ok:'#2dffb3',warn:'#ffcc33',bad:'#ff3d6e',info:'#2de2ff',
    k:{cpu:'#2dffb3',gpu:'#2de2ff',temp:'#ffcc33',mem:'#a96bff',net:'#5ab0ff',disk:'#39f5d0',power:'#ffe14d',llm:'#ff2fd0',ok:'#2dffb3',alert:'#ff3d6e'}}),
  arctic:PH('Phosphor · Arctic','Ice blue, glacier cyan and frost on cold navy',
   {bg:'#050a12',panel:'#0a1220',bg2:'#0e1828',bg3:'#132134',bg0:'#03060b',line:'#1c2e47',lineSoft:'#142338',text:'#e8f4ff',muted:'#a9c4e0',faint:'#96b2d0',accent:'#7fdcff',accentDim:'#3a95c2',accent2:'#d6f3ff',ok:'#6ff5d2',warn:'#ffd36b',bad:'#ff6b8a',info:'#8fb8ff',
    k:{cpu:'#6ff5d2',gpu:'#8fb8ff',temp:'#ffd36b',mem:'#c3a6ff',net:'#7fdcff',disk:'#a8f0ff',power:'#fff2a8',llm:'#d6f3ff',ok:'#6ff5d2',alert:'#ff6b8a'}}),
  redops:PH('Phosphor · Red Ops','All red, for a dark room: keeps your night vision',
   {bg:'#060202',panel:'#0c0404',bg2:'#110606',bg3:'#190909',bg0:'#030101',line:'#341010',lineSoft:'#240b0b',text:'#f0b8b2',muted:'#d69a96',faint:'#c0807c',accent:'#ff2a2a',accentDim:'#a31616',accent2:'#ff6b6b',ok:'#d94a4a',warn:'#ff8a5c',bad:'#ffd0d0',info:'#ff5c5c',
    k:{cpu:'#ff3b3b',gpu:'#ff6b5c',temp:'#ff8a5c',mem:'#e04870',net:'#ff5c7a',disk:'#ff4d4d',power:'#ff9466',llm:'#ff2a55',ok:'#d94a4a',alert:'#ffd0d0'}}),
  aurora:PH('Phosphor · Aurora','Northern lights: teal, violet and rose over a polar night',
   {bg:'#04090d',panel:'#09131a',bg2:'#0c1820',bg3:'#11202b',bg0:'#020507',line:'#1a3040',lineSoft:'#12232f',text:'#e6f4f2',muted:'#a9d0d4',faint:'#93bcc0',accent:'#5cffc8',accentDim:'#2a9e7c',accent2:'#b18cff',ok:'#5cffc8',warn:'#ffcf6b',bad:'#ff6b81',info:'#6aa8ff',
    k:{cpu:'#5cffc8',gpu:'#6aa8ff',temp:'#ffcf6b',mem:'#b18cff',net:'#4de0ff',disk:'#7af0b0',power:'#ffe58a',llm:'#ff7ad9',ok:'#5cffc8',alert:'#ff6b81'}}),
  outrun:PH('Phosphor · Outrun','Sunset over a neon grid: orange, hot pink and teal',
   {bg:'#0a0718',panel:'#110d24',bg2:'#16112e',bg3:'#1e173c',bg0:'#05030c',line:'#33275a',lineSoft:'#241c44',text:'#f5ecf7',muted:'#d4c3ea',faint:'#b8a9d0',accent:'#ff8a3d',accentDim:'#b0571f',accent2:'#ff3dac',ok:'#2ef2d8',warn:'#ffd23f',bad:'#ff4f5e',info:'#5b8cff',
    k:{cpu:'#2ef2d8',gpu:'#5b8cff',temp:'#ffb347',mem:'#c77dff',net:'#53d8ff',disk:'#3ee6a0',power:'#ffd23f',llm:'#ff3dac',ok:'#2ef2d8',alert:'#ff4f5e'}}),
  lcd:{label:'Phosphor · LCD',blurb:'Reflective calculator LCD by day; blue backlight by night',style:'phosphor',glow:{light:0,dark:.6},
   light:{bg:'#b8c3a0',panel:'#c8d2b1',bg2:'#c2cdab',bg3:'#b4bf98',bg0:'#aab68c',line:'#94a07a',lineSoft:'#a5b08b',text:'#1b2416',muted:'#46533b',faint:'#6a7660',accent:'#1f2e18',accentDim:'#3c4d33',accent2:'#2b3d22',ok:'#1f5a1f',warn:'#7a5200',bad:'#8a1a1a',info:'#1b2f4a',
    k:{cpu:'#1f4a1a',gpu:'#1b2f5a',temp:'#7a4a10',mem:'#432460',net:'#124848',disk:'#2a4a1a',power:'#5a4800',llm:'#5c1a44',ok:'#1f5a1f',alert:'#8a1a1a'}},
   dark:{bg:'#061433',panel:'#0a1c45',bg2:'#0d2252',bg3:'#122b63',bg0:'#040e26',line:'#1e3b7a',lineSoft:'#173068',text:'#eaf2ff',muted:'#a9bde6',faint:'#7189bf',accent:'#ffffff',accentDim:'#b7c9f0',accent2:'#cfe0ff',ok:'#b9ffd8',warn:'#ffe7a3',bad:'#ffb3c1',info:'#cfe0ff',
    k:{cpu:'#b9ffd8',gpu:'#cfe0ff',temp:'#ffe7a3',mem:'#e2cfff',net:'#bff4ff',disk:'#d2ffe8',power:'#fff3c4',llm:'#ffd0f0',ok:'#b9ffd8',alert:'#ffb3c1'}}},
  ember:{label:'Ember',blurb:'Charcoal and amber',
   dark:{bg:'#0b0d10',panel:'#12151a',bg2:'#171b21',bg3:'#1e232b',bg0:'#0c0e11',line:'#262c36',lineSoft:'#1d222a',text:'#dfe5ee',muted:'#96a0b0',faint:'#6a7484',accent:'#f7931a',accentDim:'#b06c10',accent2:'#ffb85c',ok:'#2ecc8f',warn:'#f0b429',bad:'#ef5a5a',info:'#58a6ff'},
   light:{bg:'#f3f1ec',panel:'#ffffff',bg2:'#f8f6f2',bg3:'#eeeae3',bg0:'#ebe7e0',line:'#d8d2c8',lineSoft:'#e6e1d8',text:'#1c1f26',muted:'#5b6470',faint:'#8a929e',accent:'#d97706',accentDim:'#b8690a',accent2:'#a8640d',ok:'#1f9d63',warn:'#c48a08',bad:'#d63f3f',info:'#2f6fd6'}},
  mono:{label:'Mono',blurb:'Grayscale — quiet and focused',
   dark:{bg:'#0f0f0f',panel:'#161616',bg2:'#1b1b1b',bg3:'#232323',line:'#2c2c2c',text:'#e8e8e8',muted:'#9a9a9a',faint:'#6b6b6b',accent:'#f2f2f2',accentDim:'#9a9a9a',ok:'#9ccfae',warn:'#d9c48a',bad:'#d99090',info:'#a9b9c9'},
   light:{bg:'#f4f4f4',panel:'#ffffff',bg2:'#f8f8f8',bg3:'#ececec',line:'#d5d5d5',text:'#151515',muted:'#5a5a5a',faint:'#8c8c8c',accent:'#111111',accentDim:'#555555',ok:'#2f8f5b',warn:'#a37a12',bad:'#c04747',info:'#3a5f8a'}},
  github:{label:'GitHub',blurb:'GitHub Dark and Light Default',
   dark:{bg:'#0d1117',panel:'#161b22',bg2:'#1c2128',bg3:'#21262d',bg0:'#010409',line:'#30363d',lineSoft:'#262c34',text:'#e6edf3',muted:'#8b949e',faint:'#6e7681',accent:'#58a6ff',accentDim:'#1f6feb',accent2:'#a371f7',ok:'#3fb950',warn:'#d29922',bad:'#f85149',info:'#79c0ff'},
   light:{bg:'#f6f8fa',panel:'#ffffff',bg2:'#f6f8fa',bg3:'#eaeef2',bg0:'#eff2f5',line:'#d0d7de',lineSoft:'#e1e6eb',text:'#1f2328',muted:'#656d76',faint:'#8c959f',accent:'#0969da',accentDim:'#0a4fb5',accent2:'#8250df',ok:'#1a7f37',warn:'#9a6700',bad:'#cf222e',info:'#0969da'}},
  catppuccin:{label:'Catppuccin',blurb:'Soft pastels — Mocha and Latte',
   dark:{bg:'#181825',panel:'#1e1e2e',bg2:'#242438',bg3:'#313244',bg0:'#11111b',line:'#45475a',lineSoft:'#313244',text:'#cdd6f4',muted:'#a6adc8',faint:'#6c7086',accent:'#89dceb',accentDim:'#5fa8b6',accent2:'#cba6f7',ok:'#a6e3a1',warn:'#f9e2af',bad:'#f38ba8',info:'#89b4fa'},
   light:{bg:'#e6e9ef',panel:'#eff1f5',bg2:'#e9ecf2',bg3:'#dce0e8',bg0:'#dce0e8',line:'#bcc0cc',lineSoft:'#ccd0da',text:'#4c4f69',muted:'#6c6f85',faint:'#9ca0b0',accent:'#209fb5',accentDim:'#1a7f91',accent2:'#8839ef',ok:'#40a02b',warn:'#df8e1d',bad:'#d20f39',info:'#1e66f5'}},
 };
 /* Phosphor effects: all static layers or compositor-only animation (transform, opacity) */
 const FXDEF={glow:'normal',scan:'kiosk',vignette:'on',grain:'off',beam:'off',flicker:'off',fringe:'off'};
 const GLOW={off:0,soft:.5,normal:1,bright:1.6};
 const DEF={mode:'dark',theme:'noctyx',tunit:'F',fx:FXDEF,kiosk:{alerts:0,backups:0,rotation:false,fit:'shrink',dense:false,cols:0},custom:{bg:'#0b0e1a',panel:'#121629',text:'#e2e8f6',muted:'#97a3c2',accent:'#3fe6ff',line:'#242a47',ok:'#2ecc8f',warn:'#f0b429',bad:'#ef5a5a'}};
 function derive(b){const f={...b},dark=lum(f.bg)<.4;f.scheme=dark?'dark':'light';
  f.bg0??=mix(f.bg,dark?'#000000':'#ffffff',.25);f.bg2??=mix(f.panel,f.text,dark?.04:.03);f.bg3??=mix(f.panel,f.text,dark?.09:.07);
  f.lineSoft??=mix(f.line,f.bg,.4);f.faint??=mix(f.muted,f.bg,.3);f.accentDim??=mix(f.accent,f.bg,.35);f.accent2??=f.accent;
  f.accentInk??=lum(f.accent)>.45?'#0a0d14':'#ffffff';f.info??=dark?'#58a6ff':'#2f6fd6';
  f.scrim??=dark?'rgba(2,4,10,.62)':'rgba(20,24,34,.38)';f.glow??=mix(f.bg,f.accent,dark?.07:.05);f.dots??=mix(f.bg,f.text,dark?.07:.08);
  return f}
 const LS='nx_settings_v1',LSS='nx_site_settings_v1';
 const read=k=>{try{return JSON.parse(localStorage.getItem(k)||'null')}catch(e){return null}};
 const local=()=>read(LS)||{},site=()=>read(LSS)||{};
 const eff=()=>{const s=site(),l=local();return{...DEF,...s,...l,custom:{...DEF.custom,...(s.custom||{}),...(l.custom||{})},fx:{...FXDEF,...(s.fx||{}),...(l.fx||{})},kiosk:{...DEF.kiosk,...(s.kiosk||{}),...(l.kiosk||{})}}};
 const light=()=>{try{return matchMedia('(prefers-color-scheme: light)').matches}catch(e){return false}};
 function face(s){s=s||eff();if(s.theme=='custom')return{id:'custom',...derive(s.custom)};
  const p=PRESETS[s.theme]||PRESETS.noctyx,sc=s.mode=='system'?(light()?'light':'dark'):s.mode=='light'?'light':'dark';
  const k=p.style?(p[sc].k||Object.fromEntries(Object.entries(p.dark.k).map(([a,b])=>[a,mix(b,'#000000',.4)]))):null;
  return{id:PRESETS[s.theme]?s.theme:'noctyx',style:p.style||'',k,glowScale:(p.glow||{})[sc]??(sc=='light'?.35:1),...derive(p[sc])}}
 const VARS={bg:'--bg',panel:'--bg-1',bg2:'--bg-2',bg3:'--bg-3',bg0:'--bg-0',line:'--line',lineSoft:'--line-soft',text:'--fg',muted:'--fg-dim',faint:'--fg-faint',
  accent:'--accent',accentDim:'--accent-dim',accent2:'--accent-2',accentInk:'--accent-ink',ok:'--ok',warn:'--warn',bad:'--bad',info:'--info',scrim:'--scrim',glow:'--glow',dots:'--dots'};
 function apply(s){const f=face(s),r=document.documentElement;
  for(const[k,v]of Object.entries(VARS))if(f[k])r.style.setProperty(v,f[k]);
  for(const k of KK){if(f.k&&f.k[k])r.style.setProperty('--k-'+k,f.k[k]);else r.style.removeProperty('--k-'+k)}
  const fx=(s||eff()).fx||FXDEF;
  for(const[k,v]of Object.entries(FXDEF))r.dataset['fx'+k[0].toUpperCase()+k.slice(1)]=fx[k]??v;
  if(f.style)r.style.setProperty('--gl',String((GLOW[fx.glow]??1)*f.glowScale));else r.style.removeProperty('--gl');
  r.style.setProperty('color-scheme',f.scheme);r.dataset.theme=f.scheme;r.dataset.palette=f.id;r.dataset.style=f.style||'';return f}
 function save(patch){const l={...local(),...patch};if(patch.custom)l.custom={...(local().custom||{}),...patch.custom};if(patch.fx)l.fx={...(local().fx||{}),...patch.fx};if(patch.kiosk)l.kiosk={...(local().kiosk||{}),...patch.kiosk};
  try{localStorage.setItem(LS,JSON.stringify(l))}catch(e){};apply();return l}
 function clearLocal(){try{localStorage.removeItem(LS)}catch(e){};apply()}
 function setSite(s){try{localStorage.setItem(LSS,JSON.stringify(s||{}))}catch(e){};apply()}
 apply();
 try{matchMedia('(prefers-color-scheme: light)').addEventListener('change',()=>{if(eff().mode=='system')apply()})}catch(e){}
 return{PRESETS,BASE,DEF,FXDEF,derive,face,eff,local,site,apply,save,clearLocal,setSite,mix}
})();
