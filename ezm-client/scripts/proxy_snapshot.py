#!/usr/bin/env python3
"""Serialize a shell's pre-proxy state as data for child-shell restoration."""
import json
import os
import sys

NAMES = ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy',
         'NO_PROXY', 'no_proxy', 'PROXYCHAINS_CONF_FILE', 'PROXYCHAINS_QUIET_MODE')


def validate_record(record):
    if not isinstance(record, dict) or set(record) != {'set', 'exported', 'value'}:
        raise ValueError('恢复快照字段不完整')
    if type(record['set']) is not bool or type(record['exported']) is not bool:
        raise ValueError('恢复快照标记无效')
    if not isinstance(record['value'], str) or '\0' in record['value']:
        raise ValueError('恢复快照变量值无效')
    if not record['set'] and (record['exported'] or record['value']):
        raise ValueError('恢复快照状态不一致')
    return record


def main():
    action, root = sys.argv[1:]
    if action == 'encode':
        fields = sys.stdin.buffer.read().decode('utf-8', 'surrogateescape').split('\0')
        if fields.pop() != '' or len(fields) != (len(NAMES) + 1) * 3:
            raise ValueError('恢复快照字段数量无效')
        records = []
        for offset in range(0, len(fields), 3):
            was_set, exported, value = fields[offset:offset + 3]
            if was_set not in ('0', '1') or exported not in ('0', '1'):
                raise ValueError('恢复快照标记无效')
            records.append(validate_record({'set': was_set == '1', 'exported': exported == '1', 'value': value}))
        print(json.dumps({'version': 1, 'root': root, 'variables': dict(zip(NAMES, records[:-1])),
                          'preload': records[-1]}, ensure_ascii=True, separators=(',', ':')))
    elif action == 'decode':
        state = json.loads(os.environ['_MIHOMO_PROXY_SNAPSHOT'])
        if not isinstance(state, dict) or type(state.get('version')) is not int or state['version'] != 1:
            raise ValueError('恢复快照版本无效')
        if state.get('root') != root:
            raise ValueError('恢复快照不属于当前项目，请先在原项目中关闭代理')
        if not isinstance(state.get('variables'), dict) or set(state['variables']) != set(NAMES):
            raise ValueError('恢复快照变量不完整')
        records = [validate_record(state['variables'][name]) for name in NAMES]
        records.append(validate_record(state['preload']))
        fields = []
        for record in records:
            fields.extend(('1' if record['set'] else '0', '1' if record['exported'] else '0', record['value']))
        fields.append('ok')  # No output is emitted until the entire payload is validated.
        sys.stdout.buffer.write(('\0'.join(fields) + '\0').encode('utf-8', 'surrogateescape'))
    else:
        raise ValueError('未知快照操作')


if __name__ == '__main__':
    try:
        main()
    except (ValueError, KeyError, TypeError, OSError) as error:
        sys.exit('无法读取或保存代理恢复快照：' + str(error))
