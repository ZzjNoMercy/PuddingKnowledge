"""Strict bounded-structure JSON shared by Wiki input adapters."""
import json


def decode_request(data):
    def pairs(items):
        result={}
        for key,value in items:
            if key in result:raise ValueError('Duplicate JSON key')
            result[key]=value
        return result
    value=json.loads(data,object_pairs_hook=pairs,parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Invalid number')))
    stack=[(value,0)];count=0
    while stack:
        item,depth=stack.pop();count+=1
        if depth>32 or count>100000:raise ValueError('JSON structure budget exceeded')
        if isinstance(item,dict):stack.extend((v,depth+1) for v in item.values())
        elif isinstance(item,list):stack.extend((v,depth+1) for v in item)
    return value

