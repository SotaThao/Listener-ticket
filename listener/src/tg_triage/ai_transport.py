"""Bounded provider JSON transport; duplicated in independent service packages (parity tested)."""
import http.client
import ipaddress
import json
import os
import queue
import threading
from pathlib import Path
import re
import socket
import ssl
import time
from urllib.parse import urlsplit

PROVIDERS = {'ollama', 'anthropic', 'openai', 'deepseek', 'compatible'}
ENDPOINTS = {'anthropic': 'https://api.anthropic.com', 'openai': 'https://api.openai.com/v1', 'deepseek': 'https://api.deepseek.com'}
LIMIT = 1024 * 1024
TEXT_INPUT_LIMIT = 128 * 1024
IMAGE_INPUT_LIMIT = 20 * 1024 * 1024
IMAGE_URL = re.compile(r'data:image/(?:jpeg|png|webp);base64,[A-Za-z0-9+/]+={0,2}')

class ProviderError(ValueError):
    def __init__(self, code='provider_failed'):
        self.code = code
        super().__init__(code)

def resolve(host):
    result=queue.Queue(maxsize=1)
    def run():
        try:result.put(socket.getaddrinfo(host,443,type=socket.SOCK_STREAM))
        except Exception:result.put(None)
    threading.Thread(target=run,daemon=True).start()
    try:value=result.get(timeout=5)
    except queue.Empty:raise ProviderError('dns_timeout') from None
    if not value:raise ProviderError('invalid_endpoint')
    return value

def validate_endpoint(provider, url):
    try:
        u = urlsplit(url)
        port = u.port
        if provider not in PROVIDERS or not u.hostname or u.username or u.password or u.query or u.fragment:
            raise ValueError()
        if any(ord(c) < 33 or ord(c) == 127 for c in url):
            raise ValueError()
        if provider == 'ollama':
            if u.scheme != 'http' or u.hostname not in ('127.0.0.1', 'localhost', '::1') or u.path not in ('', '/'):
                raise ValueError()
        elif u.scheme != 'https' or (port is not None and port != 443):
            raise ValueError()
        if provider in ENDPOINTS and url.rstrip('/') != ENDPOINTS[provider]:
            raise ValueError()
        if provider == 'compatible':
            for item in resolve(u.hostname):
                if not ipaddress.ip_address(item[4][0]).is_global:
                    raise ValueError()
        return u
    except (ValueError, OSError, TypeError):
        raise ProviderError('invalid_endpoint') from None

class PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host, address, timeout):
        super().__init__(host, timeout=timeout, context=ssl.create_default_context())
        self.address = address
    def connect(self):
        sock = socket.create_connection((self.address, 443), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)

def request_json(url, payload, headers, timeout, provider):
    u = validate_endpoint(provider, url.rsplit('/api/chat', 1)[0] if provider == 'ollama' else
                          url.rsplit('/messages', 1)[0].removesuffix('/v1') if provider == 'anthropic' else
                          url.rsplit('/chat/completions', 1)[0])
    connection = None
    response = None
    watchdog = None
    expired = threading.Event()
    try:
        if provider == 'ollama':
            connection = http.client.HTTPConnection('127.0.0.1' if u.hostname=='localhost' else u.hostname, u.port or 11434, timeout=timeout)
        else:
            addresses = [item[4][0] for item in resolve(u.hostname)]
            if not addresses or any(not ipaddress.ip_address(a).is_global for a in addresses):
                raise ProviderError('invalid_endpoint')
            connection = PinnedHTTPS(u.hostname, addresses[0], timeout)
        path = urlsplit(url).path
        connection.connect()
        upstream_socket=connection.sock
        def expire():
            expired.set()
            try:upstream_socket.shutdown(socket.SHUT_RDWR)
            except OSError:pass
        watchdog=threading.Timer(timeout,expire);watchdog.daemon=True;watchdog.start()
        connection.request('POST', path, json.dumps(payload).encode(), headers)
        response = connection.getresponse()
        if response.status != 200:
            raise ProviderError({401:'auth_failed',403:'auth_failed',404:'model_not_found',429:'rate_limited'}.get(response.status, 'provider_failed'))
        deadline=time.monotonic()+timeout
        chunks=[];size=0
        while size<=LIMIT and not response.isclosed():
            remaining=deadline-time.monotonic()
            if remaining<=0:raise ProviderError('timeout')
            response.fp.raw._sock.settimeout(remaining)
            chunk=response.read1(min(65536,LIMIT+1-size))
            if not chunk:break
            chunks.append(chunk);size+=len(chunk)
        raw=b''.join(chunks)
        if expired.is_set():
            raise ProviderError('timeout')
        if len(raw) > LIMIT:
            raise ProviderError('response_too_large')
        return json.loads(raw)
    except ProviderError:
        raise
    except (TimeoutError, socket.timeout):
        raise ProviderError('timeout') from None
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise ProviderError('timeout' if expired.is_set() else 'invalid_json') from None
    except Exception:
        raise ProviderError('timeout' if expired.is_set() else 'provider_failed') from None
    finally:
        if watchdog:watchdog.cancel()
        if response:response.close()
        if connection:
            connection.close()

def read_key(path, scope, environ=None, group_name=None):
    """Credential file is explicitly scoped; environment keys must have explicit scope too."""
    env = os.environ if environ is None else environ
    if env.get('AGENTFLEET_KEY_SCOPE') == scope:
        return env.get('AGENTFLEET_API_KEY', '')
    if not path:
        return ''
    try:
        p = Path(path)
        if os.name != 'nt':
            info=p.stat()
            if info.st_mode & 0o027:raise ValueError()
            if info.st_mode & 0o040:
                import grp
                if not group_name or info.st_gid!=grp.getgrnam(group_name).gr_gid:raise ValueError()
        value = json.loads(p.read_text())
        return value['key'] if value['scope'] == scope else ''
    except Exception:
        raise ProviderError('credentials_unavailable') from None

_EMAIL = re.compile(r'[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}')
# Kept verbatim: timestamps (YYYY-MM-DD [HH:MM[:SS]], dd.mm.yyyy, a lone HH:MM), bracketed message ids
# ([123456789], [trả lời 123456789]) and the code-built id list. Everything else that looks like a phone number goes.
_REDACT = re.compile(
    r'(?P<keep>\d{4}-\d{2}-\d{2}(?:[ T]\d{1,2}:\d{2}(?::\d{2})?)?(?![\d:-])'
    r'|(?<![\d.:-])\d{1,2}[.-]\d{1,2}[.-]\d{4}(?![\d.:-])'
    r'|(?<![\d:])\d{1,2}:\d{2}(?::\d{2})?(?![\d:])'
    r'|\[[1-9]\d*\]|\[trả lời [1-9]\d*\]'
    r'|(?<=các ID bắt buộc: )[1-9]\d*(?:, [1-9]\d*)*)'
    r'|(?P<phone>(?<!\w)\(?\+?\d[\d () .-]{6,}\d(?!\w|:\d))')

def redact(text):
    text = _EMAIL.sub('[email]', text)
    return _REDACT.sub(lambda m: m.group('keep') or '[phone]', text)

def _count(value):
    return value if type(value) is int and value >= 0 else 0

def usage_of(provider, envelope):
    """Token counts from a provider envelope; counts only, never content. Unknown shapes give zeros."""
    zero = {'prompt_tokens': 0, 'completion_tokens': 0, 'cache_hit_tokens': 0}
    try:
        if provider == 'ollama':
            return {'prompt_tokens': _count(envelope.get('prompt_eval_count')),
                    'completion_tokens': _count(envelope.get('eval_count')), 'cache_hit_tokens': 0}
        usage = envelope.get('usage')
        if not isinstance(usage, dict):
            return zero
        if provider == 'anthropic':
            hit = _count(usage.get('cache_read_input_tokens'))
            return {'prompt_tokens': _count(usage.get('input_tokens')) + hit,
                    'completion_tokens': _count(usage.get('output_tokens')), 'cache_hit_tokens': hit}
        details = usage.get('prompt_tokens_details')
        hit = _count(usage.get('prompt_cache_hit_tokens')) or (
            _count(details.get('cached_tokens')) if isinstance(details, dict) else 0)
        return {'prompt_tokens': _count(usage.get('prompt_tokens')),
                'completion_tokens': _count(usage.get('completion_tokens')), 'cache_hit_tokens': hit}
    except Exception:
        return zero

def _image_parts(messages):
    return [part for m in messages if isinstance(m.get('content'), list)
            for part in m['content'] if isinstance(part, dict) and part.get('type') == 'image_url']

def complete(provider, base_url, model, messages, schema, *, key='', timeout=60, max_output=2048, think=False, context=None, transport=request_json, usage_sink=None):
    validate_endpoint(provider, base_url)
    if not isinstance(model, str) or not model.strip() or len(model) > 200:
        raise ProviderError('invalid_model')
    if provider != 'ollama' and (not key or any(ord(c)<32 for c in key)):
        raise ProviderError('key_required')
    if provider != 'ollama' and think:
        raise ProviderError('thinking_unsupported')
    images = _image_parts(messages)
    if images and provider in ('ollama', 'anthropic'):
        raise ProviderError('images_unsupported')
    for part in images:
        url = part.get('image_url', {}).get('url') if isinstance(part.get('image_url'), dict) else None
        if not isinstance(url, str) or not IMAGE_URL.fullmatch(url):
            raise ProviderError('invalid_image')   # only inline data: images; a remote URL is never fetched
    # Existing local vision supports five 10MiB images including base64 overhead.
    input_limit=75*1024*1024 if provider=='ollama' else IMAGE_INPUT_LIMIT if images else TEXT_INPUT_LIMIT
    if len(json.dumps(messages).encode()) > input_limit:
        raise ProviderError('input_too_large')
    headers = {'Content-Type':'application/json'}
    body = {'model':model, 'stream':False, 'messages':messages}
    if provider == 'ollama':
        body.update(think=think, format=schema, options={'temperature':0,'num_predict':max_output})
        if context is not None:body['options']['num_ctx']=context
        path = '/api/chat'
    elif provider == 'anthropic':
        headers.update({'x-api-key':key,'anthropic-version':'2023-06-01'})
        body['system'] = '\n'.join(m['content'] for m in messages if m['role']=='system') + '\nReturn only JSON matching this schema: ' + json.dumps(schema)
        body['messages'] = [m for m in messages if m['role'] != 'system']
        body.update(max_tokens=max_output, tools=[{'name':'result','description':'Return structured JSON result','input_schema':schema}], tool_choice={'type':'auto'})
        path = '/v1/messages'
    else:
        headers['Authorization'] = 'Bearer ' + key
        body.update(response_format={'type':'json_object'})
        body['max_completion_tokens' if provider=='openai' else 'max_tokens'] = max_output
        body['messages'] = [{'role':'system','content':'Return JSON matching this schema: '+json.dumps(schema)}]+messages
        path = '/chat/completions'
    try:
        envelope = transport(base_url.rstrip('/')+path, body, headers, min(max(float(timeout), 1), 1800), provider)
        if usage_sink is not None:
            try:usage_sink(usage_of(provider, envelope))
            except Exception:pass
        if provider == 'anthropic':
            parts = [p for p in envelope['content'] if p.get('type')=='tool_use' and p.get('name')=='result']
            result = parts[0]['input'] if len(parts)==1 else json.loads(''.join(p.get('text','') for p in envelope['content'] if p.get('type')=='text'))
        else:
            content = envelope['message']['content'] if provider=='ollama' else envelope['choices'][0]['message']['content']
            result = json.loads(re.sub(r'^\s*<think>.*?</think>\s*', '', content, count=1, flags=re.S))
        if not isinstance(result, dict):
            raise ValueError()
        return result
    except ProviderError:
        raise
    except Exception:
        raise ProviderError('invalid_json') from None
