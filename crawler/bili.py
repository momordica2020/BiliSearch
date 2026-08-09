"""Bilibili 匿名 API 客户端。

要点：
- 用 curl_cffi 伪装 Chrome 的 TLS 指纹（可选依赖，缺失时回退 urllib）；
- SPI 接口换 buvid3/buvid4 cookie，nav 接口拿 WBI 密钥并签名；
- 内置限速、随机抖动与退避重试，遇到 -352/-412/-509 等风控码自动换指纹重试。
"""

import hashlib
import json
import random
import time
import urllib.parse

try:
    from curl_cffi import requests as _curl_req

    HAS_CURL_CFFI = True
except Exception:  # pragma: no cover - 仅在没有 curl_cffi 的环境走这里
    import urllib.error
    import urllib.request

    _curl_req = None
    HAS_CURL_CFFI = False

BASE_URL = "https://api.bilibili.com"
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52,
]

# 风控 / 临时性错误码：重试可能成功
RETRYABLE_CODES = {-352, -412, -509, -799, -111, -403}


class BiliError(Exception):
    """B 站接口错误（含风控、限频、参数错误）。"""


def get_mixin_key(orig: str) -> str:
    return "".join(orig[i] for i in MIXIN_KEY_ENC_TAB)[:32]


# av 号 -> BV 号（B 站 bvid 编码，字符表来自 bilibili-API-collect 实测版本）
_BV_TABLE = "fZodR9XQDSUm21yCkr6zBqiveYah8bt4xsWpHnJE7jL5VG3guMTKNPAwcF"
_BV_S = [11, 10, 3, 8, 4, 6]
_BV_XOR = 177451812
_BV_ADD = 8728348608


def av2bv(aid):
    x = (int(aid) ^ _BV_XOR) + _BV_ADD
    bv = list("BV1??4?1?7??")
    for i in range(6):
        bv[_BV_S[i]] = _BV_TABLE[x // 58 ** i % 58]
    return "".join(bv)


class BiliClient:
    def __init__(self, state: dict, interval: float = 1.2, timeout: float = 15.0,
                 max_retries: int = 4):
        self.state = state
        self.interval = interval
        self.timeout = timeout
        self.max_retries = max_retries
        self._last = 0.0
        self._cookie = None
        self._wbi_key = state.get("wbi_key") or None
        self._wbi_at = float(state.get("wbi_at") or 0)
        if HAS_CURL_CFFI:
            self._session = _curl_req.Session(impersonate="chrome")
        else:
            self._session = None

    # ---------- 基础请求 ----------

    def _headers(self, extra=None):
        h = {
            "User-Agent": DEFAULT_UA,
            "Referer": "https://www.bilibili.com/",
            "Origin": "https://www.bilibili.com",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Sec-Fetch-Site": "same-site",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Dest": "empty",
        }
        if extra:
            h.update(extra)
        return h

    def _throttle(self):
        wait = self._last + self.interval + random.uniform(0, 0.4) - time.time()
        if wait > 0:
            time.sleep(wait)
        self._last = time.time()

    def _bare_get(self, path, params=None):
        """不需要 cookie 的请求（spi / nav），不参与重试。"""
        self._throttle()
        url = BASE_URL + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        if HAS_CURL_CFFI:
            resp = self._session.get(url, headers=self._headers(), timeout=self.timeout)
            return resp.json()
        req = urllib.request.Request(url, headers=self._headers())
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return json.loads(r.read().decode("utf-8"))

    def _get(self, path, params=None, signed=False):
        """带 cookie 的 JSON 请求；风控码/网络错误自动退避重试。"""
        self._ensure_cookies()
        last_err = None
        for attempt in range(self.max_retries):
            try:
                if signed:
                    self._ensure_wbi()
                    real_params = self._sign(params or {})
                else:
                    real_params = params or {}
                self._throttle()
                url = BASE_URL + path
                if real_params:
                    url += "?" + urllib.parse.urlencode(real_params)
                headers = self._headers({"Cookie": self._cookie})
                if HAS_CURL_CFFI:
                    resp = self._session.get(url, headers=headers, timeout=self.timeout)
                    data = resp.json()
                else:
                    req = urllib.request.Request(url, headers=headers)
                    with urllib.request.urlopen(req, timeout=self.timeout) as r:
                        data = json.loads(r.read().decode("utf-8"))
                code = data.get("code")
                if code == 0:
                    return data
                if code in RETRYABLE_CODES and attempt + 1 < self.max_retries:
                    last_err = BiliError(f"{path} code={code} {data.get('message')}")
                    self._refresh_identity()
                    time.sleep(min(30, 3 * (2 ** attempt)) + random.uniform(0, 2))
                    continue
                raise BiliError(f"{path} code={code} {data.get('message')}")
            except BiliError:
                raise
            except Exception as exc:  # HTTP 错误 / 超时 / 连接失败
                last_err = exc
                if attempt + 1 < self.max_retries:
                    time.sleep(min(30, 3 * (2 ** attempt)) + random.uniform(0, 2))
                    continue
                raise BiliError(f"{path} 网络错误: {exc!r}") from exc
        raise BiliError(f"{path} 重试耗尽: {last_err!r}")

    def _refresh_identity(self):
        """风控时重置 cookie 与 WBI 密钥，下次请求重新获取。"""
        for k in ("buvid3", "buvid4", "b_nut", "wbi_key", "wbi_at"):
            self.state.pop(k, None)
        self._cookie = None
        self._wbi_key = None

    def _ensure_cookies(self):
        if self._cookie:
            return
        if not self.state.get("buvid3"):
            spi = self._bare_get("/x/frontend/finger/spi")
            if spi.get("code") != 0:
                raise BiliError(f"获取指纹失败: {spi}")
            d = spi.get("data") or {}
            self.state["buvid3"] = d.get("b_3")
            self.state["buvid4"] = d.get("b_4")
            self.state["b_nut"] = int(time.time())
        extra = self.state.get("cookie_extra", "")
        self._cookie = (
            f"buvid3={self.state['buvid3']}; buvid4={self.state.get('buvid4', '')}; "
            f"b_nut={self.state.get('b_nut', int(time.time()))}"
            + (f"; {extra}" if extra else "")
        )

    def _ensure_wbi(self):
        if self._wbi_key and time.time() - self._wbi_at < 86400:
            return
        nav = self._bare_get("/x/web-interface/nav")
        wbi = (nav.get("data") or {}).get("wbi_img") or {}
        img = wbi.get("img_url", "").rsplit("/", 1)[-1].split(".")[0]
        sub = wbi.get("sub_url", "").rsplit("/", 1)[-1].split(".")[0]
        if not (img and sub):
            raise BiliError(f"无法获取 WBI 密钥: nav code={nav.get('code')}")
        self._wbi_key = get_mixin_key(img + sub)
        self._wbi_at = time.time()
        self.state["wbi_key"] = self._wbi_key
        self.state["wbi_at"] = self._wbi_at

    def _sign(self, params):
        p = dict(params)
        p["wts"] = int(time.time())
        query = urllib.parse.urlencode(sorted(p.items()))
        p["w_rid"] = hashlib.md5((query + self._wbi_key).encode()).hexdigest()
        return p

    # ---------- 业务接口 ----------

    def video(self, ident: str):
        """ident: BVxxxx 或 avNNN。返回 view 接口的 data 对象。"""
        if ident.lower().startswith("av"):
            params = {"aid": ident[2:]}
        else:
            params = {"bvid": ident}
        return (self._get("/x/web-interface/view", params).get("data") or {})

    def video_related(self, ident: str, limit: int = 10):
        """相关视频列表（部分接口限 20 条）。"""
        if ident.lower().startswith("av"):
            d = self.video(ident)
            ident = d.get("bvid") or ident
        data = self._get("/x/web-interface/archive/related", {"bvid": ident}).get("data") or []
        return data[:limit]

    def user(self, mid):
        return (self._get("/x/space/acc/info", {"mid": mid}).get("data") or {})

    def user_card(self, mid):
        """备用用户资料接口（acc/info 被风控时用）。"""
        data = self._get("/x/web-interface/card", {"mid": mid, "photo": False}).get("data") or {}
        card = data.get("card") or {}
        return {"name": card.get("name") or "", "sign": card.get("sign") or ""}

    def user_videos(self, mid, pn=1, ps=30):
        """用户投稿。返回 (vlist, total)。"""
        data = self._get(
            "/x/space/wbi/arc/search",
            {"mid": mid, "pn": pn, "ps": ps, "order": "pubdate", "platform": "web"},
            signed=True,
        ).get("data") or {}
        vlist = (data.get("list") or {}).get("vlist") or []
        total = int((data.get("page") or {}).get("count") or 0)
        return vlist, total

    def user_dynamics(self, mid, offset=None):
        """用户动态。返回 (items, has_more, next_offset)。"""
        params = {"host_mid": mid, "timezone_offset": -480,
                  "web_location": "333.935", "platform": "web"}
        if offset:
            params["offset"] = offset
        data = self._get(
            "/x/polymer/web-dynamic/v1/feed/space", params, signed=True
        ).get("data") or {}
        return data.get("items") or [], bool(data.get("has_more")), data.get("offset")

    def dynamic_detail(self, dyn_id):
        """按动态 ID 取详情（动态种子用）。"""
        return self._get("/x/polymer/web-dynamic/v1/detail", {"id": dyn_id}).get("data") or {}

    def article(self, cvid):
        return (self._get("/x/article/view", {"id": cvid}).get("data") or {})

    def user_articles(self, mid, pn=1, ps=20):
        """用户专栏。返回 (articles, total)。"""
        data = self._get(
            "/x/space/article",
            {"mid": mid, "pn": pn, "ps": ps, "sort": "publish_time"},
        ).get("data") or {}
        return data.get("articles") or [], int(data.get("count") or 0)

    def popular(self, pn=1, ps=20):
        """热门视频，用于无种子时引导爬取。"""
        data = self._get("/x/web-interface/popular", {"pn": pn, "ps": ps}).get("data") or {}
        return data.get("list") or []

    def ranking(self, rid=0):
        """分区排行榜（rid=0 全站，1 动画 / 3 音乐 / 4 游戏 / 5 娱乐 / 36 科技等）。"""
        data = self._get(
            "/x/web-interface/ranking/v2", {"rid": rid, "type": "all"}
        ).get("data") or {}
        return data.get("list") or []

    def precious(self):
        """入站必刷（约百条经典视频）。"""
        data = self._get("/x/web-interface/popular/precious").get("data") or {}
        return data.get("list") or []

    def series(self, number=1):
        """每周必看第 N 期（每期约 8 条）。"""
        data = self._get(
            "/x/web-interface/popular/series/one", {"number": number}
        ).get("data") or {}
        return data.get("list") or []
