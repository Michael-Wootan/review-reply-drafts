#!/usr/bin/env python3
"""Offline tests for review parsing, reply finishing and the email. No browser,
network or API keys needed."""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["COMPANY_NAME"] = "Acme Supply Co."
os.environ.pop("REPLY_SIGNATURE", None)
sys.path.insert(0, os.path.join(ROOT, "src"))
import reply_to_reviews as r  # noqa: E402

checks = 0


def ok(cond, name):
    global checks
    assert cond, name
    checks += 1
    print(f"  ok  {name}")


page1 = {
    "filters": {"pagination": {"totalPages": 3}},
    "reviews": [
        {"id": "a1", "rating": 5, "title": "Great beans", "text": "Loved the Ethiopia.",
         "consumer": {"displayName": " Dana "}, "dates": {"publishedDate": "2026-07-31T10:00:00.000Z"}, "reply": None},
        {"id": "a2", "rating": 2, "title": "Late", "text": "Took two weeks.",
         "consumer": {"displayName": "Sam"}, "dates": {"publishedDate": "2026-07-30T10:00:00.000Z"},
         "reply": {"message": "Thanks Sam"}},
        {"id": "a3", "rating": 4, "title": None, "text": None, "consumer": None, "dates": None, "reply": None},
    ],
}
seen = set()
found = r.reviews_from_page_props(page1, seen)
ok([x.review_id for x in found] == ["a1", "a3"], "reviews that already have a company reply are skipped")
ok(found[0].consumer_name == "Dana" and found[0].stars == 5, "reviewer name is trimmed and the rating is read")
ok(found[1].title == "" and found[1].text == "" and found[1].consumer_name == "", "missing fields become empty, not errors")
ok(r.reviews_from_page_props(page1, seen) == [], "a review seen on an earlier page is not drafted twice")
ok(r.total_pages(page1) == 3 and r.total_pages({}) == 1, "page count comes from pagination, defaulting to one")

d = found[0]
ok(d.stars_glyph == "★★★★★" and found[1].stars_glyph == "★★★★☆", "stars render as filled and empty glyphs")
ok(d.created_display == "Jul 31, 2026", "the review date is shown in a readable form")
ok(d.business_url.endswith("/reviews/a1"), "each draft links to its review in the business app")

sig = r.REPLY_SIGNATURE
ok(sig == "-Acme Supply Co. Team", "the signature defaults to the company name")
ok(r.finalize_reply("Thank you, Dana.") == "Thank you, Dana.\n" + sig, "a missing signature is added on its own line")
ok(r.finalize_reply("Thank you, Dana.\n" + sig + "\n") == "Thank you, Dana.\n" + sig, "an existing signature is not doubled")
DASH = chr(8212)
ok(DASH not in r.finalize_reply(f"Glad it landed {DASH} see you soon."), "em dashes are removed from every reply")

msg = r.build_user_message(d)
ok("Review by Dana (5 stars)" in msg and "Loved the Ethiopia." in msg, "the request carries reviewer, rating and text")
one = r.Review("b1", 1, "Bad", "Wrong item.", "", "")
ok("Review by anonymous (1 star)" in r.build_user_message(one), "one star is singular and a missing name is anonymous")

drafts = [(d, "Thank you, Dana.\n" + sig), (one, "Hi, thanks for telling us.\n" + sig)]
html_body = r.render_email_html(drafts)
ok("2 unreplied reviews" in html_body and "5★ × 1, 1★ × 1" in html_body, "the email summarizes count and star breakdown")
evil = r.Review("c1", 3, "<script>x</script>", "a <b>b</b>", "<i>n</i>", "")
ok("<script>" not in r.render_email_html([(evil, "ok\n" + sig)]), "review text is escaped in the HTML email")
ok("No unreplied reviews" in r.render_email_html([]) and "No unreplied reviews" in r.render_email_plain([]),
   "a quiet week still produces an email body")
plain = r.render_email_plain(drafts)
ok("Draft reply:" in plain and "Open review:" in plain, "the plain-text part carries every draft and link")

tone = r.load_tone_guidance()
ok(sig in tone, "the tone guidance and the code agree on the signature")

print(f"\n{checks} checks passed")
