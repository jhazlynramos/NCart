const verificationRows = document.querySelector("#student-verification-rows");
const verificationEmpty = document.querySelector("#student-verification-empty");
const verificationError = document.querySelector("#student-verification-error");

const escapeVerificationHtml = (value) => String(value ?? "").replace(/[&<>"']/g, (character) => ({
  "&": "&amp;",
  "<": "&lt;",
  ">": "&gt;",
  '"': "&quot;",
  "'": "&#39;",
}[character]));

const formatVerificationDate = (value) => {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "Unavailable" : date.toLocaleString();
};

const loadStudentVerifications = async () => {
  try {
    const response = await fetch("/api/admin/student-verifications");
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Could not load student verifications.");

    verificationRows.innerHTML = data.verifications.map((verification) => `
      <tr data-user-id="${Number(verification.id)}">
        <td>${escapeVerificationHtml(verification.full_name)}</td>
        <td>${escapeVerificationHtml(verification.email)}</td>
        <td>${escapeVerificationHtml(verification.student_id_number)}</td>
        <td>${escapeVerificationHtml(formatVerificationDate(verification.verification_submitted_at))}</td>
        <td><img class="student-verification-image" src="/api/admin/student-verifications/${Number(verification.id)}/image" alt="Submitted Student ID for ${escapeVerificationHtml(verification.full_name)}" loading="lazy"></td>
        <td class="student-verification-decision">
          <label class="visually-hidden" for="verification-reason-${Number(verification.id)}">Reason if rejecting ${escapeVerificationHtml(verification.full_name)}</label>
          <input id="verification-reason-${Number(verification.id)}" class="student-verification-reason" type="text" maxlength="500" placeholder="Reason required if rejected">
          <div>
            <button type="button" data-status="verified">Verify</button>
            <button type="button" data-status="rejected">Reject</button>
          </div>
        </td>
      </tr>
    `).join("");
    verificationEmpty.hidden = data.verifications.length !== 0;
    verificationError.hidden = true;
  } catch (error) {
    verificationError.textContent = error.message;
    verificationError.hidden = false;
  }
};

verificationRows.addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-status]");
  if (!button) return;
  const row = button.closest("tr[data-user-id]");
  const reason = row.querySelector(".student-verification-reason").value.trim();
  if (button.dataset.status === "rejected" && !reason) {
    verificationError.textContent = "Enter a reason before rejecting this Student ID.";
    verificationError.hidden = false;
    row.querySelector(".student-verification-reason").focus();
    return;
  }

  const buttons = row.querySelectorAll("button");
  buttons.forEach((item) => { item.disabled = true; });
  verificationError.hidden = true;
  try {
    const response = await fetch(`/api/admin/student-verifications/${encodeURIComponent(row.dataset.userId)}/review`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ status: button.dataset.status, reason }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Could not save the verification decision.");
    row.remove();
    verificationEmpty.hidden = verificationRows.children.length !== 0;
  } catch (error) {
    verificationError.textContent = error.message;
    verificationError.hidden = false;
    buttons.forEach((item) => { item.disabled = false; });
  }
});

loadStudentVerifications();
