#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

ENV_FILE=.env
if [ ! -f "$ENV_FILE" ]; then
  cp .env.example "$ENV_FILE"
fi

read -rsp "请输入旺店通 AppSecret（不会回显）: " WDT_SECRET
echo
python3 - "$ENV_FILE" "$WDT_SECRET" <<'PY'
from pathlib import Path
import sys
path=Path(sys.argv[1])
secret=sys.argv[2]
text=path.read_text(encoding='utf-8')
updates={
    'WDT_SID':'jxf',
    'WDT_APPKEY':'jxf-gw',
    'WDT_APPSECRET':secret,
    'WDT_BASE_URL':'https://api.wangdian.cn/openapi2',
}
lines=text.splitlines()
out=[]
seen=set()
for line in lines:
    key=line.split('=',1)[0] if '=' in line else ''
    if key in updates:
        out.append(f'{key}={updates[key]}')
        seen.add(key)
    else:
        out.append(line)
for key,val in updates.items():
    if key not in seen:
        out.append(f'{key}={val}')
path.write_text('\n'.join(out)+'\n', encoding='utf-8')
PY
chmod 600 "$ENV_FILE"
echo "旺店通正式环境参数已写入 .env（权限 600）。"
echo "启动后访问：http://localhost:8010/api/wdt/test 验证连接。"
