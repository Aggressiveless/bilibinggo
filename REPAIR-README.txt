Binggo 动态转发接口本地修复版
版本：5.0.5+repostfix.1（基于官方 v5.0.5，非官方发布）

Windows 使用步骤
1. 将 Windows ZIP 完整解压到一个新文件夹，例如 D:\A_Bonus\Binggo-RepostFix。不要覆盖原 Binggo 安装目录，也不要只复制 EXE。
2. 双击 Binggo-RepostFix.exe。浏览器应打开 http://127.0.0.1:8182。
3. 在概览确认版本为 5.0.5+repostfix.1。原版使用 8181，两个页面请区分。
4. 此版使用程序旁的 user-data 文件夹，首次是空白数据，需要重新扫码登录。没有复制原版 Cookie、API Key 或活动库。
5. 暂时不要启动定时调度或批量参与。选择一条你确实要参与的活动，点击一次参与。
   参与按钮仍会执行该活动的关注、点赞、转发、评论等原有动作。
6. 到 B 站个人动态确认实际转发已出现，并核对原活动的文案、评论等要求。
   接口返回成功不等于已满足活动全部条件，也不代表必定中奖。
7. 若提示“转发结果未确认”，先检查个人动态，不要立刻重试。

数据与退出
日志：user-data\data\logs\binggo.log。
登录凭证：user-data\config\cookies.txt，仅保留本机，不要分享。
本程序有后台服务。需要彻底退出时，在任务管理器中仅结束 Binggo-RepostFix.exe。
原版安装和 %APPDATA%\Binggo 未被本修复版覆盖。

修复内容
- 从旧 dynamic_repost 接口迁移到 https://api.bilibili.com/x/dynamic/feed/create/dyn。
- 使用 JSON dyn_req、web_repost_src，动态 ID 保持字符串；CSRF 放查询参数。
- 沿用普通文本转发，不新增富文本 @ 用户解析能力。
- 检查业务 code、data.result 或返回的新动态 ID；不再把含“已”“重复”的任意错误判成成功。
- 转发提交 retries=0；网络超时、异常 HTTP、非 JSON 或含糊结果提示先人工核对，不自动重复提交。
- 独立的启动器、8182 端口、单实例标识与 user-data 目录。

验证结果（2026-11-21，日本时间）
- 后端离线测试：427 passed，2 skipped；测试过程中阻断外网连接。
- 前端测试：4 passed；类型检查与生产构建通过。
- 打包后的 Windows EXE：/api/runtime 与首页 HTTP 200；确认版本、独立目录及 8182 端口。
- 尚未使用真实账号发送动态；实际账号兼容性需按上述单条验证步骤确认。
- 未修复更新数据源时具体 /opus/ 页面返回 404 的另一问题。

源码与重建
源码包来自 https://github.com/luovicter-collab/bilibinggo 的 v5.0.5，加本地修复。
修改差异见随附 repost-fix.patch。原始源码下载经 Git blob SHA-1 校验。
构建使用 Windows x64、Python 3.11.13；依赖版本见 requirements-repair-lock.txt。
在源码根目录打开 PowerShell，用 Windows Python 3.11 执行：
  py -3.11 -m venv .repair-venv
  .\.repair-venv\Scripts\python.exe -m pip install -r requirements-repair-lock.txt
  Push-Location web\frontend
  npm.cmd ci
  npm.cmd run build
  Pop-Location
  .\.repair-venv\Scripts\python.exe -m pytest tests/ -q
  .\.repair-venv\Scripts\python.exe -m PyInstaller packaging/windows/binggo.spec --noconfirm
生成目录：dist\Binggo-RepostFix。请使用本说明的命令；原仓库 build.ps1 面向官方安装包命名。

参考
官方源码：https://github.com/luovicter-collab/bilibinggo/tree/v5.0.5
相同报错：https://github.com/luovicter-collab/bilibinggo/issues/18
新接口参考：https://github.com/shanmiteko/LotteryAutoScript/blob/accfb8ba5502688b96fcac6f47d58042b2fcadba/lib/net/bili.js
