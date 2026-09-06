// fetch_deps.js — download the runtime pieces the bridge needs:
//   1) eufy's libsctp WASM + worker shims (from eufy's public CDN) -> ./worker/
//   2) go2rtc binary -> ./bin/
//   3) ffmpeg binary  -> ./bin/  (best-effort; falls back to "use your package manager")
// Pure Node. Zip extraction uses PowerShell (Windows) or unzip/tar (Linux/macOS).
const https = require("https");
const fs = require("fs");
const path = require("path");
const { execSync, spawnSync } = require("child_process");

const HERE = __dirname;
const WORKER = path.join(HERE, "worker");
const BIN = path.join(HERE, "bin");
fs.mkdirSync(WORKER, { recursive: true });
fs.mkdirSync(BIN, { recursive: true });
const isWin = process.platform === "win32";
const arch = process.arch === "arm64" ? "arm64" : "amd64";
// Keep this aligned with security.eufy.com's current web-client versionControl.
// These files are runtime-critical: a missing worker must fail the image build.
const SCTP_VERSION = "0_0_4";

function dl(url, dest) {
  return new Promise((res, rej) => {
    https.get(url, { headers: { "User-Agent": "fetch_deps" } }, (r) => {
      if (r.statusCode >= 300 && r.statusCode < 400) return res(dl(r.headers.location, dest));
      if (r.statusCode !== 200) return rej(new Error(`HTTP ${r.statusCode} for ${url}`));
      const f = fs.createWriteStream(dest);
      r.pipe(f);
      f.on("finish", () => f.close(() => res(fs.statSync(dest).size)));
    }).on("error", rej);
  });
}
function have(cmd) { try { execSync((isWin ? "where " : "command -v ") + cmd, { stdio: "ignore" }); return true; } catch { return false; } }
function unzip(zip, outdir) {
  if (isWin) execSync(`powershell -NoProfile -Command "Expand-Archive -Force -LiteralPath '${zip}' -DestinationPath '${outdir}'"`);
  else execSync(`unzip -o "${zip}" -d "${outdir}"`);
}

(async () => {
  // 1) eufy workers (public static assets). Versions match the web client at time of writing.
  const CDN = "https://security.eufy.com/plugin/";
  const workers = [
    `libsctp_${SCTP_VERSION}.js`,
    `libsctp_${SCTP_VERSION}.wasm`,
    `worker_sctp_send_${SCTP_VERSION}.js`,
    `worker_sctp_recv_${SCTP_VERSION}.js`,
  ];
  const workerFailures = [];
  for (const w of workers) {
    process.stdout.write(`eufy worker ${w} ... `);
    try { console.log(await dl(CDN + w, path.join(WORKER, w)), "bytes"); }
    catch (e) {
      workerFailures.push(w);
      console.log("FAILED:", e.message);
    }
  }
  if (workerFailures.length) {
    throw new Error(
      `Missing runtime-critical eufy worker assets: ${workerFailures.join(", ")}. ` +
      "Check security.eufy.com's web-client versionControl and update SCTP_VERSION."
    );
  }

  // 2) go2rtc
  process.stdout.write("go2rtc ... ");
  try {
    if (isWin) {
      const zip = path.join(BIN, "go2rtc.zip");
      await dl(`https://github.com/AlexxIT/go2rtc/releases/latest/download/go2rtc_win64${arch === "arm64" ? "_arm64" : ""}.zip`, zip);
      unzip(zip, BIN); fs.rmSync(zip, { force: true });
      console.log("ok (bin/go2rtc.exe)");
    } else {
      const bin = path.join(BIN, "go2rtc");
      const plat = process.platform === "darwin" ? "mac" : "linux";
      await dl(`https://github.com/AlexxIT/go2rtc/releases/latest/download/go2rtc_${plat}_${arch}`, bin);
      fs.chmodSync(bin, 0o755);
      console.log("ok (bin/go2rtc)");
    }
  } catch (e) { console.log("FAILED:", e.message, "\n  Get it from https://github.com/AlexxIT/go2rtc/releases and put it in bin/"); }

  // 3) ffmpeg (best-effort)
  if (have("ffmpeg")) { console.log("ffmpeg ... already on PATH"); }
  else if (isWin) {
    process.stdout.write("ffmpeg (win, ~80MB) ... ");
    try {
      const zip = path.join(BIN, "ffmpeg.zip");
      await dl("https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip", zip);
      unzip(zip, BIN);
      const sub = fs.readdirSync(BIN).find((d) => d.startsWith("ffmpeg-"));
      if (sub) fs.copyFileSync(path.join(BIN, sub, "bin", "ffmpeg.exe"), path.join(BIN, "ffmpeg.exe"));
      fs.rmSync(zip, { force: true });
      console.log("ok (bin/ffmpeg.exe)");
    } catch (e) { console.log("FAILED:", e.message, "\n  Install ffmpeg and ensure it's on PATH, or drop ffmpeg.exe in bin/"); }
  } else {
    console.log("ffmpeg ... not found — install via your package manager (e.g. `sudo apt install ffmpeg`)");
  }

  console.log("\nDone. Next: `node get_auth.js` (one-time login), then start_bridge" + (isWin ? ".cmd" : ".sh"));
})().catch((error) => {
  console.error("fetch_deps FATAL:", error.message);
  process.exitCode = 1;
});
