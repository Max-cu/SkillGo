"""
P02-3CH (002_toc_3ch) — 三栏目录页（3 章版）

与 002_toc（四栏）同源，只是去掉了第 4 个条目、前 3 栏按画布 3 等分重新分布。
问题与修正方式与 patch_002_toc 完全一致：

  1. TOC 条目的 semantic shape 内部有 2 个 <text>（标题 + 英文副标题）
  2. 标题 shape (s-002-shape-27) 内部有 3 个 <text>（"目录"/"content" 混合字号）
  converter 只允许 semantic shape 内 1 个 text 主体，多余的被丢弃。

修正：移除这些 shape 的 data-pptx-semantic-object，
  让它们走普通 convert_text 路径（每个 text 独立转换）。

与四栏版的差异：**不含 s-002-shape-66**（第 4 条已从模板中删除）。

页面对应：002_toc_3ch.svg（3 章 deck 的目录页）
"""

try:
    from patches.common import strip_semantic_objects, find_multi_text_shape_ids
except ImportError:  # 以裸模块名（非包）导入时的兜底
    from common import strip_semantic_objects, find_multi_text_shape_ids

# 要剥哪些 shape —— **按结构条件筛**，不再硬编码 id（理由见 common.py 的说明）：
#   · default 002_toc_3ch 命中 4 个：标题（3 个 text）+ 3 个 TOC 条目（各 2 个 text）
#   · minimal / vibrant 命中 0
# 🔄 2026-10-15：原为硬编码 id 名单（s-002-shape-27/39/48/57 —— 比四栏版少第 4 条）。
#    因 id **跨风格撞号**，旧名单会误剥 minimal 的对应形状，故改为条件式。


def preprocess_content(content: str) -> str:
    """剥掉「直接含 ≥2 个 <text>」的语义形状（converter 只允许语义形状内一个文本主体）"""
    return strip_semantic_objects(content, find_multi_text_shape_ids(content))


def register_monkey_patches():
    """P02-3CH 不需要额外的 monkey-patch，文本走 convert_text 即可"""
    pass
