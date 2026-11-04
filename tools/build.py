#!/usr/bin/env python3
"""Genera index.html a partir de Main.dc.html (fuente de diseño).
Uso: python3 tools/build.py RUTA/Main.dc.html
"""
import re, sys, pathlib, json, hashlib, time
src = pathlib.Path(sys.argv[1]).read_text()
root = pathlib.Path(__file__).resolve().parent.parent

helmet = re.search(r'<helmet>(.*?)</helmet>', src, re.S).group(1)
body = re.search(r'</helmet>(.*?)</x-dc>', src, re.S).group(1)
script = re.search(r'<script type="text/x-dc"[^>]*>(.*?)</script>', src, re.S).group(1)

# sc-for / sc-if -> <template> (válido dentro de tablas)
body = re.sub(r'<sc-(for|if)\b', lambda m: '<template data-k="%s"' % m.group(1), body)
body = re.sub(r'</sc-(for|if)>', '</template>', body)

# LIVE viene de data.json
script, n = re.subn(r'const LIVE = \{.*?\n    \};', 'const LIVE = window.LIVE_DATA;', script, count=1, flags=re.S)
assert n == 1, 'no encontré el bloque LIVE'

pub = lambda s: s.replace('Gexbot', 'Perfil γ')
body, script = pub(body), pub(script)

runtime = r'''
(function(){
  const tpl = document.getElementById('tpl');
  const mount = document.getElementById('app');
  const lookup = (path, sc) => path.trim().split('.').reduce((o, k) => o == null ? undefined : o[k], sc);
  const ONE = /^\{\{([^}]+)\}\}$/;
  const val = (s, sc) => { const m = s.match(ONE); if (m) return lookup(m[1], sc);
    return s.replace(/\{\{([^}]+)\}\}/g, (_, p) => { const v = lookup(p, sc); return v == null ? '' : String(v); }); };
  const child = (sc, k, v) => { const o = Object.create(sc); Object.defineProperty(o, k, { value: v, enumerable: true }); return o; };
  function render(node, sc, out) {
    for (const n of node.childNodes) {
      if (n.nodeType === 3) { out.appendChild(document.createTextNode(n.data.includes('{{') ? val(n.data, sc) ?? '' : n.data)); continue; }
      if (n.nodeType !== 1) continue;
      if (n.localName === 'template') {
        const k = n.getAttribute('data-k');
        if (k === 'for') { const list = val(n.getAttribute('list'), sc) || []; const as = n.getAttribute('as');
          list.forEach(it => render(n.content, child(sc, as, it), out)); }
        else if (k === 'if') { if (val(n.getAttribute('value'), sc)) render(n.content, sc, out); }
        continue;
      }
      const el = document.createElementNS(n.namespaceURI, n.localName);
      for (const a of n.attributes) {
        if (a.name.startsWith('hint-')) continue;
        if (a.name.startsWith('on')) { const f = val(a.value, sc); if (typeof f === 'function') el.addEventListener(a.name.slice(2), f); continue; }
        const v = a.value.includes('{{') ? val(a.value, sc) : a.value;
        el.setAttribute(a.name, v == null ? '' : String(v));
      }
      render(n, sc, el); out.appendChild(el);
    }
  }
  class DCLogic { constructor(p) { this.props = p || {}; this.state = {}; } setState(s) { Object.assign(this.state, s); paint(); } }
  window.DCLogic = DCLogic;
  let comp;
  function paint() { if (!comp || !window.LIVE_DATA) return; const f = document.createDocumentFragment();
    try { render(tpl.content, comp.renderVals(), f); mount.replaceChildren(f); } catch (e) { console.error(e); } }
  window.__boot = C => { comp = new C({}); paint(); };
  let lastD = '', lastA = '', DATA = null, AUTO = null;
  const AUTO_URLS = ['https://api.github.com/repos/eazpiroz/katapunta-0dte/contents/auto.json?ref=live', 'https://raw.githubusercontent.com/eazpiroz/katapunta-0dte/live/auto.json'];
  const todayMX = () => new Intl.DateTimeFormat('en-CA', { timeZone: 'America/Mexico_City' }).format(new Date());
  function merge() {
    if (!DATA) return null;
    const d = JSON.parse(JSON.stringify(DATA));
    const a = AUTO;
    if (!a || a.date !== todayMX()) return d;
    d.on = true;
    d.day = Object.assign({}, d.day || {}, a.day || {});
    for (const tk of ['SPX', 'SPY', 'QQQ']) {
      const x = a[tk]; if (!x || !x.read) continue;
      const L = d[tk] || (d[tk] = { o: [], h: [], l: [], c: [], v: [], reads: [], rows: [] });
      if (x.c && x.c.length >= (L.c || []).length) ['o', 'h', 'l', 'c', 'v'].forEach(k => { L[k] = x[k]; });
      if (x.pre) L.pre = x.pre;
      if (x.rows && x.rows.length) L.rows = [x.rows];
      const manual = DATA.date === a.date ? (L.reads || []) : [];
      const ar = Object.assign({}, x.read);
      const mg = manual.length ? manual[manual.length - 1] : null;
      const mins = s => { const p = String(s).split(':'); return +p[0] * 60 + +p[1]; };
      if (mg && mg.gt && mg.zg && mins(ar.t) - mins(mg.t) <= 30) { ar.zg = mg.zg; ar.gt = mg.gt; ar.cwv = mg.mp; ar.pwv = mg.mn; }
      L.reads = manual.concat([ar]);
    }
    return d;
  }
  async function load() {
    try {
      const r = await fetch('data.json?t=' + Date.now(), { cache: 'no-store' }); const t = await r.text();
      if (t !== lastD) { lastD = t; const d = JSON.parse(t);
        if (d.build && d.build !== window.BUILD && !location.search.includes(d.build)) { location.replace(location.pathname + '?v=' + d.build); return; }
        DATA = d; }
    } catch (e) { console.error(e); }
    for (const u of AUTO_URLS) {
      try {
        const r = await fetch(u, { headers: { Accept: 'application/vnd.github.raw+json' }, cache: 'no-cache' });
        if (r.ok) { const t = await r.text(); if (t !== lastA) { lastA = t; AUTO = JSON.parse(t); } break; }
      } catch (e) { console.error(e); }
    }
    const m = merge(); if (m) { window.LIVE_DATA = m; paint(); }
  }
  window.__load = load;
  setInterval(load, 75000);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) load(); });
})();
'''

build = hashlib.sha1((body+script+str(time.time())).encode()).hexdigest()[:8]
html = f'''<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>Flight Deck 0DTE</title>
{helmet}
</head>
<body>
<div id="app"><div style="color:#8A94A3;font-family:Inter,system-ui,sans-serif;padding:24px">Cargando lectura…</div></div>
<template id="tpl">{body}</template>
<script>window.BUILD='{build}';{runtime}</script>
<script>
{script}
window.__boot(Component);
window.__load();
</script>
</body>
</html>
'''
(root / 'index.html').write_text(html)
dp = root / 'data.json'
if dp.exists():
    d = json.loads(dp.read_text()); d['build'] = build; dp.write_text(json.dumps(d, separators=(',', ':')))
print('index.html', len(html))
