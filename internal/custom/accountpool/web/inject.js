(function () {
  const TARGET_HREF = "/account-pool";
  const ICON = `
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">
      <path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"></path>
      <circle cx="9" cy="7" r="4"></circle>
      <path d="M22 21v-2a4 4 0 0 0-3-3.87"></path>
      <path d="M16 3.13a4 4 0 0 1 0 7.75"></path>
    </svg>`;

  function injectNavItem() {
    const nav = document.querySelector(".nav-section");
    if (!nav || nav.querySelector('[data-account-pool-nav="true"]')) {
      return;
    }

    const item = document.createElement("a");
    item.href = TARGET_HREF;
    item.className = "nav-item";
    item.dataset.accountPoolNav = "true";
    item.innerHTML = `<span class="nav-icon">${ICON}</span><span class="nav-label">账号池</span>`;
    item.title = "账号池";
    item.addEventListener("click", function () {
      window.location.href = TARGET_HREF;
    });
    nav.appendChild(item);
  }

  injectNavItem();
  const observer = new MutationObserver(injectNavItem);
  observer.observe(document.body, { childList: true, subtree: true });
})();
