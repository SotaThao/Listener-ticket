"""Strict route plus canonical draft parser. Candidate IDs are opaque host references."""
import json
import math
from copy import deepcopy
from .analysis_task import TASK_SCHEMA, valid_payload
from .classifier import CASE_SCHEMA, parse_case_analysis, strip_thinking

INGRESS_ANALYSIS_SCHEMA=deepcopy(CASE_SCHEMA)
INGRESS_ANALYSIS_SCHEMA['additionalProperties']=False
INGRESS_ANALYSIS_SCHEMA['properties']['details']['additionalProperties']=False
INGRESS_ANALYSIS_SCHEMA['properties']['title']['maxLength']=200
INGRESS_ANALYSIS_SCHEMA['properties']['summary']['maxLength']=500
INGRESS_ANALYSIS_SCHEMA['properties']['reason']['maxLength']=300
INGRESS_ANALYSIS_SCHEMA['properties']['missing_info']['maxItems']=3

INGRESS_SCHEMA={"type":"object","properties":{"groups":{"type":"array","items":{
    "type":"object","properties":{
        "message_ids":{"type":"array","items":{"type":"integer"}},
        "action":{"type":"string","enum":["NEW","CONTINUE","CLARIFY","CHAT","ANALYZE","STATUS"]},
        "candidate_id":{"type":["string","null"]},"confidence":{"type":"number"},
        "question":{"type":"string"},"answer":{"type":"string","maxLength":1500},"needs_choice":{"const":False},"options":{"type":"array","maxItems":0},"task":TASK_SCHEMA,"analysis":INGRESS_ANALYSIS_SCHEMA},
    "required":["message_ids","action","candidate_id","confidence","question","analysis"],
    "additionalProperties":False}}},"required":["groups"],"additionalProperties":False}

# Equivalent detail-key groups avoid repeating identical leaf schemas in the 8KB wire budget.
wire_analysis=deepcopy(INGRESS_ANALYSIS_SCHEMA)
fields=wire_analysis['properties']['details']['properties']
patterns={}
for kind in ('string','array'):
    names=[name for name,value in fields.items() if value['type']==kind]
    patterns['^('+'|'.join(names)+r')(?![\s\S])']=deepcopy(fields[names[0]])
wire_analysis['properties']['details']={'type':'object','patternProperties':patterns,'additionalProperties':False}
INGRESS_SCHEMA['properties']['groups']['items']['properties']['analysis']=wire_analysis


# Fresh provider output uses a task; default parsing below still accepts legacy records.
INGRESS_SCHEMA['properties']['groups']['items']['allOf']=[{
    'if':{'properties':{'action':{'const':'ANALYZE'}}},
    'then':{'required':['task'],'properties':{'candidate_id':{'const':None}}},
    'else':{'not':{'required':['task']}}}]


def parse_ingress(content,diagnostics=None,*,require_analyze_task=False):
    from .ingress_diagnostics import reject
    def fail(code,path,value=None):
        return reject(diagnostics,code,path,value)
    try:
        data=json.loads(strip_thinking(content))
        if not isinstance(data,dict):
            return fail('type','root',data)
        if set(data)!={'groups'}:
            return fail('additional_field','root',data)
        if not isinstance(data['groups'],list):
            return fail('type','groups',data['groups'])
        if not 1<=len(data['groups'])<=20:
            return fail('max_items','groups',data['groups'])
        result=[]
        allowed={'message_ids','action','candidate_id','confidence','question','analysis','answer','options','needs_choice','task'}
        required={'message_ids','action','candidate_id','confidence','question','analysis'}
        for index,group in enumerate(data['groups']):
            path=f'groups[{index}]'
            if not isinstance(group,dict):
                return fail('type',path,group)
            if set(group)-allowed:
                return fail('additional_field',path,group)
            if required-set(group):
                field=sorted(required-set(group))[0]
                return reject(diagnostics,'required',path+'.'+field)
            ids=group['message_ids'];confidence=group['confidence']
            if not isinstance(ids,list) or not ids or any(type(i) is not int for i in ids) or len(ids)!=len(set(ids)):
                return fail('invalid_value',path+'.message_ids',ids)
            if group['action'] not in ('NEW','CONTINUE','CLARIFY','CHAT','ANALYZE','STATUS'):
                return fail('invalid_value',path+'.action',group['action'])
            if type(confidence) not in (int,float) or not math.isfinite(confidence) or not 0<=confidence<=1:
                return fail('invalid_value',path+'.confidence',confidence)
            if not isinstance(group['question'],str) or len(group['question'])>500:
                return fail('invalid_value',path+'.question',group['question'])
            if group['candidate_id'] is not None and not isinstance(group['candidate_id'],str):
                return fail('type',path+'.candidate_id',group['candidate_id'])
            if require_analyze_task and group['action']=='ANALYZE' and 'task' not in group:
                return reject(diagnostics,'required',path+'.task')
            before=len(diagnostics) if isinstance(diagnostics,list) else 0
            if not valid_payload(group,diagnostics=diagnostics,path=path):
                if isinstance(diagnostics,list) and len(diagnostics)>before:
                    return None
                field=('task' if 'task' in group else 'options' if group.get('options') else 'needs_choice' if 'needs_choice' in group else 'answer')
                return fail('invalid_value',path+'.'+field,group.get(field))
            raw=group['analysis']
            if not isinstance(raw,dict):
                return fail('type',path+'.analysis',raw)
            if set(raw)-set(INGRESS_ANALYSIS_SCHEMA['properties']):
                return fail('additional_field',path+'.analysis',raw)
            details=raw.get('details',{})
            if not isinstance(details,dict):
                return fail('type',path+'.analysis.details',details)
            if set(details)-set(INGRESS_ANALYSIS_SCHEMA['properties']['details']['properties']):
                return fail('additional_field',path+'.analysis.details',details)
            for field,cap in (('title',200),('summary',500),('reason',300)):
                value=raw.get(field,'')
                if not isinstance(value,str):
                    return fail('type',path+'.analysis.'+field,value)
                if len(value)>cap:
                    return fail('max_length',path+'.analysis.'+field,value)
            missing=raw.get('missing_info',[])
            if not isinstance(missing,list) or len(missing)>3:
                return fail('max_items',path+'.analysis.missing_info',missing)
            analysis=parse_case_analysis(json.dumps(raw,ensure_ascii=False))
            if analysis is None:
                return fail('canonical_invalid',path+'.analysis',raw)
            result.append(dict(group,analysis=analysis))
        return result
    except (ValueError,TypeError,KeyError):
        return reject(diagnostics,'json_invalid','root')


CRITIC_SCHEMA={'type':'object','properties':{'reviews':{'type':'array','minItems':1,'maxItems':20,
    'items':{'type':'object','properties':{'group_index':{'type':'integer','minimum':0,'maximum':19},
        'verdict':{'type':'string','enum':['approve','context','reject']}},
        'required':['group_index','verdict'],'additionalProperties':False}}},
    'required':['reviews'],'additionalProperties':False}


def parse_critic(content,group_count):
    """Only bounded indices/verdicts cross this boundary; valid rejection is not a format error."""
    try:
        value=json.loads(strip_thinking(content))
        if type(group_count) is not int or not 1<=group_count<=20 or not isinstance(value,dict) or set(value)!={'reviews'}:
            return None
        rows=value['reviews']
        if not isinstance(rows,list) or len(rows)!=group_count:
            return None
        verdicts={}
        for row in rows:
            if not isinstance(row,dict) or set(row)!={'group_index','verdict'}:
                return None
            index=row['group_index'];verdict=row['verdict']
            if type(index) is not int or not 0<=index<group_count or index in verdicts:
                return None
            if not isinstance(verdict,str) or verdict not in ('approve','context','reject'):
                return None
            verdicts[index]=verdict
        return [verdicts[i] for i in range(group_count)]
    except (ValueError,TypeError,KeyError):
        return None
