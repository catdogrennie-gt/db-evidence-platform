# 冻结观测边界实施计划

依据[设计](../specs/2026-11-13-report-observation-bounds-design.md)，按现有持续演进授权执行一个有界改进。使用writing-plans组织工作，不增加平台AI执行权限。

1. 新增 `tests/platform/test_report_observation_bounds.py`，验证真实冻结清单、权限、来源隔离及损坏/窗口边界，先记录失败。
2. 新增 `platform_app/report_observation_bounds.py`，只读重核清单中的时点样本；在 `investigations.investigation_detail` 与 `intelligence.issue_detail` 返回单独投影，不改保存的report。
3. `ExecutiveSummary` 接收投影，问题页与历史页均传入；新增前端回归，显示历史时间边界与AI状态来源，原始判断不改写。
4. 运行相关新鲜检查、独立审查，核对实际失败请求与程序投影。运行服务保持封存16，达到封存/发布门槛才切换；不为本项重开故障或调用模型。
5. 更新进度、手册、场景与CHECKPOINT，分开记录代码、部署、展示和语义诊断状态。

重点反例：同主机不同item不能合并；缺少后续样本不能推出恢复；窗口外新样本不能进入历史投影；只读访问不扩大敏感权限；元数据与引用有效不等于自由文本语义已验。
