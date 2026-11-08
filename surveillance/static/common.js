"use strict";
window.demo = {
  csrf: () => document.querySelector('meta[name="csrf-token"]').content,
  async request(url, options = {}) {
    const headers = {"X-CSRFToken": this.csrf(), ...options.headers};
    const response = await fetch(url, {...options, headers});
    const data = await response.json().catch(() => ({error: "The server returned an unexpected response. Reload the page."}));
    if (!response.ok) {
      const error = new Error(data.error || `Request failed (${response.status}).`);
      error.status = response.status;
      throw error;
    }
    return data;
  },
  json(url, data, options = {}) {
    return this.request(url, {...options, method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(data)});
  }
};
