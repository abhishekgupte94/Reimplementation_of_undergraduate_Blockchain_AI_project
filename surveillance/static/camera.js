"use strict";
const video = document.getElementById("webcam");
const overlay = document.getElementById("overlay");
const statusLine = document.getElementById("camera-status");
const startButton = document.getElementById("start-camera");
const stopButton = document.getElementById("stop-camera");
const sampler = document.createElement("canvas");
const weaponStatus = document.getElementById("weapon-status");
let room = 1, stream = null, capture = null, timer = null;
let pendingFrame = null, sequence = 0, epoch = 0, changing = false, starting = false;

function setRoom(nextRoom) {
  room = nextRoom;
  for (const tab of document.querySelectorAll("[data-room]")) {
    tab.setAttribute("aria-selected", String(Number(tab.dataset.room) === room));
  }
  document.getElementById("room-heading").textContent = `Room ${room} · Camera slot ${room}`;
  document.getElementById("camera-panel").setAttribute("aria-labelledby", `room-${room}`);
  overlay.getContext("2d").clearRect(0, 0, overlay.width, overlay.height);
}

function releaseStream() {
  const old = stream;
  stream = null;
  if (old) old.getTracks().forEach(track => track.stop());
  video.srcObject = null;
}

async function stopCamera(message = "Webcam stopped.", beacon = false) {
  epoch += 1;
  clearTimeout(timer);
  const old = capture;
  capture = null;
  releaseStream();
  overlay.getContext("2d").clearRect(0, 0, overlay.width, overlay.height);
  stopButton.disabled = true;
  startButton.disabled = starting;
  statusLine.textContent = message;
  weaponStatus.textContent = "SSD pen detection stopped.";
  if (old) {
    if (beacon) {
      const body = new FormData();
      Object.entries({...old, csrf_token: demo.csrf()}).forEach(([key, value]) => body.append(key, value));
      navigator.sendBeacon("/api/camera/stop", body);
    } else {
      try { await demo.json("/api/camera/stop", old, {keepalive: true}); }
      catch (error) { statusLine.textContent = `${message} ${error.message}`; }
    }
  }
}

function drawFaces(data) {
  if (overlay.width !== data.width) overlay.width = data.width;
  if (overlay.height !== data.height) overlay.height = data.height;
  const context = overlay.getContext("2d");
  context.clearRect(0, 0, overlay.width, overlay.height);
  context.lineWidth = 1.5;
  context.font = "12px system-ui";
  for (const face of data.faces) {
    const [x, y, width, height] = face.box;
    const color = face.user_id === null ? "#ffcb66" : "#66e6a4";
    context.strokeStyle = color;
    context.strokeRect(x, y, width, height);
    const labelWidth = Math.min(Math.ceil(context.measureText(face.name).width) + 8, data.width);
    const labelX = Math.min(x, data.width - labelWidth);
    const labelY = y >= 18 ? y - 18 : y;
    context.fillStyle = "#142033";
    context.fillRect(labelX, labelY, labelWidth, 16);
    context.fillStyle = color;
    context.fillText(face.name, labelX + 4, labelY + 12, labelWidth - 8);
  }
  context.lineWidth = 2;
  context.font = "16px system-ui";
  for (const detection of data.weapons?.detections || []) {
    const [x, y, width, height] = detection.box;
    context.strokeStyle = "#6cc9ff";
    context.fillStyle = context.strokeStyle;
    context.strokeRect(x, y, width, height);
    context.fillText(`${detection.label} ${(detection.confidence * 100).toFixed(0)}%`, x, Math.max(18, y - 6));
  }
}

async function sendFrame(frameEpoch) {
  if (!capture || !stream || changing || document.hidden || frameEpoch !== epoch) return;
  const began = performance.now();
  try {
    const width = Math.min(video.videoWidth, 640);
    if (!width || !video.videoHeight) throw new Error("The webcam is not delivering frames.");
    sampler.width = width;
    sampler.height = Math.round(video.videoHeight * width / video.videoWidth);
    sampler.getContext("2d").drawImage(video, 0, 0, sampler.width, sampler.height);
    const blob = await new Promise(resolve => sampler.toBlob(resolve, "image/jpeg", 0.8));
    if (!blob) throw new Error("The browser could not capture a frame.");
    if (!capture || frameEpoch !== epoch) return;
    const body = new FormData();
    Object.entries({...capture, sequence: sequence++}).forEach(([key, value]) => body.append(key, value));
    body.append("frame", blob, "frame.jpg");
    const data = await demo.request("/api/camera/frame", {method: "POST", body});
    if (!capture || frameEpoch !== epoch) return;
    drawFaces(data);
    const known = [...new Set(data.faces.filter(face => face.user_id !== null).map(face => face.name))];
    statusLine.textContent = `Room ${room} · ${data.mode} · ${known.length ? `Recognised: ${known.join(", ")}` : "No enrolled face recognised"}`;
    const weapons = data.weapons || {status: "unavailable", detections: []};
    weaponStatus.textContent = weapons.status === "ready"
      ? `SSD · ${weapons.detections.length ? "Pen observed in a recent frame" : "No pen detected in the latest sample"}`
      : `SSD · ${weapons.status}${weapons.message ? ": " + weapons.message : ""}. Face tracking continues.`;
  } catch (error) {
    if (frameEpoch === epoch) await stopCamera(`Capture stopped: ${error.message}`);
  } finally {
    if (capture && !changing && frameEpoch === epoch) {
      timer = setTimeout(scheduleFrame, Math.max(0, 500 - (performance.now() - began)));
    }
  }
}

function scheduleFrame() {
  pendingFrame = sendFrame(epoch);
}

startButton.addEventListener("click", async () => {
  if (starting || capture || document.hidden) return;
  starting = true;
  startButton.disabled = true;
  const startEpoch = ++epoch;
  statusLine.textContent = "Requesting webcam permission…";
  try {
    if (!navigator.mediaDevices?.getUserMedia) throw new Error("Webcam access requires localhost or HTTPS.");
    const opened = await navigator.mediaDevices.getUserMedia({video: {width: {ideal: 640}, height: {ideal: 480}}, audio: false});
    if (document.hidden || startEpoch !== epoch) {
      opened.getTracks().forEach(track => track.stop());
      return;
    }
    stream = opened;
    video.srcObject = stream;
    await video.play();
    const lease = await demo.json("/api/camera/start", {room});
    if (document.hidden || startEpoch !== epoch) {
      await demo.json("/api/camera/stop", lease, {keepalive: true});
      releaseStream();
      return;
    }
    capture = lease;
    sequence = 0;
    stopButton.disabled = false;
    stream.getVideoTracks().forEach(track => track.addEventListener("ended", () => {
      if (stream) stopCamera("Webcam disconnected.");
    }));
    scheduleFrame();
  } catch (error) { await stopCamera(`Unable to start: ${error.message}`); }
  finally { starting = false; startButton.disabled = Boolean(capture); }
});

stopButton.addEventListener("click", () => stopCamera());
document.querySelectorAll("[data-room]").forEach(tab => tab.addEventListener("click", async () => {
  const next = Number(tab.dataset.room);
  if (changing || starting || next === room) return;
  if (!capture) { setRoom(next); return; }
  changing = true;
  clearTimeout(timer);
  try {
    await pendingFrame;
    if (!capture) return;
    const oldEpoch = epoch;
    const lease = await demo.json("/api/camera/switch", {...capture, room: next});
    if (oldEpoch !== epoch || !capture) {
      await demo.json("/api/camera/stop", lease, {keepalive: true});
      return;
    }
    capture = lease;
    epoch += 1;
    sequence = 0;
    setRoom(next);
    statusLine.textContent = `Room ${room} selected. Waiting for fresh recognition…`;
    weaponStatus.textContent = "SSD waiting for a sample from the selected room.";
  } catch (error) { await stopCamera(`Room switch failed: ${error.message}`); }
  finally {
    changing = false;
    if (capture) scheduleFrame();
  }
}));
document.addEventListener("visibilitychange", () => {
  if (document.hidden) stopCamera("Webcam stopped because this page is hidden.", true);
});
window.addEventListener("pagehide", () => stopCamera("Webcam stopped.", true));
