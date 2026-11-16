#!/usr/bin/env python3
"""Preflight for publishing ResolveOS: checks indexed files for secret-like names/content.
Does not print matching lines, only paths. Not a substitute for manual review.
"""
import subprocess, pathlib, re, sys
root=pathlib.Path(__file__).resolve().parent.parent
try:
  entries=subprocess.check_output(['git','ls-files','--cached','-z'],cwd=root).decode().split('\0')
except subprocess.CalledProcessError:
  print('Run from an initialized git repo with files staged.');sys.exit(2)
files=[s for s in entries if s]
bad_names=[];suspect_content=[]
name_re=re.compile(r'(^|/)(?:\.env(?:\..*)?|\.env\.keys|id_rsa|id_ed25519|[^/]*\.(?:pem|p12|pfx|key)|credentials(?:[._-].*)?\.json|service.account.*\.json)$',re.I)
content_res=[
  re.compile(rb'AIza[0-9A-Za-z_-]{25,}'),
  re.compile(rb'gh[pousr]_[0-9A-Za-z]{25,}'),
  re.compile(rb'sk-[0-9A-Za-z_-]{24,}'),
  re.compile(rb'rk_(?:admin|agent|operator|approver)_[A-Za-z0-9]{20,}'),
  re.compile(rb'-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----'),
]
for s in files:
  path=root/s
  if name_re.search(s) and not s.endswith('.env.example') and not s.endswith('.env.prod.example'):
    bad_names.append(s)
  if not path.is_file() or path.stat().st_size>2_500_000:continue
  raw=path.read_bytes()
  if any(p.search(raw) for p in content_res):suspect_content.append(s)
print(f'Inspected {len(files)} tracked/staged files.')
if bad_names: print('STOP: secret-like staged paths:',*bad_names,sep='\n - ')
if suspect_content: print('STOP: secret-like content in:',*suspect_content,sep='\n - ')
if bad_names or suspect_content:
  print('Unstage affected files, rotate exposed secrets, inspect git history before pushing.')
  sys.exit(1)
print('No matching secret patterns detected. Review staged changes manually too.')
