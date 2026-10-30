"""Semantic follow-up boundary, parsed independently of all transport and durable state."""
import json
from .models import Classification, TICKET_TYPES, utf8_valid

MAX_SPLIT=5
FOLLOWUP_SCHEMA={"type":"object","properties":{
    "action":{"type":"string","enum":["update","split","clarify","chat"]},
    "confidence":{"type":"number"}, "target_ordinal":{"type":["integer","null"]},
    "summary":{"type":"string"}, "questions":{"type":"array","items":{"type":"string"}},
    "groups":{"type":"array","maxItems":MAX_SPLIT,"items":{"type":"object","properties":{
        "message_ids":{"type":"array","items":{"type":"integer"}},
        "title":{"type":"string"},"summary":{"type":"string"},
        "type":{"type":"string","enum":list(TICKET_TYPES)},
        "priority":{"type":"string","enum":["low","medium","high"]},
        "missing_info":{"type":"array","items":{"type":"string"}}},
        "required":["message_ids","title","summary","type","priority","missing_info"]}}},
    "required":["action","confidence","target_ordinal","summary","questions","groups"]}


FOLLOWUP_SCHEMA['properties']['source_roles']={'type':'array','maxItems':40,'items':{'type':'object',
    'properties':{'message_id':{'type':'integer'},'role':{'type':'string','enum':['ancillary','request','unknown']},
                  'confidence':{'type':'number'}},'required':['message_id','role','confidence']}}
FOLLOWUP_SCHEMA['properties']['unresolved_message_ids']={'type':'array','items':{'type':'integer'},'maxItems':40}
FOLLOWUP_SCHEMA['properties']['desired_count']={"type":["integer","null"],"minimum":2,"maximum":MAX_SPLIT}
FOLLOWUP_SCHEMA['properties']['bindings']={"type":"array","maxItems":MAX_SPLIT,"items":{
    "type":"object","properties":{"ordinal":{"type":"integer","minimum":0,"maximum":MAX_SPLIT-1},
    "message_ids":{"type":"array","items":{"type":"integer"}}},"required":["ordinal","message_ids"]}}


def case_analysis_data(value):
    """JSON-safe validated case projection; no model repo/issue/tool authority crosses here."""
    from .classifier import parse_case_analysis, CASE_SCHEMA
    from .models import canonical_category
    if (not isinstance(value,dict) or any(key not in value for key in CASE_SCHEMA['required'])
            or value.get('actionable') is not True or canonical_category(value.get('category')) not in ('bug','task','feedback')
            or canonical_category(value.get('category'))!=canonical_category(value.get('type'))):
        return None
    parsed=parse_case_analysis(json.dumps(value,ensure_ascii=False))
    if parsed is None or parsed.verdict!='ticket' or parsed.classification is None:
        return None
    c=parsed.classification
    return dict(verdict=parsed.verdict,category=parsed.category,type=c.type,confidence=c.confidence,
                title=c.title[:300],summary=c.summary[:2000],priority=c.priority,actionable=parsed.actionable,
                key_message_ids=list(parsed.key_message_ids),missing_info=list(parsed.missing_info),
                reason=parsed.reason or '',details={k:list(v) if isinstance(v,tuple) else v for k,v in parsed.details})


def validate_followup(value):
    if not isinstance(value,dict) or value.get('action') not in ('update','split','clarify','chat'):
        return None
    confidence=value.get('confidence')
    if type(confidence) not in (int,float) or not 0 <= confidence <= 1:
        return None
    target=value.get('target_ordinal')
    if target is not None and (type(target) is not int or not 0 <= target < MAX_SPLIT):
        return None
    summary=value.get('summary','')
    questions=value.get('questions',[])
    groups=value.get('groups',[])
    if (not utf8_valid(summary) or not isinstance(questions,list)
            or any(not utf8_valid(q) for q in questions) or not isinstance(groups,list) or len(groups)>MAX_SPLIT):
        return None
    result=dict(action=value['action'],confidence=confidence,target_ordinal=target,
                summary=summary[:2000],questions=[q[:200] for q in questions[:3]],groups=[])
    if value.get('lifecycle') is not None:
        from .lifecycle import validate_lifecycle
        result['lifecycle']=validate_lifecycle(value['lifecycle'])
        if result['lifecycle'] is None:
            return None
    if value.get('case_analysis') is not None:
        analysis=case_analysis_data(value['case_analysis'])
        if value['action']!='update' or analysis is None:
            return None
        result['case_analysis']=analysis
    roles=value.get('source_roles',[])
    if not isinstance(roles,list) or len(roles)>40:
        return None
    result['source_roles']=[]
    for role in roles:
        if (not isinstance(role,dict) or type(role.get('message_id')) is not int
                or role.get('role') not in ('ancillary','request','unknown')
                or type(role.get('confidence')) not in (int,float) or not 0<=role['confidence']<=1):
            return None
        result['source_roles'].append({k:role[k] for k in ('message_id','role','confidence')})
    unresolved=value.get('unresolved_message_ids',[])
    if not isinstance(unresolved,list) or len(unresolved)>40 or any(type(i) is not int for i in unresolved):
        return None
    result['unresolved_message_ids']=unresolved
    count=value.get('desired_count')
    bindings=value.get('bindings',[])
    if count is not None and (type(count) is not int or not 2<=count<=MAX_SPLIT):
        return None
    if not isinstance(bindings,list) or len(bindings)>MAX_SPLIT:
        return None
    result['desired_count']=count
    result['bindings']=[]
    for binding in bindings:
        if (not isinstance(binding,dict) or type(binding.get('ordinal')) is not int
                or not 0<=binding['ordinal']<MAX_SPLIT or not isinstance(binding.get('message_ids'),list)
                or not binding['message_ids'] or len(binding['message_ids'])>40
                or any(type(i) is not int for i in binding['message_ids'])):
            return None
        result['bindings'].append(dict(ordinal=binding['ordinal'],message_ids=binding['message_ids']))
    for group in groups:
        if not isinstance(group,dict):
            return None
        ids=group.get('message_ids'); missing=group.get('missing_info',[])
        c=Classification(group.get('type'),confidence,group.get('title'),group.get('summary'),group.get('priority'))
        if (not isinstance(ids,list) or not ids or any(type(i) is not int for i in ids)
                or not c.is_valid() or c.type not in TICKET_TYPES or not isinstance(missing,list)
                or any(not utf8_valid(q) for q in missing)):
            return None
        result['groups'].append(dict(message_ids=ids,title=c.title[:300],summary=c.summary[:2000],
                                     type=c.type,priority=c.priority,missing_info=[q[:200] for q in missing[:3]]))
    if result['action']=='split' and len(groups)<2:
        return None
    if result['action']=='update' and not summary.strip():
        return None
    return result


def parse_followup(content):
    from .classifier import strip_thinking
    try:
        return validate_followup(json.loads(strip_thinking(content)))
    except (ValueError,TypeError):
        return None

# Keep final JSON comfortably inside the existing provider output budget.
FOLLOWUP_SCHEMA['properties']['summary']['maxLength']=300
FOLLOWUP_SCHEMA['properties']['questions'].update(maxItems=3)
FOLLOWUP_SCHEMA['properties']['questions']['items']['maxLength']=180
for field in ('title','summary'):
    FOLLOWUP_SCHEMA['properties']['groups']['items']['properties'][field]['maxLength']=200 if field=='title' else 300


ROLE_SCHEMA={"type":"object","properties":{"source_roles":FOLLOWUP_SCHEMA['properties']['source_roles']},
             "required":["source_roles"],"additionalProperties":False}


def parse_source_roles(content):
    from .classifier import strip_thinking
    try:
        data=json.loads(strip_thinking(content))
    except (ValueError,TypeError):
        return None
    if not isinstance(data,dict) or 'source_roles' not in data:
        return None
    checked=validate_followup(dict(action='clarify',confidence=1.0,target_ordinal=None,
                                  summary='',questions=[],groups=[],source_roles=data['source_roles']))
    return {'source_roles':checked['source_roles']} if checked is not None else None

# Optional authoritative-shape draft, still untrusted until source/snapshot guards in the engine.
from .classifier import CASE_SCHEMA
FOLLOWUP_SCHEMA['properties']['case_analysis']={'anyOf':[CASE_SCHEMA,{'type':'null'}]}
FOLLOWUP_SCHEMA['properties']['lifecycle']={'type':'object','properties':{
    'kind':{'type':'string','enum':['evidence','recurrence','unclear']},
    'knownworking_quote':{'type':'string','maxLength':200},
    'recurrence_quote':{'type':'string','maxLength':200},
    'temporal_order_confirmed':{'type':'boolean'},
    'knownworking_source_ids':{'type':'array','maxItems':40,'items':{'type':'integer','minimum':1}},
    'recurrence_source_ids':{'type':'array','maxItems':40,'items':{'type':'integer','minimum':1}}},
    'required':['kind','knownworking_source_ids','recurrence_source_ids']}
