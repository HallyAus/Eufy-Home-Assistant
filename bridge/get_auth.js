// get_auth.js — one-time auth grab for the bridge.
// Opens the eufy web portal in a real Chrome (persistent profile in ./userdata, gitignored), logs you in,
// and captures the signaling session token + your account id into ./auth.json (gitignored).
//
// Requires: `npm i playwright-core` and a Chrome install. The signaling needs a eufy cloud session token;
// the video itself stays LAN-local. Re-run this if ws/sign starts returning non-200 (token expired).
//
// Roadmap: replace this with a pure-Python eufy passport login (email/password -> token) for headless setups.
const { chromium } = require("playwright-core");
const path = require("path");
const fs = require("fs");

(async () => {
  const ctx = await chromium.launchPersistentContext(path.join(__dirname, "userdata"), {
    headless: false, channel: "chrome", viewport: { width: 1200, height: 800 },
    args: ["--disable-blink-features=AutomationControlled"],
  });
  const page = ctx.pages()[0] || (await ctx.newPage());
  let out = null;
  page.on("request", (r) => {
    const u = r.url();
    if (/\/v1\/smart\/nvr\/ws\/sign/.test(u)) {
      const h = r.headers();
      let sn = null;
      let region = "us-pr";
      try {
        const parsed = new URL(u);
        sn = parsed.searchParams.get("station_sn");
        if (parsed.hostname === "security-smart-eu.eufylife.com") region = "eu-pr";
        if (parsed.hostname === "security-smart-ie.eufylife.com") region = "ie-pr";
      } catch {}
      out = { authToken: h["x-auth-token"], gtoken: h["gtoken"], webCountry: h["web-country"] || "US",
              appName: h["app-name"] || "eufy_mega", stationSn: sn, region };
    }
  });

  console.log("Log into your eufy account in the browser window, then open the NVR live view once...");
  await page.goto("https://nvr.eufy.com/", { waitUntil: "domcontentloaded", timeout: 60000 });
  for (let i = 0; i < 120 && !out; i++) await page.waitForTimeout(1000);  // wait up to 2 min for ws/sign

  if (!out || !out.authToken) { console.error("No ws/sign request seen — make sure you opened the NVR live view."); await ctx.close(); process.exit(1); }
  // grab the encrypted account id from localStorage; the bridge decrypts it (AES passphrase "aes")
  try { out.auid = await page.evaluate(() => localStorage.getItem("auid")); } catch {}
  fs.writeFileSync(path.join(__dirname, "auth.json"), JSON.stringify(out, null, 2));
  console.log("Wrote auth.json (token", out.authToken.slice(0, 8) + "..., station", out.stationSn + ").");
  await ctx.close(); process.exit(0);
})().catch((e) => { console.error("FATAL", e && e.stack ? e.stack : e); process.exit(1); });
