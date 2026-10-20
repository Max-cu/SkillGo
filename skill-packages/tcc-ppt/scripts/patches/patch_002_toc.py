"""
P02 (002_toc) — 目录页
问题：
  1. TOC 4个条目的 semantic shape 内部有 2个 <text>（标题+副标题）
  2. 标题 shape (s-002-shape-27) 内部有 3个 <text>（"目录"/"content" 混合字号）
  converter 只允许 semantic shape 内 1 个 text 主体，多余的被丢弃。
  修正：移除这些 shape 的 data-pptx-semantic-object，
  让它们走普通 convert_text 路径（每个 text 独立转换）。

🔴 2026-10-14 修复历史遗留 bug：
   本文件原先**内联**了一段 `<g[^>]*\bid="..."` 的正则，不兼容 `<ns0:g>` 前缀；
   而 002_toc.svg 恰是 `ns0:` 前缀（build_svgs 不做命名空间规范化）⇒ 该 patch
   一直**静默空转** ⇒ default 风格下 4 章目录页导出直接报
   "Semantic shape requires at most one paragraph-based text component"。
   现改为调用 patches/common.py 的公共实现（与 patch_002_toc_3ch 等同构，兼容两种前缀）。

页面对应：P02_toc.svg
"""

try:
    from patches.common import strip_semantic_objects, find_multi_text_shape_ids
except ImportError:  # 以裸模块名（非包）导入时的兜底
    from common import strip_semantic_objects, find_multi_text_shape_ids

# 要剥哪些 shape —— **按结构条件筛**，不再硬编码 id（理由见 common.py 的说明）：
#   · default 002_toc 命中 5 个：标题（3 个 text）+ 4 个 TOC 条目（各 2 个 text）
#   · minimal / vibrant 命中 0（它们的语义形状各含 1 个 text，天然合规）
# 🔄 2026-10-15：原为硬编码 id 名单（s-002-shape-27/39/48/57/66）。因 id **跨风格撞号**
#    （minimal 的模板由 default 派生、同名 id 结构不同）⇒ 旧名单会**误剥 minimal** 的对应形状。


def preprocess_content(content: str) -> str:
    """剥掉「直接含 ≥2 个 <text>」的语义形状（converter 只允许语义形状内一个文本主体）"""
    return strip_semantic_objects(content, find_multi_text_shape_ids(content))


def register_monkey_patches():
    """P02 不需要额外的 monkey-patch，文本走 convert_text 即可"""
    pass
