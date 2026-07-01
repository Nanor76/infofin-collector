window.infofinShouldPollResults = () => {
  const status = document.querySelector("#job-status .job-status");
  return !status || status.dataset.terminal !== "true";
};

// Format date to YYYY-MM-DD
function formatISODate(date) {
  const yyyy = date.getFullYear();
  const mm = String(date.getMonth() + 1).padStart(2, '0');
  const dd = String(date.getDate()).padStart(2, '0');
  return `${yyyy}-${mm}-${dd}`;
}

document.addEventListener("DOMContentLoaded", () => {
  // Initialize Lucide icons on load
  if (window.lucide) {
    window.lucide.createIcons();
  }

  // Set default dates if empty
  const dateFromInput = document.getElementById("date_from");
  const dateToInput = document.getElementById("date_to");
  
  if (dateFromInput && dateToInput) {
    const today = new Date();
    // Default to 30 days ago if not set
    if (!dateToInput.value) {
      dateToInput.value = formatISODate(today);
    }
    if (!dateFromInput.value) {
      const thirtyDaysAgo = new Date();
      thirtyDaysAgo.setDate(today.getDate() - 30);
      dateFromInput.value = formatISODate(thirtyDaysAgo);
    }
  }

  // Handle Date Presets
  const presetButtons = document.querySelectorAll(".btn-preset");
  presetButtons.forEach(btn => {
    btn.addEventListener("click", () => {
      presetButtons.forEach(b => b.classList.remove("active"));
      btn.classList.add("active");

      const preset = btn.dataset.preset;
      if (preset === "custom") {
        return;
      }

      const days = parseInt(preset, 10);
      const today = new Date();
      const startDate = new Date();
      startDate.setDate(today.getDate() - days);

      if (dateFromInput && dateToInput) {
        dateToInput.value = formatISODate(today);
        dateFromInput.value = formatISODate(startDate);
      }
    });
  });

  // If user manually changes date inputs, set preset to custom
  if (dateFromInput && dateToInput) {
    const setCustomActive = () => {
      presetButtons.forEach(b => {
        if (b.dataset.preset === "custom") {
          b.classList.add("active");
        } else {
          b.classList.remove("active");
        }
      });
    };
    dateFromInput.addEventListener("input", setCustomActive);
    dateToInput.addEventListener("input", setCustomActive);
  }

  // Handle Form Submission
  const searchForm = document.getElementById("search-form");
  if (searchForm) {
    searchForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const formData = new FormData(searchForm);
      const markets = formData.getAll("markets");
      if (!markets.length) {
        alert("Veuillez sélectionner au moins un marché.");
        return;
      }
      
      const payload = {
        markets,
        date_from: formData.get("date_from"),
        date_to: formData.get("date_to"),
        document_types: formData.getAll("document_types"),
        query: formData.get("query") || null,
        issuer_isin: formData.get("issuer_isin") || null,
        dedupe_url: true,
      };

      // Add loading state to button
      const submitBtn = searchForm.querySelector("button[type='submit']");
      const originalText = submitBtn.innerHTML;
      submitBtn.disabled = true;
      submitBtn.innerHTML = `<i data-lucide="loader-2" class="spin-icon"></i> <span>Recherche en cours...</span>`;
      if (window.lucide) {
        window.lucide.createIcons();
      }

      try {
        const response = await fetch("/api/searches", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });
        if (!response.ok) {
          const error = await response.json().catch(() => ({}));
          alert(error.detail || "Échec du lancement de la recherche.");
          submitBtn.disabled = false;
          submitBtn.innerHTML = originalText;
          if (window.lucide) {
            window.lucide.createIcons();
          }
          return;
        }
        const data = await response.json();
        window.location.href = `/searches/${data.job_id}`;
      } catch (err) {
        console.error(err);
        alert("Une erreur est survenue lors de la communication avec le serveur.");
        submitBtn.disabled = false;
        submitBtn.innerHTML = originalText;
        if (window.lucide) {
          window.lucide.createIcons();
        }
      }
    });
  }

  // Clipboard copy click handling
  document.body.addEventListener("click", async (event) => {
    const target = event.target;
    if (!(target instanceof HTMLElement)) {
      return;
    }
    
    // Use closest to match button even when clicking SVG icon/span inside
    const copyButton = target.closest(".copy-link");
    if (!copyButton) {
      return;
    }

    const url = copyButton.dataset.url;
    if (!url || !navigator.clipboard) {
      return;
    }

    try {
      await navigator.clipboard.writeText(url);
      
      // Visual feedback
      const originalHTML = copyButton.innerHTML;
      copyButton.innerHTML = `<i data-lucide="check"></i>`;
      copyButton.classList.add("copied");
      if (window.lucide) {
        window.lucide.createIcons();
      }

      setTimeout(() => {
        copyButton.innerHTML = originalHTML;
        copyButton.classList.remove("copied");
        if (window.lucide) {
          window.lucide.createIcons();
        }
      }, 2000);
    } catch (err) {
      console.error("Clipboard copy failed:", err);
    }
  });
});

// Refresh Lucide icons when HTMX swaps content
document.body.addEventListener("htmx:afterSwap", () => {
  if (window.lucide) {
    window.lucide.createIcons();
  }
});
