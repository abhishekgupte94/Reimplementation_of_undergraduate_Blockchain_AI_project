"use strict";

(() => {
  const widget = document.getElementById("ssd-alerts");
  if (!widget) return;

  const banner = document.getElementById("ssd-alert-banner");
  const message = document.getElementById("ssd-alert-message");
  const announcement = document.getElementById("ssd-alert-announcement");
  const acknowledge = document.getElementById("ssd-alert-acknowledge");
  const errorLine = document.getElementById("ssd-alert-error");
  const history = document.getElementById("ssd-alert-history");
  const historyStatus = document.getElementById("ssd-history-status");
  const feed = widget.dataset.feed;
  const formatter = new Intl.DateTimeFormat("en-IE", {
    dateStyle: "medium", timeStyle: "medium", timeZone: widget.dataset.timeZone,
  });
  let timer, refreshRequest, shownAlert, historyState;
  let authorized = true, acknowledging = false;

  function showPending(alert) {
    banner.hidden = !alert;
    if (shownAlert === alert?.id) return;
    shownAlert = alert?.id;
    const text = alert ? `Simulation alert: ${alert.label} detected in Room ${alert.room}.` : "";
    message.textContent = text;
    announcement.textContent = text;
    if (alert) acknowledge.dataset.alertId = alert.id;
  }

  function showHistory(alerts) {
    if (!history) return;
    historyStatus.textContent = alerts.length ? "" : "No SSD alerts have been recorded.";
    historyStatus.hidden = alerts.length > 0;
    const state = JSON.stringify(alerts);
    if (state === historyState) return;
    historyState = state;
    const focusedId = history.contains(document.activeElement) && document.activeElement.dataset.alertId;
    let restoreFocus;
    const rows = alerts.map(alert => {
      const row = document.createElement("tr");
      for (const value of [
        formatter.format(new Date(alert.observed_at)),
        alert.room,
        alert.label,
        `${(alert.confidence * 100).toFixed(1)}%`,
      ]) {
        row.insertCell().textContent = value;
      }
      const status = row.insertCell();
      if (alert.acknowledged) {
        status.textContent = "Acknowledged";
      } else {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "secondary";
        button.dataset.alertId = alert.id;
        button.textContent = `Acknowledge alert ${alert.id}`;
        status.append(button);
        if (button.dataset.alertId === focusedId) restoreFocus = button;
      }
      return row;
    });
    history.replaceChildren(...rows);
    restoreFocus?.focus();
  }

  function endSession(error) {
    if (error.status !== 401 && error.status !== 403) return false;
    authorized = false;
    showPending(null);
    if (history) {
      history.replaceChildren();
      historyStatus.hidden = false;
      historyStatus.textContent = "Sign in as an administrator to view SSD alerts.";
    }
    return true;
  }

  function refresh() {
    clearTimeout(timer);
    if (document.hidden || !authorized || acknowledging || refreshRequest) return;
    refreshRequest = demo.request(history ? feed : `${feed}?pending=1`, {
      signal: AbortSignal.timeout(8000),
    }).then(data => {
      showPending(data.pending);
      if (history) showHistory(data.alerts);
    }).catch(error => {
      if (!endSession(error) && historyStatus) {
        historyStatus.hidden = false;
        historyStatus.textContent = "Could not refresh SSD alerts. Retrying…";
      }
    }).finally(() => {
      refreshRequest = null;
      if (authorized && !document.hidden && !acknowledging) timer = setTimeout(refresh, 3000);
    });
  }

  document.addEventListener("click", async event => {
    const button = event.target.closest("button[data-alert-id]");
    if (!button || !authorized || acknowledging) return;
    const alertId = button.dataset.alertId;
    acknowledging = true;
    clearTimeout(timer);
    button.disabled = true;
    errorLine.hidden = true;
    try {
      // Finish any in-flight poll before changing the alert so it cannot show stale status.
      await refreshRequest;
      if (!authorized) return;
      await demo.json(`${feed}/${alertId}/acknowledge`, {}, {
        signal: AbortSignal.timeout(8000),
      });
      if (String(shownAlert) === alertId) showPending(null);
    } catch (error) {
      if (!endSession(error)) {
        errorLine.textContent = `Could not acknowledge the alert. ${error.message}`;
        errorLine.hidden = false;
      }
    } finally {
      button.disabled = false;
      acknowledging = false;
      refresh();
    }
  });

  document.addEventListener("visibilitychange", refresh);
  window.addEventListener("pagehide", () => clearTimeout(timer));
  window.addEventListener("pageshow", refresh);
  refresh();
})();
