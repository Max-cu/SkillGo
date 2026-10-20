# TCC PPT skill

本目录就是技能包本身（脚本、参考、模板、图标）。照片落页记在工作区状态文件 `/workspace/_state/photo_plan.json`，由 `scripts/write_photo_plan.py` 写入，并在 `SKILL.md` 的 `metadata.state_files` 中声明。

打包：

```sh
python skill-packages/tcc-ppt/build_package.py /tmp/tcc-ppt.zip
```

照片安排与页级检查：

```sh
python -m pytest skill-packages/tcc-ppt/tests/test_photo_plan.py -q
```
