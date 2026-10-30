"""Grounded episode decisions; a closed issue alone never proves a successful fix."""
QUESTION = 'Anh/Chị cho Em biết lỗi này chưa từng hết, hay đã chạy tốt rồi mới xảy ra lại?'


def validate_lifecycle(value):
    if not isinstance(value, dict) or value.get('kind') not in ('evidence', 'recurrence', 'unclear'):
        return None
    result = {'kind': value['kind']}
    for field in ('knownworking_source_ids', 'recurrence_source_ids'):
        ids = value.get(field, [])
        if (not isinstance(ids, list) or len(ids) > 40 or
                any(type(i) is not int or i <= 0 for i in ids) or len(set(ids)) != len(ids)):
            return None
        result[field] = ids
    for field in ('knownworking_quote','recurrence_quote'):
        quote=value.get(field,'')
        if not isinstance(quote,str) or len(quote)>200:
            return None
        result[field]=quote
    temporal=value.get('temporal_order_confirmed',False)
    if type(temporal) is not bool:
        return None
    result['temporal_order_confirmed']=temporal
    return result


def closed_decision(value, sources, reply, incoming_ids=None):
    """Only supplied chronological evidence can authorize a distinct recurrence episode."""
    lifecycle = value.get('lifecycle') or {}
    if lifecycle.get('kind') == 'evidence':
        return 'evidence'
    if lifecycle.get('kind') != 'recurrence':
        return 'unclear'
    lookup = {m.message_id: m for m in sources + [reply]}
    working = lifecycle.get('knownworking_source_ids', [])
    recurring = lifecycle.get('recurrence_source_ids', [])
    analysis = value.get('case_analysis')
    incoming_ids=set(incoming_ids) if incoming_ids is not None else {reply.message_id}
    if (not working or not recurring or
            not set(working + recurring) <= set(lookup) or not set(recurring)&incoming_ids or
            analysis is None or not analysis['key_message_ids'] or
            not set(analysis['key_message_ids']) <= set(recurring) or
            any(not lookup[i].text.strip() for i in working)):
        return 'unclear'
    shared=set(working)&set(recurring)
    if shared:
        success=lifecycle.get('knownworking_quote','').strip()
        failure=lifecycle.get('recurrence_quote','').strip()
        if (set(working)!=set(recurring) or len(shared)!=1 or not shared<=incoming_ids
                or not lifecycle.get('temporal_order_confirmed') or not success or not failure or success==failure
                or not all(success in lookup[i].text and failure in lookup[i].text for i in shared)):
            return 'unclear'
    elif (max((lookup[i].received_at, lookup[i].message_id) for i in working) >=
          min((lookup[i].received_at, lookup[i].message_id) for i in recurring)):
        return 'unclear'
    return 'recurrence'
