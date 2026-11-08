"use strict";
const chart = document.getElementById("room-chart");
if (chart) {
  const context = chart.getContext("2d");
  const values = [Number(chart.dataset.roomOne), Number(chart.dataset.roomTwo)];
  const style = getComputedStyle(chart);
  const colors = [style.getPropertyValue("--room-one"), style.getPropertyValue("--room-two")];
  const total = values.reduce((sum, value) => sum + value, 0);
  let start = -Math.PI / 2;
  values.forEach((value, index) => {
    const angle = value / total * Math.PI * 2;
    if (angle > 0) {
      context.beginPath();
      context.arc(150, 150, 138, start, start + angle);
      context.arc(150, 150, 84, start + angle, start, true);
      context.closePath();
      context.fillStyle = colors[index].trim();
      context.fill();
    }
    start += angle;
  });
}
