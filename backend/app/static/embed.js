/* 嵌入（iframe）模式补丁 —— 由外壳 shell.html 载入子页面时生效
 *
 * 作用：
 * 1. 隐藏子页面自带的左侧导航 / 顶部条，避免和外层菜单重复（原来的多页导航改为单壳多标签）
 * 2. 暴露 fxNav(url)：子页面里的跳转交给外层开新标签，而不是整页跳走
 *
 * 只有带 ?embed=1 时才生效，直接访问页面（不带参数）行为和以前完全一致。
 */
(function () {
  var embed = /[?&]embed=1(&|$)/.test(location.search);

  if (embed) {
    document.documentElement.classList.add('fx-embed');
    var css = [
      /* 左导航交给外壳，子页只留内容 */
      'html.fx-embed aside.sidebar{display:none!important}',
      'html.fx-embed body{display:block!important;min-height:100vh!important}',
      'html.fx-embed .main{min-height:100vh}',
      /* 顶部条（面包屑/版本/刷新）由外壳统一提供 */
      'html.fx-embed .main > .topbar{display:none!important}',
      /* 分销页内部的三个子标签，改由左侧二级菜单承载，避免两处重复 */
      'html.fx-embed .content > .tabs{display:none!important}',
      /* 内容区留白收紧一点，iframe 里更省空间 */
      'html.fx-embed .content{padding:16px 18px 32px!important}',
      'html.fx-embed .wrap{padding:16px 18px!important}'
    ].join('');
    var style = document.createElement('style');
    style.id = 'fx-embed-style';
    style.textContent = css;
    (document.head || document.documentElement).appendChild(style);
  }

  /* 统一跳转入口：在 iframe 里就交给外壳开标签，否则按普通跳转 */
  window.fxNav = function (url) {
    if (!url) return;
    if (window.parent && window.parent !== window && typeof window.parent.__fxOpen === 'function') {
      window.parent.__fxOpen(url);
    } else {
      location.href = url;
    }
  };
})();

/* ================= 表格增强：双击表头排序 / 双击行打开编辑 =================
 *
 * 为什么放在 embed.js：所有子页面都引了这一份，六个页面各写一遍必然走形。
 *
 * 用法（页面末尾调用）：
 *   fxTable.sort(document.querySelector('#panel-products table'));   // 双击表头排序
 *   fxTable.rowEdit($('prodBody'), tr => openProdModal(idOf(tr)));   // 双击行打开编辑
 *
 * 两个关键点：
 * 1. 排序状态挂在 table 上，并用 MutationObserver 盯着 tbody —— 页面重新渲染
 *    （innerHTML 重建行）之后自动把顺序套回去，不用每个渲染函数都记得调。
 * 2. 双击行只在「不是点在输入框/按钮/链接」时触发。商品表里有一级价、大类
 *    这些内联输入框，双击它们不能弹编辑框，否则根本没法改数。
 */
(function () {
  var ARROW = { asc: '▲', desc: '▼' };
  var store = new WeakMap();

  function injectCss() {
    if (document.getElementById('fx-table-css')) return;
    var st = document.createElement('style');
    st.id = 'fx-table-css';
    st.textContent = [
      'th.fx-sort{cursor:pointer;user-select:none}',
      'th.fx-sort:hover{color:#2463eb}',
      'th.fx-sort .fx-ar{font-size:9px;margin-left:3px;opacity:.8}',
      'tbody.fx-dbl tr{cursor:pointer}'
    ].join('');
    (document.head || document.documentElement).appendChild(st);
  }

  /* 取值口径：单元格里有输入框就取输入框的值（一级价/大类是内联编辑的），
     否则取文本。排的是「屏幕上看到的数」，不是 DOM 里的旧值。 */
  var EMPTY = /^(|—|–|-|n\/a)$/i;

  function cellMeta(tr, idx) {
    var td = tr.children[idx];
    if (!td) return { text: '', num: false };
    var f = td.querySelector('input,select,textarea');
    var text = f ? String(f.value || '').trim() : String(td.textContent || '').trim();
    /* 单元格可以自己声明排序值：像「销售额 RAW→最终」这种一格两数的，
       光看文本只能拿到 RAW，用 data-sort 指定按最终值排。 */
    var ds = td.getAttribute('data-sort');
    if (ds !== null && ds !== '' && !isNaN(parseFloat(ds))) {
      return { text: text, num: true, val: parseFloat(ds) };
    }
    /* 右对齐列（class=num）和数字输入框按数字排；其余按文本排（ISO 时间戳按文本排本来就有序） */
    var isNum = td.classList.contains('num') || (f && f.type === 'number');
    return { text: text, num: !!isNum };
  }

  /* 只取开头的数字：右对齐单元格里常常「价格 + 日期」两行拼在一起
     （如 82.24 与 2026-02-06 → "82.242026-02-06"），整串转数字会失败。 */
  function leadNum(s) {
    var m = String(s).replace(/[¥￥,\s%]/g, '').match(/^-?\d+(\.\d+)?/);
    return m ? parseFloat(m[0]) : NaN;
  }

  function sortNow(table) {
    var st = store.get(table);
    if (!st) return;
    var tbody = table.tBodies[0];
    if (!tbody || tbody.rows.length < 2) return;
    var rows = Array.prototype.slice.call(tbody.rows);
    var idx = st.idx;
    rows.sort(function (a, b) {
      var ma = cellMeta(a, idx), mb = cellMeta(b, idx);
      var ea = EMPTY.test(ma.text), eb = EMPTY.test(mb.text);
      if (ea && eb) return 0;
      if (ea) return 1;      /* 空值永远沉底，不跟着升降翻转 */
      if (eb) return -1;
      var na = (ma.val !== undefined) ? ma.val : (ma.num ? leadNum(ma.text) : NaN);
      var nb = (mb.val !== undefined) ? mb.val : (mb.num ? leadNum(mb.text) : NaN);
      var r = (!isNaN(na) && !isNaN(nb))
        ? na - nb
        : ma.text.localeCompare(mb.text, 'zh', { numeric: true, sensitivity: 'base' });
      return st.dir === 'desc' ? -r : r;
    });
    st.busy = true;
    var frag = document.createDocumentFragment();
    rows.forEach(function (r) { frag.appendChild(r); });
    tbody.appendChild(frag);
    setTimeout(function () { st.busy = false; }, 0);
  }

  function markHeader(table) {
    var st = store.get(table);
    var ths = table.tHead ? table.tHead.rows[0].cells : [];
    Array.prototype.forEach.call(ths, function (th) {
      var old = th.querySelector('.fx-ar');
      if (old) old.parentNode.removeChild(old);
      if (st && th.cellIndex === st.idx) {
        var s = document.createElement('span');
        s.className = 'fx-ar';
        s.textContent = ARROW[st.dir] || '';
        th.appendChild(s);
      }
    });
  }

  function sort(table) {
    if (!table || !table.tHead || !table.tHead.rows[0] || table.dataset.fxSort === '1') return;
    table.dataset.fxSort = '1';
    injectCss();
    Array.prototype.forEach.call(table.tHead.rows[0].cells, function (th) {
      th.classList.add('fx-sort');
    });

    table.tHead.addEventListener('dblclick', function (e) {
      var th = e.target.closest ? e.target.closest('th') : null;
      if (!th || !table.tHead.contains(th)) return;
      if (e.target.closest('input,button,select,a')) return;
      var col = th.dataset ? th.dataset.col : '';
      if (col === 'ck' || col === 'ops' || col === 'pick') return;   // 勾选/操作列不排序
      if (!(th.textContent || '').trim() && th.querySelector('input')) return;
      var idx = th.cellIndex;
      var st = store.get(table);
      if (st && st.idx === idx) st.dir = st.dir === 'asc' ? 'desc' : 'asc';
      else st = { idx: idx, dir: 'asc' };
      store.set(table, st);
      markHeader(table);
      sortNow(table);

      /* 行是 innerHTML 重建的，渲染完要把排序顺序套回去 */
      if (!table._fxObserver) {
        var tb = table.tBodies[0];
        if (tb) {
          table._fxObserver = new MutationObserver(function () {
            var s = store.get(table);
            if (s && !s.busy) sortNow(table);
          });
          table._fxObserver.observe(tb, { childList: true });
        }
      }
    });
  }

  /* 回调式排序：表头每次渲染都会重建（透视表、动态列），DOM 排序只能排当前页，
     所以把「点哪一列、升还是降」交给页面，由页面去排自己的数据源再重渲染。 */
  function sortData(table, onSort) {
    if (!table || !table.tHead || table.dataset.fxSortData === '1') return;
    table.dataset.fxSortData = '1';
    injectCss();
    var st = { idx: -1, dir: 'asc' };
    function paint() {
      var row = table.tHead.rows[0];
      if (!row) return;
      Array.prototype.forEach.call(row.cells, function (th) {
        th.classList.add('fx-sort');
        var old = th.querySelector('.fx-ar');
        if (old) old.parentNode.removeChild(old);
        if (th.cellIndex === st.idx) {
          var s = document.createElement('span');
          s.className = 'fx-ar';
          s.textContent = ARROW[st.dir] || '';
          th.appendChild(s);
        }
      });
    }
    table.tHead.addEventListener('dblclick', function (e) {
      var th = e.target.closest ? e.target.closest('th') : null;
      if (!th || !table.tHead.contains(th)) return;
      if (e.target.closest('input,button,select,a')) return;
      var col = th.dataset ? th.dataset.col : '';
      if (col === 'ck' || col === 'ops' || col === 'pick') return;
      if (!(th.textContent || '').trim() && th.querySelector('input')) return;
      if (st.idx === th.cellIndex) st.dir = st.dir === 'asc' ? 'desc' : 'asc';
      else { st.idx = th.cellIndex; st.dir = 'asc'; }
      onSort(st.idx, st.dir);
      paint();
    });
    return paint;
  }

  function rowEdit(tbody, handler) {
    if (!tbody || tbody.dataset.fxDbl === '1') return;
    tbody.dataset.fxDbl = '1';
    injectCss();
    tbody.classList.add('fx-dbl');
    tbody.addEventListener('dblclick', function (e) {
      if (e.target.closest && e.target.closest('input,select,textarea,button,a,label')) return;
      var tr = e.target.closest('tr');
      if (!tr || tr.parentElement !== tbody) return;
      handler(tr, e);
    });
  }

  window.fxTable = { sort: sort, sortData: sortData, rowEdit: rowEdit };
})();
