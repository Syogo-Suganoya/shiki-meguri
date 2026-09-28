/**
 * 提出用のデモ動画を撮る。
 *
 *   docker compose --profile video up --build video
 *
 * アプリの最初の画面から、予定の入力 → 衣装 → 承認 → 当日の流れ → AIに相談 までを
 * 利用者と同じ順に操作して録画する。画面の写し（docs/shots.js）と同じく、
 * 画面を変えたら撮り直すだけで追随する。
 *
 * ヘッドレスのブラウザにはマウスの矢印が出ないので、偽の矢印と字幕を画面に重ねて
 * 何を押したかが分かるようにする。重ねるのは録画の中だけで、アプリには手を入れない。
 */

const { execFileSync } = require("child_process");
const fs = require("fs");
const puppeteer = require("puppeteer");

const BASE = process.env.BASE_URL || "http://api:8080";
const OUT = process.env.OUT_DIR || "/work/_video";
const WIDTH = 1280;
const HEIGHT = 800;

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// 録画の中にだけ重ねる、矢印と字幕。ページを移ってもそのたびに差し込まれる。
const OVERLAY = () => {
  const mount = () => {
    if (document.getElementById("rec-cursor")) return;
    const style = document.createElement("style");
    style.textContent = `
      #rec-cursor{position:fixed;left:0;top:0;width:22px;height:22px;z-index:2147483647;pointer-events:none;
        transform:translate(640px,400px);transition:transform .7s cubic-bezier(.4,0,.2,1)}
      #rec-cursor svg{filter:drop-shadow(0 1px 2px rgba(0,0,0,.35))}
      #rec-cursor .ring{position:absolute;left:-14px;top:-14px;width:28px;height:28px;border-radius:50%;
        border:3px solid #b3402e;opacity:0;transform:scale(.4)}
      #rec-cursor.tap .ring{animation:rec-tap .45s ease-out}
      @keyframes rec-tap{0%{opacity:.9;transform:scale(.4)}100%{opacity:0;transform:scale(1.5)}}
      #rec-caption{position:fixed;left:50%;bottom:34px;transform:translateX(-50%);z-index:2147483646;
        max-width:86%;padding:12px 26px;background:rgba(42,39,35,.9);color:#fff;border-radius:4px;
        font:500 21px/1.6 "Noto Sans JP","Noto Sans CJK JP",sans-serif;letter-spacing:.04em;text-align:center;
        pointer-events:none;opacity:0;transition:opacity .35s}
      #rec-caption.show{opacity:1}`;
    document.head.appendChild(style);
    const cursor = document.createElement("div");
    cursor.id = "rec-cursor";
    cursor.innerHTML = `<span class="ring"></span><svg width="22" height="22" viewBox="0 0 22 22">
      <path d="M2 1 L2 18 L6.5 13.8 L9.6 20.5 L12.6 19.1 L9.6 12.6 L15.6 12.6 Z" fill="#fff" stroke="#2a2723" stroke-width="1.4"/></svg>`;
    const caption = document.createElement("div");
    caption.id = "rec-caption";
    document.body.append(cursor, caption);
    const saved = sessionStorage.getItem("rec.cursor");
    if (saved) {
      cursor.style.transition = "none";
      cursor.style.transform = saved;
      requestAnimationFrame(() => (cursor.style.transition = ""));
    }
  };
  if (document.body) mount();
  else document.addEventListener("DOMContentLoaded", mount);
};

async function main() {
  fs.mkdirSync(OUT, { recursive: true });
  const browser = await puppeteer.launch({
    executablePath: process.env.CHROME_BIN || "/usr/bin/chromium-browser",
    args: ["--no-sandbox", "--disable-dev-shm-usage", "--font-render-hinting=none"],
  });
  const page = await browser.newPage();
  await page.setViewport({ width: WIDTH, height: HEIGHT, deviceScaleFactor: 1 });
  await page.evaluateOnNewDocument(OVERLAY);
  page.on("pageerror", (e) => console.log(`[画面] ${e.message}`));

  // ---------------------------------------------------------------- 操作の部品
  const caption = async (text, hold = 0) => {
    await page.evaluate((t) => {
      const el = document.getElementById("rec-caption");
      if (!el) return;
      el.textContent = t;
      el.classList.toggle("show", Boolean(t));
    }, text);
    if (hold) await sleep(hold);
  };

  // 矢印を要素の上まで動かしてから押す。押した所に輪を出す。
  const moveTo = async (selector) => {
    await page.waitForSelector(selector, { visible: true, timeout: 30000 });
    const el = await page.$(selector);
    await el.evaluate((n) => n.scrollIntoView({ block: "center", behavior: "smooth" }));
    await sleep(700);
    const box = await el.boundingBox();
    const x = box.x + Math.min(box.width / 2, 60);
    const y = box.y + box.height / 2;
    await page.evaluate((x, y) => {
      const c = document.getElementById("rec-cursor");
      const t = `translate(${x}px,${y}px)`;
      c.style.transform = t;
      sessionStorage.setItem("rec.cursor", t);
    }, x, y);
    await sleep(850);
    return { x, y };
  };
  const click = async (selector, after = 900) => {
    const { x, y } = await moveTo(selector);
    await page.evaluate(() => {
      const c = document.getElementById("rec-cursor");
      c.classList.remove("tap");
      void c.offsetWidth;
      c.classList.add("tap");
    });
    await page.mouse.click(x, y);
    await sleep(after);
  };
  const choose = async (selector, value) => {
    await moveTo(selector);
    await page.select(selector, value);
    await sleep(900);
  };
  const type = async (selector, text) => {
    await click(selector, 300);
    await page.type(selector, text, { delay: 110 });
    await sleep(700);
  };
  // 読める速さで下へ送る。
  const scroll = async (px, ms = 2400) => {
    const steps = Math.max(1, Math.round(ms / 40));
    for (let i = 0; i < steps; i++) {
      await page.evaluate((d) => window.scrollBy(0, d), px / steps);
      await sleep(40);
    }
  };
  const scrollTop = async () => {
    await page.evaluate(() => window.scrollTo({ top: 0, behavior: "smooth" }));
    await sleep(900);
  };
  const waitFor = async (heading) => {
    try {
      await page.waitForFunction(
        (t) => document.querySelector("#stage h1")?.textContent.includes(t),
        { timeout: 30000 },
        heading,
      );
    } catch (e) {
      const seen = await page.evaluate(() => document.body.innerText.slice(0, 300));
      throw new Error(`「${heading}」が出なかった。画面にはこれが出ていた:\n${seen}`);
    }
    await sleep(900);
  };
  // 相談欄にエージェントの返事が増えるまで待つ。
  const waitReply = async (before) => {
    await page.waitForFunction(
      (n) => document.querySelectorAll("#talk .msg.agent").length > n,
      { timeout: 30000 },
      before,
    );
    await sleep(2600);
  };
  const agentCount = () => page.evaluate(() => document.querySelectorAll("#talk .msg.agent").length);

  // ------------------------------------------------------------------ 録画
  await page.goto(`${BASE}/console.html`, { waitUntil: "networkidle0" });
  await sleep(500);
  const webm = `${OUT}/demo.webm`;
  const recorder = await page.screencast({ path: webm });

  // 1. 予定
  await waitFor("式の予定");
  await caption("① 式の予定を入れます。誰の式かは訊きません", 2200);
  await choose("#intake-form select[name=venue_station]", "新宿");
  await type("#intake-form input[name=venue_name]", "ホテル椿山");
  await choose("#intake-form select[name=home_station]", "吉祥寺");
  await caption("種別・日時・会場の最寄り駅・出発する駅を選ぶだけです", 1600);
  await click("#intake-form button[type=submit]");

  await waitFor("この予定で進めますか");
  await caption("② 当日の逆算の元になるので、登録する前に一度確かめます", 2600);
  await click("#intake-go", 0);
  await caption("エージェントが衣装の候補を探しています");

  // 2. 衣装
  await waitFor("衣装をえらぶ");
  await caption("③ ドレスコードに合う候補を、費用の安い順に並べます", 3200);
  await caption("えらんでも、この時点ではまだ予約は入りません", 2400);
  await click("#stage button[data-outfit]", 0);

  // 3. 確認
  await waitFor("この内容で確定しますか");
  await caption("④ 受取場所を、移動時間・手数料・運賃を足し合わせて選びました", 3400);
  await click("details.compare > summary", 2400);
  await caption("ほかの受取場所との比較も見られます（経路と運賃は駅すぱあと API）", 3400);
  await scroll(360, 2000);
  await caption("お金が動く操作は、金額にかかわらず本人の承認を通します", 3000);
  await click("#approve", 0);
  await caption("予約を確定し、経路を探しています");

  // 4. 当日
  await waitFor("当日の流れ");
  await page.evaluate(() => window.scrollTo(0, 0));
  await caption("⑤ 承認すると、開式から逆算した当日の流れが決まります", 3400);
  await scroll(520, 4200);
  await caption("受取の手続きも着替えも乗換も、時間として計算に入っています", 3000);
  await scrollTop();

  // 5. 相談
  await caption("⑥ あとは「AIに相談」から。返却期限の注意もここに届きます", 1200);
  await click("#talk-open", 1200);
  let n = await agentCount();
  await click("#hints button:first-child", 0);
  await waitReply(n);
  n = await agentCount();
  await caption("自分の言葉で訊いても答えます", 600);
  await type("#say-text", "何時に家を出ればいい？");
  await page.keyboard.press("Enter");
  await waitReply(n);
  await sleep(1500);
  await caption("");
  await click("#talk-close", 1500);
  await recorder.stop();
  await browser.close();

  // 投稿先を選ばないよう mp4 にもしておく。
  const mp4 = `${OUT}/demo.mp4`;
  execFileSync("ffmpeg", ["-y", "-loglevel", "error", "-i", webm,
    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", "30", "-movflags", "+faststart", mp4]);
  console.log(`録画: ${mp4}`);
}

main().catch((err) => {
  console.error(err.message);
  process.exit(1);
});
