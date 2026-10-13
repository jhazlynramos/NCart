(() => {
  const itemsElement = document.querySelector("#cart-items");
  const toolbar = document.querySelector("#cart-toolbar");
  const summary = document.querySelector("#cart-summary");
  const emptyState = document.querySelector("#cart-empty");
  const selectAll = document.querySelector("#cart-select-all");
  const itemCount = document.querySelector("#cart-item-count");
  const subtotalElement = document.querySelector("#cart-subtotal");
  const checkoutButton = document.querySelector("#cart-checkout");
  const feedback = document.querySelector("#cart-feedback");
  const money = (value) => `₱${Number(value || 0).toFixed(2)}`;
  const escapeHtml = (value) => String(value ?? "").replace(/[&<>'"]/g, (character) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;",
  })[character]);
  let cart = [];

  const showFeedback = (message) => {
    feedback.textContent = message;
    feedback.hidden = !message;
  };

  const selectedItems = () => Array.from(itemsElement.querySelectorAll(".cart-item-checkbox:checked"))
    .map((checkbox) => cart.find((item) => String(item.cart_item_id) === checkbox.value))
    .filter(Boolean);

  const updateTotals = () => {
    const checked = selectedItems();
    const available = cart.filter((item) => item.is_available);
    const subtotal = checked.reduce((sum, item) => sum + Number(item.price || 0) * item.cart_quantity, 0);
    subtotalElement.textContent = money(subtotal);
    checkoutButton.disabled = checked.length === 0;
    selectAll.checked = available.length > 0 && checked.length === available.length;
    selectAll.indeterminate = checked.length > 0 && checked.length < available.length;
  };

  const render = () => {
    const nonempty = cart.length > 0;
    toolbar.hidden = !nonempty;
    summary.hidden = !nonempty;
    emptyState.hidden = nonempty;
    const unitCount = cart.reduce((sum, item) => sum + item.cart_quantity, 0);
    itemCount.textContent = `${unitCount} ${unitCount === 1 ? "item" : "items"}`;
    if (!nonempty) {
      itemsElement.replaceChildren();
      return;
    }
    itemsElement.innerHTML = cart.map((item) => {
      const available = item.available_quantity ?? 0;
      const quantity = item.cart_quantity;
      const unavailableClass = item.is_available ? "" : " cart-item-unavailable";
      const image = item.image_url
        ? `<img class="cart-item-image" src="${escapeHtml(item.image_url)}" alt="${escapeHtml(item.title || "Unavailable item")}">`
        : '<div class="cart-item-image-placeholder">No image</div>';
      const quantityControl = item.title
        ? `<div class="cart-item-controls">
             <button type="button" data-cart-action="decrease" aria-label="Decrease quantity" ${quantity <= 1 ? "disabled" : ""}>−</button>
             <input type="number" min="1" max="${Math.max(1, available)}" value="${quantity}" aria-label="Item quantity" ${available < 1 ? "disabled" : ""}>
             <button type="button" data-cart-action="increase" aria-label="Increase quantity" ${quantity >= available || available < 1 ? "disabled" : ""}>+</button>
             <small>${available < 1 ? "Out of stock" : `${available} available`}</small>
           </div>`
        : "";
      return `
        <article class="cart-item${unavailableClass}" data-cart-item-id="${item.cart_item_id}">
          <label class="cart-checkbox-wrap">
            <input type="checkbox" class="cart-item-checkbox" value="${item.cart_item_id}" aria-label="Select item for checkout"
              ${item.is_available ? "": "disabled"}>
          </label>
          ${image}
          <div class="cart-item-copy">
            <h2>${escapeHtml(item.title || "Unavailable item")}</h2>
            <p>${escapeHtml(item.category || "Item details unavailable")}</p>
            <span class="cart-item-price">${item.price === null ? "Price unavailable" : money(item.price)}</span>
            ${item.is_available ? "" : `<small class="cart-unavailable-message">${escapeHtml(item.availability_message)}</small>`}
            ${quantityControl}
          </div>
          <button class="cart-item-remove" type="button" data-cart-action="remove">Remove</button>
        </article>`;
    }).join("");
    updateTotals();
  };

  const loadCart = async () => {
    showFeedback("");
    const response = await fetch("/api/cart", { cache: "no-store" });
    const data = await response.json();
    if (!response.ok) {
      if (response.status === 401) {
        cart = [];
        render();
        showFeedback("Please log in to view your cart.");
        return;
      }
      throw new Error(data.error || "Could not load your cart.");
    }
    cart = data.items || [];
    render();
    window.dispatchEvent(new CustomEvent("ncart:cart-updated", { detail: { count: data.count } }));
  };

  const updateQuantity = async (itemId, quantity) => {
    const response = await fetch(`/api/cart/${encodeURIComponent(itemId)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ quantity }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Could not update quantity.");
    await loadCart();
  };

  itemsElement.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-cart-action]");
    if (!button) return;
    const row = button.closest(".cart-item");
    const item = cart.find((entry) => String(entry.cart_item_id) === row.dataset.cartItemId);
    if (!item) return;
    const action = button.dataset.cartAction;
    button.disabled = true;
    try {
      if (action === "remove") {
        const response = await fetch(`/api/cart/${encodeURIComponent(item.cart_item_id)}`, { method: "DELETE" });
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || "Could not remove this item.");
        window.dispatchEvent(new CustomEvent("ncart:cart-updated", { detail: { count: data.count } }));
        await loadCart();
      } else {
        await updateQuantity(item.cart_item_id, item.cart_quantity + (action === "increase" ? 1 : -1));
      }
    } catch (error) {
      showFeedback(error.message);
      await loadCart();
    }
  });

  itemsElement.addEventListener("change", async (event) => {
    if (event.target.matches(".cart-item-checkbox")) {
      updateTotals();
      return;
    }
    if (!(event.target.matches(".cart-item-controls input"))) return;
    const row = event.target.closest(".cart-item");
    const item = cart.find((entry) => String(entry.cart_item_id) === row.dataset.cartItemId);
    if (!item) return;
    const quantity = Number(event.target.value);
    try {
      if (!Number.isInteger(quantity) || quantity < 1) throw new Error("Quantity must be at least 1.");
      await updateQuantity(item.cart_item_id, quantity);
    } catch (error) {
      showFeedback(error.message);
      await loadCart();
    }
  });

  selectAll.addEventListener("change", () => {
    itemsElement.querySelectorAll(".cart-item-checkbox:not(:disabled)").forEach((checkbox) => {
      checkbox.checked = selectAll.checked;
    });
    updateTotals();
  });

  checkoutButton.addEventListener("click", () => {
    const selected = selectedItems().filter((item) => item.is_available);
    if (!selected.length || checkoutButton.disabled) return;
    const ids = selected.map((item) => item.cart_item_id).join(",");
    window.location.href = `/orders?checkout=1&cart_item_ids=${encodeURIComponent(ids)}`;
  });

  loadCart().catch((error) => {
    showFeedback(error.message);
    emptyState.hidden = false;
  });
})();
