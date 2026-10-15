"""Shared dashboard HTML parsing helpers for UI regression tests."""
import re
from html.parser import HTMLParser


def compact(text):
    return re.sub(r"\s+", "", text)


class DashboardHTML(HTMLParser):
    """Check tag nesting and collect visible text without CSS or script content."""

    VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input",
                 "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.errors = []
        self.tags = []
        self.visible = []
        self.checks = []
        self.feed(html)
        self.close()
        if self.stack:
            self.errors.append("Unclosed tags: " + repr(self.stack))

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        self.tags.append((tag, attrs))
        if tag not in self.VOID_TAGS:
            self.stack.append((tag, attrs.get("class", "").split()))

    def handle_startendtag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))

    def handle_endtag(self, tag):
        if not self.stack or self.stack[-1][0] != tag:
            self.errors.append("Unexpected closing tag: " + tag)
        else:
            self.stack.pop()

    def handle_data(self, data):
        if not any(tag in {"style", "script"} for tag, _ in self.stack):
            self.visible.append(data)
            if any("checks" in classes for _, classes in self.stack):
                self.checks.append(data)

    @property
    def text(self):
        return " ".join(self.visible)

    @property
    def check_text(self):
        return " ".join(self.checks)
