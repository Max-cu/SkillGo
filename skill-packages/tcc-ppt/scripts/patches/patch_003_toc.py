"""
P03 (003_toc) — 章节页
问题：
  s-003-shape-23（章节说明两行 {{CHAPTER_DESC_LINE1}} + {{CHAPTER_DESC_LINE2}}）
  是 semantic shape，内部却有 2 个 <text>。
  → converter 只允许语义形状内 1 个 text 主体，抛出：
    Semantic shape requires at most one paragraph-based text component

修正：与 patch_002_toc 同构 —— 移除该 shape 的 data-pptx-semantic-object，
  两行文字各自走普通 convert_text 路径，两行都不会丢。

页面对应：slide_03_ch1.svg / slide_06_ch2.svg / slide_09_ch3.svg / slide_12_ch4.svg
（同一模板 003_toc.svg 被 4 个章节页复用）
"""

try:
    from patches.common import strip_semantic_objects, find_multi_text_shape_ids
except ImportError:  # 以裸模块名（非包）导入时的兜底
    from common import strip_semantic_objects, find_multi_text_shape_ids

# 要剥哪些 shape —— **按结构条件筛**，不再硬编码 id（理由见 common.py 的说明）：
#   · default 003_toc 命中 1 个：章节说明（LINE1 / LINE2 两行 = 2 个 text）
#   · minimal / vibrant 命中 0（它们的章节说明只占 1 个 text）
# 🔄 2026-10-15：原为硬编码 id 名单（s-003-shape-23）。因 id **跨风格撞号**，
#    旧名单在 minimal 上会**误剥**其同号形状（minimal 的该形状本是 1 个 text、合规）。
#    ⇒ 改为条件式后，vibrant 的 `_note`（"新增元素须避开 s-003-shape-23"）也随之失效 —— 不必再避。


def preprocess_content(content: str) -> str:
    """剥掉「直接含 ≥2 个 <text>」的语义形状（converter 只允许语义形状内一个文本主体）"""
    return strip_semantic_objects(content, find_multi_text_shape_ids(content))


def register_monkey_patches():
    """P03 不需要额外的 monkey-patch，文本走 convert_text 即可"""
    pass
