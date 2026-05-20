const form = document.getElementById("booking-form");
const dateFromInput = document.getElementById("booking_date_from");
const dateToInput = document.getElementById("booking_date_to");
const dateFromDisplay = document.getElementById("booking_date_from_display");
const dateToDisplay = document.getElementById("booking_date_to_display");
const dateFromBtn = document.getElementById("booking_date_from_btn");
const dateToBtn = document.getElementById("booking_date_to_btn");
const hourFromSelect = document.getElementById("hour_from");
const hourToSelect = document.getElementById("hour_to");
const periodNotice = document.getElementById("period-notice");
const messageEl = document.getElementById("message");
const submitBtn = document.getElementById("submit-btn");

const ALL_HOURS = Array.from({ length: 24 }, (_, i) => String(i).padStart(2, "0"));
const MAX_BOOKING_HOURS = 96;

let rangeCheck = null;

function setMinDate() {
  const today = new Date();
  const min = `${today.getFullYear()}-${String(today.getMonth() + 1).padStart(2, "0")}-${String(today.getDate()).padStart(2, "0")}`;
  dateFromInput.min = min;
  dateToInput.min = min;
}

function syncDateDisplay(nativeInput, displayInput) {
  displayInput.value = nativeInput.value ? formatDateNorwegian(nativeInput.value) : "";
}

function syncAllDateDisplays() {
  syncDateDisplay(dateFromInput, dateFromDisplay);
  syncDateDisplay(dateToInput, dateToDisplay);
}

function openDatePicker(nativeInput) {
  if (typeof nativeInput.showPicker === "function") {
    try {
      nativeInput.showPicker();
      return;
    } catch {
      /* fall through */
    }
  }
  nativeInput.focus();
  nativeInput.click();
}

function initDatePickers() {
  const pairs = [
    [dateFromInput, dateFromDisplay, dateFromBtn],
    [dateToInput, dateToDisplay, dateToBtn],
  ];

  for (const [nativeInput, displayInput, pickerBtn] of pairs) {
    const open = () => openDatePicker(nativeInput);

    pickerBtn.addEventListener("click", open);
    displayInput.addEventListener("click", open);
    nativeInput.addEventListener("change", () => {
      syncDateDisplay(nativeInput, displayInput);
      onDateChange();
    });
    nativeInput.addEventListener("input", () => syncDateDisplay(nativeInput, displayInput));
  }

  syncAllDateDisplays();
}

function formatPeriodLabel(dateFrom, hourFrom, dateTo, hourTo) {
  return `${formatDateNorwegian(dateFrom)} kl. ${hourFrom}:00 – ${formatDateNorwegian(dateTo)} kl. ${hourTo}:00`;
}

function letterCount(value) {
  const matches = value.match(/[a-zA-ZæøåÆØÅ]/g);
  return matches ? matches.length : 0;
}

function phoneDigits(value) {
  return value.replace(/\D/g, "");
}

function isValidCarReg(value) {
  return /^[A-ZÆØÅ]{2}\d{5}$/u.test(value.toUpperCase());
}

function validateContactFields(name, phone, carReg) {
  if (!name || !phone || !carReg) {
    return "Fyll ut alle felter";
  }
  if (letterCount(name) < 2) {
    return "Navn må ha minst to bokstaver.";
  }
  const digits = phoneDigits(phone);
  if (digits.length !== 8) {
    return "Telefon må ha åtte siffer.";
  }
  if (!isValidCarReg(carReg)) {
    return "Reg.nr må ha to bokstaver og fem siffer.";
  }
  return null;
}

function escapeHtml(text) {
  return String(text)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function formatDateNorwegian(isoDate) {
  const [year, month, day] = isoDate.split("-");
  return `${String(day).padStart(2, "0")}.${String(month).padStart(2, "0")}.${year}`;
}

function hourEndLabel(hour) {
  const next = (parseInt(hour, 10) + 1) % 24;
  return String(next).padStart(2, "0");
}

function formatHoursList(hours) {
  if (!hours.length) return "";
  const sorted = [...hours].sort();
  const ranges = [];
  let rangeStart = sorted[0];
  let rangeEnd = sorted[0];

  for (let i = 1; i < sorted.length; i++) {
    const current = parseInt(sorted[i], 10);
    const previous = parseInt(sorted[i - 1], 10);
    if (current === previous + 1) {
      rangeEnd = sorted[i];
    } else {
      ranges.push([rangeStart, rangeEnd]);
      rangeStart = sorted[i];
      rangeEnd = sorted[i];
    }
  }
  ranges.push([rangeStart, rangeEnd]);
  return ranges
    .map(([start, end]) => `${start}:00 – ${hourEndLabel(end)}:00`)
    .join(", ");
}

function groupSlotsByDate(slots) {
  const byDate = {};
  slots.forEach(({ date, hour }) => {
    if (!byDate[date]) byDate[date] = [];
    byDate[date].push(hour);
  });
  Object.keys(byDate).forEach((d) => byDate[d].sort());
  return byDate;
}

function revealMessage() {
  messageEl.classList.remove("hidden");
  requestAnimationFrame(() => {
    messageEl.scrollIntoView({ behavior: "smooth", block: "nearest" });
  });
}

function showMessage(text, type) {
  messageEl.textContent = text;
  messageEl.className = `message ${type}`;
  revealMessage();
}

function hideMessage() {
  messageEl.classList.add("hidden");
}

function showConfirmation(booking) {
  messageEl.innerHTML = `
    <p class="confirmation-title">Plassen er booket!</p>
    <ul class="confirmation-details">
      <li><span>Navn</span> ${escapeHtml(booking.name)}</li>
      <li><span>Telefon</span> ${escapeHtml(booking.phone)}</li>
      <li><span>Reg.nr</span> ${escapeHtml(booking.car_reg)}</li>
      <li><span>Periode</span> ${escapeHtml(
        formatPeriodLabel(
          booking.booking_date_from,
          booking.hour_from,
          booking.booking_date_to,
          booking.hour_to
        )
      )}</li>
      <li><span>Timer</span> ${escapeHtml(String(booking.slot_count))} time(r)</li>
    </ul>
  `;
  messageEl.className = "message success confirmation";
  revealMessage();
}

function hidePeriodNotice() {
  periodNotice.classList.add("hidden");
  periodNotice.innerHTML = "";
}

function updatePeriodNotice(bookedSlots, dateFrom, dateTo, slotCount) {
  const overLimit = slotCount > MAX_BOOKING_HOURS;

  if (!overLimit && !bookedSlots.length) {
    hidePeriodNotice();
    return;
  }

  let html = "";
  if (overLimit) {
    html +=
      '<p class="booking-limit">Plassen kan bookes maks fire dager (96 timer) om gangen.</p>';
  }

  if (bookedSlots.length) {
    const byDate = groupSlotsByDate(bookedSlots);
    const dates = Object.keys(byDate).sort();
    const items = dates
      .map((date) => {
        const hoursLabel = formatHoursList(byDate[date]);
        return `<li><strong>${escapeHtml(formatDateNorwegian(date))}:</strong> ${escapeHtml(hoursLabel)}</li>`;
      })
      .join("");

    const rangeLabel =
      dateFrom === dateTo
        ? formatDateNorwegian(dateFrom)
        : `${formatDateNorwegian(dateFrom)} – ${formatDateNorwegian(dateTo)}`;

    html += `
      <div class="${overLimit ? "booked-slots-section" : ""}">
        <strong>Allerede booket ${escapeHtml(rangeLabel)} (${bookedSlots.length} timer):</strong>
        <ul class="booked-slots-list">${items}</ul>
      </div>
    `;
  }

  periodNotice.innerHTML = html;
  periodNotice.classList.remove("hidden");
}

function initHourSelects() {
  const options = ALL_HOURS.map((h) => `<option value="${h}">${h}:00</option>`).join("");
  hourFromSelect.innerHTML = options;
  hourToSelect.innerHTML = options;
  hourFromSelect.disabled = false;
  hourToSelect.disabled = false;
  hourFromSelect.value = "08";
  hourToSelect.value = "18";
}

function resetHourSelects() {
  hourFromSelect.innerHTML = '<option value="">–</option>';
  hourToSelect.innerHTML = '<option value="">–</option>';
  hourFromSelect.disabled = true;
  hourToSelect.disabled = true;
  rangeCheck = null;
  submitBtn.disabled = true;
}

function syncDateConstraints() {
  if (dateFromInput.value) {
    dateToInput.min = dateFromInput.value;
    if (dateToInput.value && dateToInput.value < dateFromInput.value) {
      dateToInput.value = dateFromInput.value;
      syncDateDisplay(dateToInput, dateToDisplay);
    }
  }
}

async function checkRange() {
  const dateFrom = dateFromInput.value;
  const dateTo = dateToInput.value || dateFrom;
  const hourFrom = hourFromSelect.value;
  const hourTo = hourToSelect.value;

  if (!dateFrom || !dateTo || !hourFrom || !hourTo) {
    rangeCheck = null;
    submitBtn.disabled = true;
    return;
  }

  if (dateTo < dateFrom) {
    showMessage("Til-dato kan ikke være før fra-dato.", "error");
    submitBtn.disabled = true;
    return;
  }

  try {
    const params = new URLSearchParams({
      date_from: dateFrom,
      date_to: dateTo,
      hour_from: hourFrom,
      hour_to: hourTo,
    });
    const res = await fetch(`/available-times?${params}`);
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || "Kunne ikke sjekke perioden.");
    }

    rangeCheck = await res.json();

    updatePeriodNotice(
      rangeCheck.booked_slots || [],
      dateFrom,
      dateTo,
      rangeCheck.slot_count
    );

    if (rangeCheck.slot_count > MAX_BOOKING_HOURS) {
      hideMessage();
      submitBtn.disabled = true;
      return;
    }

    if (rangeCheck.available) {
      hideMessage();
      submitBtn.disabled = false;
    } else {
      hideMessage();
      submitBtn.disabled = true;
    }
  } catch (err) {
    rangeCheck = null;
    submitBtn.disabled = true;
    showMessage(err.message || "Kunne ikke sjekke perioden.", "error");
  }
}

async function onDateChange() {
  hideMessage();
  if (dateFromInput.value && !dateToInput.value) {
    dateToInput.value = dateFromInput.value;
    syncDateDisplay(dateToInput, dateToDisplay);
  }
  syncAllDateDisplays();
  syncDateConstraints();

  if (dateFromInput.value && dateToInput.value) {
    if (!hourFromSelect.options.length || hourFromSelect.disabled) {
      initHourSelects();
    }
    await checkRange();
  } else {
    resetHourSelects();
  }
}

hourFromSelect.addEventListener("change", checkRange);
hourToSelect.addEventListener("change", checkRange);

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  hideMessage();

  const dateFrom = dateFromInput.value;
  const dateTo = dateToInput.value || dateFrom;
  const hourFrom = hourFromSelect.value;
  const hourTo = hourToSelect.value;

  if (!dateFrom || !dateTo) {
    showMessage("Velg fra- og til-dato.", "error");
    return;
  }

  if (!hourFrom || !hourTo) {
    showMessage("Velg klokkeslett.", "error");
    return;
  }

  await checkRange();
  if (!rangeCheck?.available) {
    return;
  }

  const name = document.getElementById("name").value.trim();
  const phone = document.getElementById("phone").value.trim();
  const carReg = document.getElementById("car_reg").value.trim();

  const contactError = validateContactFields(name, phone, carReg);
  if (contactError) {
    showMessage(contactError, "error");
    return;
  }

  const payload = {
    name,
    phone: phoneDigits(phone),
    car_reg: carReg.toUpperCase(),
    booking_date_from: dateFrom,
    booking_date_to: dateTo,
    hour_from: hourFrom,
    hour_to: hourTo,
  };

  submitBtn.disabled = true;
  submitBtn.textContent = "Sender...";

  try {
    const res = await fetch("/bookings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });

    const data = await res.json().catch(() => ({}));

    if (res.ok) {
      if (data.booking) showConfirmation(data.booking);
      else showMessage(data.message || "Plassen er booket!", "success");
      form.reset();
      setMinDate();
      syncAllDateDisplays();
      resetHourSelects();
      hidePeriodNotice();
    } else if (res.status === 409) {
      showMessage(
        data.detail ||
          "Plassen er allerede booket i dette tidsrommet. Velg et annet tidsrom.",
        "error"
      );
      await checkRange();
    } else if (res.status === 422) {
      showMessage(
        typeof data.detail === "string" ? data.detail : "Fyll ut alle felter",
        "error"
      );
    } else {
      const detail =
        typeof data.detail === "string"
          ? data.detail
          : "Noe gikk galt. Prøv igjen.";
      showMessage(detail, "error");
    }
  } catch {
    showMessage("Kunne ikke sende booking. Sjekk nettverket og prøv igjen.", "error");
  } finally {
    submitBtn.disabled = !rangeCheck?.available;
    submitBtn.textContent = "Book plass";
  }
});

setMinDate();
initDatePickers();
