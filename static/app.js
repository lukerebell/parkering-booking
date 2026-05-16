const form = document.getElementById("booking-form");
const dateInput = document.getElementById("booking_date");
const hourOptions = document.getElementById("hour-options");
const messageEl = document.getElementById("message");
const submitBtn = document.getElementById("submit-btn");

function setMinDate() {
  const today = new Date();
  const yyyy = today.getFullYear();
  const mm = String(today.getMonth() + 1).padStart(2, "0");
  const dd = String(today.getDate()).padStart(2, "0");
  dateInput.min = `${yyyy}-${mm}-${dd}`;
}

function escapeHtml(text) {
  return String(text)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

const MONTHS_NO = [
  "januar", "februar", "mars", "april", "mai", "juni",
  "juli", "august", "september", "oktober", "november", "desember",
];

function formatDateNorwegian(isoDate) {
  const [year, month, day] = isoDate.split("-");
  return `${parseInt(day, 10)}. ${MONTHS_NO[parseInt(month, 10) - 1]} ${year}`;
}

function showMessage(text, type) {
  messageEl.textContent = text;
  messageEl.className = `message ${type}`;
  messageEl.classList.remove("hidden");
}

function hourEndLabel(hour) {
  const next = (parseInt(hour, 10) + 1) % 24;
  return String(next).padStart(2, "0");
}

function formatHourLabel(hour) {
  return `${hour}:00 – ${hourEndLabel(hour)}:00`;
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

function showConfirmation(booking) {
  const dateLabel = formatDateNorwegian(booking.booking_date);
  const hours = booking.booking_hours || [];
  const hoursLabel = formatHoursList(hours);

  messageEl.innerHTML = `
    <p class="confirmation-title">Plassen er booket!</p>
    <ul class="confirmation-details">
      <li><span>Navn</span> ${escapeHtml(booking.name)}</li>
      <li><span>Telefon</span> ${escapeHtml(booking.phone)}</li>
      <li><span>Reg.nr</span> ${escapeHtml(booking.car_reg)}</li>
      <li><span>Dato</span> ${escapeHtml(dateLabel)}</li>
      <li><span>Timer</span> ${escapeHtml(hoursLabel)}</li>
    </ul>
  `;
  messageEl.className = "message success";
  messageEl.classList.remove("hidden");
}

function hideMessage() {
  messageEl.classList.add("hidden");
}

function getSelectedHours() {
  return [...hourOptions.querySelectorAll('input[type="checkbox"]:checked')].map(
    (el) => el.value
  );
}

function setHourOptionsDisabled(disabled) {
  hourOptions.classList.toggle("disabled", disabled);
  hourOptions.querySelectorAll("input").forEach((input) => {
    input.disabled = disabled;
  });
}

function renderHourPlaceholder(text) {
  hourOptions.innerHTML = `<p class="hour-placeholder">${text}</p>`;
  setHourOptionsDisabled(true);
  submitBtn.disabled = true;
}

async function loadAvailableTimes(date) {
  submitBtn.disabled = true;

  if (!date) {
    renderHourPlaceholder("Velg dato først");
    return;
  }

  renderHourPlaceholder("Laster tider...");

  try {
    const res = await fetch(`/available-times?date=${encodeURIComponent(date)}`);
    if (!res.ok) throw new Error("Kunne ikke hente ledige tider.");

    const data = await res.json();
    const times = data.available_times || [];

    if (times.length === 0) {
      renderHourPlaceholder("Ingen ledige tider denne dagen");
      return;
    }

    hourOptions.innerHTML = "";
    times.forEach((hour) => {
      const id = `hour-${hour}`;
      const label = document.createElement("label");
      label.className = "hour-option";
      label.htmlFor = id;
      label.innerHTML = `
        <input type="checkbox" id="${id}" name="booking_hours" value="${hour}">
        <span>${formatHourLabel(hour)}</span>
      `;
      hourOptions.appendChild(label);
    });
    setHourOptionsDisabled(false);
    submitBtn.disabled = false;
  } catch {
    renderHourPlaceholder("Feil ved lasting av tider");
    showMessage("Kunne ikke hente ledige tider. Prøv igjen.", "error");
  }
}

dateInput.addEventListener("change", () => {
  hideMessage();
  loadAvailableTimes(dateInput.value);
});

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  hideMessage();

  const selectedHours = getSelectedHours();

  const payload = {
    name: document.getElementById("name").value.trim(),
    phone: document.getElementById("phone").value.trim(),
    car_reg: document.getElementById("car_reg").value.trim(),
    booking_date: dateInput.value,
    booking_hours: selectedHours,
  };

  if (selectedHours.length === 0) {
    showMessage("Velg minst én ledig time.", "error");
    return;
  }

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
      if (data.booking) {
        showConfirmation(data.booking);
      } else {
        showMessage(data.message || "Plassen er booket!", "success");
      }
      form.reset();
      setMinDate();
      renderHourPlaceholder("Velg dato først");
    } else if (res.status === 409) {
      showMessage(
        data.detail || "Minst én av timene er allerede booket. Velg andre timer.",
        "error"
      );
      await loadAvailableTimes(dateInput.value);
    } else {
      const detail = Array.isArray(data.detail)
        ? data.detail.map((d) => d.msg).join(" ")
        : data.detail;
      showMessage(detail || "Noe gikk galt. Prøv igjen.", "error");
    }
  } catch {
    showMessage("Kunne ikke sende booking. Sjekk nettverket og prøv igjen.", "error");
  } finally {
    submitBtn.disabled = false;
    submitBtn.textContent = "Book plass 44";
  }
});

setMinDate();
