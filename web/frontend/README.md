# Binggo 前端（Vite + TypeScript）

源码根目录。设计与 DOM 结构冻结，见 `docs/plans/05-frontend-engineering-impl.md`。

## 开发

```bash
# 终端 A：后端 API（默认 http://127.0.0.1:8787）
python scripts/run_dashboard.py

# 终端 B：前端 HMR
cd web/frontend
npm ci
npm run dev
```

浏览器打开 Vite 地址（默认 `http://127.0.0.1:5173`）。`/api` 经 proxy 转发；可用环境变量 `BINGGO_API_PROXY` 覆盖目标。

## 生产构建

```bash
cd web/frontend
npm ci
npm run build
```

产物输出到 `web/static/dist/`（不提交 git）。FastAPI 只托管该目录。

## 测试

```bash
# 单元测试（vitest）
npm test

# 生产构建（E2E 依赖 dist）
npm run build

# 端到端冒烟（隔离 HOME，端口 8791；首次需装 Chromium）
npx playwright install chromium
npm run test:e2e
```

Windows 已安装 Edge 时，可直接使用它运行测试，无需下载 Chromium：

```powershell
$env:BINGGO_E2E_BROWSER_CHANNEL = "msedge"
$env:BINGGO_OFFLINE = "1"
npm.cmd run test:e2e
```

队列验收拦截启动与变更请求，模拟忙碌时提交、上下移动、移除和读取历史日志；不会执行真实参与。检索验收覆盖开始、停止、重载和继续检索。

E2E 不依赖本机已在跑的 `8787` 控制台；真 Cookie 手测仍用 `python scripts/run_dashboard.py`。
