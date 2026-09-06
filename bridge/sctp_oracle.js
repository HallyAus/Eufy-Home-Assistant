// sctp_oracle.js - run eufy's exact libsctp WASM as a framing oracle.
const fs = require("fs");
const path = require("path");

const WDIR = path.join(__dirname, "worker");
const SCTP_VERSION = "0_0_4";
const GLUE = path.join(WDIR, `libsctp_${SCTP_VERSION}.js`);
const WASM = path.join(WDIR, `libsctp_${SCTP_VERSION}.wasm`);
const LINK = { Unknow: 0, Cmd: 1, File: 2, Notify: 3, PlayBack: 4, Live: 5, SendLive: 6, Inner: 99 };
const CH = { COMMAND: 0, MEDIA: 1, NOTIFY: 2, DOWNLOAD: 3, PLAYBACK: 4, LIVE: 5, MAX: 6 };
function link2channel(link) {
  switch (link) {
    case LINK.Cmd: return CH.COMMAND;
    case LINK.File: return CH.DOWNLOAD;
    case LINK.Notify: return CH.NOTIFY;
    case LINK.PlayBack: return CH.PLAYBACK;
    case LINK.Live:
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
function loadFactory() {
  const code = fs.readFileSync(GLUE, "utf8");
  const m = { exports: {} };
  const fn = new Function("module", "exports", "require", "__dirname", code + "\n;module.exports=libsctp;");
  fn(m, m.exports, require, WDIR);
  return m.exports;
}
async function initModule() {
  const libsctp = loadFactory();
  return libsctp({ wasmBinary: new Uint8Array(fs.readFileSync(WASM)) });
}
function makeManager(Module, mode, datachannel_id, opts) {
  const o = opts || {};
  Module._set_mxlog_level(5);
  const fm = Module._sctp_frame_manager_create(mode, datachannel_id, 15000, mode === 1 ? 1000 : 5000, 1000, 10);
  const sendCb = Module.addFunction(function (id, data, size) {
    const out = Buffer.allocUnsafe(size);
    for (let i = 0; i < size; i++) out[i] = Module.HEAPU8[data + i];
    if (o.onPacket) o.onPacket(id, out);
    return 0;
  }, "iiii");
  Module._sctp_frame_manager_set_send_packet_callback(fm, sendCb);
  if (mode === 0) {
    const recvCb = Module.addFunction(function (id, sctp_channel, data, size) {
      const out = Buffer.allocUnsafe(size);
      for (let i = 0; i < size; i++) out[i] = Module.HEAPU8[data + i];
      if (o.onFrame) o.onFrame(id, channel2link(sctp_channel), out);
      return 0;
    }, "iiiii");
    Module._sctp_frame_manager_set_recv_frame_callback(fm, recvCb);
  }
  return fm;
}
function pushFrame(Module, fm, link, bytes) {
  const fb = Module._sctp_frame_manager_get_frame_buffer(fm, bytes.length);
  if (fb === 0) throw new Error("get_frame_buffer failed size=" + bytes.length);
  const dataPtr = Module._sctp_frame_buffer_get_data(fb);
  Module.HEAPU8.set(bytes, dataPtr);
  Module._sctp_frame_buffer_set_size(fb, bytes.length);
  const ret = Module._sctp_frame_manager_push_frame_data(fm, fb, link2channel(link));
  if (ret) throw new Error("push_frame_data ret=" + ret);
}
function pushPacket(Module, fm, bytes) {
  const pb = Module._sctp_frame_manager_get_packet_buffer(fm, bytes.length);
  if (pb === 0) throw new Error("get_packet_buffer failed size=" + bytes.length);
  const dataPtr = Module._sctp_packet_get_data(pb);
  Module.HEAPU8.set(bytes, dataPtr);
  const ret = Module._sctp_frame_manager_push_packet_data(fm, pb);
  if (ret) throw new Error("push_packet_data ret=" + ret);
}
function buildOpenLive(userId, channelArray) {
  const payload = Buffer.from(JSON.stringify({ account_id: userId, cmd: 1103, payload: { channel_info: { array_size: channelArray.length, channel_array: channelArray } } }), "utf8");
  const header = Buffer.alloc(16);
  header.write("XZYH", 0, "ascii");
  header.writeUInt16LE(1350, 4);
  header.writeUInt32LE(payload.length, 6);
  header[12] = 255;
  header[15] = 2;
  return Buffer.concat([header, payload]);
}

// Some Eufy control failures are not JSON. They are a signed int32 status followed by zeros.
function fixedControlStatus(frame) {
  if (!Buffer.isBuffer(frame) || frame.length < 20 || frame.toString("ascii", 0, 4) !== "XZYH") return null;
  if (frame.readUInt16LE(4) === 1032) return null;
  const payload = frame.subarray(16);
  if (payload.length < 4) return null;
  for (let i = 4; i < payload.length; i++) if (payload[i] !== 0) return null;
  let binaryPrefix = false;
  for (let i = 0; i < 4; i++) if (payload[i] < 0x20 || payload[i] > 0x7e) binaryPrefix = true;
  return binaryPrefix ? payload.readInt32LE(0) : null;
}

const b64 = (buf) => Buffer.from(buf).toString("base64");
const unb64 = (s) => Buffer.from(s, "base64");

async function selftest() {
  const Module = await initModule();
  const txPackets = [];
  const sender = makeManager(Module, 1, 0, { onPacket: (id, buf) => txPackets.push(buf) });
  const frames = [];
  const receiver = makeManager(Module, 0, 0, { onPacket: () => {}, onFrame: (id, link, buf) => frames.push({ link, buf }) });
  const cmd = buildOpenLive("TESTUSER1234567890", [0]);
  pushFrame(Module, sender, LINK.Cmd, cmd);
  for (const packet of txPackets) pushPacket(Module, receiver, packet);
  for (let k = 0; k < 5; k++) Module._sctp_frame_manager_on_100ms_timer(receiver, Date.now() + k * 100);
  const pass = frames.length === 1 && frames[0].buf.equals(cmd);
  console.log(pass ? "SELFTEST PASS: eufy framing roundtrips in Node." : "SELFTEST FAIL: framing roundtrip mismatch.");
  process.exit(pass ? 0 : 2);
}

async function serve() {
  const Module = await initModule();
  const send = (obj) => process.stdout.write(JSON.stringify(obj) + "\n");
  const sender = makeManager(Module, 1, 0, { onPacket: (id, buf) => send({ ev: "tx", src: "send", b64: b64(buf) }) });
  const receiver = makeManager(Module, 0, 1, {
    onPacket: (id, buf) => send({ ev: "tx", src: "recv", b64: b64(buf) }),
    onFrame: (id, link, frame) => {
      const status = fixedControlStatus(frame);
      if (status !== null) {
        send({ ev: "control_status", channel: link, status });
        console.error(`[oracle] fixed control status ${status}`);
        if (status === -104) {
          console.error("EUFY_AUTHORIZATION_ERROR_-104: use the eufy account that owns/administers this NVR; shared/member accounts cannot open NVR command sessions.");
          setTimeout(() => { try { process.kill(process.ppid, "SIGTERM"); } catch (e) {} }, 10);
        }
      }
      send({ ev: "frame", channel: link, b64: b64(frame) });
    },
  });
  setInterval(() => { try { Module._sctp_frame_manager_on_100ms_timer(receiver, Date.now()); } catch (e) {} }, 100);
  let input = "";
  process.stdin.on("data", (chunk) => {
    input += chunk.toString("utf8");
    let nl;
    while ((nl = input.indexOf("\n")) >= 0) {
      const line = input.slice(0, nl); input = input.slice(nl + 1);
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
(mode === "serve" ? serve() : selftest()).catch((error) => {
  console.error("ORACLE FATAL", error && error.stack ? error.stack : error);
  process.exit(1);
});
