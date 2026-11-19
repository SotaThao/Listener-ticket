#!/usr/bin/env python3
"""Measure cache reuse on two identical decide requests per configured link."""

import argparse
from functools import partial
from importlib import import_module
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
p8 = import_module('eval-assistant-p8')

from tg_triage import assistant, router_transport
from tg_triage.assistant_contract import CONTRACT, STATES
from tg_triage.__main__ import _load_assistant_pack


def _links(router_url, key, settings, chat):
    defaults = settings.assistant if settings else p8.AssistantConfig()
    router = partial(chat, router_url, key)
    yield 'gemini', defaults.models[0], router, defaults.gemini_timeout_s
    yield 'gpt', defaults.models[1], router, defaults.gpt_timeout_s
    if settings and settings.model.provider == 'deepseek' and settings.model.cloud_consent:
        from agentfleet_common.ai_transport import read_key

        model = settings.model
        key = read_key(model.key_file, model.provider + '|' + model.base_url, group_name='tglistener')
        if not key:
            raise ValueError('DeepSeek credential unavailable')
        hosted = partial(router_transport.deepseek_chat, model.base_url, key, max_output=model.max_output)
        yield 'deepseek', model.name, hosted, defaults.deepseek_timeout_s


def _probe_metrics(reply):
    state = 'NO_DECISION'
    template = None
    try:
        envelope = json.loads(assistant.router_proposal_content(reply.content))
        group, = envelope['groups']
        if envelope['contract'] == CONTRACT and group['state'] in STATES:
            state, template = group['state'], group.get('template')
    except (KeyError, TypeError, ValueError):
        pass
    return dict(tokens_in=reply.prompt_tokens, tokens_out=reply.completion_tokens,
                cached_tokens=reply.cached_tokens, latency_ms=reply.latency_ms,
                actual_state=state, expected_state='CHAT', creation_agrees=state not in ('NO_DECISION', 'READY'),
                template_agrees=state != 'NO_DECISION' and template is None)


def run_probe(*, router_url, key_file, config=None, chat=None, clock=time.monotonic,
              pack_mode='full', metrics_out=None):
    router_transport.validate_base_url(router_url)
    path = Path(key_file)
    if not path.is_absolute():
        raise ValueError('Key file path must be absolute')
    key = p8._assistant_token(str(path))
    if not key:
        raise ValueError('Router key unavailable')
    settings = p8.offline_config(config) if config else None
    pack = _load_assistant_pack(pack_mode=pack_mode)
    payload = {'sources': [{'message_id': 1, 'sender': 'probe', 'text': 'Chào mọi người',
                            'reply_to_id': None, 'has_image': False, 'pixels_available': False}]}
    messages = [
        {'role': 'system', 'content': assistant.decide_system(pack.text, core_pack=pack.core)},
        {'role': 'user', 'content': '\nUNTRUSTED BATCH DATA:\n' + json.dumps(payload, ensure_ascii=False)},
    ]
    tools = assistant._tools()
    failed = False
    rows = []
    print(f'{pack_mode} prefix: {assistant.decide_prefix_hash(pack.text, core_pack=pack.core)}')
    print('Link     Call  prompt_tokens  output_tokens  cached_tokens  latency_ms')
    for link, model, send, timeout in _links(router_url, key, settings, chat or router_transport.chat):
        deadline = clock() + 60
        for call in (1, 2):
            remaining = deadline - clock()
            if remaining <= 0:
                print(f'{link:<8} {call:>4}  cache window elapsed')
                failed = True
                break
            try:
                reply = send(model, messages, tools=tools, tool_choice='none',
                             timeout=min(remaining, timeout))
            except Exception:
                # Transport and injected errors may include a credential.
                print(f'{link:<8} {call:>4}  request failed')
                rows.append(dict(expected_state='CHAT', actual_state='NO_DECISION',
                                 creation_agrees=False, template_agrees=False))
                failed = True
            else:
                rows.append(_probe_metrics(reply))
                print(f'{link:<8} {call:>4}  {reply.prompt_tokens:>13d}  '
                      f'{reply.completion_tokens:>13d}  {reply.cached_tokens:>13d}  {reply.latency_ms:>10d}')
    if metrics_out is None:
        print('Fixed synthetic CHAT probe. Tool calls are disabled.')
        p8.print_pack_comparison({pack_mode: rows})
    else:
        metrics_out.extend(rows)
    return 1 if failed else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--router-url', required=True)
    parser.add_argument('--key-file', required=True)
    parser.add_argument('--config', help='Use configured model links, including hosted DeepSeek when enabled')
    parser.add_argument('--pack-mode', choices=('full', 'core'), default='full')
    parser.add_argument('--compare', action='store_true', help='Probe both prefixes per configured link')
    args = parser.parse_args(argv)
    try:
        options = vars(args)
        if not options.pop('compare'):
            return run_probe(**options)
        options.pop('pack_mode')
        rows = {'full': [], 'core': []}
        statuses = [run_probe(**options, pack_mode=mode, metrics_out=results) for mode, results in rows.items()]
        print('Fixed synthetic CHAT probe. Tool calls are disabled.')
        p8.print_pack_comparison(rows)
        return max(statuses)
    except Exception as error:
        print('Cache probe failed: ' + type(error).__name__, file=sys.stderr)
    return 2


if __name__ == '__main__':
    raise SystemExit(main())
