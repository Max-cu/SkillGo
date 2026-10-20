"""
P09 (009_content) — 创作内容页

**历史**：页头标题所在的 `<g id="s-009-shape-10">` 曾是"原生记录"（带 `data-pptx-native-ref`，
只有 `data-pptx-bounds`）⇒ 导出走原生还原、按公司原 PPT 的窄框（约 305.6px）换行，而非模板标称的 1128.99
（约 7 个汉字就折行）—— 见 `pitfalls.md` 对应条目。

⚠️ **现状（2026-10-15 复核）**：该形状**已经修好** —— 自带
`data-pptx-frame="52.87 14.53 1128.99 54.8"` + `data-pptx-prst="rect"`，且只有 **1 个** `<text>`
⇒ 既不缺 frame、也不违反"至多一个文本主体"。
⇒ **本 patch 的 strip 已非必需**（剥了也能导出，但会把它从"语义形状路径"改回"普通文本路径"，
   实测为 1px 级差异）。
🛑 **2026-10-15 决定：暂不摘。** 理由：① 它今天工作正常、无任何故障；② 摘掉会改到
**每份 deck 的全部内容页页头**（影响面远大于门面页 —— 此前同类改动虽是 1px 级，但那是"个别形状"，
这里是"每页都动"）；③ 收益仅是"少一条过时代码路径"。⇒ **保持现状**，将来有实测需求再摘。

页面对应：所有 slide_NN_body.svg（同一模板 009_content.svg 被多个内容页复用）
"""

try:
    from patches.common import strip_semantic_objects
except ImportError:  # 以裸模块名（非包）导入时的兜底
    from common import strip_semantic_objects

# 需要移除 data-pptx-semantic-object 的 shape ID
# 🔴 与 001 / 002 / 003 不同：这里仍是**硬编码 id**。09 的页头形状现自带 frame（见上），
#    本集合**已非必需**；若将来摘除，注意本 patch 会对**所有内容页**生效（影响面大于门面页）。
UNFRAMED_SHAPE_IDS = {
    's-009-shape-10',  # 页头 {{PAGE_TITLE}} 容器
}


def preprocess_content(content: str) -> str:
    """移除 009 页头语义形状的 semantic-object（历史上它缺 frame；现该形状已自带 frame，本步已非必需）"""
    return strip_semantic_objects(content, UNFRAMED_SHAPE_IDS)


def register_monkey_patches():
    """P09 不需要额外的 monkey-patch，文本走 convert_text 即可"""
    pass
