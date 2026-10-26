"""原始 DSL 的生产成本护栏；仅标准库，不访问网络或配置。"""
import copy
import re
from datetime import datetime, timedelta, timezone
import elk_core as core


def _instant(value, tz, now):
    value = core.parse_time(value, tz)
    if value == 'now':
        return now
    relative = re.fullmatch(r'now-(\d+)([smhdw])', value)
    if relative:
        return now - timedelta(seconds=int(relative[1]) * {'s':1,'m':60,'h':3600,'d':86400,'w':604800}[relative[2]])
    return datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(timezone.utc)


def _simple_query(query):
    if not isinstance(query, dict) or len(query) != 1:
        return False
    kind, value = next(iter(query.items()))
    if kind in {'match_all', 'match_none', 'term', 'terms', 'match', 'match_phrase', 'range', 'exists', 'ids', 'prefix'}:
        return isinstance(value, dict)
    if kind != 'bool' or not isinstance(value, dict):
        return False
    for key, children in value.items():
        if key in {'minimum_should_match', 'boost'}:
            continue
        if key not in {'must', 'filter', 'must_not', 'should'}:
            return False
        if not all(_simple_query(x) for x in (children if isinstance(children, list) else [children])):
            return False
    return True


def prepare(env, args, tz):
    body = copy.deepcopy(args['body'])
    api = args.get('api') or '_search'
    if api not in ('_search', '_count'):
        raise core.ElkError('DSL 仅支持 _search/_count')
    if env.get('production') and args.get('allow_slow') is not True:
        def reject():
            raise core.ElkError('生产 DSL 慢查询护栏：请缩小 since/until（默认近 15 分钟），使用简单查询；'
                                '复杂 DSL/聚合请先改用 elk_agg，确实无法缩小时显式 allow_slow=true')
        query = body.get('query', {'match_all': {}})
        if set(body) - {'query', 'size', 'timeout', '_source', 'track_total_hits', 'from'} or not _simple_query(query):
            reject()
        now = datetime.now(timezone.utc)
        try:
            start = _instant(args.get('since') or '15m', tz, now)
            end = _instant(args.get('until') or 'now', tz, now)
            if not 0 <= (end-start).total_seconds() <= float(env.get('slow_guard_days', 7)) * 86400:
                reject()
        except (ValueError, TypeError, OverflowError):
            reject()
        body['query'] = {'bool': {'must': [query], 'filter': [{'range': {
            env['_fields']['timestamp']: {'gte': start.isoformat(), 'lte': end.isoformat()}}}]}}
    elif args.get('since') is not None or args.get('until') is not None:
        now = datetime.now(timezone.utc)
        try:
            start = _instant(args.get('since') or '15m', tz, now)
            end = _instant(args.get('until') or 'now', tz, now)
        except (ValueError, TypeError, OverflowError):
            raise core.ElkError('since/until 不是有效的时间范围')
        if (end - start).total_seconds() < 0:
            raise core.ElkError('since 不能晚于 until')
        body['query'] = {'bool': {'must': [body.get('query', {'match_all': {}})], 'filter': [{'range': {
            env['_fields']['timestamp']: {'gte': start.isoformat(), 'lte': end.isoformat()}}}]}}
    if api == '_search':
        body['size'] = max(0, min(int(body.get('size', 50)), int(env.get('max_size', 500))))
        cap = max(1, min(int(env.get('search_timeout', 25)), int(env.get('timeout', 60))-5))
        requested = re.fullmatch(r'(\d+(?:\.\d+)?)(ms|s|m|h|d)', str(body.get('timeout', '')))
        if requested:
            seconds = float(requested[1]) * {'ms':.001,'s':1,'m':60,'h':3600,'d':86400}[requested[2]]
            if 0 < seconds <= cap:
                return body
        body['timeout'] = str(cap) + 's'
    return body
