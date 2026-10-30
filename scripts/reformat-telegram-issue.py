"""Preview or PATCH an existing ticket using the daemon's canonical renderer.

Input is a reviewed JSON source export, not arbitrary Markdown. This command
never opens Telegram/SQLite, creates an issue, or changes source associations.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
from datetime import datetime

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'listener' / 'src'))
from tg_triage.issue_format import format_issue
from tg_triage.models import Classification, IncomingMessage
from tg_triage.ticket_templates import active


def render(spec):
    """Only reviewed fields enter format_issue; missing details still use the type template."""
    sources = spec['sources']
    if not isinstance(sources, list) or not sources:
        raise ValueError('At least one reviewed source is required')
    messages = []
    for source in sources:
        values = dict(source)
        values['received_at'] = datetime.fromisoformat(values['received_at'])
        if values['received_at'].tzinfo is None:
            raise ValueError('Source timestamp must include timezone')
        messages.append(IncomingMessage(**values))
    messages.sort(key=lambda m: (m.received_at, m.message_id))
    if len({m.chat_id for m in messages}) != 1 or len({m.message_id for m in messages}) != len(messages):
        raise ValueError('Sources must be distinct messages in one chat')
    category = spec['category']
    if category not in ('bug', 'task', 'feedback'):
        raise ValueError('A reviewed ticket category is required')
    # Confidence is required by the existing record validator; manual rendering
    # explicitly omits model/confidence claims from provenance.
    classification = Classification(category, 1.0, spec['title'], spec['summary'], 'medium')
    return format_issue(messages[0], classification, 'manual', False,
                        case_messages=messages, images=spec.get('images', ()),
                        documents=spec.get('documents', ()), details=spec.get('details') or {})


def gh(arguments, payload=None):
    result = subprocess.run(['gh', 'api', *arguments],
                            input=None if payload is None else json.dumps(payload, ensure_ascii=False).encode('utf-8'),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if result.returncode:
        raise RuntimeError('GitHub request failed; read back the issue before retrying')
    return json.loads(result.stdout)


def update_existing(repo, number, issue, expected_sha, request=gh):
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo) or type(number) is not int or number <= 0:
        raise ValueError('Invalid existing issue target')
    if not re.fullmatch(r'[0-9a-f]{64}', expected_sha or ''):
        raise ValueError('Expected current body SHA-256 is required')
    endpoint = f'repos/{repo}/issues/{number}'
    before = request([endpoint, '--method', 'GET'])
    marker = issue.body.split('\n', 1)[0]
    if (before.get('number') != number or 'pull_request' in before
            or not before.get('body', '').startswith(marker + '\n')):
        raise ValueError('Existing issue source marker mismatch')
    if hashlib.sha256(before['body'].encode('utf-8')).hexdigest() != expected_sha:
        raise ValueError('Issue changed since preview; review again')
    # Preserve unrelated user labels while replacing only the triage type/status labels.
    owned = {'bug', 'task', 'feature', 'question', 'feedback', 'needs-human', 'triage', 'source:telegram'}
    labels = [item['name'] for item in before['labels'] if item['name'] not in owned]
    labels.extend(issue.labels)
    payload = dict(title=issue.title, body=issue.body, labels=labels)
    if any(before[key] != payload[key] for key in ('title', 'body')) or set(labels) != {x['name'] for x in before['labels']}:
        request([endpoint, '--method', 'PATCH', '--input', '-'], payload)
    after = request([endpoint, '--method', 'GET'])
    if (after.get('number') != number or after.get('title') != issue.title or after.get('body') != issue.body
            or {x['name'] for x in after['labels']} != set(labels)):
        raise RuntimeError('Readback mismatch; inspect existing issue without creating a replacement')
    return dict(number=number, url=after['html_url'], body_sha256=hashlib.sha256(issue.body.encode()).hexdigest(),
                github_create_calls=0, status='verified')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--repo')
    parser.add_argument('--issue', type=int)
    parser.add_argument('--expected-body-sha256')
    args = parser.parse_args()
    issue = render(json.loads(args.input.read_text(encoding='utf-8-sig')))
    payload = dict(title=issue.title, body=issue.body, labels=list(issue.labels),
                   templates_fingerprint=active().fingerprint)
    if args.output:
        args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    if args.apply:
        result = update_existing(args.repo or '', args.issue, issue, args.expected_body_sha256)
        print(json.dumps(result, ensure_ascii=False))
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
