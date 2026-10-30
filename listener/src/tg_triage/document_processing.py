"""Host-bounded document map stage. Model selects evidence; it never grants tool authority."""
import hashlib
import json
import re

DIRECT_BYTES=24*1024
PART_BYTES=12*1024
BLOCK_BYTES=384
MAX_PARTS=16
MAX_SELECTED_BYTES=1600
MAX_SELECTED_BLOCKS=4
PART_SCHEMA={
    'type':'object','properties':{
        'status':{'type':'string','enum':['relevant','irrelevant']},
        'block_ids':{'type':'array','items':{'type':'integer'},'maxItems':MAX_SELECTED_BLOCKS}},
    'required':['status','block_ids'],'additionalProperties':False}


def _hash(value):
    return hashlib.sha256(value).hexdigest()


def _blocks(text):
    """Lossless host ranges; neither Unicode codepoints nor bytes are dropped."""
    result=[];offset=0;buffer=''
    def emit(value):
        nonlocal offset
        data=value.encode()
        result.append(dict(block_id=len(result),text=value,start_byte=offset,end_byte=offset+len(data)))
        offset+=len(data)
    for line in text.splitlines(keepends=True):
        if buffer and len((buffer+line).encode())>BLOCK_BYTES:
            emit(buffer);buffer=''
        for char in line:
            if len((buffer+char).encode())>BLOCK_BYTES:
                emit(buffer);buffer=''
            buffer+=char
    if buffer:
        emit(buffer)
    return result


def parts(text):
    result=[];current=[];size=0
    for block in _blocks(text):
        count=block['end_byte']-block['start_byte']
        if current and size+count>PART_BYTES:
            data=''.join(b['text'] for b in current).encode()
            result.append(dict(part_index=len(result),start_byte=current[0]['start_byte'],end_byte=current[-1]['end_byte'],sha256=_hash(data),blocks=current))
            current=[];size=0
        current.append(block);size+=count
    if current:
        data=''.join(b['text'] for b in current).encode()
        result.append(dict(part_index=len(result),start_byte=current[0]['start_byte'],end_byte=current[-1]['end_byte'],sha256=_hash(data),blocks=current))
    return result


def selected(value,part):
    if not isinstance(value,dict) or set(value)!={'status','block_ids'} or value.get('status') not in ('relevant','irrelevant'):
        return None
    ids=value['block_ids'];lookup={b['block_id']:b for b in part['blocks']}
    if (not isinstance(ids,list) or len(ids)>MAX_SELECTED_BLOCKS or any(type(i) is not int or i not in lookup for i in ids)
            or ids!=sorted(set(ids)) or bool(ids)!=(value['status']=='relevant')):
        return None
    text=''.join(lookup[i]['text'] for i in ids)
    if len(text.encode())>MAX_SELECTED_BYTES:return None
    return text


def valid_processing(value,size,sha):
    if (not isinstance(value,dict) or set(value)!={'kind','status','text_sha256','text_byte_count','parts','selected_sha256'}
            or value.get('kind')!='bounded_block_selection_v1' or value.get('status')!='complete'
            or type(size) is not int or value.get('text_byte_count')!=size or value.get('text_sha256')!=sha
            or not isinstance(value.get('selected_sha256'),str) or re.fullmatch('[0-9a-f]{64}',value['selected_sha256']) is None):return False
    receipts=value.get('parts')
    if not isinstance(receipts,list) or not 1<=len(receipts)<=MAX_PARTS:return False
    end=0
    for index,part in enumerate(receipts):
        if (not isinstance(part,dict) or set(part)!={'part_index','start_byte','end_byte','sha256','selected_sha256'}
                or type(part.get('part_index')) is not int or part['part_index']!=index
                or type(part.get('start_byte')) is not int or part['start_byte']!=end
                or type(part.get('end_byte')) is not int or not end<part['end_byte']<=min(size,end+PART_BYTES)
                or any(not isinstance(part.get(k),str) or re.fullmatch('[0-9a-f]{64}',part[k]) is None for k in ('sha256','selected_sha256'))):return False
        end=part['end_byte']
    return end==size


def prepare(rows,classifier):
    """All chunks must have a valid bounded result before any document is usable."""
    from .thinking import CURRENT
    from .document_reader import MAX_TEXT_BYTES,valid_contents
    rows=[dict(row) for row in rows]
    readable=[r for r in rows if r.get('read_status')=='read']
    if not readable:return rows
    if not valid_contents(rows):
        for row in readable:row.update(read_status='processing_failed');row.pop('text',None)
        return rows
    if all('processing' in r for r in readable):return rows
    if any('processing' in r for r in readable):
        for row in readable:row.update(read_status='processing_failed');row.pop('text',None)
        return rows
    total=sum(len(r.get('text','').encode()) for r in readable)
    escaped=sum(len(json.dumps(r.get('text','')).encode()) for r in readable)
    if max(total,escaped)<=DIRECT_BYTES:return rows
    work=[(r,parts(r['text'])) for r in readable]
    count=sum(len(p) for _,p in work)
    turn=CURRENT.get();ask=getattr(classifier,'analyze_document_part',None)
    if (total>MAX_TEXT_BYTES or not 1<=count<=MAX_PARTS or turn is None or ask is None):
        for row in readable:row.update(read_status='processing_failed');row.pop('text',None)
        return rows
    manifest=tuple((r['message_id'],r['sha256'],r.get('text_sha256',_hash(r['text'].encode()))) for r,_ in work)
    if not turn.configure_document_work(count,manifest):
        for row in readable:row.update(read_status='processing_failed');row.pop('text',None)
        return rows
    for row,chunks in work:
        quotes=[];receipts=[]
        text_data=row['text'].encode();text_sha=_hash(text_data)
        for part in chunks:
            response=ask(row,part)
            quote=selected(response,part)
            if quote is None:
                for failed in readable:failed.update(read_status='processing_failed');failed.pop('text',None)
                turn.reason=turn.reason or 'document_processing_failed'
                return rows
            quotes.append(quote)
            receipts.append({**{k:part[k] for k in ('part_index','start_byte','end_byte','sha256')},'selected_sha256':_hash(quote.encode())})
        text='\n'.join(quotes)
        if not text.strip():
            for failed in readable:failed.update(read_status='processing_failed');failed.pop('text',None)
            turn.reason='document_processing_failed';return rows
        row.update(text=text,text_sha256=text_sha,text_byte_count=len(text_data),
                   extraction_kind=row.get('extraction_kind','utf8_text_v1'),
                   processing=dict(kind='bounded_block_selection_v1',status='complete',text_sha256=text_sha,
                                   text_byte_count=len(text_data),parts=receipts,selected_sha256=_hash(text.encode())))
    return rows
