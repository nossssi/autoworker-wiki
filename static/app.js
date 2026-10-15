(() => {
  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => [...el.querySelectorAll(s)];
  const store = {
    get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch (e) {} },
  };

  // ── 다크 모드 ──
  $('[data-theme-toggle]')?.addEventListener('click', () => {
    const cur = document.documentElement.dataset.theme
      || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
    const next = cur === 'dark' ? 'light' : 'dark';
    document.documentElement.dataset.theme = next;
    store.set('theme', next);
  });

  // ── 모바일 메뉴 ──
  const closeMenu = () => document.body.classList.remove('menu-open');
  $('[data-menu]')?.addEventListener('click', () => document.body.classList.toggle('menu-open'));
  $('[data-scrim]')?.addEventListener('click', closeMenu);
  // 현재 페이지 메뉴 항목이 보이도록
  $('.nav a[aria-current="page"]')?.scrollIntoView({ block: 'center' });

  // ── 코드 복사 ──
  $$('.code').forEach((box) => {
    const btn = $('.copy', box);
    btn?.addEventListener('click', async () => {
      const text = $('pre code', box).innerText;
      try {
        await navigator.clipboard.writeText(text);
      } catch (e) {
        const ta = Object.assign(document.createElement('textarea'), { value: text });
        document.body.append(ta); ta.select(); document.execCommand('copy'); ta.remove();
      }
      btn.textContent = '복사됨 ✓'; btn.classList.add('done');
      setTimeout(() => { btn.textContent = '복사'; btn.classList.remove('done'); }, 1600);
    });
  });

  // ── 이미지 확대 ──
  document.addEventListener('click', (e) => {
    const img = e.target.closest('figure.img img');
    if (!img) return;
    const lb = document.createElement('div');
    lb.className = 'lightbox';
    lb.innerHTML = `<img src="${img.currentSrc || img.src}" alt="">`;
    lb.addEventListener('click', () => lb.remove());
    document.body.append(lb);
  });

  // ── 본문 안 목차 블록 채우기 ──
  $$('[data-toc]').forEach((nav) => {
    const hs = $$('.content h2[id], .content h3[id], .content h4[id]');
    nav.innerHTML = '<ul>' + hs.map((h) => `<li class="l${h.tagName[1]}"><a href="#${h.id}">${h.textContent}</a></li>`).join('') + '</ul>';
  });

  // ── 오른쪽 목차 현재 위치 ──
  const tocLinks = $$('.toc a');
  if (tocLinks.length && 'IntersectionObserver' in window) {
    const map = new Map(tocLinks.map((a) => [decodeURIComponent(a.hash.slice(1)), a]));
    const io = new IntersectionObserver((entries) => {
      entries.forEach((en) => {
        if (en.isIntersecting) {
          tocLinks.forEach((a) => a.classList.remove('active'));
          map.get(en.target.id)?.classList.add('active');
        }
      });
    }, { rootMargin: '-80px 0px -70% 0px' });
    map.forEach((_, id) => { const el = document.getElementById(id); if (el) io.observe(el); });
  }

  // ── 검색 ──
  const overlay = $('[data-search]');
  const input = $('[data-search-input]');
  const results = $('[data-search-results]');
  let index = null, sel = 0, items = [];

  const loadIndex = async () => {
    if (index) return index;
    const r = await fetch('/search-index.json');
    index = (await r.json()).map((d) => ({ ...d, _t: d.t.toLowerCase(), _x: d.x.toLowerCase() }));
    return index;
  };
  const escHtml = (s) => s.replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  const highlight = (text, terms) => {
    let out = escHtml(text);
    terms.forEach((t) => {
      if (!t) return;
      const re = new RegExp(t.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'gi');
      out = out.replace(re, (m) => `<mark>${m}</mark>`);
    });
    return out;
  };
  const snippet = (doc, terms) => {
    const i = Math.max(0, ...terms.map((t) => doc._x.indexOf(t)).filter((i) => i >= 0).slice(0, 1));
    const start = Math.max(0, i - 40);
    return (start ? '…' : '') + doc.x.slice(start, start + 140);
  };
  const run = async () => {
    const q = input.value.trim().toLowerCase();
    if (!q) { results.innerHTML = ''; items = []; return; }
    const terms = q.split(/\s+/).filter(Boolean);
    const docs = await loadIndex();
    const scored = [];
    for (const d of docs) {
      let score = 0, ok = true;
      for (const t of terms) {
        const inT = d._t.includes(t), inX = d._x.includes(t);
        if (!inT && !inX) { ok = false; break; }
        score += inT ? 10 : 1;
      }
      if (ok) scored.push([score, d]);
    }
    scored.sort((a, b) => b[0] - a[0]);
    items = scored.slice(0, 30).map(([, d]) => d);
    sel = 0;
    results.innerHTML = items.length
      ? items.map((d, i) => `<a class="sr${i === 0 ? ' sel' : ''}" href="${d.u}">
          <div class="sr-path">${escHtml([d.s, d.p].filter(Boolean).join(' › '))}</div>
          <div class="sr-title">${highlight(d.t, terms)}</div>
          <div class="sr-snip">${highlight(snippet(d, terms), terms)}</div></a>`).join('')
      : `<div class="sr-empty">"${escHtml(input.value)}" 검색 결과가 없어요.</div>`;
  };
  const open = () => {
    overlay.hidden = false; closeMenu();
    input.focus(); input.select(); loadIndex();
  };
  const close = () => { overlay.hidden = true; };
  $$('[data-search-open]').forEach((b) => b.addEventListener('click', open));
  overlay?.addEventListener('click', (e) => { if (e.target === overlay) close(); });
  input?.addEventListener('input', run);
  input?.addEventListener('keydown', (e) => {
    const links = $$('.sr', results);
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      if (!links.length) return;
      links[sel]?.classList.remove('sel');
      sel = (sel + (e.key === 'ArrowDown' ? 1 : -1) + links.length) % links.length;
      links[sel].classList.add('sel');
      links[sel].scrollIntoView({ block: 'nearest' });
    } else if (e.key === 'Enter' && links[sel]) {
      location.href = links[sel].href;
    }
  });
  document.addEventListener('keydown', (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') { e.preventDefault(); overlay.hidden ? open() : close(); }
    else if (e.key === 'Escape') { close(); closeMenu(); $('.lightbox')?.remove(); }
    else if (e.key === '/' && overlay.hidden && !/input|textarea/i.test(document.activeElement.tagName)) { e.preventDefault(); open(); }
  });
})();
