"""Fail-closed renderer for existing fixed PostgreSQL row-permission rules.

An applicable but unresolvable policy is an error, never an absent predicate.
Variable-based policies are explicitly denied until a typed resolver is added.
"""
from fastapi import HTTPException

from apps.metrics.compiler import quote_identifier


def strict_row_filter(session, user, policies, ds, table_id):
    from apps.datasource.models.datasource import CoreField

    def deny():
        raise HTTPException(403, '行权限规则为空、不完整或含暂不支持的变量条件，请联系管理员修正；查询未执行')

    def literal(value):
        if not isinstance(value, (str, int, float)) or isinstance(value, bool):
            deny()
        # PostgreSQL standard_conforming_strings is enabled by the executor.
        return "'" + str(value).replace("'", "''") + "'"

    def leaf(item):
        try:
            field = session.get(CoreField, int(item['field_id']))
        except (KeyError, ValueError, TypeError):
            deny()
        if field is None or field.ds_id != ds.id or field.table_id != table_id:
            deny()
        name = quote_identifier(field.field_name)
        term = item.get('term')
        if item.get('value_type') not in (None, '', 'normal'):
            deny()
        if item.get('filter_type') == 'enum':
            values = item.get('enum_value')
            operator = {None: 'IN', '': 'IN', 'eq': 'IN', 'in': 'IN', 'not_eq': 'NOT IN', 'not in': 'NOT IN'}.get(term)
            if not operator or not isinstance(values, list) or not values:
                deny()
            return f'{name} {operator} ({", ".join(literal(value) for value in values)})'
        if item.get('filter_type') not in (None, '', 'condition', 'logic', 'custom'):
            deny()
        if term in ('null', 'not_null'):
            return name + (' IS NULL' if term == 'null' else ' IS NOT NULL')
        if term in ('empty', 'not_empty'):
            return name + (" = ''" if term == 'empty' else " <> ''")
        value = item.get('value')
        if term in ('in', 'not in'):
            if not isinstance(value, str) or not value:
                deny()
            return f'{name} {term.upper()} ({", ".join(literal(part) for part in value.split(","))})'
        if term in ('like', 'not like'):
            if not isinstance(value, str):
                deny()
            return f'{name} {term.upper()} {literal("%" + value + "%")}'
        operator = {'eq': '=', 'not_eq': '<>', 'lt': '<', 'le': '<=', 'gt': '>', 'ge': '>='}.get(term)
        if operator is None or value is None:
            deny()
        return f'{name} {operator} {literal(value)}'

    def tree(node, depth=0):
        if depth > 20 or not isinstance(node, dict):
            deny()
        logic = str(node.get('logic', '')).upper()
        items = node.get('items')
        if logic not in ('AND', 'OR') or not isinstance(items, list) or not items:
            deny()
        clauses = []
        for item in items:
            if not isinstance(item, dict):
                deny()
            if item.get('type') == 'tree':
                clauses.append(tree(item.get('sub_tree'), depth + 1))
            elif item.get('type') == 'item':
                clauses.append(leaf(item))
            else:
                deny()
        return '(' + f' {logic} '.join(clauses) + ')'

    return ' AND '.join(tree(policy.tree) for policy in policies)
