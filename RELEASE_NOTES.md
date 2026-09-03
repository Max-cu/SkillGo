# SkillGo v0.2.5

这是一次 OCR 接入兼容性与工作台体验更新：SkillGo 现在可以直接接入 MinerU 文件解析服务，任务入口的 OCR 选项也改成了清晰的滑动开关。

## 主要更新

- 平台设置新增“OpenAI 兼容接口”和“MinerU 文件解析”两种接口类型。
- MinerU 地址可填写服务根地址或完整 `/file_parse` 地址；平台会用 multipart 上传图片，并读取 `results.*.md_content`。
- MinerU 固定只提供 OCR 能力，不会被误选为对话或视觉模型。
- 连接测试通过服务 OpenAPI 快速确认 `/file_parse` 协议，不再执行一次完整 OCR。
- “开始任务”和 Skill 对话页的“OCR 识别”均改为左右滑动开关，开启、关闭和禁用状态更清楚。
- 已有模型连接升级后继续按 OpenAI 兼容协议工作，不改变现有默认模型。

## 升级

Git 工作区部署建议先执行备份，再升级到 `v0.2.5`：

```bash
sudo SKILLGO_INSTALL_ROOT=/opt/skillgo \
  SKILLGO_DEPLOY_ENV=deploy/ecs.env \
  bash deploy/backup-skillgo.sh

sudo SKILLGO_INSTALL_ROOT=/opt/skillgo \
  SKILLGO_DEPLOY_ENV=deploy/ecs.env \
  bash deploy/upgrade-skillgo.sh v0.2.5
```

完整的首次部署、升级、验证和回滚说明见 [`deploy/README.md`](deploy/README.md)。

## 部署提示

视觉模型继续使用 OpenAI-compatible 多模态 Chat Completions；OCR 可选择相同协议或 MinerU `/file_parse`。图片、OCR 文字和视觉结果均按不可信数据处理。公网正式环境仍应配置 TLS 并妥善托管密钥；Skill 联网授权继续遵守版本级审核与默认断网策略。

## 验证

- Backend: `165 passed`
- Frontend: TypeScript + Vite production build passed
- Database: 空库、旧版本升级和模型接口类型迁移验证通过

完整变更见 [`CHANGELOG.md`](CHANGELOG.md)。

