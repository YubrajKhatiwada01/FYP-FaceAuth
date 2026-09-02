/* ── Theme Toggle System ─────────────────────────────────────────────────── */
(function () {
  const STORAGE_KEY = 'faceauth-theme';
  const html = document.documentElement;

  function getCurrentTheme() {
    return html.getAttribute('data-theme') === 'light' ? 'light' : 'dark';
  }

  function applyTheme(theme) {
    if (theme === 'light') {
      html.setAttribute('data-theme', 'light');
    } else {
      html.removeAttribute('data-theme');
    }
    localStorage.setItem(STORAGE_KEY, theme);
    // Update all theme toggle buttons on the page
    document.querySelectorAll('.theme-toggle').forEach(btn => {
      btn.setAttribute('aria-label', theme === 'light' ? 'Switch to dark mode' : 'Switch to light mode');
      btn.setAttribute('title', theme === 'light' ? 'Switch to dark mode' : 'Switch to light mode');
    });
    // Dispatch custom event for any page-specific listeners
    window.dispatchEvent(new CustomEvent('themechange', { detail: { theme } }));
  }

  function toggleTheme() {
    const next = getCurrentTheme() === 'dark' ? 'light' : 'dark';
    applyTheme(next);
  }

  // Apply saved theme on load (also done in inline script for no-flash, this is a safety net)
  const saved = localStorage.getItem(STORAGE_KEY);
  if (saved === 'light') {
    applyTheme('light');
  }

  // Wire up all theme toggle buttons (works for nav + dashboard)
  document.addEventListener('DOMContentLoaded', function () {
    document.querySelectorAll('.theme-toggle, #themeToggleBtn').forEach(btn => {
      btn.addEventListener('click', function (e) {
        e.stopPropagation();
        toggleTheme();
      });
    });
  });

  // Also wire up immediately if DOM is already ready
  if (document.readyState !== 'loading') {
    document.querySelectorAll('.theme-toggle, #themeToggleBtn').forEach(btn => {
      btn.addEventListener('click', function (e) {
        e.stopPropagation();
        toggleTheme();
      });
    });
  }

  // Expose globally for manual use
  window.FaceAuthTheme = { toggle: toggleTheme, apply: applyTheme, get: getCurrentTheme };
})();

const navToggle = document.querySelector(".nav-toggle");
const navLinks = document.querySelector(".nav-links");

if (navToggle && navLinks) {
  navToggle.addEventListener("click", () => {
    const isOpen = navToggle.getAttribute("aria-expanded") === "true";
    navToggle.setAttribute("aria-expanded", String(!isOpen));
    navLinks.classList.toggle("is-open", !isOpen);
  });
}

// Dashboard Sidebar & Header Dropdown Handlers
document.addEventListener('DOMContentLoaded', () => {
  const hamburger = document.getElementById('hamburgerToggle');
  const sidebar = document.getElementById('sidebarNav');
  const overlay = document.getElementById('sidebarOverlay');
  const sidebarClose = document.querySelector('.sidebar-close');

  if (hamburger && sidebar && overlay) {
    const toggleSidebar = () => {
      const isOpen = hamburger.getAttribute('aria-expanded') === 'true';
      hamburger.setAttribute('aria-expanded', String(!isOpen));
      sidebar.classList.toggle('is-open');
      overlay.classList.toggle('is-visible');
    };

    hamburger.addEventListener('click', toggleSidebar);
    if (sidebarClose) sidebarClose.addEventListener('click', toggleSidebar);
    overlay.addEventListener('click', toggleSidebar);

    // Close sidebar when clicking links (mobile)
    const navItems = document.querySelectorAll('.sidebar-nav a');
    navItems.forEach(item => {
      item.addEventListener('click', () => {
        if (window.innerWidth <= 768) {
          toggleSidebar();
        }
      });
    });
  }

  // ---------------------------------------------------------------------------
  // Header Dropdowns (Notifications & Profile) Handlers
  // ---------------------------------------------------------------------------
  const notifBtn = document.getElementById('notificationBtn');
  const notifDropdown = document.getElementById('notificationDropdown');
  const notifBadge = document.getElementById('notificationBadge');
  const notifPing = document.getElementById('notificationPing');
  const notifUnreadCount = document.getElementById('notificationUnreadCount');
  const notifList = document.getElementById('notificationList');
  const clearNotifBtn = document.getElementById('clearNotificationsBtn');
  const notifTabs = document.querySelectorAll('.notif-tab');

  const profileBtn = document.getElementById('profileBtn');
  const profileDropdown = document.getElementById('profileDropdown');

  const adminProfileModal = document.getElementById('adminProfileModal');
  const openAdminProfileModalBtn = document.getElementById('openAdminProfileModalBtn');
  const closeAdminProfileModal = document.getElementById('closeAdminProfileModal');
  const closeAdminProfileModalDoneBtn = document.getElementById('closeAdminProfileModalDoneBtn');

  let notificationsData = [];
  let currentTab = 'all';

  const closeAllDropdowns = () => {
    if (notifDropdown) {
      notifDropdown.classList.remove('is-open');
      notifDropdown.setAttribute('aria-hidden', 'true');
    }
    if (notifBtn) notifBtn.setAttribute('aria-expanded', 'false');
    if (profileDropdown) {
      profileDropdown.classList.remove('is-open');
      profileDropdown.setAttribute('aria-hidden', 'true');
    }
    if (profileBtn) profileBtn.setAttribute('aria-expanded', 'false');
  };

  if (notifBtn && notifDropdown) {
    notifBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      const isOpen = notifDropdown.classList.contains('is-open');
      closeAllDropdowns();
      if (!isOpen) {
        notifDropdown.classList.add('is-open');
        notifDropdown.setAttribute('aria-hidden', 'false');
        notifBtn.setAttribute('aria-expanded', 'true');
        fetchNotifications();
      }
    });
  }

  if (profileBtn && profileDropdown) {
    profileBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      const isOpen = profileDropdown.classList.contains('is-open');
      closeAllDropdowns();
      if (!isOpen) {
        profileDropdown.classList.add('is-open');
        profileDropdown.setAttribute('aria-hidden', 'false');
        profileBtn.setAttribute('aria-expanded', 'true');
        fetchAdminProfile();
      }
    });
  }

  if (notifDropdown) notifDropdown.addEventListener('click', (e) => e.stopPropagation());
  if (profileDropdown) profileDropdown.addEventListener('click', (e) => e.stopPropagation());

  document.addEventListener('click', () => {
    closeAllDropdowns();
  });

  async function fetchNotifications() {
    if (!notifList) return;
    try {
      const res = await fetch('/api/notifications');
      if (!res.ok) throw new Error('Failed to fetch notifications');
      const data = await res.json();
      if (data.success) {
        notificationsData = data.notifications || [];
        updateUnreadCount(data.unread_count || 0);
        renderNotifications();
      }
    } catch (err) {
      console.warn('Notifications fetch error:', err);
      notifList.innerHTML = `<div class="notif-empty">Failed to load recent notifications.</div>`;
    }
  }

  function updateUnreadCount(count) {
    if (notifBadge) {
      notifBadge.textContent = count;
      notifBadge.style.display = count > 0 ? 'flex' : 'none';
    }
    if (notifPing && notifBtn) {
      if (count > 0) notifBtn.classList.add('has-unread');
      else notifBtn.classList.remove('has-unread');
    }
    if (notifUnreadCount) {
      notifUnreadCount.textContent = count > 0 ? `${count} new` : 'All read';
    }
  }

  function renderNotifications() {
    if (!notifList) return;
    const filtered = notificationsData.filter(item => {
      if (currentTab === 'all') return true;
      return item.category === currentTab;
    });

    if (filtered.length === 0) {
      notifList.innerHTML = `<div class="notif-empty">No ${currentTab === 'all' ? '' : currentTab.replace('_', ' ')} notifications found.</div>`;
      return;
    }

    const getIconSvg = (iconType) => {
      switch (iconType) {
        case 'check_circle':
          return `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg>`;
        case 'x_circle':
          return `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/></svg>`;
        case 'user':
          return `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>`;
        case 'bluetooth':
          return `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="6.5 6.5 17.5 17.5 12 23 12 1 17.5 6.5 6.5 17.5"/></svg>`;
        case 'shield':
          return `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>`;
        default:
          return `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/></svg>`;
      }
    };

    notifList.innerHTML = filtered.map(item => `
      <div class="notif-item" data-id="${item.id}">
        <div class="notif-icon-badge ${item.badge_color}">
          ${getIconSvg(item.icon_type)}
        </div>
        <div class="notif-content">
          <div class="notif-header-row">
            <h4 class="notif-title">${item.title}</h4>
            <span class="notif-time">${item.timestamp.split(' ')[1] || item.timestamp}</span>
          </div>
          <p class="notif-detail">${item.details || (item.username + ' at ' + item.access_point)}</p>
          <span class="notif-meta-pill">📍 ${item.access_point} • ${item.username}</span>
        </div>
      </div>
    `).join('');
  }

  notifTabs.forEach(tab => {
    tab.addEventListener('click', () => {
      notifTabs.forEach(t => t.classList.remove('is-active'));
      tab.classList.add('is-active');
      currentTab = tab.dataset.tab;
      renderNotifications();
    });
  });

  if (clearNotifBtn) {
    clearNotifBtn.addEventListener('click', () => {
      updateUnreadCount(0);
      notificationsData = [];
      renderNotifications();
    });
  }

  fetchNotifications();

  async function fetchAdminProfile() {
    try {
      const res = await fetch('/api/profile');
      if (!res.ok) return;
      const data = await res.json();
      if (data.success && data.profile) {
        const p = data.profile;
        const initial = (p.full_name || p.username || 'A')[0].toUpperCase();

        const menuAdminName = document.getElementById('menuAdminName');
        const menuAdminEmail = document.getElementById('menuAdminEmail');
        const menuAvatar = document.getElementById('menuAvatar');
        const headerAvatar = document.getElementById('headerAvatar');

        if (menuAdminName) menuAdminName.textContent = p.full_name || p.username;
        if (menuAdminEmail) menuAdminEmail.textContent = p.email || 'admin@faceauth.sec';
        if (menuAvatar) menuAvatar.textContent = initial;
        if (headerAvatar) headerAvatar.textContent = initial;

        const modalAvatar = document.getElementById('modalAvatar');
        const modalAdminFullName = document.getElementById('modalAdminFullName');
        const modalAdminUsername = document.getElementById('modalAdminUsername');
        const modalAdminEmail = document.getElementById('modalAdminEmail');
        const modalAdminPhone = document.getElementById('modalAdminPhone');
        const modalSessionId = document.getElementById('modalSessionId');
        const modalClientIp = document.getElementById('modalClientIp');
        const modalDbSync = document.getElementById('modalDbSync');

        if (modalAvatar) modalAvatar.textContent = initial;
        if (modalAdminFullName) modalAdminFullName.textContent = p.full_name || p.username;
        if (modalAdminUsername) modalAdminUsername.textContent = p.username;
        if (modalAdminEmail) modalAdminEmail.textContent = p.email;
        if (modalAdminPhone) modalAdminPhone.textContent = p.phone || 'N/A';
        if (modalSessionId) modalSessionId.textContent = `SEC-${p.session_id.substring(0, 6).toUpperCase()}-ACTIVE`;
        if (modalClientIp) modalClientIp.textContent = p.ip_address || '127.0.0.1';
        if (modalDbSync) modalDbSync.textContent = p.db_sync || 'DynamoDB + S3 Sync';
      }
    } catch (err) {
      console.warn('Profile fetch error:', err);
    }
  }

  if (openAdminProfileModalBtn && adminProfileModal) {
    openAdminProfileModalBtn.addEventListener('click', () => {
      closeAllDropdowns();
      fetchAdminProfile();
      adminProfileModal.classList.add('active');
    });
  }

  if (closeAdminProfileModal && adminProfileModal) {
    closeAdminProfileModal.addEventListener('click', () => {
      adminProfileModal.classList.remove('active');
    });
  }

  if (closeAdminProfileModalDoneBtn && adminProfileModal) {
    closeAdminProfileModalDoneBtn.addEventListener('click', () => {
      adminProfileModal.classList.remove('active');
    });
  }

  // Close modals when clicking outside
  document.addEventListener('click', (e) => {
    if (e.target.classList.contains('modal')) {
      e.target.classList.remove('active');
    }
  });

  // Close modals with Escape key
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
      closeAllDropdowns();
      document.querySelectorAll('.modal.active').forEach(modal => {
        modal.classList.remove('active');
      });
    }
  });
});


