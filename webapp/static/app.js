document.addEventListener("DOMContentLoaded", () => {
  const searchForm = document.getElementById("search-form");
  if (searchForm) {
    searchForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const formData = new FormData(searchForm);
      const markets = formData.getAll("markets");
      if (!markets.length) {
        alert("Sélectionnez au moins un marché.");
        return;
      }
      const payload = {
        markets,
        date_from: formData.get("date_from"),
        date_to: formData.get("date_to"),
        document_types: formData.getAll("document_types"),
        query: formData.get("query") || null,
        issuer_isin: formData.get("issuer_isin") || null,
        dedupe_url: formData.get("dedupe_url") === "on",
      };
      const response = await fetch("/api/searches", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (!response.ok) {
        const error = await response.json().catch(() => ({}));
        alert(error.detail || "Échec de la création de la recherche.");
        return;
      }
      const data = await response.json();
      window.location.href = `/searches/${data.job_id}`;
    });
  }

  document.body.addEventListener("click", async (event) => {
    const target = event.target;
    if (!(target instanceof HTMLElement)) {
      return;
    }
    if (!target.classList.contains("copy-link")) {
      return;
    }
    const url = target.dataset.url;
    if (!url || !navigator.clipboard) {
      return;
    }
    await navigator.clipboard.writeText(url);
    target.textContent = "Copié";
    setTimeout(() => {
      target.textContent = "Copier";
    }, 1200);
  });
});