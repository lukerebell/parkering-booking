async function copyTextToClipboard(text) {
  if (navigator.clipboard && window.isSecureContext) {
    await navigator.clipboard.writeText(text);
    return true;
  }

  const ta = document.createElement("textarea");
  ta.value = text;
  ta.setAttribute("readonly", "");
  ta.style.position = "absolute";
  ta.style.left = "-9999px";
  document.body.appendChild(ta);
  ta.select();
  const copied = document.execCommand("copy");
  document.body.removeChild(ta);
  if (!copied) {
    throw new Error("copy-failed");
  }
  return true;
}

function initAdminCopyButtons() {
  document.querySelectorAll(".admin-copy-link-btn").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const stack = btn.closest(".admin-action-stack");
      const feedback = stack?.querySelector(".admin-copy-feedback");
      const manual = stack?.querySelector(".admin-manual-link");
      const url = btn.getAttribute("data-url") || "";

      if (!url) {
        if (feedback) {
          feedback.textContent = "Fant ikke administrasjonslenken.";
          feedback.classList.remove("hidden");
        }
        return;
      }

      try {
        await copyTextToClipboard(url);
        if (feedback) {
          feedback.textContent = "Lenken er kopiert!";
          feedback.classList.remove("hidden");
        }
        if (manual) {
          manual.classList.add("hidden");
        }
      } catch {
        if (feedback) {
          feedback.textContent =
            "Kunne ikke kopiere automatisk. Marker og kopier lenken manuelt.";
          feedback.classList.remove("hidden");
        }
        if (manual) {
          manual.classList.remove("hidden");
        }
      }
    });
  });
}

initAdminCopyButtons();
