Binggo 6.0.3（Komerser fork）

下载：https://github.com/Komerser/bilibinggo/releases/tag/v6.0.3
源码：https://github.com/Komerser/bilibinggo
上游：https://github.com/luovicter-collab/bilibinggo

首次使用
1. 下载 Binggo-Portable-win64.zip 并完整解压。
2. 双击 Binggo-RepostFix.exe，浏览器打开 http://127.0.0.1:8182 。
3. 按页面提示登录并配置模型；用户配置与历史保存在程序旁的 user-data 文件夹。

升级
1. 等待已有任务完成后退出旧程序。
2. 备份原程序及整个 user-data 文件夹。
3. 在原目录覆盖程序文件并保留 user-data；启动后即可继续使用原配置与历史。
4. 请勿将含 Cookie、模型密钥及数据库的 user-data 上传公开仓库。

主要更新
- 活动页支持导出当前筛选中的开放活动，并预览导入文件中的重复项与过期项。
- 数据源更新和主动检索分批保存，可在日志中继续失败或中断的任务。
- 动态转发接口修复与按活动要求收藏。
- 抽奖线索检索、候选复核、增量导入，以及唯一新视频目标数量。
- 主动检索默认组合搜索，调用模型前筛除缺少活动证据的候选。
- 自动顺序参与、统一任务队列、日志及概览用量统计。
- MCP v2 控制与自动化契约。

完整说明见 CHANGELOG.md 和 GitHub Release。发布包未做商业代码签名。
