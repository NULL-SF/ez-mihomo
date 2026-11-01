#!/usr/bin/env python3
"""Generate mihomo server configurations in the current project directory."""
import argparse, base64, ipaddress, json, os, secrets, shutil, subprocess, tempfile, uuid
from pathlib import Path
from urllib.parse import quote, urlencode
import sys
import urllib.request
from types import SimpleNamespace
sys.dont_write_bytecode=True
sys.path.insert(0,str(Path(__file__).resolve().parent))
from service import ROOT, SPECS

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
 if not directory.is_dir():raise ValueError('端口配置必须是目录，内含各协议的 .txt 文件')
 expected={p+'.txt' for p in SPECS}
 actual={f.name for f in directory.iterdir() if f.suffix=='.txt'}
 if actual!=expected:raise ValueError('端口目录必须包含且仅包含七种协议的 .txt 文件')
 ports={}
 for protocol in SPECS:
  value=(directory/(protocol+'.txt')).read_text().strip()
  if not value.isascii() or not value.isdecimal():raise ValueError(protocol+'：端口文件应只填写一个整数')
  number=int(value)
  if not 1024<=number<=65535:raise ValueError(protocol+'：服务端口必须在 1024—65535 内')
  ports[protocol]=number
 if len(set(ports.values()))!=len(ports):raise ValueError('每种协议应使用不同的端口')
 return ports

def generate(a):
 server=host(a.server);name=host(a.tls_name or a.server)
 ipaddress.ip_address(a.listen)
 ports=load_ports(a.ports,a.base_port)
 if a.self_signed and (a.cert or a.key):raise ValueError('自签名和外部证书不能同时指定')
 if not a.self_signed and not(a.cert and a.key):raise ValueError('请提供 --cert/--key，或 --self-signed 本地测试')
 if not 1<=a.reality_port<=65535:raise ValueError('REALITY 目标端口无效')
 reality=host(a.reality_target)
 reality_address='['+reality+']' if ':' in reality else reality
 if any((ROOT/'runtime'/p/'config.json').exists() or (ROOT/'output/links'/(p+'.txt')).exists() for p in SPECS):raise ValueError('当前项目已有节点配置，拒绝覆盖或更换凭据')
 if not (ROOT/'bin/mihomo_1.19.33').is_file():raise ValueError('缺少仓库自带的 bin/mihomo_1.19.33，请重新获取完整项目')
 (ROOT/'runtime').mkdir(parents=True,exist_ok=True,mode=0o700)
 with tempfile.TemporaryDirectory(dir=ROOT/'runtime') as temp:
  temp=Path(temp);temp.chmod(0o700)
  config=temp/'config';config.mkdir(mode=0o700)
  certs=temp/'certs';certs.mkdir(parents=True,mode=0o700)
  link_directory=temp/'output/links';link_directory.mkdir(parents=True,mode=0o700)
  if a.self_signed:
   try:ipaddress.ip_address(name);san='IP:'+name
   except ValueError:san='DNS:'+name
   run('openssl','req','-x509','-newkey','rsa:2048','-sha256','-nodes','-days','30','-subj','/CN='+name,'-addext','subjectAltName='+san,'-keyout',str(certs/'server.key'),'-out',str(certs/'server.crt'))
  else:
   shutil.copyfile(a.cert,certs/'server.crt');shutil.copyfile(a.key,certs/'server.key')
  (certs/'server.key').chmod(0o600);(certs/'server.crt').chmod(0o600)
  run('openssl','x509','-in',str(certs/'server.crt'),'-noout','-checkend','0')
  try:ipaddress.ip_address(name);option='-verify_ip'
  except ValueError:option='-verify_hostname'
  run('openssl','verify','-trusted',str(certs/'server.crt'),'-partial_chain','-purpose','sslserver',option,name,str(certs/'server.crt'))
  if run('openssl','x509','-in',str(certs/'server.crt'),'-pubkey','-noout')!=run('openssl','pkey','-in',str(certs/'server.key'),'-passin','pass:','-pubout'):raise ValueError('证书和私钥不匹配')
  # Certificate paths are relative to each mihomo runtime directory.
  cert,key='../../certs/server.crt','../../certs/server.key'
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
  for p in SPECS:
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
   home=temp/'runtime'/p;home.mkdir(parents=True)
   write(home/'config.json',doc)
   if p=='vmess':
    link={'v':'2','ps':p,'add':server,'port':str(port),'id':uid,'aid':'0','scy':'auto','net':'tcp','type':'none','host':'','path':'','tls':'tls','sni':name}
    link='vmess://'+base64.b64encode(json.dumps(link).encode()).decode()
   elif p=='shadowsocks':
    # SIP002: SS2022 credentials MUST use percent encoding, not base64.
    link=f'ss://{quote(listener["cipher"],safe="")}:{quote(password,safe="")}@{endpoint}'
   else:link=f'{p}://{quote(auth,safe=":")}@{endpoint}?{urlencode(q)}'
   write(link_directory/(p+'.txt'),link+'\n')
  port_directory=config/'ports';port_directory.mkdir(mode=0o700)
  for p in SPECS:write(port_directory/(p+'.txt'),str(ports[p])+'\n')
  # Validate without opening listeners; permit only this project's certificate directory.
  for p in SPECS:
   home=temp/'runtime'/p
   result=subprocess.run([str(ROOT/'bin/mihomo_1.19.33'),'-t','-d',str(home),'-f',str(home/'config.json')],env=dict(os.environ,SAFE_PATHS=str(certs)),capture_output=True)
   if result.returncode:raise ValueError(p+'：mihomo 配置校验失败')
  (ROOT/'output').mkdir(parents=True,exist_ok=True)
  publications=[(certs,ROOT/'certs'),(port_directory,ROOT/'config/ports')]
  publications += [(temp/'runtime'/p,ROOT/'runtime'/p) for p in SPECS]
  publications.append((link_directory,ROOT/'output/links'))
  installed=[]
  try:
   for index,(staged,destination) in enumerate(publications):
    backup=temp/('previous-'+str(index));destination.parent.mkdir(parents=True,exist_ok=True)
    if destination.exists():destination.rename(backup)
    installed.append((destination,backup))
    staged.rename(destination)
  except BaseException:
   for destination,backup in reversed(installed):
    if destination.exists():shutil.rmtree(destination)
    if backup.exists():backup.rename(destination)
   raise
 print('运行配置目录：'+str(ROOT/'runtime'))
 print('节点链接目录：'+str(ROOT/'output/links'))
 print('已生成七种协议的 mihomo 配置和链接；凭据不输出到终端。')
 if a.self_signed:print('自签名仅用于测试：客户端必须导入 certs/server.crt 作为信任证书，分享链接不包含该证书。')
def initialize():
 existing=[(ROOT/'runtime'/p/'config.json').is_file() for p in SPECS]
 links=[(ROOT/'output/links'/(p+'.txt')).is_file() for p in SPECS]
 if all(existing) and all(links):return
 if any(existing) or any(links):raise ValueError('节点配置不完整，请恢复 runtime/ 与 output/links/，不自动覆盖凭据。')
 load_ports(ROOT/'config/ports')
 # Prefer IPv4 so the exported nodes remain usable on IPv4-only clients.
 opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
 server=None
 for endpoint in ('https://api.ipify.org','https://api6.ipify.org'):
  try:
   with opener.open(endpoint,timeout=12) as response:value=response.read(128).decode().strip()
   address=ipaddress.ip_address(value)
   if address.is_global:server=str(address);break
  except (OSError,ValueError,UnicodeError):continue
 if server is None:raise ValueError('无法探测 VPS 公网 IP，请检查网络后重试 start。')
 print('首次启动，自动配置公网 IP：'+server,flush=True)
 print('将自动申请 Let’s Encrypt 免费证书并同意其服务条款：https://letsencrypt.org/repository/',flush=True)
 from certificate import issue
 issue(server,'')
 generate(SimpleNamespace(server=server,tls_name=server,ports=ROOT/'config/ports',base_port=None,
  listen='::' if ':' in server else '0.0.0.0',self_signed=False,
  cert=ROOT/'runtime/acme/config/live/ezm-node/fullchain.pem',key=ROOT/'runtime/acme/config/live/ezm-node/privkey.pem',
  vless_security='reality',reality_target='www.microsoft.com',reality_port=443))

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
