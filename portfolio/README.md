# Axure 导出维护说明

以本地 Axure RP 11 工程 `居家血糖记录与AI复诊速览_中保真_V2.2.rp` 为可编辑源文件。P07 的时间校验、保留未知和重复核对均通过 Axure 原生交互实现。P05 与 P08 使用当前核对结果，原始提取内容保留在核对界面供对照。

在 Axure 中通过「发布 → 生成 HTML 文件」导出到独立目录。用本地 HTTP 服务打开 `index.html`，从首页跑通录入，验证有效与非法时间、具体时间与未知互改、数值编辑与取消、P05 与 P08 一致和返回首页。

验收后，用新导出的 HTML 和 `data/`、`files/`、`images/`、`plugins/`、`resources/` 更新仓库中的原型资源，并更新受影响的截图。原生导出包含运行所需的交互，旧版 `portfolio/review-time.js` 和 `portfolio/review-time.css` 已移除。
