"use strict";
const trainingStatus = document.getElementById("training-status");
const uploadResult = document.getElementById("upload-result");
let polling = false;
async function refreshStatus() {
  if (polling || document.hidden) return;
  polling = true;
  try {
    const data = await demo.request("/api/enrollment/status");
    const pieces = [`${data.count} accepted photograph(s); minimum ${data.minimum}.`];
    if (data.job) pieces.push(data.job.message);
    if (data.ready) pieces.push(`Recognition available (${data.mode}).${data.model_current ? "" : " New photos await retraining."}`);
    else if (!data.job) pieces.push("Upload three photos to queue background training.");
    if (data.job && data.job.status === "queued") pieces.push("The separate trainer process must be running.");
    trainingStatus.textContent = pieces.join(" ");
    document.getElementById("retry-training").disabled = data.count < data.minimum || Boolean(data.job && ["queued", "extracting", "training"].includes(data.job.status));
  } catch (error) { trainingStatus.textContent = error.message; }
  finally { polling = false; }
}
document.getElementById("upload-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = document.getElementById("upload-button");
  button.disabled = true;
  uploadResult.textContent = "Validating photographs…";
  try {
    const data = await demo.request("/api/enrollment/photos", {method: "POST", body: new FormData(event.target)});
    uploadResult.replaceChildren();
    const summary = document.createElement("p");
    summary.textContent = `Accepted ${data.accepted.length} photograph(s). ${data.count} total.`;
    uploadResult.append(summary);
    for (const rejected of data.rejected) {
      const line = document.createElement("p");
      line.className = "error";
      line.textContent = `${rejected.name}: ${rejected.reason}`;
      uploadResult.append(line);
    }
    if (data.accepted.length) {
      const link = document.createElement("a");
      link.href = "/enrollment";
      link.textContent = "Refresh accepted photographs";
      uploadResult.append(link);
    }
    event.target.reset();
    await refreshStatus();
  } catch (error) { uploadResult.textContent = error.message; }
  finally { button.disabled = false; }
});
document.getElementById("retry-training").addEventListener("click", async () => {
  try { await demo.json("/api/enrollment/retrain", {}); await refreshStatus(); }
  catch (error) { trainingStatus.textContent = error.message; }
});
refreshStatus();
setInterval(refreshStatus, 2000);
