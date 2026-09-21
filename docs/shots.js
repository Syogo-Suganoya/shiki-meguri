/**
 * 紹介ページ（web/index.html）に載せる画面の写しを撮る。
 *
 *   docker compose --profile shots up --build shots
 *
 * 利用者と同じ順に操作して撮るので、画面を変えたら撮り直すだけで追随する。
 * 利用者 id はブラウザごとに作られ、起動のたびに新しいプロファイルになるので、
 * 毎回「予定を入れる」から撮れる（エミュレータを空にする必要はない）。
 */

const fs = require("fs");
const puppeteer = require("puppeteer");

const BASE = process.env.BASE_URL || "http://api:8080";
const OUT = process.env.OUT_DIR || "/work/web/shots";
const WIDTH = 1120;

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function main() {
  fs.mkdirSync(OUT, { recursive: true });
  const browser = await puppeteer.launch({
    executablePath: process.env.CHROME_BIN || "/usr/bin/chromium-browser",
    args: ["--no-sandbox", "--disable-dev-shm-usage", "--font-render-hinting=none"],
  });
  const page = await browser.newPage();
  await page.setViewport({ width: WIDTH, height: 860, deviceScaleFactor: 2 });

  const shot = async (name, height) => {
    await page.setViewport({ width: WIDTH, height, deviceScaleFactor: 2 });
    // 直前の操作でページが送られていることがある。頭出しを揃える。
    await page.evaluate(() => window.scrollTo(0, 0));
    // 通知の帯が写り込むと画面の説明と食い違う。消えるまで待つ（最長6秒）。
    await page
      .waitForFunction(() => !document.getElementById("toast")?.classList.contains("show"), {
        timeout: 6000,
      })
      .catch(() => {});
    await sleep(400);
    await page.screenshot({ path: `${OUT}/${name}.png` });
    console.log(`撮影: ${name}.png`);
  };
  // 画面が切り替わるまで待つ。見出しの文言で判断する。
  const waitFor = async (heading) => {
    try {
      await page.waitForFunction(
        (t) => document.querySelector("#stage h1")?.textContent.includes(t),
        { timeout: 20000 },
        heading,
      );
    } catch (e) {
      // 何が出ていたのかが分からないと直しようがない。
      const seen = await page.evaluate(() => document.body.innerText.slice(0, 300));
      throw new Error(`「${heading}」が出なかった。画面にはこれが出ていた:\n${seen}`);
    }
    await sleep(700);
  };
  page.on("console", (m) => { if (m.type() === "error") console.log(`[画面] ${m.text()}`); });
  page.on("pageerror", (e) => console.log(`[画面] ${e.message}`));
  const click = async (selector) => {
    await page.waitForSelector(selector, { timeout: 20000 });
    await page.click(selector);
  };

  await page.goto(`${BASE}/ui/console.html`, { waitUntil: "networkidle0" });
  await waitFor("式の予定");
  if (await page.$("#stage form")) {
    await shot("01-plan", 1060);
    await click("#intake-form button[type=submit]");
    await waitFor("この予定で進めますか");
    await shot("02-check", 690);
    await click("#intake-go");
  } else {
    throw new Error("入力フォームが出なかった。予定の登録前の画面から撮る必要がある。");
  }

  await waitFor("衣装をえらぶ");
  await shot("03-outfit", 850);

  await click("#stage button[data-outfit]");
  await waitFor("この内容で確定しますか");
  await shot("04-confirm", 860);

  await click("#approve");
  await waitFor("当日の流れ");
  await shot("05-day", 1060);

  await click("#talk-open");
  await sleep(900);
  await shot("06-chat", 890);

  await browser.close();
}

main().catch((err) => {
  console.error(err.message);
  process.exit(1);
});
