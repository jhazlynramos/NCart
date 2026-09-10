const logoutLink = document.querySelector('.dashboard-logout');

const messagesLink = document.querySelector('.dashboard-nav-link[href="/messages"]');
if (messagesLink && !messagesLink.querySelector('.messages-unread-badge')) {
  const unreadBadge = document.createElement('b');
  unreadBadge.className = 'messages-unread-badge';
  unreadBadge.hidden = true;
  messagesLink.append(unreadBadge);
  const updateUnreadBadge = async () => {
    try {
      const response = await fetch('/api/messages', { cache: 'no-store' });
      if (!response.ok) return;
      const data = await response.json();
      const unreadCount = Number(data.unread_count || 0);
      unreadBadge.textContent = unreadCount > 99 ? '99+' : String(unreadCount);
      unreadBadge.hidden = unreadCount === 0;
    } catch {
      unreadBadge.hidden = true;
    }
  };
  updateUnreadBadge();
  window.setInterval(updateUnreadBadge, 2500);
}

if (logoutLink) {
  const modal = document.createElement('div');
  modal.className = 'message-modal';
  modal.id = 'shared-logout-modal';
  modal.hidden = true;
  modal.setAttribute('role', 'dialog');
  modal.setAttribute('aria-modal', 'true');
  modal.setAttribute('aria-labelledby', 'shared-logout-title');
  modal.innerHTML = `
    <div class="message-dialog">
      <button class="message-close" type="button" aria-label="Close">&times;</button>
      <div class="message-icon" aria-hidden="true">&#8617;</div>
      <h2 class="message-title" id="shared-logout-title">Log out?</h2>
      <p class="message-text">Are you sure you want to log out?</p>
      <div class="message-actions">
        <button class="message-button message-cancel" type="button">Cancel</button>
        <button class="message-button logout-confirm" type="button">Log out</button>
      </div>
    </div>`;
  document.body.append(modal);

  const closeModal = () => {
    modal.classList.remove('is-visible');
    modal.hidden = true;
  };
  const openModal = (event) => {
    event.preventDefault();
    modal.hidden = false;
    modal.classList.add('is-visible');
  };

  logoutLink.removeAttribute('onclick');
  logoutLink.addEventListener('click', openModal);
  modal.querySelector('.message-close').addEventListener('click', closeModal);
  modal.querySelector('.message-cancel').addEventListener('click', closeModal);
  modal.addEventListener('click', (event) => {
    if (event.target === modal) closeModal();
  });
  modal.querySelector('.logout-confirm').addEventListener('click', () => {
    window.location.href = logoutLink.href;
  });
}
