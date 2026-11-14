#!/usr/bin/env python3
"""Build a safe HN-inspired multipart email from fully reviewed entries.

Input JSON: {start, end, sections: [{heading, items: [{id,title,score,
comments,created,url,story,discussion}]}]}. No network access or sending.
"""
from __future__ import annotations

import argparse
from datetime import datetime
from email.message import EmailMessage
from html import escape
import json
import os
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

UK = ZoneInfo('Europe/London')
HEADINGS = [
    'Top 10 stories by votes', 'Top 10 discussed stories',
    'Top 10 Ask HN threads by comments', 'All popular Show HN threads (50+ points)',
]
ORANGE = '#ff6600'


def safe_url(raw: str) -> str:
    """Only allow conventional web links in href attributes."""
    parsed = urlsplit(raw)
    if parsed.scheme.lower() not in {'http', 'https'} or not parsed.netloc or any(c.isspace() for c in raw):
        raise ValueError(f'unsafe URL: {raw!r}')
    return escape(raw, quote=True)


def validate(data: dict) -> list[dict]:
    groups = data['sections']
    if [g['heading'] for g in groups] != HEADINGS:
        raise ValueError('missing or reordered digest sections')
    items = [item for group in groups for item in group['items']]
    if [len(g['items']) for g in groups[:3]] != [10, 10, 10]:
        raise ValueError('first three sections must contain ten items each')
    ids = [str(item['id']) for item in items]
    if any(not x.isdecimal() for x in ids):
        raise ValueError('invalid HN ID')
    seen = {}
    for group in groups:
        section_ids = [str(item['id']) for item in group['items']]
        if len(section_ids) != len(set(section_ids)):
            raise ValueError('duplicate HN ID within a section')
        for item in group['items']:
            ident = str(item['id'])
            if ident in seen and item != seen[ident]:
                raise ValueError('inconsistent repeated HN item')
            seen[ident] = item
    ordinary_ids = {str(item['id']) for group in groups[:2] for item in group['items']}
    ask_ids = {str(item['id']) for item in groups[2]['items']}
    show_ids = {str(item['id']) for item in groups[3]['items']}
    if ordinary_ids & (ask_ids | show_ids) or ask_ids & show_ids:
        raise ValueError('Ask/Show IDs overlap other categories')
    for item in items:
        for key in ('id', 'title', 'score', 'comments', 'created', 'story', 'discussion'):
            if key not in item or item[key] is None or str(item[key]).strip() == '':
                raise ValueError(f'missing {key} in item {item.get("id")}')
        if '\n' in item['story'] or '\n' in item['discussion']:
            raise ValueError(f'summary must be a single paragraph: {item["id"]}')
        if item.get('url'):
            safe_url(item['url'])
        datetime.fromisoformat(item['created']).astimezone(UK)
    if any(int(item['score']) < 50 for item in groups[3]['items']):
        raise ValueError('Show HN section includes below-threshold story')
    return items


def build(data: dict, *, recipient: str | None = None, sender: str | None = None, test: bool = False, archive_url: str | None = None, message_id: str | None = None) -> EmailMessage:
    recipient = recipient or os.environ.get('TWIHN_RECIPIENT')
    sender = sender or os.environ.get('TWIHN_SENDER')
    if not recipient or not sender:
        raise ValueError('recipient and sender must be configured privately')
    for address in (recipient, sender):
        if any(c in address for c in '\r\n') or '@' not in address:
            raise ValueError('invalid email address')
    items = validate(data)
    start, end = (datetime.fromisoformat(data[key]).astimezone(UK) for key in ('start', 'end'))
    subject = f'Hacker News weekly roundup — {start.date().isoformat()} to {end.date().isoformat()}'
    if test:
        subject += ' [HTML test]'
    window = f"{start.strftime('%d %b %Y, %H:%M %Z')} – {end.strftime('%d %b %Y, %H:%M %Z')}"
    note = 'Ranked independently by HN score and comments, so a story can appear in both rankings. All Show HN threads with 50+ points are included. Discussion themes are drawn from visible threads, not comment vote counts.'
    plain = [subject, '=' * len(subject), '', f'Window: {window} (Europe/London)', note, '']
    html_parts = [
        '<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">',
        f'<title>{escape(subject)}</title></head>',
        '<body style="margin:0;padding:0;background:#f6f6ef;color:#242424;font-family:Arial,Helvetica,sans-serif;">',
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="border-collapse:collapse;background:#f6f6ef;"><tr><td align="center" style="padding:18px 10px 36px;">',
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="max-width:640px;border-collapse:collapse;background:#ffffff;">',
        f'<tr><td style="background:{ORANGE};padding:14px 24px;color:#fff;font-size:14px;font-weight:bold;letter-spacing:.06em;">Y&nbsp; &nbsp; HACKER NEWS / WEEKLY</td></tr>',
        '<tr><td style="padding:28px 24px 14px;">',
        '<h1 style="margin:0 0 8px;font-size:26px;line-height:1.25;font-weight:bold;color:#222;">The weekly roundup</h1>',
        f'<p style="margin:0 0 12px;font-size:14px;line-height:1.5;color:#666;">{escape(window)} &middot; Europe/London</p>',
        f'<p style="margin:0;font-size:13px;line-height:1.6;color:#555;">{escape(note)}</p>',
        '</td></tr>',
    ]
    if archive_url:
        expected = 'https://twihn.com/issues/' + end.date().isoformat() + '/'
        if archive_url != expected:
            raise ValueError('archive URL must match production issue permalink')
        plain.extend(['Read on the web: ' + archive_url, ''])
        html_parts.append(f'<tr><td style="padding:8px 24px;"><a href="{safe_url(archive_url)}">Read on the web</a></td></tr>')
    for group in data['sections']:
        heading = group['heading']
        plain.extend([f'## {heading}', ''])
        html_parts.extend([
            '<tr><td style="padding:24px 24px 0;">',
            f'<h2 style="margin:0;padding:0 0 10px;border-bottom:2px solid {ORANGE};font-size:18px;line-height:1.35;color:#d45100;">{escape(heading)}</h2>',
            '</td></tr>',
        ])
        if not group['items']:
            plain.extend(['None met the 50-point threshold this week.', ''])
            html_parts.append('<tr><td style="padding:16px 24px;color:#666;font-size:14px;">None met the 50-point threshold this week.</td></tr>')
        for number, item in enumerate(group['items'], 1):
            ident = str(item['id'])
            hn_url = f'https://news.ycombinator.com/item?id={ident}'
            source = item.get('url') or ''
            title_url = safe_url(source or hn_url)
            stamp = datetime.fromisoformat(item['created']).astimezone(UK).strftime('%d %b, %H:%M %Z')
            plain.extend([
                f'{number}. {item["title"]} — {item["score"]} points · {item["comments"]} comments · {stamp}',
                *([f'Source: {source}'] if source else []), f'HN discussion: {hn_url}',
                f'Story: {item["story"]}', f'Discussion: {item["discussion"]}', '',
            ])
            html_parts.extend([
                '<tr><td style="padding:0 24px;">',
                '<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="border-collapse:collapse;border-bottom:1px solid #e8e8e0;"><tr>',
                f'<td style="width:28px;padding:18px 8px 18px 0;vertical-align:top;color:#e05b00;font-size:14px;font-weight:bold;">{number}.</td>',
                '<td style="padding:16px 0 18px;vertical-align:top;">',
                f'<a href="{title_url}" style="color:#222;text-decoration:none;font-size:17px;font-weight:bold;line-height:1.4;">{escape(item["title"])}</a>',
                f'<p style="margin:6px 0 12px;color:#767676;font-size:12px;line-height:1.5;">{int(item["score"]):,} points &nbsp;&middot;&nbsp; {int(item["comments"]):,} comments &nbsp;&middot;&nbsp; {escape(stamp)} &nbsp;&middot;&nbsp; <a href="{safe_url(hn_url)}" style="color:#cc5200;text-decoration:underline;">HN discussion</a></p>',
                f'<p style="margin:0 0 10px;font-size:14px;line-height:1.6;color:#333;"><strong style="color:#b64800;">Story</strong> &mdash; {escape(item["story"])}</p>',
                f'<p style="margin:0;font-size:14px;line-height:1.6;color:#333;"><strong style="color:#b64800;">Discussion</strong> &mdash; {escape(item["discussion"])}</p>',
                '</td></tr></table></td></tr>',
            ])
    footer = f"Data: Hacker News Search API, collected {end.strftime('%Y-%m-%d %H:%M %Z')} (Europe/London)."
    plain.append(footer)
    html_parts.extend([
        f'<tr><td style="padding:24px;color:#777;font-size:12px;line-height:1.6;background:#f9f9f5;">{escape(footer)}<br>Every item links to its HN discussion and, where provided, the source page.</td></tr>',
        '</table></td></tr></table></body></html>',
    ])
    message = EmailMessage()
    message['From'] = sender
    message['To'] = recipient
    message['Subject'] = subject
    if message_id:
        if '\n' in message_id or '\r' in message_id or not message_id.startswith('<') or not message_id.endswith('>'):
            raise ValueError('invalid Message-ID')
        message['Message-ID'] = message_id
    message.set_content('\n'.join(plain) + '\n')
    message.add_alternative(''.join(html_parts), subtype='html')
    if message.get_content_type() != 'multipart/alternative':
        raise AssertionError('multipart/alternative assembly failed')
    if len(items) != sum(len(g['items']) for g in data['sections']):
        raise AssertionError('item count mismatch')
    return message


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('input_json', type=Path)
    parser.add_argument('output_eml', type=Path)
    parser.add_argument('--test', action='store_true')
    parser.add_argument('--recipient')
    parser.add_argument('--sender')
    parser.add_argument('--archive-url')
    parser.add_argument('--message-id')
    args = parser.parse_args()
    data = json.loads(args.input_json.read_text())
    message = build(data, test=args.test, recipient=args.recipient, sender=args.sender, archive_url=args.archive_url, message_id=args.message_id)
    args.output_eml.write_bytes(message.as_bytes())
    print(f'Built {args.output_eml}: {sum(len(g["items"]) for g in data["sections"])} items; multipart/alternative')


if __name__ == '__main__':
    main()
