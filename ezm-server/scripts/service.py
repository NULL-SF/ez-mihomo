#!/usr/bin/env python3
"""Manage only processes started by this project, with PID reuse checks."""
import argparse, json, os, re, signal, socket, subprocess, time, tempfile
from pathlib import Path
import sys
sys.dont_write_bytecode=True
ROOT=Path(__file__).resolve().parents[1]
SPECS=('vless','vmess','hysteria2','trojan','shadowsocks','tuic','anytls')

def state_file(p):
 return ROOT/'runtime'/p/('subscription.pid' if p=='subscription' else 'mihomo.pid')

def user_config_access(path,mode=0o600):
 """Keep project files owned by the installing user."""
 if path.is_symlink():return
 if os.geteuid()==0:
  owner=ROOT.stat()
  uid=owner.st_uid if owner.st_uid!=0 else int(os.environ.get('SUDO_UID',owner.st_uid))
  gid=owner.st_gid if owner.st_uid!=0 else int(os.environ.get('SUDO_GID',owner.st_gid))
  os.chown(path,uid,gid)
 path.chmod(mode)

def information_access():
 from permissions import restore_access
 restore_access(ROOT)

def command(p):
 if p=='subscription':return [sys.executable,str(ROOT/'scripts/subscription.py'),'run']
 return [str(ROOT/'bin/mihomo_1.19.33'),'-d',str(ROOT/'runtime'/p),'-f',str(ROOT/'runtime'/p/'config.json')]

def identity(pid):
 try:
  text=Path(f'/proc/{pid}/stat').read_text();fields=text[text.rfind(')')+2:].split()
  if fields[0]=='Z':return None
  return fields[19]
 except (OSError,IndexError):return None

def owned(p, report_permission=False):
 try:
  state=json.loads(state_file(p).read_text())
  if identity(state['pid'])!=state['start']:return None
  argv=Path(f'/proc/{state["pid"]}/cmdline').read_bytes().split(b'\0')
  if argv==[x.encode() for x in command(p)]+[b'']:return state['pid']
 except PermissionError:
  if report_permission:raise
 except (OSError,ValueError,KeyError,TypeError,IndexError):pass
 return None

def write_state(path,value):
 with tempfile.NamedTemporaryFile(dir=path.parent,mode='w',delete=False) as stream:
  temporary=Path(stream.name)
  try:
   user_config_access(temporary,0o600)
   json.dump(value,stream);stream.flush();os.fsync(stream.fileno())
  except BaseException:
   temporary.unlink(missing_ok=True);raise
 try:temporary.replace(path)
 finally:temporary.unlink(missing_ok=True)

def deployed_port(protocol):
 try:
  port=entry(protocol)['port']
 except PermissionError:return '需 sudo 查看'
 except FileNotFoundError:return '未部署'
 except (OSError,ValueError,KeyError,IndexError,TypeError):return '配置无效'
 return str(port) if type(port) is int and 1<=port<=65535 else '配置无效'

def transports(protocol):
 return ['udp'] if protocol in ('hysteria2','tuic') else ['tcp','udp'] if protocol=='shadowsocks' else ['tcp']

def entry(p):
 n=json.loads((ROOT/'runtime'/p/'config.json').read_text())['listeners'][0]
 return {'listen':n['listen'],'port':int(n['port']),'networks':transports(p)}

def preflight(p):
 item=entry(p)
 sockets=[]
 try:
  for n in item['networks']:
   s=socket.socket(socket.AF_INET6 if ':' in item['listen'] else socket.AF_INET,socket.SOCK_DGRAM if n=='udp' else socket.SOCK_STREAM);sockets.append(s)
   if n=='tcp':s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
   s.bind((item['listen'],item['port']))
 finally:
  for s in sockets:s.close()

def listening(p,pid):
 row=entry(p)
 proc=Path(f'/proc/{pid}');inodes=set()
 try:
  for fd in (proc/'fd').iterdir():
   try:target=fd.readlink().as_posix()
   except FileNotFoundError:continue
   if target.startswith('socket:['):inodes.add(target[8:-1])
  for network in row['networks']:
   found=False
   for suffix in ('','6'):
    table=proc/f'net/{network}{suffix}'
    if not table.exists():continue  # IPv6 may be disabled on the VPS.
    for line in table.read_text().splitlines()[1:]:
     fields=line.split()
     if fields[9] in inodes and int(fields[1].split(':')[1],16)==row['port'] and fields[3]==('0A' if network=='tcp' else '07'):found=True
   if not found:return False
  return True
 except (OSError,ValueError,IndexError):return False

def start(p):
 if not (ROOT/'runtime'/p/'config.json').is_file():raise ValueError('缺少服务端配置，请执行 sudo bash ./server.sh start 自动初始化（root 可省略 sudo）')
 if owned(p):print(p+': 已运行');return False
 preflight(p)
 env=dict(os.environ,SAFE_PATHS=str(ROOT/'runtime/certs'))
 (ROOT/'runtime'/p).mkdir(parents=True,exist_ok=True)
 for k in list(env):
  if k.lower() in ('http_proxy','https_proxy','all_proxy','no_proxy') or k in ('LD_PRELOAD','PROXYCHAINS_CONF_FILE','PROXYCHAINS_QUIET_MODE','_MIHOMO_PROXY_SNAPSHOT'):env.pop(k,None)
 with (ROOT/'log'/(p+'.log')).open('ab') as log:
  child=subprocess.Popen(command(p),cwd=ROOT,env=env,stdin=subprocess.DEVNULL,stdout=log,stderr=log,start_new_session=True,umask=0o077)
  state=state_file(p)
  try:
   tick=identity(child.pid)
   if not tick:raise ValueError(p+': 启动失败，查看 log/'+p+'.log')
   write_state(state,{'pid':child.pid,'start':tick})
   for _ in range(40):
    if child.poll() is not None:raise ValueError(p+': 启动退出，查看 log/'+p+'.log')
    if listening(p,child.pid):break
    time.sleep(.1)
   else:raise ValueError(p+': 核心未建立预期监听，查看 log/'+p+'.log')
  except BaseException:
   if child.poll() is None:
    child.terminate()
    try:child.wait(timeout=3)
    except subprocess.TimeoutExpired:child.kill();child.wait()
   state.unlink(missing_ok=True)
   raise
 print(p+': 已启动');return True

def stop(p):
 pid=owned(p)
 if pid:
  try:os.kill(pid,signal.SIGTERM)
  except ProcessLookupError:pass
  for _ in range(50):
   if not owned(p):break
   time.sleep(.1)
  if owned(p):raise ValueError(p+': 未退出，保留状态文件')
 state_file(p).unlink(missing_ok=True)
 print(p+': 已停止')

if __name__=='__main__':
 parser=argparse.ArgumentParser();parser.add_argument('action',choices=['start','stop','restart','status','run']);parser.add_argument('protocol',choices=[*SPECS,'all']);a=parser.parse_args()
 try:
  if a.action!='status':
   for d in ['runtime','log']:(ROOT/d).mkdir(mode=0o700,parents=True,exist_ok=True)
  protocols=list(SPECS) if a.protocol=='all' else [a.protocol]
  if a.action=='run':
   if a.protocol=='all':raise ValueError('run 只接受一个协议')
   if not (ROOT/'runtime'/a.protocol/'config.json').is_file():raise ValueError('缺少服务端配置，请执行 sudo bash ./server.sh start 自动初始化（root 可省略 sudo）')
   from outbound import synchronize
   synchronize(protocols)
   for key in list(os.environ):
    if key.lower() in ('http_proxy','https_proxy','all_proxy','no_proxy') or key in ('LD_PRELOAD','PROXYCHAINS_CONF_FILE','PROXYCHAINS_QUIET_MODE','_MIHOMO_PROXY_SNAPSHOT'):os.environ.pop(key,None)
   os.environ['SAFE_PATHS']=str(ROOT/'runtime/certs');(ROOT/'runtime'/a.protocol).mkdir(parents=True,exist_ok=True)
   os.chdir(ROOT);os.execv(command(a.protocol)[0],command(a.protocol))
  started=[]
  try:
   if a.action in ('stop','restart'):
    for p in protocols:stop(p)
   if a.action in ('start','restart'):
    for p in protocols:
     if not (ROOT/'runtime'/p/'config.json').is_file():raise ValueError('缺少服务端配置，请执行 sudo bash ./server.sh start 自动初始化（root 可省略 sudo）')
    from outbound import synchronize
    synchronize(protocols)
    for p in protocols:
     if start(p):started.append(p)
   if a.action=='status':
    from outbound import describe, load_port
    print(describe(load_port(check_ports=False)))
    try:
     result=subprocess.run([command(protocols[0])[0],'-v'],capture_output=True,text=True,check=True,timeout=3)
     version=re.search(r'\bv[0-9][^\s]*',result.stdout)
     print('核心：mihomo '+(version.group() if version else '版本未知'))
    except (OSError,subprocess.SubprocessError):print('核心：不可用，请检查 bin/mihomo_1.19.33')
    for p in protocols:
     try:state='运行中' if owned(p,report_permission=True) else '未运行'
     except PermissionError:state='无法确认状态（需 sudo 查看）'
     print(f'{p}: {state}；端口 {deployed_port(p)}；'+ '/'.join(n.upper() for n in transports(p)))
  except BaseException:
   for p in reversed(started):stop(p)
   raise

 except (OSError,ValueError,KeyError,TypeError,IndexError) as e:parser.exit(1,str(e)+'\n')
