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

  // --- Market Selection Map & List Interaction ---
  const searchInput = document.getElementById('market-search');
  if (searchInput) {
    searchInput.addEventListener('input', () => {
      const query = searchInput.value.toLowerCase().normalize("NFD").replace(/[\u0300-\u036f]/g, "");
      const rows = document.querySelectorAll('.market-row');
      rows.forEach(row => {
        const city = row.dataset.city.toLowerCase().normalize("NFD").replace(/[\u0300-\u036f]/g, "");
        const name = row.dataset.name.toLowerCase().normalize("NFD").replace(/[\u0300-\u036f]/g, "");
        if (city.includes(query) || name.includes(query)) {
          row.style.display = 'flex';
        } else {
          row.style.display = 'none';
        }
      });
    });
  }

  const interactiveCountryCodes = [
    "FR", "NO", "IT", "NL", "BE", "PT", "IE", "ES", "SE", "DK", "FI", "AT", "PL", "CZ", "HR", "SI", "EE", "LV", "LT", "SK", "RO", "BG", "MT"
  ];

  const mapWrapper = document.getElementById('europe-map-svg');
  const mapLoading = document.getElementById('map-loading');

  function getMarketsByCountryCode(code) {
    const markets = [];
    const rows = document.querySelectorAll(`.market-row[data-code="${code}"]`);
    rows.forEach(row => {
      markets.push(row.dataset.name);
    });
    return markets;
  }

  function updatePathSelectionState(path, code) {
    const checkboxes = document.querySelectorAll(`.market-row[data-code="${code}"] input[type="checkbox"]`);
    let anyChecked = false;
    checkboxes.forEach(cb => {
      if (cb.checked) anyChecked = true;
    });
    
    if (anyChecked) {
      path.classList.add('country-selected');
    } else {
      path.classList.remove('country-selected');
    }
  }

  function toggleCountryMarkets(code) {
    const checkboxes = document.querySelectorAll(`.market-row[data-code="${code}"] input[type="checkbox"]`);
    if (!checkboxes.length) return;
    
    // Determine if all are checked
    let allChecked = true;
    checkboxes.forEach(cb => {
      if (!cb.checked) allChecked = false;
    });
    
    // Toggle all
    checkboxes.forEach(cb => {
      cb.checked = !allChecked;
    });
    
    // Update path class
    const path = document.getElementById(code);
    if (path) {
      updatePathSelectionState(path, code);
    }
  }

  function setupInteractiveMap() {
    if (!mapWrapper) return;
    const svgElement = mapWrapper.querySelector('svg');
    if (!svgElement) return;
    
    // Make responsive by removing fixed width/height attributes
    svgElement.removeAttribute('width');
    svgElement.removeAttribute('height');
    svgElement.style.width = '100%';
    svgElement.style.height = '100%';
    svgElement.style.maxHeight = '100%';
    svgElement.style.maxWidth = '100%';
    svgElement.setAttribute('data-testid', 'search-map-canvas');

    // Wrap all children of the SVG in a viewport group for panning/zooming
    let viewport = svgElement.querySelector('#map-viewport');
    if (!viewport) {
      viewport = document.createElementNS("http://www.w3.org/2000/svg", "g");
      viewport.setAttribute('id', 'map-viewport');
      while (svgElement.firstChild) {
        viewport.appendChild(svgElement.firstChild);
      }
      svgElement.appendChild(viewport);
    }
    
    // Zoom and Pan State
    let zoom = 1.0;
    let panX = 0;
    let panY = 0;
    let isPanning = false;
    let startX = 0;
    let startY = 0;
    let dragDistance = 0;
    let clickStartX = 0;
    let clickStartY = 0;

    // Viewport center constants based on viewBox="380.0 175.0 238.0 183.0"
    const cx = 499.0;
    const cy = 266.5;

    function updateTransform() {
      // Zoom relative to center (cx, cy)
      viewport.setAttribute('transform', `translate(${panX}, ${panY}) translate(${cx}, ${cy}) scale(${zoom}) translate(${-cx}, ${-cy})`);
    }

    // Drag-to-Pan event listeners
    svgElement.addEventListener('mousedown', (e) => {
      if (e.button !== 0) return; // Only left click
      isPanning = true;
      clickStartX = e.clientX;
      clickStartY = e.clientY;
      startX = e.clientX - panX;
      startY = e.clientY - panY;
      svgElement.style.cursor = 'grabbing';
      e.preventDefault();
    });

    window.addEventListener('mousemove', (e) => {
      if (!isPanning) return;
      panX = e.clientX - startX;
      panY = e.clientY - startY;
      updateTransform();
    });

    window.addEventListener('mouseup', (e) => {
      if (!isPanning) return;
      isPanning = false;
      svgElement.style.cursor = 'grab';
      
      const dx = e.clientX - clickStartX;
      const dy = e.clientY - clickStartY;
      dragDistance = Math.sqrt(dx * dx + dy * dy);
    });

    // Mouse Wheel Zoom
    svgElement.addEventListener('wheel', (e) => {
      e.preventDefault();
      const zoomFactor = 1.1;
      if (e.deltaY < 0) {
        zoom = Math.min(5.0, zoom * zoomFactor);
      } else {
        zoom = Math.max(0.6, zoom / zoomFactor);
      }
      updateTransform();
    }, { passive: false });

    // Floating UI Zoom Controls
    const btnZoomIn = document.getElementById('map-zoom-in');
    const btnZoomOut = document.getElementById('map-zoom-out');
    const btnZoomReset = document.getElementById('map-zoom-reset');

    if (btnZoomIn) {
      btnZoomIn.addEventListener('click', (e) => {
        e.preventDefault();
        zoom = Math.min(5.0, zoom * 1.25);
        updateTransform();
      });
    }

    if (btnZoomOut) {
      btnZoomOut.addEventListener('click', (e) => {
        e.preventDefault();
        zoom = Math.max(0.6, zoom / 1.25);
        updateTransform();
      });
    }

    if (btnZoomReset) {
      btnZoomReset.addEventListener('click', (e) => {
        e.preventDefault();
        zoom = 1.0;
        panX = 0;
        panY = 0;
        updateTransform();
      });
    }
    
    const paths = viewport.querySelectorAll('path');
    const tooltip = document.getElementById('map-tooltip');
    
    paths.forEach(path => {
      const code = path.getAttribute('id');
      if (!code) return;
      
      const isInteractive = interactiveCountryCodes.includes(code);
      if (isInteractive) {
        path.classList.add('country-interactive');
        path.setAttribute('data-testid', `search-map-country-${code.toLowerCase()}`);
        
        // Initial state sync
        updatePathSelectionState(path, code);
        
        // Hover
        path.addEventListener('mouseenter', (e) => {
          const title = path.getAttribute('title') || code;
          const countryMarkets = getMarketsByCountryCode(code);
          const listItemsHtml = countryMarkets.map(m => `<li>${m}</li>`).join('');
          
          if (tooltip) {
            tooltip.innerHTML = `
              <div class="tooltip-title">${title} (${code})</div>
              <ul class="tooltip-markets">${listItemsHtml}</ul>
              <div class="tooltip-hint">Clic pour basculer la sélection</div>
            `;
            tooltip.style.opacity = '1';
          }
        });
        
        path.addEventListener('mousemove', (e) => {
          if (tooltip) {
            const mapBounds = mapWrapper.getBoundingClientRect();
            tooltip.style.left = `${e.clientX - mapBounds.left + 15}px`;
            tooltip.style.top = `${e.clientY - mapBounds.top + 15}px`;
          }
        });
        
        path.addEventListener('mouseleave', () => {
          if (tooltip) tooltip.style.opacity = '0';
        });
        
        // Click
        path.addEventListener('click', (e) => {
          if (dragDistance > 5) return; // Prevent click during pan drag
          toggleCountryMarkets(code);
        });
      } else {
        // Gray out non-supported country paths
        path.style.opacity = '0.35';
        path.style.pointerEvents = 'none';
      }
    });
  }

  if (mapWrapper) {
    fetch('/static/europe.svg?v=2.2')
      .then(res => {
        if (!res.ok) throw new Error("Erreur de chargement");
        return res.text();
      })
      .then(svgText => {
        if (mapLoading) mapLoading.style.display = 'none';
        mapWrapper.innerHTML = svgText;
        setupInteractiveMap();
        if (window.lucide) {
          window.lucide.createIcons();
        }
      })
      .catch(err => {
        console.error(err);
        if (mapLoading) {
          mapLoading.innerHTML = `<span style="color:var(--error); font-size: 11px;"><i data-lucide="alert-triangle"></i> Échec du chargement de la carte</span>`;
          if (window.lucide) window.lucide.createIcons();
        }
      });
  }

  // Listen to list changes to sync back to map paths
  document.addEventListener('change', (e) => {
    if (e.target && e.target.classList.contains('market-input')) {
      const row = e.target.closest('.market-row');
      if (row) {
        const code = row.dataset.code;
        const path = document.getElementById(code);
        if (path) {
          updatePathSelectionState(path, code);
        }
      }
    }
  });

  // Quick action buttons
  const btnAll = document.getElementById('btn-select-all');
  const btnNone = document.getElementById('btn-select-none');

  if (btnAll) {
    btnAll.addEventListener('click', () => {
      const checkboxes = document.querySelectorAll('.market-input');
      checkboxes.forEach(cb => cb.checked = true);
      
      interactiveCountryCodes.forEach(code => {
        const path = document.getElementById(code);
        if (path) path.classList.add('country-selected');
      });
    });
  }

  if (btnNone) {
    btnNone.addEventListener('click', () => {
      const checkboxes = document.querySelectorAll('.market-input');
      checkboxes.forEach(cb => cb.checked = false);
      
      interactiveCountryCodes.forEach(code => {
        const path = document.getElementById(code);
        if (path) path.classList.remove('country-selected');
      });
    });
  }
});

// Refresh Lucide icons when HTMX swaps content
document.body.addEventListener("htmx:afterSwap", () => {
  if (window.lucide) {
    window.lucide.createIcons();
  }
});
