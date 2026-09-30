// Headless driver for the Passport simulator.
// Usage: node driver.mjs <firmware.bin> [mic.wav]
// Control API on http://127.0.0.1:4199:
//   GET /shot?name=x      -> saves shots/x.png (device canvas)
//   GET /key?k=OK&ms=120  -> press key (OK/UP/DOWN/POWER) for ms
//   GET /reload           -> hard restart runtime with same firmware
// UART is appended to uart.log continuously.
import { chromium } from "playwright-core";
import http from "node:http";
import fs from "node:fs";
import path from "node:path";

const [firmware, micWav] = process.argv.slice(2);
// ?audioClock=emulated: XiaoZhi needs I2S paced by the emulated CPU clock.
const SIM = process.env.SIM_URL ?? "http://127.0.0.1:4190/?audioClock=emulated";
fs.mkdirSync("shots", { recursive: true });
fs.writeFileSync("uart.log", "");

const args = [
  "--autoplay-policy=no-user-gesture-required",
  "--use-fake-ui-for-media-stream",
  "--use-fake-device-for-media-stream",
];
if (micWav) args.push(`--use-file-for-fake-audio-capture=${path.resolve(micWav)}`);

// HEADLESS=0 opens a visible window for demos (inspector stays closed).
const headless = process.env.HEADLESS !== "0";
const browser = await chromium.launch({ channel: "chrome", headless, args });
const context = await browser.newContext({ permissions: ["microphone"],
  viewport: headless ? undefined : { width: 1280, height: 800 } });
const page = await context.newPage();
page.on("pageerror", (e) => fs.appendFileSync("uart.log", `\n[pageerror] ${e.message}\n`));
await page.goto(SIM);
await page.evaluate(() => document.querySelector("#simulator-notice")?.close());

async function showUart() {
  await page.evaluate(() => {
    const panel = document.querySelector("#debug-panel");
    if (!panel.classList.contains("is-open")) document.querySelector("#inspector-toggle").click();
    document.querySelector('[data-debug-tab="uart"]').click();
  });
}

async function upload() {
  if (headless) await showUart();
  await page.waitForFunction(() => !document.querySelector("#firmware-upload").disabled, null, { timeout: 60000 });
  await page.setInputFiles("#firmware-file", path.resolve(firmware));
}
await upload();

let seen = 0;
setInterval(async () => {
  try {
    const text = await page.$eval("#uart-output", (el) => el.textContent);
    if (text.length < seen) seen = 0;
    if (text.length > seen) {
      fs.appendFileSync("uart.log", text.slice(seen));
      seen = text.length;
    }
  } catch {}
}, 300);

const keyMap = { OK: "Enter", UP: "ArrowUp", DOWN: "ArrowDown", POWER: "p" };
http.createServer(async (req, res) => {
  const url = new URL(req.url, "http://x");
  try {
    if (url.pathname === "/shot") {
      const name = url.searchParams.get("name") ?? String(Date.now());
      await page.locator("#qemu-display").screenshot({ path: `shots/${name}.png` });
      res.end(`shots/${name}.png\n`);
    } else if (url.pathname === "/page") {
      await page.screenshot({ path: "shots/page.png" });
      res.end("shots/page.png\n");
    } else if (url.pathname === "/key") {
      const k = keyMap[url.searchParams.get("k")];
      const ms = Number(url.searchParams.get("ms") ?? 120);
      await page.evaluate(() => document.activeElement?.blur());
      await page.keyboard.down(k);
      await page.waitForTimeout(ms);
      await page.keyboard.up(k);
      res.end("ok\n");
    } else if (url.pathname === "/say") {
      // Feed a 16-bit mono WAV into the device microphone at emulated speed.
      const wav = fs.readFileSync(url.searchParams.get("file"));
      let pos = 12, rate = 16000, data;
      while (pos < wav.length) {
        const id = wav.toString("ascii", pos, pos + 4), size = wav.readUInt32LE(pos + 4);
        if (id === "fmt ") rate = wav.readUInt32LE(pos + 12);
        if (id === "data") { data = wav.subarray(pos + 8, pos + 8 + size); break; }
        pos += 8 + size;
      }
      await page.evaluate(([b64, r]) => window.__simTest.micFile(b64, r), [data.toString("base64"), rate]);
      res.end(`fed ${data.length / 2 / rate}s @${rate}Hz\n`);
    } else if (url.pathname === "/click") {
      await page.click(url.searchParams.get("sel"));
      res.end("ok\n");
    } else if (url.pathname === "/eval") {
      const out = await page.evaluate(url.searchParams.get("js"));
      res.end(JSON.stringify(out) + "\n");
    } else if (url.pathname === "/reload") {
      seen = 0;
      fs.appendFileSync("uart.log", "\n===== RELOAD =====\n");
      await page.reload();
      await page.evaluate(() => document.querySelector("#simulator-notice")?.close());
      await upload();
      res.end("ok\n");
    } else if (url.pathname === "/quit") {
      res.end("bye\n");
      await browser.close();
      process.exit(0);
    } else res.end("?\n");
  } catch (e) {
    res.statusCode = 500;
    res.end(String(e) + "\n");
  }
}).listen(4199, "127.0.0.1");
console.log("driver ready");
