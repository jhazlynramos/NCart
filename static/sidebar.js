(() => {
  const shell = document.querySelector(".dashboard-shell");
  const sidebar = shell?.querySelector(".dashboard-sidebar");
  if (!shell || !sidebar) return;

  sidebar.querySelectorAll('.dashboard-nav a[href="/profile"], .dashboard-nav a[href="/notifications"], .dashboard-logout, .dashboard-nav a[href="/logout"]').forEach((link) => link.remove());
  if (sidebar.querySelector(".dashboard-sidebar-toggle")) return;
  const nav = sidebar.querySelector(".dashboard-nav");
  let cartNavLink = nav?.querySelector('a[href="/cart"]');
  if (nav && !cartNavLink) {
    cartNavLink = document.createElement("a");
    cartNavLink.className = "dashboard-nav-link";
    cartNavLink.href = "/cart";
    cartNavLink.hidden = true;
    cartNavLink.innerHTML = '<span class="dashboard-nav-icon" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"><path d="M3 4h2l2.2 11.2a2 2 0 0 0 2 1.6h8.3a2 2 0 0 0 2-1.6L21 8H6"/><circle cx="10" cy="21" r="1"/><circle cx="18" cy="21" r="1"/></svg></span><span class="dashboard-nav-label">Cart</span>';
    const favoritesLink = nav.querySelector('a[href="/favorites"]');
    (favoritesLink || nav.lastElementChild)?.insertAdjacentElement(favoritesLink ? "afterend" : "beforebegin", cartNavLink);
  }
  if (cartNavLink && !cartNavLink.querySelector(".cart-nav-count")) {
    const badge = document.createElement("span");
    badge.className = "cart-nav-count";
    badge.hidden = true;
    cartNavLink.append(badge);
  }
  if (cartNavLink) cartNavLink.hidden = true;
  const refreshCartCount = async () => {
    const badge = cartNavLink?.querySelector(".cart-nav-count");
    if (!badge) return;
    const response = await fetch("/api/cart/count", { cache: "no-store" });
    if (!response.ok) throw new Error("Could not load cart count.");
    const data = await response.json();
    const count = Number(data.count || 0);
    badge.textContent = String(count);
    badge.hidden = count < 1;
    cartNavLink.setAttribute("aria-label", count ? `Cart, ${count} items` : "Cart");
  };
  window.addEventListener("ncart:cart-updated", (event) => {
    const badge = cartNavLink?.querySelector(".cart-nav-count");
    if (!badge || !Number.isFinite(Number(event.detail?.count))) return;
    const count = Number(event.detail.count);
    badge.textContent = String(count);
    badge.hidden = count < 1;
    cartNavLink.setAttribute("aria-label", count ? `Cart, ${count} items` : "Cart");
  });
  const brand = sidebar.querySelector(".dashboard-brand");
  const navLinks = Array.from(sidebar.querySelectorAll(".dashboard-nav-link"));
  const top = document.createElement("div");
  top.className = "dashboard-sidebar-top";

  const toggle = document.createElement("button");
  toggle.className = "dashboard-sidebar-toggle";
  toggle.type = "button";
  toggle.setAttribute("aria-controls", "dashboard-navigation");
  toggle.innerHTML = '<span class="dashboard-sidebar-toggle-icon" aria-hidden="true"></span>';

  if (brand) top.append(brand);
  top.append(toggle);
  sidebar.prepend(top);

  if (nav) nav.id = "dashboard-navigation";

  const currentPath = window.location.pathname
    .replace(/^\/templates/, "")
    .replace(/\.html$/, "")
    .replace(/\/+$/, "") || "/home";

  const setupGlobalHeader = async () => {
    const content = shell.querySelector(".dashboard-content");
    if (!content) return;
    let header = content.querySelector(":scope > .dashboard-header");
    if (!header) {
      header = document.createElement("header");
      header.className = "dashboard-header";
      content.prepend(header);
    }
    header.classList.add("dashboard-global-header");

    if (!header.querySelector(".dashboard-global-brand")) {
      const brandLink = document.createElement("a");
      brandLink.className = "dashboard-global-brand";
      brandLink.href = "/home";
      brandLink.textContent = "NCart";
      header.prepend(brandLink);
    }

    let actions = header.querySelector(".dashboard-header-actions");
    if (!actions) {
      actions = document.createElement("div");
      actions.className = "dashboard-header-actions";
      header.append(actions);
    }

    let notificationWrap = actions.querySelector(".notification-header-wrap");
    if (!notificationWrap) {
      notificationWrap = document.createElement("div");
      notificationWrap.className = "notification-header-wrap";
      const notificationButton = document.createElement("button");
      notificationButton.type = "button";
      notificationButton.className = "notification-header-trigger";
      notificationButton.setAttribute("aria-label", "Notifications");
      notificationButton.setAttribute("aria-expanded", "false");
      notificationButton.setAttribute("aria-controls", "notification-dropdown");
      notificationButton.innerHTML = '<span class="notification-bell" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"><path d="M18 8a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9"/><path d="M10 21h4"/></svg></span><span class="notification-badge" hidden>0</span>';
      const notificationDropdown = document.createElement("div");
      notificationDropdown.className = "notification-dropdown";
      notificationDropdown.id = "notification-dropdown";
      notificationDropdown.hidden = true;
      notificationDropdown.innerHTML = '<div class="notification-dropdown-header"><span>Notifications</span><button class="notification-mark-all" type="button">Mark all as read</button></div><ul class="notification-list"></ul><a class="notification-view-all" href="/notifications">View all notifications</a>';
      notificationWrap.append(notificationButton, notificationDropdown);
      actions.append(notificationWrap);
    }

    let accountWrap = actions.querySelector(".account-dropdown-wrap");
    if (!accountWrap) {
      accountWrap = document.createElement("div");
      accountWrap.className = "account-dropdown-wrap";
      accountWrap.hidden = true;
      const accountButton = document.createElement("button");
      accountButton.type = "button";
      accountButton.className = "account-dropdown-trigger";
      accountButton.setAttribute("aria-label", "Open account menu");
      accountButton.setAttribute("aria-expanded", "false");
      accountButton.setAttribute("aria-controls", "account-dropdown");
      accountButton.innerHTML = '<span class="account-avatar account-trigger-avatar"><img alt="" hidden><span aria-hidden="true">N</span></span><span class="account-trigger-name"></span><svg class="account-verified-badge account-trigger-verified" viewBox="0 0 20 20" role="img" aria-label="Administrator-approved account" hidden><circle cx="10" cy="10" r="8.25"></circle><path d="m6.3 10.1 2.4 2.4 5-5.1"></path></svg><svg class="account-chevron" viewBox="0 0 20 20" aria-hidden="true"><path d="m5 7.5 5 5 5-5"></path></svg>';
      const accountDropdown = document.createElement("div");
      accountDropdown.className = "account-dropdown";
      accountDropdown.id = "account-dropdown";
      accountDropdown.hidden = true;
      accountDropdown.innerHTML = '<div class="account-dropdown-summary"><span class="account-avatar account-summary-avatar"><img alt="" hidden><span aria-hidden="true">N</span></span><span class="account-summary-copy"><strong class="account-summary-name"></strong><small class="account-summary-role">Account</small></span><svg class="account-verified-badge" viewBox="0 0 20 20" role="img" aria-label="Administrator-approved account" hidden><circle cx="10" cy="10" r="8.25"></circle><path d="m6.3 10.1 2.4 2.4 5-5.1"></path></svg></div><div class="account-menu-items"><a class="account-menu-item" href="/profile"><svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="8" r="4"></circle><path d="M20 21a8 8 0 0 0-16 0"></path></svg><span>View Profile</span></a><a class="account-menu-item" href="/logout" title="Sign out and return to sign in"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M10 17l5-5-5-5"></path><path d="M15 12H3"></path><path d="M12 3h6a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2h-6"></path></svg><span>Switch Account</span><span class="account-menu-arrow" aria-hidden="true">›</span></a><a class="account-menu-item" href="/profile" title="Manage your account profile and privacy"><svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="3"></circle><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1-1.7 2.9-.2-.1a1.7 1.7 0 0 0-1.9.2l-.2.2h-3.4l-.1-.2a1.7 1.7 0 0 0-1.9-.2l-.2.1-1.7-2.9.1-.1a1.7 1.7 0 0 0 .3-1.9l-.1-.2-1.7-1 1.7-2.9 1.9.3a1.7 1.7 0 0 0 1.5-.9l.1-.2h3.4l.1.2a1.7 1.7 0 0 0 1.5.9l1.9-.3 1.7 2.9-1.7 1-.1.2Z"></path></svg><span>Settings &amp; Privacy</span></a><a class="account-menu-item" href="/reports" title="Contact NCart through the complaints and support page"><svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="9"></circle><path d="M9.6 9a2.5 2.5 0 1 1 4.3 1.7c-1.2 1.2-1.9 1.5-1.9 3"></path><path d="M12 17h.01"></path></svg><span>Help &amp; Support</span></a></div><div class="account-theme-section"><button class="account-theme-toggle" type="button" role="switch" aria-checked="false" aria-label="Dark Mode"><svg class="account-theme-icon account-theme-icon-moon" viewBox="0 0 24 24" aria-hidden="true"><path d="M20.5 15.5A8.5 8.5 0 0 1 8.5 3.5 8.6 8.6 0 1 0 20.5 15.5Z"></path></svg><svg class="account-theme-icon account-theme-icon-sun" viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="4"></circle><path d="M12 2v2m0 16v2M4.93 4.93l1.42 1.42m11.3 11.3 1.42 1.42M2 12h2m16 0h2M4.93 19.07l1.42-1.42m11.3-11.3 1.42-1.42"></path></svg><span class="account-theme-label">Dark Mode</span><span class="account-theme-state">Off</span><span class="account-theme-switch" aria-hidden="true"><span></span></span></button></div><div class="account-menu-footer"><a class="account-menu-item account-menu-logout" href="/logout"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M10 17l5-5-5-5"></path><path d="M15 12H3"></path><path d="M12 3h6a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2h-6"></path></svg><span>Log Out</span></a></div>';
      accountWrap.append(accountButton, accountDropdown);
      actions.append(accountWrap);
    }

    const button = notificationWrap.querySelector(".notification-header-trigger");
    const dropdown = notificationWrap.querySelector(".notification-dropdown");
    const badge = button.querySelector(".notification-badge");
    const list = dropdown.querySelector(".notification-list");
    const markAllButton = dropdown.querySelector(".notification-mark-all");
    const formatTime = (value) => {
      try {
        const date = new Date(value);
        if (Number.isNaN(date.getTime())) return "Just now";
        const seconds = Math.max(0, Math.floor((Date.now() - date.getTime()) / 1000));
        if (seconds < 60) return seconds === 0 ? "Just now" : `${seconds}s ago`;
        const minutes = Math.floor(seconds / 60);
        if (minutes < 60) return `${minutes} minute${minutes === 1 ? "" : "s"} ago`;
        const hours = Math.floor(minutes / 60);
        if (hours < 24) return `${hours} hour${hours === 1 ? "" : "s"} ago`;
        const days = Math.floor(hours / 24);
        return `${days} day${days === 1 ? "" : "s"} ago`;
      } catch {
        return "Just now";
      }
    };

    const renderNotifications = (items) => {
      list.innerHTML = "";
      if (!items.length) {
        list.innerHTML = '<li class="notification-empty">No notifications yet.</li>';
        return;
      }
      items.forEach((item) => {
        const row = document.createElement("li");
        const card = document.createElement("a");
        card.className = `notification-item${item.is_read ? "" : " unread"}`;
        card.href = item.link_url || "/notifications";
        card.dataset.notificationId = String(item.id);
        card.innerHTML = `
          <span class="notification-icon" aria-hidden="true">${(item.kind || "general").startsWith("message") ? "💬" : (item.kind || "general") === "purchase" ? "🛒" : (item.kind || "general") === "rating" ? "⭐" : "🔔"}</span>
          <div class="notification-content">
            <p class="notification-title">${String(item.title || "Notification").replace(/[&<>"']/g, (char) => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"})[char])}</p>
            <p class="notification-message">${String(item.message || "").replace(/[&<>"']/g, (char) => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"})[char])}</p>
            <div class="notification-time">${formatTime(item.created_at)}</div>
          </div>
          <span class="notification-read-indicator" aria-hidden="true"></span>
        `;
        row.append(card);
        list.append(row);
      });
    };

    const loadNotifications = async () => {
      try {
        const response = await fetch("/api/notifications?limit=20");
        if (!response.ok) return;
        const data = await response.json();
        const items = Array.isArray(data.notifications) ? data.notifications : [];
        const count = Number(data.unread_count || 0);
        badge.textContent = count > 99 ? "99+" : String(count);
        badge.hidden = count === 0;
        renderNotifications(items);
      } catch (error) {
        console.error(error);
      }
    };

    const accountButton = accountWrap.querySelector(".account-dropdown-trigger");
    const accountDropdown = accountWrap.querySelector(".account-dropdown");
    const closeNotificationDropdown = () => {
      dropdown.hidden = true;
      button.setAttribute("aria-expanded", "false");
    };
    const closeAccountDropdown = () => {
      accountDropdown.hidden = true;
      accountButton.setAttribute("aria-expanded", "false");
    };

    button.addEventListener("click", () => {
      const shouldOpen = dropdown.hidden;
      closeAccountDropdown();
      dropdown.hidden = !shouldOpen;
      button.setAttribute("aria-expanded", String(shouldOpen));
      if (!dropdown.hidden) {
        void loadNotifications();
      }
    });
    accountButton.addEventListener("click", () => {
      const shouldOpen = accountDropdown.hidden;
      closeNotificationDropdown();
      accountDropdown.hidden = !shouldOpen;
      accountButton.setAttribute("aria-expanded", String(shouldOpen));
    });

    const themeToggle = accountDropdown.querySelector(".account-theme-toggle");
    const applyTheme = (isDark) => {
      document.documentElement.dataset.theme = isDark ? "dark" : "light";
      themeToggle.setAttribute("aria-checked", String(isDark));
      themeToggle.setAttribute("aria-label", isDark ? "Switch to Light Mode" : "Switch to Dark Mode");
      themeToggle.querySelector(".account-theme-label").textContent = isDark ? "Light Mode" : "Dark Mode";
      themeToggle.querySelector(".account-theme-state").textContent = isDark ? "On" : "Off";
      try {
        window.localStorage.setItem("ncart-theme", isDark ? "dark" : "light");
      } catch (error) {
        console.error("Could not save the NCart theme preference.", error);
      }
    };
    applyTheme(document.documentElement.dataset.theme === "dark");
    themeToggle.addEventListener("click", () => {
      applyTheme(themeToggle.getAttribute("aria-checked") !== "true");
    });

    markAllButton.addEventListener("click", async () => {
      try {
        const response = await fetch("/api/notifications/mark-read", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ all: true }),
        });
        if (!response.ok) throw new Error("Could not mark notifications as read.");
        await loadNotifications();
      } catch (error) {
        console.error(error);
      }
    });

    dropdown.addEventListener("click", async (event) => {
      const link = event.target.closest(".notification-item");
      if (!link) return;
      event.preventDefault();
      const id = link.dataset.notificationId;
      if (id) {
        try {
          const response = await fetch("/api/notifications/mark-read", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ notification_id: Number(id) }),
          });
          if (!response.ok) throw new Error("Could not mark notification as read.");
        } catch (error) {
          console.error(error);
        }
      }
      closeNotificationDropdown();
      await loadNotifications();
      window.location.assign(link.href);
    });

    document.addEventListener("click", (event) => {
      if (!notificationWrap.contains(event.target)) closeNotificationDropdown();
      if (!accountWrap.contains(event.target)) closeAccountDropdown();
    });
    document.addEventListener("keydown", (event) => {
      if (event.key !== "Escape") return;
      closeNotificationDropdown();
      closeAccountDropdown();
    });

    try {
      const response = await fetch("/api/profile", { cache: "no-store" });
      if (response.status !== 401) {
        if (!response.ok) throw new Error("Could not load account information.");
        const data = await response.json();
        const profile = data.profile;
        if (!profile) throw new Error("Account information was unavailable.");
        const fullName = String(profile.full_name || "NCart member").trim();
        const initial = fullName.charAt(0).toUpperCase() || "N";
        const initials = accountWrap.querySelectorAll(".account-avatar");
        initials.forEach((avatar) => {
          const image = avatar.querySelector("img");
          const fallback = avatar.querySelector("[aria-hidden='true']");
          fallback.textContent = initial;
          if (profile.profile_image_url) {
            image.src = profile.profile_image_url;
            image.hidden = false;
            fallback.hidden = true;
            image.addEventListener("error", () => {
              image.hidden = true;
              fallback.hidden = false;
            }, { once: true });
          }
        });
        accountButton.setAttribute("aria-label", `Account menu for ${fullName}`);
        accountWrap.querySelector(".account-trigger-name").textContent = fullName;
        accountWrap.querySelector(".account-summary-name").textContent = fullName;
        accountWrap.querySelector(".account-summary-role").textContent =
          profile.role === "admin" ? "Admin account" : "Buyer account";
        accountWrap.querySelectorAll(".account-verified-badge").forEach((badgeIcon) => {
          badgeIcon.hidden = !profile.verified;
        });
        accountWrap.hidden = false;
        if (cartNavLink) {
          cartNavLink.hidden = false;
          void refreshCartCount().catch((error) => console.error(error));
        }
      }
    } catch (error) {
      console.error(error);
    }

    void loadNotifications();
  };

  void setupGlobalHeader();

  const navigationLabels = {
    "/listings": "Posted Items",
    "/reports": "Complaints",
  };
  navLinks.forEach((link) => {
    const icon = link.querySelector("span");
    if (icon) {
      icon.classList.add("dashboard-nav-icon");
      const href = link.getAttribute("href");
      if (href === "/reports") {
        icon.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"><path d="M21 11.5a8.4 8.4 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.4 8.4 0 0 1-3.8-.9L3 21l1.9-5.7a8.4 8.4 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.4 8.4 0 0 1 3.8-.9h.5a8.5 8.5 0 0 1 8 8v.5Z"/><path d="M12 8v4"/><path d="M12 16h.01"/></svg>';
      } else if (href === "/logout") {
        icon.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"><path d="M10 17l5-5-5-5"/><path d="M15 12H3"/><path d="M12 3h6a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2h-6"/></svg>';
      }
    }

    const labelText = Array.from(link.childNodes)
      .filter((node) => node.nodeType === Node.TEXT_NODE)
      .map((node) => node.textContent)
      .join("")
      .trim();
    if (!labelText) return;

    const label = document.createElement("span");
    label.className = "dashboard-nav-label";
    const displayLabel = navigationLabels[link.getAttribute("href")] || labelText;
    label.textContent = displayLabel;
    Array.from(link.childNodes)
      .filter((node) => node.nodeType === Node.TEXT_NODE)
      .forEach((node) => node.remove());
    link.append(label);
    link.setAttribute("aria-label", displayLabel);
    link.title = displayLabel;
  });

  const emptyStateIconSelectors = {
    "/marketplace": ["#marketplace-empty > span"],
    "/favorites": ["#favorites-empty > span"],
    "/cart": ["#cart-empty > span"],
    "/listings": ["#listing-empty > span"],
    "/messages": ["#messages-empty > span"],
    "/orders": ["#orders-empty > span", "#seller-orders-empty > span"],
    "/reports": ["#reports-empty .reports-empty-icon"],
  };

  const addPageFeatureIcons = (href) => {
    const icon = sidebar.querySelector(`.dashboard-nav-link[href="${href}"] .dashboard-nav-icon svg`);
    if (!icon) return;

    emptyStateIconSelectors[href]?.forEach((selector) => {
      shell.querySelectorAll(selector).forEach((emptyStateIcon) => {
        emptyStateIcon.replaceChildren(icon.cloneNode(true));
      });
    });

    const heading = shell.querySelector(".dashboard-view h1, .dashboard-header h1");
    if (!heading && href === "/messages") {
      const featureIcon = shell.querySelector(".conversation-list-icon");
      if (featureIcon) featureIcon.replaceChildren(icon.cloneNode(true));
      return;
    }
    if (!heading || heading.querySelector(".dashboard-feature-heading-icon")) return;
    const headingIcon = document.createElement("span");
    headingIcon.className = "dashboard-feature-heading-icon";
    headingIcon.setAttribute("aria-hidden", "true");
    headingIcon.append(icon.cloneNode(true));
    heading.prepend(headingIcon);
  };

  if (currentPath !== "/admin") addPageFeatureIcons(currentPath);

  const storageKey = "ncart-sidebar-collapsed";
  let initiallyCollapsed = false;
  try {
    initiallyCollapsed = window.localStorage.getItem(storageKey) === "true";
  } catch {
    // Keep the sidebar usable when browser storage is unavailable.
  }
  if (initiallyCollapsed) shell.classList.add("is-sidebar-restoring");
  shell.classList.toggle("is-sidebar-collapsed", initiallyCollapsed);
  if (initiallyCollapsed) {
    shell.getBoundingClientRect();
    window.setTimeout(() => shell.classList.remove("is-sidebar-restoring"), 0);
  }

  const updateToggleState = (collapsed) => {
    toggle.setAttribute("aria-expanded", String(!collapsed));
    toggle.setAttribute("aria-label", collapsed ? "Expand sidebar" : "Collapse sidebar");
  };
  updateToggleState(initiallyCollapsed);

  const addAdminNavigation = async () => {
    try {
      const response = await fetch("/api/admin/reports/summary");
      if (response.status === 403 || response.status === 401) return;
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "Could not load admin navigation.");
      const reportLink = nav?.querySelector('a[href="/reports"]');
      if (reportLink) {
        let badge = reportLink.querySelector(".reports-pending-badge");
        if (!badge) {
          badge = document.createElement("b");
          badge.className = "reports-pending-badge";
          badge.setAttribute("aria-label", "pending complaints");
          reportLink.append(badge);
        }
        const count = Number(data.summary.pending);
        badge.textContent = count > 99 ? "99+" : String(count);
        badge.hidden = count === 0;
      }
      if (nav && !nav.querySelector('a[href="/admin"]')) {
        const dashboardLink = document.createElement("a");
        dashboardLink.className = "dashboard-nav-link";
        dashboardLink.href = "/admin";
        dashboardLink.innerHTML = '<span class="dashboard-nav-icon" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"><path d="M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8Z"/><path d="m19.4 15 .1.1a1.7 1.7 0 1 1-2.4 2.4l-.1-.1a1.7 1.7 0 0 0-2.9 1.2v.2a1.7 1.7 0 1 1-3.4 0v-.2a1.7 1.7 0 0 0-2.9-1.2l-.1.1a1.7 1.7 0 1 1-2.4-2.4l.1-.1a1.7 1.7 0 0 0-1.2-2.9H4a1.7 1.7 0 1 1 0-3.4h.2a1.7 1.7 0 0 0 1.2-2.9l-.1-.1a1.7 1.7 0 1 1 2.4-2.4l.1.1a1.7 1.7 0 0 0 2.9-1.2V4a1.7 1.7 0 1 1 3.4 0v.2a1.7 1.7 0 0 0 2.9 1.2l.1-.1a1.7 1.7 0 1 1 2.4 2.4l-.1.1a1.7 1.7 0 0 0 1.2 2.9h.2a1.7 1.7 0 1 1 0 3.4h-.2a1.7 1.7 0 0 0-1.2 1.1Z"/></svg></span>';
        const label = document.createElement("span");
        label.className = "dashboard-nav-label";
        label.textContent = "Admin dashboard";
        dashboardLink.append(label);
        dashboardLink.setAttribute("aria-label", "Admin dashboard");
        dashboardLink.title = "Admin dashboard";
        nav.append(dashboardLink);
      }
      if (currentPath === "/admin") addPageFeatureIcons("/admin");
    } catch (error) {
      console.error(error);
    }
  };
  void addAdminNavigation();
  window.setInterval(() => { void addAdminNavigation(); }, 15000);

  toggle.addEventListener("click", () => {
    const collapsed = shell.classList.toggle("is-sidebar-collapsed");
    updateToggleState(collapsed);
    try {
      window.localStorage.setItem(storageKey, String(collapsed));
    } catch {
      // The current page still responds if browser storage is unavailable.
    }
  });
})();
