"""转录后本地 LLM 精炼：用本地 Ollama 模型把听写结果整理得更通顺/简洁。

全本地、可选、带超时兜底——Ollama 没开 / 模型没拉 / 超时 / 出错，一律原样返回，
绝不卡听写、绝不丢内容。两种力度：
  light   ：只修标点/错别字/口误结巴，尽量保留原话原意（默认，最稳）
  concise ：去口语赘述、重复、口头禅，更简洁（会改写得多一些）

支持带"思考"的模型（如 GLM-4.7-Flash）：Ollama 把 reasoning 放在 message.thinking，
我们只取 message.content（最终答案）。对不支持思考的模型自动退化为普通请求。
"""

from __future__ import annotations

import json
import re
import urllib.request

from src.utils.logger import logger

# 兜底：万一某些模型把思考内联进 content（<think>...</think>），剥掉它
_THINK_RE = re.compile(r"<think>.*?</think>", re.S | re.I)

_SYS = {
    "light": (
        "你是中文听写润色助手。任务：只修正标点、错别字、明显的口误和结巴重复，"
        "尽量保留原话、原意和原有用词，不要扩写、不要删改实质内容。"
        "直接输出修正后的文本本身，不要解释、不要加引号、不要加任何前后缀。"
    ),
    "concise": (
        "你是中文听写润色助手。任务：在不改变原意的前提下，去掉口语赘述、重复、"
        "口头禅（嗯、那个、就是说、然后），让表达简洁通顺。"
        "直接输出整理后的文本本身，不要解释、不要加引号、不要加任何前后缀。"
    ),
}


class OllamaPolisher:
    """通过本地 Ollama /api/chat 做转录后润色。任何异常都返回原文。"""

    def __init__(self, url: str, model: str, mode: str = "light",
                 timeout: float = 20.0, think: bool = True) -> None:
        self.url = (url or "http://localhost:11434").rstrip("/")
        self.model = model or "glm-4.7-flash"
        self.mode = mode if mode in _SYS else "light"
        self.timeout = float(timeout)
        self.think = think

    def _chat(self, text: str, think) -> str:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": _SYS[self.mode]},
                {"role": "user", "content": text},
            ],
            "stream": False,
            "options": {"temperature": 0.2},
        }
        if think is not None:
            payload["think"] = think
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self.url + "/api/chat", data=data,
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            obj = json.loads(resp.read().decode("utf-8"))
        return ((obj.get("message") or {}).get("content") or "")

    def polish(self, text: str) -> str:
        """返回精炼后的文本；任何失败/超时/空结果都原样返回 text。"""
        if not text or len(text.strip()) < 2:
            return text
        last_err = None
        # 先按配置带 think 试；失败再退化为不带 think（兼容不支持思考的模型）
        for think in ([True, None] if self.think else [None]):
            try:
                out = self._chat(text, think)
            except Exception as exc:  # noqa: BLE001 — 兜底：润色绝不能拖垮听写
                last_err = exc
                continue
            out = _THINK_RE.sub("", out or "").strip().strip("「」“”\"'")
            if out:
                logger.info(f"[Polish] {self.model}/{self.mode}: {len(text)}→{len(out)} 字")
                return out
        logger.warning(f"[Polish] 未生效，退回原文（{last_err!r}）")
        return text
