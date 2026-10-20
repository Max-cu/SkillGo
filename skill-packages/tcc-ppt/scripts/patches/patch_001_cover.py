"""
P01 (001_cover) — 封面页

**历史**（2026-10-15 之前）：`s-001-shape-4`（英文副标题 {{TITLE_EN}}）与
`s-001-shape-23`（汇报时间 {{DATE}}）带 `data-pptx-semantic-object="shape"` 且内含 `<text>`，
但既无 `data-pptx-native-ref`（hydrate 无从回填）、自身也无 `data-pptx-frame`
⇒ converter 抛「Semantic shape text requires data-pptx-frame on its owner」。
当时的修正 = 剥掉这两个 shape 的 `data-pptx-semantic-object`，让文本走普通 `convert_text` 路径。

🔄 **2026-10-15 起**：default 门面页语义形状已**全部内联 `data-pptx-frame`**
（做法 = 内联 native record 的**全部字段** + 删 `data-pptx-native-ref`，与 minimal / vibrant 同格式）
⇒ 这两个 shape 现已自带 frame、**不再需要剥壳** ⇒ 白名单为空集
⇒ **本 patch 对全风格空转**（模块保留，仅作将来"三无"模板的兜底：
届时往 `common.py` 的 `UNFRAMED_SEMANTIC_SHAPE_IDS` 里加 id，或直接依赖下面的「条件筛」）。

页面对应：slide_01_cover.svg
"""

try:
    from patches.common import (
        strip_semantic_objects, find_frame_less_shape_ids, UNFRAMED_SEMANTIC_SHAPE_IDS,
    )
except ImportError:  # 以裸模块名（非包）导入时的兜底
    from common import (
        strip_semantic_objects, find_frame_less_shape_ids, UNFRAMED_SEMANTIC_SHAPE_IDS,
    )

# 剥壳对象 = **条件筛出的「取不到 frame」的形状** ∪ **手工白名单**
# （白名单定义在 common.py —— **单一真相源**，check_templates.py 也读它做「三层皆无」自检）。
# 当前两者都是空集 ⇒ 本 patch 空转。
UNFRAMED_SHAPE_IDS = UNFRAMED_SEMANTIC_SHAPE_IDS


def preprocess_content(content: str) -> str:
    """剥掉「既无 data-pptx-frame、又无 data-pptx-native-ref」的语义形状（取不到 frame ⇒ 必崩）"""
    ids = set(find_frame_less_shape_ids(content)) | set(UNFRAMED_SHAPE_IDS)
    return strip_semantic_objects(content, ids)


def register_monkey_patches():
    """P01 不需要额外的 monkey-patch，文本走 convert_text 即可"""
    pass
