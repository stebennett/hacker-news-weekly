"""Concrete Himalaya v2 adapters. Only the explicit `send` action submits mail.

All other actions are read-only. Configure account/home/commands in a private JSON
file, not in this repository. Readback compares protected headers and decoded MIME
alternatives; transit headers and CRLF normalization do not falsify content parity.
"""
from __future__ import annotations

import argparse
from email import policy
from email.parser import BytesParser
import json
import os
from pathlib import Path
import subprocess
import sys

from .orchestrator import RuntimeError, digest, validate_message


def run(argv, settings, *, json_output=False):
    env = dict(os.environ)
    if settings.get('home'):
        env['HOME'] = settings['home']
    # Inherit the orchestration process group: its whole-stage deadline must kill
    # this adapter and all of its Himalaya children, not merely the Python leader.
    result = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True,
                            timeout=float(settings.get('timeout', 30)), env=env)
    if result.returncode:
        raise RuntimeError(f'Himalaya exited {result.returncode}: {result.stderr[:2048].decode(errors="replace")}')
    if len(result.stdout) > 20 * 1024 * 1024:
        raise RuntimeError('Himalaya readback exceeds 20 MiB')
    return json.loads(result.stdout) if json_output else result.stdout


def prefix(settings):
    command = [settings['binary']]
    if settings.get('account'):
        command += ['--account', settings['account']]
    if settings.get('config'):
        command += ['--config', settings['config']]
    return command


def message_content(raw, sender, recipient) -> dict:
    message = BytesParser(policy=policy.default).parsebytes(raw)
    validate_message(message, sender, recipient)
    if any(len(message.get_all(header, [])) != 1 for header in ('From','To','Subject','Message-ID')):
        raise RuntimeError('duplicate or missing protected header')
    if message.defects:
        raise RuntimeError('malformed MIME message')
    return dict(headers={key: str(message[key]) for key in ('From','To','Subject','Message-ID')},
                parts=[dict(type=part.get_content_type(), text=part.get_content().replace('\r\n','\n')) for part in message.iter_parts()])


def preflight(settings, context):
    base = prefix(settings)
    result = run(base + ['account','check','--json'], settings, json_output=True)
    # CLI versions may return either a structured backend report or a success
    # object. Refuse explicit negative reports; successful handshake alone is
    # preflight evidence, never evidence of a send.
    if not result or result is False or (isinstance(result, dict) and (result.get('error') or result.get('success') is False)):
        raise RuntimeError('account check did not confirm configured backends')
    rows = run(base + ['envelope','list','--mailbox',settings.get('sent_mailbox','Sent Items'),
                       '--page-size','1','--json'], settings, json_output=True)
    if not isinstance(rows, list):
        raise RuntimeError('Sent Items readback must return an envelope array')
    return dict(accountChecked=True,sentItemsChecked=True,sender=context['sender'],recipient=context['recipient'])


def send(settings, context):
    raw = Path(context['email']).read_bytes()
    if digest(raw) != context['messageSha256']:
        raise RuntimeError('submission file differs from durable intent')
    protected = message_content(raw,context['sender'],context['recipient'])
    if protected['headers']['Message-ID'] != context['messageId']:
        raise RuntimeError('submission Message-ID differs from durable intent')
    # The caller persists intent before launching us. No retries under any error.
    run(prefix(settings) + ['message','send','--save',settings.get('sent_mailbox','Sent Items'),
                            '--',context['email']],settings)
    return dict(submitted=True)  # Not interpreted as delivery verification.


def verify(settings, context):
    base = prefix(settings)
    mailbox = settings.get('sent_mailbox','Sent Items')
    expected_raw = Path(context['email']).read_bytes()
    if digest(expected_raw) != context['messageSha256']:
        raise RuntimeError('saved submission differs from durable intent')
    expected = message_content(expected_raw,context['sender'],context['recipient'])
    page_size = int(settings.get('verify_page_size',50))
    max_pages = int(settings.get('verify_max_pages',4))
    if not 1 <= page_size <= 1000 or not 1 <= max_pages <= 100:
        raise RuntimeError('invalid bounded Sent Items scan limits')
    seen = set()
    for page in range(1,max_pages+1):
        rows = run(base + ['envelope','list','--mailbox',mailbox,'--page',str(page),
                           '--page-size',str(page_size),'--json'],settings,json_output=True)
        if not isinstance(rows,list):
            raise RuntimeError('invalid Sent Items envelope result')
        for row in rows:
            ident = str(row.get('id',''))
            if not ident or ident in seen:
                continue
            seen.add(ident)
            raw = run(base + ['message','read','--mailbox',mailbox,'--raw',ident],settings)
            parsed = BytesParser(policy=policy.default).parsebytes(raw)
            if str(parsed.get('Message-ID','')) != context['messageId']:
                continue
            if message_content(raw,context['sender'],context['recipient']) != expected:
                raise RuntimeError('Sent Items Message-ID matched but content/recipient differed')
            return dict(verified=True,messageId=context['messageId'],messageSha256=context['messageSha256'],
                        recipient=context['recipient'],sentItemsId=ident,readbackSha256=digest(raw),
                        contentMatched=True,mailbox=mailbox)
        if len(rows) < page_size:
            break
    raise RuntimeError('exact sent copy not found in bounded scan; ambiguous, NEVER retry automatically')


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('preflight','send','verify','website-disabled'))
    parser.add_argument('--settings',type=Path)
    parser.add_argument('--context',type=Path,required=True)
    args=parser.parse_args(argv)
    try:
        if args.action == 'website-disabled':
            raise RuntimeError('website publication not activated: repo, reviewed PR and verified production deployment required; baseline email remains available')
        if args.settings is None:
            raise RuntimeError('private --settings file required')
        settings=json.loads(args.settings.read_bytes())
        context=json.loads(args.context.read_bytes())
        result={'preflight':preflight,'send':send,'verify':verify}[args.action](settings,context)
        print(json.dumps(result));return 0
    except Exception as exc:
        print(str(exc),file=sys.stderr);return 2


if __name__=='__main__':
    raise SystemExit(main())
