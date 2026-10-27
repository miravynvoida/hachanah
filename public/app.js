(() => {
  const tg = window.Telegram?.WebApp;
  if (tg) { tg.ready(); tg.expand(); }

  const state = {
    me: null, page: 'home', items: [], orders: [], activeOrder: null, staff: [],
    stops: [], audit: [], categories: {}, query: '', section: 'Все', stopOnly: false,
    modalItem: null, modal: null, busy: false
  };
  const app = document.getElementById('app');
  const toastEl = document.getElementById('toast');
  let toastTimer;

  const esc = (s='') => String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const roleName = r => ({waiter:'Официант',trainee:'Стажёр',manager:'Управляющий',admin:'Администратор'}[r] || r || 'Сотрудник');
  const timeText = d => {
    if (!d) return '—';
    const x = new Date(d); return Number.isNaN(x.getTime()) ? d : x.toLocaleString('ru-RU',{day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'});
  };
  const since = d => {
    if (!d) return '';
    const ms = Math.max(0, Date.now() - new Date(d).getTime());
    const mins = Math.floor(ms/60000);
    if (mins < 60) return `${mins} мин.`;
    return `${Math.floor(mins/60)} ч ${mins%60} мин.`;
  };
  function showToast(message) {
    toastEl.textContent = message; toastEl.classList.add('show');
    clearTimeout(toastTimer); toastTimer=setTimeout(()=>toastEl.classList.remove('show'),2600);
    if (tg?.HapticFeedback) tg.HapticFeedback.notificationOccurred('success');
  }
  async function api(path, options={}) {
    const headers = {'Content-Type':'application/json', ...(options.headers||{})};
    if (tg?.initData) headers['X-Telegram-Init-Data'] = tg.initData;
    const response = await fetch(path, {...options, headers});
    let data={}; try { data=await response.json(); } catch {}
    if (!response.ok) {
      const err = new Error(data.detail || `HTTP ${response.status}`); err.status=response.status; throw err;
    }
    return data;
  }
  function roleHint(err) {
    if (err?.message === 'no_active_shift') return 'Сначала начни смену.';
    if (err?.message === 'item_is_stopped') return 'Это блюдо сейчас в стоп-листе.';
    if (err?.message === 'manager_required') return 'Только управляющий или администратор может управлять сотрудниками.';
    if (err?.message === 'approval_pending') return 'Доступ ещё не подтверждён менеджером.';
    if (err?.message === 'telegram_auth_required') return 'Открой приложение через кнопку в Telegram.';
    if (err?.message === 'order_forbidden') return 'Нет доступа к этому заказу.';
    if (err?.message === 'item_not_found') return 'Позиция не найдена. Обнови меню.';
    if (err?.message === 'not_found') return 'Не найдено.';
    return err?.message || 'Не удалось выполнить действие.';
  }
  function topbar() {
    const initials = `${(state.me?.display_name||state.me?.first_name||'')[0]||''}${state.me?.last_name?.[0]||''}`.toUpperCase() || '✦';
    return `<header class="topbar"><div class="brand"><div class="brand-mark">✦</div><span>Помощник официанта</span></div><button class="avatar" data-action="profile" aria-label="Профиль">${esc(initials)}</button></header>`;
  }
  function shell(content) {
    const nav=[['home','⌂','Главная'],['menu','▤','Меню'],['stops','⊘','Стоп-лист'],['orders','▣','Заказы'],['profile','♙','Профиль']];
    app.innerHTML=`<div class="app-wrap">${topbar()}${content}</div><nav class="bottom-nav"><div class="nav-inner">${nav.map(([id,ico,label])=>`<button class="nav-item ${state.page===id?'active':''}" data-page="${id}"><span class="nav-icon">${ico}</span><span>${label}</span></button>`).join('')}</div></nav>${state.modal||''}`;
    app.querySelectorAll('[data-page]').forEach(b=>b.addEventListener('click',()=>go(b.dataset.page)));
    app.querySelectorAll('[data-action="profile"]').forEach(b=>b.addEventListener('click',()=>go('profile')));
    wireContent();
  }
  function noAccess(message, subtitle) {
    app.innerHTML=`<div class="center-state"><div class="auth-box"><div class="brand"><div class="brand-mark">✦</div><span>Помощник официанта</span></div><h1>${esc(message)}</h1><p>${esc(subtitle)}</p><button class="btn btn-primary btn-full" id="reload">Проверить снова</button></div></div>`;
    document.getElementById('reload')?.addEventListener('click',boot);
  }
  async function refreshCore() {
    const [menu, stops, orders, categories] = await Promise.all([
      api('/api/menu'), api('/api/stops'), api('/api/orders'), api('/api/categories')
    ]);
    state.items=menu.items||[]; state.stops=stops.items||[]; state.orders=orders.orders||[]; state.categories=categories.sections||{};
  }
  async function syncAvailability() {
    if (!state.me || document.hidden) return;
    try {
      const response = await api('/api/availability');
      const latest = new Map((response.items||[]).map(item => [item.id, item]));
      state.items = state.items.map(item => latest.has(item.id) ? {...item, ...latest.get(item.id)} : item);
      state.stops = state.items.filter(item => item.stock_status === 'stop');
      if (!state.modal && (state.page === 'home' || state.page === 'stops' || (state.page === 'menu' && document.activeElement?.id !== 'menu-search'))) render();
    } catch (err) { if (err.status===401 || err.status===403) return; console.debug('Availability refresh skipped:', err.message); }
  }
  async function boot() {
    app.innerHTML='<div class="center-state"><div class="spinner"></div><div>Загружаем помощника…</div></div>';
    if (!tg || !tg.initData) {
      noAccess('Открой в Telegram','Запусти бота и нажми «Открыть помощника». Так Telegram подтвердит твою учётную запись.'); return;
    }
    try {
      const me=await api('/api/me'); state.me=me;
      await refreshCore();
      if (state.activeOrder) {
        try { state.activeOrder=(await api(`/api/orders/${state.activeOrder.id}`)).order; } catch { state.activeOrder=null; }
      }
      render();
    } catch(err) {
      if (err.message==='approval_pending' || err.status===403) noAccess('Доступ ожидает подтверждения','Попроси менеджера одобрить заявку. После этого нажми /start в боте и открой приложение снова.');
      else if (err.status===401) noAccess('Не удалось проверить Telegram','Закрой Mini App и открой его заново через кнопку бота.');
      else noAccess('Не удалось загрузить приложение',roleHint(err));
    }
  }
  async function go(page) {
    state.page=page;
    if (page==='orders') { try { const r=await api('/api/orders'); state.orders=r.orders||[]; } catch(e){ showToast(roleHint(e)); } }
    if (page==='stops') { try { const r=await api('/api/stops'); state.stops=r.items||[]; } catch(e){ showToast(roleHint(e)); } }
    if (page==='menu') { state.query=''; state.section='Все'; state.stopOnly=false; }
    render();
  }
  function render() {
    if (!state.me) return;
    let content='';
    if (state.page==='home') content=renderHome();
    else if (state.page==='menu') content=renderMenu();
    else if (state.page==='stops') content=renderStops();
    else if (state.page==='orders') content=renderOrders();
    else content=renderProfile();
    shell(content);
  }
  function renderHome() {
    const active=state.me.active_shift;
    return `
      <section class="hero">
        <div class="hero-row"><div><div class="eyebrow">Рабочая панель · Большая Покровская, 49</div><h1>Доброй смены,<br>${esc(state.me.display_name||state.me.first_name||'коллега')}!</h1><p>Меню, наличие блюд и черновики заказов — в одном месте.</p></div><div style="font-size:46px;opacity:.8">✦</div></div>
        <div class="shift-status"><span class="dot" style="background:${active?'#299b5f':'#b68a36'}"></span>${active?`Смена активна · ${since(active.started_at)}`:'Смена ещё не началась'}</div>
      </section>
      <section class="stats"><div class="stat"><strong>${state.stops.length}</strong><span>Позиций в стоп-листе</span></div><div class="stat"><strong>${state.items.length}</strong><span>Позиций в каталоге</span></div><div class="stat"><strong>${state.orders.filter(o=>o.status==='draft').length}</strong><span>Черновиков заказов</span></div></section>
      <div class="section-heading"><h2>Быстрые действия</h2><small>${active?'Можно работать':'Начни смену'}</small></div>
      ${!active?`<div class="card card-pad" style="margin-bottom:13px"><div class="row"><div class="shift-icon">◷</div><div class="stack" style="flex:1"><strong>Отметь начало смены</strong><span class="muted small">Изменения стоп-листа и заказы доступны во время активной смены.</span></div></div><button class="btn btn-primary btn-full" data-action="start-shift" style="margin-top:14px">Начать смену</button></div>`:`<div class="card shift-card" style="margin-bottom:13px"><div class="row"><div class="shift-icon">✓</div><div class="stack"><strong>Смена началась</strong><span class="muted small">Начало: ${timeText(active.started_at)}</span></div></div><button class="btn btn-ghost btn-small" data-action="finish-shift">Завершить</button></div>`}
      <div class="grid-tiles">
        <button class="tile tile-red" data-page="stops"><span class="tile-icon">⊘</span><strong>Стоп-лист</strong><small>${state.stops.length?`${state.stops.length} позиций недоступно`:'Все позиции доступны'}</small><span class="tile-arrow">↗</span></button>
        <button class="tile tile-green" data-page="menu"><span class="tile-icon">▤</span><strong>Меню</strong><small>Состав, описание, аллергены</small><span class="tile-arrow">↗</span></button>
        <button class="tile tile-cream" data-action="new-order"><span class="tile-icon">▣</span><strong>Новый заказ</strong><small>Выбрать стол и позиции</small><span class="tile-arrow">↗</span></button>
        <button class="tile tile-blue" data-page="profile"><span class="tile-icon">◷</span><strong>Моя смена</strong><small>Профиль и история</small><span class="tile-arrow">↗</span></button>
      </div>
      <div class="notice" style="margin-top:16px">Заказ сохраняется как внутренний черновик для официанта. Он не отправляется на кухню и не заменяет кассовую систему.</div>`;
  }
  function renderMenu() {
    const sections=['Все',...new Set(state.items.map(i=>i.section))];
    const shown=filteredItems();
    return `<div class="row-between"><div><div class="eyebrow">Справочник ресторана</div><h1 class="page-title">Меню</h1><p class="subtitle">Найди позицию и быстро проверь состав.</p></div><span class="badge badge-ok">${state.items.length} поз.</span></div>
      <label class="search-box"><span class="search-symbol">⌕</span><input id="menu-search" value="${esc(state.query)}" placeholder="Блюдо, ингредиент, описание…" autocomplete="off"><button class="icon-btn" id="clear-search" aria-label="Очистить" style="width:28px;height:28px">×</button></label>
      <div class="chips">${sections.map(s=>`<button class="chip ${state.section===s?'active':''}" data-section="${esc(s)}">${esc(s)}</button>`).join('')}</div>
      <div class="row-between" style="margin:5px 0 11px"><span class="muted small">${shown.length} позиций</span><button class="chip ${state.stopOnly?'active':''}" data-action="toggle-stop-filter">${state.stopOnly?'Показать всё':'Только стоп'}</button></div>
      <div class="menu-list ${shown.length>8?'grid':''}" id="menu-list">${shown.length?shown.map(menuCard).join(''):empty('Ничего не найдено','Попробуй другое название или ингредиент.','⌕')}</div>`;
  }
  function filteredItems() {
    const q=state.query.trim().toLocaleLowerCase('ru');
    return state.items.filter(i=>{
      const fitsSection=state.section==='Все'||i.section===state.section;
      const fitsStop=!state.stopOnly||i.stock_status==='stop';
      const hay=[i.name,i.category,i.section,i.composition,i.description,i.extra_info,i.wow,i.speech].join(' ').toLocaleLowerCase('ru');
      return fitsSection&&fitsStop&&(!q||hay.includes(q));
    });
  }
  function imageMarkup(item, cls='thumb') {
    if (item.image) return `<img class="${cls}" src="${esc(item.image)}" loading="lazy" alt="${esc(item.name)}">`;
    return `<div class="${cls} thumb-placeholder">${item.section==='Бар'?'☕':item.section==='Детское меню'?'☺':'✦'}</div>`;
  }
  function menuCard(item) {
    return `<article class="card menu-card" data-item="${item.id}">${imageMarkup(item)}<div class="menu-card-main"><div class="row-between" style="align-items:flex-start"><div style="min-width:0"><h3 class="item-title">${esc(item.name)}</h3><div class="item-category">${esc(item.category)}</div></div>${item.stock_status==='stop'?'<span class="badge badge-stop">В стопе</span>':''}</div><div class="item-description">${esc(item.description||item.composition||item.extra_info||'Открой карточку для подробностей.')}</div>${item.price!=null?`<div class="item-price">${Number(item.price).toLocaleString('ru-RU')} ₽</div>`:''}<div class="card-actions"><button class="btn btn-ghost btn-small" data-action="details" data-id="${item.id}">Подробнее</button>${item.stock_status==='stop'?`<button class="btn btn-secondary btn-small" data-action="restore" data-id="${item.id}">Вернуть</button>`:`<button class="btn btn-danger btn-small" data-action="stop" data-id="${item.id}">В стоп</button><button class="btn btn-primary btn-small" data-action="add-order" data-id="${item.id}">+ В заказ</button>`}</div></div></article>`;
  }
  function renderStops() {
    return `<div class="eyebrow">Актуальное наличие</div><h1 class="page-title">Стоп-лист</h1><p class="subtitle">Недоступные позиции и история изменений.</p>
      <div class="row-between" style="margin:18px 0 12px"><span class="badge badge-stop">${state.stops.length} ${plural(state.stops.length,'позиция','позиции','позиций')} в стопе</span><button class="btn btn-ghost btn-small" data-action="show-audit">История</button></div>
      ${state.stops.length?`<div class="menu-list">${state.stops.map(item=>`<article class="card menu-card">${imageMarkup(item)}<div class="menu-card-main"><h3 class="item-title">${esc(item.name)}</h3><div class="item-category">${esc(item.category)}</div>${item.stop_reason?`<div class="item-description">Причина: ${esc(item.stop_reason)}</div>`:''}<div class="item-category">Изменено: ${timeText(item.updated_at)}${item.updated_by_name?' · '+esc(item.updated_by_name):''}</div><div class="card-actions"><button class="btn btn-secondary btn-small" data-action="details" data-id="${item.id}">Подробнее</button><button class="btn btn-primary btn-small" data-action="restore" data-id="${item.id}">Вернуть в меню</button></div></div></article>`).join('')}</div>`:empty('Стоп-лист пуст','Сейчас все позиции каталога доступны.','✓')}
      <div id="audit-panel"></div>`;
  }
  function plural(n, one, few, many) { const x=n%100, y=n%10; if(x>10&&x<20)return many;if(y>1&&y<5)return few;if(y===1)return one;return many; }
  function renderOrders() {
    if (state.activeOrder) return renderOrderDetail(state.activeOrder);
    return `<div class="eyebrow">Внутренний блокнот</div><div class="row-between"><div><h1 class="page-title">Заказы</h1><p class="subtitle">Черновики по столам — не отправляются на кухню.</p></div><button class="btn btn-primary" data-action="new-order">+ Заказ</button></div>
      <div class="notice" style="margin:16px 0">Цены внесены по актуальному списку; если у позиции нет подтверждённой цены, она не включается в итог.</div>
      ${state.orders.length?`<div class="menu-list">${state.orders.map(o=>`<button class="card card-pad" data-action="open-order" data-id="${o.id}" style="text-align:left;color:var(--text);width:100%"><div class="row-between"><div class="stack"><strong>Стол ${esc(o.table_number)}</strong><span class="muted small">${o.items_count||0} ${plural(o.items_count||0,'позиция','позиции','позиций')} · ${timeText(o.updated_at)}</span></div><span class="badge ${o.status==='saved'?'badge-ok':'badge-amber'}">${o.status==='saved'?'Сохранён':'Черновик'}</span></div></button>`).join('')}</div>`:empty('Заказов пока нет','Создай черновик, выбери стол и добавь блюда.','▣')}`;
  }
  function renderOrderDetail(order) {
    const lines=order.lines||[];
    return `<button class="btn btn-ghost btn-small" data-action="back-orders">← Все заказы</button><div class="row-between" style="margin-top:16px"><div><div class="eyebrow">Черновик официанта</div><h1 class="page-title">Стол ${esc(order.table_number)}</h1></div><span class="badge ${order.status==='saved'?'badge-ok':'badge-amber'}">${order.status==='saved'?'Сохранён':'Черновик'}</span></div>
      ${lines.length?`<div class="card" style="margin-top:16px">${lines.map(line=>`<div class="order-line"><div class="stack" style="flex:1"><strong>${esc(line.item_name)}</strong><span class="muted small">${esc(line.category||'')}</span>${line.note?`<span class="small">Комментарий: ${esc(line.note)}</span>`:''}<div class="row" style="margin-top:8px"><div class="qty-control"><button data-action="qty-minus" data-line="${line.id}" aria-label="Уменьшить">−</button><span>${line.quantity}</span><button data-action="qty-plus" data-line="${line.id}" aria-label="Увеличить">+</button></div><button class="btn btn-ghost btn-small" data-action="line-note" data-line="${line.id}">Комментарий</button></div></div><button class="icon-btn" data-action="delete-line" data-line="${line.id}" aria-label="Удалить позицию">×</button></div>`).join('')}</div>`:empty('В заказе пока пусто','Добавь позиции из меню.','＋')}
      <button class="btn btn-secondary btn-full" style="margin-top:13px" data-action="add-more">＋ Добавить блюда из меню</button>
      <div class="order-summary row-between"><div class="stack"><strong>${lines.reduce((n,l)=>n+l.quantity,0)} ${plural(lines.reduce((n,l)=>n+l.quantity,0),'позиция','позиции','позиций')}</strong><span class="small">${order.total==null?'Есть позиции без цены':Number(order.total).toLocaleString('ru-RU')+' ₽'}</span></div><span style="font-size:20px">▣</span></div>
      <div class="field"><label for="order-comment">Комментарий к заказу</label><textarea id="order-comment" placeholder="Например: подать позже, без лука…">${esc(order.comment||'')}</textarea></div>
      <button class="btn btn-primary btn-full" data-action="save-order">✓ Сохранить черновик</button><button class="btn btn-ghost btn-full" style="margin-top:9px" data-action="delete-order">Удалить заказ</button>
      <p class="muted small" style="text-align:center;margin-top:12px">Сохранение не отправляет заказ в кассу или на кухню.</p>`;
  }
  function renderProfile() {
    const active=state.me.active_shift;
    return `<div class="eyebrow">Учётная запись</div><h1 class="page-title">Профиль</h1><div class="card card-pad" style="margin-top:16px"><div class="row"><div class="avatar" style="width:52px;height:52px;font-size:19px">${esc((state.me.first_name||'')[0]||'✦')}</div><div class="stack" style="flex:1"><strong>${esc(state.me.display_name||[state.me.first_name,state.me.last_name].filter(Boolean).join(' ')||'Сотрудник')}</strong><span class="muted small">${esc(roleName(state.me.role))}</span><span class="muted small">Telegram ID: ${state.me.telegram_id}</span></div></div></div>
      <div class="section-heading"><h2>Моя смена</h2></div><div class="card card-pad"><div class="row"><div class="shift-icon">${active?'✓':'◷'}</div><div class="stack" style="flex:1"><strong>${active?'Смена активна':'Сейчас нет активной смены'}</strong><span class="muted small">${active?`Начата ${timeText(active.started_at)}`:'Начинай смену перед изменением наличия и созданием заказов.'}</span></div></div><button class="btn ${active?'btn-danger':'btn-primary'} btn-full" data-action="${active?'finish-shift':'start-shift'}" style="margin-top:14px">${active?'Завершить смену':'Начать смену'}</button></div>
      ${state.me.role==='manager'||state.me.role==='admin'?`<div class="section-heading"><h2>Управление командой</h2></div><div id="staff-management" class="card card-pad"><div class="loading">Загружаем сотрудников…</div></div>`:''}
      <div class="section-heading"><h2>История смен</h2></div><div id="shift-history" class="card card-pad"><div class="loading">Загружаем…</div></div>
      <div class="section-heading"><h2>О приложении</h2></div><div class="card card-pad"><div class="stack"><strong>Помощник официанта</strong><span class="muted small">Первая версия · Telegram Mini App</span><span class="muted small">Ресторан: Нижний Новгород, Большая Покровская, 49</span></div></div>`;
  }
  function empty(title, desc, icon='✦') { return `<div class="empty"><div class="empty-icon">${icon}</div><strong>${esc(title)}</strong><span class="small">${esc(desc)}</span></div>`; }
  function showModal(html) {
    state.modal=`<div class="overlay" id="overlay"><div class="modal">${html}</div></div>`;
    // Re-render around modal without losing current page.
    const old=app.querySelector('.overlay');
    if(old) old.remove();
    app.insertAdjacentHTML('beforeend', state.modal);
    wireModal();
  }
  function hideModal() { app.querySelector('.overlay')?.remove(); state.modal=null; }
  function showItemDetails(item) {
    if(!item) return;
    const price=item.price==null?'Цена не указана':`${Number(item.price).toLocaleString('ru-RU')} ₽`;
    showModal(`<div class="modal-top"><div><div class="eyebrow">${esc(item.section)}</div><h2 class="page-title" style="font-size:23px;margin-top:6px">${esc(item.name)}</h2><div class="item-category">${esc(item.category)}</div></div><button class="icon-btn" data-action="close-modal">×</button></div>
      ${item.image?`<img class="detail-image" src="${esc(item.image)}" alt="${esc(item.name)}">`:''}
      ${item.description?`<section class="detail-section"><h3>Описание для гостя</h3><p>${esc(item.description)}</p></section>`:''}
      ${item.composition?`<section class="detail-section"><h3>Состав</h3><p>${esc(item.composition)}</p></section>`:''}
      ${item.extra_info?`<section class="detail-section"><h3>Дополнительная информация и ограничения</h3><p>${esc(item.extra_info)}</p></section>`:''}
      ${item.wow?`<section class="detail-section"><h3>Особенности подачи</h3><p>${esc(item.wow)}</p></section>`:''}
      ${item.speech?`<section class="detail-section"><h3>Речевой модуль</h3><p>${esc(item.speech)}</p></section>`:''}
      <section class="detail-section"><h3>Цена</h3><p class="muted">${esc(price)}</p></section>
      <div class="row" style="margin-top:14px">${item.stock_status==='stop'?`<button class="btn btn-primary" style="flex:1" data-action="restore" data-id="${item.id}">Вернуть в меню</button>`:`<button class="btn btn-danger" style="flex:1" data-action="stop" data-id="${item.id}">В стоп-лист</button><button class="btn btn-primary" style="flex:1" data-action="add-order" data-id="${item.id}">＋ В заказ</button>`}</div>`);
  }
  function showCreateOrderModal(pendingItemId=null) {
    const lunchItems=state.items.filter(x=>(x.section||'').toLowerCase().includes('ланч'));
    const opts=(items,label)=>`<option value="">${label}</option><option value="skip">Пропустить</option>`+[...new Map(items.map(x=>[x.id,x])).values()].map(x=>`<option value="${esc(x.id)}">${esc(x.name)}</option>`).join('');
    const forKind=(week,kind)=>lunchItems.filter(x=>{const c=(x.category||'').toLowerCase();const wm=c.includes(`неделя ${week}`)||((week==='1'||week==='3')&&c.includes('1 и 3 неделя'))||((week==='2'||week==='4')&&c.includes('2 и 4 неделя'))||c==='бизнес-ланч';const km=kind==='salad'?c.includes('салат'):kind==='soup'?c.includes('суп'):(c.includes('горяч')||c.includes('1 и 3 неделя')||c.includes('2 и 4 неделя')||c==='бизнес-ланч');return wm&&km;});
    showModal(`<div class="modal-top"><div><div class="eyebrow">Новая запись</div><h2 class="page-title" style="font-size:23px">Новый заказ</h2><p class="subtitle">Черновик официанта — не отправляется на кухню.</p></div><button class="icon-btn" data-action="close-modal">×</button></div>
      <form id="new-order-form"><div class="field"><label for="table-number">Номер стола</label><input id="table-number" name="table_number" placeholder="Например, 12" maxlength="30" required autocomplete="off"></div>
      <div class="field"><label for="lunch-type">Тип заказа</label><select id="lunch-type" name="lunch_type"><option value="business">Бизнес-ланч обычный</option><option value="hit">Ланч-хит</option><option value="none">Обычный заказ (без ланча)</option></select></div>
      <div id="lunch-fields"><div class="field"><label for="lunch-week">Неделя меню</label><select id="lunch-week" name="lunch_week"><option value="1">Первая неделя</option><option value="2">Вторая неделя</option><option value="3">Третья неделя</option><option value="4">Четвёртая неделя</option></select></div>
      <div class="field"><label for="lunch-count">Количество блюд</label><select id="lunch-count" name="lunch_count"><option value="2">2 блюда — 350 ₽ / 490 ₽</option><option value="3">3 блюда — 380 ₽ / 520 ₽</option></select></div>
      <div class="field"><label for="lunch-salad">Салат</label><select id="lunch-salad" name="lunch_salad"></select></div><div class="field"><label for="lunch-soup">Суп</label><select id="lunch-soup" name="lunch_soup"></select></div><div class="field"><label for="lunch-hot">Горячее</label><select id="lunch-hot" name="lunch_hot"></select></div>
      <div class="field"><label for="lunch-drink">Напиток</label><select id="lunch-drink" name="lunch_drink"></select></div><p class="muted small" id="lunch-note">Шот эспрессо добавляется автоматически и входит в стоимость ланча.</p></div>
      <button class="btn btn-primary btn-full" type="submit">Создать заказ</button></form><input type="hidden" id="pending-item-id" value="${esc(pendingItemId||'')}">`);
    const ov=app.querySelector('#overlay');
    const fill=()=>{const w=ov.querySelector('#lunch-week').value;for(const [f,k,l] of [['lunch-salad','salad','Выберите салат'],['lunch-soup','soup','Выберите суп'],['lunch-hot','hot','Выберите горячее']])ov.querySelector('#'+f).innerHTML=opts(forKind(w,k),l+' или пропустите');};
    const drinks=()=>{const hit=ov.querySelector('#lunch-type').value==='hit';const list=hit?['Морс','Чай чёрный','Чай зелёный','Американо','Капучино','Белое сухое вино','Белое полусладкое вино','Красное сухое вино','Красное полусладкое вино']:['Морс','Чай чёрный','Чай зелёный'];ov.querySelector('#lunch-drink').innerHTML='<option value="">Напиток не выбран</option>'+list.map(x=>`<option>${x}</option>`).join('');ov.querySelector('#lunch-note').textContent=hit?'Ланч-хит: напиток на выбор. Шот эспрессо входит в стоимость.':'Обычный ланч: морс, чёрный или зелёный чай. Шот эспрессо входит в стоимость.';};
    ov.querySelector('#lunch-week').addEventListener('change',fill);ov.querySelector('#lunch-type').addEventListener('change',()=>{const show=ov.querySelector('#lunch-type').value!=='none';ov.querySelector('#lunch-fields').hidden=!show;drinks();});fill();drinks();setTimeout(()=>document.getElementById('table-number')?.focus(),80);
  }
  function wireModal() {
    const overlay=app.querySelector('#overlay');
    overlay?.addEventListener('click',e=>{ if(e.target===overlay) hideModal(); });
    overlay?.querySelectorAll('[data-action]').forEach(b=>b.addEventListener('click',handleAction));
    overlay?.querySelector('#new-order-form')?.addEventListener('submit',async e=>{
      e.preventDefault(); const table=document.getElementById('table-number').value.trim();const form=e.currentTarget;const pending=document.getElementById('pending-item-id').value;const type=form.elements.lunch_type.value;if(!table)return;
      try{const r=await api('/api/orders',{method:'POST',body:JSON.stringify({table_number:table})});state.activeOrder=r.order;if(pending)await addToOrder(pending,1,'',false);
        if(type!=='none'){const count=Number(form.elements.lunch_count.value);const chosen=[form.elements.lunch_salad.value,form.elements.lunch_soup.value,form.elements.lunch_hot.value].filter(x=>x&&x!=='skip');if(chosen.length!==count){showToast(`Выбери ровно ${count} блюда из разных категорий`);return;}const week=form.elements.lunch_week.value;const price=type==='hit'?(count===2?490:520):(count===2?350:380);const drink=form.elements.lunch_drink.value;const espresso=state.items.find(x=>x.name.toLowerCase()==='эспрессо 30мл')||state.items.find(x=>x.name.toLowerCase().includes('эспрессо')&&!x.name.toLowerCase().includes('тоник'));const details=`${type==='hit'?'Ланч-хит':'Бизнес-ланч'} · неделя ${week} · ${count} блюда · пакет ${price} ₽${drink?' · напиток: '+drink:''} · шот эспрессо включён`;for(const itemId of chosen)await addToOrder(itemId,1,details,false);if(espresso)await addToOrder(espresso.id,1,'Включён в ланч — не считать отдельно',false);await api(`/api/orders/${state.activeOrder.id}/save`,{method:'POST',body:JSON.stringify({comment:details})});state.activeOrder=(await api(`/api/orders/${state.activeOrder.id}`)).order;}
        hideModal();await go('orders');showToast(`Создан заказ · стол ${table}`);
      }catch(err){showToast(roleHint(err));}
    });
  }
  function wireContent() {
    app.querySelectorAll('[data-action]').forEach(b=>b.addEventListener('click',handleAction));
    app.querySelectorAll('[data-page]').forEach(b=>b.addEventListener('click',()=>go(b.dataset.page)));
    app.querySelectorAll('[data-section]').forEach(b=>b.addEventListener('click',()=>{state.section=b.dataset.section;render();}));
    const search=app.querySelector('#menu-search');
    if(search) search.addEventListener('input',e=>{state.query=e.target.value;const el=app.querySelector('#menu-list');if(el){el.innerHTML=filteredItems().map(menuCard).join('')||empty('Ничего не найдено','Попробуй другое название или ингредиент.','⌕');el.querySelectorAll('[data-action]').forEach(b=>b.addEventListener('click',handleAction));}});
    app.querySelector('#clear-search')?.addEventListener('click',()=>{state.query='';render();});
    app.querySelectorAll('[data-item]').forEach(el=>el.addEventListener('click',e=>{if(e.target.closest('button'))return;const item=state.items.find(i=>i.id===el.dataset.item)||state.stops.find(i=>i.id===el.dataset.item);showItemDetails(item);}));
    if(state.page==='profile') { loadShiftHistory(); if(['manager','admin'].includes(state.me.role)) loadStaff(); }
  }
  async function handleAction(e) {
    const b=e.currentTarget || e.target.closest('[data-action]');
    if(!b) return;
    const action=b.dataset.action, id=b.dataset.id;
    if(action==='close-modal'){hideModal();return;}
    if(action==='start-shift'){await doShift('start');return;}
    if(action==='finish-shift'){await doShift('finish');return;}
    if(action==='details'){const item=state.items.find(i=>i.id===id)||state.stops.find(i=>i.id===id);showItemDetails(item);return;}
    if(action==='stop'){await stopItem(id);return;}
    if(action==='restore'){await restoreItem(id);return;}
    if(action==='toggle-stop-filter'){state.stopOnly=!state.stopOnly;render();return;}
    if(action==='show-audit'){await showAudit();return;}
    if(action==='new-order'){showCreateOrderModal();return;}
    if(action==='add-order'){await addOrCreateOrder(id);return;}
    if(action==='open-order'){try{state.activeOrder=(await api(`/api/orders/${id}`)).order;render();}catch(err){showToast(roleHint(err));}return;}
    if(action==='back-orders'){state.activeOrder=null;await go('orders');return;}
    if(action==='add-more'){state.page='menu';render();showToast('Выбери блюдо и нажми «+ В заказ».');return;}
    if(action==='save-order'){await saveOrder();return;}
    if(action==='delete-order'){await deleteOrder();return;}
    if(action==='qty-minus'||action==='qty-plus'){await changeQty(Number(b.dataset.line), action==='qty-plus'?1:-1);return;}
    if(action==='delete-line'){await deleteLine(Number(b.dataset.line));return;}
    if(action==='line-note'){await editLineNote(Number(b.dataset.line));return;}
    if(action==='staff-role'){await changeStaff(Number(b.dataset.id),b.dataset.role,'approved');return;}
    if(action==='staff-reject'){await changeStaff(Number(b.dataset.id),'waiter','rejected');return;}
  }
  async function doShift(which) {
    try {
      const result=await api(`/api/shifts/${which}`,{method:'POST',body:JSON.stringify({})});
      state.me.active_shift=which==='start'?result.shift:null;
      await refreshCore(); render();
      showToast(which==='start'?'Смена началась':'Смена завершена');
    } catch(err) { showToast(roleHint(err)); }
  }
  async function stopItem(id) {
    const item=state.items.find(i=>i.id===id)||state.stops.find(i=>i.id===id); if(!item)return;
    showModal(`<div class="modal-top"><div><div class="eyebrow">Изменение наличия</div><h2 class="page-title" style="font-size:22px">В стоп-лист?</h2><p class="subtitle">${esc(item.name)}</p></div><button class="icon-btn" data-action="close-modal">×</button></div><div class="field"><label for="stop-reason">Причина (необязательно)</label><input id="stop-reason" placeholder="Например, закончилось на кухне" maxlength="300"></div><button class="btn btn-danger btn-full" id="confirm-stop">Добавить в стоп-лист</button>`);
    app.querySelector('#confirm-stop')?.addEventListener('click',async()=>{
      const reason=app.querySelector('#stop-reason')?.value||'';
      try { await api(`/api/items/${id}/stop`,{method:'POST',body:JSON.stringify({reason})}); hideModal(); await refreshCore(); render(); showToast('Позиция добавлена в стоп-лист'); }
      catch(err){showToast(roleHint(err));}
    });
  }
  async function restoreItem(id) {
    try { await api(`/api/items/${id}/restore`,{method:'POST',body:JSON.stringify({})}); hideModal(); await refreshCore(); render(); showToast('Позиция снова доступна'); }
    catch(err){showToast(roleHint(err));}
  }
  async function addOrCreateOrder(itemId) {
    const item=state.items.find(i=>i.id===itemId); if(!item)return;
    if(item.stock_status==='stop'){showToast('Позиция в стоп-листе, добавить её нельзя.');return;}
    if(!state.me.active_shift){showToast('Сначала начни смену.');return;}
    if(!state.activeOrder){showCreateOrderModal(itemId);return;}
    await addToOrder(itemId,1,'',true);
  }
  async function addToOrder(itemId,qty=1,note='',showMessage=true) {
    if(!state.activeOrder){showToast('Сначала создай заказ по столу.');return;}
    try {
      const r=await api(`/api/orders/${state.activeOrder.id}/lines`,{method:'POST',body:JSON.stringify({item_id:itemId,quantity:qty,note})});
      state.activeOrder=r.order;
      if(showMessage){hideModal();showToast('Добавлено в заказ');if(state.page==='orders')render();else {state.page='orders';render();}}
    } catch(err){showToast(roleHint(err));}
  }
  async function changeQty(lineId,delta) {
    const line=state.activeOrder?.lines?.find(l=>l.id===lineId); if(!line)return;
    const quantity=line.quantity+delta;
    if(quantity<1){await deleteLine(lineId);return;}
    try {const r=await api(`/api/orders/${state.activeOrder.id}/lines/${lineId}`,{method:'PATCH',body:JSON.stringify({quantity,note:line.note||''})});state.activeOrder=r.order;render();}
    catch(err){showToast(roleHint(err));}
  }
  async function deleteLine(lineId) {
    if(!state.activeOrder)return;
    try {const r=await api(`/api/orders/${state.activeOrder.id}/lines/${lineId}`,{method:'DELETE'});state.activeOrder=r.order;render();showToast('Позиция удалена');}
    catch(err){showToast(roleHint(err));}
  }
  async function editLineNote(lineId) {
    const line=state.activeOrder?.lines?.find(l=>l.id===lineId); if(!line)return;
    showModal(`<div class="modal-top"><div><h2 class="page-title" style="font-size:22px">Комментарий к позиции</h2><p class="subtitle">${esc(line.item_name)}</p></div><button class="icon-btn" data-action="close-modal">×</button></div><form id="line-note-form"><div class="field"><label for="line-note">Заметка</label><textarea id="line-note" maxlength="300" placeholder="Например: без зелени">${esc(line.note||'')}</textarea></div><button class="btn btn-primary btn-full" type="submit">Сохранить заметку</button></form>`);
    app.querySelector('#line-note-form')?.addEventListener('submit',async e=>{e.preventDefault();try{const note=app.querySelector('#line-note').value;const r=await api(`/api/orders/${state.activeOrder.id}/lines/${lineId}`,{method:'PATCH',body:JSON.stringify({quantity:line.quantity,note})});state.activeOrder=r.order;hideModal();render();showToast('Комментарий сохранён');}catch(err){showToast(roleHint(err));}});
  }
  async function saveOrder() {
    if(!state.activeOrder)return;
    const comment=app.querySelector('#order-comment')?.value||'';
    try {const r=await api(`/api/orders/${state.activeOrder.id}/save`,{method:'POST',body:JSON.stringify({comment})});state.activeOrder=r.order;const l=await api('/api/orders');state.orders=l.orders||[];render();showToast('Черновик сохранён');}
    catch(err){showToast(roleHint(err));}
  }
  async function deleteOrder() {
    if(!state.activeOrder)return;
    showModal(`<div class="modal-top"><div><h2 class="page-title" style="font-size:22px">Удалить заказ?</h2><p class="subtitle">Стол ${esc(state.activeOrder.table_number)} · действие нельзя отменить.</p></div><button class="icon-btn" data-action="close-modal">×</button></div><div class="row"><button class="btn btn-ghost" style="flex:1" data-action="close-modal">Отмена</button><button class="btn btn-danger" style="flex:1" id="confirm-delete-order">Удалить</button></div>`);
    app.querySelector('#confirm-delete-order')?.addEventListener('click',async()=>{try{await api(`/api/orders/${state.activeOrder.id}/delete`,{method:'POST',body:JSON.stringify({})});state.activeOrder=null;hideModal();await go('orders');showToast('Заказ удалён');}catch(err){showToast(roleHint(err));}});
  }
  async function showAudit() {
    try {
      const r=await api('/api/audit?limit=60'); state.audit=r.events||[];
      const node=app.querySelector('#audit-panel'); if(!node)return;
      node.innerHTML=`<div class="section-heading"><h2>История изменений</h2><small>${state.audit.length} событий</small></div><div class="card card-pad">${state.audit.length?state.audit.map(a=>`<div class="history-row"><div class="stack"><strong>${a.action==='stop'?'Добавлено в стоп':'Возвращено в меню'} · ${esc(a.item_name)}</strong><span class="muted small">${esc(a.display_name||[a.first_name,a.last_name].filter(Boolean).join(' ')||('ID '+a.employee_id))}${a.reason?' · '+esc(a.reason):''}</span></div><span class="muted small">${timeText(a.created_at)}</span></div>`).join(''):'<span class="muted small">Изменений пока нет.</span>'}</div>`;
      node.scrollIntoView({behavior:'smooth',block:'start'});
    } catch(err){showToast(roleHint(err));}
  }
  async function loadShiftHistory() {
    try {
      const r=await api('/api/shifts/history?limit=30'); const node=app.querySelector('#shift-history'); if(!node)return;
      node.innerHTML=r.shifts?.length?r.shifts.map(s=>`<div class="history-row"><div class="stack"><strong>${esc(s.display_name||[s.first_name,s.last_name].filter(Boolean).join(' ')||'Сотрудник')} · ${s.status==='active'?'Смена активна':'Смена завершена'}</strong><span class="muted small">${timeText(s.started_at)}${s.ended_at?' — '+timeText(s.ended_at):''}</span></div><span class="badge ${s.status==='active'?'badge-ok':'badge-amber'}">${s.status==='active'?'Активна':'Закрыта'}</span></div>`).join(''):'<span class="muted small">Истории пока нет.</span>';
    } catch(err){const node=app.querySelector('#shift-history');if(node)node.innerHTML=`<span class="muted small">${esc(roleHint(err))}</span>`;}
  }
  async function loadStaff() {
    const node=app.querySelector('#staff-management'); if(!node)return;
    try {
      const r=await api('/api/staff'); state.staff=r.staff||[];
      const pending=state.staff.filter(x=>x.status==='pending');
      const approved=state.staff.filter(x=>x.status==='approved');
      const roleButtons=(s)=>`<div class="row" style="flex-wrap:wrap;margin-top:8px">${[['waiter','Официант'],['trainee','Стажёр'],['manager','Управляющий'],['admin','Администратор']].map(([role,label])=>`<button class="btn ${s.role===role?'btn-primary':'btn-ghost'} btn-small" data-action="staff-role" data-id="${s.telegram_id}" data-role="${role}">${label}</button>`).join('')}</div>`;
      node.innerHTML=`<div class="stack"><strong>Заявки на доступ · ${pending.length}</strong>${pending.length?pending.map(s=>`<div class="staff-row"><div class="row-between"><div class="stack"><strong>${esc(s.display_name||[s.first_name,s.last_name].filter(Boolean).join(' ')||'Без имени')}</strong><span class="muted small">ID ${s.telegram_id} · @${esc(s.username||'нет username')}</span></div><span class="badge badge-amber">Ожидает</span></div>${roleButtons(s)}<button class="btn btn-danger btn-small" data-action="staff-reject" data-id="${s.telegram_id}" style="margin-top:6px">Отклонить заявку</button></div>`).join(''):'<span class="muted small">Новых заявок нет.</span>'}<div class="divider" style="margin:12px 0"></div><strong>Сотрудники · ${approved.length}</strong>${approved.map(s=>`<div class="staff-row"><div class="row-between"><div class="stack"><strong>${esc(s.display_name||[s.first_name,s.last_name].filter(Boolean).join(' ')||'Без имени')}</strong><span class="muted small">${esc(roleName(s.role))} · ID ${s.telegram_id}</span></div><span class="badge badge-ok">Активен</span></div>${roleButtons(s)}<button class="btn btn-danger btn-small" data-action="staff-reject" data-id="${s.telegram_id}" style="margin-top:6px">Отключить доступ</button></div>`).join('')}</div>`;
      node.querySelectorAll('[data-action="staff-role"],[data-action="staff-reject"]').forEach(b=>b.addEventListener('click',handleAction));
    } catch(err) { node.innerHTML=`<span class="muted small">${esc(roleHint(err))}</span>`; }
  }
  async function changeStaff(id,role,status) {
    try { await api(`/api/staff/${id}`,{method:'PATCH',body:JSON.stringify({role,status})}); showToast(status==='approved'?'Данные сотрудника обновлены':'Доступ отключён'); await loadStaff(); }
    catch(err) { showToast(roleHint(err)); }
  }

  document.addEventListener('visibilitychange', () => { if (!document.hidden) syncAvailability(); });
  setInterval(syncAvailability, 15000);
  boot();
})();
