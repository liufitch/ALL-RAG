# Dify 风格 Dataset 前端流程实施计划

> **面向智能体工作者：** 必需子技能：使用 `superpowers:subagent-driven-development`（推荐）或 `superpowers:executing-plans`，按任务逐项执行本计划。步骤使用复选框（`- [ ]`）语法进行跟踪。

**目标：** 将当前单弹窗页面改为“创建空知识库 → 上传文档 → 配置/真实预览 → 异步处理进度”的可刷新路由流程。

**架构：** React Router 按 dataset URL 恢复页面状态，API 模块集中处理错误和 JSON，feature 组件按三步职责拆分。任务轮询 hook 只读取 PostgreSQL 驱动的 API 状态，并在终态或卸载时停止。

**技术栈：** React 19、React Router、Vite 8、Vitest、jsdom、React Testing Library。

**规格：** `docs/superpowers/specs/2026-08-31-dify-style-dataset-indexing-design.md`

## 全局约束

- 创建知识库弹窗只显示名称、描述和权限。
- 页面任何位置不得出现可编辑 Milvus host、port、collection、dimension 或 metric。
- 高质量模式显示后端模型列表；经济模式隐藏模型。
- 父子分段必须自动切换并锁定高质量。
- 预览只在用户点击“预览块”时请求，编辑参数后标记需要刷新。
- 支持上传提示必须包含 TXT、MD、PDF、DOCX、XLS/XLSX、CSV。
- 任务运行时轮询，终态停止；后台标签页降低频率。
- API 错误优先显示稳定 `message`，不得把原始 HTML/stack 直接渲染。

---

## 文件结构

- 修改：`frontend/package.json` — Router 和测试依赖/脚本。
- 修改：`frontend/vite.config.js` — Vitest jsdom 配置。
- 创建：`frontend/src/test/setup.js` — jest-dom 和浏览器 mock。
- 创建: `frontend/src/api/client.js` — `requestJson`/`ApiError`。
- 创建: `frontend/src/api/datasets.js`。
- 创建: `frontend/src/api/documents.js`。
- 创建: `frontend/src/api/indexing.js`。
- 创建: `frontend/src/components/Modal.jsx`。
- 创建: `frontend/src/components/StatusBadge.jsx`。
- 创建: `frontend/src/components/ProgressBar.jsx`。
- 创建: `frontend/src/features/datasets/CreateDatasetModal.jsx`。
- 创建: `frontend/src/features/datasets/DocumentUploadStep.jsx`。
- 创建: `frontend/src/features/datasets/IndexingSettingsStep.jsx`。
- 创建: `frontend/src/features/datasets/ChunkPreview.jsx`。
- 创建: `frontend/src/features/datasets/IndexingProgressStep.jsx`。
- 创建: `frontend/src/hooks/useIndexingJob.js`。
- 创建: `frontend/src/pages/DatasetListPage.jsx`。
- 创建: `frontend/src/pages/DatasetDocumentsPage.jsx`。
- 创建: `frontend/src/pages/DatasetProcessPage.jsx`。
- 修改: `frontend/src/App.jsx` — 路由壳，不再承载业务表单细节。
- 修改: `frontend/src/main.jsx` — BrowserRouter。
- 修改: `frontend/src/styles.css` — 新页面布局和响应式样式。
- 测试：features/hooks/api 旁对应的 `*.test.jsx`/`*.test.js` 文件。

### 任务 1：测试框架、API 客户端与路由

**接口：**
- 提供：`requestJson(path, options) -> Promise<unknown>` 和 `ApiError`。
- 提供路由 `/datasets`、`/datasets/:datasetId/documents`、`/datasets/:datasetId/process`。

- [ ] **步骤 1：添加失败的 API 和路由测试**

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

- [ ] **步骤 2：安装依赖并验证测试失败**

添加脚本/依赖，然后运行：

```bash
npm --prefix frontend install
npm --prefix frontend test -- --run
```

预期：失败，因为 API 客户端/路由不存在。

- [ ] **步骤 3：实现最小客户端和路由壳**

Add:

```json
"scripts": { "test": "vitest", "test:run": "vitest run" }
```

并添加依赖 `react-router-dom`；开发依赖包括 `vitest`、`jsdom`、`@testing-library/react`、`@testing-library/jest-dom` 和 `@testing-library/user-event`。

实现 `requestJson`：添加 `VITE_API_BASE` 前缀，仅对非 FormData body 设置 JSON content type，解析结构化错误，并将 204 视为 `undefined`。`App` 只包含布局和 `<Routes>`；未知路由重定向到 `/datasets`。

- [ ] **步骤 4：运行前端基础测试**

运行: `npm --prefix frontend test -- --run src/api/client.test.js src/App.test.jsx`

预期：通过。

- [ ] **步骤 5：提交前端基础**

```bash
git add frontend/package.json frontend/package-lock.json frontend/vite.config.js frontend/src/test frontend/src/api/client.js frontend/src/api/client.test.js frontend/src/App.jsx frontend/src/App.test.jsx frontend/src/main.jsx
git commit -m "feat: add dataset frontend routing foundation"
```

### 任务 2：Dataset 列表与空 Dataset 弹窗

**接口：**
- 产出: `listDatasets(filters)`, `getDatasetStats()`, `createDataset(payload)`。
- `CreateDatasetModal.onCreated(dataset)` 会导航至 `/datasets/{id}/documents`。

- [ ] **步骤 1：编写失败的 UI 契约**

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

- [ ] **步骤 2：运行并验证功能缺失**

运行: `npm --prefix frontend test -- --run src/features/datasets/CreateDatasetModal.test.jsx`

预期：因组件缺失而失败。

- [ ] **步骤 3：提取列表页并实现最小弹窗**

将列表/统计/筛选/删除行为从 `App.jsx` 移到 `DatasetListPage`。提交内容必须严格为：

```javascript
{ name: form.name.trim(), description: form.description.trim(), permission: form.permission }
```

不要发送 category、owner、tags、retrieval config、embedding model 或 vector store。成功后关闭弹窗，并调用 `navigate(`/datasets/${dataset.id}/documents`)`，无需等待下一次列表刷新。

- [ ] **步骤 4：运行列表/弹窗测试**

运行: `npm --prefix frontend test -- --run src/features/datasets/CreateDatasetModal.test.jsx src/pages/DatasetListPage.test.jsx`

预期：通过；序列化 DOM 扫描不包含 Milvus 配置标签。

- [ ] **步骤 5：提交空 Dataset UI**

```bash
git add frontend/src/api/datasets.js frontend/src/components/Modal.jsx frontend/src/features/datasets/CreateDatasetModal.jsx frontend/src/features/datasets/CreateDatasetModal.test.jsx frontend/src/pages/DatasetListPage.jsx frontend/src/pages/DatasetListPage.test.jsx frontend/src/App.jsx
git commit -m "feat: create empty datasets from the frontend"
```

### 任务 3：文档上传步骤

**接口：**
- 产出: `uploadDocuments(datasetId, files)` and `listDocuments(datasetId, filters)`。
- `DocumentUploadStep.onContinue(documentIds)` 会导航至处理页面。

- [ ] **步骤 1：编写失败的上传交互测试**

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

- [ ] **步骤 2：运行并验证组件缺失**

运行: `npm --prefix frontend test -- --run src/features/datasets/DocumentUploadStep.test.jsx`

预期：导入时失败。

- [ ] **步骤 3：实现拖放、待处理列表和部分结果渲染**

客户端扩展名/大小检查用于改善反馈，但不能替代后端校验。构建一个 `FormData`，将每个接受的文件以 `files` 字段追加，绝不手动设置 multipart `Content-Type`。显示 pending、uploading、accepted、duplicate 和 rejected 状态。继续按钮将已接受文档 ID 放入导航状态，刷新后也能从服务器文档列表恢复。

- [ ] **步骤 4：运行上传/页面测试**

运行: `npm --prefix frontend test -- --run src/features/datasets/DocumentUploadStep.test.jsx src/pages/DatasetDocumentsPage.test.jsx`

预期：拖放、选择器、移除、部分失败、重复、刷新列表和继续导航测试均通过。

- [ ] **步骤 5：提交文档流程**

```bash
git add frontend/src/api/documents.js frontend/src/features/datasets/DocumentUploadStep.jsx frontend/src/features/datasets/DocumentUploadStep.test.jsx frontend/src/pages/DatasetDocumentsPage.jsx frontend/src/pages/DatasetDocumentsPage.test.jsx frontend/src/App.jsx
git commit -m "feat: upload dataset documents in a separate step"
```

### 任务 4：索引设置与真实预览

**接口：**
- 产出: `getIndexingOptions()` and `previewSegments(datasetId, request)`。
- 产出符合 `IndexingPreviewRequest` 的已校验 UI 请求。
- `ChunkPreview` 以平铺列表或父子树的形式渲染分段预览。

- [ ] **步骤 1：编写失败的模式和预览测试**

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

- [ ] **步骤 2：运行并验证设置组件缺失**

运行: `npm --prefix frontend test -- --run src/features/datasets/IndexingSettingsStep.test.jsx`

预期：导入时失败。

- [ ] **步骤 3：实现由选项驱动的表单和预览渲染**

从 `/api/indexing/options` 初始化，不使用硬编码模型数组。普通模式请求字段为 separator、max chunk length 和 overlap。父子模式字段为 parent mode/length 以及 child length/overlap。检索字段包括 search method、Top K、score-threshold 开关和阈值。请求前校验 overlap、Top K 和阈值。任何配置编辑后设置 `previewStale=true`；保留旧预览，并显示“配置已变化，请重新预览”。从 metadata 渲染源页、标题、工作表和行范围，并显示 Parser 警告。

- [ ] **步骤 4：运行设置/预览测试**

运行: `npm --prefix frontend test -- --run src/features/datasets/IndexingSettingsStep.test.jsx src/features/datasets/ChunkPreview.test.jsx`

预期：默认值、模型选择、经济模式、父子树、平面分段、警告、校验和过期预览测试均通过。

- [ ] **步骤 5：提交设置与预览**

```bash
git add frontend/src/api/indexing.js frontend/src/features/datasets/IndexingSettingsStep.jsx frontend/src/features/datasets/IndexingSettingsStep.test.jsx frontend/src/features/datasets/ChunkPreview.jsx frontend/src/features/datasets/ChunkPreview.test.jsx
git commit -m "feat: configure and preview dataset chunks"
```

### 任务 5：创建任务并轮询持久化进度

**接口：**
- 产出: `createIndexingJob`, `getIndexingJob`, `retryIndexingJob`, `cancelIndexingJob`。
- 产出: `useIndexingJob(datasetId, jobId, {activeInterval, hiddenInterval})`。
- 终态：`completed`、`partial_success`、`failed`、`cancelled`。

- [ ] **步骤 1：编写失败的轮询和进度测试**

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

- [ ] **步骤 2：运行并观察 hook/组件缺失**

运行: `npm --prefix frontend test -- --run src/hooks/useIndexingJob.test.js src/features/datasets/IndexingProgressStep.test.jsx`

预期：导入失败。

- [ ] **步骤 3：实现保存/处理确认和稳健轮询**

首次“保存并处理”请求发送 `confirm_full_reindex=false`。若后端返回 `FULL_REINDEX_CONFIRMATION_REQUIRED`，显示返回的全文档计数和范围，仅在用户确认后以 `confirm_full_reindex=true` 重发未改变的请求。跳转到处理页时将 `job_id` 放入 URL 查询参数，使刷新后能够恢复。

轮询从 2 秒开始；当 `document.visibilityState === "hidden"` 时使用配置的较慢间隔；卸载时中止进行中的 fetch；网络错误时退避，绝不将网络错误转换为任务 `failed`。渲染总体和逐文档进度/阶段、安全错误、警告；为非终态任务提供取消，为失败文档提供重试。

- [ ] **步骤 4：运行任务流程测试**

运行: `npm --prefix frontend test -- --run src/hooks/useIndexingJob.test.js src/features/datasets/IndexingProgressStep.test.jsx src/pages/DatasetProcessPage.test.jsx`

预期：提交、范围扩大确认、运行中/隐藏/终态轮询、重试、取消和刷新恢复测试均通过。

- [ ] **步骤 5：提交任务进度 UI**

```bash
git add frontend/src/hooks/useIndexingJob.js frontend/src/hooks/useIndexingJob.test.js frontend/src/features/datasets/IndexingProgressStep.jsx frontend/src/features/datasets/IndexingProgressStep.test.jsx frontend/src/pages/DatasetProcessPage.jsx frontend/src/pages/DatasetProcessPage.test.jsx frontend/src/api/indexing.js frontend/src/App.jsx
git commit -m "feat: track persistent indexing progress"
```

### 任务 6：视觉整合、无障碍与生产构建

**接口：**
- 提供响应式双列设置/预览布局，以及可用的移动端单列布局。

- [ ] **步骤 1：添加无障碍和页面流程断言**

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

- [ ] **步骤 2：在最终样式前运行完整前端测试**

运行: `npm --prefix frontend test -- --run`

预期：功能测试通过；新的页面流程断言可能因缺少对话框/无障碍标签而失败。

- [ ] **步骤 3：完成共享组件和 CSS**

创建可访问的弹窗焦点处理、状态徽章和带 `role="progressbar"` 的进度条、错误/警告提示以及加载骨架。更新 `styles.css`，支持三步页头、上传拖放区、双列设置/预览、父级树、文档进度行和现有侧栏。宽度低于 920px 时将设置/预览折叠为单列；低于 620px 时堆叠操作。无引用后移除过时的 `.vector-store-summary`、`.connection-status`、Milvus 表单和旧的大型弹窗样式。

- [ ] **步骤 4：运行测试并进行生产构建**

运行:

```bash
npm --prefix frontend test -- --run
npm --prefix frontend run build
```

预期：所有测试通过，Vite 创建 `frontend/dist`，且没有由缺少导入导致的编译警告。

- [ ] **步骤 5：提交视觉整合**

```bash
git add frontend/src/components/Modal.jsx frontend/src/components/StatusBadge.jsx frontend/src/components/ProgressBar.jsx frontend/src/styles.css frontend/src/pages/DatasetFlow.test.jsx
git commit -m "feat: complete dataset indexing user experience"
```

`frontend/dist` 是已验证的构建产物，但仍保持忽略；部署时从源代码构建。

## 阶段验证

运行:

```bash
npm --prefix frontend test -- --run
npm --prefix frontend run build
rg -n "Milvus|collectionName|embeddingDimension|metricType|autoCreateCollection" frontend/src
git diff --check
```

预期：测试/构建通过，且 `rg` 找不到面向用户的 Milvus 配置状态；剩余的 “Milvus” 文本只能是未渲染的开发者/API 命名，并且必须人工审查。
