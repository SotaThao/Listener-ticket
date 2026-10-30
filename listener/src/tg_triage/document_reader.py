"""Bounded plain-text attachment evidence. Never execute markdown or fetch its links."""
import hashlib
import json
import re
import tempfile
import unicodedata
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit
from .attachments import document_extension

MAX_DOCUMENTS=4
MAX_BYTES=256*1024
MAX_TOTAL_BYTES=512*1024
MAX_TEXT_BYTES=192*1024
MAX_REQUEST_BYTES=48*1024
TEXT_MIME={'text/plain','text/markdown','text/x-markdown','text/x-web-markdown'}
HTML_MIME={'text/html','application/xhtml+xml','application/octet-stream'}


def wire_body(body):
    """Host-only alternatives never become wire data or serialized text-budget input."""
    return {k:v for k,v in body.items() if not k.startswith('_')}


class _StaticHTML(HTMLParser):
    """Inert evidence extraction: no rendering, resource loading or script evaluation."""
    hidden={'script','style','template','noscript','svg','canvas','iframe','object','embed'}
    blocks={'p','div','section','article','header','footer','main','aside','nav','h1','h2','h3','h4','h5','h6',
            'li','ul','ol','table','tr','td','th','form','label','option','button','br','hr','title'}
    voids={'input','br','hr','img','meta','link','source','area','base','wbr','embed'}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack=[];self.output=[]

    def handle_starttag(self,tag,attrs):
        attrs=dict(attrs)
        hidden=bool(self.stack and self.stack[-1][1]) or tag in self.hidden or 'hidden' in attrs or attrs.get('aria-hidden')=='true'
        style=(attrs.get('style') or '').replace(' ','').lower()
        hidden=hidden or 'display:none' in style or 'visibility:hidden' in style
        if tag=='input' and (attrs.get('type') or '').lower()=='hidden':hidden=True
        if not hidden:
            if tag in self.blocks:self.output.append('\n')
            if tag=='img' and attrs.get('alt'):self.output.append(' '+attrs['alt']+' ')
            if tag in ('input','textarea','select'):
                fields=[]
                for key in ('name','type','minlength','maxlength','pattern'):
                    if attrs.get(key):fields.append(key+'='+attrs[key])
                if 'required' in attrs:fields.append('required')
                if fields:self.output.append(' ['+tag+' '+' '.join(fields)+'] ')
                for key in ('aria-label','placeholder'):
                    if attrs.get(key):self.output.append(' '+attrs[key]+' ')
            if tag=='a' and attrs.get('href'):
                href=attrs['href'].strip()
                parsed=urlsplit(href)
                if parsed.scheme in ('http','https','mailto','tel') or href.startswith(('/','#')):
                    self.output.append(' ['+href+'] ')
        if tag not in self.voids:self.stack.append((tag,hidden))

    def handle_startendtag(self,tag,attrs):
        self.handle_starttag(tag,attrs)
        if tag not in self.voids:self.handle_endtag(tag)

    def handle_endtag(self,tag):
        for index in range(len(self.stack)-1,-1,-1):
            if self.stack[index][0]==tag:
                _,hidden=self.stack[index];del self.stack[index:]
                if not hidden and tag in self.blocks:self.output.append('\n')
                return

    def handle_data(self,data):
        if not self.stack or not self.stack[-1][1]:self.output.append(data)

    def text(self):
        return '\n'.join(line for raw in ''.join(self.output).splitlines() if (line:=' '.join(raw.split())))


def _kind(message):
    name=(message.document_name or '').lower().replace('\\','/').rsplit('/',1)[-1]
    mime=(message.document_mime or '').split(';')[0].strip().lower()
    if name.endswith(('.html','.htm')) and mime in HTML_MIME:
        # Do not let an executable with an appended safe extension become readable.
        from .attachments import DENIED_EXTENSIONS
        if not any(p.strip() in DENIED_EXTENSIONS for p in name.split('.')[1:]):return 'html_static_v1'
    if document_extension(message.document_name,message.document_mime) in ('md','txt'):return 'utf8_text_v1'
    return None


def text_intent(message):
    name=(message.document_name or '').lower().replace('\\','/').rsplit('/',1)[-1]
    mime=(message.document_mime or '').split(';')[0].strip().lower()
    return message.has_document and (name.endswith(('.md','.txt','.html','.htm')) or mime in (TEXT_MIME|HTML_MIME)-{'application/octet-stream'})


def read_documents(messages,downloader):
    results=[];total=0;attempts=0
    for message in messages:
        if not message.has_document:
            continue
        from .group_notices import clean_question
        name=clean_question((message.document_name or '').replace('\\','/').rsplit('/',1)[-1])[:100]
        row=dict(message_id=message.message_id,name=name,read_status='unsupported',byte_count=0)
        results.append(row)
        if not text_intent(message):
            continue
        attempts+=1
        if attempts>MAX_DOCUMENTS:
            row['read_status']='limit';continue
        kind=_kind(message)
        if kind is None:
            continue
        if type(message.document_size) is int and message.document_size>MAX_BYTES:
            row['read_status']='oversize';continue
        row['read_status']='failed'
        from .thinking import CURRENT
        turn=CURRENT.get()
        if downloader is None or turn is not None and turn.is_set():
            continue
        try:
            with tempfile.TemporaryDirectory(prefix='tg-triage-text-') as directory:
                root=Path(directory);root.chmod(0o700)
                saved=downloader(message.chat_id,message.message_id,root)
                if saved is None:
                    continue
                path=Path(saved);resolved=path.resolve()
                if root.resolve() not in resolved.parents or not path.is_file():
                    continue
                probe=path
                while probe!=root:
                    if probe.is_symlink():
                        break
                    probe=probe.parent
                if probe!=root:
                    continue
                if path.stat().st_size>MAX_BYTES:
                    row['read_status']='oversize';continue
                with path.open('rb') as stream:
                    data=stream.read(MAX_BYTES+1)
                row['byte_count']=len(data)
                total+=len(data)
                if len(data)>MAX_BYTES or total>MAX_TOTAL_BYTES:
                    row['read_status']='oversize';continue
                try:
                    text=data.decode('utf-8-sig')
                except UnicodeDecodeError:
                    row['read_status']='invalid_encoding';continue
                if any(unicodedata.category(c)=='Cc' and c not in '\n\r\t' for c in text):
                    row['read_status']='binary';continue
                if kind=='html_static_v1':
                    if not re.search(r'<(?:!doctype\s+html|html|head|body|main|section|div|p|h[1-6]|form)\b',text,re.I):
                        row['read_status']='unsupported';continue
                    parser=_StaticHTML();parser.feed(text);parser.close();text=parser.text()
                    if not text.strip():
                        row['read_status']='empty';continue
                encoded=text.encode('utf-8')
                if len(encoded)>MAX_TEXT_BYTES:
                    row['read_status']='oversize';continue
                row.update(read_status='read',sha256=hashlib.sha256(data).hexdigest(),text=text,
                           extraction_kind=kind,text_byte_count=len(encoded),text_sha256=hashlib.sha256(encoded).hexdigest())
        except Exception:
            pass  # No file name, contents, paths or remote exception text in logs.
    return results


def proof(rows):
    return [{k:v for k,v in row.items() if k not in ('text','name')} for row in rows]


def valid_proof(rows,messages):
    if not isinstance(rows,list) or len(rows)!=len([m for m in messages if m.has_document]):
        return False
    by_id={r.get('message_id'):r for r in rows if isinstance(r,dict)}
    if len(by_id)!=len(rows) or set(by_id)!={m.message_id for m in messages if m.has_document}:
        return False
    total=0;count=0;text_total=0;legacy_total=0
    for message in messages:
        if not text_intent(message):
            continue
        row=by_id[message.message_id];count+=1
        size=row.get('byte_count')
        if (row.get('read_status')!='read' or type(size) is not int or not 0<=size<=MAX_BYTES
                or not isinstance(row.get('sha256'),str) or re.fullmatch('[0-9a-f]{64}',row['sha256']) is None
                or set(row)-{'message_id','read_status','byte_count','sha256','extraction_kind','text_byte_count','text_sha256','processing'}):
            return False
        kind=_kind(message)
        if kind is None:return False
        extraction_fields={'extraction_kind','text_byte_count','text_sha256'}
        has_extraction=bool(set(row)&(extraction_fields|{'processing'}))
        if kind=='html_static_v1' or has_extraction:
            if (not extraction_fields<=set(row) or row.get('extraction_kind')!=kind
                    or type(row.get('text_byte_count')) is not int or not 0<=row['text_byte_count']<=MAX_TEXT_BYTES
                    or not isinstance(row.get('text_sha256'),str) or re.fullmatch('[0-9a-f]{64}',row['text_sha256']) is None):return False
            text_total+=row['text_byte_count']
        else:
            # Before extraction metadata existed, readable MD/TXT had the old16/32KiB envelope.
            if size>16*1024:return False
            legacy_total+=size
        if 'processing' in row:
            from .document_processing import valid_processing
            if not valid_processing(row['processing'],row.get('text_byte_count'),row.get('text_sha256')):return False
        total+=size
    return count<=MAX_DOCUMENTS and total<=MAX_TOTAL_BYTES and text_total<=MAX_TEXT_BYTES and legacy_total<=32*1024


def valid_contents(rows):
    """Validate host rows before cloud projection; redacted wire text deliberately has a different hash."""
    if not isinstance(rows,list):return False
    for row in rows:
        if not isinstance(row,dict):return False
        if row.get('read_status')!='read':
            if 'text' in row:return False
            continue
        text=row.get('text')
        if not isinstance(text,str):return False
        data=text.encode('utf-8')
        if 'processing' in row:
            from .document_processing import valid_processing
            if (not valid_processing(row['processing'],row.get('text_byte_count'),row.get('text_sha256'))
                    or hashlib.sha256(data).hexdigest()!=row['processing']['selected_sha256']):return False
        elif 'text_sha256' in row:
            if row.get('text_byte_count')!=len(data) or row.get('text_sha256')!=hashlib.sha256(data).hexdigest():return False
    return True


def failure_text(rows):
    states={r.get('read_status') for r in rows if r.get('read_status')!='read'}
    if 'invalid_encoding' in states or 'binary' in states:
        return 'Em chưa đọc được tài liệu: tệp không phải UTF-8 hợp lệ hoặc có dữ liệu nhị phân. Anh/Chị gửi bản văn bản UTF-8 giúp Em nhé.'
    if 'oversize' in states or 'limit' in states:
        return 'Tài liệu vượt giới hạn xử lý: tối đa 4 tệp, 256 KiB mỗi tệp và 512 KiB tổng. Em đã giữ tin để kiểm tra, chưa cập nhật ticket.'
    if 'unsupported' in states:
        return 'Em chưa đọc được tài liệu: tên tệp, MIME hoặc nội dung không khớp định dạng văn bản/HTML được hỗ trợ. Em chưa cập nhật ticket.'
    if 'processing_failed' in states:
        return 'Em đã nhận tài liệu nhưng chưa phân tích đủ mọi phần, nên chưa cập nhật ticket.'
    return 'Em chưa tải/đọc được đầy đủ tài liệu để phân tích, nên chưa cập nhật ticket.'


def _text_messages(messages):
    # Cloud image wrappers keep the trusted text object; pixel bytes retain their separate budget.
    result=[]
    for message in messages:
        value=dict(message)
        if isinstance(value.get('content'),list):
            parts=value['content']
            from .ai_transport import IMAGE_URL
            if any(not isinstance(p,dict) or p.get('type') not in ('text','image_url')
                    or p.get('type')=='text' and not isinstance(p.get('text'),str)
                    or p.get('type')=='image_url' and (not isinstance(p.get('image_url'),dict)
                        or not isinstance(p['image_url'].get('url'),str) or not IMAGE_URL.fullmatch(p['image_url']['url'])) for p in parts):
                return None
            texts=[p['text'] for p in parts if p.get('type')=='text']
            if len(texts)!=1:
                return None
            value['content']=texts[0]
            value['images']=['']*sum(p.get('type')=='image_url' for p in parts)
        result.append(value)
    return result


def request_fits(body):
    from .thinking import DocumentContextJSON,ContextJSON,request_text_bytes
    try:
        messages=_text_messages(body['messages'])
        if messages is None:
            return False
        normalized=dict(wire_body(body),messages=messages)
        marked=[m for m in messages if isinstance(m.get('content'),DocumentContextJSON)]
        if not marked:
            return request_text_bytes(normalized)<=8000
        if len(marked)!=1 or marked[0].get('role')!='user':
            return False
        packet=json.loads(marked[0]['content'])
        rows=packet.pop('DOCUMENT_CONTENTS')
        ids=set(packet.get('NEW_SOURCE_IDS',packet.get('DOCUMENT_SOURCE_IDS',[])))
        if not isinstance(rows,list) or len(rows)>20 or len({r['message_id'] for r in rows})!=len(rows):
            return False
        total=0;count=0
        for row in rows:
            if type(row['message_id']) is not int or row['message_id'] not in ids:
                return False
            if row.get('read_status')=='read':
                text=row['text'];size=row['byte_count']
                if (not isinstance(text,str) or type(size) is not int or not 0<=size<=MAX_BYTES
                        or len(text.encode('utf-8'))>MAX_TEXT_BYTES or not re.fullmatch('[0-9a-f]{64}',row['sha256'])):
                    return False
                count+=1;total+=max(size,len(text.encode('utf-8')))
            elif 'text' in row:
                return False
        if count>MAX_DOCUMENTS or total>MAX_TOTAL_BYTES:
            return False
        base=[dict(m,content=ContextJSON(json.dumps(packet,ensure_ascii=False,separators=(',',':')))) if m is marked[0] else m for m in messages]
        if request_text_bytes(dict(normalized,messages=base))>8000:
            return False
        # Escaped serialization is a stricter bound than raw UTF-8 character accounting.
        escaped=dict(normalized,messages=[{k:v for k,v in m.items() if k!='images'} for m in messages])
        return (request_text_bytes(normalized)<=MAX_REQUEST_BYTES
                and len(json.dumps(escaped).encode('utf-8'))+512<=MAX_REQUEST_BYTES)
    except (ValueError,TypeError,KeyError,UnicodeError):
        return False


def has_document_context(messages):
    from .thinking import DocumentContextJSON
    return any(isinstance(m.get('content'),DocumentContextJSON)
        or isinstance(m.get('content'),list) and any(isinstance(p,dict)
            and isinstance(p.get('text'),DocumentContextJSON) for p in m['content']) for m in messages)


def valid_task_details(analysis,diagnostics=None,path='analysis'):
    """Check renderer essentials, not the semantic correctness of model-defined work."""
    from .models import utf8_valid,DETAIL_STRING_KEYS,DETAIL_LIST_KEYS
    from .ingress_diagnostics import reject
    def bad(code,field,value=None):
        reject(diagnostics,code,path+'.details'+('.'+field if field else ''),value)
        return False
    if isinstance(analysis,dict):
        kind=analysis.get('type');category=analysis.get('category')
        details=analysis.get('details',{})
    else:
        kind=analysis.classification.type if analysis.classification else None
        category=analysis.category;details=dict(analysis.details or ())
    if kind!='task' and category!='task':
        return True
    if not isinstance(details,dict):
        return bad('type','',details)
    if set(details)-set(DETAIL_STRING_KEYS)-set(DETAIL_LIST_KEYS):
        return bad('additional_field','',details)
    for key,value in details.items():
        if key in DETAIL_STRING_KEYS and not isinstance(value,str):
            return bad('type',key,value)
        if key in DETAIL_LIST_KEYS and not isinstance(value,(list,tuple)):
            return bad('type',key,value)
        if isinstance(value,str) and (not utf8_valid(value) or len(value)>500):
            return bad('max_length',key,value)
        if isinstance(value,(list,tuple)):
            if len(value)>10:
                return bad('max_items',key,value)
            for index,item in enumerate(value):
                if not utf8_valid(item) or len(item)>200:
                    return bad('max_length',f'{key}[{index}]',item)
    # Task aliases may only be cleared, never carry criteria/context under the wrong renderer keys.
    for alias,empty in (('context',(None,'')),('acceptance',(None,[],()))):
        if details.get(alias) not in empty:
            return bad('task_essentials',alias,details.get(alias))
    from .group_notices import clean_analysis
    from .issue_format import strip_image_marker
    def visible(value):
        return clean_analysis(strip_image_marker(value)).strip() if utf8_valid(value) else ''
    work=details.get('work');done=details.get('done')
    if not utf8_valid(work) or not visible(work):
        return bad('task_essentials','work',work)
    if not isinstance(done,(list,tuple)) or not 1<=len(done)<=10 or not all(utf8_valid(v) and visible(v) for v in done):
        return bad('task_essentials','done',done)
    return True


def wire_text_bytes(body):
    """Count the actual wire shape/escaping, excluding only validated pixel payloads."""
    if _text_messages(body['messages']) is None:
        return MAX_REQUEST_BYTES+1
    messages=[]
    from .provider import _image_mime
    for message in body['messages']:
        value=dict(message)
        if isinstance(value.get('content'),list):
            parts=[]
            for original in value['content']:
                part=dict(original)
                if part.get('type')=='image_url':
                    image=dict(part['image_url'])
                    image['url']=image['url'].split(';base64,',1)[0]+';base64,'
                    part['image_url']=image
                parts.append(part)
            value['content']=parts
        if 'images' in value:
            if not isinstance(value['images'],list) or any(not isinstance(v,str) or _image_mime(v) is None for v in value['images']):
                return MAX_REQUEST_BYTES+1
            value['images']=['']*len(value['images'])
        messages.append(value)
    return len(json.dumps(dict(wire_body(body),messages=messages)).encode('utf-8'))
