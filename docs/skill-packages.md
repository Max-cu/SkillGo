# Skill 包格式

普通技能包以一个 `SKILL.md` 为入口，可包含脚本、参考资料和素材；需要平台扩展配置时，可添加 `skillgo.yaml`。

```text
my-skill/
├── SKILL.md
├── scripts/          # 可选：执行脚本
├── references/       # 可选：参考文档
├── assets/           # 可选：模板与素材
├── requirements.txt  # 可选：Python 依赖
└── skillgo.yaml      # 可选：SkillGo 扩展声明
```

- 支持常见仓库下载包的外层包装目录，安全文件树中必须且只能有一个 `SKILL.md`。
- 名称、说明缺失时可从目录或正文推断；兼容 UTF-8 BOM，并对标识做规范化处理。
- 默认 ZIP 上限 **50 MiB**、解压后上限 **250 MiB**、文件数上限 **500**。素材密集型 Skill 可由管理员调整文件数配置，大小限制仍然生效。
- 路径穿越、符号链接、特殊文件、重复路径和异常压缩等内容会被拒绝。
- 可声明 `capabilities`、`python_dependencies`；依赖分析不等于任意安装命令的执行授权。

示例见 [`examples/`](../examples/)，格式与校验实现见 [`skill_package.py`](../backend/app/skill_package.py)。


[返回项目首页](../README.md)
