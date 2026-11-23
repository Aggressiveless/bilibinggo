# Windows 打包说明

## 小白用户

请前往 GitHub **Releases** 下载：

- `Binggo-Setup-win64.exe` — 推荐，一键安装
- `Binggo-Portable-win64.zip` — 免安装解压即用

安装包**未做商业代码签名**；首次运行若被 SmartScreen 拦截，请选择「仍要运行」。

**覆盖安装前**请先完全退出 Binggo（任务管理器结束所有 `Binggo-RepostFix.exe`，含后台 `--serve` 进程），否则可能提示「拒绝访问」无法替换程序。安装程序会自动尝试结束相关进程。

## 开发者本地构建

需要本机已安装 **Python 3.12+** 与 **Node.js 20+**（`npm`）。安装包 / 便携版用户不需要 Node；Node 仅用于从源码构建前端。可选安装 [Inno Setup 6](https://jrsoftware.org/isinfo.php) 以生成 Setup。

```powershell
cd D:\path\to\bilibili_binggo
powershell -ExecutionPolicy Bypass -File packaging\windows\build.ps1
```

`build.ps1` 会：

1. 从 `src.app_paths.__version__` 读取版本（唯一权威版本号）
2. `python packaging/windows/generate_icon.py`（从 `packaging/assets/app-icon.png` 生成 `binggo.ico`；PNG 由 `scripts/render_brand_png.py` 从 `assets/brand/icon.svg` 导出）
3. `npm ci && npm run build` 构建前端
3. PyInstaller → 便携 ZIP
4. 若本机有 ISCC：以 `/DAppVersion=<version>` 注入 Inno，产出 Setup

跳过安装包：`$env:BINGGO_SKIP_INNO=1`

产物：

- `dist\Binggo-RepostFix\` — 程序目录（含 `Binggo-RepostFix.exe`）
- `dist\Binggo-Portable-win64.zip` — 便携压缩包
- `dist\Binggo-Setup-win64.exe` — 安装包（需 Inno Setup 6）

向导语言：若本机 Inno 含简体中文语言包则可选中文，否则为英文（不影响安装结果）。

## 升级

从 Releases 下载新版 Setup / Portable，自行覆盖安装或解压。概览页「检查更新」仅打开下载页，**不会**自动安装。

## GitHub 自动发布

推送 **Release**（`published`）时，`.github/workflows/release-windows.yml` 会自动构建并上传上述两个文件。

也可在 Actions 页手动 **Run workflow** 下载构建产物（不上传 Release）。

## 数据与端口

此 fork 使用 `binggo_repair_launcher.py`，固定将配置和历史保存在程序旁的 `user-data`，与原版数据目录分开。默认控制台地址为 `http://127.0.0.1:8182`；可用 `BINGGO_CONTROL_PORT` 指定其他端口。

升级前等待当前任务结束，退出旧程序并备份整个 `user-data`，再覆盖程序文件。发布包不得包含个人 Cookie、模型密钥、数据库或历史记录。

`BINGGO_SKIP_INNO=1` 可跳过本地安装包构建。安装器与便携 ZIP 均使用 `Binggo-RepostFix.exe`。
