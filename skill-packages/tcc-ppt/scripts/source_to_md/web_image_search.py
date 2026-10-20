#!/usr/bin/env python3
"""百度图片搜索与下载。

🔴 关键：**必须先访问 image.baidu.com 首页建立 Cookie 会话**，
   否则接口会直接拒绝：
       {"antiFlag":1,"message":"Forbid spider access"}

链路：
    ① 访问 https://image.baidu.com/          拿 Cookie（BAIDUID 等）
    ② GET /search/acjson                     带 Referer + X-Requested-With
    ③ 解析 data[]                            取 middleURL / width / height / 标题 / 来源
    ④ 过滤 + 语域排序 + 同源去重              横向图 + 最小宽度；现场实拍优先；同源只留少数
    ⑤ 下载 middleURL + 校验 + 文档闸          Content-Type / 体积 / Pillow；剔除扫描件/证书

🔴 两条 2026-10-08 新增的"质量闸"（起因：抽象概念页搜回一堆资质证书）：
    · **语域排序** —— 排序键不再只看清晰度（证书是高对比扫描件、`clarity` 天然最高，
      会被顶到最前），改为「语域分 → 清晰度 → 宽度」。
    · **文档/证书剔除** —— 下载后按「近白底 + 极低饱和」判据剔除扫描件类图，**宁缺毋滥**。

用法（作为模块）:
    from web_image_search import BaiduImageSearch
    s = BaiduImageSearch()
    cands, stats = s.search("素材主题 关键词", limit=30)
    s.download(cands[0], Path("out.jpg"))
"""

from __future__ import annotations

import http.cookiejar
import io
import json
import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

# ── 端点 ──
BAIDU_HOME = "https://image.baidu.com/"
BAIDU_API = "https://image.baidu.com/search/acjson"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# ── 默认过滤阈值 ──
MIN_WIDTH = 800              # 接口声明宽度的粗筛线（避免小图）
MIN_ACTUAL_WIDTH = 580       # 下载后**实际**宽度的硬门槛（PPT 并排图框即 580 宽）
MIN_BYTES = 10 * 1024        # 最小体积（百度 middleURL 是压缩图，通常 10~30KB）
ALLOWED_TYPES = ("jpeg", "jpg", "png")

# ── 文档/证书剔除阈值（2026-10-08 实测标定）──
#   扫描件证书的形态：**大面积白底 + 极低饱和度**。
#   实测样本：证书 white 0.49~0.83 / satlow 0.55~0.97；实拍照片 white 0.00~0.23（分界干净）。
#   扩样 30 张：精确率 80% / 召回 57%。
#   ⚠️ 能挡「扫描件证书」，挡不住「拍摄的证书」与白底信息图 —— 是**兜底闸**，不是银弹。
DOC_WHITE_MIN = 0.28         # 近白底像素占比下限（min(R,G,B) >= 230）
DOC_SATLOW_MIN = 0.50        # 极低饱和像素占比下限（HSV 饱和度 < 0.15）

# ── 语域权重（2026-10-08）──
#   实测：同一对象，泛词返回「怎么选 / 一文看懂 / 操作规程」类**文章配图**，
#   加项目实名或动作词才返回**现场实拍**（含"本报记者摄"）。
#   ⇒ 排序时给"现场语域"加分、给"文章/文档/证书语域"减分。
_LOW_TITLE = ("怎么选", "一文看懂", "一文", "大全", "指南", "攻略", "操作规程",
              "操作规范", "原理", "评估", "认证", "证书", "喜报", "荣获",
              "报告", "课件", "教程", "科普", "是什么", "有哪些", "区别",
              "详解", "范文", "模板", "申报", "ppt")
_HIGH_TITLE = ("记者", "摄", "现场", "施工", "吊装", "浇筑", "拆除", "巡检",
               "投产", "建成", "开工", "贯通", "试压", "焊接", "安装", "调试",
               "首船", "封顶", "发运", "运转")
_LOW_HOST = ("baijiahao", "blog.csdn", "csdn.net", "zhihu", "mbd.baidu",
             "mianfeiwendang", "doc88", "docin", "book118", "wenku", "360doc",
             "renrendoc")
_HIGH_HOST = ("news.", "people.com.cn", "xinhuanet", "chinanews", "cctv",
              "gov.cn", "chinadaily", "thepaper.cn", "cnr.cn", "stdaily")


@dataclass
class ImageCandidate:
    """一条搜索候选。"""
    middle_url: str
    thumb_url: str = ""
    width: int = 0
    height: int = 0
    title: str = ""            # 图片内容标题（fromPageTitleEnc）
    source_host: str = ""      # 来源站点
    source_url: str = ""       # 原始来源 URL（replaceUrl[].ObjURL）
    clarity: float = 0.0       # 清晰度分
    filesize: int = 0
    img_type: str = ""
    keyword: str = ""          # 本次搜索用的关键词

    @property
    def is_landscape(self) -> bool:
        """是否横向构图（PPT 图框全是横向，竖图裁剪难看）。"""
        return self.width > 0 and self.height > 0 and self.width > self.height

    @property
    def aspect(self) -> float:
        return (self.width / self.height) if self.height else 0.0

    def as_dict(self) -> dict:
        return {
            "width": self.width, "height": self.height,
            "title": self.title, "source_host": self.source_host,
            "source_url": self.source_url, "download_url": self.middle_url,
            "keyword": self.keyword, "clarity_score": self.clarity,
            "filesize": self.filesize, "img_type": self.img_type,
        }


@dataclass
class SearchStats:
    raw: int = 0            # 接口返回条数
    landscape: int = 0      # 横向通过的
    wide_enough: int = 0    # 宽度达标的
    duplicates: int = 0     # 同源去重丢掉的
    candidates: list = field(default_factory=list)


def register_score(cand: "ImageCandidate") -> int:
    """语域分：>0 = 更像现场实拍；<0 = 更像文章配图 / 文档 / 证书。

    ⚠️ 只做**排序**，不剔除 —— 分数低只代表排后面，仍可作为候选（宁多勿少）。
    """
    t = (cand.title or "").lower()
    h = (cand.source_host or "").lower()
    s = 0
    for k in _HIGH_TITLE:
        if k in t:
            s += 2
    for k in _LOW_TITLE:
        if k in t:
            s -= 3
    for k in _LOW_HOST:
        if k in h:
            s -= 2
    for k in _HIGH_HOST:
        if k in h:
            s += 1
    return s


def document_ratio(im) -> tuple[float, float]:
    """返回 (近白底占比, 极低饱和占比) —— 用于识别扫描件 / 证书类图。

    缩到 ≤320px 再统计，速度与精度兼顾。用 `tobytes()` 而非 `getdata()`，
    避免 Pillow 的 deprecation 噪音。
    """
    rgb = im.convert("RGB")
    rgb.thumbnail((320, 320))
    raw = rgb.tobytes()
    n = len(raw) // 3 or 1
    white = satlow = 0
    for i in range(0, n * 3, 3):
        r = raw[i]
        g = raw[i + 1]
        b = raw[i + 2]
        if r >= 230 and g >= 230 and b >= 230:
            white += 1
        mx = max(r, g, b)
        if (0.0 if mx == 0 else (mx - min(r, g, b)) / mx) < 0.15:
            satlow += 1
    return white / n, satlow / n


def looks_like_document(im) -> tuple[bool, float, float]:
    """是否疑似文档 / 证书类图。返回 (判定, 近白底占比, 极低饱和占比)。"""
    white, satlow = document_ratio(im)
    return (white >= DOC_WHITE_MIN and satlow >= DOC_SATLOW_MIN), white, satlow


class BaiduImageSearchError(RuntimeError):
    """搜索/下载过程中的错误（含被反爬拦截）。"""


class BaiduImageSearch:
    """百度图片搜索器（自动管理 Cookie 会话）。"""

    def __init__(self, timeout: int = 20):
        self.timeout = timeout
        self.cookie_jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cookie_jar)
        )
        self._session_ready = False

    # ── 会话 ──
    def _ensure_session(self) -> None:
        """访问首页建立 Cookie 会话（只执行一次）。"""
        if self._session_ready:
            return
        req = urllib.request.Request(BAIDU_HOME, headers={
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9",
        })
        try:
            with self.opener.open(req, timeout=self.timeout) as r:
                r.read()
        except Exception as e:
            raise BaiduImageSearchError(f"建立会话失败（访问首页）: {e}") from e
        self._session_ready = True

    # ── 搜索 ──
    def _fetch_page(self, keyword: str, page: int, rn: int) -> list[dict]:
        """请求一页结果，返回原始 data 列表。"""
        params = {
            "tn": "resultjson_com",
            "ipn": "rj",
            "word": keyword,
            "pn": str(page * rn),
            "rn": str(rn),
            "ie": "utf-8",
            "oe": "utf-8",
        }
        url = f"{BAIDU_API}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={
            "User-Agent": UA,
            "Referer": BAIDU_HOME,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "X-Requested-With": "XMLHttpRequest",
        })
        try:
            with self.opener.open(req, timeout=self.timeout) as r:
                raw = r.read().decode("utf-8", "replace")
        except Exception as e:
            raise BaiduImageSearchError(f"请求接口失败: {e}") from e

        # 反爬拦截检测
        if "Forbid spider" in raw or '"antiFlag":1' in raw:
            raise BaiduImageSearchError(
                "被百度反爬拦截（Forbid spider access）—— 会话可能失效，重试或稍后再试"
            )

        try:
            data = json.loads(raw, strict=False)
        except Exception:
            # 百度偶尔返回带控制字符的 JSON，退回容错解析
            try:
                cleaned = raw.replace("\r", " ").replace("\n", " ")
                data = json.loads(cleaned, strict=False)
            except Exception as e:
                raise BaiduImageSearchError(f"JSON 解析失败: {e}") from e

        items = data.get("data") or []
        return [x for x in items if isinstance(x, dict) and x]

    @staticmethod
    def _to_candidate(item: dict, keyword: str) -> ImageCandidate:
        # 原始来源 URL：replaceUrl 是数组，取第一个的 ObjURL
        source_url = ""
        ru = item.get("replaceUrl")
        if isinstance(ru, list) and ru:
            first = ru[0] if isinstance(ru[0], dict) else {}
            source_url = first.get("ObjURL") or first.get("ObjUrl") or ""

        def _int(v, default=0):
            try:
                return int(v)
            except (TypeError, ValueError):
                return default

        def _float(v, default=0.0):
            try:
                return float(v)
            except (TypeError, ValueError):
                return default

        return ImageCandidate(
            middle_url=(item.get("middleURL") or item.get("thumbURL")
                        or item.get("hoverURL") or ""),
            thumb_url=item.get("thumbURL") or "",
            width=_int(item.get("width")),
            height=_int(item.get("height")),
            title=(item.get("fromPageTitleEnc") or item.get("fromPageTitle") or "").strip(),
            source_host=(item.get("fromURLHost") or "").strip(),
            source_url=source_url,
            clarity=_float(item.get("clarityScore")),
            filesize=_int(item.get("filesize")),
            img_type=(item.get("type") or "").strip().lower(),
            keyword=keyword,
        )

    def search(
        self,
        keyword: str,
        limit: int = 30,
        *,
        min_width: int = MIN_WIDTH,
        landscape_only: bool = True,
        retries: int = 3,
    ) -> tuple[list[ImageCandidate], SearchStats]:
        """搜索并过滤，返回 (候选列表, 统计)。

        过滤规则：只收横向图 + 宽度 >= min_width + 有可下载 URL。
        """
        self._ensure_session()

        stats = SearchStats()
        last_err: Exception | None = None

        for attempt in range(1, retries + 1):
            try:
                items = self._fetch_page(keyword, page=0, rn=max(limit, 30))
            except BaiduImageSearchError as e:
                last_err = e
                self._session_ready = False   # 会话可能失效 → 重建后重试
                if attempt < retries:
                    time.sleep(1.5 * attempt)
                    continue
                raise

            stats.raw = len(items)
            cands: list[ImageCandidate] = []
            for it in items:
                c = self._to_candidate(it, keyword)
                if not c.middle_url:
                    continue
                if landscape_only and not c.is_landscape:
                    continue
                stats.landscape += 1
                if c.width < min_width:
                    continue
                stats.wide_enough += 1
                if c.img_type and c.img_type not in ALLOWED_TYPES:
                    continue
                cands.append(c)

            # 语域优先（现场实拍 > 中性 > 文章/文档/证书），再比清晰度与宽度。
            # 🔴 旧版只按 clarity 排 —— 证书是高对比扫描件、清晰度天然最高，会被顶到最前。
            cands.sort(key=lambda x: (-register_score(x), -x.clarity, -x.width))

            # 同源去重：同一篇文章的配图常有多张同标题 / 同目录的裁剪版本，只留代表。
            # 排完序再去重 ⇒ 保留的是语域与清晰度最好的那张。
            seen_title: set[str] = set()
            seen_url: set[str] = set()
            seen_dir: dict[str, int] = {}
            uniq: list[ImageCandidate] = []
            for c in cands:
                t = re.sub(r"\s+", "", c.title or "")[:36]
                u = (c.source_url or "").strip()
                d = u.rsplit("/", 1)[0] if "/" in u else ""
                if len(t) >= 6 and t in seen_title:
                    stats.duplicates += 1
                    continue
                if u and u in seen_url:
                    stats.duplicates += 1
                    continue
                if d and seen_dir.get(d, 0) >= 2:      # 同目录最多留 2 张
                    stats.duplicates += 1
                    continue
                if len(t) >= 6:
                    seen_title.add(t)
                if u:
                    seen_url.add(u)
                if d:
                    seen_dir[d] = seen_dir.get(d, 0) + 1
                uniq.append(c)

            stats.candidates = uniq
            return uniq[:limit], stats

        raise BaiduImageSearchError(f"搜索失败（重试 {retries} 次）: {last_err}")

    # ── 下载 ──
    def download(self, cand: ImageCandidate, dest: Path,
                 *, min_bytes: int = MIN_BYTES,
                 min_actual_width: int = MIN_ACTUAL_WIDTH,
                 reject_documents: bool = True) -> tuple[bool, str]:
        """下载并校验。返回 (是否成功, 说明)。

        ⚠️ 百度 `middleURL` 是**压缩后的中等图**，其**实际尺寸往往远小于接口声明的
        `width`/`height`**（实测：接口声称 1440×1080，实下 667×500）。
        因此这里**以下载后的真实尺寸为准**，并回写进 candidate。

        🔴 `reject_documents=True`（默认）时，会剔除**扫描件 / 证书类**图
        （近白底 + 极低饱和，见 `DOC_WHITE_MIN` / `DOC_SATLOW_MIN`）—— 这类图
        在 PPT 里通常毫无用处，且证书上常印着**别家公司的名称**，会造成误导。
        剔除后**不补位**（宁缺毋滥）。
        """
        if not cand.middle_url:
            return False, "无可下载地址"

        req = urllib.request.Request(cand.middle_url, headers={
            "User-Agent": UA,
            "Referer": BAIDU_HOME,          # 百度 CDN 有防盗链
            "Accept": "image/avif,image/webp,image/*,*/*;q=0.8",
        })
        try:
            with self.opener.open(req, timeout=self.timeout) as r:
                data = r.read()
                ctype = r.headers.get("Content-Type", "")
        except Exception as e:
            return False, f"下载失败: {type(e).__name__}: {e}"

        if len(data) < min_bytes:
            return False, f"体积过小（{len(data)} B < {min_bytes} B）"
        if "image" not in ctype.lower():
            return False, f"非图片类型（Content-Type: {ctype}）"

        # 校验 + 读**真实**尺寸 + 文档闸
        is_doc = False
        white = satlow = 0.0
        try:
            from PIL import Image
            im = Image.open(io.BytesIO(data))
            actual_w, actual_h = im.size
            if reject_documents:
                # ⚠️ 必须在 verify() **之前**算 —— verify() 之后对象不可再用
                is_doc, white, satlow = looks_like_document(im)
            im.verify()                      # 完整性校验
        except Exception as e:
            return False, f"图片无法打开: {type(e).__name__}"

        if reject_documents and is_doc:
            return False, (f"疑似文档/证书类图，已剔除"
                           f"（近白底 {white:.0%} / 极低饱和 {satlow:.0%}）")

        if actual_w < min_actual_width:
            return False, f"实际宽度不足（{actual_w}px < {min_actual_width}px）"
        if actual_w <= actual_h:
            return False, f"实际为竖构图（{actual_w}×{actual_h}）"

        # 回写真实尺寸（供登记与审计使用）
        cand.width, cand.height = actual_w, actual_h
        cand.filesize = len(data)

        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        return True, f"{len(data):,} B  {actual_w}×{actual_h}"


# ── CLI（便于单独测试）──
def _main() -> int:
    import argparse
    import sys

    ap = argparse.ArgumentParser(description="百度图片搜索（测试用 CLI）")
    ap.add_argument("keyword", help="搜索关键词")
    ap.add_argument("-o", "--outdir", default=".", help="输出目录")
    ap.add_argument("-n", "--count", type=int, default=3, help="下载张数")
    ap.add_argument("--no-landscape-filter", action="store_true", help="不过滤横向构图")
    ap.add_argument("--keep-documents", action="store_true",
                    help="保留扫描件/证书类图（默认剔除）")
    args = ap.parse_args()

    s = BaiduImageSearch()
    try:
        cands, stats = s.search(
            args.keyword,
            limit=max(args.count * 3, 20),
            landscape_only=not args.no_landscape_filter,
        )
    except BaiduImageSearchError as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        return 1

    print(f"接口返回 {stats.raw} 条 | 横向 {stats.landscape} | "
          f"宽度达标 {stats.wide_enough} | 同源去重 -{stats.duplicates} | "
          f"最终候选 {len(cands)}")
    if not cands:
        print("没有符合条件的结果")
        return 1

    outdir = Path(args.outdir)
    got = 0
    for i, c in enumerate(cands[: args.count], 1):
        dest = outdir / f"web_{i:02d}.jpg"
        ok, msg = s.download(c, dest, reject_documents=not args.keep_documents)
        print(f"  [{'OK ' if ok else 'SKIP'}] {dest.name}  {c.width}x{c.height}  {msg}")
        if c.title:
            print(f"         标题: {c.title[:60]}")
        got += ok
    return 0 if got else 1


if __name__ == "__main__":
    import sys
    sys.exit(_main())
