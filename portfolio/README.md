# Axure 导出维护说明

V2.1 的原始 `.rp` 保存在本地项目的 Axure试做目录。本轮修复位于 HTML 导出层，原始工程仍使用旧交互。

重新导出时先输出到独立目录。保留 `portfolio/review-time.js` 和 `portfolio/review-time.css`，参照本仓库 P05 HTML 恢复两处资源引用、P07 表单、两处 `data-record-two-time` 和 `data-time-confirm-hook`，并同步 P05 的提示文案。补丁复用 P07 原确认控件的 Axure 动作来解锁 P05 保存并返回核对页。若导出后的元件或面板结构有变化，以元件标签核对确认动作与时间状态的挂载点，并检查 CSS 定位。

发布前从首页跑通录入，分别用有效钟点和「保留未知」走到保存回执。核对 P05 与 P08 的时间和值，检查错误输入、取消数值编辑、返回首页，并更新相关截图。
