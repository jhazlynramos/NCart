if (window.__ncartLogoutInitialized) {
  // Some templates include this shared script twice; initialize it only once.
} else {
window.__ncartLogoutInitialized = true;
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
  const modalStyles = document.createElement('style');
  modalStyles.textContent = `
    #shared-logout-modal {
      position: fixed;
      inset: 0;
      z-index: 100;
      display: grid;
      place-items: center;
      padding: 20px;
      background: rgba(17, 21, 52, 0.42);
      opacity: 0;
      transition: opacity 160ms ease;
    }
    #shared-logout-modal[hidden] { display: none; }
    #shared-logout-modal.is-visible { opacity: 1; }
    #shared-logout-modal .message-dialog {
      position: relative;
      width: min(100%, 360px);
      min-height: 0;
      padding: 30px 28px 14px;
      border: 1px solid #dfe2df;
      border-radius: 12px;
      background: #ffffff;
      box-shadow: 0 18px 45px rgba(17, 21, 52, 0.22);
      text-align: center;
      transform: translateY(8px);
      transition: transform 160ms ease;
    }
    #shared-logout-modal.is-visible .message-dialog { transform: translateY(0); }
    #shared-logout-modal .message-close {
      position: absolute;
      top: 10px;
      right: 12px;
      display: grid;
      place-items: center;
      width: 30px;
      height: 30px;
      padding: 0;
      border: 0;
      border-radius: 50%;
      color: #4a504d;
      background: transparent;
      cursor: pointer;
      font-size: 24px;
      line-height: 1;
    }
    #shared-logout-modal .message-close:hover,
    #shared-logout-modal .message-close:focus-visible {
      color: #080f76;
      background: #eef0ee;
      outline: none;
    }
    #shared-logout-modal .message-icon {
      display: grid;
      place-items: center;
      width: 48px;
      height: 48px;
      margin: 0 auto 14px;
      border-radius: 50%;
      color: #ffffff;
      background: #080f76;
      font-size: 24px;
    }
    #shared-logout-modal .message-title { margin: 0 0 8px; color: #111534; font: 400 22px Georgia, serif; }
    #shared-logout-modal .message-text { min-height: 0; margin: 10px 0 18px; color: #555b58; font: 12px/1.5 Consolas, monospace; }
    #shared-logout-modal .message-actions { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-top: 22px; }
    #shared-logout-modal .message-button { min-height: 36px; border: 0; border-radius: 6px; color: #ffffff; background: #080f76; cursor: pointer; font: 12px Consolas, monospace; }
    #shared-logout-modal .message-cancel { color: #202626; background: #eef0ee; }
    #shared-logout-modal .message-button:hover, #shared-logout-modal .message-button:focus-visible { filter: brightness(1.12); }
    @media (prefers-reduced-motion: reduce) { #shared-logout-modal, #shared-logout-modal .message-dialog { transition: none; } }
  `;
  document.head.append(modalStyles);
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
    logoutLink.focus();
  };
  const openModal = (event) => {
    event.preventDefault();
    modal.hidden = false;
    modal.classList.add('is-visible');
    modal.querySelector('.message-cancel').focus();
  };

  logoutLink.removeAttribute('onclick');
  logoutLink.addEventListener('click', openModal);
  modal.querySelector('.message-close').addEventListener('click', closeModal);
  modal.querySelector('.message-cancel').addEventListener('click', closeModal);
  modal.addEventListener('click', (event) => {
    if (event.target === modal) closeModal();
  });
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && !modal.hidden) closeModal();
  });
  modal.querySelector('.logout-confirm').addEventListener('click', () => {
    window.location.href = logoutLink.href;
  });
}
}
