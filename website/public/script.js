document.addEventListener('DOMContentLoaded', () => {
  const rawPage = window.location.pathname.split('/').pop() || 'index.html';
  const currentPage = rawPage === '' ? 'index.html' : rawPage;

  const navItems = [
    { href: 'index.html', label: 'ホーム', page: 'index.html' },
    { href: 'guide.html', label: 'しばきかた', page: 'guide.html' },
    { href: 'index.html#features', label: 'できること', page: '' },
    { href: 'terms.html', label: '利用規約', page: 'terms.html' },
    { href: 'privacy.html', label: 'プライバシーポリシー', page: 'privacy.html' },
    { href: 'credits.html', label: 'クレジット', page: 'credits.html' },
  ];

  const headerEl = document.querySelector('header.site-header');
  if (headerEl) {
    const linksHtml = navItems
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
        ${linksHtml}
      </nav>
    </div>`;
  }

  const footerEl = document.querySelector('footer.site-footer');
  if (footerEl) {
    footerEl.innerHTML = `
    <div class="container footer-inner">
      <p>イキスギ</p>
      <div class="footer-links">
        <a href="guide.html">しばきかた</a>
        <a href="terms.html">利用規約</a>
        <a href="privacy.html">プライバシーポリシー</a>
        <a href="credits.html">クレジット</a>
      </div>
    </div>`;
  }

  const toggleBtn = document.querySelector('.menu-toggle');
  const navLinks = document.querySelector('.nav-links');

  if (toggleBtn && navLinks) {
    toggleBtn.addEventListener('click', () => {
      const isOpen = navLinks.classList.toggle('open');
      toggleBtn.setAttribute('aria-expanded', String(isOpen));
    });
  }

  const inviteBtn = document.getElementById('invite-btn');
  if (inviteBtn) {
    inviteBtn.addEventListener('click', () => {
      alert('ボットの一般公開は準備中ゾ。みとけよみとけよ～');
    });
  }
});
