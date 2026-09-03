# SkillGo v0.2.4

这是一次附件智能能力更新：SkillGo 现在可以接入私有视觉模型和 OCR 模型，平台会在对话与 Skill 任务开始前理解用户上传的图片。

## 主要更新

- 平台设置支持为每个模型标注对话、视觉和 OCR 能力，并分别选择三类默认模型。
- PNG、JPG/JPEG、WebP 图片默认由视觉模型分析，普通工作台对话和 Skill 任务使用同一条附件理解链路。
- “OCR 识别”默认关闭；开启后先提取图片文字，再将 OCR 结果和原图一起交给视觉模型，最后仍由对话模型回答。
- OCR 失败时继续视觉理解并记录警告；没有可靠分析结果时请求硬失败，不会伪造理解结果。
- 附件记录保存分析模式、状态和实际使用的模型，Skill 沙箱只能把这些结果作为不可信证据使用。
- 新增数据库迁移，已有模型连接自动保持对话能力，升级不清空业务数据。

## 升级

Git 工作区部署建议先执行备份，再升级到 `v0.2.4`：

```bash
sudo SKILLGO_INSTALL_ROOT=/opt/skillgo \
  SKILLGO_DEPLOY_ENV=deploy/ecs.env \
  bash deploy/backup-skillgo.sh

sudo SKILLGO_INSTALL_ROOT=/opt/skillgo \
  SKILLGO_DEPLOY_ENV=deploy/ecs.env \
  bash deploy/upgrade-skillgo.sh v0.2.4
```

完整的首次部署、升级、验证和回滚说明见 [`deploy/README.md`](deploy/README.md)。

## 部署提示

视觉和 OCR 模型当前需兼容 OpenAI 多模态 Chat Completions 消息格式。图片、OCR 文字和视觉结果均按不可信数据处理。公网正式环境仍应配置 TLS 并妥善托管密钥；Skill 联网授权继续遵守版本级审核与默认断网策略。

## 验证

- Backend: `161 passed`
- Frontend: TypeScript + Vite production build passed
- Database: 空库、旧版本升级和附件智能迁移验证通过

完整变更见 [`CHANGELOG.md`](CHANGELOG.md)。

