import { test, expect } from "@playwright/test";

const dataset = (id, name = id) => ({
  id, name, description: "", status: "draft", indexing_status: "not_started",
  permission: "only_me", visibility: "private", owner: "test", category: "通用知识",
  document_count: 0, chunk_count: 0, tags: [], embedding_model: null,
  created_at: "2026-09-05T00:00:00Z", updated_at: "2026-09-05T00:00:00Z",
});

async function mockAPI(page, overrides = {}) {
  const calls = [];
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    calls.push({ path: url.pathname, query: url.searchParams, method: request.method() });
    if (overrides.handler && await overrides.handler(route, url)) return;
    if (url.pathname.endsWith("/list")) {
      const pageNumber = Number(url.searchParams.get("page") || 1);
      const all = Array.from({ length: 11 }, (_, i) => dataset(`kb-${i + 1}`));
      return route.fulfill({ json: { items: all.slice((pageNumber - 1) * 10, pageNumber * 10), total: 11 } });
    }
    if (url.pathname.endsWith("/stats")) {
      return route.fulfill({ json: { total: 11, ready: 0, indexing: 0, draft: 11, failed: 0, documents: 0, chunks: 0 } });
    }
    if (url.pathname.endsWith("/documents")) {
      return route.fulfill({ json: { items: [], total: 0 } });
    }
    return route.fulfill({ status: 404, json: { detail: "not found" } });
  });
  return calls;
}

test("creation sends the strict contract and uploads into the created dataset", async ({ page }) => {
  let creation;
  let upload;
  await mockAPI(page, { handler: async (route, url) => {
    if (route.request().method() !== "POST") return false;
    if (url.pathname === "/api/knowledge_base") {
      creation = route.request().postDataJSON();
      if (Object.keys(creation).sort().join() !== "description,name,permission") {
        await route.fulfill({ status: 422, json: { detail: [{ msg: "Extra inputs are not permitted" }] } });
      } else await route.fulfill({ status: 201, json: dataset("new-id", creation.name) });
      return true;
    }
    if (url.pathname === "/api/knowledge_base/new-id/documents/upload") {
      upload = route.request().postDataBuffer().toString();
      await route.fulfill({ status: 201, json: {
        documents: [{ id: "doc-1", dataset_id: "new-id", name: "good.txt", status: "waiting", duplicate: false }],
        rejected: [{ filename: "bad.exe", code: "UNSUPPORTED_EXTENSION", message: "File type is not supported." }],
      } });
      return true;
    }
    return false;
  } });
  await page.goto("/");
  await page.getByRole("button", { name: "新建知识库" }).click();
  await page.getByLabel("知识库名称", { exact: true }).fill("New dataset");
  await page.getByRole("button", { name: "创建", exact: true }).click();
  await expect(page.getByRole("heading", { name: "New dataset" })).toBeVisible();
  expect(creation).toEqual({ name: "New dataset", description: "", permission: "only_me" });
  await page.getByLabel("上传文件", { exact: true }).setInputFiles([
    { name: "good.txt", mimeType: "text/plain", buffer: Buffer.from("hello") },
    { name: "bad.exe", mimeType: "application/octet-stream", buffer: Buffer.from("bad") },
  ]);
  await expect(page.getByText("上传成功 1 个文件")).toBeVisible();
  await expect(page.getByText(/bad.exe.*File type is not supported/)).toBeVisible();
  expect(upload).toContain('name="files"; filename="good.txt"');
  expect(upload).toContain('name="files"; filename="bad.exe"');
});

test("pagination reaches the eleventh record and resets after filtering", async ({ page }) => {
  const calls = await mockAPI(page);
  await page.goto("/");
  await expect(page.getByText("共 11 条记录", { exact: true })).toBeVisible();
  // 开发模式的 StrictMode 会重放初始 effect；只断言筛选/分页不增加统计请求。
  const initialStatsCalls = calls.filter(c => c.path.endsWith("/stats")).length;
  await page.getByRole("button", { name: "下一页", exact: true }).click();
  await expect(page.getByRole("button", { name: "kb-11", exact: true })).toBeVisible();
  await page.getByPlaceholder(/搜索名称/).fill("search");
  await expect.poll(() => calls.filter(c => c.path.endsWith("/list")).at(-1)?.query.get("q")).toBe("search");
  expect(calls.filter(c => c.path.endsWith("/list")).at(-1).query.get("page")).toBe("1");
  expect(calls.filter(c => c.path.endsWith("/stats"))).toHaveLength(initialStatsCalls);
});

test("a delayed old search cannot replace the latest results", async ({ page }) => {
  let release;
  let oldStarted;
  const started = new Promise(resolve => { oldStarted = resolve; });
  const delayed = new Promise(resolve => { release = resolve; });
  await mockAPI(page, { handler: async (route, url) => {
    if (!url.pathname.endsWith("/list")) return false;
    const q = url.searchParams.get("q");
    if (!q) return false;
    if (q === "old") { oldStarted(); await delayed; }
    await route.fulfill({ json: { items: [dataset(q)], total: 1 } }).catch(() => {});
    return true;
  } });
  await page.goto("/");
  await page.getByPlaceholder(/搜索名称/).fill("old");
  await started;
  await page.getByPlaceholder(/搜索名称/).fill("latest");
  await expect(page.getByText("latest", { exact: true })).toBeVisible();
  release();
  await page.waitForTimeout(350);
  await expect(page.getByText("latest", { exact: true })).toBeVisible();
  await expect(page.getByText("old", { exact: true })).toHaveCount(0);
});

test("structured API errors are readable and stay inside the creation dialog", async ({ page }) => {
  await mockAPI(page, { handler: async (route) => {
    if (route.request().method() !== "POST") return false;
    await route.fulfill({ status: 422, json: { detail: [{ loc: ["body", "name"], msg: "Invalid name", type: "value_error" }] } });
    return true;
  } });
  await page.goto("/");
  await page.getByRole("button", { name: "新建知识库" }).click();
  await page.getByLabel("知识库名称", { exact: true }).fill("Test");
  await page.getByRole("button", { name: "创建", exact: true }).click();
  await expect(page.getByRole("dialog").getByRole("alert")).toContainText("Invalid name");
});

test("stats failure does not hide the dataset list", async ({ page }) => {
  await mockAPI(page, { handler: async (route, url) => {
    if (!url.pathname.endsWith("/stats")) return false;
    await route.fulfill({ status: 503, json: { detail: "stats unavailable" } });
    return true;
  } });
  await page.goto("/");
  await expect(page.getByRole("button", { name: "kb-1", exact: true })).toBeVisible();
  await expect(page.getByRole("alert")).toContainText("stats unavailable");
});

test("deleting the last record on a page returns to the previous page", async ({ page }) => {
  let removed = false;
  await mockAPI(page, { handler: async (route, url) => {
    if (route.request().method() === "DELETE") {
      removed = true;
      await route.fulfill({ status: 204 });
      return true;
    }
    if (removed && url.pathname.endsWith("/list")) {
      const items = url.searchParams.get("page") === "2" ? [] : [dataset("remaining")];
      await route.fulfill({ json: { items, total: 10 } });
      return true;
    }
    return false;
  } });
  await page.goto("/");
  await page.getByRole("button", { name: "下一页", exact: true }).click();
  page.on("dialog", dialog => dialog.accept());
  await page.getByRole("button", { name: "删除 kb-11", exact: true }).click();
  await expect(page.getByRole("button", { name: "remaining", exact: true })).toBeVisible();
  await expect(page.getByText("第 1 / 1 页", { exact: true })).toBeVisible();
});

test("upload infrastructure failure refreshes partially committed documents", async ({ page }) => {
  let uploaded = false;
  await mockAPI(page, { handler: async (route, url) => {
    if (!/\/documents(?:\/upload)?$/.test(url.pathname)) return false;
    if (route.request().method() === "POST") {
      uploaded = true;
      await route.fulfill({ status: 503, json: { detail: "storage unavailable" } });
    } else await route.fulfill({ json: { items: uploaded ? [
      { id: "partial", dataset_id: "kb-1", name: "saved.txt", status: "waiting", duplicate: false },
    ] : [], total: uploaded ? 1 : 0 } });
    return true;
  } });
  await page.goto("/");
  await page.getByRole("button", { name: "kb-1", exact: true }).click();
  await page.getByLabel("上传文件", { exact: true }).setInputFiles({ name: "saved.txt", mimeType: "text/plain", buffer: Buffer.from("hello") });
  await expect(page.getByRole("alert")).toContainText("storage unavailable");
  await expect(page.getByRole("cell", { name: "saved.txt", exact: true })).toBeVisible();
});

for (const viewport of [{ width: 1440, height: 900 }, { width: 390, height: 844 }]) {
  test(`layout remains usable at ${viewport.width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize(viewport);
    await mockAPI(page);
    await page.goto("/");
    await expect(page.getByText("共 11 条记录", { exact: true })).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({ path: testInfo.outputPath(`list-${viewport.width}.png`), fullPage: true });
    await page.getByRole("button", { name: "新建知识库" }).click();
    await expect(page.getByRole("dialog")).toBeVisible();
    const bounds = await page.getByRole("dialog").boundingBox();
    expect(bounds.x).toBeGreaterThanOrEqual(0);
    expect(bounds.x + bounds.width).toBeLessThanOrEqual(viewport.width);
    await page.screenshot({ path: testInfo.outputPath(`create-${viewport.width}.png`), fullPage: true });
    await page.keyboard.press("Escape");
    await page.getByRole("button", { name: "kb-1", exact: true }).click();
    await expect(page.getByText("暂无文档", { exact: true })).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({ path: testInfo.outputPath(`documents-${viewport.width}.png`), fullPage: true });
    // 空库是当前本机的常见状态，也纳入截图，避免只看有数据的表格。
    await page.route("**/api/knowledge_base/list?**", route => route.fulfill({ json: { items: [], total: 0 } }));
    await page.route("**/api/knowledge_base/stats", route => route.fulfill({ json: {
      total: 0, ready: 0, indexing: 0, draft: 0, failed: 0, documents: 0, chunks: 0,
    } }));
    await page.reload();
    await expect(page.getByText("暂无知识库", { exact: true })).toBeVisible();
    await page.screenshot({ path: testInfo.outputPath(`empty-${viewport.width}.png`), fullPage: true });
  });
}

test("failed pagination never labels old rows as the new page", async ({ page }) => {
  await mockAPI(page, { handler: async (route, url) => {
    if (!url.pathname.endsWith("/list") || url.searchParams.get("page") !== "2") return false;
    await route.fulfill({ status: 503, json: { detail: "list unavailable" } });
    return true;
  } });
  await page.goto("/");
  await expect(page.getByRole("button", { name: "kb-1", exact: true })).toBeVisible();
  await page.getByRole("button", { name: "下一页", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("list unavailable");
  await expect(page.getByRole("button", { name: "kb-1", exact: true })).toHaveCount(0);
});

test("document pagination errors hide stale rows and clear after retry", async ({ page }) => {
  let failed = false;
  await mockAPI(page, { handler: async (route, url) => {
    if (!url.pathname.endsWith("/documents")) return false;
    if (url.searchParams.get("page") === "2" && !failed) {
      failed = true;
      await route.fulfill({ status: 503, json: { detail: "documents unavailable" } });
    } else await route.fulfill({ json: { items: [{
      id: "doc", dataset_id: "kb-1", name: url.searchParams.get("page") === "2" ? "second.txt" : "first.txt",
      status: "waiting", duplicate: false,
    }], total: 21 } });
    return true;
  } });
  await page.goto("/");
  await page.getByRole("button", { name: "kb-1", exact: true }).click();
  await page.getByRole("button", { name: "下一页", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("documents unavailable");
  await expect(page.getByRole("cell", { name: "first.txt", exact: true })).toHaveCount(0);
  await page.getByRole("button", { name: "刷新文档", exact: true }).click();
  await expect(page.getByRole("cell", { name: "second.txt", exact: true })).toBeVisible();
  await expect(page.getByRole("alert")).toHaveCount(0);
});
