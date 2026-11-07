"""本地词库（Local Vocabulary）：你自己的专有名词 + 显式纠错规则，全程本地。

文件格式（vocabulary.txt，纯文本、可手改，也是 MCP vocab_* 工具读写的同一个文件）：
    Claude                # 一行一个词：作为「热词」注入 Qwen3-ASR 的 context，提高识别率
    Cloud => Claude       # 显式纠错规则：识别结果里出现左边，就换成右边（任何引擎都生效）
    # 以 # 开头是注释

设计原则（与 docs/v0.3.0-blueprint.md 一致，本文件是其中「词表 + 显式规则」的第一步）：
  * 只做精确匹配，不做拼音/模糊——宁可漏改，不可错改。
  * 英文规则按单词边界、大小写不敏感；中文规则按字面子串。最长优先、单次扫描、不重入。
  * 规则左边至少 2 个字符；每句最多替换 VOCAB_MAX_REPLACEMENTS 处（默认 8）。
  * 文件改了自动生效（按修改时间热加载），不用重启服务。
  * 任何异常都原样返回文本——词库出错绝不影响听写。
"""

from __future__ import annotations

import os
import re
import threading

from src.utils.logger import logger

_LATIN = re.compile(r"[A-Za-z0-9]")


class Vocabulary:
    def __init__(self, path: str, max_replacements: int = 8, enabled: bool = True):
        self.path = os.path.expanduser(path)
        self.max_replacements = max(0, int(max_replacements))
        self.enabled = enabled
        self._lock = threading.Lock()
        self._mtime: float | None = None
        self._terms: list[str] = []
        self._rules: list[tuple[str, str]] = []
        self._pattern: re.Pattern | None = None

    # ---------------- 加载 ----------------

    def _reload_if_changed(self) -> None:
        try:
            mtime = os.path.getmtime(self.path)
        except OSError:
            mtime = None
        if mtime == self._mtime:
            return
        terms: list[str] = []
        rules: list[tuple[str, str]] = []
        if mtime is not None:
            with open(self.path, encoding="utf-8") as f:
                for raw in f:
                    line = raw.split(" #", 1)[0].strip()  # 允许行尾注释（# 前需有空格）
                    if not line or line.startswith("#"):
                        continue
                    if "=>" in line:
                        wrong, right = (s.strip() for s in line.split("=>", 1))
                        if len(wrong) < 2 or not right or wrong == right:
                            logger.warning(f"[词库] 忽略无效规则：{line!r}（左边至少 2 个字符，且左右不能相同）")
                            continue
                        rules.append((wrong, right))
                        if right not in terms:
                            terms.append(right)
                    elif line not in terms:
                        terms.append(line)
        self._terms, self._rules, self._mtime = terms, rules, mtime
        self._pattern = self._compile(rules)
        if mtime is not None:
            logger.info(f"[词库] 已加载 {len(terms)} 个词、{len(rules)} 条纠错规则：{self.path}")

    @staticmethod
    def _compile(rules: list[tuple[str, str]]) -> re.Pattern | None:
        if not rules:
            return None
        parts = []
        # 最长优先：同一位置上，较长的规则先匹配（正则交替是「左侧优先」）
        for idx in sorted(range(len(rules)), key=lambda i: -len(rules[i][0])):
            w = rules[idx][0]
            body = re.escape(w)
            if _LATIN.search(w[0]):
                body = r"(?<![A-Za-z0-9_])" + body
            if _LATIN.search(w[-1]):
                body = body + r"(?![A-Za-z0-9_])"
            parts.append(f"(?P<r{idx}>{body})")
        return re.compile("|".join(parts), re.IGNORECASE)

    # ---------------- 对外接口 ----------------

    def terms(self) -> list[str]:
        with self._lock:
            try:
                self._reload_if_changed()
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[词库] 读取失败：{exc}")
            return list(self._terms) if self.enabled else []

    def context(self) -> str | None:
        """给 Qwen3-ASR 的 context（热词）。没有词时返回 None。"""
        words = self.terms()
        return f"Vocabulary: {', '.join(words)}." if words else None

    def correct(self, text: str) -> str:
        """按显式规则纠错；记录每一处修改。任何异常都返回原文。"""
        if not text or not self.enabled or self.max_replacements == 0:
            return text
        try:
            with self._lock:
                self._reload_if_changed()
                pattern, rules = self._pattern, list(self._rules)
            if pattern is None:
                return text
            changes: list[str] = []

            def _sub(m: re.Match) -> str:
                if len(changes) >= self.max_replacements:
                    return m.group(0)
                right = rules[int(m.lastgroup[1:])][1]
                if m.group(0) == right:
                    return right
                changes.append(f"{m.group(0)}→{right}")
                return right

            out = pattern.sub(_sub, text)
            if changes:
                logger.info(f"[词库] 纠正 {len(changes)} 处：{', '.join(changes)}")
            return out
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[词库] 纠错失败，保留原文：{exc}")
            return text

    def stats(self) -> dict:
        terms = self.terms()
        return {"file": self.path, "enabled": self.enabled, "terms": len(terms), "rules": len(self._rules)}


def from_env(app_dir: str) -> Vocabulary:
    return Vocabulary(
        os.getenv("VOCAB_FILE", os.path.join(app_dir, "vocabulary.txt")),
        max_replacements=int(os.getenv("VOCAB_MAX_REPLACEMENTS", "8")),
        enabled=os.getenv("VOCAB_ENABLED", "true").lower() == "true",
    )
