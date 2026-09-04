# SkillGo v0.2.6

这是一次 PDF OCR 闭环与附件上传体验更新：扫描型 PDF 现在可以由 MinerU 真实识别，工作台会连续展示上传、解析和完成状态。

## 主要更新

- 开启 OCR 后，PDF 以原生 `application/pdf` multipart 文件交给 MinerU `/file_parse`，识别结果进入对话或 Skill 任务上下文。
- PDF 只调用 OCR 模型；图片继续执行视觉理解，并在开启开关后叠加 OCR，避免能力和输入格式错配。
- 工作台显示真实附件上传百分比，上传结束后继续显示“PDF OCR 识别中”“OCR 与视觉分析中”等处理阶段。
- 已处理附件显示“OCR 已识别”“视觉 + OCR 已完成”等状态；部分能力失败时也会如实提示。
- 扫描型 PDF 未开启 OCR 时返回明确的开启提示，伪造或损坏的 PDF 会被文件签名校验拒绝。

## 升级

Git 工作区部署建议先执行备份，再升级到 `v0.2.6`：

```bash
sudo SKILLGO_INSTALL_ROOT=/opt/skillgo \
  SKILLGO_DEPLOY_ENV=deploy/ecs.env \
  bash deploy/backup-skillgo.sh

sudo SKILLGO_INSTALL_ROOT=/opt/skillgo \
  SKILLGO_DEPLOY_ENV=deploy/ecs.env \
  bash deploy/upgrade-skillgo.sh v0.2.6
```

完整的首次部署、升级、验证和回滚说明见 [`deploy/README.md`](deploy/README.md)。

## 部署提示

视觉模型继续使用 OpenAI-compatible 多模态 Chat Completions；PDF OCR 需要 MinerU 文件解析接口。附件、OCR 文字和视觉结果均按不可信数据处理。公网正式环境仍应配置 TLS 并妥善托管密钥；Skill 联网授权继续遵守版本级审核与默认断网策略。

## 验证

- Backend: `169 passed`
- Frontend: TypeScript + Vite production build passed
- MinerU: 使用临时生成的 PDF 实测 `/file_parse` 返回 HTTP 200，并正确识别测试文字

完整变更见 [`CHANGELOG.md`](CHANGELOG.md)。

