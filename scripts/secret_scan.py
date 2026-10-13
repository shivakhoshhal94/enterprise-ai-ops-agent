"""Deterministic nonignored-file scan. Findings print file/rule, never matched values."""

import json
from pathlib import Path
import re
import subprocess
import sys
from sqlalchemy.engine import make_url
from enterprise_ai_ops_agent.config import Settings


def main():
    root=Path(__file__).resolve().parents[1]
    settings=Settings(_env_file=(root/'.env',root/'local.env'))
    secrets=[]
    for name in ('openai_api_key','approval_api_key','database_url','analytics_database_url','knowledge_database_url'):
        value=getattr(settings,name)
        if value:
            text=value.get_secret_value()
            if len(text)>=8 and 'replace_with_' not in text:secrets.append(text)
            if name.endswith('database_url'):
                password=make_url(text).password
                if password and len(password)>=8 and 'replace_with_' not in password:secrets.append(password)
    paths=subprocess.run(['git','ls-files','--cached','--others','--exclude-standard','-z'],cwd=root,
                         check=True,capture_output=True).stdout.decode().split('\0')
    findings=[];fixtures=0;count=0
    for name in set(paths):
        path=root/name
        if not path.is_file():continue
        try:text=path.read_text(encoding='utf-8-sig')
        except (UnicodeError,OSError):continue
        count+=1
        if any(value in text for value in secrets):findings.append({'file':name,'rule':'configured-secret-value'})
        for match in re.finditer(r'\bsk-[A-Za-z0-9_-]{20,}',text):
            value=match.group()
            if not any(word in value.lower() for word in ('fake','test','unit','example','placeholder')):
                findings.append({'file':name,'rule':'provider-key-pattern'})
        for match in re.finditer(r'postgresql(?:\+psycopg)?://[^\s"\'<>]+:[^\s"\'<>]+@',text):
            value=match.group().lower()
            if name.startswith('tests/') or any(word in value for word in ('replace','fixture','fake','example','password','secret','unit')):
                fixtures+=1
            else:findings.append({'file':name,'rule':'non-placeholder-database-credentials'})
    print(json.dumps({'scanned_text_files':count,'findings':findings,'placeholder_fixture_urls':fixtures,
                      'scope':'tracked and nonignored files; ignored local secrets are not printed or modified'}))
    return 1 if findings else 0


if __name__=='__main__':
    try:raise SystemExit(main())
    except Exception:
        print('Secret scan failed; check local configuration and Git availability.',file=sys.stderr)
        raise SystemExit(1)
