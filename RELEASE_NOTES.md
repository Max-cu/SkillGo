# SkillGo v0.4.1

本次发布的主线是**文档/图片理解的按需路由与扫描件原位处理**：新增 `inspect_document` 工具，在 MinerU 结构化版面解析与视觉模型之间自动路由，并让数字版 PDF 跳过无谓 OCR。

## 主要更新

**文档理解按需路由（inspect_document）**

- `structure` 意图调用 MinerU `/file_parse` 并请求 `content_list`，返回每个文本块的 `type`/`text`/`bbox [x0,y0,x1,y1]`/`page_idx`，支持 `pages` 页码范围，为扫描件 OCR、原位双语翻译与版面标注提供块级坐标。
- `understand` 意图把渲染后的 PNG/JPEG/WebP 页面交给视觉模型回答版面问题；`auto` 按文件类型自动选择（图片走视觉、PDF 走结构化解析）。
- 完整块列表落盘 `/workspace/work/document_inspection/<sha256>.json`，模型上下文只返回路径与前 20 个样例块，避免大结果挤占上下文。
- 解析结果按文件摘要/意图/页码范围缓存（上限 32 条），同一文件不重复 OCR；失败不写缓存。

**避免冗余 OCR**

- 系统提示明确：存在可用文本层的数字版 PDF 直接用 PyMuPDF 抽取文字与块坐标（`page.get_text`），不再调用 OCR；仅扫描件/图片版 PDF 或需要块级坐标时才使用 `inspect_document(structure)`。

**工具与可观测性**

- 工具注册校验：`intent` 限定 `structure`/`understand`/`auto`，`pages` 必须为 `[start, end]` 两个 0 起始且 start ≤ end 的整数。
- 执行过程新增"解析上传文档"行动事件，携带 `mode`、`page_count`、`block_count`、`block_types`、`cached` 与耗时数据。

## 升级

本版本**无数据库迁移**。MinerU 服务地址在平台模型连接中配置（接口类型 MinerU、能力 OCR），升级后在管理后台把 OCR 连接指向 MinerU 服务并使用"测试连接"验证（校验 `/openapi.json` 的 `/file_parse` 路径，不触发真实 OCR）。

Git 工作区部署：

```bash
sudo SKILLGO_INSTALL_ROOT=/opt/skillgo \
  SKILLGO_DEPLOY_ENV=deploy/ecs.env \
  bash deploy/backup-skillgo.sh

sudo SKILLGO_INSTALL_ROOT=/opt/skillgo \
  SKILLGO_DEPLOY_ENV=deploy/ecs.env \
  bash deploy/upgrade-skillgo.sh v0.4.1
```

源码压缩包部署按现有安装方式更新源文件后重建 api、worker、web 镜像。

## 验证与能力边界

- 后端 481 项回归全部通过，含 MinerU 结构化解析、页码范围表单、错误映射、意图路由、块落盘与缓存等新增测试。
- 结构化解析要求 OCR 连接使用 MinerU 文件解析协议（`api_format=mineru`）；未配置时返回可恢复的 `DOCUMENT_STRUCTURE_BACKEND_UNAVAILABLE`，不影响其余工具。
- 文档上传上限 30 MiB；支持 PDF、PNG、JPEG、WebP，其他类型在工具入口被拒绝。

完整变更见 [CHANGELOG.md](CHANGELOG.md)。
