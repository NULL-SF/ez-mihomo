"""Parse seven share-link formats into mihomo JSON (valid YAML), without logging secrets."""
import base64, json, os, re, sys, uuid
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlsplit, urlunsplit, urlencode
PROTOCOLS=('vless','vmess','hysteria2','trojan','shadowsocks','tuic','anytls')
class LinkError(ValueError):
    pass


def boolean(value):
    if str(value).lower() in {"1", "true"}:
        return True
    if str(value).lower() in {"0", "false"}:
        return False
    raise LinkError("布尔参数必须为 true/false 或 1/0")


def integer(value, low=0, high=65535):
    if isinstance(value, bool) or not re.fullmatch(r"[0-9]+", str(value)):
        raise LinkError("整数参数格式错误")
    try:
        number = int(value)
    except (ValueError, TypeError):
        raise LinkError("数值参数格式错误") from None
    if not low <= number <= high:
        raise LinkError("数值参数超出范围")
    return number


class Params:
    def __init__(self, data):
        self.data = dict(data)
        self.used = set()

    def get(self, *names, default=""):
        found = [n for n in names if n in self.data]
        self.used.update(found)
        values = [self.data[n] for n in found]
        if any(not isinstance(v, (str, int, bool)) for v in values):
            raise LinkError("链接参数必须是字符串、整数或布尔值")
        def canonical(value):
            if value == "":
                return value
            if names[0] in {"type", "security", "fp"}:
                value = str(value).lower()
                if names[0] == "type":
                    return {"raw": "tcp", "websocket": "ws", "splithttp": "xhttp", "mkcp": "kcp"}.get(value, value)
                return value
            if names[0] == "insecure":
                return boolean(value)
            if names[0] == "pinSHA256":
                return str(value).replace(":", "").lower()
            return value
        values = [canonical(value) for value in values]
        if values and any(v != values[0] for v in values):
            raise LinkError("同一参数的不同别名存在冲突")
        return values[0] if values else default

    def finish(self, allow_unknown=False):
        # Presentation-only metadata does not affect a connection.
        ignored = {"ps", "remarks", "remark", "v"}
        if not allow_unknown and set(self.data) - self.used - ignored:
            raise LinkError("链接含尚未支持或不适用于当前传输的参数；未生成不完整配置")


def json_object(value):
    try:
        result = json.loads(value, object_pairs_hook=unique_object,
                            parse_constant=lambda _: (_ for _ in ()).throw(LinkError("JSON 不支持非有限数值")))
    except (ValueError, TypeError):
        raise LinkError("扩展配置必须是无重复字段的 JSON 对象") from None
    if not isinstance(result, dict):
        raise LinkError("扩展配置必须是 JSON 对象")
    return result


def list_strings(value):
    result = [s.strip() for s in str(value).split(",")]
    if not result or any(not s for s in result):
        raise LinkError("逗号分隔的参数不能包含空项")
    return result


def duration(value, low=0, high=2147483647):
    match = re.fullmatch(r"([0-9]+)(s|m|h)?", str(value))
    if not match:
        raise LinkError("时间参数须为整数秒或带 s/m/h 单位")
    return integer(str(int(match[1]) * {None: 1, "s": 1, "m": 60, "h": 3600}[match[2]]), low, high)


def port_list(value):
    pieces = str(value).split(",")
    result = []
    for piece in pieces:
        ends = piece.strip().split("-")
        if len(ends) not in {1, 2}:
            raise LinkError("端口列表格式错误")
        start, end = integer(ends[0], 1), integer(ends[-1], 1)
        if end < start:
            raise LinkError("端口范围起点不能大于终点")
        result.append(str(start) if start == end else f"{start}-{end}")
    return ",".join(result)


def unique_object(pairs):
    result={}
    for k,v in pairs:
        if k in result:raise LinkError('链接包含重复字段')
        result[k]=v
    return result

def b64(value):
    try:return base64.b64decode(value+'='*(-len(value)%4),altchars=b'-_',validate=True).decode()
    except (ValueError,UnicodeError):raise LinkError('Base64 编码无效') from None

def user_uuid(value):
    if not isinstance(value,str):raise LinkError('用户 ID 必须是 UUID 字符串')
    return str(uuid.UUID(value))

def headers(value):
    obj=json_object(value)
    if any(not isinstance(v,str) or any(c in k+v for c in '\r\n') for k,v in obj.items()):raise LinkError('headers 须为无换行的字符串键值对象')
    return obj

def ss_plugin(value):
    # SIP003 escapes separators with backslashes; never split escaped semicolons.
    parts=[];current='';escaped=False
    for c in value:
        if escaped:current+='\\'+c;escaped=False
        elif c=='\\':escaped=True
        elif c==';':parts.append(current);current=''
        else:current+=c
    if escaped:raise LinkError('SS 插件参数末尾不能是转义符')
    parts.append(current)
    def unescape(text):return re.sub(r'\\(.)',r'\1',text)
    plugin=unescape(parts[0]);opts={}
    for part in parts[1:]:
        key,sep,v=part.partition('=');key=unescape(key)
        if not key or key in opts:raise LinkError('SS 插件字段为空或重复')
        opts[key]=unescape(v) if sep else True
    if plugin in ('obfs-local','simple-obfs','obfs'):
        if set(opts)-{'obfs','obfs-host'} or opts.get('obfs') not in ('http','tls'):raise LinkError('simple-obfs 参数无效或未适配')
        host=opts.get('obfs-host','')
        if not isinstance(host,str):raise LinkError('obfs-host 必须是字符串')
        return 'obfs',{'mode':opts['obfs'],'host':host}
    if plugin=='v2ray-plugin':
        if set(opts)-{'mode','host','path','tls','mux','skip-cert-verify'}:raise LinkError('v2ray-plugin 含尚未适配的参数')
        if opts.get('mode','websocket')!='websocket':raise LinkError('v2ray-plugin 当前仅支持 WebSocket 模式')
        result={'mode':'websocket'}
        for k,v in opts.items():
            if k in ('tls','mux','skip-cert-verify'):result[k]=boolean(v)
            elif k!='mode':
                if not isinstance(v,str):raise LinkError('插件 host/path 必须是字符串')
                result[k]=v
        return plugin,result
    raise LinkError('mihomo 当前适配不支持此 SIP003 插件')

def tls(p,n,host,enabled=True):
    ca=None
    if not enabled:return ca
    sni=p.get('sni','serverName','servername','peer',default=host or n['server'])
    if not isinstance(sni,str):raise LinkError('SNI 必须是字符串')
    n['servername' if n['type'] in ('vless','vmess') else 'sni']=sni
    insecure=p.get('insecure','allowInsecure','allowinsecure','skip-cert-verify')
    if insecure!='':n['skip-cert-verify']=boolean(insecure)
    pin=p.get('pinSHA256','pinnedPeerCertSha256')
    if pin:
        pin=str(pin).replace(':','').lower()
        if not re.fullmatch('[0-9a-f]{64}',pin):raise LinkError('证书 SHA-256 指纹格式无效')
        if n.get('skip-cert-verify'):raise LinkError('证书指纹验证不能与跳过验证同时使用')
        n['fingerprint']=pin
    alpn=p.get('alpn')
    if alpn:n['alpn']=list_strings(alpn)
    fp=p.get('fp','fingerprint','client-fingerprint')
    if fp:
        if n['type'] in ('hysteria2','tuic'):raise LinkError('QUIC 协议不支持浏览器 TLS 指纹参数')
        n['client-fingerprint']=str(fp).lower()
    ca=p.get('ca','caFile')
    if ca:
        path=Path(ca).expanduser()
        if not path.is_absolute():path=Path(os.environ.get('MIHOMO_ROOT','.'))/path
        try:ca=path.read_text()
        except OSError:raise LinkError('指定的 CA 文件不可读') from None
        if '-----BEGIN CERTIFICATE-----' not in ca:raise LinkError('CA 必须是 PEM 证书')
    ech=p.get('ech','echConfigList')
    if ech:
        if '://' in ech:raise LinkError('请使用 Base64 ECH 配置，尚未适配 DNS 查询扩展')
        n['ech-opts']={'enable':True,'config':ech}
    return ca

def transport(p,n):
    net=str(p.get('type','net',default='tcp')).lower()
    net={'raw':'tcp','websocket':'ws','splithttp':'xhttp','mkcp':'kcp'}.get(net,net)
    host=p.get('host')
    if not isinstance(host,str):raise LinkError('host 必须是字符串')
    header=p.get('headerType',default='none') or 'none'
    if net in ('ws','httpupgrade'):
        n['network']='ws';path=p.get('path',default='/') or '/'
        if not isinstance(path,str):raise LinkError('path 必须是字符串')
        if not path.startswith('/'):path='/'+path
        opts={'path':path,'headers':{}}
        if host:opts['headers']['Host']=host
        h=p.get('headers')
        if h:
            h=headers(h)
            if any(k.lower()=='host' for k in h):raise LinkError('Host 请通过 host 参数指定')
            opts['headers'].update(h)
        ed=p.get('ed');eh=p.get('eh')
        parsed=urlsplit(path);pairs=parse_qsl(parsed.query,keep_blank_values=True);eds=[v for k,v in pairs if k=='ed']
        if len(eds)>1 or (eds and ed!=''):raise LinkError('early-data ed 重复')
        if eds:
            ed=eds[0];opts['path']=urlunsplit(('', '', parsed.path,urlencode([(k,v) for k,v in pairs if k!='ed']),parsed.fragment))
        if ed!='':opts['max-early-data']=integer(ed,0,4294967295);opts['early-data-header-name']=eh or 'Sec-WebSocket-Protocol'
        elif eh:raise LinkError('eh 需要 ed')
        if net=='httpupgrade':opts['v2ray-http-upgrade']=True
        n['ws-opts']=opts
    elif net=='grpc':
        mode=p.get('mode',default='gun') or 'gun';multi=p.get('multiMode')
        if mode!='gun' or (multi!='' and boolean(multi)):raise LinkError('当前适配仅支持标准 gRPC gun 模式')
        authority=p.get('authority')
        if authority and authority not in (host,n.get('servername'),p.data.get('sni'),p.data.get('serverName'),p.data.get('peer')):raise LinkError('独立 gRPC authority 尚未适配')
        n.update(network='grpc',**{'grpc-opts':{'grpc-service-name':p.get('serviceName','path')}})
        ua=p.get('user_agent')
        if ua:n['grpc-opts']['grpc-user-agent']=ua
    elif net=='xhttp':
        if n['type']!='vless':raise LinkError('当前 mihomo 仅为 VLESS 提供 XHTTP')
        opts={'path':p.get('path',default='/') or '/','host':host,'mode':p.get('mode',default='auto') or 'auto'}
        if opts['mode'] not in ('auto','packet-up','stream-up','stream-one'):raise LinkError('XHTTP mode 无效')
        h=p.get('headers');extra=p.get('extra')
        if h:opts['headers']=headers(h)
        if extra:
            ex=json_object(extra)
            mapping={'headers':'headers','xPaddingBytes':'x-padding-bytes','noGRPCHeader':'no-grpc-header','scMaxEachPostBytes':'sc-max-each-post-bytes','scMinPostsIntervalMs':'sc-min-posts-interval-ms'}
            if set(ex)-set(mapping)-{'xmux'}:raise LinkError('XHTTP extra 中部分字段尚未适配')
            for k,v in ex.items():
                if k=='xmux':
                    xm={'maxConcurrency':'max-concurrency','maxConnections':'max-connections','cMaxReuseTimes':'c-max-reuse-times','hMaxRequestTimes':'h-max-request-times','hMaxReusableSecs':'h-max-reusable-secs','hKeepAlivePeriod':'h-keep-alive-period'}
                    if not isinstance(v,dict) or set(v)-set(xm):raise LinkError('XHTTP xmux 字段无效')
                    opts['reuse-settings']={xm[a]:integer(b,0,2147483647) if a=='hKeepAlivePeriod' else str(b) for a,b in v.items()}
                else:
                    if mapping[k] in opts:raise LinkError('XHTTP 扩展字段重复')
                    opts[mapping[k]]=v if k in ('headers','noGRPCHeader') else str(v)
        n.update(network='xhttp',**{'xhttp-opts':opts})
    elif net=='kcp':
        if n['type']!='vmess':raise LinkError('当前 mihomo VLESS 不支持 mKCP')
        opts={'seed':p.get('seed','path'),'header':header}
        for a,b in {'mtu':'mtu','tti':'tti','uplinkCapacity':'uplink-capacity','downlinkCapacity':'downlink-capacity','readBufferSize':'read-buffer','writeBufferSize':'write-buffer'}.items():
            v=p.get(a)
            if v!='':opts[b]=integer(v,0,4294967295)
        v=p.get('congestion')
        if v!='':opts['congestion']=boolean(v)
        n.update(network='mkcp',**{'mkcp-opts':opts})
    elif net in ('http','h2') or (net=='tcp' and header=='http'):
        path=p.get('path',default='/') or '/';n['network']='h2' if net=='h2' else 'http'
        n['h2-opts' if net=='h2' else 'http-opts']={'host':list_strings(host or n['server']),'path':path} if net=='h2' else {'path':list_strings(path),'headers':{'Host':list_strings(host or n['server'])}}
    elif net=='tcp':
        if header!='none':raise LinkError('TCP headerType 无效')
        n['network']='tcp'
    else:raise LinkError('尚未适配此传输类型')
    tfo=p.get('tfo','tcpFastOpen')
    if tfo!='':n['tfo']=boolean(tfo)
    return host

def parse_link(profile,link,port=10808):
    try:return _parse(profile,link,port)
    except LinkError:raise
    except (ValueError,TypeError,KeyError,UnicodeError):raise LinkError('链接字段格式错误') from None

def _parse(profile,link,port):
    link=link.strip().lstrip('\ufeff');part=link.split('#',1)[0]
    if not part or any(c.isspace() for c in part) or re.search(r'%(?![0-9a-fA-F]{2})',part):raise LinkError('请提供单条完整链接；空格须 URL 编码')
    profile={'hy2':'hysteria2','ss':'shadowsocks'}.get(profile,profile)
    if profile not in PROTOCOLS:raise LinkError('未知线路')
    u=urlsplit(link)
    if profile=='vmess' and u.scheme=='vmess' and '@' not in u.netloc:
        d=json_object(b64(unquote(part.split('://',1)[1])))
        n={'name':'node','type':'vmess','server':d.get('add'),'port':integer(d.get('port'),1),'uuid':user_uuid(d.get('id')),'alterId':integer(d.get('aid') or 0),'cipher':d.get('scy') or 'auto','udp':True}
        fields={k:v for k,v in d.items() if k not in ('add','port','id','aid','scy','v','ps') and v not in ('',None)}
        if 'type' in fields:
            typ=fields.pop('type')
            if fields.get('net')=='grpc' and typ in ('gun','multi'):fields['mode']=typ
            else:fields['headerType']=typ
        p=Params(fields);sec=p.get('security','tls',default='none') or 'none'
    else:
        expected={'hysteria2':('hysteria2','hy2'),'shadowsocks':('ss',)}.get(profile,(profile,))
        if u.scheme not in expected:raise LinkError('链接协议与线路不匹配')
        if profile=='shadowsocks' and '@' not in u.netloc:
            decoded=b64(unquote(part.split('://',1)[1].split('?',1)[0]));u=urlsplit('ss://'+decoded+('?' + urlsplit(link).query if urlsplit(link).query else ''))
        pairs=parse_qsl(u.query,keep_blank_values=True,strict_parsing=True,errors='strict') if u.query else []
        if len(dict(pairs))!=len(pairs):raise LinkError('链接参数重复')
        p=Params(pairs)
        if u.path not in ('','/'):raise LinkError('传输路径须放在 path 参数')
        rawport=u.netloc.rsplit('@',1)[-1]
        rawport=rawport.split(']',1)[1][1:] if rawport.startswith('[') else rawport.rsplit(':',1)[1] if ':' in rawport else ''
        hopping=profile=='hysteria2' and (',' in rawport or '-' in rawport)
        rp=int(port_list(rawport).split(',')[0].split('-')[0]) if hopping else integer(rawport or ('443' if profile in ('hysteria2','trojan','anytls') else ''),1)
        n={'name':'node','type':'ss' if profile=='shadowsocks' else profile,'server':u.hostname,'port':rp,'udp':True}
        username=unquote(u.username or '',errors='strict');password=unquote(u.password,errors='strict') if u.password is not None else None
        sec='tls'
        if profile in ('vless','vmess'):
            if password is not None:raise LinkError('VLESS/VMess 不能含密码字段')
            n['uuid']=user_uuid(username);sec=p.get('security','tls',default='none') or 'none'
            if profile=='vless':
                n['encryption']=p.get('encryption',default='none') or 'none';flow=p.get('flow')
                if flow:n['flow']=flow
            else:n.update(alterId=integer(p.get('aid','alterId',default=0)),cipher=p.get('encryption','scy',default='auto') or 'auto')
        elif profile=='shadowsocks':
            if password is None:
                credentials=b64(username)
                if ':' not in credentials:raise LinkError('SS 缺少加密方式或密码')
                username,password=credentials.split(':',1)
            n.update(cipher=username,password=password);sec='none'
            plugin=p.get('plugin')
            if plugin:n['plugin'],n['plugin-opts']=ss_plugin(plugin)
        elif profile=='tuic':
            if password is None:
                if not username:raise LinkError('TUIC 缺少令牌')
                n['token']=username
            else:n.update(uuid=user_uuid(username),password=password)
            for names,key in [(('congestion_control','congestion-controller'),'congestion-controller'),(('udp_relay_mode','udp-relay-mode'),'udp-relay-mode')]:
                v=p.get(*names)
                if v:n[key]=v
            for names,key in [(('disable_sni','disable-sni'),'disable-sni'),(('reduce_rtt','reduce-rtt'),'reduce-rtt'),(('udp_over_stream','udp-over-stream'),'udp-over-stream')]:
                v=p.get(*names)
                if v!='':n[key]=boolean(v)
        else:
            n['password']=username+(':'+password if password is not None else '')
            if profile=='hysteria2':
                hops=p.get('mport','ports')
                if hopping:
                    if hops and port_list(hops)!=port_list(rawport):raise LinkError('跳跃端口设置冲突')
                    hops=rawport
                interval=p.get('hopInterval','hop-interval')
                if hops:n['ports']=port_list(hops);n['hop-interval']=duration(interval or '30',5)
                elif interval:raise LinkError('hopInterval 需要跳跃端口列表')
                obfs=p.get('obfs')
                if obfs and obfs!='none':
                    if obfs!='salamander':raise LinkError('仅支持 Salamander 混淆')
                    n['obfs']=obfs;n['obfs-password']=p.get('obfs-password','obfsPassword')
                    if not n['obfs-password']:raise LinkError('缺少混淆密码')
                for direction in ('up','down'):
                    v=p.get(direction);mbps=p.get(direction+'mbps',direction+'Mbps',direction+'_mbps')
                    if v and mbps:raise LinkError('带宽参数重复')
                    if v:n[direction]=str(v)
                    if mbps:
                        if not re.fullmatch('[0-9]+(?:\.[0-9]+)?',str(mbps)):raise LinkError('Mbps 参数无效')
                        n[direction]=str(mbps)+' mbps'
                for a,b in {'initStreamReceiveWindow':'initial-stream-receive-window','maxStreamReceiveWindow':'max-stream-receive-window','initConnectionReceiveWindow':'initial-connection-receive-window','maxConnectionReceiveWindow':'max-connection-receive-window'}.items():
                    v=p.get(a)
                    if v!='':n[b]=integer(v,0,2147483647)
            elif profile=='anytls':
                for key in ('idle-session-check-interval','idle-session-timeout','min-idle-session'):
                    v=p.get(key)
                    if v!='':n[key]=integer(v,0,2147483647)
            elif profile=='trojan':
                security=p.get('security',default='tls')
                if security!='tls':raise LinkError('Trojan 必须使用 TLS')
                # Trojan supports the same WS/gRPC transport options.
    if not isinstance(n['server'],str) or not n['server']:raise LinkError('缺少服务器地址')
    if profile in ('vless','vmess','trojan'):
        host=transport(p,n)
        if sec not in ('none','tls','reality'):raise LinkError('不支持此安全类型')
        if profile in ('vless','vmess'):n['tls']=sec!='none'
        if sec=='reality':
            pbk=p.get('pbk','publicKey','password')
            if not pbk:raise LinkError('REALITY 缺少公钥')
            n['reality-opts']={'public-key':pbk,'short-id':p.get('sid','shortId'),'support-x25519mlkem768':boolean(p.get('support-x25519mlkem768',default=True))}
            p.get('spx','spiderX') # Not part of the authenticated proxy connection.
        ca=tls(p,n,host,sec!='none')
        if sec=='reality':n.setdefault('client-fingerprint','chrome')
        if n.get('flow') and (n['flow']!='xtls-rprx-vision' or n['network']!='tcp' or sec=='none'):raise LinkError('Vision 需要 TCP + TLS/REALITY')
    else:ca=tls(p,n,'',sec!='none')
    if 'password' in n and not n['password']:raise LinkError('密码为空')
    # SIP002 explicitly permits unrecognized URI query arguments to be ignored.
    p.finish(allow_unknown=profile=='shadowsocks')
    cfg=config(n,port)
    if ca:cfg['tls']={'custom-certifactes':[ca]}
    return cfg

def config(node,port):
    cfg={'mixed-port':integer(port,1),'bind-address':'127.0.0.1','allow-lan':False,'mode':'rule','log-level':'warning','profile':{'store-selected':False,'store-fake-ip':False},'rules':['MATCH,node' if node else 'MATCH,REJECT']}
    if node:cfg['proxies']=[node]
    return cfg

def main():
    try:cfg=parse_link(os.environ['MIHOMO_PROFILE'],os.environ['MIHOMO_LINK'],os.environ['MIHOMO_PORT'])
    except LinkError as e:print('[mihomo] '+str(e),file=sys.stderr);return 1
    target=Path(sys.argv[1]);tmp=target.with_name(target.name+f'.{os.getpid()}.tmp')
    try:
        with open(tmp,'x',opener=lambda p,f:os.open(p,f,0o600)) as stream:json.dump(cfg,stream,indent=2);stream.write('\n')
        tmp.replace(target)
    finally:tmp.unlink(missing_ok=True)
    return 0
if __name__=='__main__':sys.exit(main())
