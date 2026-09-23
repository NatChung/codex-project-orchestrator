import os
from pathlib import Path
p=Path(__file__).resolve().parent
os.chdir(p/'vendor/mcp_agent_mail')
os.environ.update(STORAGE_ROOT=str(p/'mail-storage'),DATABASE_URL='sqlite+aiosqlite:///'+str(p/'mail.sqlite3'),HTTP_HOST='127.0.0.1',HTTP_RBAC_DEFAULT_ROLE='writer',HTTP_PORT='18766',HTTP_ALLOW_LOCALHOST_UNAUTHENTICATED='false',HTTP_BEARER_TOKEN=(p/'http-token').read_text().strip(),ALLOW_ABSOLUTE_ATTACHMENT_PATHS='false',LLM_ENABLED='false',TOOLS_FILTER_ENABLED='true',TOOLS_FILTER_PROFILE='custom',TOOLS_FILTER_MODE='include',AUTO_RETIRE_STALE_AGENTS_ENABLED='false',TOOLS_FILTER_TOOLS='unretire_agent,retire_agent,health_check,ensure_project,register_agent,whois,send_message,fetch_inbox,acknowledge_message,set_contact_policy',TOOLS_FILTER_CLUSTERS='')
os.execv('/usr/bin/sandbox-exec',['sandbox-exec','-f',str(p/'mail.sb'),str(p/'vendor/mcp_agent_mail/.venv/bin/python'),'-m','mcp_agent_mail.cli','serve-http','--host','127.0.0.1','--port','18766'])
