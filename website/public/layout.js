(() => {
  const getCurrentPage = () => {
    const segment = window.location.pathname.split('/').pop() || 'index.html';
    if (segment === '') return 'index.html';
    return segment.endsWith('.html') ? segment : `${segment}.html`;
  };

  const renderLayout = () => {
    const currentPage = getCurrentPage();
    const isHome = currentPage === 'index.html';

    const navItems = [
      { href: 'index.html', label: 'ホーム', page: 'index.html' },
      { href: 'guide.html', label: 'しばきかた', page: 'guide.html' },
      // { href: isHome ? '#features' : 'index.html#features', label: 'できること', page: '' },
      //{ href: 'terms.html', label: '利用規約', page: 'terms.html' },
      { href: 'privacy.html', label: 'プライバシーポリシー', page: 'privacy.html' },
      { href: 'credits.html', label: 'クレジット', page: 'credits.html' },
    ];

    const footerItems = [
      { href: 'guide.html', label: 'しばきかた' },
      //{ href: 'terms.html', label: '利用規約' },
      { href: 'privacy.html', label: 'プライバシーポリシー' },
      { href: 'credits.html', label: 'クレジット' },
    ];

    const headerEl = document.querySelector('header.site-header');
    if (headerEl) {
      const navLinksHtml = navItems
        .map((item) => {
          const isActive = item.page !== '' && item.page === currentPage;
          return `<a href="${item.href}"${isActive ? ' class="active"' : ''}>${item.label}</a>`;
        })
        .join('\n        ');

      headerEl.innerHTML = `
    <div class="container nav-inner">
      <a href="index.html" class="brand">
        <span>編隊サーバー管理青年アキラ</span>
      </a>
      <button class="menu-toggle" aria-label="メニューを開く" aria-expanded="false">☰</button>
      <nav class="nav-links">
        ${navLinksHtml}
      </nav>
    </div>`;

      const toggleBtn = headerEl.querySelector('.menu-toggle');
      const navLinks = headerEl.querySelector('.nav-links');
      if (toggleBtn && navLinks) {
        toggleBtn.addEventListener('click', () => {
          const isOpen = navLinks.classList.toggle('open');
          toggleBtn.setAttribute('aria-expanded', String(isOpen));
        });
      }
    }

    const footerEl = document.querySelector('footer.site-footer');
    if (footerEl) {
      const footerLinksHtml = footerItems
        .map((item) => `<a href="${item.href}">${item.label}</a>`)
        .join('\n        ');

      footerEl.innerHTML = `
    <div class="container footer-inner">
      <p>イキスギ</p>
      <div class="footer-links">
        ${footerLinksHtml}
      </div>
    </div>`;
    }
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', renderLayout);
  } else {
    renderLayout();
  }
})();
