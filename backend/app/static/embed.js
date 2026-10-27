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
