"""Model-defined, bounded read-only task drafts; legacy analysis remains compatible."""
from .models import utf8_valid
from .analysis_choices import valid_options, MISSING
from .group_notices import clean_analysis

TASK_SCHEMA={'type':'object','properties':{
    'title':{'type':'string','minLength':1,'maxLength':90,'pattern':r'\S'},
    'work':{'type':'string','minLength':1,'maxLength':160,'pattern':r'\S'},
    'acceptance':{'type':'array','minItems':1,'maxItems':2,'uniqueItems':True,
        'items':{'type':'string','minLength':1,'maxLength':90,'pattern':r'\S'}}},
    'required':['title','work','acceptance'],'additionalProperties':False}


def single_line(value):
    return ' '.join(clean_analysis(value).split())


def projection(task,diagnostics=None,path='task'):
    from .ingress_diagnostics import reject,MISSING
    def bad(code,field='',value=MISSING,limit=None):
        reject(diagnostics,code,path+('.'+field if field else ''),value,limit=limit)
        return None
    if not isinstance(task,dict):
        return bad('type',value=task)
    fields={'title','work','acceptance'}
    if set(task)-fields:
        return bad('additional_field',value=task)
    if fields-set(task):
        return bad('required',sorted(fields-set(task))[0])
    output={}
    for field,cap in [('title',90),('work',160)]:
        value=task[field]
        if not isinstance(value,str):
            return bad('type',field,value)
        if not utf8_valid(value) or not value.strip():
            return bad('invalid_value',field,value)
        if len(value)>cap:
            return bad('max_length',field,value,cap)
        clean=single_line(value)
        if not clean:
            return bad('invalid_value',field,clean)
        if len(clean)>cap:
            return bad('max_length',field,clean,cap)
        output[field]=clean
    criteria=task['acceptance']
    if not isinstance(criteria,list):
        return bad('type','acceptance',criteria)
    if len(criteria) not in (1,2):
        return bad('max_items','acceptance',criteria,2)
    clean=[]
    for index,value in enumerate(criteria):
        field=f'acceptance[{index}]'
        if not isinstance(value,str):
            return bad('type',field,value)
        if not utf8_valid(value) or not value.strip():
            return bad('invalid_value',field,value)
        if len(value)>90:
            return bad('max_length',field,value,90)
        text=single_line(value)
        if not text:
            return bad('invalid_value',field,text)
        if len(text)>90:
            return bad('max_length',field,text,90)
        if text.casefold() in {c.casefold() for c in clean}:
            return bad('invalid_value',field,text)
        clean.append(text)
    output['acceptance']=clean
    return output


def valid_task(task,diagnostics=None,path='task'):
    return projection(task,diagnostics=diagnostics,path=path) is not None


def valid_payload(group,diagnostics=None,path=""):
    """Shared live/cache parser contract; absent task retains the old answer format."""
    action=group.get('action')
    options=group.get('options',[])
    if not valid_options(options,action,group.get('needs_choice',MISSING)):
        return False
    answer=group.get('answer','')
    if not utf8_valid(answer) or len(answer)>1500:
        return False
    if action=='STATUS':
        return (not set(group)-{'message_ids','action','candidate_id','confidence','question','analysis',
                    'answer','options','needs_choice','candidate','host_repo'} and not options and not answer.strip()
                and group.get('candidate_id') is None and group.get('question')==''
                and len(group.get('message_ids',[]))==1)
    if 'task' in group:
        from .ingress_diagnostics import reject
        prefix=path+'.' if path else ''
        if action!='ANALYZE':
            reject(diagnostics,'forbidden_field',prefix+'task',group['task'])
            return False
        if options or group.get('candidate_id') is not None:
            field='options' if options else 'candidate_id'
            reject(diagnostics,'invalid_value',prefix+field,group.get(field))
            return False
        return valid_task(group['task'],diagnostics=diagnostics,path=prefix+'task')
    if action=='ANALYZE':
        return bool(answer.strip()) and group.get('candidate_id') is None
    return not answer.strip()


def render(task):
    value=projection(task)
    if value is None:
        raise ValueError('Invalid analysis task')
    text='Việc: '+value['title']+'\nThực hiện: '+value['work']+'\nĐạt: '+'; '.join(value['acceptance'])
    if len(text)>500:
        raise ValueError('Analysis task exceeds notice budget')
    return text
