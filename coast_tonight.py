#!/usr/bin/env python3
"""Display the official Coast to Coast AM Tonight listing (Python 3.9+)."""

import argparse
from dataclasses import asdict, dataclass, field
from datetime import date
from html.parser import HTMLParser
import json
import math
import os
import re
import shutil
import sys
import textwrap
from urllib.error import URLError
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.request import Request, urlopen
import webbrowser


HOME_URL = "https://www.coasttocoastam.com/"
AIRTIME = "10 pm–2 am Pacific / 1 am–5 am Eastern (following morning)"
VOID_TAGS = set("area base br col embed hr img input link meta param source track wbr".split())
BLOCK_TAGS = set("p div section article li h1 h2 h3 h4 br".split())
CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\x1b]")
HALF_LABEL = re.compile(r"^(First Half|Second Half)(\s*:)?\s*(.*)$", re.IGNORECASE)


class ListingError(Exception):
    """The site did not provide a usable listing."""


@dataclass(frozen=True)
class Palette:
    """Small ANSI palette used by the human-readable terminal view."""

    enabled: bool = False

    def paint(self, text, code):
        if not self.enabled:
            return text
        return f"\033[{code}m{text}\033[0m"

    def bold(self, text):
        return self.paint(text, "1")

    def dim(self, text):
        return self.paint(text, "2")

    def cyan(self, text):
        return self.paint(text, "36")

    def blue(self, text):
        return self.paint(text, "34")

    def yellow(self, text):
        return self.paint(text, "33")


def color_enabled(mode, stream):
    """Resolve auto/always/never color behavior without coloring JSON output."""
    if mode == "always":
        return True
    if mode == "never" or "NO_COLOR" in os.environ:
        return False
    try:
        return bool(stream.isatty()) and os.environ.get("TERM") != "dumb"
    except (AttributeError, OSError):
        return False


@dataclass
class Node:
    tag: str
    attrs: dict = field(default_factory=dict)
    children: list = field(default_factory=list)

    def find_all(self, *, css_class=None, tag=None):
        for child in self.children:
            if isinstance(child, Node):
                if (tag is None or child.tag == tag) and (
                    css_class is None or css_class in (child.attrs.get("class") or "").split()
                ):
                    yield child
                yield from child.find_all(css_class=css_class, tag=tag)

    def find(self, **kwargs):
        return next(self.find_all(**kwargs), None)

    def text(self):
        if self.tag in {"script", "style"}:
            return ""
        content = "".join(c.text() if isinstance(c, Node) else c for c in self.children)
        return content + ("\n" if self.tag in BLOCK_TAGS else "")


class PageParser(HTMLParser):
    """A small HTML tree reader; does not execute the site's JavaScript."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("document")
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = Node(tag, dict(attrs))
        self.stack[-1].children.append(node)
        if tag not in VOID_TAGS:
            self.stack.append(node)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID_TAGS:
            self.handle_endtag(tag)

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def parse_html(html):
    parser = PageParser()
    parser.feed(html)
    parser.close()
    return parser.root


def clean_text(node):
    if not node:
        return ""
    # The site is remote input; strip terminal control characters before
    # displaying or serializing text fetched from it.
    text = CONTROL_CHARS.sub("", node.text())
    return " ".join(text.split())


def paragraphs(node):
    if node is None:
        return []
    result = []
    for line in node.text().splitlines():
        cleaned = clean_text_from_string(line)
        if cleaned:
            result.append(cleaned)
    return result


def clean_text_from_string(value):
    value = CONTROL_CHARS.sub("", value)
    return " ".join(value.split())


def fetch_html(url, timeout):
    request = Request(url, headers={
        "User-Agent": "Mozilla/5.0 (compatible; CoastTonight/1.0)",
        "Accept": "text/html",
        "Cache-Control": "no-cache",
    })
    with urlopen(request, timeout=timeout) as response:
        # Keep a changed or broken server from returning an unbounded response.
        limit = 5 * 1024 * 1024
        body = response.read(limit + 1)
        if len(body) > limit:
            raise ListingError("The website returned an unexpectedly large page.")
        encoding = response.headers.get_content_charset() or "utf-8"
        try:
            return body.decode(encoding, errors="replace")
        except LookupError:
            # A malformed charset header should not make the CLI crash.
            return body.decode("utf-8", errors="replace")


@dataclass
class Show:
    title: str
    date: str
    host: str
    guests: list
    description: list
    url: str
    airtime: str = AIRTIME
    warnings: list = field(default_factory=list)


def parse_tonight(html):
    # Scope every selector to Tonight, so Last Night and sidebar listings cannot win.
    tonight = parse_html(html).find(css_class="coast-shows-tonight")
    if tonight is None:
        raise ListingError("No Tonight listing was found. The site may be updating or its layout changed.")
    title = clean_text(tonight.find(css_class="item-title"))
    url = ""
    show_date = ""
    for link in tonight.find_all(tag="a"):
        candidate = urljoin(HOME_URL, link.attrs.get("href", ""))
        parsed = urlsplit(candidate)
        match = re.fullmatch(r"/show/(\d{4}-\d{2}-\d{2})-show/?", parsed.path)
        if match and parsed.scheme == "https" and parsed.netloc == urlsplit(HOME_URL).netloc:
            try:
                show_date = date.fromisoformat(match.group(1)).isoformat()
            except ValueError:
                # Ignore malformed dated links and keep looking for the real listing.
                continue
            # Keep only the validated same-origin path.  Query strings and
            # fragments are remote input and must not reach terminal output.
            url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
            break
    if not title or not url:
        raise ListingError("Tonight's title or dated show link is missing; the site layout may have changed.")
    host_block = tonight.find(css_class="coast-linked-host")
    host = clean_text(host_block.find(css_class="linked-value")) if host_block else ""
    return Show(title, show_date, host, [], paragraphs(tonight.find(css_class="item-summary")), url)


def add_details(show, html):
    page = parse_html(html)
    content = page.find(css_class="component-coast-content-show")
    if content is None:
        raise ListingError("The show page did not contain the expected show details.")
    guests_block = page.find(css_class="linked-guest-value")
    if guests_block:
        show.guests = list(dict.fromkeys(
            name for link in guests_block.find_all(tag="a") if (name := clean_text(link))
        ))
    description = []
    for block in content.find_all(css_class="type-html"):
        description.extend(paragraphs(block))
    if description:
        show.description = description


def get_tonight(timeout=20.0):
    show = parse_tonight(fetch_html(HOME_URL, timeout))
    try:
        add_details(show, fetch_html(show.url, timeout))
    except (URLError, OSError, ValueError, ListingError) as exc:
        show.warnings.append(f"Show details unavailable; displaying the homepage listing: {exc}")
    return show


def display(show, color="auto", stream=None, error_stream=None):
    """Print a readable listing, with color only when requested/supported."""
    stream = stream or sys.stdout
    error_stream = error_stream or sys.stderr
    palette = Palette(color_enabled(color, stream))
    try:
        columns = shutil.get_terminal_size((88, 24)).columns
    except OSError:
        columns = 88
    width = max(40, min(100, columns))

    heading = "Coast to Coast AM"
    print(palette.bold(palette.cyan(heading)), file=stream)
    print(palette.dim("Tonight's official listing"), file=stream)
    print(palette.blue("─" * width), file=stream)

    def field(label, value):
        label_width = 8
        value = value or "Not listed"
        available = max(20, width - label_width - 3)
        lines = textwrap.wrap(
            str(value), width=available, break_long_words=False, break_on_hyphens=False
        ) or ["Not listed"]
        print(f"{palette.bold(label.ljust(label_width))}  {lines[0]}", file=stream)
        for line in lines[1:]:
            print(" " * (label_width + 2) + line, file=stream)

    field("Date", f"{show.date} (evening's broadcast date)")
    field("Topic", show.title)
    field("Host", show.host or "Not listed")
    field("Guests", ", ".join(show.guests) or "Not available")
    field("Airtime", show.airtime)

    print(file=stream)
    print(palette.bold(palette.cyan("Description")), file=stream)
    if show.description:
        for paragraph in show.description:
            half_match = HALF_LABEL.match(paragraph)
            if half_match:
                label = half_match.group(1)
                separator = half_match.group(2) or ""
                remainder = half_match.group(3)
                continuation_indent = " " * (3 + len(label) + len(separator))
                wrapped = textwrap.wrap(
                    remainder,
                    width=max(20, width - len(continuation_indent)),
                    break_long_words=False,
                    break_on_hyphens=False,
                )
                if wrapped:
                    print(
                        "  " + palette.bold(label) + separator + " " + wrapped[0],
                        file=stream,
                    )
                    for line in wrapped[1:]:
                        print(continuation_indent + line, file=stream)
                else:
                    print("  " + palette.bold(label) + separator, file=stream)
                print(file=stream)
                continue
            wrapped = textwrap.wrap(
                paragraph, width=max(20, width - 4), break_long_words=False, break_on_hyphens=False
            ) or [""]
            print("  " + wrapped[0], file=stream)
            for line in wrapped[1:]:
                print("  " + line, file=stream)
            print(file=stream)
    else:
        print("  " + palette.dim("No description available."), file=stream)

    print(palette.blue("─" * width), file=stream)
    print(palette.dim("Show page"), file=stream)
    print(palette.cyan(show.url), file=stream)
    for warning in show.warnings:
        print(palette.yellow(f"Warning: {warning}"), file=error_stream)


def positive_timeout(value):
    try:
        number = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("timeout must be a positive number") from None
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("timeout must be a finite positive number")
    return number


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="print structured JSON")
    parser.add_argument("--open", action="store_true", help="also open the show page in your browser")
    parser.add_argument("--color", choices=("auto", "always", "never"), default="auto",
                        help="colorize terminal output (default: auto; JSON is never colorized)")
    parser.add_argument("--timeout", type=positive_timeout, default=20.0,
                        metavar="SECONDS", help="network timeout per request (default: 20)")
    args = parser.parse_args(argv)
    try:
        show = get_tonight(args.timeout)
    except (URLError, OSError, ValueError, ListingError) as exc:
        print(f"Could not load tonight's show: {exc}\nCheck {HOME_URL}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(asdict(show), ensure_ascii=False, indent=2))
    else:
        display(show, color=args.color)
    if args.open and not webbrowser.open(show.url):
        print(f"Could not open a browser. Visit {show.url}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
