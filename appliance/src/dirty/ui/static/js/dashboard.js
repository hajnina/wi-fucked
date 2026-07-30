/* Airgapped: no external scripts, no build step, no framework.
   Renders the decision journal — the machine explaining itself. */

(function () {
  "use strict";

  var LABELS = {
    activate_backup: "Backup activated",
    allocate_normal: "Using normal connections",
    no_connectivity: "No usable connection",
  };

  var CLASSES = {
    activate_backup: "spend",
    no_connectivity: "none",
  };

  function bps(value) {
    if (typeof value !== "number") return String(value);
    if (Math.abs(value) >= 1000000) return (value / 1000000).toFixed(1) + " Mbps";
    if (Math.abs(value) >= 1000) return (value / 1000).toFixed(0) + " kbps";
    return value + " bps";
  }

  function pretty(key, value) {
    if (value === null || value === undefined) return "—";
    if (/_bps$/.test(key)) return bps(value);
    if (/_ms$/.test(key)) return value + " ms";
    if (/_s$/.test(key)) return value + " s";
    if (/_pct$/.test(key)) return value + "%";
    return String(value);
  }

  function label(key) {
    return key.replace(/_(bps|ms|pct|s)$/, "").replace(/_/g, " ");
  }

  function render(decisions) {
    var host = document.getElementById("decisions");
    if (!host) return;

    if (!decisions.length) {
      host.innerHTML =
        '<p class="empty">Nothing decided yet. Decisions appear here as soon as ' +
        "there is connectivity to reason about.</p>";
      return;
    }

    host.innerHTML = "";
    decisions.forEach(function (d) {
      var card = document.createElement("article");
      card.className = "decision " + (CLASSES[d.action] || "");

      var title = document.createElement("h3");
      title.textContent = LABELS[d.action] || d.action;
      card.appendChild(title);

      var reason = document.createElement("p");
      reason.className = "reason";
      reason.textContent = d.reason;
      card.appendChild(reason);

      var list = document.createElement("dl");
      Object.keys(d.inputs || {}).forEach(function (key) {
        var dt = document.createElement("dt");
        dt.textContent = label(key);
        var dd = document.createElement("dd");
        dd.textContent = pretty(key, d.inputs[key]);
        list.appendChild(dt);
        list.appendChild(dd);
      });
      card.appendChild(list);

      host.appendChild(card);
    });
  }

  function refresh() {
    fetch("/api/decisions?limit=20")
      .then(function (r) {
        return r.ok ? r.json() : [];
      })
      .then(render)
      .catch(function () {
        /* The dashboard must survive the API being briefly unavailable —
           it is most useful precisely when things are going wrong. */
      });
  }

  refresh();
  setInterval(refresh, 5000);
})();
