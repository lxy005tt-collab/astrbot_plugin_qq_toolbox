"""Deterministic replacement of visible outgoing text."""

import re

from astrbot.api.message_components import Node, Nodes, Plain, Record


DEFAULT_MARKDOWN_SYMBOLS = ("*", "-", "+", "#", "_", "`", ">", "~", "|", "[", "]")


class WordFilter:
    def __init__(self, settings):
        self.settings = settings
        self._signature = None
        self._pair_rule = None
        self._pair_replacements = {}
        self._rules = []
        self._replacement = "***"
        self.refresh()

    @property
    def block_asterisk(self):
        return bool(self.settings.get("enabled", True) and self.settings.get("block_asterisk", False))

    @property
    def block_symbols(self):
        if not self.settings.get("enabled", True):
            return ()
        symbols = ["*"] if self.block_asterisk else []
        if self.settings.get("block_markdown", False):
            symbols.extend(symbol for symbol in self.settings.get("markdown_symbols", DEFAULT_MARKDOWN_SYMBOLS)
                           if isinstance(symbol, str) and symbol)
        return tuple(dict.fromkeys(symbols))

    @property
    def has_block_rules(self):
        return bool(self.block_symbols)

    def blocks_text(self, text):
        return isinstance(text, str) and any(symbol in text for symbol in self.block_symbols)

    def blocks_components(self, components):
        for component in components:
            if isinstance(component, (Plain, Record)) and self.blocks_text(getattr(component, "text", "")):
                return True
            if isinstance(component, Node) and self.blocks_components(component.content):
                return True
            if isinstance(component, Nodes) and self.blocks_components(component.nodes):
                return True
        return False

    def refresh(self):
        pairs = {}
        for index, entry in enumerate(self.settings.get("word_replacements", []), 1):
            entry = str(entry).strip()
            if not entry:
                continue
            source, separator, target = entry.partition("=")
            if not separator:
                source, separator, target = entry.partition("＝")
            if not separator or not source.strip():
                raise ValueError(f"词语替换：第 {index} 项请写成 原词 = 替换词，例如 折腾 = 搞")
            pairs[source.strip()] = target.strip()
        pairs = tuple(pairs.items())
        words = tuple(dict.fromkeys(str(w).strip() for w in self.settings.get("words", []) if str(w).strip()))
        patterns = tuple(str(p).strip() for p in self.settings.get("patterns", []) if str(p).strip())
        signature = (
            bool(self.settings.get("enabled", True)), pairs, words, patterns,
            str(self.settings.get("replacement", "***")),
            bool(self.settings.get("ignore_case", True)),
        )
        if signature == self._signature:
            return
        enabled, pairs, words, patterns, replacement, ignore_case = signature
        flags = re.IGNORECASE if ignore_case else 0
        rules = []
        pair_rule = None
        pair_replacements = {}
        if enabled:
            if pairs:
                ordered = sorted(pairs, key=lambda pair: len(pair[0]), reverse=True)
                pair_rule = re.compile("|".join(
                    f"(?P<r{index}>{re.escape(source)})"
                    for index, (source, _) in enumerate(ordered)
                ), flags)
                pair_replacements = {f"r{index}": target for index, (_, target) in enumerate(ordered)}
            if words:
                rules.append(re.compile("|".join(re.escape(w) for w in sorted(words, key=len, reverse=True)), flags))
            for index, pattern in enumerate(patterns, 1):
                try:
                    rules.append(re.compile(pattern, flags))
                except re.error as error:
                    raise ValueError(f"输出禁词替换：第 {index} 条正则规则无效：{error}") from error
        self._pair_rule = pair_rule
        self._pair_replacements = pair_replacements
        self._rules = rules
        self._replacement = replacement
        self._signature = signature

    def replace(self, text):
        self.refresh()
        if self._pair_rule is not None:
            # Replace all pairs in one pass so newly inserted words are not
            # processed again by another pair, and keep target text literal.
            text = self._pair_rule.sub(lambda match: self._pair_replacements[match.lastgroup], text)
        for pattern in self._rules:
            # The replacement is literal text, not a regex backreference template.
            text = pattern.sub(lambda match: self._replacement, text)
        return text

    def components(self, components):
        output = []
        pending = []

        def flush():
            if not pending:
                return
            original = "".join(part.text for part in pending)
            replaced = self.replace(original)
            if replaced == original:
                output.extend(pending)
            else:
                output.append(pending[0].copy(update={"text": replaced}))
            pending.clear()

        for component in components:
            if isinstance(component, Plain):
                pending.append(component)
                continue
            flush()
            if isinstance(component, Node):
                component = component.copy(update={"content": self.components(component.content)})
            elif isinstance(component, Nodes):
                component = component.copy(update={"nodes": self.components(component.nodes)})
            output.append(component)
        flush()
        return output

    def chain(self, message):
        self.refresh()
        if self._pair_rule is None and not self._rules:
            return message
        return message.derive(self.components(message.chain))
