"""Bounded validation metadata; never retain input values or unknown field names."""
import re
from .models import DETAIL_STRING_KEYS,DETAIL_LIST_KEYS

CODES=frozenset(('required','type','max_length','max_items','additional_field','forbidden_field','invalid_value',
    'task_essentials','candidate_unknown','json_invalid','canonical_invalid'))
TYPES=frozenset(('missing','null','boolean','integer','number','string','array','object','unknown'))
FIELDS=frozenset(('root','groups','message_ids','action','candidate_id','confidence','question','analysis',
    'answer','options','needs_choice','task','title','work','acceptance','category','type','actionable',
    'verdict','summary','priority','missing_info','reason','details','key_message_ids')+DETAIL_STRING_KEYS+DETAIL_LIST_KEYS)
MISSING=object()


def safe_path(path):
    if not isinstance(path,str) or len(path)>250:
        return False
    parts=path.split('.')
    for part in parts:
        match=re.fullmatch(r'([a-z_]+)(?:\[([0-9]|1[0-9])\])?',part)
        if match is None or match[1] not in FIELDS:
            return False
    return bool(parts)


def project(records):
    result=[]
    if not isinstance(records,list):
        return result
    for row in records[:8]:
        if (not isinstance(row,dict) or not isinstance(row.get('code'),str) or row['code'] not in CODES
                or not isinstance(row.get('actual_type'),str) or row['actual_type'] not in TYPES or not safe_path(row.get('path'))):
            continue
        clean={k:row[k] for k in ('code','path','actual_type')}
        for field in ('length','limit'):
            if type(row.get(field)) is int and 0<=row[field]<=1000000:
                clean[field]=row[field]
        result.append(clean)
    return result


def reject(records,code,path,value=MISSING,limit=None):
    if isinstance(records,list) and len(records)<8:
        kind=('missing' if value is MISSING else 'null' if value is None else 'boolean' if type(value) is bool
            else 'integer' if type(value) is int else 'number' if type(value) is float else 'string' if isinstance(value,str)
            else 'array' if isinstance(value,(list,tuple)) else 'object' if isinstance(value,dict) else 'unknown')
        row=dict(code=code,path=path,actual_type=kind)
        if isinstance(value,(str,list,tuple,dict)):
            row['length']=min(len(value),1000000)
        if type(limit) is int and 0<=limit<=1000000:
            row['limit']=limit
        records.extend(project([row]))
    return None
