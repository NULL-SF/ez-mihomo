#!/usr/bin/env python3
"""Generate mihomo server configurations in the current project directory."""
import argparse, base64, errno, ipaddress, json, os, secrets, shutil, socket, subprocess, tempfile, uuid
from pathlib import Path
from urllib.parse import quote, urlencode, urlsplit, parse_qsl, unquote
import sys
import urllib.request
from types import SimpleNamespace
sys.dont_write_bytecode=True
sys.path.insert(0,str(Path(__file__).resolve().parent))
from service import ROOT, SPECS, stop, owned, transports, use_original_certificates, information_group, user_config_access
from outbound import load_port as load_relay_port, render as render_outbound

def write(path,value):
 path.write_text(value if isinstance(value,str) else json.dumps(value,ensure_ascii=False,indent=2)+'\n');path.chmod(0o600)
def run(*args):
 return subprocess.run(args,check=True,capture_output=True,text=True).stdout.strip()
def host(value):
 try:return str(ipaddress.ip_address(value))
 except ValueError:
  import re
  value=value.encode('idna').decode().lower()
  if len(value)>253 or any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?',s) for s in value.split('.')):raise ValueError('请输入域名或 IP，不含 URL 和端口')
  return value

def load_ports(path, base_port=None):
 if base_port is not None:
  if not 1024<=base_port<=65536-len(SPECS):raise ValueError('连续端口范围必须在 1024—65535 内')
  return {p:base_port+i for i,p in enumerate(SPECS)}
 directory=Path(path)
 if directory.exists() and not directory.is_dir():raise ValueError('端口配置必须是目录，内含各协议的 .txt 文件')
 missing=[]
 ports={}
 for index,protocol in enumerate(SPECS):
  file=directory/(protocol+'.txt')
  if file.exists() or file.is_symlink():
   value=file.read_text().strip()
  else:
   value=str(11801+index)
   missing.append(protocol)
  if not value.isascii() or not value.isdecimal():raise ValueError(protocol+'：端口文件应只填写一个整数')
  number=int(value)
  if not 1024<=number<=65535:raise ValueError(protocol+'：服务端口必须在 1024—65535 内')
  ports[protocol]=number
 if len(set(ports.values()))!=len(ports):raise ValueError('每种协议应使用不同的端口')
 # Validate the complete set before publishing defaults; preserve existing inputs.
 if missing:
  directory.mkdir(parents=True,exist_ok=True)
  for protocol in missing:
   file=directory/(protocol+'.txt')
   with file.open('x') as stream:stream.write(str(ports[protocol])+'\n')
   user_config_access(file)
 return ports

def owned_bindings(pid,listen):
 # Inspect the live process rather than a config that may need rebuilding.
 proc=Path(f'/proc/{pid}');inodes=set();bindings=set()
 for fd in (proc/'fd').iterdir():
  try:target=fd.readlink().as_posix()
  except FileNotFoundError:continue
  if target.startswith('socket:['):inodes.add(target[8:-1])
 for network in ('tcp','udp'):
  for suffix,family in (('',socket.AF_INET),('6',socket.AF_INET6)):
   table=proc/f'net/{network}{suffix}'
   if not table.exists():continue
   for row in table.read_text().splitlines()[1:]:
    fields=row.split()
    if fields[9] not in inodes or fields[3]!=('0A' if network=='tcp' else '07'):continue
    address,port=fields[1].split(':');raw=bytes.fromhex(address)
    address=socket.inet_ntop(family,b''.join(raw[i:i+4][::-1] for i in range(0,len(raw),4)))
    if address==listen or address in ('0.0.0.0','::') or listen in ('0.0.0.0','::'):
     bindings.add((int(port,16),network))
 return bindings

def check_bindings(listen,ports,selected):
 # A rebuild can reuse listeners owned by the nodes being replaced.
 reusable=set()
 for protocol in selected:
  pid=owned(protocol)
  if not pid:continue
  try:reusable.update(owned_bindings(pid,listen))
  except (OSError,ValueError,IndexError):pass
 family=socket.AF_INET6 if ':' in listen else socket.AF_INET
 for protocol in selected:
  for network in transports(protocol):
   with socket.socket(family,socket.SOCK_DGRAM if network=='udp' else socket.SOCK_STREAM) as probe:
    if network=='tcp':probe.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
    try:probe.bind((listen,ports[protocol]))
    except OSError as error:
     if error.errno==errno.EADDRINUSE and (ports[protocol],network) in reusable:continue
     raise ValueError(f'{protocol}：端口 {ports[protocol]}/{network.upper()} 不可用：{error.strerror}；未重建节点。') from error

def generate(a):
 selected=getattr(a,"protocols",tuple(SPECS))
 relay_port=load_relay_port(write_default=True)
 server=host(a.server);name=host(a.tls_name or a.server)
 ipaddress.ip_address(a.listen)
 ports=load_ports(a.ports,a.base_port)
 if a.self_signed and (a.cert or a.key):raise ValueError('自签名和外部证书不能同时指定')
 if not a.self_signed and not(a.cert and a.key):raise ValueError('请提供 --cert/--key，或 --self-signed 本地测试')
 if not 1<=a.reality_port<=65535:raise ValueError('REALITY 目标端口无效')
 reality=host(a.reality_target)
 reality_address='['+reality+']' if ':' in reality else reality
 if not getattr(a,'repair',False) and any((ROOT/'runtime'/p/'config.json').exists() or (ROOT/'output/links'/(p+'.txt')).exists() for p in selected):raise ValueError('所选协议已有节点配置或链接，拒绝覆盖现有节点认证信息')
 if not (ROOT/'bin/mihomo_1.19.33').is_file():raise ValueError('缺少仓库自带的 bin/mihomo_1.19.33，请重新获取完整项目')
 (ROOT/'runtime').mkdir(parents=True,exist_ok=True,mode=0o700)
 with tempfile.TemporaryDirectory(dir=ROOT/'runtime') as temp:
  temp=Path(temp);temp.chmod(0o700)
  config=temp/'config';config.mkdir(mode=0o700)
  certs=temp/'runtime/certs/config/live/ezm-node';certs.mkdir(parents=True,mode=0o700)
  link_directory=temp/'output/links';link_directory.mkdir(parents=True,mode=0o700)
  if a.self_signed:
   try:ipaddress.ip_address(name);san='IP:'+name
   except ValueError:san='DNS:'+name
   run('openssl','req','-x509','-newkey','rsa:2048','-sha256','-nodes','-days','30','-subj','/CN='+name,'-addext','subjectAltName='+san,'-keyout',str(certs/'privkey.pem'),'-out',str(certs/'fullchain.pem'))
  else:
   shutil.copyfile(a.cert,certs/'fullchain.pem');shutil.copyfile(a.key,certs/'privkey.pem')
  (certs/'privkey.pem').chmod(0o600);(certs/'fullchain.pem').chmod(0o600)
  run('openssl','x509','-in',str(certs/'fullchain.pem'),'-noout','-checkend','0')
  try:ipaddress.ip_address(name);option='-verify_ip'
  except ValueError:option='-verify_hostname'
  run('openssl','verify','-trusted',str(certs/'fullchain.pem'),'-partial_chain','-purpose','sslserver',option,name,str(certs/'fullchain.pem'))
  if run('openssl','x509','-in',str(certs/'fullchain.pem'),'-pubkey','-noout')!=run('openssl','pkey','-in',str(certs/'privkey.pem'),'-passin','pass:','-pubout'):raise ValueError('证书和私钥不匹配')
  # Certificate paths are relative to each mihomo runtime directory.
  cert,key='../certs/config/live/ezm-node/fullchain.pem','../certs/config/live/ezm-node/privkey.pem'
  ids={p:str(uuid.uuid4()) for p in SPECS}
  passwords={p:secrets.token_urlsafe(24) for p in SPECS}
  private=public=sid=''
  if a.vless_security=='reality':
   pem=temp/'reality.pem'
   run('openssl','genpkey','-algorithm','X25519','-out',str(pem))
   private_der=subprocess.check_output(['openssl','pkey','-in',str(pem),'-outform','DER'])
   public_der=subprocess.check_output(['openssl','pkey','-in',str(pem),'-pubout','-outform','DER'])
   private=base64.urlsafe_b64encode(private_der[-32:]).decode().rstrip('=')
   public=base64.urlsafe_b64encode(public_der[-32:]).decode().rstrip('=')
   sid=secrets.token_hex(8)
  ipv6=':' in server;address='['+server+']' if ipv6 else server
  for p in selected:
   port=ports[p];uid=ids[p];password=passwords[p]
   endpoint=f'{address}:{port}'
   q={'sni':name};auth=password
   listener={'name':p,'type':p,'listen':a.listen,'port':port}
   secure={'certificate':cert,'private-key':key}
   if p in ('vless','vmess'):
    user={'uuid':uid}
    if p=='vmess':user['alterId']=0
    listener['users']=[user]
    if p=='vless':
     listener['decryption']='none';auth=uid
     if a.vless_security=='reality':
      user['flow']='xtls-rprx-vision'
      listener['reality-config']={'dest':f'{reality_address}:{a.reality_port}','server-names':[reality],'private-key':private,'short-id':[sid]}
      q={'encryption':'none','type':'tcp','security':'reality','flow':'xtls-rprx-vision','sni':reality,'fp':'chrome','pbk':public,'sid':sid}
     else:listener.update(secure);q.update(encryption='none',type='tcp',security='tls')
    else:listener.update(secure)
   elif p=='hysteria2':
    obfs=secrets.token_urlsafe(24)
    listener.update(secure,users={'node':password},obfs='salamander',**{'obfs-password':obfs,'alpn':['h3']})
    q.update(obfs='salamander',**{'obfs-password':obfs})
   elif p=='trojan':
    listener.update(secure,users=[{'username':'node','password':password}]);q.update(security='tls',type='tcp')
   elif p=='shadowsocks':
    password=base64.b64encode(secrets.token_bytes(16)).decode();passwords[p]=password
    listener.update(type='shadowsocks',cipher='2022-blake3-aes-128-gcm',password=password,udp=True)
   elif p=='tuic':
    listener.update(secure,users={uid:password},alpn=['h3'],**{'congestion-controller':'cubic'})
    auth=uid+':'+password;q.update(congestion_control='cubic',alpn='h3')
   else:listener.update(secure,users={'node':password})
   doc={'mode':'rule','log-level':'warning','listeners':[listener],'rules':['MATCH,DIRECT'],'profile':{'store-selected':False,'store-fake-ip':False}}
   doc=render_outbound(doc,relay_port)
   home=temp/'runtime'/p;home.mkdir(parents=True)
   if p=='vmess':
    link={'v':'2','ps':p,'add':server,'port':str(port),'id':uid,'aid':'0','scy':'auto','net':'tcp','type':'none','host':'','path':'','tls':'tls','sni':name}
    link='vmess://'+base64.b64encode(json.dumps(link).encode()).decode()
   elif p=='shadowsocks':
    # SIP002: SS2022 credentials MUST use percent encoding, not base64.
    link=f'ss://{quote(listener["cipher"],safe="")}:{quote(password,safe="")}@{endpoint}'
   else:link=f'{p}://{quote(auth,safe=":")}@{endpoint}?{urlencode(q)}'
   doc['_ezm']={'server':server,'tls-name':name}
   write(home/'config.json',doc)
   write(link_directory/(p+'.txt'),link+'\n')
  port_directory=config/'ports';port_directory.mkdir(mode=0o700)
  for p in SPECS:write(port_directory/(p+'.txt'),str(ports[p])+'\n')
  # Validate without opening listeners; permit only this project's certificate directory.
  for p in selected:
   home=temp/'runtime'/p
   result=subprocess.run([str(ROOT/'bin/mihomo_1.19.33'),'-t','-d',str(home),'-f',str(home/'config.json')],env=dict(os.environ,SAFE_PATHS=str(certs)),capture_output=True)
   if result.returncode:raise ValueError(p+'：mihomo 配置校验失败')
  check_bindings(a.listen,ports,selected)
  (ROOT/'output').mkdir(parents=True,exist_ok=True)
  publications=[]
  if not getattr(a,'managed_source',False):
   publications=[(certs/name,ROOT/'runtime/certs/config/live/ezm-node'/name) for name in ('fullchain.pem','privkey.pem')]
  if not getattr(a,'repair',False):
   publications += [(port_directory/(p+'.txt'),ROOT/'config/ports'/(p+'.txt')) for p in SPECS]
  publications += [(temp/'runtime'/p,ROOT/'runtime'/p) for p in selected]
  (ROOT/'output/links').mkdir(parents=True,exist_ok=True,mode=0o700)
  publications += [(link_directory/(p+'.txt'),ROOT/'output/links'/(p+'.txt')) for p in selected]
  installed=[]
  try:
   if getattr(a,'repair',False):
    for p in selected:
     if owned(p):
      stop(p)
   for index,(staged,destination) in enumerate(publications):
    backup=temp/('previous-'+str(index));destination.parent.mkdir(parents=True,exist_ok=True)
    if destination.is_file():shutil.copy2(destination,backup)
    elif destination.exists():destination.rename(backup)
    installed.append((destination,backup))
    if destination.parent==ROOT/'output/links':
     os.chown(staged,-1,information_group());staged.chmod(0o660)
    elif destination.parent==ROOT/'config/ports':
     user_config_access(staged)
    staged.replace(destination)
  except BaseException:
   for destination,backup in reversed(installed):
    if backup.is_file():backup.replace(destination)
    else:
     if destination.is_dir():shutil.rmtree(destination)
     elif destination.exists():destination.unlink()
     if backup.exists():backup.rename(destination)
   raise
 print('运行配置目录：'+str(ROOT/'runtime'))
 print('节点链接目录：'+str(ROOT/'output/links'))
 print('\n【节点链接已更新】'+', '.join(selected),flush=True)
 print('请在客户端导入 output/links/ 中对应的新链接，旧链接不再可用。\n',flush=True)
 if a.self_signed:print('自签名仅用于测试：客户端必须导入 runtime/certs/config/live/ezm-node/fullchain.pem 作为信任证书，分享链接不包含该证书。')
def certificate_name():
 import re
 value=run('openssl','x509','-in',str(ROOT/'runtime/certs/config/live/ezm-node/fullchain.pem'),'-noout','-ext','subjectAltName')
 match=re.search(r'(?:IP Address:|DNS:)([^,\s]+)',value)
 if not match:raise ValueError('无法从现有证书确定节点地址')
 return host(match.group(1))

def node_link(protocol, doc):
 metadata=doc.get('_ezm',{})
 if not isinstance(metadata,dict):raise ValueError('节点地址信息无效')
 server=metadata.get('server') or certificate_name()
 name=metadata.get('tls-name') or server
 listener=doc['listeners'][0];port=listener['port']
 address='['+server+']' if ':' in server else server
 endpoint=f'{address}:{port}';q={'sni':name}
 if protocol=='vmess':
  user=listener['users'][0]
  value={'v':'2','ps':protocol,'add':server,'port':str(port),'id':user['uuid'],'aid':str(user.get('alterId',0)),'scy':'auto','net':'tcp','type':'none','host':'','path':'','tls':'tls','sni':name}
  link='vmess://'+base64.b64encode(json.dumps(value).encode()).decode()
 elif protocol=='shadowsocks':
  link=f'ss://{quote(listener["cipher"],safe="")}:{quote(listener["password"],safe="")}@{endpoint}'
 else:
  if protocol=='vless':
   user=listener['users'][0];auth=user['uuid'];reality=listener.get('reality-config')
   q.update(encryption='none',type='tcp',security='tls')
   if reality:
    raw=base64.urlsafe_b64decode(reality['private-key']+'='*(-len(reality['private-key'])%4))
    key=bytes.fromhex('302e020100300506032b656e04220420')+raw
    public=subprocess.run(['openssl','pkey','-inform','DER','-pubout','-outform','DER'],input=key,capture_output=True,check=True).stdout[-32:]
    q.update(security='reality',flow=user['flow'],sni=reality['server-names'][0],fp='chrome',pbk=base64.urlsafe_b64encode(public).decode().rstrip('='),sid=reality['short-id'][0])
  elif protocol=='trojan':
   auth=listener['users'][0]['password'];q.update(security='tls',type='tcp')
  elif protocol=='tuic':
   uid,password=next(iter(listener['users'].items()));auth=uid+':'+password;q.update(congestion_control=listener.get('congestion-controller','cubic'),alpn=','.join(listener.get('alpn',['h3'])))
  else:
   auth=next(iter(listener['users'].values()))
   if protocol=='hysteria2':q.update(obfs=listener['obfs'],**{'obfs-password':listener['obfs-password']})
  link=f'{protocol}://{quote(auth,safe=":")}@{endpoint}?{urlencode(q)}'
 return link

def link_identity(value):
 if value.startswith('vmess://'):
  encoded=value[len('vmess://'):]
  return json.loads(base64.urlsafe_b64decode(encoded+'='*(-len(encoded)%4)))
 parsed=urlsplit(value)
 return (parsed.scheme,unquote(parsed.netloc),parsed.path,sorted(parse_qsl(parsed.query,keep_blank_values=True)))

def complete(protocol,port=None):
 config=ROOT/'runtime'/protocol/'config.json';link=ROOT/'output/links'/(protocol+'.txt')
 try:
  if port is None:port=load_ports(ROOT/'config/ports')[protocol]
  doc=json.loads(config.read_text())
  if not isinstance(doc,dict) or not isinstance(doc.get('listeners'),list):return False
  if not doc['listeners'] or not isinstance(doc['listeners'][0],dict):return False
  if doc['listeners'][0].get('type')!=protocol:return False
  if doc['listeners'][0].get('port')!=port:return False
  value=link.read_text().strip()
  if not value or len(value.splitlines())!=1:return False
  if link_identity(value)!=link_identity(node_link(protocol,doc)):return False
  result=subprocess.run([str(ROOT/'bin/mihomo_1.19.33'),'-t','-d',str(config.parent),'-f',str(config)],
   env=dict(os.environ,SAFE_PATHS=str(ROOT/'runtime/certs')),capture_output=True,timeout=15)
  return result.returncode==0
 except (OSError,ValueError,KeyError,TypeError,AttributeError,IndexError,StopIteration,subprocess.SubprocessError):
  return False

def detect_server():
 # Prefer addresses actually assigned to an active interface over NAT egress.
 result=subprocess.run(['ip','-j','address','show','up','scope','global'],capture_output=True,text=True,check=True)
 candidates=[]
 for interface in json.loads(result.stdout):
  for item in interface.get('addr_info',[]):
   flags=item.get('flags',[])
   if any(item.get(flag) or flag in flags for flag in ('deprecated','tentative','dadfailed')):continue
   if item.get('preferred_life_time')==0:continue
   try:address=ipaddress.ip_address(item.get('local',''))
   except ValueError:continue
   if address.is_global:
    temporary=bool(item.get('temporary') or 'temporary' in flags)
    candidates.append((address.version,temporary,str(address)))
 if candidates:
  _,temporary,value=min(candidates)
  if temporary:print('提示：探测到的是临时 IPv6 地址，地址变更后需重新部署节点。',flush=True)
  return value
 # Some VPS providers expose a mapped IPv4 rather than assigning it to the NIC.
 # This fallback is used only when there is no public address on the host.
 opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
 for endpoint in ('https://api.ipify.org','https://ipv4.icanhazip.com'):
  try:
   with opener.open(endpoint,timeout=12) as response:value=response.read(128).decode().strip()
   address=ipaddress.ip_address(value)
   if address.version==4 and address.is_global:
    print('提示：使用 IPv4 出口地址；若处于 NAT 后，需确认公网 TCP 80 和节点端口已映射到本机。',flush=True)
    return str(address)
  except (OSError,ValueError,UnicodeError):continue
 raise ValueError('未找到本机公网地址，也无法探测 IPv4 出口；请检查网络和地址配置。')

def recover_certificate():
 # Losing certificate state must not rotate otherwise valid node credentials.
 directory=ROOT/'runtime/certs'
 if (directory/'config/renewal/ezm-node.conf').is_file():return
 addresses=set()
 for protocol in SPECS:
  try:
   doc=json.loads((ROOT/'runtime'/protocol/'config.json').read_text())
   if not isinstance(doc,dict) or not isinstance(doc.get('_ezm',{}),dict):continue
   address=ipaddress.ip_address(doc.get('_ezm',{}).get('server',''))
   if address.is_global:addresses.add(str(address))
  except (OSError,ValueError,TypeError):continue
 if not addresses:return  # Fresh installation or internally managed local test certificates.
 if len(addresses)!=1:raise ValueError('现有节点的服务器地址不一致，无法安全恢复证书')
 lost_directory=not directory.is_dir()
 print('证书续期配置缺失，将重新申请证书；现有节点配置和链接不变。',flush=True)
 from certificate import check_certificate, recover_pending_reissue, ready, LIVE, certificate_lock, enable_application
 with certificate_lock():
  recover_pending_reissue()
  enable_application()
  check_certificate(requested=addresses.pop())
  if not ready(LIVE/'fullchain.pem',LIVE/'privkey.pem',0):
   raise ValueError('证书不可用，请稍后重新执行 start')
 # A deleted watched directory requires restarting its TLS listeners.
 if lost_directory:
  for protocol in SPECS:
   try:listener=json.loads((ROOT/'runtime'/protocol/'config.json').read_text())['listeners'][0]
   except (OSError,ValueError,TypeError,KeyError,IndexError):continue
   if 'certificate' in listener and owned(protocol):stop(protocol)

def initialize():
 ports=load_ports(ROOT/'config/ports')
 load_relay_port(write_default=True)
 recover_certificate()
 use_original_certificates()
 missing=[protocol for protocol in SPECS if not complete(protocol,ports[protocol])]
 if not missing:return
 print('\n'+'='*60,flush=True)
 print('【警告】即将生成或重建节点',flush=True)
 print('节点配置、端口或链接缺失、无效或不一致：'+', '.join(missing),flush=True)
 print('受影响节点的认证信息（UUID、密码等）和链接将重新生成，旧链接将失效！',flush=True)
 print('完成后请在客户端导入 output/links/ 中对应的新链接。',flush=True)
 print('='*60+'\n',flush=True)
 existing=(ROOT/'runtime/certs/config/live/ezm-node/fullchain.pem').is_file() and (ROOT/'runtime/certs/config/live/ezm-node/privkey.pem').is_file()
 if existing:
  server=certificate_name()
 else:
  server=detect_server()
 print('自动配置节点地址：'+server,flush=True)
 if not existing:
  print('将自动申请 Let’s Encrypt 免费证书并同意其服务条款：https://letsencrypt.org/repository/',flush=True)
  subprocess.run([sys.executable,str(ROOT/'scripts/certificate.py'),'issue','--identifier',server],check=True)
 generate(SimpleNamespace(server=server,tls_name=server,ports=ROOT/'config/ports',base_port=None,
  listen='::' if ':' in server else '0.0.0.0',self_signed=False,
  cert=ROOT/'runtime/certs/config/live/ezm-node/fullchain.pem' if existing else ROOT/'runtime/certs/config/live/ezm-node/fullchain.pem',
  key=ROOT/'runtime/certs/config/live/ezm-node/privkey.pem' if existing else ROOT/'runtime/certs/config/live/ezm-node/privkey.pem',
  vless_security='reality',reality_target='www.microsoft.com',reality_port=443,
  protocols=missing,repair=True,managed_source=True))

if __name__=='__main__':
 if len(sys.argv)==1:
  try:initialize()
  except (ValueError,OSError,subprocess.SubprocessError,KeyError) as error:sys.exit('初始化失败：'+str(error))
  sys.exit(0)
 parser=argparse.ArgumentParser()
 parser.add_argument('--server',required=True);parser.add_argument('--tls-name')
 parser.add_argument('--ports',type=Path,default=ROOT/'config/ports',help='各协议端口 .txt 文件所在目录')
 parser.add_argument('--base-port',type=int,help='覆盖端口配置，使用从此端口起的七个连续端口');parser.add_argument('--listen',default='0.0.0.0')
 parser.add_argument('--cert',type=Path);parser.add_argument('--key',type=Path);parser.add_argument('--self-signed',action='store_true')
 parser.add_argument('--vless-security',choices=['tls','reality'],default='reality')
 parser.add_argument('--reality-target',default='www.microsoft.com');parser.add_argument('--reality-port',type=int,default=443)
 try:generate(parser.parse_args())
 except (ValueError,OSError,subprocess.CalledProcessError,KeyError) as e:parser.exit(1,'生成失败：'+str(e)+'\n')
