# README 结构与素材约定

目标是让读者快速看懂产品用途、使用方式、实际案例及未验证范围。README 展示产品入口，部署、接口和完整验收留在对应文档。当前主图均来自实际页面；模型锁案例已作指定核对，缺少曲线等已知问题如实保留。

## 主图和亮点直接展示

本次明确要求：**主图及核心功能图不放在折叠块里。** 在项目标题、一句定位和导航之后直接展示主工作台；随后按少量核心能力展示真实图，例如问题与风险、带证据关系的报告、冻结曲线与当前指标下钻。

每张图要让读者理解一件事，配一句用途与数据身份说明。二级内容可以使用 `details`，例如长参数、完整检查明细、历史版本说明；最短使用入口、重要能力边界和核心图保持可见。

## 推荐阅读顺序

1. **项目名与一句定位**：说明既有监控采集、平台巡检分析、人工处置之间的分工。
2. **直接可见的主图**：来自实际运行产品，不用一整屏的设计原型代替。
3. **核心亮点与对应图**：按“问题结果 → 报告 → 证据 / 曲线 → 处置复验”串起工作流。
4. **最短启动与走读**：给出先决条件、运行入口、首次配置和一个短案例，详细参数链接到操作 / 部署文档。
5. **架构与数据路径**：说明原有控制库、Zabbix 历史、采集 / AI worker 及冻结报告；设计预留与已接通组件分开。
6. **真实案例与验证范围**：列实际观察、负向对照、结果及有限边界，链接详细证据。
7. **文档、当前限制与历史版本**：以简洁导航结束，长表可移至二级文档。

不必为了套模板加入不存在的贡献生态、生产用户背书或下载渠道。实现状态改变时，先同步功能、图与验证范围，再调整宣传性短句。

## 图片与证据身份

| 类型 | 应如何呈现 |
| --- | --- |
| 真实产品截图 | 标明版本或日期、隔离 / 生产等实际环境身份；图中计数按当时账号范围解释 |
| 实际数值离线图 | 明确是冻结数值生成的图，保留单位、窗口和缺口，不称为产品截图 |
| 设计图 / 原型 | 明确设计或模拟身份；展示规划和交互，不证明后端接通 |
| 模型结果 | 与规则摘要分开标识，成功不等于每句结论已正确；保留未知和反证 |
| 历史截图 | 归入对应历史快照，不能拿来证明新界面已经运行 |

图片放在仓库内，使用相对路径和描述性的 alt 文本。新截图优先从实际页面采集清楚的视口，避免把很长的全页图压到文字不可读。任何注释、裁剪或标注都不能改写观察、状态和数值。

不在图片、文件名或说明中包含 Token、密码、个人资料、私密 URL 参数或未经审查的部署资料。公开前由协调者检查完整截图及来源，而不是只检查可见缩略图。不要把别人的截图、商标、客户评价或图表挪作自己的成果。

## 当前素材清单

| 材料 | 当前身份与使用位置 |
| --- | --- |
| `docs/platform/screenshots/workbench.png` | 旧版真实工作台，留作历史实现说明；不能标为新版首页 |
| `docs/platform/screenshots/ai-cited-report.png` | 旧版真实模型报告局部，适合历史案例引用 |
| `docs/platform/architecture.svg` | 旧版设计基线，图中可选组件不能自动标为当前集成 |
| `docs/platform/index.html` 等设计文件 | 设计原型，与实测截图分开 |
| `docs/images/intelligence-overview.jpg` | 实际问题首页，三个授权对象；自动模型策略暂停 |
| `docs/images/intelligence-report.jpg` | 实际锁报告及证据关系；预算导致无冻结曲线的缺口保留显示 |
| `docs/images/intelligence-metrics.jpg` | 实际 Zabbix 指标看板；与报告的冻结依据分开 |

新增素材时记录：仓库相对路径、实际采集日期、版本 / 提交、数据来源、窗口、账号范围含义、公开检查结果。私密证明保留在运行目录；公开图注只保留读者理解图片所需的信息。

## 状态、链接与命令

- 版本、测试和 CI 徽章必须有对应记录。没有 CI 时，不添加伪装成持续自动通过的徽章。
- 旧版测试数量属于旧版快照；新改造要用新鲜输出，跳过、失败与待验分开统计。
- 缺少正式许可声明时，不照搬别人的 MIT / Apache / AGPL 徽章。
- 相对链接必须存在，锚点须对应标题；Release / 下载地址实际存在并回读后才写成已发布。
- 启动命令在注明的平台和版本核对，凭据使用部署环境的私密配置，不写入示例值。
- 短测、合成目标和单次模型成功不包装成生产采用、业务收益或通用准确率。

## 实际参考与采用理由

以下链接已于 2026-11-08 浏览核对，参考的是结构与表达方法，没有复制其他项目的文字或图片。

| 来源 | 本项目采用的原则 |
| --- | --- |
| [GitHub：About READMEs](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/about-readmes) | 先回答用途、价值及如何开始，使用仓库内相对文档 / 图片链接 |
| [GitHub：Collapsed sections](https://docs.github.com/en/get-started/writing-on-github/working-with-advanced-formatting/organizing-information-with-collapsed-sections) | 把并非每位读者都需要的细节折叠；核心图直显是本次产品展示要求 |
| [Bytebase README](https://github.com/bytebase/bytebase/blob/main/README.md) | 顶部导航和图形之后解释产品定位，再展开功能与使用方式 |
| [DBeaver README](https://github.com/dbeaver/dbeaver/blob/devel/README.md) | 核心产品图直接显示，下载 / 运行 / 文档 / 架构各有入口 |
| [Grafana README](https://github.com/grafana/grafana/blob/main/README.md) | 简短能力说明与开始使用入口，深度文档链接出去 |
| [FastAPI README](https://github.com/fastapi/fastapi/blob/master/README.md) | 安装、创建、运行和检查结果连贯，替代用法放入折叠区 |
| [Standard Readme](https://github.com/RichardLitt/standard-readme/blob/main/spec.md) | 短描述、安装、使用与有效链接；其规范主要面向库，按产品需要取用 |
| [Make a README](https://www.makeareadme.com/) | 用截图帮助理解，安装步骤具体，不要求读者猜测 |

新版内容入口见[操作指南](user-guide.md)，历史版本见[版本索引](releases/README.md)。
