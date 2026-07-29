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
})();
