"""Turn-deadline transport adapter; shared service transport stays byte-identical.

DNS runs in a daemon resolver (no credentials or HTTP); only the calling thread performs
network requests and owns the model lock. No detached model request can outlive its turn.
"""
import http.client
import ipaddress
import json
import queue
import re
import socket
import threading
import time
from urllib.parse import urlsplit
from .ai_transport import (ProviderError, ENDPOINTS, PROVIDERS, PinnedHTTPS, LIMIT,
                           IMAGE_URL, usage_of)

_DNS_SLOTS = threading.BoundedSemaphore(2)


def _remaining(deadline):
    value = deadline-time.monotonic()
    if value <= 0:
        raise ProviderError('timeout')
    return value


def _addresses(host, deadline):
    if not _DNS_SLOTS.acquire(timeout=_remaining(deadline)):
        raise ProviderError('dns_timeout')
    result = queue.Queue(maxsize=1)
    def resolve():
        try:
            try:
                result.put(socket.getaddrinfo(host,443,type=socket.SOCK_STREAM))
            except OSError:
                result.put([])
        finally:
            _DNS_SLOTS.release()
    try:
        threading.Thread(target=resolve, daemon=True).start()
    except BaseException:
        _DNS_SLOTS.release()
        raise
    try:
        values = result.get(timeout=min(5,_remaining(deadline)))
    except queue.Empty:
        raise ProviderError('dns_timeout') from None
    addresses = [v[4][0] for v in values]
    if not addresses or any(not ipaddress.ip_address(a).is_global for a in addresses):
        raise ProviderError('invalid_endpoint')
    return addresses


class DeadlineHTTPS(PinnedHTTPS):
    def __init__(self, host, address, deadline):
        self.deadline = deadline
        super().__init__(host,address,_remaining(deadline))

    def connect(self):
        raw = socket.create_connection((self.address,443),_remaining(self.deadline))
        try:
            raw.settimeout(_remaining(self.deadline))
            self.sock = self._context.wrap_socket(raw,server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


def complete(provider, base_url, model, messages, schema, *, key='', timeout=20,
             max_output=2048, think=False, usage_sink=None):
    deadline = time.monotonic()+timeout
    connection = response = watchdog = None
    expired = threading.Event()
    try:
        u = urlsplit(base_url)
        if (provider not in PROVIDERS or not u.hostname or u.username or u.password or u.query or u.fragment
                or any(ord(c)<33 or ord(c)==127 for c in base_url)):
            raise ProviderError('invalid_endpoint')
        if provider == 'ollama':
            if u.scheme!='http' or u.hostname not in ('localhost','127.0.0.1','::1') or u.path not in ('','/'):
                raise ProviderError('invalid_endpoint')
        elif u.scheme!='https' or u.port not in (None,443) or (provider in ENDPOINTS and base_url.rstrip('/')!=ENDPOINTS[provider]):
            raise ProviderError('invalid_endpoint')
        if not isinstance(model,str) or not model.strip() or len(model)>200:
            raise ProviderError('invalid_model')
        if provider!='ollama' and (not key or any(ord(c)<32 for c in key)):
            raise ProviderError('key_required')
        if provider!='ollama' and think:
            raise ProviderError('thinking_unsupported')
        images=[p for m in messages if isinstance(m.get('content'),list) for p in m['content']
                if isinstance(p,dict) and p.get('type')=='image_url']
        if images and provider in ('ollama','anthropic'):
            raise ProviderError('images_unsupported')
        if any(not isinstance(p.get('image_url'),dict) or not isinstance(p['image_url'].get('url'),str)
               or not IMAGE_URL.fullmatch(p['image_url']['url']) for p in images):
            raise ProviderError('invalid_image')
        limit=75*1024*1024 if provider=='ollama' else 20*1024*1024 if images else 128*1024
        if len(json.dumps(messages).encode())>limit:
            raise ProviderError('input_too_large')
        from .document_reader import has_document_context,MAX_REQUEST_BYTES,wire_text_bytes
        document_request=has_document_context(messages)
        body=dict(model=model,stream=False,messages=messages)
        headers={'Content-Type':'application/json'}
        if provider=='ollama':
            body.update(think=think,format=schema,options=dict(temperature=0,num_predict=max_output))
            path='/api/chat'
            connection=http.client.HTTPConnection('127.0.0.1' if u.hostname=='localhost' else u.hostname,
                u.port or 11434,timeout=_remaining(deadline))
        else:
            if provider=='anthropic':
                headers.update({'x-api-key':key,'anthropic-version':'2023-06-01'})
                body['system']='\n'.join(m['content'] for m in messages if m['role']=='system')+'\nReturn only JSON matching this schema: '+json.dumps(schema)
                body['messages']=[m for m in messages if m['role']!='system']
                body.update(max_tokens=max_output,tools=[dict(name='result',description='Return structured JSON result',input_schema=schema)],tool_choice={'type':'auto'})
                path='/v1/messages'
            else:
                headers['Authorization']='Bearer '+key
                body.update(response_format={'type':'json_object'})
                body['max_completion_tokens' if provider=='openai' else 'max_tokens']=max_output
                if provider=='deepseek':
                    body['thinking']={'type':'disabled'}
                    body['temperature']=0
                body['messages']=[dict(role='system',content='Return JSON matching this schema: '+json.dumps(schema))]+messages
                path=u.path.rstrip('/')+'/chat/completions'
            if document_request and wire_text_bytes(body)>MAX_REQUEST_BYTES:
                raise ProviderError('input_too_large')
            addresses=_addresses(u.hostname,deadline)
            connection=DeadlineHTTPS(u.hostname,addresses[0],deadline)
        if document_request and wire_text_bytes(body)>MAX_REQUEST_BYTES:
            raise ProviderError('input_too_large')
        connection.connect()
        sock=connection.sock
        def expire():
            expired.set()
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        watchdog=threading.Timer(_remaining(deadline),expire)
        watchdog.daemon=True
        watchdog.start()
        sock.settimeout(_remaining(deadline))
        connection.request('POST',path,json.dumps(body).encode(),headers)
        response=connection.getresponse()
        if response.status!=200:
            raise ProviderError({401:'auth_failed',403:'auth_failed',404:'model_not_found',429:'rate_limited'}.get(response.status,'provider_failed'))
        chunks=[]; size=0
        while size<=LIMIT and not response.isclosed():
            response.fp.raw._sock.settimeout(_remaining(deadline))
            chunk=response.read1(min(65536,LIMIT+1-size))
            if not chunk:
                break
            chunks.append(chunk); size+=len(chunk)
        if expired.is_set():
            raise ProviderError('timeout')
        if size>LIMIT:
            raise ProviderError('response_too_large')
        envelope=json.loads(b''.join(chunks))
        if usage_sink:
            usage_sink(usage_of(provider,envelope))
        if provider=='anthropic':
            parts=[p for p in envelope['content'] if p.get('type')=='tool_use' and p.get('name')=='result']
            result=parts[0]['input'] if len(parts)==1 else json.loads(''.join(p.get('text','') for p in envelope['content'] if p.get('type')=='text'))
        else:
            content=envelope['message']['content'] if provider=='ollama' else envelope['choices'][0]['message']['content']
            result=json.loads(re.sub(r'^\s*<think>.*?</think>\s*','',content,count=1,flags=re.S))
        if not isinstance(result,dict):
            raise ProviderError('invalid_json')
        return result
    except ProviderError:
        raise
    except (TimeoutError,socket.timeout):
        raise ProviderError('timeout') from None
    except (ValueError,KeyError,TypeError):
        raise ProviderError('invalid_json') from None
    except Exception:
        raise ProviderError('timeout' if expired.is_set() else 'provider_failed') from None
    finally:
        if watchdog:
            watchdog.cancel()
        if response:
            response.close()
        if connection:
            connection.close()
