#!/usr/bin/env python3
"""Display the official Coast to Coast AM Tonight listing (Python 3.9+)."""

import argparse
from dataclasses import asdict, dataclass, field
from datetime import date
from html.parser import HTMLParser
import json
import math
import re
import shutil
import sys
import textwrap
from urllib.error import URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import Request, urlopen
import webbrowser


HOME_URL = "https://www.coasttocoastam.com/"
AIRTIME = "10 pm–2 am Pacific / 1 am–5 am Eastern (following morning)"
VOID_TAGS = set("area base br col embed hr img input link meta param source track wbr".split())
BLOCK_TAGS = set("p div section article li h1 h2 h3 h4 br".split())


class ListingError(Exception):
    """The site did not provide a usable listing."""


@dataclass
class Node:
    tag: str
    attrs: dict = field(default_factory=dict)
    children: list = field(default_factory=list)

    def find_all(self, *, css_class=None, tag=None):
        for child in self.children:
            if isinstance(child, Node):
                if (tag is None or child.tag == tag) and (
                    css_class is None or css_class in child.attrs.get("class", "").split()
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
    return " ".join(node.text().split()) if node else ""


def paragraphs(node):
    if node is None:
        return []
    return [" ".join(line.split()) for line in node.text().splitlines() if line.strip()]


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
        return body.decode(encoding, errors="replace")


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
            show_date = date.fromisoformat(match.group(1)).isoformat()
            url = candidate
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


def display(show):
    width = max(40, min(100, shutil.get_terminal_size((88, 24)).columns))
    heading = "Coast to Coast AM — Tonight"
    print(heading)
    print("=" * len(heading))
    print(f"Date:   {show.date} (evening's broadcast date)")
    print(f"Topic:  {show.title}")
    print(f"Host:   {show.host or 'Not listed'}")
    print(f"Guests: {', '.join(show.guests) or 'Not available'}")
    print(f"Airtime: {show.airtime}")
    for paragraph in show.description:
        print("\n" + textwrap.fill(paragraph, width=width))
    print(f"\nShow: {show.url}")
    for warning in show.warnings:
        print(f"Warning: {warning}", file=sys.stderr)


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
        display(show)
    if args.open and not webbrowser.open(show.url):
        print(f"Could not open a browser. Visit {show.url}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
