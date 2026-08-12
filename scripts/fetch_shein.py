#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SHEIN 品牌店铺上新抓取脚本（第一版尝试）
===============================================================
目标：
- 监控配置里的 SHEIN 品牌店铺页（store_code）
- 使用 Playwright 在 GitHub Actions 的浏览器环境中抓取商品卡片
- 输出仓库根目录 shein.json，供前端看板静态读取

注意：
- SHEIN 对服务端直连有明显风控，第一版以浏览器渲染 + DOM 提取为主
- 若命中 risk challenge / system updating，不抛致命错误，仍写出 shein.json
- 这样即使 SHEIN 暂时失败，也不会阻塞其它品牌 JSON 的生成与提交
"""

from __future__ import annotations

import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

ROOT = Path(__file__).resolve().parents[1]
OUT_FILE = ROOT / "shein.json"
SOURCES_FILE = Path(__file__).resolve().with_name("shein_sources.json")


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def clean_text(val: str) -> str:
    return re.sub(r"\s+", " ", (val or "")).strip()


def parse_price(text: str):
    if not text:
        return None
    m = re.search(r"\$\s*([0-9]+(?:\.[0-9]{1,2})?)", text)
    return float(m.group(1)) if m else None


def slug_from_url(url: str) -> str:
    path = urlparse(url).path or ""
    return path.rstrip("/").split("/")[-1]


def extract_products(page, brand_name: str, store_code: str):
    js = r"""
    ({ brandName, storeCode }) => {
      const uniq = new Map();
      const anchors = Array.from(document.querySelectorAll('a[href*="-p-"]'));
      for (const a of anchors) {
        const href = a.getAttribute('href') || '';
        if (!href || href.includes('/store/')) continue;
        const abs = new URL(href, location.origin).href;
        if (uniq.has(abs)) continue;

        const container = a.closest('section, article, li, div');
        const text = (container?.innerText || a.innerText || '').replace(/\s+/g, ' ').trim();
        const title = (a.getAttribute('title') || a.textContent || text.split('$')[0] || '').replace(/\s+/g, ' ').trim();
        const imgNode = (container || a).querySelector('img');
        const img = imgNode?.getAttribute('src') || imgNode?.getAttribute('data-src') || imgNode?.getAttribute('data-lazy-src') || '';
        const soldText = /\b\d+[\d,.+]*\s*(sold|reviews?)\b/i.exec(text)?.[0] || '';
        const discount = /-\d+%/.exec(text)?.[0] || '';
        const prices = [...text.matchAll(/\$\s*([0-9]+(?:\.[0-9]{1,2})?)/g)].map(m => Number(m[1]));
        const tags = [];
        if (/\btrends?\b/i.test(text)) tags.push('Trends');
        if (/\blocal\b/i.test(text)) tags.push('Local');

        uniq.set(abs, {
          brandName,
          storeCode,
          title,
          url: abs,
          image: img,
          price: prices[0] ?? null,
          salePrice: prices[1] ?? null,
          discount,
          soldText,
          tags,
          rawText: text.slice(0, 600)
        });
      }
      return Array.from(uniq.values());
    }
    """
    return page.evaluate(js, {"brandName": brand_name, "storeCode": store_code})


def detect_block(page) -> str:
    try:
        url = page.url or ""
        body = clean_text(page.locator("body").inner_text(timeout=5000)[:2500])
    except Exception:
        url, body = "", ""
    if "/risk/challenge" in url:
        return "risk_challenge"
    if "System Updating" in body:
        return "system_updating"
    if "Access Denied" in body:
        return "access_denied"
    return ""


def scrape_store(playwright, store: dict):
    brand = store["brand"]
    store_code = store["storeCode"]
    url = store["url"]
    result = {"brand": brand, "storeCode": store_code, "url": url, "ok": False, "count": 0}

    browser = playwright.chromium.launch(headless=True)
    context = browser.new_context(
        locale="en-US",
        viewport={"width": 1440, "height": 1800},
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/126.0.0.0 Safari/537.36"
        ),
    )
    page = context.new_page()
    page.set_extra_http_headers({"Accept-Language": "en-US,en;q=0.9"})

    try:
        print(f"[shein] open store {brand} ({store_code})", flush=True)
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3500)

        # 下拉几次，尽量把首屏后的商品卡片刷出来
        for _ in range(5):
            page.mouse.wheel(0, 2400)
            page.wait_for_timeout(1800)

        block = detect_block(page)
        if block:
            result["error"] = block
            print(f"  -> blocked: {block}", flush=True)
            return result, []

        products = extract_products(page, brand, store_code)
        if not products:
            result["error"] = "no_product_cards_found"
            print("  -> no product cards found", flush=True)
            return result, []

        # 补充 slug，去重，清洗空标题
        cleaned = []
        seen = set()
        for p in products:
            p["title"] = clean_text(p.get("title") or "") or slug_from_url(p["url"]).replace("-", " ")
            p["image"] = urljoin(page.url, p.get("image") or "") if p.get("image") else ""
            p["slug"] = slug_from_url(p["url"])
            key = p["url"]
            if key in seen:
                continue
            seen.add(key)
            cleaned.append(p)

        result["ok"] = True
        result["count"] = len(cleaned)
        print(f"  -> ok: {len(cleaned)} items", flush=True)
        return result, cleaned
    finally:
        context.close()
        browser.close()


def main():
    if not SOURCES_FILE.exists():
        raise FileNotFoundError(f"missing config: {SOURCES_FILE}")
    conf = json.loads(SOURCES_FILE.read_text(encoding="utf-8"))
    stores = conf.get("stores") or []
    if not stores:
        raise RuntimeError("shein_sources.json 没有 stores 配置")

    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        payload = {
            "fetchedAt": now_iso(),
            "source": "SHEIN store pages",
            "count": 0,
            "products": [],
            "stores": stores,
            "errors": [{"scope": "bootstrap", "message": f"playwright unavailable: {type(e).__name__}: {e}"}],
        }
        OUT_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print("[shein] playwright unavailable, wrote empty shein.json", flush=True)
        return 0

    all_products = []
    errors = []
    summaries = []

    with sync_playwright() as p:
        for store in stores:
            try:
                summary, products = scrape_store(p, store)
                summaries.append(summary)
                if summary.get("ok"):
                    all_products.extend(products)
                else:
                    errors.append(summary)
            except Exception as e:
                msg = {"brand": store.get("brand"), "storeCode": store.get("storeCode"), "error": f"{type(e).__name__}: {e}"}
                print(f"  -> exception: {msg['error']}", flush=True)
                summaries.append({**store, "ok": False, "count": 0, "error": msg["error"]})
                errors.append(msg)

    payload = {
        "fetchedAt": now_iso(),
        "source": "SHEIN store pages",
        "count": len(all_products),
        "products": all_products,
        "stores": summaries,
        "errors": errors,
        "note": "第一版为 Playwright DOM 抓取；若命中 SHEIN risk challenge，将保留错误信息并返回空列表。",
    }
    OUT_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[shein] wrote {len(all_products)} items -> {OUT_FILE}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
