"""
Common utilities shared by all template patches.
SPLIT_024 handles: inferring data-pptx-frame from geometry carrier
when template layouts omit it.
"""

import re
import copy
from xml.etree import ElementTree as ET

SVG_NS = 'http://www.w3.org/2000/svg'

# ── 「未带 frame 的语义形状」白名单 —— **单一真相源** ──
# 这些 shape 带 `data-pptx-semantic-object`，但既无 `data-pptx-frame`、也无 `data-pptx-native-ref`
# ⇒ 转换器会抛「Semantic shape text requires data-pptx-frame on its owner」，
#   由 patch_001_cover 的 strip_semantic_objects() 移除其 semantic-object（改走普通文本路径）兜底。
# 🔴 `check_templates.py` 读本常量做自检：**三层皆无**（frame / native-ref / 本名单）的语义形状 ⇒ 报错。
#    ⇒ 新风格若模板自带 frame（minimal / vibrant 即是），**无需**进本名单；
#      若新模板"三无"，Step 3 的模板校验会当场拦下，不再静默等到导出才炸。
# 🔑 2026-10-15 起为**空集**：default 门面页已按 minimal/vibrant 同格式统一 ——
#    每个**语义形状**都带内联 `data-pptx-frame` 且**不带** `data-pptx-native-ref`
#    （做法 = 把 native record 的**全部字段**内联 + 删 ref，等价于 hydrate 的产物；
#      001 那两处原本三无的形状由 geometry 的 `data-pptx-bounds` 补出 frame）。
#    ⇒ 不再存在「带 semantic-object 却取不到 frame」的形状
#    ⇒ patch_001_cover 的 strip_semantic_objects 收到空名单、自动空转（代码保留以备将来）
UNFRAMED_SEMANTIC_SHAPE_IDS: set = set()


def infer_frame_from_shape(shape: ET.Element) -> str | None:
    """从 shape 的 path/rect geometry carrier 推算 frame 字符串 'x y w h'"""
    # 1. 尝试 data-pptx-bounds
    bounds = shape.get('data-pptx-bounds')
    if bounds:
        return bounds
    # 2. 尝试从第一个 path 子元素的 d 属性提取边界框
    for child in shape:
        tag = child.tag.replace(f'{{{SVG_NS}}}', '')
        if tag == 'path':
            d = child.get('d', '')
            nums = [float(x) for x in re.findall(r'[\d.]+', d)]
            if nums:
                xs = nums[0::2]  # 偶数索引是 x
                ys = nums[1::2]  # 奇数索引是 y
                if xs and ys:
                    min_x, max_x = min(xs), max(xs)
                    min_y, max_y = min(ys), max(ys)
                    return f'{min_x:.2f} {min_y:.2f} {max_x - min_x:.2f} {max_y - min_y:.2f}'
        elif tag == 'rect':
            x = float(child.get('x', 0))
            y = float(child.get('y', 0))
            w = float(child.get('width', 0))
            h = float(child.get('height', 0))
            return f'{x:.2f} {y:.2f} {w:.2f} {h:.2f}'
    return None


def make_patched_semantic_shape_text_body():
    """
    Returns a replacement for _semantic_shape_text_body that:
    - Takes only the first <text> child (if multiple)
    - Infers data-pptx-frame from geometry when missing
    - Returns None (gracefully skip) on empty/unconvertible text
    """
    from svg_to_pptx.drawingml.converter import SvgNativeConversionError, convert_text

    def _patched(shape, ctx):
        texts = [c for c in shape if c.tag.replace(f'{{{SVG_NS}}}', '') == 'text']
        nested = [c for c in shape.iter() if c.tag.replace(f'{{{SVG_NS}}}', '') == 'text']
        if nested != texts:
            raise SvgNativeConversionError(
                'Semantic shape text must be one direct SVG text component'
            )
        if not texts:
            return None

        text = copy.deepcopy(texts[0])
        frame = shape.get('data-pptx-frame')
        if frame is None:
            frame = infer_frame_from_shape(shape)
            if frame is None:
                return None

        text.set('data-pptx-frame', frame)
        text.attrib.pop('data-pptx-shape-id', None)
        text.attrib.pop('data-pptx-shape-name', None)
        text.attrib.pop('data-pptx-shape-scope', None)

        result = convert_text(text, ctx)
        if result is None:
            return None

        m = re.search(r'(<p:txBody>.*?</p:txBody>)', result.xml, re.DOTALL)
        if m is None:
            return None
        return m.group(1)

    return _patched


def make_patched_build_custom_geom():
    """Returns a replacement for _build_preserved_custom_geom that gracefully
    returns None when custgeom/sha256 metadata is missing."""
    import svg_to_pptx.drawingml.elements as elements_mod
    _orig = elements_mod._build_preserved_custom_geom

    def _patched(elem):
        kind = elem.get('data-pptx-geometry-kind')
        if kind is None or kind != 'custom':
            return None
        encoded = elem.get('data-pptx-custgeom')
        expected_hash = elem.get('data-pptx-geometry-sha256')
        if not encoded or not expected_hash:
            return None
        return _orig(elem)

    return _patched


def make_patched_convert_semantic_shape(patched_text_body_fn):
    """
    返回一个替换版 _convert_semantic_shape，内部使用 patched_text_body_fn
    替代原本的 _semantic_shape_text_body（因为后者在模块内是局部引用，
    直接 monkey-patch 模块属性不生效）。
    """
    import copy
    from xml.etree import ElementTree as ET
    from svg_to_pptx.drawingml.converter import (
        SvgNativeConversionError,
        convert_element,
        SEMANTIC_OBJECT_ATTRIBUTE,
        SEMANTIC_SHAPE_KIND,
        _append_shape_text,
        _make_semantic_geometry_carrier_visible,
    )

    def _patched(shape, ctx):
        if shape.get(SEMANTIC_OBJECT_ATTRIBUTE) != SEMANTIC_SHAPE_KIND:
            return None

        carriers = [
            child
            for child in shape
            if child.get('data-pptx-part') == 'geometry'
            and child.tag.replace(f'{{{SVG_NS}}}', '')
            in {'circle', 'ellipse', 'line', 'path', 'polygon', 'polyline', 'rect'}
        ]
        if len(carriers) != 1:
            raise SvgNativeConversionError(
                'Semantic shape requires exactly one direct geometry carrier'
            )

        carrier = copy.deepcopy(carriers[0])
        _make_semantic_geometry_carrier_visible(carrier)

        # 从 shape 继承关键属性到 carrier
        for name, value in shape.attrib.items():
            if (
                name in {
                    'data-pptx-frame',
                    'data-pptx-geometry-kind',
                    'data-pptx-object',
                    'data-pptx-prst',
                }
                or name.startswith('data-pptx-av-')
            ) and carrier.get(name) is None:
                carrier.set(name, value)

        for name in (
            'data-pptx-shape-id',
            'data-pptx-shape-name',
            'data-pptx-shape-scope',
            'data-name',
        ):
            if carrier.get(name) is None and shape.get(name) is not None:
                carrier.set(name, str(shape.get(name)))

        if shape.get('data-pptx-geometry-kind') == 'custom':
            for name in (
                'data-pptx-custgeom',
                'data-pptx-geometry-kind',
                'data-pptx-geometry-sha256',
            ):
                carrier.attrib.pop(name, None)

        geometry = convert_element(carrier, ctx)
        if geometry is None:
            raise SvgNativeConversionError(
                'Semantic shape geometry carrier produced no native shape'
            )

        # ★ 使用传入的 patched text_body 函数（支持 frame 推断 & 多 text 容错）
        text_body = patched_text_body_fn(shape, ctx)
        return _append_shape_text(geometry, text_body) if text_body else geometry

    return _patched


SEMANTIC_OBJECT_ATTR = 'data-pptx-semantic-object'


def strip_semantic_object(content: str, shape_id: str) -> str:
    """移除指定 ``<g>`` 上的 ``data-pptx-semantic-object``（不依赖属性先后顺序）。

    兼容导出阶段可能出现的两种标签写法：``<g ...>`` 与 ``<ns0:g ...>``。

    🔴 为什么必须收成**一份**（2026-10-14 修）：
        原先五个 patch 各自内联了同一段正则，其中四个后来更新成兼容 ``ns0:`` 的写法，
        唯独 ``patch_002_toc`` 漏改、仍是 ``(<g[^>]*...)``。而 ``build_svgs.py`` 只做
        文本替换、**不做命名空间规范化** ⇒ ``default/002_toc.svg``（``ns0:`` 前缀）
        里这段正则匹配不到 ⇒ patch 静默空转 ⇒ 导出直接报
        ``Semantic shape requires at most one paragraph-based text component``。
        收成公共函数后，这类"复制多份、更新漏一份"的漂移不会再发生。
    """
    pattern = re.compile(
        rf'(<(?:ns0:)?g\b(?=[^>]*\bid="{re.escape(shape_id)}")[^>]*?)'
        rf'\s+{SEMANTIC_OBJECT_ATTR}="[^"]*"'
    )
    return pattern.sub(r'\1', content)


def strip_semantic_objects(content: str, shape_ids) -> str:
    """批量版 ``strip_semantic_object``：按给定 id 集合逐个剥壳。"""
    for shape_id in shape_ids:
        content = strip_semantic_object(content, shape_id)
    return content


# ── 语义形状的「条件式」查询（2026-10-15）──
# 🔴 为什么要有它：各 patch 原先用**硬编码 id 名单**决定剥哪些形状，而 id 是**跨风格撞号**的
#    （minimal / vibrant 的门面页由 default 派生，同名 id 不同结构）⇒ 名单会**误剥别的风格**的形状。
#    ⇒ 改成按「结构条件」筛：谁真有问题就剥谁，天然适配所有现有 / 未来风格。
#    （对比：default 002_toc 的 5 个、002_toc_3ch 的 4 个、003 的 1 个 —— 与旧 id 名单**完全一致**；
#      minimal / vibrant 命中 0 —— 恰好消掉了原先的误剥。）


def iter_semantic_shapes(content: str):
    """产出 ``(id, attrs, direct_text_count)`` —— 内容里的每个语义形状。

    ``direct_text_count`` 只看**直接子** ``<text>``（converter 的判据）。
    优先 ``xml.etree`` 精确解析（能正确处理嵌套 ``<g>``）；解析失败则退化为正则（兼容 ``ns0:`` 前缀）。
    """
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        root = None
    if root is not None:
        for el in root.iter():
            if el.get(SEMANTIC_OBJECT_ATTR) is None or not el.get('id'):
                continue
            n = sum(1 for c in el if c.tag.replace(f'{{{SVG_NS}}}', '') == 'text')
            yield el.get('id'), el.attrib, n
        return
    pat = re.compile(
        rf'<(?:ns0:)?g\b[^>]*{SEMANTIC_OBJECT_ATTR}="[^"]*"[^>]*>(.*?)</(?:ns0:)?g>', re.S)
    for m in pat.finditer(content):
        tag, block = m.group(0), m.group(1)
        sid = re.search(r'\bid="([^"]+)"', tag)
        if not sid:
            continue
        attrs = dict(re.findall(r'([\w:.\-]+)="([^"]*)"', tag))
        yield sid.group(1), attrs, len(re.findall(r'<(?:ns0:)?text\b', block))


def find_multi_text_shape_ids(content: str) -> list:
    """**直接子 ``<text>`` 数 > 1** 的语义形状 id。

    converter 只允许语义形状内一个文本主体，多余的会被丢弃
    ⇒ 报 ``Semantic shape requires at most one paragraph-based text component``。
    """
    return [sid for sid, _a, n in iter_semantic_shapes(content) if n > 1]


def find_frame_less_shape_ids(content: str) -> list:
    """**既无 ``data-pptx-frame``、又无 ``data-pptx-native-ref``** 的语义形状 id。

    这种形状取不到 frame（hydrate 无从回填、自身也没内联）
    ⇒ 报 ``Semantic shape text requires data-pptx-frame on its owner``。
    """
    return [sid for sid, a, _n in iter_semantic_shapes(content)
            if 'data-pptx-frame' not in a and 'data-pptx-native-ref' not in a]


def strip_flat_forbidden_attrs(content: str) -> str:
    """Remove all structured-mode-only attributes from SVG content."""
    FLAT_FORBIDDEN = [
        r'\s*data-pptx-layer="[^"]*"',
        r'\s*data-pptx-layout="[^"]*"',
        r'\s*data-pptx-layout-name="[^"]*"',
        r'\s*data-pptx-master="[^"]*"',
        r'\s*data-pptx-master-name="[^"]*"',
        r'\s*data-pptx-show-master-shapes="[^"]*"',
        r'\s*data-pptx-show-inherited-shapes="[^"]*"',
        r'\s*data-pptx-idx="[^"]*"',
        r'\s*data-pptx-placeholder="[^"]*"',
        r'\s*data-pptx-carrier="[^"]*"',
        r'\s*data-pptx-binding="[^"]*"',
        r'\s*data-pptx-geometry-kind="[^"]*"',
    ]
    for pat in FLAT_FORBIDDEN:
        content = re.sub(pat, '', content)
    content = re.sub(r'<rect[^>]*data-pptx-layer="(?:master|layout)"[^>]*/>', '', content)
    content = re.sub(r'\n\s*\n\s*\n', '\n\n', content)
    return content
