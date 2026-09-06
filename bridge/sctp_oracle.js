// sctp_oracle.js - run eufy's exact libsctp WASM (sctp_frame_manager) in Node as a framing oracle.
// Two frame managers: a SENDER (mode 1) that turns app frames (e.g. the XZYH openLive cmd) into
// PTCS wire packets, and a RECEIVER (mode 0) that turns received PTCS packets back into whole frames.
//
// Modes:
//   node sctp_oracle.js selftest   -> offline roundtrip test (no NVR): frame the openLive cmd,
//                                      feed the packets into the receiver, confirm it reassembles.
//   node sctp_oracle.js serve      -> newline-delimited JSON protocol over stdio for the Python client:
//       in : {"op":"send","link":1,"b64":...}  push an app frame to the SENDER (link 1=cmd,6=sendlive)
//            {"op":"recv","b64":...}            push a received PTCS packet to the RECEIVER
//       out: {"ev":"ready"}
//            {"ev":"tx","src":"send"|"recv","b64":...}  a PTCS packet to transmit on the DataChannel
//            {"ev":"frame","channel":<linkType>,"b64":...}  a reassembled whole frame
const fs = require("fs");
const path = require("path");

const WDIR = path.join(__dirname, "worker");   // eufy libsctp files, fetched by fetch_deps.js
const SCTP_VERSION = "0_0_4";
const GLUE = path.join(WDIR, `libsctp_${SCTP_VERSION}.js`);
const WASM = path.join(WDIR, `libsctp_${SCTP_VERSION}.wasm`);

// link_type / channel_type, mirrored from worker_sctp_send/recv
const LINK = { Unknow: 0, Cmd: 1, File: 2, Notify: 3, PlayBack: 4, Live: 5, SendLive: 6, Inner: 99 };
const CH = { COMMAND: 0, MEDIA: 1, NOTIFY: 2, DOWNLOAD: 3, PLAYBACK: 4, LIVE: 5, MAX: 6 };
function link2channel(link) {
  switch (link) {
    case LINK.Cmd: return CH.COMMAND;
    case LINK.File: return CH.DOWNLOAD;
    case LINK.Notify: return CH.NOTIFY;
    case LINK.PlayBack: return CH.PLAYBACK;
    case LINK.Live: return CH.LIVE;
    case LINK.SendLive: return CH.LIVE;
    default: return CH.MAX;
  }
}
function channel2link(ch) {
  switch (ch) {
    case CH.COMMAND: return LINK.Cmd;
    case CH.MEDIA: return LINK.Live;
    case CH.NOTIFY: return LINK.Notify;
    case CH.DOWNLOAD: return LINK.File;
    case CH.PLAYBACK: return LINK.PlayBack;
    case CH.LIVE: return LINK.Live;
    case CH.MAX: return LINK.Inner;
    default: return LINK.Unknow;
  }
}

// Load the Emscripten factory `libsctp` out of the (export-less) glue file.
function loadFactory() {
  const code = fs.readFileSync(GLUE, "utf8");
  const m = { exports: {} };
  const fn = new Function("module", "exports", "require", "__dirname", code + "\n;module.exports=libsctp;");
  fn(m, m.exports, require, WDIR);
  return m.exports;
}

async function initModule() {
  const libsctp = loadFactory();
  const wasmBinary = fs.readFileSync(WASM); // Buffer -> Uint8Array view ok
  const Module = await libsctp({ wasmBinary: new Uint8Array(wasmBinary) });
  return Module;
}

// A frame manager wrapper. mode: 1=sender, 0=receiver. datachannel_id is just a label.
function makeManager(Module, mode, datachannel_id, opts) {
  const o = opts || {};
  const recv_frame_max_delay = 15000;
  const max_packet_count = mode === 1 ? 1000 : 5000;
  const max_packet_bytes = 1000;
  const max_fec_group_count = 10;
  Module._set_mxlog_level(5);
  const fm = Module._sctp_frame_manager_create(mode, datachannel_id, recv_frame_max_delay, max_packet_count, max_packet_bytes, max_fec_group_count);

  // send_packet callback: (id, data, size) -> bytes to put on the wire
  const sendCb = Module.addFunction(function (id, data, size) {
    const out = Buffer.allocUnsafe(size);
    const heap = Module.HEAPU8;
    for (let i = 0; i < size; i++) out[i] = heap[data + i];
    if (o.onPacket) o.onPacket(id, out);
    return 0;
  }, "iiii");
  Module._sctp_frame_manager_set_send_packet_callback(fm, sendCb);

  if (mode === 0) {
    // recv_frame callback: (id, sctp_channel, data, size) -> a whole reassembled frame
    const recvCb = Module.addFunction(function (id, sctp_channel, data, size) {
      const out = Buffer.allocUnsafe(size);
      const heap = Module.HEAPU8;
      for (let i = 0; i < size; i++) out[i] = heap[data + i];
      if (o.onFrame) o.onFrame(id, channel2link(sctp_channel), out);
      return 0;
    }, "iiiii");
    Module._sctp_frame_manager_set_recv_frame_callback(fm, recvCb);
  }
  return fm;
}

function pushFrame(Module, fm, link, bytes) {
  const size = bytes.length;
  const fb = Module._sctp_frame_manager_get_frame_buffer(fm, size);
  if (fb === 0) throw new Error("get_frame_buffer failed size=" + size);
  const dataPtr = Module._sctp_frame_buffer_get_data(fb);
  Module.HEAPU8.set(bytes, dataPtr);
  Module._sctp_frame_buffer_set_size(fb, size);
  const ret = Module._sctp_frame_manager_push_frame_data(fm, fb, link2channel(link));
  if (ret) throw new Error("push_frame_data ret=" + ret);
}

function pushPacket(Module, fm, bytes) {
  const size = bytes.length;
  const pb = Module._sctp_frame_manager_get_packet_buffer(fm, size);
  if (pb === 0) throw new Error("get_packet_buffer failed size=" + size);
  const dataPtr = Module._sctp_packet_get_data(pb);
  Module.HEAPU8.set(bytes, dataPtr);
  const ret = Module._sctp_frame_manager_push_packet_data(fm, pb);
  if (ret) throw new Error("push_packet_data ret=" + ret);
}

// Build the openLive XZYH command exactly like the web bundle (Hr header + JSON payload).
function buildOpenLive(userId, channelArray) {
  const payloadObj = {
    account_id: userId,
    cmd: 1103,
    payload: { channel_info: { array_size: channelArray.length, channel_array: channelArray } },
  };
  const payload = Buffer.from(JSON.stringify(payloadObj), "utf8");
  const header = Buffer.alloc(16);
  header.write("XZYH", 0, "ascii");          // magic
  header.writeUInt16LE(1350, 4);              // command_id
  header.writeUInt32LE(payload.length, 6);    // param_len
  header[10] = 0;
  header[11] = 0;                             // segmen
  header[12] = 255;                          // channel_id
  header[13] = 0;                             // sign_code
  header[14] = 0;                             // is_response
  header[15] = 2;                             // dev_type (cloud=2, matches web app Kt.envType="cloud")
  return Buffer.concat([header, payload]);
}

const b64 = (buf) => Buffer.from(buf).toString("base64");
const unb64 = (s) => Buffer.from(s, "base64");

async function selftest() {
  const Module = await initModule();
  console.log("[oracle] wasm loaded; exports present:",
    ["_sctp_frame_manager_create", "_sctp_frame_manager_get_frame_buffer", "_sctp_frame_manager_get_packet_buffer"]
      .every((n) => typeof Module[n] === "function"));

  const txPackets = [];
  const sender = makeManager(Module, 1, 0, { onPacket: (id, buf) => { txPackets.push(buf); } });

  const frames = [];
  const nacks = [];
  const receiver = makeManager(Module, 0, 0, {
    onPacket: (id, buf) => { nacks.push(buf); },
    onFrame: (id, link, buf) => { frames.push({ link, buf }); },
  });

  const cmd = buildOpenLive("TESTUSER1234567890", [0]);
  console.log("[oracle] openLive cmd len=", cmd.length, "head=", cmd.slice(0, 16).toString("hex"));
  console.log("[oracle] openLive json=", cmd.slice(16).toString("utf8"));

  pushFrame(Module, sender, LINK.Cmd, cmd);
  console.log("[oracle] sender produced", txPackets.length, "PTCS packet(s):");
  txPackets.forEach((p, i) => console.log(`   pkt[${i}] len=${p.length} head=${p.slice(0, 32).toString("hex")}`));

  // feed packets into the receiver
  for (const p of txPackets) pushPacket(Module, receiver, p);
  // drive timers a few times in case reassembly is deferred
  for (let k = 0; k < 5; k++) Module._sctp_frame_manager_on_100ms_timer(receiver, Date.now() + k * 100);

  console.log("[oracle] receiver emitted", frames.length, "frame(s), nacks=", nacks.length);
  frames.forEach((f, i) => {
    const ok = f.buf.equals(cmd);
    console.log(`   frame[${i}] link=${f.link} len=${f.buf.length} roundtrip_ok=${ok} head=${f.buf.slice(0, 16).toString("hex")}`);
    if (!ok) console.log("     json=", f.buf.slice(16).toString("utf8"));
  });
  const pass = frames.length === 1 && frames[0].buf.equals(cmd);
  console.log(pass ? "\nSELFTEST PASS: eufy framing roundtrips in Node." : "\nSELFTEST: see above (roundtrip not exact).");
  process.exit(pass ? 0 : 2);
}

async function serve() {
  const Module = await initModule();
  const send = (obj) => process.stdout.write(JSON.stringify(obj) + "\n");
  const sender = makeManager(Module, 1, 0, { onPacket: (id, buf) => send({ ev: "tx", src: "send", b64: b64(buf) }) });
  const receiver = makeManager(Module, 0, 1, {
    onPacket: (id, buf) => send({ ev: "tx", src: "recv", b64: b64(buf) }),
    onFrame: (id, link, buf) => send({ ev: "frame", channel: link, b64: b64(buf) }),
  });
  setInterval(() => { try { Module._sctp_frame_manager_on_100ms_timer(receiver, Date.now()); } catch (e) {} }, 100);

  let buf = "";
  process.stdin.on("data", (chunk) => {
    buf += chunk.toString("utf8");
    let nl;
    while ((nl = buf.indexOf("\n")) >= 0) {
      const line = buf.slice(0, nl); buf = buf.slice(nl + 1);
      if (!line.trim()) continue;
      let msg;
      try { msg = JSON.parse(line); } catch (e) { continue; }
      try {
        if (msg.op === "send") pushFrame(Module, sender, msg.link || LINK.Cmd, unb64(msg.b64));
        else if (msg.op === "recv") pushPacket(Module, receiver, unb64(msg.b64));
        else if (msg.op === "buildOpenLive") send({ ev: "openLive", b64: b64(buildOpenLive(msg.userId, msg.channels || [0])) });
      } catch (e) { send({ ev: "error", op: msg.op, msg: String(e) }); }
    }
  });
  process.stdin.on("end", () => process.exit(0));
  send({ ev: "ready" });
}

const mode = process.argv[2] || "selftest";
(mode === "serve" ? serve() : selftest()).catch((e) => { console.error("ORACLE FATAL", e && e.stack ? e.stack : e); process.exit(1); });
