(() => {
  const errorElement = document.querySelector("#admin-load-error");
  const rowsElement = document.querySelector("#admin-recent-rows");
  const emptyElement = document.querySelector("#admin-recent-empty");
  const pendingAccountRows = document.querySelector("#pending-account-rows");
  const pendingAccountEmpty = document.querySelector("#pending-account-empty");
  const pendingAccountError = document.querySelector("#pending-account-error");
  const pendingAccountCount = document.querySelector("#pending-account-count");
  if (!errorElement || !rowsElement || !emptyElement) return;

  const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, (character) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  })[character]);
  const formatDate = (value) => value ? new Date(value).toLocaleString() : "—";

  const loadPendingAccounts = async () => {
    if (!pendingAccountRows || !pendingAccountEmpty || !pendingAccountError) return;
    try {
      const response = await fetch("/api/admin/account-confirmations");
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "Could not load pending accounts.");
      pendingAccountRows.innerHTML = data.accounts.map((account) => {
        const isAdmin = account.account_type === "Admin";
        const requestStatus = account.admin_request_status;
        const deliveryStatus = account.admin_otp_delivery_status;
        let status = account.status;
        let actions = `
          <button type="button" class="pending-account-decision" data-decision="approved">Approve</button>
          <button type="button" class="pending-account-decision" data-decision="rejected">Reject</button>`;
        if (isAdmin && requestStatus === "approved") {
          status = deliveryStatus === "sent"
            ? "Approved — OTP sent"
            : deliveryStatus === "sending"
              ? "Approved — sending OTP"
              : deliveryStatus === "not_sent"
                ? "Approved — OTP not sent"
                : "Approved — OTP delivery failed";
          actions = ["failed", "not_sent"].includes(deliveryStatus)
            ? '<button type="button" class="pending-account-decision" data-retry-otp>Retry OTP Email</button>'
            : "—";
        }
        return `
          <tr data-account-id="${escapeHtml(account.id)}">
            <td>${escapeHtml(account.full_name)}</td>
            <td>${escapeHtml(account.email)}</td>
            <td>${escapeHtml(account.account_type)}</td>
            <td>${escapeHtml(formatDate(account.created_at))}</td>
            <td>${escapeHtml(status)}</td>
            <td class="student-verification-decision">${actions}</td>
          </tr>`;
      }).join("");
      pendingAccountCount.textContent = String(data.accounts.length);
      pendingAccountEmpty.hidden = data.accounts.length > 0;
      pendingAccountError.hidden = true;
      pendingAccountError.textContent = "";
    } catch (error) {
      pendingAccountError.textContent = error instanceof Error ? error.message : "Could not load pending accounts.";
      pendingAccountError.hidden = false;
    }
  };

  pendingAccountRows?.addEventListener("click", async (event) => {
    const button = event.target.closest("button[data-decision], button[data-retry-otp]");
    if (!button) return;
    const row = button.closest("tr[data-account-id]");
    row.querySelectorAll("button").forEach((item) => { item.disabled = true; });
    pendingAccountError.hidden = true;
    try {
      const retryOtp = button.hasAttribute("data-retry-otp");
      const response = await fetch(
        `/api/admin/account-confirmations/${encodeURIComponent(row.dataset.accountId)}/${retryOtp ? "retry-otp" : "review"}`,
        {
          method: "POST",
          ...(retryOtp ? {} : {
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ decision: button.dataset.decision }),
          }),
        },
      );
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "Could not save the account decision.");
      await loadPendingAccounts();
    } catch (error) {
      const message = error instanceof Error ? error.message : "Could not save the account decision.";
      await loadPendingAccounts();
      pendingAccountError.textContent = message;
      pendingAccountError.hidden = false;
      row.querySelectorAll("button").forEach((item) => { item.disabled = false; });
    }
  });

  const loadDashboard = async () => {
    try {
      const response = await fetch("/api/admin/reports/summary");
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "Could not load admin dashboard.");
      const summary = data.summary;
      document.querySelector("#summary-pending").textContent = summary.pending;
      document.querySelector("#summary-under-review").textContent = summary.under_review;
      document.querySelector("#summary-approved").textContent = summary.approved;
      document.querySelector("#summary-denied").textContent = summary.denied;
      rowsElement.innerHTML = summary.recent.map((report) => `
        <tr>
          <td>#${escapeHtml(report.id)}</td>
          <td>${escapeHtml(report.reporter_name || "Deleted user")}</td>
          <td>${escapeHtml(report.reported_user_name || "Deleted user")}</td>
          <td>${escapeHtml(report.listing_title)}</td>
          <td>${escapeHtml(report.reason)}</td>
          <td><span class="report-status status-${escapeHtml(report.status)}">${escapeHtml(report.status.replaceAll("_", " "))}</span></td>
          <td>${escapeHtml(formatDate(report.created_at))}</td>
          <td><a class="review-report-button" href="/reports?report=${encodeURIComponent(report.id)}">Review</a></td>
        </tr>`).join("");
      emptyElement.hidden = summary.recent.length > 0;
    } catch (error) {
      errorElement.textContent = error.message;
      errorElement.hidden = false;
    }
  };

  void loadDashboard();
  void loadPendingAccounts();
})();
