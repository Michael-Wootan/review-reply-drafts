"""
Review reply drafter.

Runs weekly. For each Trustpilot review of the company that has no company
reply yet, asks Claude to draft one using the tone guidance in
config/tone_guidance.md, then emails the whole batch so a person can review
each draft and post it.

Nothing is posted automatically. The output is drafts.

Reviews are read from the public review pages with a headless browser. This
is unofficial: if the page markup changes or the request is blocked, the run
fails loudly so it can be fixed. Check the review platform's terms before
running it; their API is the supported route.

Set DRY_RUN=true to print drafts to the log instead of emailing.

Required env vars:
  ANTHROPIC_API_KEY
  REVIEW_DOMAIN                  (the company's domain on the review site)
  SMTP_HOST, SMTP_PORT           (e.g. smtp.gmail.com, 587)
  SMTP_USERNAME, SMTP_PASSWORD   (sending mailbox and app password)
  EMAIL_TO                       (where the drafts land)
Optional:
  COMPANY_NAME, REPLY_SIGNATURE, CLAUDE_MODEL, CONFIG_DIR, DRY_RUN
"""

from __future__ import annotations

import html
import json
import os
import smtplib
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Iterator

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = Path(os.environ.get("CONFIG_DIR", ROOT / "config"))

REVIEW_PAGE = "https://www.trustpilot.com/review/{domain}"
BUSINESS_REVIEW_URL = "https://businessapp.b2b.trustpilot.com/reviews"
ANTHROPIC_MODEL = os.environ.get("CLAUDE_MODEL") or "claude-sonnet-4-5"
COMPANY_NAME = os.environ.get("COMPANY_NAME") or "Acme Supply Co."
# Last line of every reply. The tone guidance should name the same signature.
REPLY_SIGNATURE = os.environ.get("REPLY_SIGNATURE") or f"-{COMPANY_NAME} Team"


# --- Trustpilot public page reader -----------------------------------------


@dataclass
class Review:
    review_id: str
    stars: int
    title: str
    text: str
    consumer_name: str
    created_at: str  # ISO-8601

    @property
    def stars_glyph(self) -> str:
        return "★" * self.stars + "☆" * (5 - self.stars)

    @property
    def created_display(self) -> str:
        try:
            dt = datetime.fromisoformat(self.created_at.replace("Z", "+00:00"))
            return dt.strftime("%b %d, %Y")
        except (ValueError, AttributeError):
            return self.created_at or ""

    @property
    def business_url(self) -> str:
        return f"{BUSINESS_REVIEW_URL}/{self.review_id}"


def reviews_from_page_props(page_props: dict, seen: set[str]) -> list[Review]:
    """Unreplied reviews on one page. A review carries a `reply` object once the
    company has responded (null otherwise), which is how unreplied ones are found.
    `seen` is shared across pages so a review that shifts between pages while
    paging is not drafted twice."""
    out: list[Review] = []
    for r in page_props.get("reviews") or []:
        if r.get("reply"):
            continue
        if r["id"] in seen:
            continue
        seen.add(r["id"])
        consumer = r.get("consumer") or {}
        dates = r.get("dates") or {}
        out.append(Review(
            review_id=r["id"],
            stars=int(r.get("rating", 0)),
            title=r.get("title", "") or "",
            text=r.get("text", "") or "",
            consumer_name=(consumer.get("displayName") or "").strip(),
            created_at=dates.get("publishedDate", "") or "",
        ))
    return out


def total_pages(page_props: dict) -> int:
    pagination = (page_props.get("filters") or {}).get("pagination") or {}
    return int(pagination.get("totalPages") or 1)


class ReviewPageReader:
    """Reads reviews from the public review pages in a headless browser
    (Playwright). The pages are rendered behind a JavaScript check, so a plain
    HTTP client does not receive the review data.

    Each page embeds a __NEXT_DATA__ JSON blob containing the reviews shown
    on that page.
    """

    GET_NEXT_DATA_JS = (
        "() => { const el = document.getElementById('__NEXT_DATA__');"
        " return el ? el.textContent : null; }"
    )

    def __init__(self, domain: str) -> None:
        self.domain = domain

    def _read_next_data(self, browser_page, url: str) -> dict:
        browser_page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        # The page navigates once more after the initial load; poll until
        # __NEXT_DATA__ shows up. An evaluate() that lands in the middle of
        # that navigation raises "Execution context was destroyed" (this
        # killed a production run); it only means "not yet", so wait and
        # poll again instead of crashing.
        for _ in range(45):
            try:
                raw = browser_page.evaluate(self.GET_NEXT_DATA_JS)
            except self._playwright_error as exc:
                if "Execution context was destroyed" not in str(exc):
                    raise
                raw = None
            if raw:
                return json.loads(raw)["props"]["pageProps"]
            browser_page.wait_for_timeout(1000)
        raise RuntimeError(
            f"__NEXT_DATA__ never appeared for {url}; the request was "
            "blocked or the page markup changed."
        )

    def _read_next_data_with_retry(
        self, browser_page, url: str, attempts: int = 2
    ) -> dict:
        """Reload once if a page read fails: one bad page load should not
        lose the week."""
        last_exc: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                return self._read_next_data(browser_page, url)
            except (RuntimeError, self._playwright_error) as exc:
                last_exc = exc
                print(f"[warn] read attempt {attempt}/{attempts} failed for {url}: {exc}")
        raise RuntimeError(
            f"could not read {url} after {attempts} attempts"
        ) from last_exc

    def iter_unreplied_reviews(self) -> Iterator[Review]:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright

        self._playwright_error = PlaywrightError
        base = REVIEW_PAGE.format(domain=self.domain)
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            context = browser.new_context(
                viewport={"width": 1280, "height": 900},
                locale="en-US",
            )
            browser_page = context.new_page()
            try:
                page = 1
                pages = 1
                seen: set[str] = set()
                while page <= pages:
                    url = base if page == 1 else f"{base}?page={page}"
                    page_props = self._read_next_data_with_retry(browser_page, url)
                    pages = total_pages(page_props)
                    yield from reviews_from_page_props(page_props, seen)
                    page += 1
            finally:
                browser.close()


# --- Claude reply drafter ---------------------------------------------------


def load_tone_guidance() -> str:
    for name in ("tone_guidance.md", "tone_guidance.example.md"):
        path = CONFIG_DIR / name
        if path.exists():
            return path.read_text(encoding="utf-8")
    raise FileNotFoundError("Missing config/tone_guidance.md")


def build_user_message(review: Review) -> str:
    return (
        f"Review by {review.consumer_name or 'anonymous'} "
        f"({review.stars} star{'s' if review.stars != 1 else ''}):\n"
        f"Title: {review.title}\n"
        f"Body:\n{review.text}\n\n"
        "Write the reply now."
    )


def finalize_reply(text: str) -> str:
    """House rules enforced in code, whatever the model returned: no em
    dashes, and the signature on the last line."""
    text = text.strip().replace("\u2014", ", ")
    if not text.rstrip().endswith(REPLY_SIGNATURE):
        text = text.rstrip() + "\n" + REPLY_SIGNATURE
    return text


def draft_reply(client, review: Review, tone: str) -> str:
    resp = client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=400,
        system=tone,
        messages=[{"role": "user", "content": build_user_message(review)}],
    )
    text = "".join(
        block.text for block in resp.content if getattr(block, "type", "") == "text"
    )
    return finalize_reply(text)


# --- Email rendering --------------------------------------------------------


def render_email_html(drafts: list[tuple[Review, str]]) -> str:
    """Build the HTML body with each review as a copy-paste-ready card."""
    if not drafts:
        return (
            "<p style='font-family:system-ui,sans-serif;color:#444'>"
            "No unreplied reviews right now.</p>"
        )

    cards = []
    for review, reply in drafts:
        reply_html = html.escape(reply).replace("\n", "<br>")
        review_body_html = html.escape(review.text).replace("\n", "<br>")
        cards.append(f"""
<div style="border:1px solid #e3e3e3;border-radius:8px;padding:18px 20px;margin:14px 0;background:#fff;">
  <div style="font-size:13px;color:#666;margin-bottom:6px;">
    <span style="color:#00b67a;font-size:16px;letter-spacing:1px;">{review.stars_glyph}</span>
    &nbsp;by <strong>{html.escape(review.consumer_name or "anonymous")}</strong>
    &nbsp;·&nbsp; {html.escape(review.created_display)}
  </div>
  <div style="font-weight:600;font-size:15px;color:#111;margin-bottom:6px;">
    {html.escape(review.title)}
  </div>
  <div style="font-size:14px;color:#333;line-height:1.5;margin-bottom:14px;">
    {review_body_html}
  </div>
  <div style="margin-bottom:12px;">
    <a href="{html.escape(review.business_url)}"
       style="display:inline-block;background:#1f2937;color:#fff;text-decoration:none;
              padding:8px 14px;border-radius:6px;font-size:13px;font-weight:500;">
      Open review &rarr;
    </a>
  </div>
  <div style="font-size:12px;color:#666;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:6px;">
    Draft reply (copy / paste)
  </div>
  <pre style="background:#f6f8fa;border:1px solid #e3e3e3;border-radius:6px;
              padding:12px 14px;font-family:ui-monospace,Menlo,monospace;
              font-size:13px;line-height:1.5;color:#111;white-space:pre-wrap;
              margin:0;">{reply_html}</pre>
</div>
""".strip())

    by_stars: dict[int, int] = {}
    for review, _ in drafts:
        by_stars[review.stars] = by_stars.get(review.stars, 0) + 1
    breakdown = ", ".join(
        f"{stars}★ × {count}" for stars, count in sorted(by_stars.items(), reverse=True)
    )

    return f"""
<div style="font-family:system-ui,-apple-system,Segoe UI,sans-serif;
            max-width:720px;margin:0 auto;padding:24px;color:#111;background:#fafafa;">
  <h2 style="margin:0 0 6px;font-size:20px;">Review reply drafts ready</h2>
  <p style="margin:0 0 18px;color:#555;font-size:14px;">
    {len(drafts)} unreplied review{'s' if len(drafts) != 1 else ''} for {html.escape(COMPANY_NAME)}.
    Breakdown: {breakdown}.
  </p>
  {''.join(cards)}
  <p style="color:#888;font-size:12px;margin-top:18px;">
    Generated automatically. Edit replies before posting if anything reads off.
  </p>
</div>
""".strip()


def render_email_plain(drafts: list[tuple[Review, str]]) -> str:
    if not drafts:
        return "No unreplied reviews right now."
    lines = [f"{len(drafts)} unreplied review(s) for {COMPANY_NAME}.\n"]
    for i, (review, reply) in enumerate(drafts, start=1):
        lines.append(f"\n--- {i}. {review.stars}★ by {review.consumer_name or 'anonymous'} ({review.created_display}) ---")
        lines.append(f"Title: {review.title}")
        lines.append(f"Review: {review.text}")
        lines.append(f"Open review: {review.business_url}")
        lines.append("\nDraft reply:")
        lines.append(reply)
    return "\n".join(lines)


# --- SMTP send --------------------------------------------------------------


def send_email(
    *,
    host: str,
    port: int,
    username: str,
    password: str,
    to: str,
    subject: str,
    html_body: str,
    plain_body: str,
) -> None:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = username
    msg["To"] = to
    msg.set_content(plain_body)
    msg.add_alternative(html_body, subtype="html")

    with smtplib.SMTP(host, port, timeout=30) as server:
        server.starttls()
        server.login(username, password)
        server.send_message(msg)


# --- Main -------------------------------------------------------------------


REQUIRED_ENV = [
    "ANTHROPIC_API_KEY",
    "REVIEW_DOMAIN",
    "SMTP_HOST",
    "SMTP_PORT",
    "SMTP_USERNAME",
    "SMTP_PASSWORD",
    "EMAIL_TO",
]


def check_env() -> None:
    """Report ALL missing env vars at once, then exit."""
    missing = [name for name in REQUIRED_ENV if not os.environ.get(name)]
    if missing:
        print(
            f"[fatal] missing env var(s): {', '.join(missing)}", file=sys.stderr
        )
        sys.exit(2)


def main() -> int:
    dry_run = os.environ.get("DRY_RUN", "false").lower() == "true"
    check_env()
    domain = os.environ["REVIEW_DOMAIN"]

    import anthropic

    tp = ReviewPageReader(domain)
    claude = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    tone = load_tone_guidance()

    print(f"[info] Dry run: {dry_run}")
    print(f"[info] Fetching unreplied reviews for {domain}...")

    drafts: list[tuple[Review, str]] = []
    failed: list[tuple[Review, str]] = []

    for review in tp.iter_unreplied_reviews():
        try:
            reply = draft_reply(claude, review, tone)
            drafts.append((review, reply))
            print(
                f"[ok] drafted for {review.stars}★ by "
                f"{review.consumer_name or 'anonymous'}"
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] draft failed for {review.review_id}: {exc}")
            failed.append((review, f"draft error: {exc}"))

    print(f"\n[info] {len(drafts)} draft(s) ready, {len(failed)} failure(s).")

    if dry_run:
        print("\n[info] DRY_RUN=true, not emailing. Drafts below.\n")
        print(render_email_plain(drafts))
        return 1 if failed else 0

    # Always email, even with zero unreplied reviews, so that a quiet week
    # is distinguishable from a broken automation.
    timestamp = datetime.now(timezone.utc).strftime("%b %d, %Y")
    subject = (
        f"[{COMPANY_NAME}] Review reply drafts ({len(drafts)} review"
        f"{'s' if len(drafts) != 1 else ''}), {timestamp}"
    )
    html_body = render_email_html(drafts)
    plain_body = render_email_plain(drafts)

    try:
        send_email(
            host=os.environ["SMTP_HOST"],
            port=int(os.environ["SMTP_PORT"]),
            username=os.environ["SMTP_USERNAME"],
            password=os.environ["SMTP_PASSWORD"],
            to=os.environ["EMAIL_TO"],
            subject=subject,
            html_body=html_body,
            plain_body=plain_body,
        )
        print(f"[info] emailed {len(drafts)} draft(s) to {os.environ['EMAIL_TO']}.")
    except Exception as exc:  # noqa: BLE001
        print(f"[fatal] email send failed: {exc}", file=sys.stderr)
        return 1

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
