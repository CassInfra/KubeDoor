/* ==========================================================================
   花折 · KubeDoor — landing page interactions
   原生 JS（经典脚本，无模块 / 无依赖），file:// 下也能运行。
   ========================================================================== */
(function () {
  'use strict';

  var doc = document;
  var root = doc.documentElement;
  var params;
  try { params = new URLSearchParams(window.location.search); } catch (e) { params = { has: function () { return false; }, get: function () { return null; } }; }
  var mqReduce = window.matchMedia ? window.matchMedia('(prefers-reduced-motion: reduce)') : null;
  var STILL = params.has('still');
  var FORCE_MOTION = params.has('motion'); // 测试用：忽略系统“减少动态效果”
  var reduceMotion = STILL || (!FORCE_MOTION && !!(mqReduce && mqReduce.matches));
  var hasIO = 'IntersectionObserver' in window;

  function $(sel, ctx) { return (ctx || doc).querySelector(sel); }
  function $$(sel, ctx) { return Array.prototype.slice.call((ctx || doc).querySelectorAll(sel)); }
  function rand(a, b) { return a + Math.random() * (b - a); }

  /* ------------------------------------------------------------------
     a11y live region (copy feedback etc.)
     ------------------------------------------------------------------ */
  var live = doc.createElement('div');
  live.setAttribute('aria-live', 'polite');
  live.setAttribute('role', 'status');
  live.style.cssText = 'position:absolute;width:1px;height:1px;margin:-1px;padding:0;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap;border:0;';
  doc.body.appendChild(live);
  function announce(msg) { live.textContent = ''; setTimeout(function () { live.textContent = msg; }, 30); }

  /* ------------------------------------------------------------------
     header: scrolled state + active section
     ------------------------------------------------------------------ */
  var header = $('#site-header');
  function onScroll() { if (header) header.classList.toggle('is-scrolled', (window.scrollY || window.pageYOffset) > 8); }
  window.addEventListener('scroll', onScroll, { passive: true });
  onScroll();

  var navLinks = $$('.nav-links a');
  if (hasIO && navLinks.length) {
    var linkFor = {};
    navLinks.forEach(function (a) { linkFor[a.getAttribute('href').slice(1)] = a; });
    var clearActive = function () { navLinks.forEach(function (a) { a.classList.remove('is-active'); a.removeAttribute('aria-current'); }); };
    var sectionObserver = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (entry.target.id === 'top') { if (entry.isIntersecting) clearActive(); return; }
        var link = linkFor[entry.target.id];
        if (!link) return;
        if (entry.isIntersecting) {
          navLinks.forEach(function (a) { a.classList.remove('is-active'); a.removeAttribute('aria-current'); });
          link.classList.add('is-active');
          link.setAttribute('aria-current', 'true');
        }
      });
    }, { rootMargin: '-45% 0px -50% 0px' });
    Object.keys(linkFor).concat(['top']).forEach(function (id) { var s = doc.getElementById(id); if (s) sectionObserver.observe(s); });
  }

  /* ------------------------------------------------------------------
     mobile menu
     ------------------------------------------------------------------ */
  var toggle = $('.nav-toggle');
  var menu = $('#mobile-menu');
  function setMenu(open) {
    if (!toggle || !menu) return;
    toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
    toggle.setAttribute('aria-label', open ? '关闭导航菜单' : '打开导航菜单');
    menu.hidden = !open;
    menu.classList.toggle('is-open', open);
  }
  if (toggle && menu) {
    toggle.addEventListener('click', function () { setMenu(menu.hidden); });
    menu.addEventListener('click', function (e) { if (e.target.closest('a')) setMenu(false); });
    doc.addEventListener('click', function (e) {
      if (!menu.hidden && !e.target.closest('#mobile-menu') && !e.target.closest('.nav-toggle')) setMenu(false);
    });
    doc.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && !menu.hidden) { setMenu(false); toggle.focus(); }
    });
    if (window.matchMedia) {
      var mqWide = window.matchMedia('(min-width: 901px)');
      var onWide = function (e) { if (e.matches) setMenu(false); };
      if (mqWide.addEventListener) mqWide.addEventListener('change', onWide); else if (mqWide.addListener) mqWide.addListener(onWide);
    }
  }

  /* ------------------------------------------------------------------
     GitHub star count (cached 6h, 3s timeout, fails silently)
     ------------------------------------------------------------------ */
  (function stars() {
    var wrap = $('[data-star-count]');
    var val = $('[data-star-value]');
    if (!wrap || !val || STILL) return;
    var KEY = 'kubedoor-site-stars';
    var TTL = 6 * 3600 * 1000;
    function fmt(n) { return n >= 1000 ? (Math.round(n / 100) / 10).toFixed(1).replace(/\.0$/, '') + 'k' : String(n); }
    function show(n) {
      if (typeof n !== 'number' || !isFinite(n) || n <= 0) return;
      val.textContent = fmt(n);
      wrap.hidden = false;
      var link = wrap.closest('a');
      if (link) link.setAttribute('aria-label', 'GitHub 仓库（' + n + ' 个 Star）');
    }
    try {
      var cached = JSON.parse(window.localStorage.getItem(KEY) || 'null');
      if (cached && Date.now() - cached.t < TTL) { show(cached.n); return; }
    } catch (e) { /* storage unavailable */ }
    if (!window.fetch || (navigator.onLine === false)) return;
    setTimeout(function () {
      var ctrl = window.AbortController ? new AbortController() : null;
      var timer = setTimeout(function () { if (ctrl) ctrl.abort(); }, 3000);
      fetch('https://api.github.com/repos/CassInfra/KubeDoor', ctrl ? { signal: ctrl.signal } : {})
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (d) {
          clearTimeout(timer);
          if (d && typeof d.stargazers_count === 'number') {
            show(d.stargazers_count);
            try { window.localStorage.setItem(KEY, JSON.stringify({ n: d.stargazers_count, t: Date.now() })); } catch (e) { /* ignore */ }
          }
        })
        .catch(function () { clearTimeout(timer); });
    }, 1200);
  })();

  /* ------------------------------------------------------------------
     reveal on scroll
     ------------------------------------------------------------------ */
  var revealEls = $$('.reveal');
  if (hasIO && !reduceMotion) root.classList.add('js-ready');
  if (!hasIO || reduceMotion) {
    revealEls.forEach(function (el) { el.classList.add('is-visible'); });
  } else {
    var revealObserver = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (entry.isIntersecting) {
          entry.target.classList.add('is-visible');
          revealObserver.unobserve(entry.target);
        }
      });
    }, { rootMargin: '0px 0px -6% 0px', threshold: 0.08 });
    revealEls.forEach(function (el) { revealObserver.observe(el); });
  }

  /* ------------------------------------------------------------------
     count-up stats (HTML already holds final values)
     ------------------------------------------------------------------ */
  var statsBox = $('.stats');
  if (statsBox && hasIO && !reduceMotion && window.requestAnimationFrame) {
    var counted = false;
    var countObserver = new IntersectionObserver(function (entries) {
      if (counted || !entries[0].isIntersecting) return;
      counted = true;
      countObserver.disconnect();
      $$('[data-count]', statsBox).forEach(function (el, i) {
        var target = parseInt(el.getAttribute('data-count'), 10);
        if (!isFinite(target)) return;
        var dur = 1100 + i * 90;
        var start = null;
        requestAnimationFrame(function step(now) {
          if (start === null) start = now;
          var p = Math.min(1, (now - start) / dur);
          var eased = 1 - Math.pow(1 - p, 3);
          el.textContent = String(Math.round(target * eased));
          if (p < 1) requestAnimationFrame(step); else el.textContent = String(target);
        });
        // 兜底：rAF 被节流（后台标签页等）时也保证最终显示正确数值
        setTimeout(function () { el.textContent = String(target); }, dur + 400);
      });
    }, { threshold: 0.4 });
    countObserver.observe(statsBox);
  }

  /* ------------------------------------------------------------------
     pointer-following glow on cards
     ------------------------------------------------------------------ */
  if (!reduceMotion && window.matchMedia && window.matchMedia('(hover: hover)').matches) {
    doc.addEventListener('pointermove', function (e) {
      var card = e.target.closest && e.target.closest('.glow-card');
      if (!card) return;
      var r = card.getBoundingClientRect();
      card.style.setProperty('--mx', (e.clientX - r.left) + 'px');
      card.style.setProperty('--my', (e.clientY - r.top) + 'px');
    }, { passive: true });
  }

  /* ------------------------------------------------------------------
     copy buttons
     ------------------------------------------------------------------ */
  function legacyCopy(text) {
    var ta = doc.createElement('textarea');
    ta.value = text;
    ta.setAttribute('readonly', '');
    ta.style.cssText = 'position:fixed;top:-1000px;left:-1000px;opacity:0;';
    doc.body.appendChild(ta);
    ta.select();
    var ok = false;
    try { ok = doc.execCommand('copy'); } catch (e) { ok = false; }
    doc.body.removeChild(ta);
    return ok;
  }
  function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) {
      return navigator.clipboard.writeText(text).then(function () { return true; }, function () { return legacyCopy(text); });
    }
    return Promise.resolve(legacyCopy(text));
  }
  doc.addEventListener('click', function (e) {
    var btn = e.target.closest && e.target.closest('[data-copy-target]');
    if (!btn) return;
    var src = doc.getElementById(btn.getAttribute('data-copy-target'));
    if (!src) return;
    var text = src.textContent.replace(new RegExp(String.fromCharCode(160), 'g'), ' ').replace(/\s+$/, '') + '\n';
    copyText(text).then(function (ok) {
      var label = btn.querySelector('span');
      if (label) label.textContent = ok ? '已复制' : '复制失败';
      btn.classList.toggle('is-copied', ok);
      announce(ok ? '已复制到剪贴板' : '复制失败，请手动选择文本复制');
      clearTimeout(btn._copyTimer);
      btn._copyTimer = setTimeout(function () {
        if (label) label.textContent = '复制';
        btn.classList.remove('is-copied');
      }, 1800);
    });
  });

  /* ------------------------------------------------------------------
     tabs (MCP config)
     ------------------------------------------------------------------ */
  $$('[role="tablist"]').forEach(function (list) {
    var tabs = $$('[role="tab"]', list);
    function select(tab, focus) {
      tabs.forEach(function (t) {
        var on = t === tab;
        t.setAttribute('aria-selected', on ? 'true' : 'false');
        t.tabIndex = on ? 0 : -1;
        var panel = doc.getElementById(t.getAttribute('aria-controls'));
        if (panel) panel.hidden = !on;
      });
      if (focus) tab.focus();
    }
    tabs.forEach(function (tab, i) {
      tab.addEventListener('click', function () { select(tab, false); });
      tab.addEventListener('keydown', function (e) {
        var next = null;
        if (e.key === 'ArrowRight') next = tabs[(i + 1) % tabs.length];
        else if (e.key === 'ArrowLeft') next = tabs[(i - 1 + tabs.length) % tabs.length];
        else if (e.key === 'Home') next = tabs[0];
        else if (e.key === 'End') next = tabs[tabs.length - 1];
        if (next) { e.preventDefault(); select(next, true); }
      });
    });
  });

  /* ------------------------------------------------------------------
     lightbox (native <dialog>)
     ------------------------------------------------------------------ */
  (function lightbox() {
    var dlg = $('#lightbox');
    var items = $$('[data-gallery] .shot-btn');
    if (!dlg || !items.length) return;
    var img = $('.lb-img', dlg);
    var cap = $('.lb-caption', dlg);
    var count = $('.lb-count', dlg);
    var btnPrev = $('.lb-prev', dlg);
    var btnNext = $('.lb-next', dlg);
    var btnClose = $('.lb-close', dlg);
    var idx = 0;
    var lastFocus = null;
    var supportsModal = typeof dlg.showModal === 'function';

    function preload(i) { var b = items[(i + items.length) % items.length]; var im = new Image(); im.src = b.getAttribute('data-full'); }
    function show(i) {
      idx = (i + items.length) % items.length;
      var b = items[idx];
      var thumb = b.querySelector('img');
      img.removeAttribute('src');
      img.width = parseInt(b.getAttribute('data-w'), 10) || 1600;
      img.height = parseInt(b.getAttribute('data-h'), 10) || 900;
      img.src = b.getAttribute('data-full');
      img.alt = thumb ? thumb.alt : '';
      cap.textContent = b.getAttribute('data-caption') || '';
      count.textContent = (idx + 1) + ' / ' + items.length;
      preload(idx + 1);
    }
    function open(i, trigger) {
      lastFocus = trigger || doc.activeElement;
      show(i);
      if (supportsModal) { if (!dlg.open) dlg.showModal(); } else { dlg.setAttribute('open', ''); }
      root.classList.add('lb-open');
      btnClose.focus();
    }
    function close() {
      if (supportsModal) { if (dlg.open) dlg.close(); } else { dlg.removeAttribute('open'); onClosed(); }
    }
    function onClosed() {
      root.classList.remove('lb-open');
      img.removeAttribute('src');
      if (lastFocus && lastFocus.focus) lastFocus.focus();
    }
    items.forEach(function (b, i) { b.addEventListener('click', function () { open(i, b); }); });
    btnPrev.addEventListener('click', function () { show(idx - 1); });
    btnNext.addEventListener('click', function () { show(idx + 1); });
    btnClose.addEventListener('click', close);
    dlg.addEventListener('close', onClosed);
    dlg.addEventListener('click', function (e) {
      if (e.target === dlg || e.target.classList.contains('lb-inner') || e.target.classList.contains('lb-figure')) close();
    });
    dlg.addEventListener('keydown', function (e) {
      if (e.key === 'ArrowLeft') { e.preventDefault(); show(idx - 1); }
      else if (e.key === 'ArrowRight') { e.preventDefault(); show(idx + 1); }
      else if (e.key === 'Escape' && !supportsModal) { e.preventDefault(); close(); }
    });
    var touchX = null;
    dlg.addEventListener('touchstart', function (e) { touchX = e.touches[0].clientX; }, { passive: true });
    dlg.addEventListener('touchend', function (e) {
      if (touchX === null) return;
      var dx = e.changedTouches[0].clientX - touchX;
      touchX = null;
      if (Math.abs(dx) > 50) show(idx + (dx < 0 ? 1 : -1));
    }, { passive: true });
  })();

  /* ------------------------------------------------------------------
     hero canvas: constellation of clusters with travelling pulses
     ------------------------------------------------------------------ */
  (function constellation() {
    var canvas = $('.hero-canvas');
    var hero = $('.hero');
    if (!canvas || !hero || !canvas.getContext) return;
    var ctx = canvas.getContext('2d');
    if (!ctx) return;
    var COLORS = [[34, 211, 238], [59, 130, 246], [139, 92, 246], [96, 165, 250]];
    var W = 0, H = 0, dpr = 1;
    var nodes = [], edges = [], pulses = [], sprites = [];
    var raf = 0, running = false, visible = true, last = 0, spawnIn = 0, start = 0;

    function makeSprite(c) {
      var s = doc.createElement('canvas');
      s.width = s.height = 64;
      var g = s.getContext('2d');
      var grd = g.createRadialGradient(32, 32, 0, 32, 32, 32);
      grd.addColorStop(0, 'rgba(' + c.join(',') + ',0.9)');
      grd.addColorStop(0.25, 'rgba(' + c.join(',') + ',0.35)');
      grd.addColorStop(1, 'rgba(' + c.join(',') + ',0)');
      g.fillStyle = grd;
      g.fillRect(0, 0, 64, 64);
      return s;
    }
    COLORS.forEach(function (c) { sprites.push(makeSprite(c)); });

    function build() {
      var rect = canvas.getBoundingClientRect();
      W = Math.max(1, rect.width);
      H = Math.max(1, rect.height);
      dpr = Math.min(window.devicePixelRatio || 1, 1.75);
      canvas.width = Math.round(W * dpr);
      canvas.height = Math.round(H * dpr);
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      nodes = []; edges = []; pulses = [];
      // 文案区域（含 24px 余量）内不放节点、不画连线，避免光点压在标题和正文上
      var av = null;
      var copy = $('.hero-copy');
      if (copy) {
        var cr = copy.getBoundingClientRect();
        av = { l: cr.left - rect.left - 24, t: cr.top - rect.top - 24, r: cr.right - rect.left + 24, b: cr.bottom - rect.top + 24 };
      }
      function inAvoid(x, y) { return !!av && x > av.l && x < av.r && y > av.t && y < av.b; }
      function crossesAvoid(p, q) {
        if (!av) return false;
        for (var i = 0; i <= 16; i++) {
          var f = i / 16;
          if (inAvoid(p.bx + (q.bx - p.bx) * f, p.by + (q.by - p.by) * f)) return true;
        }
        return false;
      }
      var narrow = W < 760;
      var cols = narrow ? 3 : 5;
      var rows = narrow ? 5 : 3;
      var hub = { bx: W * 0.5, by: H * (narrow ? 0.985 : 0.95), r: 3.6, ci: 0, amp: 3, sp: 0.4, ph: 0, kind: 'hub' };
      nodes.push(hub);
      var agents = [];
      var cw = W / cols, ch = H / rows;
      for (var ry = 0; ry < rows; ry++) {
        for (var cx = 0; cx < cols; cx++) {
          var ax = cw * (cx + 0.5) + rand(-cw * 0.22, cw * 0.22);
          var ay = ch * (ry + 0.5) + rand(-ch * 0.2, ch * 0.2);
          if (Math.abs(ax - hub.bx) < 90 && Math.abs(ay - hub.by) < 70) ay -= 90;
          if (inAvoid(ax, ay)) continue;
          var ci = (cx + ry) % COLORS.length;
          var agent = { bx: ax, by: ay, r: 2.6, ci: ci, amp: rand(2, 5), sp: rand(0.25, 0.55), ph: rand(0, 6.28), kind: 'agent' };
          var ai = nodes.push(agent) - 1;
          agents.push(ai);
          var n = Math.round(rand(narrow ? 3 : 4, narrow ? 5 : 7));
          var rad = Math.min(cw, ch) * (narrow ? 0.34 : 0.3);
          var prevLeaf = -1;
          for (var k = 0; k < n; k++) {
            var ang = (k / n) * Math.PI * 2 + rand(-0.4, 0.4);
            var dist = rad * rand(0.45, 1);
            var lx = ax + Math.cos(ang) * dist, ly = ay + Math.sin(ang) * dist * 0.75;
            if (inAvoid(lx, ly)) { prevLeaf = -1; continue; }
            var leaf = { bx: lx, by: ly, r: rand(1.1, 1.9), ci: ci, amp: rand(2, 6), sp: rand(0.3, 0.8), ph: rand(0, 6.28), kind: 'leaf' };
            var li = nodes.push(leaf) - 1;
            edges.push({ a: ai, b: li, kind: 'intra' });
            if (prevLeaf >= 0 && Math.random() < 0.45) edges.push({ a: prevLeaf, b: li, kind: 'intra' });
            prevLeaf = li;
          }
          if (!crossesAvoid(agent, hub)) edges.push({ a: ai, b: 0, kind: 'hub' });
        }
      }
      agents.forEach(function (ai) {
        var a = nodes[ai];
        var best = agents.filter(function (aj) { return aj !== ai; }).sort(function (p, q) {
          return dist2(nodes[p], a) - dist2(nodes[q], a);
        }).slice(0, 2);
        best.forEach(function (bj) { if (ai < bj && !crossesAvoid(a, nodes[bj])) edges.push({ a: ai, b: bj, kind: 'mesh' }); });
      });
      update(0);
    }
    function dist2(p, q) { var dx = p.bx - q.bx, dy = p.by - q.by; return dx * dx + dy * dy; }

    function update(t) {
      for (var i = 0; i < nodes.length; i++) {
        var n = nodes[i];
        n.x = n.bx + Math.sin(t * n.sp + n.ph) * n.amp;
        n.y = n.by + Math.cos(t * n.sp * 0.8 + n.ph) * n.amp * 0.8;
      }
    }

    function spawn() {
      if (pulses.length > 26 || !edges.length) return;
      var pool = Math.random();
      var kind = pool < 0.5 ? 'hub' : (pool < 0.82 ? 'intra' : 'mesh');
      var cands = edges.filter(function (e) { return e.kind === kind; });
      if (!cands.length) return;
      var e = cands[(Math.random() * cands.length) | 0];
      var toHub = kind === 'hub' ? Math.random() < 0.72 : Math.random() < 0.5;
      pulses.push({ e: e, p: 0, dir: toHub ? 1 : -1, speed: kind === 'hub' ? rand(150, 240) : rand(60, 120), ci: nodes[e.a].ci });
    }

    function drawEdges(kind, alpha, width) {
      ctx.beginPath();
      for (var i = 0; i < edges.length; i++) {
        var e = edges[i];
        if (e.kind !== kind) continue;
        var a = nodes[e.a], b = nodes[e.b];
        ctx.moveTo(a.x, a.y);
        ctx.lineTo(b.x, b.y);
      }
      ctx.strokeStyle = 'rgba(125,160,255,' + alpha + ')';
      ctx.lineWidth = width;
      ctx.stroke();
    }

    function draw(dt) {
      ctx.clearRect(0, 0, W, H);
      drawEdges('hub', 0.055, 1);
      drawEdges('mesh', 0.08, 1);
      drawEdges('intra', 0.15, 1);
      ctx.globalCompositeOperation = 'lighter';
      for (var i = pulses.length - 1; i >= 0; i--) {
        var pl = pulses[i];
        var a = nodes[pl.e.a], b = nodes[pl.e.b];
        var sx = pl.dir > 0 ? a.x : b.x, sy = pl.dir > 0 ? a.y : b.y;
        var ex = pl.dir > 0 ? b.x : a.x, ey = pl.dir > 0 ? b.y : a.y;
        var len = Math.sqrt((ex - sx) * (ex - sx) + (ey - sy) * (ey - sy)) || 1;
        pl.p += (pl.speed * dt) / len;
        if (pl.p >= 1) { pulses.splice(i, 1); continue; }
        var px = sx + (ex - sx) * pl.p, py = sy + (ey - sy) * pl.p;
        var tail = Math.min(0.22, 46 / len);
        var tp = Math.max(0, pl.p - tail);
        var tx = sx + (ex - sx) * tp, ty = sy + (ey - sy) * tp;
        var c = COLORS[pl.ci];
        var grd = ctx.createLinearGradient(tx, ty, px, py);
        grd.addColorStop(0, 'rgba(' + c.join(',') + ',0)');
        grd.addColorStop(1, 'rgba(' + c.join(',') + ',0.85)');
        ctx.strokeStyle = grd;
        ctx.lineWidth = 1.6;
        ctx.beginPath();
        ctx.moveTo(tx, ty);
        ctx.lineTo(px, py);
        ctx.stroke();
        ctx.drawImage(sprites[pl.ci], px - 9, py - 9, 18, 18);
      }
      for (var j = 0; j < nodes.length; j++) {
        var n = nodes[j];
        if (n.kind !== 'leaf') {
          var gs = n.kind === 'hub' ? 46 : 24;
          ctx.globalAlpha = n.kind === 'hub' ? 0.9 : 0.55;
          ctx.drawImage(sprites[n.ci], n.x - gs / 2, n.y - gs / 2, gs, gs);
          ctx.globalAlpha = 1;
        }
      }
      ctx.globalCompositeOperation = 'source-over';
      for (var k = 0; k < nodes.length; k++) {
        var m = nodes[k];
        var col = COLORS[m.ci];
        ctx.beginPath();
        ctx.arc(m.x, m.y, m.r, 0, Math.PI * 2);
        ctx.fillStyle = m.kind === 'leaf' ? 'rgba(' + col.join(',') + ',0.55)' : 'rgba(225,245,255,0.95)';
        ctx.fill();
        if (m.kind !== 'leaf') {
          ctx.beginPath();
          ctx.arc(m.x, m.y, m.r + (m.kind === 'hub' ? 5 : 3.2), 0, Math.PI * 2);
          ctx.strokeStyle = 'rgba(' + col.join(',') + ',0.45)';
          ctx.lineWidth = 1;
          ctx.stroke();
        }
      }
    }

    function frame(now) {
      raf = 0;
      if (!running) return;
      if (!start) { start = now; last = now; }
      var dt = Math.min(0.05, (now - last) / 1000);
      last = now;
      var t = (now - start) / 1000;
      spawnIn -= dt;
      if (spawnIn <= 0) { spawn(); spawnIn = rand(0.14, 0.32); }
      update(t);
      draw(dt);
      raf = requestAnimationFrame(frame);
    }
    function play() {
      if (reduceMotion || running || !visible || doc.hidden) return;
      running = true;
      last = performance.now();
      if (!raf) raf = requestAnimationFrame(frame);
    }
    function stop() { running = false; if (raf) { cancelAnimationFrame(raf); raf = 0; } }

    build();
    // seed a few pulses so the very first frame already looks alive
    for (var s = 0; s < 10; s++) { spawn(); }
    pulses.forEach(function (p) { p.p = Math.random() * 0.8; });
    draw(0);

    if (hasIO) {
      new IntersectionObserver(function (entries) {
        visible = entries[0].isIntersecting;
        if (visible) play(); else stop();
      }).observe(hero);
    }
    doc.addEventListener('visibilitychange', function () { if (doc.hidden) stop(); else play(); });
    var resizeTimer = 0;
    function onResize() {
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(function () {
        var rect = canvas.getBoundingClientRect();
        if (Math.abs(rect.width - W) < 2 && Math.abs(rect.height - H) < 2) return;
        build();
        draw(0);
      }, 180);
    }
    if ('ResizeObserver' in window) new ResizeObserver(onResize).observe(canvas); else window.addEventListener('resize', onResize);
    if (mqReduce) {
      var onReduce = function (e) { reduceMotion = STILL || (!FORCE_MOTION && e.matches); if (reduceMotion) stop(); else play(); };
      if (mqReduce.addEventListener) mqReduce.addEventListener('change', onReduce); else if (mqReduce.addListener) mqReduce.addListener(onReduce);
    }
    play();
  })();

  /* ------------------------------------------------------------------
     hero console: scripted KubeDoor 助手 conversation
     ------------------------------------------------------------------ */
  (function consoleDemo() {
    var box = $('[data-demo]');
    if (!box) return;
    var body = $('[data-demo-body]', box);
    var inputWrap = $('.console-input', box);
    var typed = $('[data-demo-input]', box);
    var sendBtn = $('.send-btn', box);
    if (!body || !inputWrap || !typed || !sendBtn) return;

    var stopAt = params.get('demo'); // 测试用：?demo=approval 停在待批准卡片
    var fast = !!stopAt;

    function showStatic() {
      var users = $$('.msg-user', body);
      var anchor = users[1] || $('.approve', body);
      if (anchor) body.scrollTop = Math.max(0, anchor.offsetTop - 14);
    }
    if (reduceMotion || !window.Promise) { showStatic(); return; }

    var CLUSTER = 'prod-a';
    var NAMESPACE = 'demo';
    var DEPLOYMENT = 'deploy-demo-order';
    var SCRIPT = [
      { type: 'type', text: '这个服务最近频繁重启，结合日志、事件和监控帮我找原因' },
      { type: 'send' },
      { type: 'think', ms: 600 },
      { type: 'tools', note: '只读 · 自动执行', items: ['pods', 'previous_logs', 'events', 'metrics'] },
      { type: 'ai', blocks: [
        { p: [['b', '结论：'], ['t', '容器内存长期贴近 limit（512Mi），上次退出原因为 '], ['c', 'OOMKilled'], ['t', '，随后进入 BackOff 反复重启。']] },
        { table: { head: ['证据', '来源', '结果'], rows: [
          ['上次退出', ['c', 'pods'], 'OOMKilled · 退出码 137'],
          ['退出前日志', ['c', 'previous_logs'], '无应用报错，进程被终止'],
          ['内存峰值（6h）', ['c', 'metrics'], '约为 limit 的 98%'],
          ['近期事件', ['c', 'events'], 'BackOff × 12']
        ] } },
        { p: [['t', '该命名空间未开启准入控制，建议将内存 limit 调整为 1Gi，需要我提交修改吗？']] }
      ] },
      { type: 'pause', ms: 700 },
      { type: 'type', text: '好，把内存 limit 调到 1Gi' },
      { type: 'send' },
      { type: 'think', ms: 550 },
      { type: 'approve' },
      { type: 'ai', blocks: [
        { p: [['t', '已按批准时冻结的参数执行，Deployment 开始滚动更新：']] },
        { table: { head: ['项目', '修改前', '修改后'], rows: [['内存 limit', '512Mi', { ok: '1Gi' }]] } },
        { p: [['t', '稍后可以让我核验新 Pod 的就绪状态与内存曲线。']] }
      ] },
      { type: 'pause', ms: 5200 }
    ];

    var paused = false;
    var waiters = [];
    function gate() { return paused ? new Promise(function (r) { waiters.push(r); }) : Promise.resolve(); }
    function setPaused(p) {
      paused = p;
      if (!p) { var w = waiters; waiters = []; w.forEach(function (fn) { fn(); }); }
    }
    function sleep(ms) {
      if (fast) return Promise.resolve();
      return new Promise(function (r) { setTimeout(r, ms); }).then(gate);
    }
    function el(tag, cls, text) {
      var e = doc.createElement(tag);
      if (cls) e.className = cls;
      if (text != null) e.textContent = text;
      return e;
    }
    function scrollDown(instant) {
      var top = body.scrollHeight - body.clientHeight;
      if (top <= 0) return;
      if (instant || fast || !body.scrollTo) body.scrollTop = top;
      else body.scrollTo({ top: top, behavior: 'smooth' });
    }
    function icon(id) {
      var svgNS = 'http://www.w3.org/2000/svg';
      var s = doc.createElementNS(svgNS, 'svg');
      s.setAttribute('class', 'icon');
      s.setAttribute('aria-hidden', 'true');
      var u = doc.createElementNS(svgNS, 'use');
      u.setAttribute('href', '#' + id);
      s.appendChild(u);
      return s;
    }
    function aiMeta() {
      var m = el('div', 'msg-meta');
      m.appendChild(el('span', 'ai-mini', 'AI'));
      m.appendChild(doc.createTextNode('AI 助手 · ' + CLUSTER));
      return m;
    }

    async function typeText(text) {
      inputWrap.classList.add('is-typing');
      for (var i = 1; i <= text.length; i++) {
        typed.textContent = text.slice(0, i);
        inputWrap.classList.add('has-text');
        await sleep(rand(18, 36));
      }
      await sleep(300);
    }
    function emptyState() {
      // 与产品空会话的引导文案一致
      var e = el('div', 'empty-state');
      e.appendChild(el('span', 'es-badge', 'AI'));
      e.appendChild(el('b', null, '描述你要处理的 Kubernetes 问题'));
      e.appendChild(el('span', null, '可以查询日志、分析资源、修改配置或排查 Istio 路由。'));
      e.appendChild(el('span', null, '助手会选择合适的数据来源，修改前展示操作供你批准。'));
      return e;
    }
    async function send() {
      var es = $('.empty-state', body);
      if (es && es.parentNode) es.parentNode.removeChild(es);
      sendBtn.classList.add('is-pressed');
      await sleep(170);
      sendBtn.classList.remove('is-pressed');
      var text = typed.textContent;
      typed.textContent = '';
      inputWrap.classList.remove('has-text', 'is-typing');
      var msg = el('div', 'msg msg-user is-enter');
      msg.appendChild(el('div', 'msg-meta', '你'));
      msg.appendChild(el('div', 'msg-bubble', text));
      body.appendChild(msg);
      scrollDown();
    }
    var thinkingEl = null;
    async function think(ms) {
      thinkingEl = el('div', 'msg msg-ai is-enter');
      thinkingEl.appendChild(aiMeta());
      thinkingEl.appendChild(el('div', 'thinking', '正在思考并检查集群…'));
      body.appendChild(thinkingEl);
      scrollDown();
      await sleep(ms);
    }
    function clearThinking() { if (thinkingEl && thinkingEl.parentNode) thinkingEl.parentNode.removeChild(thinkingEl); thinkingEl = null; }

    async function tools(step) {
      clearThinking();
      var group = el('div', 'tool-group is-enter');
      var head = el('div', 'tool-head');
      var label = el('span');
      label.appendChild(doc.createTextNode('工具调用 '));
      var num = el('b', null, '0');
      label.appendChild(num);
      head.appendChild(label);
      head.appendChild(el('span', 'tool-head-note', step.note));
      group.appendChild(head);
      var list = el('div', 'tool-list');
      group.appendChild(list);
      body.appendChild(group);
      scrollDown();
      for (var i = 0; i < step.items.length; i++) {
        await sleep(i === 0 ? 200 : 100);
        var chip = el('span', 'tool-chip is-running is-appear');
        chip.appendChild(el('i', 'st'));
        chip.appendChild(el('code', null, step.items[i]));
        list.appendChild(chip);
        num.textContent = String(i + 1);
        await sleep(rand(300, 450));
        chip.classList.remove('is-running');
        chip.classList.add('is-done');
      }
      await sleep(260);
    }

    function richInto(parent, part) {
      // part: ['t'|'b'|'c', text] ; returns node to stream into + text
      var kind = part[0], text = part[1], node;
      if (kind === 'b') { node = el('strong'); parent.appendChild(node); }
      else if (kind === 'c') { node = el('code'); parent.appendChild(node); }
      else { node = doc.createTextNode(''); parent.appendChild(node); }
      return { node: node, text: text };
    }
    async function stream(parent, parts) {
      for (var i = 0; i < parts.length; i++) {
        var target = richInto(parent, parts[i]);
        var full = target.text;
        var pos = 0;
        while (pos < full.length) {
          pos = Math.min(full.length, pos + (Math.random() < 0.5 ? 1 : 2));
          if (target.node.nodeType === 3) target.node.nodeValue = full.slice(0, pos); else target.node.textContent = full.slice(0, pos);
          if (pos % 12 === 0) scrollDown();
          await sleep(rand(8, 16));
        }
      }
      scrollDown();
    }
    function cell(tag, v) {
      var c = el(tag);
      if (Array.isArray(v)) c.appendChild(el('code', null, v[1]));
      else if (v && typeof v === 'object') { c.className = 'ok'; c.textContent = v.ok; }
      else c.textContent = v;
      return c;
    }
    async function ai(step) {
      clearThinking();
      var msg = el('div', 'msg msg-ai is-enter');
      msg.appendChild(aiMeta());
      var content = el('div', 'msg-content');
      msg.appendChild(content);
      body.appendChild(msg);
      scrollDown();
      for (var i = 0; i < step.blocks.length; i++) {
        var b = step.blocks[i];
        if (b.p) {
          var p = el('p');
          content.appendChild(p);
          await stream(p, b.p);
        } else if (b.table) {
          var table = el('table', 'mini-table');
          var thead = el('thead');
          var hr = el('tr');
          b.table.head.forEach(function (h) { hr.appendChild(cell('th', h)); });
          thead.appendChild(hr);
          table.appendChild(thead);
          var tbody = el('tbody');
          table.appendChild(tbody);
          content.appendChild(table);
          for (var r = 0; r < b.table.rows.length; r++) {
            var tr = el('tr', 'is-enter');
            b.table.rows[r].forEach(function (v) { tr.appendChild(cell('td', v)); });
            tbody.appendChild(tr);
            scrollDown();
            await sleep(170);
          }
        }
        await sleep(150);
      }
    }

    function pre(cls, lines) {
      var p = el('pre', cls);
      lines.forEach(function (l) {
        if (typeof l === 'string') p.appendChild(doc.createTextNode(l));
        else p.appendChild(el('span', l[0], l[1]));
      });
      return p;
    }
    async function approve() {
      clearThinking();
      var group = el('div', 'tool-group is-enter');
      var head = el('div', 'tool-head');
      var label = el('span');
      label.appendChild(doc.createTextNode('工具调用 '));
      label.appendChild(el('b', null, '1'));
      head.appendChild(label);
      var note = el('span', 'tool-head-note is-pending', '1 项待批准');
      head.appendChild(note);
      group.appendChild(head);

      var card = el('div', 'approve');
      var ah = el('div', 'approve-head');
      ah.appendChild(el('code', null, 'api'));
      ah.appendChild(el('span', 'op-tag', 'PATCH'));
      var state = el('span', 'state-tag', '等待批准');
      ah.appendChild(state);
      card.appendChild(ah);
      card.appendChild(el('div', 'approve-meta', '集群 ' + CLUSTER + ' · 命名空间 ' + NAMESPACE + ' · 来源 agent'));
      card.appendChild(el('div', 'sec-label', '准确参数'));
      // 路径较长：在 “/” 后放零宽空格，窄屏时只在路径分隔处换行
      card.appendChild(pre('params', ['path: /apis/apps/v1/namespaces/' + NAMESPACE + '/\u200bdeployments/\u200b' + DEPLOYMENT + '\ncontent_type: application/json-patch+json']));
      var dl = el('div', 'sec-label', '配置差异 ');
      dl.appendChild(el('span', 'tag-mini', '服务端 dryRun=All'));
      card.appendChild(dl);
      card.appendChild(pre('diff', [['d-hd', '--- current'], ['d-hd', '+++ proposed'], ['d-ctx', '   "limits": {'], ['d-ctx', '     "cpu": "500m",'], ['d-del', '-    "memory": "512Mi"'], ['d-add', '+    "memory": "1Gi"']]));
      var foot = el('div', 'approve-foot');
      foot.appendChild(el('span', 'approve-hint', '批准后执行以上动作'));
      foot.appendChild(el('span', 'a-btn', '拒绝'));
      var ok = el('span', 'a-btn a-btn-ok', '批准这次操作');
      foot.appendChild(ok);
      card.appendChild(foot);
      group.appendChild(card);
      body.appendChild(group);
      scrollDown();

      if (stopAt === 'approval') {
        // 测试模式：停在待批准状态
        await new Promise(function () {});
      }
      await sleep(1800);
      ok.classList.add('is-pressed');
      await sleep(260);
      ok.classList.remove('is-pressed');
      card.classList.add('is-running');
      state.textContent = '执行中';
      note.className = 'tool-head-note';
      note.textContent = '已批准 · 执行中';
      foot.innerHTML = '';
      foot.appendChild(el('span', 'approve-hint', 'resourceVersion / uid 已冻结 · 执行前已复核'));
      var running = el('span', 'approve-done');
      running.style.color = '#a5f3fc';
      running.appendChild(doc.createTextNode('按冻结参数执行中…'));
      foot.appendChild(running);
      await sleep(1000);
      card.classList.remove('is-running');
      card.classList.add('is-approved');
      state.textContent = '已完成';
      note.textContent = '已完成';
      running.style.color = '';
      running.textContent = '';
      running.appendChild(icon('i-check'));
      running.appendChild(doc.createTextNode('已批准 · 按冻结参数执行'));
      scrollDown();
      await sleep(700);
    }

    async function run() {
      // 首轮先展示页面里自带的静态对话（已定位到审批卡），让访客一进来就看到“审批 + 差异”，再开始逐步演示
      if (!fast) {
        showStatic();
        await sleep(3500);
        body.classList.add('is-fading');
        await sleep(600);
      }
      // eslint-disable-next-line no-constant-condition
      while (true) {
        body.innerHTML = '';
        body.scrollTop = 0;
        body.classList.remove('is-fading');
        body.appendChild(emptyState());
        for (var i = 0; i < SCRIPT.length; i++) {
          var step = SCRIPT[i];
          if (step.type === 'type') await typeText(step.text);
          else if (step.type === 'send') await send();
          else if (step.type === 'think') await think(step.ms);
          else if (step.type === 'tools') await tools(step);
          else if (step.type === 'ai') await ai(step);
          else if (step.type === 'approve') await approve();
          else if (step.type === 'pause') await sleep(step.ms);
        }
        if (fast) return;
        body.classList.add('is-fading');
        await sleep(600);
      }
    }

    var offscreen = false;
    function syncPause() { setPaused(offscreen || doc.hidden); }
    if (hasIO) {
      new IntersectionObserver(function (entries) {
        offscreen = !entries[0].isIntersecting;
        syncPause();
      }, { threshold: 0.1 }).observe(box);
    }
    doc.addEventListener('visibilitychange', syncPause);

    body.classList.add('is-animating');
    run().catch(function () { /* demo is decorative */ });
  })();
})();
