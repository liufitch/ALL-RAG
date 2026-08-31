# Dify-Style Dataset Frontend Workflow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将当前单弹窗页面改为“创建空知识库 → 上传文档 → 配置/真实预览 → 异步处理进度”的可刷新路由流程。

**Architecture:** React Router 按 dataset URL 恢复页面状态，API 模块集中处理错误和 JSON，feature 组件按三步职责拆分。任务轮询 hook 只读取 PostgreSQL 驱动的 API 状态，并在终态或卸载时停止。

**Tech Stack:** React 19、React Router、Vite 8、Vitest、jsdom、React Testing Library。

**Spec:** `docs/superpowers/specs/2026-08-31-dify-style-dataset-indexing-design.md`

## Global Constraints

- 创建知识库弹窗只显示名称、描述和权限。
- 页面任何位置不得出现可编辑 Milvus host、port、collection、dimension 或 metric。
- 高质量模式显示后端模型列表；经济模式隐藏模型。
- 父子分段必须自动切换并锁定高质量。
- 预览只在用户点击“预览块”时请求，编辑参数后标记需要刷新。
- 支持上传提示必须包含 TXT、MD、PDF、DOCX、XLS/XLSX、CSV。
- 任务运行时轮询，终态停止；后台标签页降低频率。
- API 错误优先显示稳定 `message`，不得把原始 HTML/stack 直接渲染。

---

## File Structure

- Modify: `frontend/package.json` — Router 和测试依赖/脚本。
- Modify: `frontend/vite.config.js` — Vitest jsdom 配置。
- Create: `frontend/src/test/setup.js` — jest-dom 和浏览器 mock。
- Create: `frontend/src/api/client.js` — `requestJson`/`ApiError`。
- Create: `frontend/src/api/datasets.js`。
- Create: `frontend/src/api/documents.js`。
- Create: `frontend/src/api/indexing.js`。
- Create: `frontend/src/components/Modal.jsx`。
- Create: `frontend/src/components/StatusBadge.jsx`。
- Create: `frontend/src/components/ProgressBar.jsx`。
- Create: `frontend/src/features/datasets/CreateDatasetModal.jsx`。
- Create: `frontend/src/features/datasets/DocumentUploadStep.jsx`。
- Create: `frontend/src/features/datasets/IndexingSettingsStep.jsx`。
- Create: `frontend/src/features/datasets/ChunkPreview.jsx`。
- Create: `frontend/src/features/datasets/IndexingProgressStep.jsx`。
- Create: `frontend/src/hooks/useIndexingJob.js`。
- Create: `frontend/src/pages/DatasetListPage.jsx`。
- Create: `frontend/src/pages/DatasetDocumentsPage.jsx`。
- Create: `frontend/src/pages/DatasetProcessPage.jsx`。
- Modify: `frontend/src/App.jsx` — 路由壳，不再承载业务表单细节。
- Modify: `frontend/src/main.jsx` — BrowserRouter。
- Modify: `frontend/src/styles.css` — 新页面布局和响应式样式。
- Test: matching `*.test.jsx`/`*.test.js` files next to features/hooks/api。

### Task 1: Test Harness, API Client, and Routes

**Interfaces:**
- Produces: `requestJson(path, options) -> Promise<unknown>` and `ApiError`。
- Produces routes `/datasets`, `/datasets/:datasetId/documents`, `/datasets/:datasetId/process`。

- [ ] **Step 1: Add failing API and routing tests**

```javascript
// frontend/src/api/client.test.js
it("maps the structured backend error", async () => {
  fetch.mockResolvedValue(new Response(JSON.stringify({ code: "BAD", message: "请求错误", request_id: "r1" }), {
    status: 422,
    headers: { "Content-Type": "application/json" },
  }));
  await expect(requestJson("/api/test")).rejects.toMatchObject({ code: "BAD", message: "请求错误" });
});

// frontend/src/App.test.jsx
it("restores the dataset documents page from the URL", async () => {
  renderAt("/datasets/dataset-1/documents");
  expect(await screen.findByRole("heading", { name: /文档/ })).toBeInTheDocument();
});
```

- [ ] **Step 2: Install dependencies and verify tests fail**

Add scripts/dependencies, then run:

```bash
npm --prefix frontend install
npm --prefix frontend test -- --run
```

Expected: FAIL because API client/routes do not exist.

- [ ] **Step 3: Implement the minimal client and route shell**

Add:

```json
"scripts": { "test": "vitest", "test:run": "vitest run" }
```

and dependencies `react-router-dom`; dev dependencies `vitest`, `jsdom`, `@testing-library/react`, `@testing-library/jest-dom`, `@testing-library/user-event`.

Implement `requestJson` to prepend `VITE_API_BASE`, set JSON content type only for non-FormData bodies, parse structured errors, and treat 204 as `undefined`. `App` contains only layout plus `<Routes>`; unknown routes redirect to `/datasets`.

- [ ] **Step 4: Run frontend base tests**

Run: `npm --prefix frontend test -- --run src/api/client.test.js src/App.test.jsx`

Expected: PASS.

- [ ] **Step 5: Commit frontend foundation**

```bash
git add frontend/package.json frontend/package-lock.json frontend/vite.config.js frontend/src/test frontend/src/api/client.js frontend/src/api/client.test.js frontend/src/App.jsx frontend/src/App.test.jsx frontend/src/main.jsx
git commit -m "feat: add dataset frontend routing foundation"
```

### Task 2: Dataset List and Empty Dataset Modal

**Interfaces:**
- Produces: `listDatasets(filters)`, `getDatasetStats()`, `createDataset(payload)`。
- `CreateDatasetModal.onCreated(dataset)` navigates to `/datasets/{id}/documents`。

- [ ] **Step 1: Write failing UI contracts**

```javascript
// frontend/src/features/datasets/CreateDatasetModal.test.jsx
it("only asks for empty-dataset fields and navigates after create", async () => {
  renderCreateModal();
  expect(screen.getByLabelText("知识库名称")).toBeInTheDocument();
  expect(screen.getByLabelText("描述")).toBeInTheDocument();
  expect(screen.getByLabelText("权限")).toBeInTheDocument();
  expect(screen.queryByText(/Milvus|Collection|向量维度|Embedding 模型/)).not.toBeInTheDocument();

  await user.type(screen.getByLabelText("知识库名称"), "产品知识库");
  await user.click(screen.getByRole("button", { name: "创建" }));
  expect(mockNavigate).toHaveBeenCalledWith("/datasets/dataset-1/documents");
});
```

- [ ] **Step 2: Run and verify feature absence**

Run: `npm --prefix frontend test -- --run src/features/datasets/CreateDatasetModal.test.jsx`

Expected: FAIL on missing component.

- [ ] **Step 3: Extract list page and implement minimal modal**

Move list/stats/filter/delete behavior out of `App.jsx` into `DatasetListPage`. Submit exactly:

```javascript
{ name: form.name.trim(), description: form.description.trim(), permission: form.permission }
```

Do not send category, owner, tags, retrieval config, embedding model or vector store. On success close the modal and call `navigate(`/datasets/${dataset.id}/documents`)` without waiting for another list refresh.

- [ ] **Step 4: Run list/modal tests**

Run: `npm --prefix frontend test -- --run src/features/datasets/CreateDatasetModal.test.jsx src/pages/DatasetListPage.test.jsx`

Expected: PASS; a serialized DOM scan contains no Milvus configuration labels.

- [ ] **Step 5: Commit empty dataset UI**

```bash
git add frontend/src/api/datasets.js frontend/src/components/Modal.jsx frontend/src/features/datasets/CreateDatasetModal.jsx frontend/src/features/datasets/CreateDatasetModal.test.jsx frontend/src/pages/DatasetListPage.jsx frontend/src/pages/DatasetListPage.test.jsx frontend/src/App.jsx
git commit -m "feat: create empty datasets from the frontend"
```

### Task 3: Document Upload Step

**Interfaces:**
- Produces: `uploadDocuments(datasetId, files)` and `listDocuments(datasetId, filters)`。
- `DocumentUploadStep.onContinue(documentIds)` navigates to process page.

- [ ] **Step 1: Write failing upload interaction test**

```javascript
// frontend/src/features/datasets/DocumentUploadStep.test.jsx
it("uploads allowed files and displays per-file rejection", async () => {
  renderUploadStep({ uploadResult: {
    documents: [{ id: "doc-1", name: "guide.pdf", status: "waiting" }],
    rejected: [{ filename: "legacy.doc", code: "UNSUPPORTED_FILE_TYPE", message: "不支持 .doc" }],
  }});
  await user.upload(screen.getByLabelText("选择文件"), [pdfFile(), docFile()]);
  await user.click(screen.getByRole("button", { name: "上传" }));
  expect(await screen.findByText("guide.pdf")).toBeInTheDocument();
  expect(screen.getByText("不支持 .doc")).toBeInTheDocument();
  expect(screen.getByText(/TXT.*MD.*PDF.*DOCX.*XLS.*XLSX.*CSV/i)).toBeInTheDocument();
});
```

- [ ] **Step 2: Run and verify component missing**

Run: `npm --prefix frontend test -- --run src/features/datasets/DocumentUploadStep.test.jsx`

Expected: FAIL on import.

- [ ] **Step 3: Implement drag/drop, pending list, and partial result rendering**

Client-side extension/size checks improve feedback but do not replace backend validation. Build one `FormData`, append each accepted file under `files`, and never set multipart `Content-Type` manually. Display pending, uploading, accepted, duplicate and rejected states. Continue button includes selected accepted document IDs in navigation state and also remains recoverable from the server document list after refresh.

- [ ] **Step 4: Run upload/page tests**

Run: `npm --prefix frontend test -- --run src/features/datasets/DocumentUploadStep.test.jsx src/pages/DatasetDocumentsPage.test.jsx`

Expected: PASS for drop, chooser, remove, partial failure, duplicate, refresh list and continue navigation.

- [ ] **Step 5: Commit document workflow**

```bash
git add frontend/src/api/documents.js frontend/src/features/datasets/DocumentUploadStep.jsx frontend/src/features/datasets/DocumentUploadStep.test.jsx frontend/src/pages/DatasetDocumentsPage.jsx frontend/src/pages/DatasetDocumentsPage.test.jsx frontend/src/App.jsx
git commit -m "feat: upload dataset documents in a separate step"
```

### Task 4: Indexing Settings and Real Preview

**Interfaces:**
- Produces: `getIndexingOptions()` and `previewSegments(datasetId, request)`。
- Produces validated UI request matching `IndexingPreviewRequest`.
- `ChunkPreview` renders a flat list or parent-child tree.

- [ ] **Step 1: Write failing mode and preview tests**

```javascript
// frontend/src/features/datasets/IndexingSettingsStep.test.jsx
it("shows models only for high quality and forces high quality for parent child", async () => {
  renderSettings({ options: indexingOptions() });
  expect(screen.getByLabelText("Embedding 模型")).toHaveValue("bge-m3");
  await user.click(screen.getByRole("button", { name: "经济" }));
  expect(screen.queryByLabelText("Embedding 模型")).not.toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "父子分段" }));
  expect(screen.getByRole("button", { name: "高质量" })).toHaveAttribute("aria-pressed", "true");
  expect(screen.getByRole("button", { name: "经济" })).toBeDisabled();
});


it("requests preview only when the preview button is clicked", async () => {
  renderSettings({ options: indexingOptions() });
  await user.clear(screen.getByLabelText("最大块长度（字符）"));
  await user.type(screen.getByLabelText("最大块长度（字符）"), "512");
  expect(api.previewSegments).not.toHaveBeenCalled();
  await user.click(screen.getByRole("button", { name: "预览块" }));
  expect(api.previewSegments).toHaveBeenCalledWith("dataset-1", expect.objectContaining({
    segmentation: expect.objectContaining({ max_chunk_length: 512 }),
  }));
});
```

- [ ] **Step 2: Run and verify settings component missing**

Run: `npm --prefix frontend test -- --run src/features/datasets/IndexingSettingsStep.test.jsx`

Expected: FAIL on import.

- [ ] **Step 3: Implement options-driven form and preview rendering**

Initialize from `/api/indexing/options`, not hard-coded model arrays. General request fields are separator, max chunk length and overlap. Parent-child fields are parent mode/length and child length/overlap. Retrieval fields include search method, Top K, score-threshold enable switch and threshold value. Validate overlap, Top K and threshold before request. After any config edit set `previewStale=true`; keep the old preview visible with “配置已变化，请重新预览”. Render source page, heading, sheet and row range from metadata, plus parser warnings.

- [ ] **Step 4: Run settings/preview tests**

Run: `npm --prefix frontend test -- --run src/features/datasets/IndexingSettingsStep.test.jsx src/features/datasets/ChunkPreview.test.jsx`

Expected: PASS for defaults, model selection, economy, parent-child tree, flat chunks, warnings, validation and stale preview.

- [ ] **Step 5: Commit settings and preview**

```bash
git add frontend/src/api/indexing.js frontend/src/features/datasets/IndexingSettingsStep.jsx frontend/src/features/datasets/IndexingSettingsStep.test.jsx frontend/src/features/datasets/ChunkPreview.jsx frontend/src/features/datasets/ChunkPreview.test.jsx
git commit -m "feat: configure and preview dataset chunks"
```

### Task 5: Create Job and Poll Persistent Progress

**Interfaces:**
- Produces: `createIndexingJob`, `getIndexingJob`, `retryIndexingJob`, `cancelIndexingJob`。
- Produces: `useIndexingJob(datasetId, jobId, {activeInterval, hiddenInterval})`。
- Terminal statuses: completed, partial_success, failed, cancelled.

- [ ] **Step 1: Write failing polling and progress tests**

```javascript
// frontend/src/hooks/useIndexingJob.test.js
it("polls running jobs and stops after a terminal response", async () => {
  vi.useFakeTimers();
  api.getIndexingJob
    .mockResolvedValueOnce({ status: "running", progress: 40 })
    .mockResolvedValueOnce({ status: "completed", progress: 100 });
  const { result } = renderHook(() => useIndexingJob("dataset-1", "job-1"));
  await waitFor(() => expect(result.current.job?.progress).toBe(40));
  await vi.advanceTimersByTimeAsync(2000);
  await waitFor(() => expect(result.current.job?.status).toBe("completed"));
  await vi.advanceTimersByTimeAsync(10000);
  expect(api.getIndexingJob).toHaveBeenCalledTimes(2);
});


// frontend/src/features/datasets/IndexingProgressStep.test.jsx
it("shows failed documents with retry and running task with cancel", async () => {
  renderProgress({ job: partialFailedJob() });
  expect(screen.getByText("manual.pdf")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "重试失败文件" })).toBeInTheDocument();
});
```

- [ ] **Step 2: Run and observe missing hook/components**

Run: `npm --prefix frontend test -- --run src/hooks/useIndexingJob.test.js src/features/datasets/IndexingProgressStep.test.jsx`

Expected: FAIL on imports.

- [ ] **Step 3: Implement save/process confirmation and robust polling**

On the first “保存并处理” request send `confirm_full_reindex=false`. If the backend returns `FULL_REINDEX_CONFIRMATION_REQUIRED`, display the returned full-document count and scope, then resend the unchanged request with `confirm_full_reindex=true` only after user confirmation. Navigate to the process page with `job_id` in URL search params so refresh can resume.

Polling starts at 2 seconds, uses a slower configured interval when `document.visibilityState === "hidden"`, aborts in-flight fetches on unmount, backs off on network error and never converts a network error into job `failed`. Render overall and per-document progress/stage, safe errors, warnings, cancel for non-terminal jobs and retry for failed documents.

- [ ] **Step 4: Run job workflow tests**

Run: `npm --prefix frontend test -- --run src/hooks/useIndexingJob.test.js src/features/datasets/IndexingProgressStep.test.jsx src/pages/DatasetProcessPage.test.jsx`

Expected: PASS for submit, expanded-scope confirmation, running/hidden/terminal polling, retry, cancel and refresh recovery.

- [ ] **Step 5: Commit task progress UI**

```bash
git add frontend/src/hooks/useIndexingJob.js frontend/src/hooks/useIndexingJob.test.js frontend/src/features/datasets/IndexingProgressStep.jsx frontend/src/features/datasets/IndexingProgressStep.test.jsx frontend/src/pages/DatasetProcessPage.jsx frontend/src/pages/DatasetProcessPage.test.jsx frontend/src/api/indexing.js frontend/src/App.jsx
git commit -m "feat: track persistent indexing progress"
```

### Task 6: Visual Integration, Accessibility, and Production Build

**Interfaces:**
- Produces responsive two-column settings/preview layout and usable mobile single column.

- [ ] **Step 1: Add accessibility and page-flow assertions**

```javascript
// frontend/src/pages/DatasetFlow.test.jsx
it("supports the full keyboard-visible flow without milvus controls", async () => {
  renderAt("/datasets");
  await user.click(screen.getByRole("button", { name: "创建知识库" }));
  expect(screen.getByRole("dialog", { name: "新建知识库" })).toBeInTheDocument();
  expect(document.body.textContent).not.toMatch(/Milvus|Collection|向量维度/);
  expect(screen.getByRole("button", { name: "创建" })).toHaveAccessibleName("创建");
});
```

- [ ] **Step 2: Run full frontend tests before final styles**

Run: `npm --prefix frontend test -- --run`

Expected: feature tests pass; the new page-flow assertion may fail on missing dialog/accessible labels.

- [ ] **Step 3: Finish shared components and CSS**

Create accessible modal focus handling, status badge and progress bar with `role="progressbar"`, error/warning notices and loading skeletons. Update `styles.css` for three-step header, upload dropzone, two-column settings/preview, parent tree, document progress rows and existing sidebar. At widths below 920px collapse settings/preview to one column; below 620px stack actions. Remove obsolete `.vector-store-summary`, `.connection-status`, Milvus form and old oversized modal styles when no longer referenced.

- [ ] **Step 4: Run tests and production build**

Run:

```bash
npm --prefix frontend test -- --run
npm --prefix frontend run build
```

Expected: all tests pass and Vite creates `frontend/dist` without compile warnings caused by missing imports.

- [ ] **Step 5: Commit visual integration**

```bash
git add frontend/src/components/Modal.jsx frontend/src/components/StatusBadge.jsx frontend/src/components/ProgressBar.jsx frontend/src/styles.css frontend/src/pages/DatasetFlow.test.jsx
git commit -m "feat: complete dataset indexing user experience"
```

`frontend/dist` is a verified build artifact but remains ignored; deployment builds it from source.

## Phase Verification

Run:

```bash
npm --prefix frontend test -- --run
npm --prefix frontend run build
rg -n "Milvus|collectionName|embeddingDimension|metricType|autoCreateCollection" frontend/src
git diff --check
```

Expected: tests/build pass and `rg` has no user-facing Milvus configuration state; any remaining “Milvus” text is limited to non-rendered developer/API naming only and must be reviewed manually.
