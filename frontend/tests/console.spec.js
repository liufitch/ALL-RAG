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

test("new knowledge base continues from upload through preview to live indexing progress", async ({ page }) => {
  let uploaded = false;
  let jobReads = 0;
  let previewIds;
  await mockAPI(page, { handler: async (route, url) => {
    const method = route.request().method();
    const path = url.pathname;
    if (path === "/api/knowledge_base" && method === "POST") {
      await route.fulfill({ status: 201, json: dataset("flow-1", "Flow KB") });
      return true;
    }
    if (path === "/api/knowledge_base/flow-1/documents/upload" && method === "POST") {
      uploaded = true;
      await route.fulfill({ status: 201, json: { documents: [
        { id: "doc-1", dataset_id: "flow-1", name: "guide.txt", status: "waiting", duplicate: false },
      ], rejected: [] } });
      return true;
    }
    if (path === "/api/knowledge_base/flow-1/documents" && method === "GET") {
      await route.fulfill({ json: { items: uploaded ? [
        { id: "doc-1", dataset_id: "flow-1", name: "guide.txt", status: "waiting", duplicate: false },
      ] : [], total: uploaded ? 1 : 0 } });
      return true;
    }
    if (path === "/api/indexing/options") {
      await route.fulfill({ json: { indexing_techniques: [{ id: "economy" }], embedding_models: [],
        segmentation_modes: [{ id: "general", supported_indexing_techniques: ["economy"] }],
        defaults: { indexing_technique: "economy", embedding_model: "", general: { max_chunk_length: 1024, overlap: 100 },
          parent_child: { parent_mode: "paragraph", parent_max_chunk_length: 4096, child_max_chunk_length: 512, child_overlap: 50 } } } });
      return true;
    }
    if (path.endsWith("/indexing/preview") && method === "POST") {
      previewIds = route.request().postDataJSON().document_ids;
      await route.fulfill({ json: { chunks: [{ id: "chunk-1", document_id: "doc-1", local_id: "1", parent_id: null,
        position: 0, content: "hello from guide", source_metadata: { page: 1 }, index_type: "general" }],
        total_chunks: 1, truncated: false, warnings: [], documents: [] } });
      return true;
    }
    if (path.endsWith("/indexing/configure") && method === "POST") {
      await route.fulfill({ status: 202, json: { rule_id: "rule-1", job_id: "job-1", document_ids: ["doc-1"], status: "queued" } });
      return true;
    }
    if (path.endsWith("/indexing/jobs") && method === "GET") {
      await route.fulfill({ json: { items: [], total: 0 } });
      return true;
    }
    if (path.endsWith("/indexing/jobs/job-1") && method === "GET") {
      jobReads += 1;
      const completed = jobReads > 2;
      await route.fulfill({ json: { id: "job-1", dataset_id: "flow-1", job_type: "index", scope: "selected", status: completed ? "completed" : "running",
        progress: completed ? 100 : 45, current_stage: completed ? "completed" : "splitting", total_documents: 1,
        processed_documents: completed ? 1 : 0, completed_documents: completed ? 1 : 0, failed_documents: 0,
        error_code: null, error: null, cancel_requested: false, created_at: null, updated_at: null,
        documents: [{ id: "jd-1", document_id: "doc-1", status: completed ? "completed" : "running", progress: completed ? 100 : 45,
          current_stage: completed ? "completed" : "splitting", total_segments: 1, processed_segments: completed ? 1 : 0,
          embedded_segments: 0, attempt: 1, error_code: null, error: null }] } });
      return true;
    }
    return false;
  } });
  await page.goto("/");
  await page.getByRole("button", { name: "新建知识库" }).click();
  await page.getByLabel("知识库名称", { exact: true }).fill("Flow KB");
  await page.getByRole("button", { name: "创建", exact: true }).click();
  await expect(page.getByText("上传文件以继续建立索引")).toBeVisible();
  await page.getByLabel("上传文件", { exact: true }).setInputFiles({ name: "guide.txt", mimeType: "text/plain", buffer: Buffer.from("hello") });
  await expect(page.getByRole("heading", { name: "配置并预览索引" })).toBeVisible();
  await page.getByRole("button", { name: "预览块" }).click();
  await expect(page.getByText("hello from guide")).toBeVisible();
  expect(previewIds).toEqual(["doc-1"]);
  await page.getByRole("button", { name: "确认并开始索引" }).click();
  await expect(page.getByRole("region", { name: "索引任务进度" })).toContainText("45%");
  await expect(page.getByRole("region", { name: "索引任务进度" })).toContainText("已完成");
});

test("parent child configuration previews children under parents and saves full document mode", async ({ page }) => {
  let previewConfig;
  let savedConfig;
  await mockAPI(page, { handler: async (route, url) => {
    const path = url.pathname;
    if (path === "/api/knowledge_base/kb-1/documents") {
      await route.fulfill({ json: { items: [{ id: "doc-1", dataset_id: "kb-1", name: "guide.txt", status: "waiting" }], total: 1 } });
      return true;
    }
    if (path === "/api/indexing/options") {
      await route.fulfill({ json: { indexing_techniques: [{ id: "high_quality" }, { id: "economy" }],
        embedding_models: [{ id: "embed-1", name: "Embedding 1" }],
        segmentation_modes: [{ id: "general", supported_indexing_techniques: ["high_quality", "economy"] },
          { id: "parent_child", supported_indexing_techniques: ["high_quality"] }],
        defaults: { indexing_technique: "economy", embedding_model: "embed-1",
          general: { max_chunk_length: 1024, overlap: 100 },
          parent_child: { parent_mode: "paragraph", parent_max_chunk_length: 4096, child_max_chunk_length: 512, child_overlap: 50 } } } });
      return true;
    }
    if (path.endsWith("/indexing/preview")) {
      previewConfig = route.request().postDataJSON();
      await route.fulfill({ json: { chunks: [
        { id: "doc-1:p-1", document_id: "doc-1", local_id: "p-1", parent_id: null, position: 0,
          content: "Parent context", source_metadata: { page: 2 }, index_type: "parent" },
        { id: "doc-1:c-1", document_id: "doc-1", local_id: "c-1", parent_id: "doc-1:p-1", position: 1,
          content: "First child", source_metadata: { page: 2 }, index_type: "child" },
        { id: "doc-1:c-2", document_id: "doc-1", local_id: "c-2", parent_id: "doc-1:p-1", position: 2,
          content: "Second child", source_metadata: { page: 2 }, index_type: "child" },
      ], total_chunks: 3, truncated: false, warnings: [], documents: [] } });
      return true;
    }
    if (path.endsWith("/indexing/configure")) {
      savedConfig = route.request().postDataJSON();
      await route.fulfill({ status: 202, json: { job_id: "job-1", status: "queued" } });
      return true;
    }
    if (path.endsWith("/indexing/jobs")) {
      await route.fulfill({ json: { items: [], total: 0 } });
      return true;
    }
    return false;
  } });
  await page.goto("/");
  await page.getByRole("button", { name: "kb-1", exact: true }).click();
  await page.getByRole("button", { name: "配置索引" }).click();
  await page.locator(".indexing-form fieldset").filter({ hasText: "分段方式" }).locator("select").selectOption("parent_child");
  await expect(page.getByRole("button", { name: "高质量" })).toHaveAttribute("aria-pressed", "true");
  await page.getByRole("combobox", { name: "父块模式" }).selectOption("full_document");
  await page.getByRole("button", { name: "预览块" }).click();
  await expect(page.getByRole("group", { name: "父块 1" })).toContainText("Parent context");
  await expect(page.getByRole("group", { name: "父块 1" })).toContainText("First child");
  await expect(page.getByRole("group", { name: "父块 1" })).toContainText("Second child");
  expect(previewConfig.segmentation.parent_mode).toBe("full_document");
  expect(previewConfig.indexing_technique).toBe("high_quality");
  await page.getByRole("button", { name: "确认并开始索引" }).click();
  expect(savedConfig.segmentation.parent_mode).toBe("full_document");
});

test("cancelled task without failed documents does not offer an ineligible retry", async ({ page }) => {
  await mockAPI(page, { handler: async (route, url) => {
    if (url.pathname === "/api/knowledge_base/kb-1/indexing/jobs") {
      await route.fulfill({ json: { items: [{ id: "cancelled-job" }], total: 1 } });
      return true;
    }
    if (url.pathname === "/api/knowledge_base/kb-1/indexing/jobs/cancelled-job") {
      await route.fulfill({ json: { id: "cancelled-job", dataset_id: "kb-1", job_type: "initial_index", scope: "selected",
        status: "cancelled", progress: 20, current_stage: null, total_documents: 1, processed_documents: 0,
        completed_documents: 0, failed_documents: 0, error_code: null, error: null, cancel_requested: true,
        created_at: null, updated_at: null, documents: [{ id: "jd-1", document_id: "doc-1", status: "cancelled", progress: 20,
          current_stage: null, total_segments: 0, processed_segments: 0, embedded_segments: 0, attempt: 1,
          error_code: null, error: null }] } });
      return true;
    }
    return false;
  } });
  await page.goto("/");
  await page.getByRole("button", { name: "kb-1", exact: true }).click();
  await expect(page.getByRole("region", { name: "索引任务进度" })).toContainText("已取消");
  await expect(page.getByRole("button", { name: "重试失败文档" })).toHaveCount(0);
});

test("retrieval streams original evidence and jumps to the cited source", async ({ page }) => {
  let requestBody;
  await mockAPI(page, { handler: async (route, url) => {
    if (url.pathname !== "/api/knowledge_base/kb-1/retrieve/stream") return false;
    requestBody = route.request().postDataJSON();
    await route.fulfill({ status: 200, headers: { "content-type": "text/event-stream" }, body: [
      'event: hit\ndata: {"reference":1,"segment_id":"seg-1","document_id":"doc-1","document_name":"guide.txt","content":"PostgreSQL stores metadata.","source_metadata":{"page":2},"score":0.9,"match_type":"keyword"}\n\n',
      'event: done\ndata: {"count":1}\n\n',
    ].join("") });
    return true;
  } });
  await page.goto("/");
  await page.getByRole("button", { name: "kb-1", exact: true }).click();
  await page.getByRole("button", { name: "检索证据" }).click();
  await page.getByLabel("检索问题").fill("元数据在哪里？");
  await page.getByRole("button", { name: "检索", exact: true }).click();
  await expect(page.getByRole("region", { name: "检索证据结果" })).toContainText("PostgreSQL stores metadata.");
  await page.getByRole("link", { name: "来源 1" }).click();
  await expect(page.locator("#source-kb-1-1")).toHaveClass(/active/);
  await expect(page.locator("#source-kb-1-1")).toContainText("PostgreSQL stores metadata.");
  await expect(page).toHaveURL(/#source-kb-1-1$/);
  expect(requestBody).toEqual({ query: "元数据在哪里？" });
});

test("failed retrieval stream removes incomplete evidence results", async ({ page }) => {
  await mockAPI(page, { handler: async (route, url) => {
    if (url.pathname !== "/api/knowledge_base/kb-1/retrieve/stream") return false;
    await route.fulfill({ status: 200, headers: { "content-type": "text/event-stream" }, body: [
      'event: hit\ndata: {"reference":1,"segment_id":"s1","document_id":"d1","document_name":"guide.txt","content":"Incomplete evidence","source_metadata":{},"score":0.8,"match_type":"keyword"}\n\n',
      'event: error\ndata: {"code":"RETRIEVAL_FAILED","message":"检索服务暂时不可用。"}\n\n',
    ].join("") });
    return true;
  } });
  await page.goto("/");
  await page.getByRole("button", { name: "kb-1", exact: true }).click();
  await page.getByRole("button", { name: "检索证据" }).click();
  await page.getByLabel("检索问题").fill("Question?");
  await page.getByRole("button", { name: "检索", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("检索服务暂时不可用");
  await expect(page.getByText("Incomplete evidence")).toHaveCount(0);
  await expect(page.getByRole("region", { name: "检索证据结果" })).toHaveCount(0);
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
