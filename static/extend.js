const boot = window.EXTEND_BOOT || {};
const form = document.getElementById("extend-form");
const endDateInput = document.getElementById("end_date");
const endDateDisplay = document.getElementById("end_date_display");
const endHourSelect = document.getElementById("end_hour");
const quoteEl = document.getElementById("extend-quote");
const messageEl = document.getElementById("extend-message");
const submitBtn = document.getElementById("extend-btn");

const ALL_HOURS = Array.from({ length: 24 }, (_, i) => String(i).padStart(2, "0"));
const VIPPS_NUMBER = boot.vipps || "908 12 444";

let latestQuote = null;

function escapeHtml(text) {
  return String(text)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function formatPrice(amount) {
  return String(amount).replace(/\B(?=(\d{3})+(?!\d))/g, " ");
}

function formatDateNorwegian(isoDate) {
  const [year, month, day] = isoDate.split("-");
  return `${String(day).padStart(2, "0")}.${String(month).padStart(2, "0")}.${year}`;
}

function syncDateDisplay() {
  if (endDateInput.value) {
    endDateDisplay.textContent = formatDateNorwegian(endDateInput.value);
    endDateDisplay.classList.remove("is-empty");
  } else {
    endDateDisplay.textContent = "";
    endDateDisplay.classList.add("is-empty");
  }
}

function showMessage(text, type) {
  messageEl.textContent = text;
  messageEl.className = `message ${type}`;
  messageEl.classList.remove("hidden");
}

function hideMessage() {
  messageEl.classList.add("hidden");
  messageEl.textContent = "";
}

function initHourSelect() {
  endHourSelect.innerHTML = ALL_HOURS.map(
    (h) => `<option value="${h}">${h}:00</option>`
  ).join("");
  endHourSelect.value = boot.endHour || "18";
}

function renderQuote(quote) {
  latestQuote = quote;
  if (!quote) {
    quoteEl.classList.add("hidden");
    quoteEl.innerHTML = "";
    submitBtn.disabled = true;
    return;
  }

  const extra = quote.extra_to_pay_kr;
  const unchanged = !!quote.idempotent;
  let paymentHtml = "";
  if (unchanged) {
    paymentHtml = "";
  } else if (extra > 0) {
    paymentHtml = `
      <p class="price-vipps">Betales med Vipps til <strong>${escapeHtml(VIPPS_NUMBER)}</strong>.</p>
      <p class="price-commitment">Ved å trykke «Forleng parkering» bekrefter du forlengelsen og forplikter deg til å betale tilleggsbeløpet via Vipps.</p>
    `;
  } else {
    paymentHtml = `<p class="price-free"><strong>Du kan forlenge parkeringen uten ekstra kostnad.</strong></p>`;
  }

  quoteEl.innerHTML = `
    <p><strong>Opprinnelig periode:</strong> ${escapeHtml(quote.original_period_label)}</p>
    <p><strong>Nytt sluttidspunkt:</strong> ${escapeHtml(formatDateNorwegian(quote.new_end_date))} kl. ${escapeHtml(quote.new_end_hour)}:00</p>
    <p><strong>Ny totalpris:</strong> ${escapeHtml(formatPrice(quote.new_total_price_kr))} kr</p>
    <p><strong>Allerede belastet:</strong> ${escapeHtml(formatPrice(quote.already_charged_kr))} kr</p>
    <p class="price-total"><strong>Ekstra å betale: ${escapeHtml(formatPrice(extra))} kr</strong></p>
    ${paymentHtml}
  `;
  quoteEl.classList.remove("hidden");
  submitBtn.disabled = !quote.available || unchanged;
}

async function previewExtend() {
  hideMessage();
  const endDate = endDateInput.value;
  const endHour = endHourSelect.value;
  if (!endDate || !endHour || !boot.token) {
    renderQuote(null);
    return;
  }

  try {
    const res = await fetch(`/api/reservations/${encodeURIComponent(boot.token)}/preview-extend`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ end_date: endDate, end_hour: endHour }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      renderQuote(null);
      showMessage(data.detail || "Kunne ikke beregne forlengelsen.", "error");
      return;
    }
    if (!data.available && data.conflict_message) {
      showMessage(data.conflict_message, "error");
    }
    renderQuote(data);
  } catch {
    renderQuote(null);
    showMessage("Kunne ikke beregne forlengelsen. Prøv igjen.", "error");
  }
}

function showExtendConfirmation(payload) {
  const quote = payload.quote || {};
  const reservation = payload.reservation || {};
  const extra = quote.extra_to_pay_kr || 0;
  let payment = "";
  if (extra > 0) {
    payment = `<p class="confirmation-payment">Betales med Vipps til <strong>${escapeHtml(quote.vipps_number || VIPPS_NUMBER)}</strong>.</p>`;
  } else {
    payment = `<p class="confirmation-payment"><strong>Ingen ekstra betaling.</strong></p>`;
  }

  messageEl.innerHTML = `
    <p class="confirmation-title">${escapeHtml(payload.message || "Parkeringen er forlenget!")}</p>
    <ul class="confirmation-details">
      <li><span>Opprinnelig</span> ${escapeHtml(quote.original_period_label || "")}</li>
      <li><span>Ny periode</span> ${escapeHtml(quote.new_period_label || reservation.period_label || "")}</li>
      <li><span>Ny totalpris</span> ${escapeHtml(formatPrice(quote.new_total_price_kr || 0))} kr</li>
      <li><span>Allerede belastet</span> ${escapeHtml(formatPrice(quote.already_charged_kr || 0))} kr</li>
      <li><span>Ekstra nå</span> ${escapeHtml(formatPrice(extra))} kr</li>
    </ul>
    ${payment}
  `;
  messageEl.className = "message success confirmation";
  messageEl.classList.remove("hidden");
}

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  hideMessage();
  await previewExtend();
  if (!latestQuote?.available) return;

  submitBtn.disabled = true;
  submitBtn.textContent = "Sender...";

  try {
    const res = await fetch(`/api/reservations/${encodeURIComponent(boot.token)}/extend`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        end_date: endDateInput.value,
        end_hour: endHourSelect.value,
      }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      showMessage(data.detail || "Kunne ikke forlenge parkeringen.", "error");
      await previewExtend();
      return;
    }

    if (data.reservation) {
      endDateInput.min = data.reservation.end_date;
      endDateInput.value = data.reservation.end_date;
      endHourSelect.value = data.reservation.end_hour;
      boot.endDate = data.reservation.end_date;
      boot.endHour = data.reservation.end_hour;
      syncDateDisplay();
    }

    showExtendConfirmation(data);
    await previewExtend();
  } catch {
    showMessage("Kunne ikke forlenge parkeringen. Sjekk nettverket og prøv igjen.", "error");
  } finally {
    submitBtn.textContent = "Forleng parkering";
    submitBtn.disabled = !latestQuote?.available || !!latestQuote?.idempotent;
  }
});

endDateInput.addEventListener("change", () => {
  syncDateDisplay();
  previewExtend();
});
endDateInput.addEventListener("input", syncDateDisplay);
endHourSelect.addEventListener("change", previewExtend);

initHourSelect();
syncDateDisplay();
previewExtend();
