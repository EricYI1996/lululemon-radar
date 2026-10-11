#!/usr/bin/env node

import fs from 'node:fs/promises';
import https from 'node:https';
import crypto from 'node:crypto';
import zlib from 'node:zlib';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const OUTPUT = path.join(ROOT, 'glowmode.json');
const BASE = 'https://www.shopglowmode.com';
const LIST_PAGE = `${BASE}/market-us/category/Women-Activewear-1894?skcRuleId=1479&sort=new`;
const API = `${BASE}/api/act/bfs/v2/goods/list`;
const PAGE_SIZE = 80;
const MAX_PAGES = 3;
const HISTORY_DAYS = 95;
const USER_AGENT = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36';

function request(url, options = {}, body = null) {
  return new Promise((resolve, reject) => {
    const target = new URL(url);
    const req = https.request({
      hostname: target.hostname,
      port: 443,
      path: `${target.pathname}${target.search}`,
      method: options.method || 'GET',
      headers: options.headers || {},
      timeout: options.timeout || 30000
    }, res => {
      const chunks = [];
      res.on('data', chunk => chunks.push(chunk));
      res.on('end', () => {
        try {
          const raw = Buffer.concat(chunks);
          const encoding = String(res.headers['content-encoding'] || '').toLowerCase();
          let decoded = raw;
          if (encoding.includes('gzip')) decoded = zlib.gunzipSync(raw);
          else if (encoding.includes('br')) decoded = zlib.brotliDecompressSync(raw);
          else if (encoding.includes('deflate')) decoded = zlib.inflateSync(raw);
          resolve({
            status: res.statusCode || 0,
            headers: res.headers,
            text: decoded.toString('utf8')
          });
        } catch (error) {
          reject(error);
        }
      });
    });
    req.on('timeout', () => req.destroy(new Error('请求超时')));
    req.on('error', reject);
    if (body) req.write(body);
    req.end();
  });
}

async function withRetry(task, label, attempts = 3) {
  let lastError;
  for (let i = 1; i <= attempts; i += 1) {
    try {
      return await task();
    } catch (error) {
      lastError = error;
      if (i < attempts) await new Promise(resolve => setTimeout(resolve, i * 2500));
    }
  }
  throw new Error(`${label}失败：${lastError?.message || lastError}`);
}

function cookieHeader(setCookie = []) {
  const values = Array.isArray(setCookie) ? setCookie : [setCookie];
  return values.filter(Boolean).map(value => value.split(';', 1)[0]).join('; ');
}

async function createSession() {
  const response = await withRetry(() => request(LIST_PAGE, {
    headers: {
      'User-Agent': USER_AGENT,
      'Accept-Language': 'en-US,en;q=0.9',
      Accept: 'text/html,application/xhtml+xml'
    }
  }), '初始化 Glowmode 会话');
  if (response.status < 200 || response.status >= 400) {
    throw new Error(`初始化页面返回 HTTP ${response.status}`);
  }
  const serverCookies = cookieHeader(response.headers['set-cookie']);
  return [serverCookies, 'language=en', 'currency=USD', 'country=US'].filter(Boolean).join('; ');
}

async function fetchPage(page, cookies) {
  const payload = JSON.stringify({
    page,
    limit: PAGE_SIZE,
    filter_map: { cate_id: '1894' },
    skip_search_words: false,
    sort: 'new',
    top_goods_skc_rule_id: 1479,
    is_vcategory: false
  });
  const response = await withRetry(() => request(API, {
    method: 'POST',
    headers: {
      'User-Agent': USER_AGENT,
      'Accept-Language': 'en-US,en;q=0.9',
      Accept: 'application/json',
      'Content-Type': 'application/json',
      'Content-Length': Buffer.byteLength(payload),
      Origin: BASE,
      Referer: LIST_PAGE,
      Cookie: cookies,
      'x-sa-id': crypto.randomUUID()
    }
  }, payload), `第 ${page} 页抓取`);
  if (response.status !== 200) throw new Error(`HTTP ${response.status}`);
  const data = JSON.parse(response.text);
  const products = data?.info?.goodsList;
  if (data?.code !== '0' || !Array.isArray(products)) {
    throw new Error(`接口数据异常：${data?.msg || data?.code || '未知错误'}`);
  }
  return { total: Number(data.info.total || 0), products };
}

async function readPrevious() {
  try {
    const data = JSON.parse(await fs.readFile(OUTPUT, 'utf8'));
    const products = Array.isArray(data) ? data : data.products;
    return Array.isArray(products) ? products : [];
  } catch (error) {
    if (error.code === 'ENOENT') return [];
    throw error;
  }
}

function productId(product) {
  return String(product.origin_product_id || product.goods_id || product.skc || '');
}

function normalizeImage(url = '') {
  return url.startsWith('http://') ? `https://${url.slice(7)}` : url;
}

function prepareProducts(current, previous, capturedAt) {
  const previousById = new Map(previous.map(product => [productId(product), product]));
  const initialRun = previousById.size === 0;
  const baselineDate = new Date(Date.parse(capturedAt) - 91 * 86400000).toISOString();
  const currentIds = new Set();
  const products = [];

  for (const product of current) {
    const id = productId(product);
    if (!id || currentIds.has(id)) continue;
    currentIds.add(id);
    const old = previousById.get(id);
    const firstSeen = old?.first_seen_at || (initialRun && !product.is_new ? baselineDate : capturedAt);
    products.push({
      ...product,
      image_url: normalizeImage(product.image_url || ''),
      first_seen_at: firstSeen,
      last_seen_at: capturedAt,
      is_active: true
    });
  }

  const keepAfter = Date.parse(capturedAt) - HISTORY_DAYS * 86400000;
  for (const old of previous) {
    const id = productId(old);
    if (!id || currentIds.has(id)) continue;
    const firstSeen = Date.parse(old.first_seen_at || 0);
    if (firstSeen >= keepAfter) products.push({ ...old, is_active: false });
  }
  return products;
}

async function main() {
  const capturedAt = new Date().toISOString();
  const previous = await readPrevious();
  const cookies = await createSession();
  const pages = await Promise.all(
    Array.from({ length: MAX_PAGES }, (_, index) => fetchPage(index + 1, cookies))
  );
  const current = pages.flatMap(page => page.products);
  if (!current.length) throw new Error('Glowmode 返回空商品列表，已停止覆盖旧文件');
  const products = prepareProducts(current, previous, capturedAt);
  const result = {
    updated_at: capturedAt,
    source: LIST_PAGE,
    source_total: pages[0]?.total || current.length,
    tracked_total: products.length,
    products
  };
  await fs.writeFile(OUTPUT, `${JSON.stringify(result, null, 2)}\n`, 'utf8');
  console.log(`Glowmode 更新完成：当前抓取 ${current.length} 条，历史保留 ${products.length} 条。`);
}

main().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
