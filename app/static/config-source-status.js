(() => {
  const sourceControls = [
    {
      label: "Dropbox",
      pillId: "dropbox-status-pill",
      toggleId: "dropbox_source_enabled",
    },
    {
      label: "iGPSPORT",
      pillId: "igpsport-status-pill",
      toggleId: "igpsport_source_enabled",
    },
    {
      label: "COROS",
      pillId: "coros-status-pill",
      toggleId: "coros_source_enabled",
    },
  ];

  const updatePill = ({ label, pillId, toggleId }) => {
    const pill = document.getElementById(pillId);
    const toggle = document.getElementById(toggleId);
    if (!pill || !toggle) {
      return;
    }

    const configured = pill.dataset.configured === "true";
    pill.classList.remove("text-bg-danger", "text-bg-success", "text-bg-warning");

    if (!toggle.checked) {
      pill.classList.add("text-bg-danger");
      pill.textContent = `${label}: Disabled`;
      return;
    }

    pill.classList.add(configured ? "text-bg-success" : "text-bg-warning");
    pill.textContent = `${label}: ${configured ? "Ready" : "Needs setup"}`;
  };

  sourceControls.forEach((source) => {
    const toggle = document.getElementById(source.toggleId);
    if (!toggle) {
      return;
    }
    toggle.addEventListener("change", () => updatePill(source));
    updatePill(source);
  });

  // Accordion auto-open for in-page anchor navigation
  const openTargetAccordion = (hashOrId, focusTargetId = null) => {
    if (!hashOrId) return;
    const cleanId = hashOrId.replace(/^#/, "");
    const target = document.getElementById(cleanId);
    if (!target) return;

    // Find the enclosing or self details element
    const details = target.tagName === "DETAILS" ? target : target.closest("details");
    if (details) {
      details.open = true;
      details.scrollIntoView({ behavior: "smooth", block: "start" });
    }

    // Optionally focus a specific element (e.g., input toggle)
    if (focusTargetId) {
      const focusEl = document.getElementById(focusTargetId);
      if (focusEl) {
        setTimeout(() => {
          focusEl.focus();
          const wrapper = focusEl.closest(".form-check, .form-control, div");
          if (wrapper) {
            wrapper.classList.add("bg-warning-subtle", "p-1", "rounded");
            setTimeout(() => {
              wrapper.classList.remove("bg-warning-subtle", "p-1", "rounded");
            }, 1800);
          }
        }, 250);
      }
    }
  };

  // Intercept anchor clicks pointing to #section-...
  document.addEventListener("click", (e) => {
    const link = e.target.closest("a[href^='#']");
    if (!link) return;

    const href = link.getAttribute("href");
    if (href && href.startsWith("#") && href.length > 1) {
      const focusTarget = link.dataset.focusTarget || null;
      openTargetAccordion(href, focusTarget);
    }
  });

  // Automatically open target accordion if page is loaded with a URL hash
  if (window.location.hash) {
    setTimeout(() => {
      openTargetAccordion(window.location.hash);
    }, 100);
  }

  window.addEventListener("hashchange", () => {
    if (window.location.hash) {
      openTargetAccordion(window.location.hash);
    }
  });
})();
