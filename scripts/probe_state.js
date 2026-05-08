#!/usr/bin/env node
// Quick websocket probe: connect to a Swidget device in plaintext mode,
// request state, and print the response. Used to compare what the
// websocket reports against what /api/v1/state returns over HTTP — in
// particular whether host.components["1"].power has the "current" key
// when the websocket delivers it.
//
// Usage: node scripts/probe_state.js [host]
//   default host: 192.168.0.86

const WebSocket = require("ws");

const HOST = process.argv[2] || "192.168.0.86";
const URL = `ws://${HOST}/api/v1/sock?=`;

console.log(`connecting: ${URL}`);
const ws = new WebSocket(URL);

const timeout = setTimeout(() => {
  console.error("timeout waiting for state response");
  ws.terminate();
  process.exit(2);
}, 10_000);

ws.on("open", () => {
  console.log("open; requesting state");
  ws.send(JSON.stringify({ type: "state", request_id: "state" }));
});

ws.on("message", (raw) => {
  let msg;
  try {
    msg = JSON.parse(raw.toString());
  } catch (err) {
    console.error("non-JSON frame:", raw.toString());
    return;
  }
  if (msg.request_id !== "state") {
    // Skip unsolicited pushes (DYNAMIC_UPDATE etc.) so the script
    // returns deterministically once the state response arrives.
    console.log(`(skipping ${msg.request_id || "<no request_id>"})`);
    return;
  }
  console.log("state response:");
  console.log(JSON.stringify(msg, null, 2));

  // Spotlight the field we actually care about so the diff with HTTP is
  // obvious without scrolling.
  const host = msg.host && msg.host.components;
  if (host) {
    for (const id of Object.keys(host)) {
      const power = host[id] && host[id].power;
      console.log(`host.components["${id}"].power = ${JSON.stringify(power)}`);
    }
  }

  clearTimeout(timeout);
  ws.close();
});

ws.on("error", (err) => {
  console.error("ws error:", err.message);
  clearTimeout(timeout);
  process.exit(1);
});

ws.on("close", () => {
  console.log("closed");
});
