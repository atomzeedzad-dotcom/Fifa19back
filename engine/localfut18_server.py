#!/usr/bin/env python3
from __future__ import annotations
import argparse, csv, datetime as dt, hashlib, io, json, logging, os, random, re, socket, socketserver, sqlite3, ssl, struct, threading, time, unicodedata, urllib.parse, urllib.request, zlib
from contextlib import contextmanager
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from functools import wraps
from fut18_catalog import (refresh_all_cards_csv, build_archive_variations, load_archive_variations,
                           refresh_manager_cache, load_manager_definitions, consumable_definitions)
from fut18_sbc_archive import load_archive as load_sbc_archive, refresh_archive as refresh_sbc_archive, stable_int as sbc_stable_int
from fut18_runtime import env_flag
from fut18_draft_slots import (DraftSlotError, slot_id as _draft_parse_slot,
                              sample_definitions as _draft_sample_definitions,
                              offer as _draft_slot_offer, choose as _draft_slot_choose)

VERSION='1.0.0-offline-release'
ROOT=Path(__file__).resolve().parent.parent
LOCALAPP=Path(os.environ.get('LOCALAPPDATA', str(Path.home()/ 'AppData/Local')))
RUNTIME=Path(os.environ.get('FIFA19_LOCAL_RUNTIME',str(LOCALAPP/'FIFA19LocalFUT'))); LOGDIR=RUNTIME/'logs'; RAW=RUNTIME/'raw'; DB_PATH=RUNTIME/'fut19-local.sqlite3'; PLAYER_DATA_DIR=RUNTIME/'data'; PLAYER_CSV=PLAYER_DATA_DIR/'CompleteDataset.csv'; PLAYER_CACHE=PLAYER_DATA_DIR/'fifa18-player-definitions.json'; PLAYER_HEAD_CACHE=PLAYER_DATA_DIR/'playerheads'; WALKOUT_REQUEST_FILE=PLAYER_DATA_DIR/'walkout-request.json'; ALL_CARDS_CSV=PLAYER_DATA_DIR/'FIFA18-all-cards.csv'; ARCHIVE_CARD_CACHE=PLAYER_DATA_DIR/'fifa18-all-card-definitions.json'; MANAGER_CACHE=PLAYER_DATA_DIR/'fifa18-managers.json'; SBC_CACHE=PLAYER_DATA_DIR/'fifa18-sbc-archive.json'; SBC_BUNDLED=ROOT/'data/fifa18-sbc-archive.json'; BUNDLED_PLAYER_CACHE=ROOT/'data/fifa18-player-definitions.json'
# Normal gameplay uses the packaged/cached databases only. Historical live crawls and
# full-catalog refreshes are opt-in diagnostics because they compete with FIFA during boot.
OFFLINE_ONLY=True
ENABLE_BACKGROUND_REFRESH=False
LOGDIR.mkdir(parents=True,exist_ok=True); RAW.mkdir(parents=True,exist_ok=True); PLAYER_DATA_DIR.mkdir(parents=True,exist_ok=True); PLAYER_HEAD_CACHE.mkdir(parents=True,exist_ok=True)
STAMP=dt.datetime.now().strftime('%Y%m%d-%H%M%S')
LOGFILE=LOGDIR/f'localfut19-{STAMP}.log'
RAW_CAPTURE_COUNTS={}
logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s',handlers=[logging.FileHandler(LOGFILE,encoding='utf-8'),logging.StreamHandler()])
log=logging.getLogger('localfut18')
FAKE_NUCLEUS=1000000001; FAKE_PERSONA=1000000001; TOKEN='LOCALFUT18_SESSION_1000000001'; PERSONA='LocalPlayer'
DRAFT_OPPONENT_TEAM_ID=243  # Real Madrid; real non-zero FIFA 18 team id for offline Draft handoff
PENDING_USERSESSION_REFRESH=threading.Event()
PENDING_DRAFT_EXTDATA_REFRESH=threading.Event()
_DRAFT_SP_LOCK=threading.RLock()


def _draft_sp_serialized(mode_argument):
    """Serialize normal SP read/modify/save, not just individual SQL writes."""
    def decorate(function):
        @wraps(function)
        def call(*args,**kwargs):
            mode=kwargs.get('mode',args[mode_argument] if len(args)>mode_argument else 'SINGLE_PLAYER')
            if _draft_mode_name(mode) in ('SINGLE_PLAYER', 'WORLD_CUP_SINGLE_PLAYER'):
                with _DRAFT_SP_LOCK:
                    return function(*args,**kwargs)
            return function(*args,**kwargs)
        return call
    return decorate

def _remote_urlopen(request,timeout=None):
    """Open an external URL only when the operator explicitly enables it."""
    if OFFLINE_ONLY:
        raise RuntimeError('remote access disabled by FIFA18_LOCAL_OFFLINE (default: enabled)')
    return urllib.request.urlopen(request,timeout=timeout)

# ---- TDF -----------------------------------------------------------------
T_VARINT=0;T_STRING=1;T_BLOB=2;T_GROUP=3;T_LIST=4;T_MAP=5;T_OBJECT_TYPE=8;T_OBJECT_ID=9

def tag(t:str,typ:int)->bytes:
    r=t.encode('ascii'); o=[0,0,0,typ&255]
    if not 1<=len(r)<=4: raise ValueError(t)
    if len(r)>0:o[0]|=(r[0]&0x40)<<1;o[0]|=(r[0]&0x10)<<2;o[0]|=(r[0]&0x0f)<<2
    if len(r)>1:o[0]|=(r[1]&0x40)>>5;o[0]|=(r[1]&0x10)>>4;o[1]|=(r[1]&0x0f)<<4
    if len(r)>2:o[1]|=(r[2]&0x40)>>3;o[1]|=(r[2]&0x10)>>2;o[1]|=(r[2]&0x0c)>>2;o[2]|=(r[2]&3)<<6
    if len(r)>3:o[2]|=(r[3]&0x40)>>1;o[2]|=r[3]&0x1f
    return bytes(o)
def vi(v:int)->bytes:
    v=int(v)&((1<<64)-1)
    if v<0x40:return bytes([v])
    o=bytearray([(v&0x3f)|0x80]);v>>=6
    while v>=0x80:o.append((v&0x7f)|0x80);v>>=7
    o.append(v&0x7f);return bytes(o)
def sv(s:str)->bytes:
    b=str(s).encode();return vi(len(b)+1)+b+b'\0'
def fi(t,v):return tag(t,T_VARINT)+vi(v)
def fs(t,v):return tag(t,T_STRING)+sv(v)
def fb(t,b):b=bytes(b);return tag(t,T_BLOB)+vi(len(b))+b
def fg(t,b):return tag(t,T_GROUP)+bytes(b)+b'\0'
def fl_int(t,vals):
    o=bytearray(tag(t,T_LIST));o.append(T_VARINT);o+=vi(len(vals));
    for v in vals:o+=vi(v)
    return bytes(o)
def fl_group(t,groups):
    o=bytearray(tag(t,T_LIST));o.append(T_GROUP);o+=vi(len(groups))
    for g in groups:o+=g+b'\0'
    return bytes(o)
def fm_ss(t,pairs):
    o=bytearray(tag(t,T_MAP));o+=bytes([T_STRING,T_STRING]);o+=vi(len(pairs))
    for k,v in pairs:o+=sv(k)+sv(v)
    return bytes(o)
def fm_empty(t,key_type,value_type):
    return tag(t,T_MAP)+bytes([key_type,value_type])+vi(0)
def fot(t,c,et):return tag(t,T_OBJECT_TYPE)+vi(c)+vi(et)
def foi(t,c,et,eid):return tag(t,T_OBJECT_ID)+vi(c)+vi(et)+vi(eid)
def decode_vi(raw,p):
    a=raw[p];p+=1;v=a&0x3f;sh=6
    if a&0x80:
        while 1:
            b=raw[p];p+=1;v|=(b&0x7f)<<sh
            if not b&0x80:break
            sh+=7
    return v,p
def get_str(raw:bytes,t:str):
    m=tag(t,T_STRING);p=raw.find(m)
    if p<0:return None
    try:n,p=decode_vi(raw,p+4);b=raw[p:p+n];return b[:-1].decode(errors='replace') if b.endswith(b'\0') else b.decode(errors='replace')
    except:return None

def get_int(raw:bytes,t:str):
    m=tag(t,T_VARINT);p=raw.find(m)
    if p<0:return None
    try:v,_=decode_vi(raw,p+4);return int(v)
    except:return None


def game_reporting_result_notification(reporting_id:int=0):
    # Blaze 3 GameReporting.ResultNotification (component 28, notification 114).
    # Aurora 17 completes submitOfflineGameReport asynchronously after the empty
    # RPC reply. FIFA's arena/skill-game path waits for this terminal result.
    rid=max(0,int(reporting_id or 0))
    return fi('EROR',0)+fi('FNL',1)+fi('GHID',rid)+fi('GRID',rid)


def stats_period_ids():
    # Blaze3 Stats.PeriodIds. Aurora 17 explicitly answers command 20, and FIFA
    # requests it during bootstrap. Values only need to be internally stable.
    now=int(time.time())
    day=now//86400
    week=day//7
    month=day//30
    return (fi('DBUF',0)+fi('DHOU',0)+fi('DLY',day)+fi('DRET',7)+
            fi('MBUF',0)+fi('MDAY',1)+fi('MHOU',0)+fi('MLY',month)+fi('MRET',12)+
            fi('WBUF',0)+fi('WDAY',1)+fi('WHOU',0)+fi('WLY',week)+fi('WRET',8))


def stats_stat_group(name:str):
    # Blaze3 Stats.StatGroupResponse. A zero-byte response leaves FIFA's arena
    # leaderboard request unresolved. Return a real, empty group contract.
    n=str(name or 'SkillGameStats')
    return (fs('CNAM',n)+fs('DESC','')+fot('ETYP',30722,1)+
            fm_empty('KSUM',T_STRING,T_VARINT)+fs('META','')+fs('NAME',n)+
            fl_group('STAT',[]))


def stats_key_scopes():
    # Blaze3 Stats.KeyScopes: map<string, KeyScopeItem>. No scopes are required
    # for local skill-game leaderboards, but the map member itself is required.
    return fm_empty('KSIT',T_STRING,T_GROUP)


def stats_leaderboard_group(name:str):
    # Blaze3 Stats.LeaderboardGroupResponse. The local board intentionally has
    # no remote rows; the client can still finish the skill-game bootstrap.
    n=str(name or 'SkillGame23')
    return (fi('ASCD',0)+fs('BNAM',n)+fs('DESC','')+fot('ETYP',30722,1)+
            fm_empty('KSUM',T_STRING,T_GROUP)+fi('LBSZ',0)+fl_group('LIST',[])+
            fs('META','')+fs('NAME',n)+fs('SNAM','score'))


def stats_centered_leaderboard():
    # Blaze3 Stats.LeaderboardStatValues. Empty LDLS is a valid zero-row board.
    return fl_group('LDLS',[])

def ascii_runs(b:bytes):
    out=[];cur=[]
    for x in b:
        if 32<=x<127:cur.append(chr(x))
        else:
            if len(cur)>=3:out.append(''.join(cur))
            cur=[]
    if len(cur)>=3:out.append(''.join(cur))
    return out[:30]

# ---- Blaze payloads ------------------------------------------------------
def preauth()->bytes:
    conf=fm_ss('CONF',[('connIdleTimeout','120s'),('defaultRequestTimeout','80s'),('pingPeriod','20s'),('voipHeadsetUpdateRate','1000')])
    b=bytearray();b+=fs('ASRC','3792815');b+=fl_int('CIDS',[1,9,30722]);b+=fg('CONF',conf);b+=fi('EEFA',1);b+=fs('ESRC','3792815');b+=fs('INST','fifa-2018-pc');b+=fi('MAID',89);b+=fi('MINR',0);b+=fs('NASP','cem_ea_id');b+=fs('PILD','');b+=fs('PLAT','pc');b+=fs('RSRC','3792815');b+=fs('SVER','Blaze 15.1.1.6.3');return bytes(b)
def core_config():
    fut=8099;easw=42232
    return [
      ('AUTH_TYPE','NUCLEUS'),('NUCLEUS_LOGIN_ENABLED','1'),('ORIGIN_LOGIN_ENABLED','1'),('OSDK_AUTH_REQUIRED','1'),('USE_TOKEN_AUTH','1'),
      ('FUT_RS4_BASE_URL',f'http://127.0.0.1:{fut}/'),('FIFA_POW_URL',f'http://127.0.0.1:{fut}/'),('FIFA_POW_CONTENT_SERVER_URL',f'http://127.0.0.1:{fut}/'),
      ('FIFA_POW_NUCLEUS_PROXY_URL',f'http://127.0.0.1:{fut}/'),('FUTDYNAMICMESSAGES_URL_BASE',f'http://127.0.0.1:{fut}/'),
      ('FUTDYNAMICMESSAGES_CUSTOMURL',f'127.0.0.1:{fut}'),('ONLINE/FUTDYNAMICMESSAGES_CUSTOMURL',f'127.0.0.1:{fut}'),
      ('OSDK_EASW_AUTH_URL',f'http://127.0.0.1:{easw}'),('OSDK_EASW_EVENT_URL',f'http://127.0.0.1:{easw}'),('OSDK_EASW_REQ_URL',f'http://127.0.0.1:{easw}'),
      ('ROSTERUPDATE_URL',f'http://127.0.0.1:{easw}/roster'),('ROUTINGCFGFILE_URL',f'http://127.0.0.1:{easw}/routing')]
def config(cfid):return fm_ss('CONF',core_config() if cfid=='OSDK_CORE' else [])+fs('ID',cfid)+fs('ITYP','')+fs('TOKN','')
def auth_payload():
    p=fs('DSNM',PERSONA)+fi('LAST',0)+fi('PID',FAKE_PERSONA)+fi('PLAT',4)+fi('STAS',0)+fi('XREF',FAKE_NUCLEUS)
    s=fi('1CON',0)+fi('BUID',FAKE_PERSONA)+fi('FRST',0)+fs('KEY',TOKEN)+fi('LLOG',int(time.time()))+fs('MAIL','local@localhost')+fg('PDTL',p)+fi('UID',FAKE_NUCLEUS)
    return fi('CNTX',0)+fi('ERRC',0)+fs('SKEY',TOKEN)+fi('ANON',0)+fi('NTOS',0)+fg('SESS',s)+fi('SPAM',0)+fi('UNDR',0)
def entitlements():
    gs=[]
    for i,n in enumerate(('FIFA18PCBoxContent','FIFA18PCOnlineAccess'),1):
        e=fs('DEVI','')+fs('GDAY','2017-09-29T00:00Z')+fs('GNAM',n)+fi('ID',i)+fi('ISCO',0)+fi('PID',FAKE_PERSONA)+fs('PJID','')+fi('PRCA',0)+fs('PRID',n)+fi('STAT',1)+fi('STRC',0)+fs('TAG','ONLINE_ACCESS')+fs('TDAY','')+fi('TYPE',1)+fi('UCNT',0)+fi('VER',0);gs.append(e)
    return fl_group('NLST',gs)
def telemetry():return fs('ADRS','127.0.0.1')+fi('ANON',0)+fs('DISA','1')+fs('FILT','')+fi('LOC',1701729619)+fi('PORT',9988)+fi('SDLY',0)+fs('SESS',TOKEN)+fs('SKEY','')+fi('SPCT',0)
def postauth():return fg('TELE',telemetry())+fg('TICK',fs('ADRS','127.0.0.1')+fi('PORT',8999)+fs('SKEY',''))+fg('UROP',fi('TMOP',0)+fi('UID',FAKE_PERSONA))
def ext_data():return fi('HWFG',0)+fg('QDAT',fi('DBPS',0)+fi('NATT',0)+fi('UBPS',0))+fi('UATT',0)
def notifications():
    user=fi('AID',FAKE_PERSONA)+fi('ALOC',1701729619)+fb('EXBB',b'')+fi('EXID',0)+fi('ID',FAKE_PERSONA)+fs('NAME',PERSONA)+fs('NASP','cem_ea_id')+fi('ORIG',FAKE_PERSONA)+fi('PIDI',0)
    n2=fg('DATA',ext_data())+fg('USER',user)
    n8=fi('BUID',FAKE_PERSONA)+foi('CGID',30722,2,FAKE_PERSONA)+fs('DSNM',PERSONA)+fs('KEY',TOKEN)+fi('LAST',int(time.time()))+fs('MAIL','local@localhost')+fs('NASP','cem_ea_id')+fi('PID',FAKE_PERSONA)+fi('PLAT',4)+fi('UID',FAKE_PERSONA)+fi('USTP',0)+fi('XREF',FAKE_NUCLEUS)
    n1=fg('DATA',ext_data())+fi('USID',FAKE_PERSONA);n5=fi('FLGS',3)+fi('ID',FAKE_PERSONA)
    return [(30722,2,n2,'UserAdded'),(30722,8,n8,'LoginSession'),(30722,1,n1,'ExtendedDataUpdate'),(30722,5,n5,'UserUpdated')]

@dataclass
class Packet:
    component:int;command:int;msg:int;typ:int;payload:bytes;metadata:bytes;raw:bytes

def parse(buf:bytearray):
    if len(buf)<16:return None
    ps=int.from_bytes(buf[:4],'big');ms=int.from_bytes(buf[4:6],'big');total=16+ps+ms
    if ps>16*1024*1024 or ms>1024*1024:raise ValueError('bad fire2 size')
    if len(buf)<total:return None
    raw=bytes(buf[:total]);del buf[:total]
    return Packet(int.from_bytes(raw[6:8],'big'),int.from_bytes(raw[8:10],'big'),int.from_bytes(raw[10:13],'big'),raw[13]>>5,raw[16+ms:],raw[16:16+ms],raw)
def frame(c,k,msg,typ,p=b'',m=b''):
    return len(p).to_bytes(4,'big')+len(m).to_bytes(2,'big')+c.to_bytes(2,'big')+k.to_bytes(2,'big')+(msg&0xffffff).to_bytes(3,'big')+bytes([(typ&7)<<5,0,0])+m+p
def draft_refresh_notification_frames():
    # Aurora17's DRAFT-REFRESH starts with UserSessions.ExtendedDataUpdate.  Its
    # follow-up UserUpdated is deliberately omitted: FIFA 18 is recorded as
    # closing Blaze when it arrives during FUT Draft.
    return [(frame(30722,1,0,2,dict((nl,np) for nc,nk,np,nl in notifications())['ExtendedDataUpdate']),'ExtendedDataUpdate')]

def route_blaze(p:Packet):
    c,k=p.component,p.command
    if (c,k)==(9,7):return preauth(),'Util.PreAuth',False
    if (c,k)==(9,2):return fi('STIM',int(time.time())),'Util.Ping',False
    if (c,k)==(9,1):
        cf=get_str(p.payload,'CFID') or '';return config(cf),f'Util.FetchClientConfig({cf})',False
    if (c,k)==(9,8):return postauth(),'Util.PostAuth',False
    if (c,k)==(9,5):return telemetry(),'Util.Telemetry',False
    if (c,k)==(9,12):return fm_ss('SMAP',[('FirstTimeFlag','0')]),'Util.GetUserOptions',False
    if (c,k)==(9,10):
        key=get_str(p.payload,'KEY') or '';return fs('DATA','0' if key=='FirstTimeFlag' else '')+fs('KEY',key),f'Util.GetUserOption({key})',False
    # FIFA changes this from MODE=1/STAT=0 to MODE=2/STAT=1 as the local match
    # scene is entered. It is a fieldless-success RPC, but naming it keeps the
    # launch trace distinct from genuinely unknown Blaze traffic.
    if (c,k)==(9,28):return b'','Util.SetClientState',False
    # Census subscription/data bookkeeping seen before and immediately after
    # match launch. Empty success is sufficient for the local/offline path.
    if (c,k)==(10,5):return b'','Census.Subscribe',False
    if (c,k)==(10,2):return b'','Census.GetCensusData',False
    # Post-arena bootstrap. Rooms remain fieldless, while the Stats commands
    # observed in the FIFA 18 trace now receive their generated Blaze3 shapes.
    if c==21 and k in (10,11,150):return b'',f'Rooms.ArenaBootstrap(0x{k:x})',False
    if c==7 and k==4:
        name=get_str(p.payload,'NAME') or 'SkillGameStats'
        return stats_stat_group(name),f'Stats.GetStatGroup({name})',False
    if c==7 and k==15:return stats_key_scopes(),'Stats.GetKeyScopesMap',False
    if c==7 and k==10:
        name=get_str(p.payload,'NAME') or 'SkillGame23'
        return stats_leaderboard_group(name),f'Stats.GetLeaderboardGroup({name})',False
    if c==7 and k==13:return stats_centered_leaderboard(),'Stats.GetCenteredLeaderboard',False
    if c==7 and k==20:return stats_period_ids(),'Stats.GetPeriodIds',False
    if c==7 and k==3:return fl_group('GRPS',[]),'Stats.GetStatGroupList',False
    # Aurora 17's working Draft/match path treats this as Blaze 3
    # GameReporting.submitOfflineGameReport. The RPC itself is an empty success;
    # the required terminal completion is sent asynchronously by BlazeHandler.
    if (c,k)==(28,2):return b'','GameReporting.SubmitOfflineGameReport',False
    if c==1 and k in (0x20,0x1d,0x30):return entitlements(),f'Authentication.Entitlements(0x{k:x})',False
    if c==1 and k==0x46:return b'','Authentication.Logout',False
    if c==1 and k==0x24:return fs('AUTH',TOKEN),'Authentication.GetAuthToken',False
    if c==1:
        # Login/account/persona commands differ slightly by Blaze generation. For this
        # first recon pass, preserve the normal RPC and return the handover's full local session.
        return auth_payload(),f'Authentication.LocalSession(0x{k:x})',True
    if c==30722:return b'',f'UserSessions.Empty(0x{k:x})',False
    return b'',f'UNKNOWN c={c} k=0x{k:x}',False

class ReuseTCP(socketserver.ThreadingMixIn,socketserver.TCPServer):allow_reuse_address=True;daemon_threads=True
class BlazeHandler(socketserver.BaseRequestHandler):
    def handle(self):
        peer=self.client_address;self.request.settimeout(120);buf=bytearray();log.warning('BLAZE CONNECT %s',peer)
        while True:
            try:
                p=parse(buf)
                if p is None:
                    b=self.request.recv(65536)
                    if not b:break
                    buf+=b;continue
            except socket.timeout:break
            except Exception as e:log.exception('BLAZE parse error %s',e);break
            # The c=10/k=5 stream is a very high-rate keepalive/state poll.  Older
            # builds wrote every copy as a separate tiny file (1,000+ per short run),
            # which made Windows Compress-Archive spend far longer enumerating files
            # than actually compressing data.  Keep the first eight binary samples for
            # diagnostics and retain the complete textual FIRE2 log for every frame.
            raw_key=(p.component,p.command,p.typ)
            raw_seen=RAW_CAPTURE_COUNTS.get(raw_key,0)
            RAW_CAPTURE_COUNTS[raw_key]=raw_seen+1
            save_raw=not ((p.component,p.command)==(10,5) and raw_seen>=8)
            if save_raw:
                rawname=f'{int(time.time()*1000)}-c{p.component}-k{p.command}-m{p.msg}.bin';(RAW/rawname).write_bytes(p.raw)
            elif raw_seen==8:
                log.warning('RAW CAPTURE suppressing repetitive c=10 k=5 binary frames after first 8; textual FIRE2 logging remains active')
            # c=10/k=5 arrives roughly 30 times/sec while FUT is open.  Sampling
            # it keeps report generation and later trace review fast without losing the
            # fact that the stream is alive.  Every non-poll frame is still logged.
            fire_seen=RAW_CAPTURE_COUNTS.get(('log',p.component,p.command,p.typ),0)
            RAW_CAPTURE_COUNTS[('log',p.component,p.command,p.typ)]=fire_seen+1
            log_fire=not ((p.component,p.command)==(10,5) and fire_seen>=20 and (fire_seen+1)%500!=0)
            if log_fire:
                log.warning('FIRE2 RX c=%d k=%d(0x%x) msg=%d type=%d bytes=%d ascii=%s',p.component,p.command,p.command,p.msg,p.typ,len(p.payload),ascii_runs(p.payload))
            elif fire_seen==20:
                log.warning('FIRE2 LOG sampling repetitive c=10 k=5 after first 20; one sample every 500 frames will remain')
            if p.typ==4:self.request.sendall(frame(p.component,p.command,p.msg,5));continue
            if p.typ!=0:continue
            payload,label,notify=route_blaze(p)
            try:self.request.sendall(frame(p.component,p.command,p.msg,1,payload))
            except OSError:break
            if log_fire:
                log.warning('FIRE2 TX %s payload=%d',label,len(payload))
            if (p.component,p.command)==(28,2):
                report_id=get_int(p.payload,'GRID') or 0
                # Aurora 17 emits the terminal notification immediately after the
                # empty submitOfflineGameReport response (roughly 20 ms later).
                time.sleep(0.020)
                result_payload=game_reporting_result_notification(report_id)
                try:
                    self.request.sendall(frame(28,114,0,2,result_payload))
                    log.warning('FIRE2 NOTIFY GameReporting.ResultNotification c=28 k=114 reportId=%d payload=%d',report_id,len(result_payload))
                except OSError:return
            # Disabled unsolicited UserUpdated notification on Blaze.
            # FIFA 18 immediately closes the Blaze connection (WinError 10054) when receiving
            # UserUpdated during FUT Draft and other transactions. Aurora 17 never sends this.
            if PENDING_USERSESSION_REFRESH.is_set():
                PENDING_USERSESSION_REFRESH.clear()
            if PENDING_DRAFT_EXTDATA_REFRESH.is_set():
                PENDING_DRAFT_EXTDATA_REFRESH.clear()
                for refresh_frame,refresh_label in draft_refresh_notification_frames():
                    try:self.request.sendall(refresh_frame);log.warning('FIRE2 NOTIFY %s c=30722 k=1 reason=draft-pick-refresh',refresh_label)
                    except OSError:return
            if notify:
                for nc,nk,np,nl in notifications():
                    try:self.request.sendall(frame(nc,nk,0,2,np));log.warning('FIRE2 NOTIFY %s c=%d k=%d',nl,nc,nk)
                    except OSError:return


class LoggingTLSServer(ThreadingHTTPServer):
    """HTTP server that performs TLS per accepted socket so handshake failures are logged."""
    def __init__(self, addr, handler, ctx):
        self.tls_context=ctx
        super().__init__(addr, handler)
    def get_request(self):
        raw, addr=self.socket.accept()
        try:
            tls=self.tls_context.wrap_socket(raw, server_side=True)
            log.warning('REDIRECTOR TLS OK %s version=%s cipher=%s', addr, tls.version(), tls.cipher())
            return tls, addr
        except Exception as e:
            log.warning('REDIRECTOR TLS FAIL %s error=%r', addr, e)
            try: raw.close()
            except Exception: pass
            raise OSError(str(e))

class QuietHTTP(BaseHTTPRequestHandler):
    protocol_version='HTTP/1.1'
    def log_message(self,fmt,*a):log.info('%s %s',self.server.label,fmt%a)
    def body(self):
        n=int(self.headers.get('Content-Length','0') or 0);return self.rfile.read(n) if n else b''
    def send(self,status,payload,ctype='application/json; charset=utf-8',headers=None):
        self.send_response(status);self.send_header('Content-Type',ctype);self.send_header('Content-Length',str(len(payload)));self.send_header('Connection','close')
        for k,v in (headers or {}).items():self.send_header(k,v)
        self.end_headers();self.wfile.write(payload)

class RedirectHandler(QuietHTTP):
    def go(self):
        body=self.body();log.warning('REDIRECTOR %s %s body=%r',self.command,self.path,body[:1000]);ip=2130706433
        x=f'''<?xml version="1.0" encoding="UTF-8"?>\n<serverinstanceinfo>\n <address member="0"><valu><hostname>127.0.0.1</hostname><ip>{ip}</ip><port>10051</port></valu></address>\n <secure>0</secure>\n</serverinstanceinfo>'''.encode()
        self.send(200,x,'application/xml')
    do_GET=go;do_POST=go

class EASWHandler(QuietHTTP):
    def go(self):
        body=self.body();log.warning('EASW %s %s headers=%s body=%r',self.command,self.path,dict(self.headers),body[:4096])
        path=urllib.parse.urlsplit(self.path).path.lower()
        if path in ('/roster','/routing','/ctl','/extension','/content'):self.send(200,b'','application/octet-stream')
        else:self.send(200,b'{}')
    do_GET=go;do_POST=go;do_PUT=go;do_DELETE=go;do_OPTIONS=go

# ---- FUT18 bootstrap / first-club compatibility --------------------------
# v0.7.7 got the retail client into FUT for the first time.  The successful
# trace then exposed a malformed-response retry storm: FIFA requested the POW
# catalog page 672 times because this server answered every POW route with {}.
# Keep this first pass deliberately conservative.  We now terminate every list
# with the retail-style container/endOfList contract and expose a small native-
# shaped local player set so the next onboarding request has real item objects
# available instead of an empty club.

HTTP_ROUTE_COUNTS = {}
CLIENT_DATA = {'onboarding': {'entries': [{'key':0,'value':0}]}, 'userHubData': {'entries': []}}
BOOT_TIME = int(time.time())
CLUB_ID = 1
CLUB_NAME = 'Local FC'
CLUB_ABBR = 'LCL'

# High-confidence base asset IDs shared by FIFA's player database across these
# PC titles.  This is intentionally only a bootstrap seed, not a claim that the
# full FIFA 18 FUT catalogue has been reconstructed yet.
STARTER_PLAYER_DEFS = [
    # 23 deterministic bronze base cards for a genuine fresh local club.
    # Wire order is FIFA 18 f442: GK,RB,CB,CB,LB,RM,CM,CM,LM,ST,ST,
    # followed by 12 substitutes/reserves.
    (163621,64,'GK',13,4,14,[62,60,58,71,39,62]),
    (211593,64,'RB',13,10,14,[70,31,45,64,62,65]),
    (192496,64,'CB',13,3,14,[58,38,47,49,64,70]),
    (230774,63,'CB',13,5,14,[63,32,43,48,65,62]),
    (203224,64,'LB',13,13,195,[56,35,49,53,63,62]),
    (221106,64,'RM',13,3,14,[68,58,59,64,53,71]),
    (240507,64,'CM',13,11,14,[72,59,63,71,26,46]),
    (239319,61,'CM',13,109,14,[64,51,54,63,52,56]),
    (237310,64,'LM',13,11,14,[73,55,61,67,60,60]),
    (2335,64,'ST',13,4,14,[36,61,53,52,32,64]),
    (230899,64,'ST',13,7,14,[84,67,47,73,21,63]),
    (216470,62,'GK',13,1960,44,[68,57,55,66,45,58]),
    (223963,62,'RB',13,10,14,[68,35,45,55,61,67]),
    (230918,60,'CB',13,5,14,[72,37,47,53,59,62]),
    (234378,59,'CB',13,19,25,[63,31,44,51,61,66]),
    (228637,61,'LB',13,1960,50,[56,30,56,57,60,61]),
    (237108,60,'RM',13,12,17,[70,60,53,59,32,44]),
    (241160,59,'CM',13,5,14,[63,44,60,63,44,47]),
    (240323,55,'CM',13,4,14,[67,46,55,59,39,52]),
    (236310,59,'LM',13,3,14,[71,36,42,59,58,57]),
    (237238,63,'ST',13,11,42,[64,59,61,55,60,66]),
    (228148,61,'ST',13,2,14,[77,59,45,65,29,51]),
    (231743,60,'ST',13,2,14,[71,55,47,57,40,58]),
]

def _attrs(face):
    return [{'index':i,'value':int(v)} for i,v in enumerate(face)]

def _extract_face_attributes(d, default_rating=50):
    """Extract the 6 card face attributes (PAC, SHO, PAS, DRI, DEF, PHY) from any player definition or card dict."""
    if not isinstance(d, dict):
        return [50, 50, 50, 50, 50, 50]
    face = d.get('face')
    if isinstance(face, (list, tuple)) and len(face) >= 6:
        f_vals = [int(x or 0) for x in face[:6]]
        if any(v != 50 for v in f_vals):
            return f_vals
    arr = d.get('attributeArray')
    if isinstance(arr, (list, tuple)) and len(arr) >= 6:
        a_vals = [int(x or 0) for x in arr[:6]]
        if any(v != 50 for v in a_vals):
            return a_vals
        elif not face:
            return a_vals
    attr_list = d.get('attributeList')
    if isinstance(attr_list, list) and len(attr_list) >= 6:
        by_idx = {}
        for a in attr_list:
            if isinstance(a, dict) and 'value' in a:
                by_idx[int(a.get('index', 0) or 0)] = int(a.get('value', 0) or 0)
        if len(by_idx) >= 6:
            l_vals = [by_idx.get(i, 50) for i in range(6)]
            if any(v != 50 for v in l_vals) or not (face or arr):
                return l_vals
    for stat_keys in [
        ('pace', 'shoot', 'pass', 'dribble', 'defend', 'physical'),
        ('pac', 'sho', 'pas', 'dri', 'def', 'phy'),
        ('PAC', 'SHO', 'PAS', 'DRI', 'DEF', 'PHY'),
    ]:
        if all(k in d for k in stat_keys):
            vals = [int(d.get(k, 0) or 0) for k in stat_keys]
            if any(v > 0 and v != 50 for v in vals):
                return vals
    if isinstance(face, (list, tuple)) and len(face) >= 6:
        return [int(x or 0) for x in face[:6]]
    if isinstance(arr, (list, tuple)) and len(arr) >= 6:
        return [int(x or 0) for x in arr[:6]]
    r = max(1, min(99, int(d.get('rating', default_rating) or default_rating)))
    return [r] * 6

# Retail FUT item payloads use a positional card-subtype family.  A captured
# userMassInfo schema from a FIFA 18-era client/API implementation has GK=0,
# defenders=1, central midfielders=2 and wide/forward players=3.  The string
# preferredPosition is retained because the native PC response model consumes
# it as text; attributeArray aliases are also supplied because later FUT builds
# moved the same six face values to the compact array form.
PLAYER_CARD_SUBTYPE = {
    'GK':0,
    'RWB':1,'RB':1,'CB':1,'LB':1,'LWB':1,
    'CDM':2,'CM':2,'CAM':2,
    'RM':3,'LM':3,'RF':3,'CF':3,'LF':3,'RW':3,'ST':3,'LW':3,
}

# Authentic FIFA 18 World Cup Russia (DLC) National Team IDs
# In World Cup mode (skuMode=WC), teamid/teamId must be the national team ID rather than a club ID.
# This binds the authentic National Team Federation crest and prevents missing-texture magenta boxes (#FF00FF).
WC_NATION_TO_TEAM = {
    7: 1325,    # Belgium
    10: 1411,   # Croatia
    13: 1330,   # Denmark
    14: 1386,   # England
    18: 1335,   # France
    21: 1337,   # Germany
    24: 1409,   # Iceland
    37: 1357,   # Poland
    38: 1360,   # Portugal
    40: 1394,   # Russia
    45: 1362,   # Spain
    46: 1365,   # Sweden
    47: 1402,   # Switzerland
    51: 1401,   # Serbia
    52: 1369,   # Argentina
    54: 1370,   # Brazil
    56: 1328,   # Colombia
    59: 1399,   # Peru
    60: 1375,   # Uruguay
    72: 1400,   # Costa Rica
    83: 1353,   # Mexico
    87: 1405,   # Panama
    111: 1396,  # Egypt
    129: 1397,  # Morocco
    133: 1410,  # Nigeria
    136: 1407,  # Senegal
    145: 1406,  # Tunisia
    161: 1398,  # Iran
    163: 1408,  # Japan
    167: 1403,  # Korea Republic
    183: 1395,  # Saudi Arabia
    195: 1318,  # Australia
    # Other major international nations as fallback
    4: 1322, 12: 1331, 22: 1338, 25: 1352, 27: 1343, 34: 1354, 35: 1387,
    36: 1355, 42: 1388, 48: 1366, 50: 1389, 53: 1324, 55: 1326, 57: 1332,
    58: 1358, 61: 1374, 70: 1391, 95: 1387, 103: 1327, 107: 1336, 108: 1344,
    140: 1361, 155: 1329, 159: 1341, 198: 1356,
}

# In World Cup mode, leagueId is the Confederation ID (1: UEFA, 2: CONMEBOL, 3: CONCACAF, 4: CAF, 5: AFC)
WC_NATION_TO_CONFEDERATION = {
    # 1: UEFA
    4: 1, 7: 1, 10: 1, 12: 1, 13: 1, 14: 1, 18: 1, 21: 1, 22: 1, 24: 1, 25: 1, 27: 1,
    34: 1, 35: 1, 36: 1, 37: 1, 38: 1, 40: 1, 42: 1, 43: 1, 44: 1, 45: 1, 46: 1, 47: 1,
    48: 1, 50: 1, 51: 1,
    # 2: CONMEBOL
    52: 2, 53: 2, 54: 2, 55: 2, 56: 2, 57: 2, 58: 2, 59: 2, 60: 2, 61: 2,
    # 3: CONCACAF
    70: 3, 72: 3, 83: 3, 87: 3, 95: 3,
    # 4: CAF
    103: 4, 107: 4, 108: 4, 111: 4, 129: 4, 133: 4, 136: 4, 140: 4, 145: 4,
    # 5: AFC
    155: 5, 159: 5, 161: 5, 163: 5, 167: 5, 183: 5, 195: 5,
    # 6: OFC
    198: 6,
}
# The 32 nations that participated in the 2018 FIFA World Cup Russia:
WC_32_NATIONS = {
    7, 10, 13, 14, 18, 21, 24, 37, 38, 40, 45, 46, 47, 51,  # UEFA (14)
    52, 54, 56, 59, 60,                                      # CONMEBOL (5)
    72, 83, 87,                                              # CONCACAF (3)
    111, 129, 133, 136, 145,                                 # CAF (5)
    161, 163, 167, 183, 195,                                 # AFC (5)
}

# Authentic World Cup Exclusive Icons added by EA specifically for the 2018 World Cup DLC
_WC_EXCLUSIVE_ICONS = [
    {'assetId': 190044, 'definitionId': 190044, 'resourceId': 190044, 'rating': 92, 'position': 'CB', 'nation': 14, 'nationality': 'England', 'teamId': 1386, 'teamid': 1386, 'leagueId': 1, 'name': 'Bobby Moore', 'displayName': 'Moore', 'clubName': 'Icons', 'rareflag': 12, 'rareFlag': 12, 'specialType': 'ICON', 'isSpecial': True, 'verifiedFifa18': True, 'face': [70, 64, 82, 78, 92, 85]},
    {'assetId': 214267, 'definitionId': 214267, 'resourceId': 214267, 'rating': 92, 'position': 'ST', 'nation': 14, 'nationality': 'England', 'teamId': 1386, 'teamid': 1386, 'leagueId': 1, 'name': 'Gary Lineker', 'displayName': 'Lineker', 'clubName': 'Icons', 'rareflag': 12, 'rareFlag': 12, 'specialType': 'ICON', 'isSpecial': True, 'verifiedFifa18': True, 'face': [88, 91, 74, 86, 40, 77]},
    {'assetId': 242510, 'definitionId': 242510, 'resourceId': 242510, 'rating': 87, 'position': 'ST', 'nation': 21, 'nationality': 'Germany', 'teamId': 1337, 'teamid': 1337, 'leagueId': 1, 'name': 'Miroslav Klose', 'displayName': 'Klose', 'clubName': 'Icons', 'rareflag': 12, 'rareFlag': 12, 'specialType': 'ICON', 'isSpecial': True, 'verifiedFifa18': True, 'face': [84, 88, 69, 81, 42, 82]},
    {'assetId': 242625, 'definitionId': 242625, 'resourceId': 242625, 'rating': 86, 'position': 'CAM', 'nation': 163, 'nationality': 'Japan', 'teamId': 1408, 'teamid': 1408, 'leagueId': 5, 'name': 'Hidetoshi Nakata', 'displayName': 'Nakata', 'clubName': 'Icons', 'rareflag': 12, 'rareFlag': 12, 'specialType': 'ICON', 'isSpecial': True, 'verifiedFifa18': True, 'face': [81, 79, 86, 85, 55, 74]},
]

_WC_DATA_FILE = ROOT / 'data' / 'fifa18-world-cup-players.json'
_WC_DATA = None
_WC_RATINGS_MAP = {}
_WC_ICONS_LIST = []
_WC_ICONS_BY_ASSET = {}

def _load_world_cup_data():
    global _WC_DATA, _WC_RATINGS_MAP, _WC_ICONS_LIST, _WC_ICONS_BY_ASSET
    if _WC_DATA is not None:
        return _WC_DATA
    if _WC_DATA_FILE.exists():
        try:
            doc = json.loads(_WC_DATA_FILE.read_text(encoding='utf-8'))
            _WC_DATA = doc
            _WC_ICONS_LIST = doc.get('icons', [])
            _WC_ICONS_BY_ASSET = {int(x['assetId']): x for x in _WC_ICONS_LIST}
            _WC_RATINGS_MAP = {int(k): v for k, v in doc.get('ratings', {}).items()}
            return _WC_DATA
        except Exception as e:
            log.warning('Failed to load WC player data: %s', e)
    return {}

def _world_cup_icons():
    """Return all 17 authentic Icon definitions available in FIFA 18 World Cup DLC."""
    _load_world_cup_data()
    if _WC_ICONS_LIST:
        return [dict(x) for x in _WC_ICONS_LIST]
    valid_base = [dict(d) for d in _icon_player_defs() if int(d.get('nation', 0)) in WC_32_NATIONS]
    return valid_base + [dict(x) for x in _WC_EXCLUSIVE_ICONS]

def _apply_world_cup_player_schema(item, defn=None):
    """Transform footballer card schema to match FIFA 18 World Cup DLC expectations.

    1. teamid/teamId: Replaced with National Team ID so the national badge displays and missing-texture magenta boxes are avoided.
    2. leagueId: Replaced with Confederation ID (UEFA, CONMEBOL, etc.).
    3. rareflag: Sanitized to World Cup-supported rarities (Gold Rare=1, Common=0, Icon=12, FoF=35) to prevent transparent 3D walkout shields.
    4. nation and nationId: Explicitly populated.
    5. clubId/clubid: Aligned with National Team ID.
    6. contracts/fitness: Infinite/99 for World Cup mode.
    7. rating and face attributes: Authentic World Cup ratings and 6 face stats applied.
    """
    x = dict(item or {})
    d = dict(defn or {})
    _load_world_cup_data()

    aid = int(x.get('assetId', 0) or d.get('assetId', 0))
    nation = int(x.get('nation') or x.get('nationId') or d.get('nation') or d.get('nationId') or 0)
    if nation <= 0 and aid > 0:
        try:
            base_def = _definition_by_asset(aid) or {}
            nation = int(base_def.get('nation', 0) or 0)
        except Exception:
            pass

    rf = int(x.get('rareflag', x.get('rareFlag', d.get('rareflag', d.get('rareFlag', 0)))) or 0)
    stype = str(x.get('specialType', d.get('specialType', ''))).upper()
    rating = int(x.get('rating', d.get('rating', 75)) or 75)

    if aid in _WC_ICONS_BY_ASSET:
        ic = _WC_ICONS_BY_ASSET[aid]
        rating = int(ic['rating'])
        x['rating'] = rating
        x['name'] = ic['name']
        x['displayName'] = ic.get('displayName', ic['name'])
        x['attributeArray'] = list(ic['face'])
        x['attributeList'] = _attrs(ic['face'])
        x['preferredPosition'] = ic.get('position', x.get('preferredPosition', 'ST'))
        x['position'] = ic.get('position', x.get('position', 'ST'))
        nation = int(ic['nation'])
        wc_rf = 12
    elif aid in _WC_RATINGS_MAP and rf != 35 and stype not in ('FOF', 'FESTIVAL OF FUTBALL'):
        wc_data = _WC_RATINGS_MAP[aid]
        rating = int(wc_data['rating'])
        x['rating'] = rating
        face = wc_data.get('face')
        if face and len(face) == 6:
            x['attributeArray'] = list(face)
            x['attributeList'] = _attrs(face)
        if 'position' in wc_data:
            x['preferredPosition'] = wc_data['position']
            x['position'] = wc_data['position']
        wc_rf = 1 if rating >= 75 else 0
    elif rf == 35 or stype in ('FOF', 'FESTIVAL OF FUTBALL'):
        wc_rf = 35
    elif stype == 'ICON' or rf == 12 or int(x.get('clubId', 0)) == 112658:
        wc_rf = 12
    elif rf != 0 or rating >= 75:
        wc_rf = 1
    else:
        wc_rf = 0

    attrs = x.get('attributeArray')
    attr_list = x.get('attributeList')
    if not attrs or len(attrs) < 6 or not attr_list or len(attr_list) < 6:
        base_def = _definition_by_asset(aid) if aid > 0 else {}
        face = _extract_face_attributes(base_def or x, default_rating=rating)
        x['attributeArray'] = face
        x['attributeList'] = _attrs(face)

    team_id = WC_NATION_TO_TEAM.get(nation)
    if team_id is None:
        team_id = int(x.get('teamid', x.get('teamId', 0)) or 0)
        if team_id <= 0:
            team_id = 1362
    confed_id = WC_NATION_TO_CONFEDERATION.get(nation, 1)

    x['nation'] = nation
    x['nationId'] = nation
    x['teamid'] = team_id
    x['teamId'] = team_id
    x['clubId'] = team_id
    x['clubid'] = team_id
    x['leagueId'] = confed_id
    x['rareflag'] = wc_rf
    x['rareFlag'] = wc_rf
    x['skuMode'] = 'WC'
    x['untradeable'] = True
    x['tradeable'] = False
    x['contract'] = 99
    x['contracts'] = 99
    x['loans'] = 0
    x['fitness'] = 99
    x['morale'] = 50
    return x

def _starter_item(idx, row, *, pile=7, state='free', item_id=None, loans=0):
    asset,rating,pos,league,club,nation,face=row
    iid=int(item_id if item_id is not None else 780000000000 + idx + 1)
    rare=1 if int(rating)>=75 else 0
    attrs=[int(x) for x in face]
    subtype=int(PLAYER_CARD_SUBTYPE.get(str(pos), 3))
    return {
        'id':iid, 'itemId':iid,
        'assetId':int(asset), 'definitionId':int(asset), 'resourceId':int(asset),
        'resourceGameYear':2019, 'itemType':'player', 'cardsubtypeid':subtype,
        'rating':int(rating), 'rareflag':rare, 'rareFlag':rare,
        'preferredPosition':str(pos), 'position':str(pos), 'leagueId':int(league),
        'teamid':int(club), 'teamId':int(club), 'nation':int(nation), 'nationId':int(nation),
        'attributeList':_attrs(attrs), 'attributeArray':attrs,
        'statsList':[], 'statsArray':[0,0,0,0,0],
        'lifetimeStats':[], 'lifetimeStatsArray':[0,0,0,0,0],
        'contract':max(99, int(loans) if int(loans)>0 else 99),
        'contracts':max(99, int(loans) if int(loans)>0 else 99),
        'fitness':99, 'morale':50,
        'formation':'f442', 'injuryGames':0, 'injuryType':'none',
        'suspension':0, 'training':0, 'trainingId':0, 'trainingResourceId':0,
        'owners':1, 'loyaltyBonus':1, 'loans':max(0,int(loans)), 'playStyle':250,
        'itemState':state, 'pile':int(pile),
        'untradeable':True, 'tradeable':False,
        'discardValue':0, 'lastSalePrice':0,
        'marketDataMinPrice':150, 'marketDataMaxPrice':15000000,
        'timestamp':BOOT_TIME,
        'posMods':[], 'assists':0, 'lifetimeAssists':0,
        # Harmless native player-detail defaults used by later item parsers.
        'skillmoves':3, 'weakfootabilitytypecode':3,
        'attackingworkrate':0, 'defensiveworkrate':0,
        'trait1':0, 'trait2':0, 'preferredfoot':1,
    }

STARTER_ITEMS=[_starter_item(i,r) for i,r in enumerate(STARTER_PLAYER_DEFS)]

# The active-squad slot contract is the critical v0.7.9 fix.  v0.7.8 placed
# player objects only behind guessed /squad routes, but the successful retail
# trace never called /squad before drawing the onboarding XI.  FIFA obtains the
# complete active squad directly from /userMassInfo.  Slot 0 is GK, 1..4 the
# back line, 5..8 midfield, and 9..10 the two forwards for f442.  Perfect
# chemistry is not required for onboarding; every starting slot only needs a
# real owned ItemData instance so the loan-replacement tutorial can select it.
SQUAD_SLOT_ITEM_INDEXES = list(range(23))

def _default_squad():
    players=[]
    used=set()
    for slot in range(23):
        row={'index':slot,'loyaltyBonus':1 if slot < len(SQUAD_SLOT_ITEM_INDEXES) else 0,
             'kitNumber':slot+1 if slot<11 else 0}
        if slot < len(SQUAD_SLOT_ITEM_INDEXES):
            item_idx=SQUAD_SLOT_ITEM_INDEXES[slot]
            item=STARTER_ITEMS[item_idx]
            if item['id'] not in used:
                used.add(item['id'])
                row['itemData']=dict(item)
        players.append(row)
    xi=[x for x in players[:11] if isinstance(x.get('itemData'),dict)]
    rating=round(sum(int(x['itemData'].get('rating',0)) for x in xi)/max(1,len(xi)))
    return {
        'id':1, 'squadId':1, 'valid':True, 'personaId':FAKE_PERSONA,
        'squadName':'Local XI', 'name':'Local XI', 'formation':'f442',
        'active':True, 'captain':STARTER_ITEMS[SQUAD_SLOT_ITEM_INDEXES[9]]['id'],
        'chemistry':100, 'changed':0, 'starRating':max(1,min(5,round((rating-55)/7))), 'rating':rating,
        'dreamSquad':None, 'newSquad':0, 'newsquad':0, 'squadType':'REGULAR_SQUAD',
        'custom':None,
        'players':players, 'actives':[], 'manager':[], 'club':[],
        'kicktakers':[],
    }

# Five loan choices are enough for the native onboarding carousel.  These use
# separate owned-item IDs and finite loan games, rather than reusing the club
# cards, so signing a loan can safely replace one starter later.
LOAN_PLAYER_SOURCE_INDEXES=[0,1,2,3,5]
LOAN_ITEMS=[
    _starter_item(100+i, STARTER_PLAYER_DEFS[src], pile=6, state='free',
                  item_id=781000000000+i+1, loans=7)
    for i,src in enumerate(LOAN_PLAYER_SOURCE_INDEXES)
]

# FIFA 18 onboarding kit selection.  v0.7.9 finally exposed the exact retail
# route: GET /ut/game/fifa18/onboarding/kits.  CardsDLL also contains the
# concrete FutGetOnboardingKitsResponse members homeItemDataList and
# awayItemDataList, and POST serialises {"homeKitId":N,"awayKitId":N} back to
# the same /kits path.  These definitions use high-confidence retail FUT kit
# resource IDs carried by the same Cards item family in the neighbouring PC
# titles; team IDs/category/card subtype are the stable fields used to render
# the actual in-game uniform.  The name aliases are intentionally explicit so
# the onboarding grid cannot fall back to the literal "undefined" label.
#
# teamId, club name, homeResourceId, awayResourceId, rating
KIT_TEAM_DEFS = [
    (1,   'Arsenal',            6300074, 6400057, 83),
    (5,   'Chelsea',            6300014, 6400007, 85),
    (10,  'Manchester City',    6300017, 6400008, 85),
    (11,  'Manchester United',  6300077, 6400058, 83),
    (21,  'Bayern München',     6300005, 6400004, 87),
    (22,  'Borussia Dortmund',  6300079, 6400059, 83),
    (45,  'Juventus',           6300082, 6400060, 83),
    (241, 'FC Barcelona',       6300010, 6400006, 86),
    (243, 'Real Madrid',        6300008, 6400005, 87),
]

BADGE_TEAM_DEFS = [
    (1,   'Arsenal',            6000057, 83),
    (5,   'Chelsea',            6000007, 85),
    (10,  'Manchester City',    6000008, 85),
    (11,  'Manchester United',  6000058, 83),
    (21,  'Bayern München',     6000004, 87),
    (22,  'Borussia Dortmund',  6000059, 83),
    (45,  'Juventus',           6000060, 83),
    (241, 'FC Barcelona',       6000006, 86),
    (243, 'Real Madrid',        6000005, 87),
]

def _kit_item(idx, team_id, club_name, resource_id, *, home):
    iid=782000000000+idx+1
    category=2 if home else 3
    state='free'
    name=f'{club_name} {"Home" if home else "Away"}'
    return {
        'id':iid,'itemId':iid,
        'timestamp':BOOT_TIME,'formation':'f442',
        'untradeable':True,'tradeable':False,
        'assetId':14 if home else 15,
        'definitionId':int(resource_id),'resourceId':int(resource_id),
        'resourceGameYear':2019,
        'rating':83,'itemType':'kit','cardsubtypeid':9,'cardassetid':35,
        'owners':1,'discardValue':0,'lastSalePrice':0,
        'itemState':state,'statsList':[],'lifetimeStats':[],'attributeList':[],
        'teamid':int(team_id),'teamId':int(team_id),'rareflag':1,'rareFlag':1,
        'leagueId':0,'pile':6,'category':category,'year':0,
        'name':name,'displayName':name,'description':name,'header':'Kit',
        'biodescription':name,'value':83,'weightrare':0,
    }

HOME_KIT_ITEMS=[]
AWAY_KIT_ITEMS=[]
for i,(team,name,home_rid,away_rid,rating) in enumerate(KIT_TEAM_DEFS):
    h=_kit_item(i,team,name,home_rid,home=True); h['rating']=rating; h['value']=rating
    a=_kit_item(100+i,team,name,away_rid,home=False); a['rating']=rating; a['value']=rating
    HOME_KIT_ITEMS.append(h); AWAY_KIT_ITEMS.append(a)


def _badge_item(idx, team_id, club_name, resource_id, rating):
    iid=783000000000+idx+1
    return {
        'id':iid,'itemId':iid,'timestamp':BOOT_TIME,'formation':'f442',
        'untradeable':True,'tradeable':False,
        'assetId':int(team_id),'definitionId':int(resource_id),'resourceId':int(resource_id),
        'resourceGameYear':2019,'rating':int(rating),'itemType':'custom',
        'cardsubtypeid':11,'cardassetid':39,'owners':1,'discardValue':0,
        'itemState':'free','statsList':[],'lifetimeStats':[],'attributeList':[],
        'teamid':int(team_id),'teamId':int(team_id),'rareflag':1,'rareFlag':1,
        'leagueId':0,'pile':6,'category':1,
        'name':club_name,'displayName':club_name,'description':club_name,
        'header':'Badge','biodescription':club_name,'value':int(rating),'weightrare':0,
    }

BADGE_ITEMS=[_badge_item(i,*row) for i,row in enumerate(BADGE_TEAM_DEFS)]

# Native FUT stadium item used by offline-match handoff. The new Aurora 17
# build's own contract tests require home kit + away kit + active stadium; the
# existing FIFA 18 save only carried kits + badge, so Draft entered the arena
# without a stadium to instantiate for kickoff.
DEFAULT_STADIUM_ITEM={
    'id':784000000001,'itemId':784000000001,'timestamp':BOOT_TIME,
    'formation':'f442','untradeable':True,'tradeable':False,
    'assetId':261,'definitionId':6200058,'resourceId':6200058,'resourceGameYear':2019,
    'rating':64,'itemType':'stadium','cardsubtypeid':10,'cardassetid':36,
    'owners':1,'discardValue':0,'lastSalePrice':0,'itemState':'activeStadium',
    'statsList':[],'lifetimeStats':[],'attributeList':[],
    'rareflag':0,'rareFlag':0,'leagueId':0,'pile':7,'category':4,'year':2019,
    'stadiumid':261,'stadiumId':261,'capacity':35000,'boost':0,
    'name':'Local FUT Stadium','displayName':'Local FUT Stadium',
    'description':'Local FUT Stadium','header':'Stadium','biodescription':'Local FUT Stadium',
    'value':64,'weightrare':0,
}
# v0.8.2 had no durable state, so the first v0.8.3 launch seeds the already-
# created local club as completed onboarding.  Manchester United matches the
# live v0.8.1/0.8.2 test club; every subsequent change is read from SQLite.
ONBOARDING_SELECTION={'homeKitId':6300077,'awayKitId':6400058,'badgeId':6000058}

_DB_LOCK=threading.RLock()

@contextmanager
def _db_connect():
    con=sqlite3.connect(str(DB_PATH),timeout=10)
    con.row_factory=sqlite3.Row
    con.execute('PRAGMA busy_timeout=5000')
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()

def _db_init():
    first=not DB_PATH.exists()
    with _DB_LOCK, _db_connect() as con:
        con.execute('PRAGMA journal_mode=WAL')
        con.execute('PRAGMA synchronous=NORMAL')
        con.execute('CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
        con.execute('CREATE TABLE IF NOT EXISTS items (id INTEGER PRIMARY KEY, data TEXT NOT NULL)')
        con.execute('CREATE TABLE IF NOT EXISTS squads (id INTEGER PRIMARY KEY, data TEXT NOT NULL)')
        con.execute('CREATE TABLE IF NOT EXISTS clientdata (key TEXT PRIMARY KEY, data TEXT NOT NULL)')
        con.execute('CREATE TABLE IF NOT EXISTS auctions (trade_id INTEGER PRIMARY KEY, data TEXT NOT NULL)')
        con.execute('CREATE TABLE IF NOT EXISTS sbc_progress (challenge_id INTEGER PRIMARY KEY, data TEXT NOT NULL)')
        con.execute('CREATE TABLE IF NOT EXISTS sbc_squads (challenge_id INTEGER PRIMARY KEY, data TEXT NOT NULL)')
        con.execute('CREATE TABLE IF NOT EXISTS sbc_favourites (set_id INTEGER PRIMARY KEY, updated_at INTEGER NOT NULL)')
        defaults={
            'clubName':CLUB_NAME,'clubAbbr':CLUB_ABBR,'credits':'0',
            'established':str(BOOT_TIME),'onboardingComplete':'1',
            'homeKitId':'6300077','awayKitId':'6400058','badgeId':'6000058',
            'activeSquadId':'1',
            'nextItemId':'790000000001','nextTradeId':'900000000001','playerDbCount':'0','playerDbSource':'bootstrap',
        }
        for k,v in defaults.items():
            con.execute('INSERT OR IGNORE INTO meta(key,value) VALUES(?,?)',(k,str(v)))
        n=con.execute('SELECT COUNT(*) FROM items').fetchone()[0]
        if not n:
            seed=[dict(x) for x in STARTER_ITEMS]
            # Preserve the kit/badge visible in the live continuation club too.
            h=dict(HOME_KIT_ITEMS[3]);h['pile']=7;h['itemState']='activeHomeKit'
            a=dict(AWAY_KIT_ITEMS[3]);a['pile']=7;a['itemState']='activeAwayKit'
            b=dict(BADGE_ITEMS[3]);b['pile']=7;b['itemState']='activeBadge'
            stadium=dict(DEFAULT_STADIUM_ITEM)
            seed += [h,a,b,stadium]
            con.executemany('INSERT OR REPLACE INTO items(id,data) VALUES(?,?)',[
                (int(x['id']),json.dumps(x,separators=(',',':'))) for x in seed
            ])
        # Existing v0.8.x saves predate stadium seeding. Migrate them in-place so
        # the next Draft match has the same native active-stadium contract as a
        # fresh club without asking the user to reset anything.
        has_stadium=False
        for row in con.execute('SELECT data FROM items').fetchall():
            try:
                item=json.loads(row[0])
                if str(item.get('itemState',''))=='activeStadium' or str(item.get('itemType',''))=='stadium':
                    has_stadium=True;break
            except Exception:
                pass
        if not has_stadium:
            stadium=dict(DEFAULT_STADIUM_ITEM)
            con.execute('INSERT OR REPLACE INTO items(id,data) VALUES(?,?)',
                        (int(stadium['id']),json.dumps(stadium,separators=(',',':'))))
            log.warning('LOCAL SAVE migrated: seeded active stadium id=%s resourceId=%s',stadium['id'],stadium['resourceId'])
        n=con.execute('SELECT COUNT(*) FROM squads').fetchone()[0]
        if not n:
            sq=_default_squad()
            con.execute('INSERT OR REPLACE INTO squads(id,data) VALUES(?,?)',(1,json.dumps(sq,separators=(',',':'))))
        for key,doc in (
            ('onboarding',{'entries':[{'key':0,'value':0}]}),
            ('userHubData',{'entries':[]}),
        ):
            con.execute('INSERT OR IGNORE INTO clientdata(key,data) VALUES(?,?)',(key,json.dumps(doc,separators=(',',':'))))
    if first:
        log.warning('LOCAL SAVE created %s with %d bronze starter players + active kits/badge/stadium and 0 coins',DB_PATH,len(STARTER_ITEMS))
    else:
        log.warning('LOCAL SAVE loaded %s',DB_PATH)

def _meta_get(key,default=''):
    with _DB_LOCK, _db_connect() as con:
        row=con.execute('SELECT value FROM meta WHERE key=?',(str(key),)).fetchone()
    return row['value'] if row else default

def _meta_set(key,value):
    with _DB_LOCK, _db_connect() as con:
        con.execute('INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)',(str(key),str(value)))

def _credits():
    try:return max(0,int(_meta_get('credits','0')))
    except Exception:return 0

def _club_name():return _meta_get('clubName',CLUB_NAME) or CLUB_NAME

def _club_abbr():return _meta_get('clubAbbr',CLUB_ABBR) or CLUB_ABBR

def _established():
    try:return int(_meta_get('established',str(BOOT_TIME)))
    except Exception:return BOOT_TIME


# ---- FIFA 18 base player database -----------------------------------------
# Base-card fallback: public FIFA 18 career-mode snapshot. This is not mislabeled as EA's FUT static JSON.
# on first use and cache a compact copy under LocalAppData.  The ID column is
# the EA/SoFIFA player asset ID; FUTBIN is useful for validating those IDs, but
# its /18/player/<number>/ URL component is a FUTBIN record number, not assetId.
# Historical EA FUT player-name/rating snapshot. This is a 2018 repository
# copy of the EA players.json payload used by FUT tooling at the time; the live
# EA CDN endpoint is no longer reliable. We use it as an allow-list/identity
# source and merge club/position/stat metadata from the FIFA 18 snapshot below.
OFFICIAL_PLAYER_SNAPSHOT_URLS = [
    'https://raw.githubusercontent.com/futapi/fut/54c70dc6fe0ae0460975628b2ec8c99998e935a6/tests/data/players.json',
]
OFFICIAL_PLAYER_CACHE = PLAYER_DATA_DIR/'fifa18-ea-fut-players-20180422.json'
PLAYER_DB_URLS = [
    'https://raw.githubusercontent.com/4m4n5/fifa18-all-player-statistics/master/Complete/CompleteDataset.csv',
    'https://cdn.jsdelivr.net/gh/4m4n5/fifa18-all-player-statistics@master/Complete/CompleteDataset.csv',
    'https://raw.githubusercontent.com/fifa-players/Fifa/master/CompleteDataset.csv',
]
_PLAYER_DEFS = None
_PLAYER_DEF_MAP = None
_PLAYER_DB_LOCK = threading.RLock()

# FUT nation IDs used by the native card renderer. Unknown countries are left
# at 0 rather than inventing an ID; the assetId still resolves the player name/
# head from FIFA's installed database. This list covers the large majority of
# players likely to appear in normal packs and can be expanded from traces.
NATION_IDS = {
    'Albania':1,'Algeria':97,'Angola':98,'Argentina':52,'Armenia':3,'Australia':195,
    'Austria':4,'Belgium':7,'Bolivia':53,'Bosnia Herzegovina':8,'Brazil':54,
    'Bulgaria':9,'Cameroon':103,'Canada':70,'Chile':55,'China PR':155,'Colombia':56,
    'Costa Rica':72,'Croatia':10,'Czech Republic':12,'Denmark':13,'Ecuador':57,
    'Egypt':111,'England':14,'Finland':17,'France':18,'Gabon':115,'Germany':21,
    'Ghana':117,'Greece':22,'Guinea':118,'Hungary':23,'Iceland':24,'Iran':161,
    'Israel':26,'Italy':27,'Ivory Coast':108,'Côte d\'Ivoire':108,'Japan':163,
    'Korea Republic':167,'Mexico':83,'Morocco':129,'Netherlands':34,'New Zealand':198,
    'Nigeria':133,'Northern Ireland':35,'Norway':36,'Paraguay':58,'Peru':59,
    'Poland':37,'Portugal':38,'Republic of Ireland':25,'Romania':39,'Russia':40,
    'Saudi Arabia':183,'Scotland':42,'Senegal':136,'Serbia':51,'Slovakia':43,
    'Slovenia':44,'South Africa':140,'Spain':45,'Sweden':46,'Switzerland':47,
    'Tunisia':145,'Turkey':48,'Ukraine':49,'United States':95,'Uruguay':60,
    'Venezuela':61,'Wales':50,'Dominican Republic':207,'DR Congo':110,'Togo':144,
    'Macedonia':19,'FYR Macedonia':19,'Georgia':20,'Cyprus':11,'Estonia':15,
    'Latvia':30,'Lithuania':31,'Luxembourg':32,'Malta':33,'Moldova':16,
    'Azerbaijan':5,'Belarus':6,'Kazakhstan':165,'Uzbekistan':191,'Syria':186,
    'Iraq':162,'Jordan':164,'Lebanon':166,'Oman':178,'Qatar':182,'UAE':190,
    'United Arab Emirates':190,'Bahrain':150,'Kuwait':168,'Kenya':120,'Mali':126,
    'Burkina Faso':101,'Cape Verde':104,'Guinea-Bissau':119,'Sierra Leone':138,
    'Zimbabwe':148,'Zambia':147,'Madagascar':124,'Mozambique':130,'Uganda':146,
    'Tanzania':142,'Benin':100,'Jamaica':75,'Trinidad & Tobago':93,'Honduras':81,
    'Panama':87,'Haiti':80,'Cuba':74,'Curaçao':85,
}

# High-value league mappings by EA club ID. Unknown leagues are 0 for now; the
# club ID itself is parsed directly from the dataset's EA club-logo URL.
LEAGUE_BY_CLUB = {}
def _league(ids, league):
    for i in ids: LEAGUE_BY_CLUB[int(i)] = int(league)
# Premier League 2017/18
_league([1,2,3,4,5,7,9,10,11,12,13,15,18,19,95,109,1795,1796,1808,1960],13)
# Bundesliga, Ligue 1, LaLiga, Serie A / Calcio A (major clubs + common IDs).
_league([21,22,23,28,31,32,34,36,38,10029,10030,10031,10032,100409,100410,111239,111395,112172,112173],19)
_league([73,66,69,71,72,74,76,77,78,79,80,1819,1823,217,219,226,224],16)
_league([241,243,240,242,244,245,246,247,448,449,450,452,453,461,462,463,481,483,1860,110832],53)
_league([45,46,47,48,50,52,54,55,56,57,59,1842,189,39,206,205,1746,199],31)


def _ival(v, default=0):
    if v is None: return int(default)
    m=re.search(r'-?\d+(?:\.\d+)?', str(v).replace(',',''))
    if not m:return int(default)
    try:return int(round(float(m.group(0))))
    except Exception:return int(default)

def _avg(row, names, default=50):
    vals=[_ival(row.get(n),0) for n in names]
    vals=[v for v in vals if v>0]
    return max(1,min(99,int(round(sum(vals)/len(vals))))) if vals else int(default)

def _face_from_csv(row,pos):
    pos=str(pos or '').upper()
    if pos=='GK':
        return [
            _ival(row.get('GK diving'),50),_ival(row.get('GK handling'),50),
            _ival(row.get('GK kicking'),50),_ival(row.get('GK reflexes'),50),
            _avg(row,['Acceleration','Sprint speed'],45),_ival(row.get('GK positioning'),50),
        ]
    return [
        _avg(row,['Acceleration','Sprint speed']),
        _avg(row,['Finishing','Shot power','Long shots','Volleys','Positioning']),
        _avg(row,['Short passing','Long passing','Vision','Crossing','Curve']),
        _avg(row,['Dribbling','Ball control','Agility','Balance','Reactions']),
        _avg(row,['Marking','Standing tackle','Interceptions','Sliding tackle','Heading accuracy']),
        _avg(row,['Strength','Stamina','Aggression','Jumping']),
    ]

def _normalise_position(pos):
    pos=str(pos or '').strip().upper()
    return {'LAM':'CAM','RAM':'CAM','LCM':'CM','RCM':'CM','LDM':'CDM','RDM':'CDM',
            'LCB':'CB','RCB':'CB','LS':'ST','RS':'ST'}.get(pos,pos)

def _csv_player_defs(path):
    out=[]
    with open(path,'r',encoding='utf-8-sig',newline='',errors='replace') as fh:
        reader=csv.DictReader(fh)
        for row in reader:
            asset=_ival(row.get('ID'),0)
            rating=_ival(row.get('Overall'),0)
            if asset<=0 or rating<=0:continue
            raw_pos=str(row.get('Preferred Positions') or '').strip().upper().replace(',', ' ')
            positions=[]
            for raw in raw_pos.split():
                pos=_normalise_position(raw)
                if pos and pos not in positions:positions.append(pos)
            pos=positions[0] if positions else 'CM'
            club_logo=str(row.get('Club Logo') or '')
            m=re.search(r'/teams/(\d+)',club_logo)
            club=_ival(m.group(1),0) if m else 0
            nation_name=str(row.get('Nationality') or '').strip()
            # CompleteDataset's Flag URL embeds the actual FIFA nation id. This
            # is more complete than a hand-maintained country-name table and
            # fixes nation metadata for the long tail of the FIFA 18 roster.
            flag=str(row.get('Flag') or '')
            nm=re.search(r'/flags/(\d+)',flag)
            nation=_ival(nm.group(1),0) if nm else int(NATION_IDS.get(nation_name,0))
            league=int(LEAGUE_BY_CLUB.get(club,0))
            out.append({
                'assetId':asset,'rating':rating,'position':pos,'positions':positions or [pos],
                'leagueId':league,'teamId':club,'nation':nation,
                'name':str(row.get('Name') or '').strip(),
                'nationality':nation_name,'clubName':str(row.get('Club') or '').strip(),
                'face':_face_from_csv(row,pos),
            })
    # Deduplicate by EA asset ID while retaining the first record from the FIFA18 snapshot.
    unique={}
    for d in out:unique.setdefault(int(d['assetId']),d)
    return list(unique.values())

def _write_player_cache(rows, source):
    tmp=PLAYER_CACHE.with_suffix('.tmp')
    tmp.write_text(json.dumps({'source':source,'gameYear':2018,'schemaVersion':2,'players':rows},separators=(',',':')),encoding='utf-8')
    tmp.replace(PLAYER_CACHE)
    _meta_set('playerDbCount',len(rows));_meta_set('playerDbSource',source)

def _download_player_csv(force=False):
    # v0.8.9.29 could mistake the old 15.3k filtered cache CSV for a complete
    # FIFA 18 roster merely because it was larger than 1 MB.  A forced refresh
    # is used when parsing proves the cached file has fewer than 17.5k assets.
    if not force and PLAYER_CSV.exists() and PLAYER_CSV.stat().st_size>1000000:return True
    for url in PLAYER_DB_URLS:
        try:
            log.warning('PLAYER DB download starting %s',url)
            req=urllib.request.Request(url,headers={'User-Agent':'FIFA18LocalFUT/0.8.5','Accept':'text/csv,*/*'})
            with _remote_urlopen(req,timeout=12) as r:
                data=r.read()
            if len(data)<1000000:raise RuntimeError(f'download too small: {len(data)} bytes')
            PLAYER_CSV.write_bytes(data)
            log.warning('PLAYER DB downloaded bytes=%d to %s',len(data),PLAYER_CSV)
            return True
        except Exception as e:
            log.warning('PLAYER DB download failed url=%s error=%s',url,e)
    return False

def _bootstrap_player_defs():
    rows=[]
    for asset,rating,pos,league,club,nation,face in STARTER_PLAYER_DEFS:
        rows.append({'assetId':int(asset),'rating':int(rating),'position':str(pos),'leagueId':int(league),'teamId':int(club),'nation':int(nation),'name':'','nationality':'','clubName':'','face':[int(x) for x in face]})
    return rows

def _official_fut_player_snapshot(allow_download=True):
    """Return the preserved 2018 EA FUT Players list keyed by asset ID.

    LegendsPlayers is intentionally not folded into normal gold/base cards: those
    entries are Icon/Legend assets and need their own FUT rarity metadata.
    """
    doc=None
    if OFFICIAL_PLAYER_CACHE.exists():
        try:doc=json.loads(OFFICIAL_PLAYER_CACHE.read_text(encoding='utf-8'))
        except Exception as e:log.warning('OFFICIAL PLAYER snapshot cache read failed: %s',e)
    online=str(os.environ.get('FUT18_OFFICIAL_PLAYER_DB_ONLINE','1')).strip().lower() not in ('0','false','no','off')
    if doc is None and allow_download and online:
        for url in OFFICIAL_PLAYER_SNAPSHOT_URLS:
            try:
                req=urllib.request.Request(url,headers={'User-Agent':'FIFA18LocalFUT/0.8.9.36.22','Accept':'application/json,*/*'})
                with _remote_urlopen(req,timeout=5.0) as r:raw=r.read(8_000_000)
                candidate=json.loads(raw.decode('utf-8-sig','replace'))
                if not isinstance(candidate,dict) or not isinstance(candidate.get('Players'),list) or len(candidate.get('Players',[]))<10000:
                    raise RuntimeError('historical EA player snapshot failed validation')
                OFFICIAL_PLAYER_CACHE.parent.mkdir(parents=True,exist_ok=True)
                OFFICIAL_PLAYER_CACHE.write_text(json.dumps(candidate,separators=(',',':')),encoding='utf-8')
                doc=candidate
                log.warning('OFFICIAL PLAYER snapshot recovered players=%d legends=%d source=%s',len(candidate.get('Players',[])),len(candidate.get('LegendsPlayers',[])),url)
                break
            except Exception as e:log.warning('OFFICIAL PLAYER snapshot unavailable url=%s: %s',url,e)
    if not isinstance(doc,dict):return None
    players=doc.get('Players',[])
    if not isinstance(players,list) or len(players)<10000:return None
    out={}
    for x in players:
        try:aid=int(x.get('id',0) or 0);rating=int(x.get('r',0) or 0)
        except Exception:continue
        if aid<=0:continue
        first=str(x.get('f','') or '').strip();last=str(x.get('l','') or '').strip();common=str(x.get('c','') or '').strip()
        out[aid]={'assetId':aid,'rating':rating,'name':common or (' '.join(v for v in (first,last) if v).strip()),'firstName':first,'lastName':last}
    return out if len(out)>=10000 else None

def _apply_official_fut_identity(rows,allow_download=True):
    """Overlay preserved EA FUT names/ratings without deleting FIFA 18 players.

    Older builds treated the historical FUT players.json as an allow-list and
    therefore shrank the 17,981-player FIFA 18 database to roughly 15.3k base
    assets.  That snapshot is still valuable as an identity authority where it
    overlaps, but it is not a complete roster authority.  Keep every valid
    FIFA 18 metadata row and overlay the preserved FUT name/rating on matches.
    """
    official=_official_fut_player_snapshot(allow_download)
    if not official:return rows,None
    merged=[];matched=0;metadata_only=0
    for raw in rows:
        if not isinstance(raw,dict):continue
        d=dict(raw)
        try:aid=int(d.get('assetId',0) or 0)
        except Exception:aid=0
        if aid<=0:continue
        o=official.get(aid)
        if o:
            matched+=1
            if int(o.get('rating',0) or 0)>0:d['rating']=int(o['rating'])
            if o.get('name'):d['name']=str(o['name'])
            d['verifiedFutBase']=True
            d['identitySource']='EA FUT players.json snapshot 2018-04-22'
        else:
            metadata_only+=1
            d['verifiedFifa18Roster']=True
            d['identitySource']='FIFA18 CompleteDataset roster'
        merged.append(d)
    if len(merged)<10000:
        log.warning('FULL PLAYER DB merge rejected rows=%d',len(merged))
        return rows,None
    log.warning('FULL PLAYER DB identity overlay retained=%d futSnapshotMatches=%d additionalFifa18Roster=%d',len(merged),matched,metadata_only)
    return merged,'FIFA18 CompleteDataset full roster + EA FUT players.json identity overlay'

def _load_player_defs(allow_download=True):
    global _PLAYER_DEFS,_PLAYER_DEF_MAP
    with _PLAYER_DB_LOCK:
        if _PLAYER_DEFS is not None:return _PLAYER_DEFS
        rows=[];source='bootstrap';cache_schema=0
        cache_source=PLAYER_CACHE if PLAYER_CACHE.exists() else (BUNDLED_PLAYER_CACHE if BUNDLED_PLAYER_CACHE.exists() else None)
        if cache_source is not None:
            try:
                doc=json.loads(cache_source.read_text(encoding='utf-8'))
                rows=doc.get('players',[]) if isinstance(doc,dict) else []
                source=str(doc.get('source','bundled cache' if cache_source==BUNDLED_PLAYER_CACHE else 'cache')) if isinstance(doc,dict) else str(cache_source)
                cache_schema=int(doc.get('schemaVersion',0) or 0) if isinstance(doc,dict) else 0
                # Copy the packaged database into LocalAppData once so later local
                # metadata updates remain independent from the release directory.
                if cache_source==BUNDLED_PLAYER_CACHE and rows and not PLAYER_CACHE.exists():
                    try:
                        PLAYER_CACHE.parent.mkdir(parents=True,exist_ok=True)
                        PLAYER_CACHE.write_text(json.dumps(doc,separators=(',',':')),encoding='utf-8')
                    except Exception as e:log.warning('PLAYER DB bundled cache copy failed: %s',e)
            except Exception as e:log.warning('PLAYER DB cache read failed: %s',e)
        candidates=[ROOT/'data'/'CompleteDataset.csv',PLAYER_CSV]
        local_csv=next((x for x in candidates if x.exists() and x.stat().st_size>1000000),None)
        # Upgrade an old/short cache from the local CSV when it is available.
        # Fresh installs download the same public FIFA18 snapshot below.
        needs_refresh=(not rows) or len(rows)<17500
        needs_schema_upgrade=(cache_schema<2 and local_csv is not None)
        if needs_refresh and local_csv is None and allow_download:
            if _download_player_csv():local_csv=PLAYER_CSV
        if (needs_refresh or needs_schema_upgrade) and local_csv is not None:
            try:
                parsed=_csv_player_defs(local_csv)
                if len(parsed)>=17500:
                    rows=parsed;source=str(local_csv);cache_schema=2
                    _write_player_cache(rows,source)
                    log.warning('FULL PLAYER DB cache upgraded rows=%d source=%s',len(rows),source)
                else:
                    # Never block server startup on a network refresh.  The
                    # cached 15.3k snapshot is still perfectly usable for FUT
                    # boot/search, so keep it for this run and let the daemon
                    # refresh upgrade to the complete 17.9k roster after the
                    # listening sockets are live.
                    log.warning('FULL PLAYER DB short local CSV rows=%d source=%s; using cached roster now, background refresh will upgrade it',len(parsed),local_csv)
                    if not rows:
                        rows=parsed;source=str(local_csv);cache_schema=2
            except Exception as e:log.exception('PLAYER DB parse failed: %s',e)
        if not rows:rows=_bootstrap_player_defs();source='bootstrap'
        # Preserve the complete FIFA18 roster. The historical EA FUT snapshot
        # overlays authoritative FUT names/ratings where IDs intersect but no
        # longer acts as an allow-list that deletes valid FIFA18 players.
        if len(rows)>=10000:
            rows,official_source=_apply_official_fut_identity(rows,allow_download)
            if official_source:source=official_source
        _PLAYER_DEFS=[x for x in rows if isinstance(x,dict) and _ival(x.get('assetId'),0)>0]
        for x in _PLAYER_DEFS:
            if 'face' not in x or not x.get('face'):
                x['face']=_extract_face_attributes(x)
        _PLAYER_DEF_MAP={int(x['assetId']):x for x in _PLAYER_DEFS}
        log.warning('PLAYER DB ready count=%d source=%s completeRoster=%s',len(_PLAYER_DEFS),source,len(_PLAYER_DEFS)>=17500)
        return _PLAYER_DEFS

def _background_refresh_full_player_db():
    """Refresh the 17.9k FIFA 18 roster without delaying local-server readiness.

    v0.8.9.29.1 performed the forced 6+ MB CSV download inside startup. On a
    slow/blocked GitHub connection that kept port 8099 closed long enough for
    run_trace.ps1 to declare the server dead.  Start with the last known-good
    local cache, then upgrade atomically in this daemon thread.
    """
    global _PLAYER_DEFS,_PLAYER_DEF_MAP
    try:
        current=list(_PLAYER_DEFS or [])
        if len(current)>=17500:
            log.warning('FULL PLAYER DB background refresh not needed rows=%d',len(current))
            return
        log.warning('FULL PLAYER DB background refresh armed currentRows=%d; server remains usable during download',len(current))
        if not _download_player_csv(force=True):
            log.warning('FULL PLAYER DB background refresh unavailable; retaining %d cached definitions for this run',len(current))
            return
        fresh=_csv_player_defs(PLAYER_CSV)
        if len(fresh)<17500:
            log.warning('FULL PLAYER DB background refresh rejected short dataset rows=%d; retaining %d cached definitions',len(fresh),len(current))
            return
        fresh,official_source=_apply_official_fut_identity(fresh,False)
        source=official_source or str(PLAYER_CSV)
        _write_player_cache(fresh,source)
        with _PLAYER_DB_LOCK:
            _PLAYER_DEFS=[x for x in fresh if isinstance(x,dict) and _ival(x.get('assetId'),0)>0]
            for x in _PLAYER_DEFS:
                if 'face' not in x or not x.get('face'):
                    x['face']=_extract_face_attributes(x)
            _PLAYER_DEF_MAP={int(x['assetId']):x for x in _PLAYER_DEFS}
            upgraded=list(_PLAYER_DEFS)
        changed=_refresh_owned_base_player_metadata(upgraded)
        log.warning('FULL PLAYER DB background refresh READY rows=%d metadataChanged=%d source=%s',len(upgraded),changed,source)
    except Exception as e:
        log.exception('FULL PLAYER DB background refresh failed; keeping current local roster: %s',e)

def _definition_by_asset(asset):
    _load_player_defs()
    try:return _PLAYER_DEF_MAP.get(int(asset))
    except Exception:return None

def _definition_by_resource(resource):
    try:resource=int(resource)
    except Exception:return None
    # Special resource IDs are not keys in the base asset map. Check local
    # variations first, then fall back to the base definition.
    for d in _special_player_defs():
        try:
            if int(d.get('resourceId',d.get('definitionId',0)) or 0)==resource:return d
        except Exception:pass
    return _definition_by_asset(resource)

def _native_walkout_asset_ids():
    """Build the native 86+ walkout identity list from cards that can actually be packed.

    Include verified special variations as well as base cards. The native list is keyed
    by footballer asset ID, while the purchase response carries the exact resourceId.
    """
    rows=list(_load_player_defs(False))
    try: rows += list(_special_player_defs())
    except Exception: pass
    ids=sorted({int(x.get('assetId',0) or 0) for x in rows
                if isinstance(x,dict) and int(x.get('rating',0) or 0)>=86
                and int(x.get('assetId',0) or 0)>0})
    return ids

def _next_item_id():
    with _DB_LOCK:
        try:n=int(_meta_get('nextItemId','790000000001'))
        except Exception:n=790000000001
        _meta_set('nextItemId',n+1)
        return n

AUTH_PLAYER_OVERRIDES = {
    7763: {'nation': 27, 'nationality': 'Italy', 'teamId': 112828, 'clubName': 'New York City FC', 'leagueId': 39},
    143001: {'nation': 52, 'nationality': 'Argentina', 'teamId': 1877, 'clubName': 'Boca Juniors', 'leagueId': 353},
    21146: {'nation': 14, 'nationality': 'England', 'teamId': 11, 'clubName': 'Manchester United', 'leagueId': 13},
    53050: {'nation': 144, 'nationality': 'Togo', 'teamId': 101014, 'clubName': 'Medipol Başakşehir', 'leagueId': 68},
    220523: {'nation': 56, 'nationality': 'Colombia', 'teamId': 241, 'clubName': 'FC Barcelona', 'leagueId': 53},
    193744: {'nation': 3, 'nationality': 'Armenia', 'teamId': 1, 'clubName': 'Arsenal', 'leagueId': 13},
    210282: {'nation': 54, 'nationality': 'Brazil', 'teamId': 52, 'clubName': 'Roma', 'leagueId': 31},
    201188: {'nation': 14, 'nationality': 'England', 'teamId': 5, 'clubName': 'Chelsea', 'leagueId': 13},
    172224: {'nation': 14, 'nationality': 'England', 'teamId': 17, 'clubName': 'Southampton', 'leagueId': 13},
    192557: {'nation': 21, 'nationality': 'Germany', 'teamId': 166, 'clubName': 'Hertha BSC', 'leagueId': 19},
    210954: {'nation': 7, 'nationality': 'Belgium', 'teamId': 5, 'clubName': 'Chelsea', 'leagueId': 13},
    53612: {'nation': 14, 'nationality': 'England', 'teamId': 1806, 'clubName': 'Stoke City', 'leagueId': 13},
    236772: {'nation': 34, 'nationality': 'Netherlands', 'teamId': 245, 'clubName': 'Ajax', 'leagueId': 10},
    221639: {'nation': 207, 'nationality': 'Dominican Republic', 'teamId': 66, 'clubName': 'Olympique Lyonnais', 'leagueId': 16},
    189157: {'nation': 110, 'nationality': 'DR Congo', 'teamId': 7, 'clubName': 'Everton', 'leagueId': 13},
    41236: {'nation': 46, 'nationality': 'Sweden', 'teamId': 11, 'clubName': 'Manchester United', 'leagueId': 13,
            'preferredPosition': 'ST', 'position': 'ST',
            'face': [65, 88, 80, 76, 37, 78], 'attributeArray': [65, 88, 80, 76, 37, 78],
            'attributeList': [{'index': 0, 'value': 65}, {'index': 1, 'value': 88}, {'index': 2, 'value': 80}, {'index': 3, 'value': 76}, {'index': 4, 'value': 37}, {'index': 5, 'value': 78}]},
    20801: {'nation': 38, 'nationality': 'Portugal', 'teamId': 243, 'clubName': 'Real Madrid CF', 'leagueId': 53,
            'preferredPosition': 'ST', 'position': 'ST'},
}

def _enrich_and_repair_player_identity(card, base=None):
    """Authoritative enrichment & validation pipeline for FIFA 18 player cards.

    Guarantees that authentic nationality (nation/nationId/nationality) and authentic
    club/league (teamId/teamid/clubName/leagueId) are strictly reconciled against
    the authoritative base player definitions, preventing scraped or legacy defaults
    (such as nation=1 Albania or team=1 Arsenal) from corrupting player cards.
    """
    if not isinstance(card, dict):
        return card
    x = dict(card)
    aid = int(x.get('assetId', 0) or 0)
    if aid <= 0:
        return x

    if base is None:
        base = _definition_by_asset(aid)

    override = AUTH_PLAYER_OVERRIDES.get(aid)
    base_dict = dict(base) if isinstance(base, dict) else {}
    if override:
        for k, v in override.items():
            if k not in base_dict or not base_dict[k] or (k == 'nation' and int(base_dict.get('nation', 0) or 0) <= 0):
                base_dict[k] = v

    # Fix Elseid Hysaj misnamed as Kolasinac in legacy entries:
    if aid == 210864 and 'Kola' in str(x.get('name', '')):
        x['name'] = 'Elseid Hysaj'
        x['displayName'] = 'Elseid Hysaj'
        x['nationality'] = 'Albania'
        x['nation'] = 1
        x['nationId'] = 1
        x['teamId'] = 48
        x['teamid'] = 48
        x['clubName'] = 'Napoli'

    # 1. Nationality reconciliation
    base_nation = int(base_dict.get('nation', base_dict.get('nationId', 0)) or 0)
    base_nat_name = str(base_dict.get('nationality', '') or '')
    if base_nation <= 0 and base_nat_name:
        base_nation = int(NATION_IDS.get(base_nat_name, 0))

    card_nation = int(x.get('nation', x.get('nationId', 0)) or 0)
    # A player's nationality in FUT is permanent. If card nation is missing, 0,
    # or dummy 1 (Albania) while base is not Albanian, or mismatches base:
    if base_nation > 0 and (card_nation <= 0 or (card_nation == 1 and base_nation != 1) or card_nation != base_nation):
        x['nation'] = base_nation
        x['nationId'] = base_nation
    else:
        resolved_nat = card_nation or base_nation
        x['nation'] = resolved_nat
        x['nationId'] = resolved_nat

    if not x.get('nationality') and base_nat_name:
        x['nationality'] = base_nat_name

    # 2. Club & League reconciliation
    base_team = int(base_dict.get('teamId', base_dict.get('teamid', 0)) or 0)
    base_club = str(base_dict.get('clubName', '') or '')
    base_league = int(base_dict.get('leagueId', 0) or 0)

    card_team = int(x.get('teamId', x.get('teamid', 0)) or 0)
    card_club = str(x.get('clubName', '') or '')

    # If card has dummy team 1 (Arsenal) while base is not Arsenal, or has empty clubName:
    if base_team > 0 and ((card_team in (0, 1) and base_team != 1) or not card_club):
        x['teamId'] = base_team
        x['teamid'] = base_team
        x['clubName'] = base_club
        if base_league > 0 and (x.get('leagueId') in (0, 13) and base_league != 13):
            x['leagueId'] = base_league
    else:
        resolved_team = card_team or base_team
        x['teamId'] = resolved_team
        x['teamid'] = resolved_team
        if not x.get('clubName') and base_club:
            x['clubName'] = base_club

    return x

def _definition_item(d, *, pile=6, rare=None, item_id=None):
    d=dict(d or {})
    d=_enrich_and_repair_player_identity(d)
    face_stats=_extract_face_attributes(d, int(d.get('rating',50) or 50))
    pos=str(d.get('position',d.get('preferredPosition','CM')) or 'CM')
    row=(int(d.get('assetId',0)),int(d.get('rating',50)),pos,
         int(d.get('leagueId',0) or 0),int(d.get('teamId',0) or 0),int(d.get('nation',0) or 0),
         face_stats)
    while len(row[-1])<6:row[-1].append(50)
    item=_starter_item(0,row,pile=pile,state='free',item_id=item_id or _next_item_id())
    # assetId is the footballer identity; definition/resourceId identify the
    # actual card variation. FIFA 18 special cards therefore keep assetId but
    # use a distinct resourceId and rareflag.
    definition=int(d.get('resourceId',d.get('definitionId',d.get('assetId',0))) or d.get('assetId',0))
    item['definitionId']=definition;item['resourceId']=definition
    nat_val = int(d.get('nationId', d.get('nation', item.get('nation', 0))) or 0)
    item['nation'] = nat_val
    item['nationId'] = nat_val
    team_val = int(d.get('teamid', d.get('teamId', item.get('teamid', 0))) or 0)
    item['teamid'] = team_val
    item['teamId'] = team_val
    item['untradeable']=False;item['tradeable']=True
    item['discardValue']=max(0,int(item['rating']*10 if item['rating']>=75 else item['rating']*4))
    if rare is not None:
        item['rareflag']=1 if rare else 0;item['rareFlag']=item['rareflag']
    elif 'rareflag' in d or 'rareFlag' in d:
        rf=int(d.get('rareflag',d.get('rareFlag',0)) or 0);item['rareflag']=rf;item['rareFlag']=rf
    if d.get('name'):item['name']=d['name'];item['displayName']=d['name']
    if d.get('clubName'):item['clubName']=d['clubName']
    if d.get('nationality'):item['nationality']=d['nationality']
    if d.get('specialType'):
        item['specialType']=str(d['specialType']);item['cardType']=str(d['specialType']);item['isSpecial']=True
        # assetId remains the base footballer identity. The native client uses
        # it as the installed/base-head fallback when a versioned dynamic DDS is
        # unavailable, while resourceId still preserves the special card identity.
        item['baseResourceId']=int(d.get('assetId',0) or 0)
        # Exact EA variations resolve their own dynamic texture. Historical rows
        # without an archived EA resource id keep the base footballer head as a
        # safe portrait fallback while retaining a distinct local definitionId.
        item['dynamicHeadResourceId']=int(d.get('assetId',0) or 0) if d.get('localCatalog') else definition
        if d.get('localCatalog'):item['localCatalog']=True
    if d.get('version') is not None:item['version']=int(d.get('version') or 0)
    if d.get('verifiedFifa18'):item['verifiedFifa18']=True
    return item

# FIFA 18 static-content archive used by the original client. Dynamic player
# portraits were requested as PC DDS textures, while the preserved web archive
# contains equivalent PNG action shots. Convert those PNGs locally when needed.
FIFA18_CONTENT_GUID='B1BA185F-AD7C-4128-8A64-746DE4EC5A82'
FIFA18_PACKOPENING_CACHE=PLAYER_DATA_DIR/'packopening'
FIFA18_PACKOPENING_CACHE.mkdir(parents=True,exist_ok=True)
_PACKOPENING_FETCH_ATTEMPTED=set()
_PACKOPENING_FETCH_LOCK=threading.RLock()

def _valid_packopening_json(name,data):
    if not isinstance(data,(bytes,bytearray)) or len(data)<128:return False
    try:doc=json.loads(bytes(data).decode('utf-8-sig'))
    except Exception:return False
    if not isinstance(doc,(dict,list)):return False
    low=bytes(data).lower()
    if name=='packopeningconfig.json':
        # Exact-year files differ in shape.  Reject tiny catch-all stubs while
        # allowing preserved retail configs with either the trigger vocabulary
        # or substantial rarity/range tables.
        return (b'istriggerpoa' in low or b'packopening' in low or b'rangevalue' in low) and len(data)>512
    if name=='packopeningsetting.json':
        return (b'walkoutlist' in low or b'revealitemtiming' in low or b'tunnel' in low or b'packopening' in low) and len(data)>1024
    return False

def _packopening_exact_bytes(name):
    """Return only already-cached/packaged exact FIFA18 pack-opening bytes."""
    name=str(name).lower()
    if name not in ('packopeningconfig.json','packopeningsetting.json'):return None
    packaged=ROOT/'data'/'packopening'/name
    cached=FIFA18_PACKOPENING_CACHE/name
    for p in (packaged,cached):
        try:
            data=p.read_bytes()
            if _valid_packopening_json(name,data):
                log.warning('PACK OPENING EXACT FILE ready name=%s source=%s bytes=%d',name,p,len(data));return data
        except Exception:pass
    return None

def _packopening_activation_pair():
    """Return (config, setting) only when BOTH exact FIFA18 files exist.

    v0.8.9.27.5 proved that activating the recovered config by itself is unsafe:
    FIFA can enter FUT, but the first pack-open navigation can terminate before
    /purchased/items is ever sent.  Never half-activate the native ceremony.
    """
    config=_packopening_exact_bytes('packopeningconfig.json')
    setting=_packopening_exact_bytes('packopeningsetting.json')
    if config is None or setting is None:
        return None,None
    return config,setting

def _prefetch_packopening_one(name):
    """Background-only archive fetch so FIFA's boot HTTP request never waits."""
    name=str(name).lower()
    if _packopening_exact_bytes(name) is not None:return
    online=str(os.environ.get('FUT18_PACKOPENING_ONLINE','1')).strip().lower() not in ('0','false','no','off')
    if not online:return
    with _PACKOPENING_FETCH_LOCK:
        if name in _PACKOPENING_FETCH_ATTEMPTED:return
        _PACKOPENING_FETCH_ATTEMPTED.add(name)
    cached=FIFA18_PACKOPENING_CACHE/name
    roots=[
        f'https://fifa17.content.easports.com/fifa/fltOnlineAssets/{FIFA18_CONTENT_GUID}/2018',
        f'https://fifa18.content.easports.com/fifa/fltOnlineAssets/{FIFA18_CONTENT_GUID}/2018',
        f'https://raw.githubusercontent.com/cdn-augames/FIFARosters_AUGMirror/91904ca96aa0b171d8fe4cd0ff4b723e917967fe/fifarosters-mirror/fifa17.content.easports.com/fifa/fltOnlineAssets/{FIFA18_CONTENT_GUID}/2018',
    ]
    # FIFA18 exposes data/store/packopeningsetting.json as a local VFS path,
    # while the recovered trigger config lived under fut/packs/packopening.
    # Probe both families in the background, but never serve a half-pair.
    rels=[f'fut/packs/packopening/{name}']
    if name=='packopeningsetting.json':
        rels += [
            'data/store/packopeningsetting.json',
            'fut/dynamicmessages/fut/packs/packopening/packopeningsetting.json',
            'fut/packs/packopening/packopeningsettings.json',
        ]
    for base in roots:
        for rel in rels:
            url=f'{base}/{rel}'
            try:
                req=urllib.request.Request(url,headers={'User-Agent':'FIFA18LocalFUT/0.8.9.27.8','Accept':'application/json,*/*'})
                with _remote_urlopen(req,timeout=2.2) as r:data=r.read(1_000_000)
                if _valid_packopening_json(name,data):
                    try:cached.write_bytes(data)
                    except Exception:pass
                    log.warning('PACK OPENING EXACT CDN HIT name=%s host=%s path=%s bytes=%d sha256=%s',name,urllib.parse.urlsplit(url).netloc,rel,len(data),hashlib.sha256(data).hexdigest())
                    return
                log.warning('PACK OPENING CDN candidate rejected name=%s host=%s path=%s bytes=%d',name,urllib.parse.urlsplit(url).netloc,rel,len(data))
            except Exception as e:
                log.info('PACK OPENING CDN miss name=%s host=%s path=%s error=%s',name,urllib.parse.urlsplit(url).netloc,rel,type(e).__name__)
    log.warning('PACK OPENING EXACT CDN unavailable name=%s; no synthetic ceremony/timing JSON will be used',name)

def _prefetch_packopening_files():
    # Started before listeners become busy. It is deliberately best-effort and
    # never blocks a FUT request; a successful fetch is cached for this or the
    # next full game boot.
    for name in ('packopeningconfig.json','packopeningsetting.json'):
        try:_prefetch_packopening_one(name)
        except Exception as e:log.info('PACK OPENING prefetch error name=%s error=%s',name,type(e).__name__)

_PLAYER_HEAD_MISSES=set()
_PLAYER_HEAD_FETCH_LOCK=threading.RLock()

def _png_to_dds(png):
    """Convert a simple 8-bit non-interlaced PNG to uncompressed BGRA DDS."""
    if not isinstance(png,(bytes,bytearray)) or not bytes(png).startswith(b'\x89PNG\r\n\x1a\n'):return None
    pos=8;w=h=0;bit=ctype=interlace=None;palette=None;trns=None;idat=[]
    try:
        while pos+12<=len(png):
            n=struct.unpack('>I',png[pos:pos+4])[0];kind=bytes(png[pos+4:pos+8]);data=bytes(png[pos+8:pos+8+n]);pos+=12+n
            if kind==b'IHDR':w,h,bit,ctype,comp,filt,interlace=struct.unpack('>IIBBBBB',data)
            elif kind==b'PLTE':palette=[tuple(data[i:i+3]) for i in range(0,len(data),3)]
            elif kind==b'tRNS':trns=data
            elif kind==b'IDAT':idat.append(data)
            elif kind==b'IEND':break
        if not w or not h or bit!=8 or interlace!=0 or ctype not in (0,2,3,4,6):return None
        bpp={0:1,2:3,3:1,4:2,6:4}[ctype];raw=zlib.decompress(b''.join(idat));stride=w*bpp
        rows=[];off=0;prev=bytearray(stride)
        def paeth(a,b,c):
            p=a+b-c;pa=abs(p-a);pb=abs(p-b);pc=abs(p-c)
            return a if pa<=pb and pa<=pc else b if pb<=pc else c
        for _ in range(h):
            ft=raw[off];off+=1;scan=bytearray(raw[off:off+stride]);off+=stride
            for i in range(stride):
                a=scan[i-bpp] if i>=bpp else 0;b=prev[i];c=prev[i-bpp] if i>=bpp else 0
                if ft==1:scan[i]=(scan[i]+a)&255
                elif ft==2:scan[i]=(scan[i]+b)&255
                elif ft==3:scan[i]=(scan[i]+((a+b)//2))&255
                elif ft==4:scan[i]=(scan[i]+paeth(a,b,c))&255
                elif ft!=0:return None
            rows.append(bytes(scan));prev=scan
        pixels=bytearray()
        for row in rows:
            for x in range(w):
                i=x*bpp
                if ctype==6:r,g,b,a=row[i:i+4]
                elif ctype==2:r,g,b=row[i:i+3];a=255
                elif ctype==0:r=g=b=row[i];a=255
                elif ctype==4:r=g=b=row[i];a=row[i+1]
                else:
                    idx=row[i]
                    if not palette or idx>=len(palette):r=g=b=0
                    else:r,g,b=palette[idx]
                    a=trns[idx] if trns is not None and idx<len(trns) else 255
                pixels.extend((b,g,r,a))
        header=[124,0x100F,h,w,w*4,0,0]+[0]*11+[32,0x41,0,32,0x00FF0000,0x0000FF00,0x000000FF,0xFF000000]+[0x1000,0,0,0,0]
        return b'DDS '+struct.pack('<31I',*header)+bytes(pixels)
    except Exception:return None

def _fetch_player_head_dds(resource_id,asset_id=0):
    """Fetch/cache an authentic dynamic portrait, then a base portrait fallback.

    No internet is required for normal play: an unavailable archive simply returns
    None and the HTTP route sends 404 so the native client can use installed art.
    """
    try:rid=int(resource_id or 0);aid=int(asset_id or 0)
    except Exception:return None
    if not rid:return None
    cache=PLAYER_HEAD_CACHE/f'p{rid}.dds'
    if cache.exists() and cache.stat().st_size>=128:
        try:
            data=cache.read_bytes()
            if data[:4]==b'DDS ':return data
        except Exception:pass
    online=str(os.environ.get('FUT18_DYNAMIC_HEADS_ONLINE','1')).strip().lower() not in ('0','false','no','off')
    if not online or rid in _PLAYER_HEAD_MISSES:return None
    root=f'https://fifa18.content.easports.com/fifa/fltOnlineAssets/{FIFA18_CONTENT_GUID}/2018'
    mirror=f'https://raw.githubusercontent.com/cdn-augames/FIFARosters_AUGMirror/91904ca96aa0b171d8fe4cd0ff4b723e917967fe/fifarosters-mirror/fifa17.content.easports.com/fifa/fltOnlineAssets/{FIFA18_CONTENT_GUID}/2018'
    urls=[('dds',f'{root}/fut/playerheads/g4/single/p{rid}.dds'),
          ('png',f'{root}/fut/playerheads/html5/single/134x134/p{rid}.png'),
          ('png',f'{mirror}/fut/playerheads/html5/single/134x134/p{rid}.png')]
    if aid:
        urls += [('png',f'{root}/fut/items/images/mobile/portraits/{aid}.png'),
                 ('png',f'{mirror}/fut/items/images/mobile/portraits/{aid}.png')]
    for kind,url in urls:
        try:
            req=urllib.request.Request(url,headers={'User-Agent':'FIFA18LocalFUT/0.8.9.36.22','Accept':'*/*'})
            with _remote_urlopen(req,timeout=1.4) as r:data=r.read(2_500_000)
            dds=data if kind=='dds' and data[:4]==b'DDS ' else _png_to_dds(data) if kind=='png' else None
            if dds and dds[:4]==b'DDS ':
                try:cache.write_bytes(dds)
                except Exception:pass
                log.warning('PLAYER HEAD archive hit resource=%d asset=%d source=%s bytes=%d',rid,aid,urllib.parse.urlsplit(url).netloc,len(dds))
                return dds
        except Exception:continue
    with _PLAYER_HEAD_FETCH_LOCK:_PLAYER_HEAD_MISSES.add(rid)
    return None

_HEAD_PLACEHOLDER_CACHE={}

def _player_head_placeholder_png(resource_id,name=''):
    """A deterministic colour-circle-with-initials PNG, always available.

    Used by the local draft console (browsers cannot render .dds), never by
    the native FIFA 18 routes, which keep their existing 404 fallback.
    """
    key=(int(resource_id or 0),str(name or ''))
    cached=_HEAD_PLACEHOLDER_CACHE.get(key)
    if cached is not None:return cached
    from PIL import Image,ImageDraw
    palette=[(198,40,40),(21,101,192),(46,125,50),(230,126,0),(106,27,154),(0,121,107),(191,54,12),(63,81,181)]
    colour=palette[int(resource_id or 0)%len(palette)]
    initials=''.join(p[0] for p in str(name or '?').split()[:2]).upper() or '?'
    img=Image.new('RGBA',(128,128),(0,0,0,0))
    d=ImageDraw.Draw(img)
    d.ellipse((2,2,125,125),fill=colour+(255,))
    bbox=d.textbbox((0,0),initials)
    tw,th=bbox[2]-bbox[0],bbox[3]-bbox[1]
    d.text((64-tw/2-bbox[0],64-th/2-bbox[1]),initials,fill=(255,255,255,255))
    import io as _io
    buf=_io.BytesIO();img.save(buf,'PNG')
    payload=buf.getvalue()
    _HEAD_PLACEHOLDER_CACHE[key]=payload
    return payload

def _player_head_png(resource_id,asset_id=0,name=''):
    """PNG bytes for the local draft console. Always returns something.

    Reuses the same DDS sources the native FIFA route already trusts
    (packaged cache, then the online archive fetch), then decodes with
    Pillow -- browsers cannot display .dds directly. Falls back to a
    placeholder on any miss or decode failure rather than a broken image.
    """
    try:rid=int(resource_id or 0)
    except Exception:rid=0
    dds=None
    if rid:
        packaged=ROOT/'data'/'playerheads'/f'p{rid}.dds'
        if packaged.exists() and packaged.is_file() and packaged.stat().st_size>=128:
            try:
                candidate=packaged.read_bytes()
                if candidate[:4]==b'DDS ':dds=candidate
            except Exception:dds=None
        if dds is None:
            dds=_fetch_player_head_dds(rid,asset_id)
    if dds:
        try:
            from PIL import Image
            import io as _io
            im=Image.open(_io.BytesIO(dds)).convert('RGBA')
            buf=_io.BytesIO();im.save(buf,'PNG')
            return buf.getvalue()
        except Exception:
            log.warning('DRAFT CONSOLE head decode failed resource=%d, using placeholder',rid)
    return _player_head_placeholder_png(rid,name)

_SPECIAL_PLAYER_DEFS=None
_ICON_PLAYER_DEFS=None
FIFA18_ICON_FILE=ROOT/'data'/'fifa18-icons.json'
# v0.8.9.3 deliberately stops synthesising FUT cards. Every special below has
# an exact FIFA 18 FUT resource/futid observed in an archived FIFA 18 database.
# The runtime can refresh the same verified catalogue from preserved category
# pages, but it never creates a card by "base asset + guessed version" again.
VERIFIED_SPECIAL_FILE=ROOT/'data'/'fifa18-verified-specials.json'
VERIFIED_SPECIAL_CACHE=RUNTIME/'fifa18-verified-specials.json'
FIFAROSTERS_MIRROR_COMMIT='91904ca96aa0b171d8fe4cd0ff4b723e917967fe'
FIFAROSTERS_RAW_ROOT=f'https://raw.githubusercontent.com/cdn-augames/FIFARosters_AUGMirror/{FIFAROSTERS_MIRROR_COMMIT}/fifarosters-mirror/www.fifarosters.com'
FIFAROSTERS_LIVE_ROOT='https://www.fifarosters.com'
# Category slugs use public FIFA 18 list pages whose item blocks expose exact
# asset id + futid/resource id. Crawling is background-only and cached locally.
VERIFIED_SPECIAL_LIVE_PAGES=(
    ('totw','TOTW',3,'if'),('toty','TOTY',5,'toty'),('tots','TOTS',11,'tots'),
    ('ptg-selected','PTGS',34,'ptgs'),
    ('fut-birthday','FUT_BIRTHDAY',30,'fut_birthday'),('futmas','FUTMAS',32,''),
    ('tott','TOTT',39,''),
)
# Only categories whose FIFA 18 rareflag is unambiguous in the recovered client.
VERIFIED_SPECIAL_PAGES=(
    ('totw8fa7.html','TOTW',3,'if'),
    ('toty8fa7.html','TOTY',5,'toty'),
    ('tots8fa7.html','TOTS',11,'tots'),
    # Additional preserved FIFA 18 category pages. The parser only accepts
    # links explicitly carrying v=18 and an exact futid, so a missing/changed
    # archive page can never fabricate a card.
    ('futmas8fa7.html','FUTMAS',32,''),
    ('tott8fa7.html','TOTT',39,''),
    ('ptg-selected8fa7.html','PTGS',34,'ptgs'),
    ('fut-birthday8fa7.html','FUT_BIRTHDAY',30,'fut_birthday'),
)
# A small offline seed of exact archived FIFA 18 cards. Online catalogue refresh
# adds the complete visible archived category pages when available.
# Resource-ID keyed corrections for historical cards where a category-list page
# is broader than the card's actual FIFA 18 item type. These are explicit
# identities, not rating/version guesses. In the v0.8.9.32 trace, Dybala's two
# 97 cards were both being tagged FOF even though they are separate legitimate
# cards: 184760486 is TOTS and 201537702 is FUT Champions.
FIFA18_EXACT_CARD_OVERRIDES={
    184760486:('TOTS',11),
    201537702:('CHAMPION',18),
    # Cristiano Ronaldo TOTW corrections (authentically 96 ST and 95 ST, previously corrupted to 86/87 CB)
    33575233:('TOTW',3,96,'ST',[91,95,84,92,34,82]),
    16798017:('TOTW',3,95,'ST',[91,94,83,91,34,81]),
    # Christian Clemens position corrections (RM, not GK)
    218296906:('TOTW',3,87,'RM',[95,83,86,87,55,81]),
    201519690:('TOTW',3,86,'RM',[94,82,85,86,54,80]),
    # Zlatan Ibrahimovic specials exact classification & authentic face stats
    83927316:('TOTW',3,90,'ST',[70,90,86,86,34,85]),
    117481748:('MOTM',20,89,'ST',[66,89,83,85,33,84]),
    50372884:('EMOTM',38,89,'ST',[66,89,85,84,33,83]),
    67150100:('OTW',21,88,'ST',[65,88,81,82,32,82]),
    134258964:('FOF',35,96,'ST',[84,96,90,94,38,94]),
    16818452:('FUTMAS',32,89,'ST',[68,89,83,83,33,89]),
}


# Native FIFA 18 card-design mapping. Resource ID is still the authority; this
# table exists so migrations never infer a design from an event page or rating.
FIFA18_RAREFLAG_BY_TYPE={
    'BASE_COMMON':0,'BASE_RARE':1,'TOTW':3,'IF':3,'TOTY':5,'RECORD_BREAKER':6,'RB':6,
    'TOTS':11,'ICON':12,'FUTTIES':16,'FUT_CHAMPIONS':18,'CHAMPION':18,
    'IMOTM':20,'OTW':21,'ULTIMATE_SCREAM':22,'HALLOWEEN':22,'SBC':24,
    'AWARD':28,'AWARD_WINNER':28,'POTM':28,'FUT_BIRTHDAY':30,'FUTMAS':32,
    'PTG_SELECTED':34,'PTGS':34,'FOF':35,'EUROPEAN_MOTM':38,'EMOTM':38,'TOTT':39,
}

def _apply_exact_card_override(d):
    if not isinstance(d,dict):return d
    try:rid=int(d.get('resourceId',d.get('definitionId',0)) or 0)
    except Exception:return d
    override=FIFA18_EXACT_CARD_OVERRIDES.get(rid)
    if not override:return d
    label=override[0];rareflag=override[1];x=dict(d)
    x['specialType']=str(label);x['rareflag']=int(rareflag);x['rareFlag']=int(rareflag)
    if len(override)>=4:
        r=int(override[2]);p=str(override[3]).upper()
        x['rating']=r;x['position']=p;x['preferredPosition']=p
        x['cardsubtypeid']=int(PLAYER_CARD_SUBTYPE.get(p,3))
    if len(override)>=5:
        f=list(override[4])
        x['face']=f;x['attributeArray']=list(f);x['attributeList']=_attrs(f)
    x['isSpecial']=True;x['verifiedFifa18']=True;x['exactCardIdentityOverride']=True
    return x

_VERIFIED_SPECIAL_SEED=(
    # TOTW / IF
    (158023,201484615,97,'TOTW',3),(20801,83906881,96,'TOTW',3),
    (176580,117617092,95,'TOTW',3),(190871,117631383,95,'TOTW',3),
    (158023,84044103,95,'TOTW',3),
    # Sergio Aguero exact FIFA 18 FUT resource IDs. These fill the sequence that
    # v0.8.9.31 collapsed to only the final 94 IF; 94 Award and 97 TOTS are not IFs.
    (153079,50484727,90,'TOTW',3),(153079,67261943,91,'TOTW',3),
    (153079,117593591,92,'TOTW',3),(153079,134370807,93,'TOTW',3),
    (153079,151148023,94,'AWARD',28),(153079,218256887,94,'TOTW',3),
    (153079,201479671,97,'TOTS',11),
    (183277,100846573,94,'TOTW',3),(176580,84062660,94,'TOTW',3),
    (190871,84076951,94,'TOTW',3),(158023,50489671,94,'TOTW',3),
    # TOTY
    (20801,67129665,99,'TOTY',5),
    # TOTS
    (20801,151015745,99,'TOTS',11),(209658,117650170,93,'TOTS',11),
    (200726,117641238,93,'TOTS',11),(200318,117640830,93,'TOTS',11),
    (190941,117631453,93,'TOTS',11),(180206,117620718,93,'TOTS',11),
    (204485,151199429,91,'TOTS',11),(180334,134398062,90,'TOTS',11),
    (231478,84117558,87,'TOTS',11),(200155,100863451,87,'TOTS',11),
)

def _special_from_verified_row(asset,rid,rating,label,rareflag):
    base=_definition_by_asset(asset)
    if not base:return None
    x=dict(base);base_rating=int(x.get('rating',0) or 0);rating=int(rating or base_rating)
    x.update({'assetId':int(asset),'definitionId':int(rid),'resourceId':int(rid),
              'rareflag':int(rareflag),'rareFlag':int(rareflag),'specialType':str(label),
              'rating':rating,'isSpecial':True,'verifiedFifa18':True,
              'source':'FifaRosters FIFA18 archive'})
    # Resource IDs encode the actual FUT variation; preserve the base player face
    # stats but scale them only enough to track the archived overall.
    delta=max(0,rating-base_rating)
    base_face=_extract_face_attributes(base, base_rating)
    x['face']=[min(99,int(v)+delta) for v in base_face[:6]]
    x['attributeArray']=list(x['face'])
    x['attributeList']=_attrs(x['face'])
    x=_enrich_and_repair_player_identity(x, base)
    return x

def _parse_verified_special_html(text,label,rareflag,required_class):
    if not isinstance(text,str):return []
    # Each card block exposes archived overall + exact asset/futid + CSS type.
    pat=re.compile(r'data-original-overall=["\'](\d+)["\'](?P<body>.{0,1400}?)href=["\'][^"\']*player=(\d+)&(?:amp;)?futid=(\d+)&(?:amp;)?v=18[^"\']*["\'](?P<tail>.{0,300}?)class=["\']([^"\']+)["\']',re.I|re.S)
    out=[];seen=set()
    for m in pat.finditer(text):
        rating=int(m.group(1));asset=int(m.group(3));rid=int(m.group(4));classes=str(m.group(6)).lower()
        if required_class and required_class.lower() not in classes:continue
        if rid in seen:continue
        d=_special_from_verified_row(asset,rid,rating,label,rareflag)
        if d:out.append(d);seen.add(rid)
    return out

def _verified_special_catalog(refresh=False):
    global _SPECIAL_PLAYER_DEFS
    if _SPECIAL_PLAYER_DEFS is not None and not refresh:return _SPECIAL_PLAYER_DEFS
    rows=[];seen=set()
    def add(ds):
        for d in ds:
            if not isinstance(d,dict):continue
            try:rid=int(d.get('resourceId',d.get('definitionId',0)) or 0)
            except Exception:continue
            if rid<=0 or rid in seen:continue
            # Never trust a cached variation whose base asset no longer exists unless complete card data is provided.
            if not d.get('name') and not _definition_by_asset(int(d.get('assetId',0) or 0)):continue
            d=_apply_exact_card_override(dict(d))
            d=_enrich_and_repair_player_identity(d)
            d['verifiedFifa18']=True;rows.append(d);seen.add(rid)
    # Packaged verified snapshot, then last runtime cache. The old runtime
    # Festival crawler used a broad re-release page, so rareFlag 35 rows from
    # that cache are not authoritative. Only exact FOF resource IDs already in
    # the packaged verified snapshot may survive cache loading.
    trusted_fof=set()
    try:
        if VERIFIED_SPECIAL_FILE.exists():
            _doc=json.loads(VERIFIED_SPECIAL_FILE.read_text(encoding='utf-8'))
            _vals=_doc.get('players',_doc) if isinstance(_doc,dict) else _doc
            if isinstance(_vals,list):
                for _x in _vals:
                    try:
                        if int(_x.get('rareflag',_x.get('rareFlag',0)) or 0)==35:
                            trusted_fof.add(int(_x.get('resourceId',_x.get('definitionId',0)) or 0))
                    except Exception:pass
    except Exception:pass
    for fp in (VERIFIED_SPECIAL_FILE,VERIFIED_SPECIAL_CACHE):
        try:
            if fp.exists():
                doc=json.loads(fp.read_text(encoding='utf-8'))
                vals=doc.get('players',doc) if isinstance(doc,dict) else doc
                if isinstance(vals,list):
                    if fp==VERIFIED_SPECIAL_CACHE:
                        vals=[x for x in vals if not isinstance(x,dict) or int(x.get('rareflag',x.get('rareFlag',0)) or 0)!=35 or int(x.get('resourceId',x.get('definitionId',0)) or 0) in trusted_fof]
                    add(vals)
        except Exception as e:log.warning('VERIFIED SPECIAL cache read failed %s: %s',fp,e)
    # Exact offline seed is always safe.
    add([d for d in (_special_from_verified_row(*row) for row in _VERIFIED_SPECIAL_SEED) if d])
    online=str(os.environ.get('FUT18_VERIFIED_SPECIALS_ONLINE','0')).strip().lower() not in ('0','false','no','off')
    if online and (refresh or len(rows)<50):
        fetched=[]
        for filename,label,rf,css in VERIFIED_SPECIAL_PAGES:
            try:
                req=urllib.request.Request(f'{FIFAROSTERS_RAW_ROOT}/{filename}',headers={'User-Agent':'FIFA18LocalFUT/0.8.9.36.22'})
                with _remote_urlopen(req,timeout=2.0) as r:text=r.read(5_000_000).decode('utf-8','replace')
                got=_parse_verified_special_html(text,label,rf,css);fetched.extend(got)
                log.warning('VERIFIED SPECIAL archive page=%s type=%s cards=%d',filename,label,len(got))
            except Exception as e:log.warning('VERIFIED SPECIAL archive unavailable page=%s: %s',filename,e)
        add(fetched)
        if fetched:
            try:
                VERIFIED_SPECIAL_CACHE.parent.mkdir(parents=True,exist_ok=True)
                VERIFIED_SPECIAL_CACHE.write_text(json.dumps({'source':'FifaRosters archived FIFA18 exact futids','players':rows},separators=(',',':')),encoding='utf-8')
            except Exception:pass
    _SPECIAL_PLAYER_DEFS=rows
    log.warning('VERIFIED SPECIAL DB ready exactCards=%d types=%s',len(rows),sorted({x.get('specialType') for x in rows}))
    return rows


def _merge_verified_special_rows(current,fetched):
    """Merge newly discovered exact IDs without reclassifying known cards."""
    by_rid={}
    for x in current or []:
        if not isinstance(x,dict):continue
        try:rid=int(x.get('resourceId',x.get('definitionId',0)) or 0)
        except Exception:continue
        if rid>0:by_rid[rid]=_apply_exact_card_override(dict(x))
    for d in fetched or []:
        if not isinstance(d,dict):continue
        try:rid=int(d.get('resourceId',d.get('definitionId',0)) or 0)
        except Exception:continue
        if rid>0 and rid not in by_rid:by_rid[rid]=_apply_exact_card_override(dict(d))
    return list(by_rid.values())

def _refresh_verified_specials_live_background():
    """Expand exact FIFA18 specials from the still-queryable FifaRosters lists.

    This never infers a promo from rating. Every accepted row must carry an
    explicit FIFA18 `v=18` URL plus its exact archived FUT/resource ID. Results
    are cached and inserted into My Club while the server is running.
    """
    global _SPECIAL_PLAYER_DEFS
    fetched=[];seen=set();page_requests=0
    for slug,label,rareflag,css in VERIFIED_SPECIAL_LIVE_PAGES:
        empty_streak=0
        # TOTW is ~1k cards; most other categories are far smaller. The list UI
        # uses 25 cards per page. Stop after two empty/no-new pages.
        for page in range(1,45):
            url=f'{FIFAROSTERS_LIVE_ROOT}/{slug}?v=18' + ('' if page==1 else f'&pageNum={page}')
            try:
                req=urllib.request.Request(url,headers={'User-Agent':'Mozilla/5.0 FIFA18LocalFUT/0.8.9.36.22','Accept':'text/html,*/*'})
                with _remote_urlopen(req,timeout=3.0) as r:
                    text=r.read(4_000_000).decode('utf-8','replace')
                page_requests+=1
                got=_parse_verified_special_html(text,label,rareflag,css)
                new=[]
                for d in got:
                    rid=int(d.get('resourceId',0) or 0)
                    if rid>0 and rid not in seen:new.append(d);seen.add(rid)
                if new:
                    fetched.extend(new);empty_streak=0
                else:
                    empty_streak+=1
                # Pages expose a total count; do not issue needless requests once
                # the current page reaches that total.
                m=re.search(r'Showing\s+\d+\s*-\s*(\d+)\s+of\s+([\d,]+)\s+players',text,re.I)
                if m and int(m.group(1))>=int(m.group(2).replace(',','')):break
                if empty_streak>=2:break
            except Exception as e:
                if page==1:log.warning('VERIFIED SPECIAL live category unavailable type=%s url=%s: %s',label,url,e)
                break
        log.warning('VERIFIED SPECIAL live crawl type=%s cardsSoFar=%d requests=%d',label,len(fetched),page_requests)
    if not fetched:return 0
    # Merge the exact live rows with the packaged/runtime snapshot and persist.
    current=_verified_special_catalog(False)
    # Discovery may add new exact resource IDs, but it must never reclassify an
    # identity already present in the packaged/runtime authoritative catalogue.
    # v0.8.9.32 overwrote known TOTS/TOTY cards when they also appeared on a
    # Festival-of-FUTball event list (e.g. Aguero 201479671 TOTS -> FOF).
    rows=_merge_verified_special_rows(current,fetched)
    try:
        VERIFIED_SPECIAL_CACHE.parent.mkdir(parents=True,exist_ok=True)
        VERIFIED_SPECIAL_CACHE.write_text(json.dumps({'schema':4,'source':'FifaRosters FIFA18 exact live/archive futids','players':rows},separators=(',',':')),encoding='utf-8')
    except Exception as e:log.warning('VERIFIED SPECIAL live cache write failed: %s',e)
    _SPECIAL_PLAYER_DEFS=rows
    _migrate_verified_specials()
    post=_festival_identity_migration(force=True,phase='post-live')
    _cleanup_exact_player_resource_duplicates()
    added=_ensure_special_players_in_club(rows)
    log.warning('VERIFIED SPECIAL live crawl complete exactCards=%d newlySeeded=%d requests=%d postIdentity=%s',len(rows),added,page_requests,post)
    return len(rows)

def _icon_player_defs():
    """Return the curated regular-FUT FIFA 18 Icon Stories catalogue.

    FIFA 18 shipped 40 regular FUT Icons with three Stories each.  Keep these
    separate from later-year LegendsPlayers snapshots and from the four
    World-Cup-only additions so the local catalogue stays year-correct.
    """
    global _ICON_PLAYER_DEFS
    if _ICON_PLAYER_DEFS is not None:return _ICON_PLAYER_DEFS
    rows=[];seen=set()
    try:
        doc=json.loads(FIFA18_ICON_FILE.read_text(encoding='utf-8'))
        vals=doc.get('players',[]) if isinstance(doc,dict) else []
        for raw in vals:
            if not isinstance(raw,dict):continue
            d=dict(raw)
            try:
                aid=int(d.get('assetId',0) or 0);rid=int(d.get('resourceId',d.get('definitionId',aid)) or 0)
                rating=int(d.get('rating',0) or 0);rf=int(d.get('rareflag',d.get('rareFlag',0)) or 0)
                team=int(d.get('teamId',d.get('teamid',0)) or 0);league=int(d.get('leagueId',0) or 0)
            except Exception:continue
            if aid<=0 or rid<=0 or rating<=0 or rid in seen:continue
            # Exact FIFA 18 Icon club/league/rarity contract. Reject malformed
            # bundled rows rather than silently creating normal-gold lookalikes.
            if rf!=12 or team!=112658 or league!=2118:continue
            d.update({'assetId':aid,'definitionId':rid,'resourceId':rid,
                      'rareflag':12,'rareFlag':12,'teamId':112658,'leagueId':2118,
                      'specialType':'ICON','isSpecial':True,'verifiedFifa18':True,
                      'clubName':'Icons'})
            rows.append(d);seen.add(rid)
    except Exception as e:
        log.warning('FIFA18 ICON catalogue read failed file=%s error=%s',FIFA18_ICON_FILE,e)
    if len(rows)!=120:
        log.warning('FIFA18 ICON catalogue validation expected=120 actual=%d; keeping valid rows only',len(rows))
    _ICON_PLAYER_DEFS=rows
    log.warning('FIFA18 ICON DB ready cards=%d footballers=%d clubId=112658 leagueId=2118 rareflag=12',
                len(rows),len({str(x.get('name','')).strip().lower() for x in rows if x.get('name')}))
    return rows

_ARCHIVE_PLAYER_DEFS=None
_MANAGER_DEFS=None

def _exact_special_player_defs():
    # Exact-ID special snapshot: archived promos plus the exact 120 FIFA 18 Icon Stories.
    return list(_verified_special_catalog(False))+list(_icon_player_defs())

def _archive_player_defs(refresh=False):
    # v0.8.9.31: historical CSV rows without an archived EA resourceId are
    # reference metadata only. They must never enter the live FUT definition pool.
    return []

def _special_player_defs():
    # Resource ID is the authoritative card identity. Promo rarity/type comes
    # from the exact archived row for that Resource ID; it is never inferred
    # from rating or from the version block alone.
    rows=[];seen=set()
    for d in _exact_special_player_defs():
        try:
            aid=int(d.get('assetId',0) or 0);rid=int(d.get('resourceId',d.get('definitionId',0)) or 0)
            rf=int(d.get('rareflag',d.get('rareFlag',0)) or 0)
        except Exception:continue
        if aid<=0 or rid<=0 or rid in seen or not bool(d.get('verifiedFifa18')):continue
        delta=rid-aid;stype=str(d.get('specialType','')).upper()
        # Normal FUT variations follow base + N*0x01000000. FIFA 18 Icons are
        # already separate exact player assets, so their definition/resource ID
        # legitimately equals the icon asset ID rather than a base-player block.
        if stype!='ICON' and (delta<=0 or delta%0x01000000!=0):continue
        x=_apply_exact_card_override(dict(d))
        rf=int(x.get('rareflag',x.get('rareFlag',rf)) or 0);stype=str(x.get('specialType',stype)).upper()
        x['resourceVersion']=0 if stype=='ICON' else delta//0x01000000;x['rareflag']=rf;x['rareFlag']=rf
        x['exactResourceIdentity']=True;rows.append(x);seen.add(rid)
    return rows

def _legacy_synthetic_special_resource_ids():
    """Return only the bogus v0.8.9.1/2 generated resource IDs for migration."""
    old=(('TOTW',3,1,120,2),('TOTY',5,4,24,8),('TOTS',11,9,90,7),('OTW',21,2,70,3),
         ('HALLOWEEN',22,3,45,4),('SBC',24,5,55,4),('FUT_BIRTHDAY',30,6,70,5),
         ('AWARD',28,7,45,5),('FUTMAS',32,8,45,5),('PTG',34,10,70,5),('FOF',35,11,70,7),('TOTT',39,12,55,4))
    ranked=sorted([x for x in _load_player_defs(False) if int(x.get('assetId',0) or 0)>0],
                  key=lambda x:(int(x.get('rating',0) or 0),int(x.get('assetId',0) or 0)),reverse=True)
    return {int(d.get('assetId',0))+ver*0x01000000 for _,_,ver,limit,_ in old for d in ranked[:limit]}

def _migrate_verified_specials():
    marker=_meta_get('verifiedSpecialCleanup','')
    if marker=='0.8.9.33':return 0
    vdefs={int(x.get('resourceId',0) or 0):dict(x) for x in _special_player_defs()}
    verified=set(vdefs);legacy=_legacy_synthetic_special_resource_ids();deleted_items=deleted_auctions=updated_items=0
    with _DB_LOCK,_db_connect() as con:
        for row in con.execute('SELECT id,data FROM items').fetchall():
            try:x=json.loads(row['data']);rid=int(x.get('resourceId',x.get('definitionId',0)) or 0)
            except Exception:continue
            # v0.8.9.1/2 tagged every generated promo with isSpecial/specialType
            # but had no verifiedFifa18 provenance. Delete those unverified promos
            # even if the new official base allow-list changes the old top-player
            # ranking and therefore the exact legacy reconstruction set.
            unverified_generated=bool(x.get('isSpecial') or x.get('specialType')) and not bool(x.get('verifiedFifa18')) and rid not in verified
            if (rid in legacy and rid not in verified) or unverified_generated:
                con.execute('DELETE FROM items WHERE id=?',(int(row['id']),));deleted_items+=1
            elif rid in verified:
                # A handful of the old guessed IDs happen to collide with real FUT
                # resource IDs. Rewrite their rating/rarity/type from the verified
                # archive while preserving the user's item id, pile and item state.
                d=vdefs[rid];keep={k:x.get(k) for k in ('id','itemId','pile','itemState','owners','contract','fitness','lastSalePrice','timestamp') if k in x}
                exact=_definition_item(d,pile=int(x.get('pile',7) or 7),item_id=int(x.get('id',row['id']) or row['id']));exact.update(keep);exact['verifiedFifa18']=True
                con.execute('UPDATE items SET data=? WHERE id=?',(json.dumps(exact,separators=(',',':')),int(row['id'])));updated_items+=1
        for row in con.execute('SELECT trade_id,data FROM auctions').fetchall():
            try:a=json.loads(row['data']);x=a.get('itemData',{});rid=int(x.get('resourceId',x.get('definitionId',0)) or 0)
            except Exception:continue
            unverified_generated=bool(x.get('isSpecial') or x.get('specialType')) and not bool(x.get('verifiedFifa18')) and rid not in verified
            if (rid in legacy and rid not in verified) or unverified_generated:
                con.execute('DELETE FROM auctions WHERE trade_id=?',(int(row['trade_id']),));deleted_auctions+=1
        con.execute('INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)',('verifiedSpecialCleanup','0.8.9.33'))
        con.execute('INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)',('specialPlayersInClub','0'))
    log.warning('VERIFIED SPECIAL cleanup removedSyntheticItems=%d updatedVerifiedItems=%d removedSyntheticAuctions=%d keptVerified=%d',deleted_items,updated_items,deleted_auctions,len(verified))
    return deleted_items+deleted_auctions+updated_items

def _repair_stored_player_identities():
    """Migrate and repair existing items in SQLite items/auctions tables.

    Fixes cards that were previously persisted with dummy nation=1 (Albanian flag)
    or dummy team=1 (Arsenal crest) so that user-owned cards immediately display
    authentic national flags and club badges in-game.
    """
    repaired_items = 0
    repaired_auctions = 0
    try:
        with _DB_LOCK, _db_connect() as con:
            for row in con.execute('SELECT id, data FROM items').fetchall():
                try:
                    raw_data = json.loads(row['data'])
                except Exception:
                    continue
                if not isinstance(raw_data, dict) or raw_data.get('itemType') != 'player':
                    continue
                aid = int(raw_data.get('assetId', 0) or 0)
                if aid <= 0:
                    continue
                base = _definition_by_asset(aid)
                enriched = _enrich_and_repair_player_identity(raw_data, base)
                changed = False
                for k in ('nation', 'nationId', 'teamId', 'teamid', 'leagueId', 'nationality', 'clubName', 'name', 'displayName'):
                    if enriched.get(k) != raw_data.get(k):
                        changed = True
                        break
                if changed:
                    con.execute('UPDATE items SET data=? WHERE id=?',
                                (json.dumps(enriched, separators=(',', ':')), int(row['id'])))
                    repaired_items += 1
                    log.warning('PLAYER IDENTITY REPAIRED item=%s name=%s asset=%d nation=%s team=%s',
                                row['id'], enriched.get('name'), aid, enriched.get('nation'), enriched.get('teamId'))

            for row in con.execute('SELECT trade_id, data FROM auctions').fetchall():
                try:
                    auc = json.loads(row['data'])
                except Exception:
                    continue
                item_data = auc.get('itemData', {})
                if not isinstance(item_data, dict) or item_data.get('itemType') != 'player':
                    continue
                aid = int(item_data.get('assetId', 0) or 0)
                if aid <= 0:
                    continue
                base = _definition_by_asset(aid)
                enriched = _enrich_and_repair_player_identity(item_data, base)
                changed = False
                for k in ('nation', 'nationId', 'teamId', 'teamid', 'leagueId', 'nationality', 'clubName', 'name', 'displayName'):
                    if enriched.get(k) != item_data.get(k):
                        changed = True
                        break
                if changed:
                    auc['itemData'] = enriched
                    con.execute('UPDATE auctions SET data=? WHERE trade_id=?',
                                (json.dumps(auc, separators=(',', ':')), int(row['trade_id'])))
                    repaired_auctions += 1
    except Exception as e:
        log.warning('PLAYER IDENTITY repair failed: %s', e)
    if repaired_items > 0 or repaired_auctions > 0:
        log.warning('PLAYER IDENTITY migration completed: repairedItems=%d repairedAuctions=%d',
                    repaired_items, repaired_auctions)
    return repaired_items + repaired_auctions



def _card_face_tuple(d):
    if not isinstance(d,dict):return ()
    face=_extract_face_attributes(d, 0)
    if any(v != 0 for v in face):
        return tuple(face)
    return ()

def _card_position(d):
    if not isinstance(d,dict):return ''
    return str(d.get('preferredPosition',d.get('position','')) or '').strip().upper()

def _raw_verified_special_rows():
    """Read raw exact-card rows without resource-id first-wins deduplication."""
    rows=[]
    for fp in (VERIFIED_SPECIAL_FILE,VERIFIED_SPECIAL_CACHE):
        try:
            if not fp.exists():continue
            doc=json.loads(fp.read_text(encoding='utf-8'))
            vals=doc.get('players',doc) if isinstance(doc,dict) else doc
            if isinstance(vals,list):rows.extend(dict(x) for x in vals if isinstance(x,dict))
        except Exception as e:log.warning('CARD IDENTITY raw source unavailable %s: %s',fp,e)
    for seed in _VERIFIED_SPECIAL_SEED:
        d=_special_from_verified_row(*seed)
        if d:rows.append(d)
    return [_apply_exact_card_override(x) for x in rows]

# Historical FIFA 18 TOTS classification manifest built from the FUTWIZ FIFA 18
# promo archive supplied by the user.  Match on name + rating + position only:
# this distinguishes examples such as 94 CAM Fekir (TOTS) from 95 CF Fekir
# (World Cup Winners / Festival-era card), avoiding the broad-page mistake that
# turned unrelated re-releases pink.
FUTWIZ_TOTS_FILE=ROOT/'data'/'fifa18-futwiz-tots.json'
_FUTWIZ_TOTS_KEYS=None

def _promo_name_key(value):
    import unicodedata
    text=unicodedata.normalize('NFKD',str(value or '')).encode('ascii','ignore').decode('ascii').casefold()
    return re.sub(r'[^a-z0-9]+','',text)

def _futwiz_tots_keys():
    global _FUTWIZ_TOTS_KEYS
    if _FUTWIZ_TOTS_KEYS is not None:return _FUTWIZ_TOTS_KEYS
    keys=set()
    try:
        doc=json.loads(FUTWIZ_TOTS_FILE.read_text(encoding='utf-8'))
        rows=doc.get('players',[]) if isinstance(doc,dict) else []
        for x in rows:
            if not isinstance(x,dict):continue
            try:key=(_promo_name_key(x.get('name')),int(x.get('rating',0) or 0),str(x.get('position','') or '').upper())
            except Exception:continue
            if key[0] and key[1]>0 and key[2]:keys.add(key)
        log.warning('FUTWIZ TOTS manifest loaded rows=%d uniqueKeys=%d',len(rows),len(keys))
    except Exception as e:
        log.warning('FUTWIZ TOTS manifest unavailable: %s',e)
    _FUTWIZ_TOTS_KEYS=keys
    return keys

def _futwiz_tots_match(x):
    if not isinstance(x,dict):return False
    try:key=(_promo_name_key(x.get('name',x.get('displayName',''))),int(x.get('rating',0) or 0),_card_position(x))
    except Exception:return False
    return key in _futwiz_tots_keys()

# Direct FUTWIZ FIFA 18 player pages classify these 95-rated cards as
# End of an Era SBC specials, not Festival cards.
_FUTWIZ_EXACT_PROMO_KEYS={
    (_promo_name_key('Vincent Kompany'),95,'CB'):(24,'SBC'),
    (_promo_name_key('Kompany'),95,'CB'):(24,'SBC'),
    (_promo_name_key('Daniele De Rossi'),95,'CDM'):(24,'SBC'),
    (_promo_name_key('De Rossi'),95,'CDM'):(24,'SBC'),
}

def _futwiz_exact_promo(x):
    if not isinstance(x,dict):return None
    try:key=(_promo_name_key(x.get('name',x.get('displayName',''))),int(x.get('rating',0) or 0),_card_position(x))
    except Exception:return None
    return _FUTWIZ_EXACT_PROMO_KEYS.get(key)

def _festival_identity_migration(force=False,phase='startup'):
    """Repair broad Festival/re-release rows without treating re-releases as FOF.

    v36.18 adds the FUTWIZ historical TOTS archive as a classification source.
    A broad Festival/re-release listing is never treated as a promo identity.
    Exact non-FOF resource/fingerprint matches remain highest priority, then an
    exact historical name/rating/position TOTS match repairs stale pink rows.
    Genuine Festival/World-Cup-Winner cards which do not match TOTS are retained.
    """
    marker='0.8.9.36.18:'+str(phase)
    if not force and _meta_get('festivalIdentityCleanup','')==marker:
        return {'changed':0,'removed':0,'unresolved':0,'totsDuplicateGroups':0,'futwizTots':0}
    raw=_raw_verified_special_rows()
    canonical=[]
    for d in raw:
        try:rf=int(d.get('rareflag',d.get('rareFlag',0)) or 0)
        except Exception:rf=0
        if rf==35:continue
        try:rid=int(d.get('resourceId',d.get('definitionId',0)) or 0)
        except Exception:rid=0
        if rid>0:canonical.append(dict(d))
    by_rid={};by_fp={};by_basic={}
    for d in canonical:
        try:
            rid=int(d.get('resourceId',d.get('definitionId',0)) or 0);aid=int(d.get('assetId',0) or 0);rating=int(d.get('rating',0) or 0)
        except Exception:continue
        if rid:by_rid[rid]=d
        fp=(aid,rating,_card_position(d),_card_face_tuple(d))
        if aid and rating and fp[-1]:by_fp.setdefault(fp,[]).append(d)
        basic=(aid,rating,_card_position(d))
        if aid and rating:by_basic.setdefault(basic,[]).append(d)

    # Snapshot the still-bad Festival population before editing. Two or more
    # distinct resource IDs with exactly the same asset/rating/position/face are
    # historical re-release duplicates, not two distinct FOF upgrades.
    bad_groups={}
    with _DB_LOCK,_db_connect() as con:
        rows=con.execute('SELECT id,data FROM items').fetchall()
    for row in rows:
        try:x=json.loads(row['data']);rf=int(x.get('rareflag',x.get('rareFlag',0)) or 0)
        except Exception:continue
        if str(x.get('itemType','')).lower()!='player' or rf!=35:continue
        try:key=(int(x.get('assetId',0) or 0),int(x.get('rating',0) or 0),_card_position(x),_card_face_tuple(x))
        except Exception:continue
        if key[-1]:bad_groups.setdefault(key,[]).append((int(row['id']),x))
    duplicate_tots={}
    for key,vals in bad_groups.items():
        resources={int(x.get('resourceId',x.get('definitionId',0)) or 0) for _,x in vals}
        if len(resources)>=2:
            # Retain the earliest observed resource version as the local canonical
            # identity, then exact-resource dedupe removes the re-release copies.
            duplicate_tots[key]=min(r for r in resources if r>0)

    changed=removed=unresolved=tots_dup=futwiz_tots=0
    def resolve(x):
        nonlocal tots_dup,futwiz_tots
        try:
            rid=int(x.get('resourceId',x.get('definitionId',0)) or 0);aid=int(x.get('assetId',0) or 0);rating=int(x.get('rating',0) or 0)
        except Exception:return None
        if rid in by_rid:return by_rid[rid]
        fp=(aid,rating,_card_position(x),_card_face_tuple(x))
        hits=by_fp.get(fp,[]) if fp[-1] else []
        if len({int(h.get('resourceId',h.get('definitionId',0)) or 0) for h in hits})==1:return hits[0]
        hits=by_basic.get((aid,rating,_card_position(x)),[])
        uniq={int(h.get('resourceId',h.get('definitionId',0)) or 0):h for h in hits}
        if len(uniq)==1:
            only=next(iter(uniq.values()))
            if int(only.get('rareflag',only.get('rareFlag',0)) or 0)==11:return only
        exact_promo=_futwiz_exact_promo(x)
        if exact_promo:
            rf,stype=exact_promo;d=dict(x);d['rareflag']=rf;d['rareFlag']=rf;d['specialType']=stype;d['cardType']=stype;d['isSpecial']=True;d['verifiedFifa18']=True
            d['identitySource']='FUTWIZ FIFA 18 exact promo page';return d
        if _futwiz_tots_match(x):
            d=dict(x);d['rareflag']=11;d['rareFlag']=11;d['specialType']='TOTS';d['cardType']='TOTS';d['isSpecial']=True;d['verifiedFifa18']=True
            d['identitySource']='FUTWIZ FIFA 18 TOTS archive (name/rating/position)'
            futwiz_tots+=1;return d
        return None
    preserve=('id','itemId','pile','itemState','owners','contract','fitness','lastSalePrice','timestamp','untradeable','tradeable')
    with _DB_LOCK,_db_connect() as con:
        for row in con.execute('SELECT id,data FROM items').fetchall():
            try:x=json.loads(row['data']);rf=int(x.get('rareflag',x.get('rareFlag',0)) or 0)
            except Exception:continue
            if str(x.get('itemType','')).lower()!='player' or rf!=35:continue
            d=resolve(x)
            if not d:
                unresolved+=1;continue
            keep={k:x.get(k) for k in preserve if k in x}
            exact=_definition_item(d,pile=int(x.get('pile',7) or 7),item_id=int(x.get('id',row['id']) or row['id']))
            exact.update(keep);exact['verifiedFifa18']=True;exact['festivalReReleaseMigrated']=True
            con.execute('UPDATE items SET data=? WHERE id=?',(json.dumps(exact,separators=(',',':')),int(row['id'])));changed+=1

        # A broad Festival re-release can carry the same TOTS face under a later
        # resource/version id. After classification, collapse those same-face
        # TOTS variants onto the earliest resource id (the original release),
        # then the exact-resource deduper below can remove the duplicate club
        # copies. This is deliberately limited to rareFlag 11 so distinct promo
        # families with coincident ratings/stats remain untouched.
        tots_groups={};tots_resource_remap={};tots_rewrites=0
        for rr in con.execute('SELECT id,data FROM items').fetchall():
            try:
                xx=json.loads(rr['data']);rrf=int(xx.get('rareflag',xx.get('rareFlag',0)) or 0)
                rrid=int(xx.get('resourceId',xx.get('definitionId',0)) or 0);aa=int(xx.get('assetId',0) or 0);rat=int(xx.get('rating',0) or 0)
            except Exception:continue
            if str(xx.get('itemType','')).lower()!='player' or rrf!=11 or rrid<=0:continue
            key=(aa,rat,_card_position(xx),_card_face_tuple(xx))
            if aa and rat and key[-1]:tots_groups.setdefault(key,[]).append((int(rr['id']),xx,rrid))
        for key,vals in tots_groups.items():
            resources=sorted({rid for _,_,rid in vals if rid>0})
            if len(resources)<2:continue
            canonical_rid=resources[0]
            for old_rid in resources[1:]:tots_resource_remap[old_rid]=canonical_rid
            for iid,xx,old_rid in vals:
                if old_rid==canonical_rid:continue
                xx['resourceId']=canonical_rid;xx['definitionId']=canonical_rid
                xx['totsReReleaseCanonicalized']=True
                con.execute('UPDATE items SET data=? WHERE id=?',(json.dumps(xx,separators=(',',':')),iid));tots_rewrites+=1

        for row in con.execute('SELECT trade_id,data FROM auctions').fetchall():
            try:a=json.loads(row['data']);x=a.get('itemData',{});rf=int(x.get('rareflag',x.get('rareFlag',0)) or 0)
            except Exception:continue
            if not isinstance(x,dict) or rf!=35:continue
            d=resolve(x)
            if not d:continue
            keep={k:x.get(k) for k in preserve if k in x}
            exact=_definition_item(d,pile=int(x.get('pile',5) or 5),item_id=int(x.get('id',x.get('itemId',0)) or 0))
            exact.update(keep);exact['verifiedFifa18']=True;exact['festivalReReleaseMigrated']=True
            if int(exact.get('rareflag',exact.get('rareFlag',0)) or 0)==11:
                old_rid=int(exact.get('resourceId',exact.get('definitionId',0)) or 0)
                if old_rid in tots_resource_remap:
                    exact['resourceId']=tots_resource_remap[old_rid];exact['definitionId']=tots_resource_remap[old_rid]
                    exact['totsReReleaseCanonicalized']=True
            a['itemData']=exact
            con.execute('UPDATE auctions SET data=? WHERE trade_id=?',(json.dumps(a,separators=(',',':')),int(row['trade_id'])))
        con.execute('INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)',('festivalIdentityCleanup',marker))
        con.execute('INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)',('specialPlayersInClub','0'))
        con.execute('INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)',('exactPlayerResourceDedupe',''))
    log.warning('CARD IDENTITY migration phase=%s repaired=%d unresolvedFOF=%d canonicalNonFOF=%d futwizTots=%d duplicateGroupsObserved=%d totsCanonicalRewrites=%d',phase,changed,unresolved,len(canonical),futwiz_tots,len(duplicate_tots),tots_rewrites)
    return {'changed':changed,'removed':removed,'unresolved':unresolved,'totsDuplicateGroups':len(duplicate_tots),'futwizTots':futwiz_tots,'totsCanonicalRewrites':tots_rewrites}

def _cleanup_exact_player_resource_duplicates():
    """Keep one club copy of each exact player resource ID.

    A FUT club cannot hold two copies of the exact same player definition. Older
    local builds could leave duplicate seeded specials after a catalogue refresh.
    Preserve a copy referenced by a saved squad when possible, otherwise the
    oldest/smallest local item id wins. Distinct resource IDs (for example TOTS
    and FUT Champions cards at the same overall) are deliberately retained.
    """
    if _meta_get('exactPlayerResourceDedupe','')=='0.8.9.33':return 0
    referenced=set()
    with _DB_LOCK,_db_connect() as con:
        for row in con.execute('SELECT data FROM squads').fetchall():
            try:doc=json.loads(row['data'])
            except Exception:continue
            def walk(v):
                if isinstance(v,dict):
                    if isinstance(v.get('itemData'),dict):
                        try:
                            iid=int(v['itemData'].get('id',0) or 0)
                            if iid:referenced.add(iid)
                        except Exception:pass
                    for z in v.values():walk(z)
                elif isinstance(v,list):
                    for z in v:walk(z)
            walk(doc)
        groups={}
        for row in con.execute('SELECT id,data FROM items ORDER BY id').fetchall():
            try:x=json.loads(row['data'])
            except Exception:continue
            if str(x.get('itemType','')).lower()!='player' or int(x.get('pile',7) or 0)!=7:continue
            try:rid=int(x.get('resourceId',x.get('definitionId',x.get('assetId',0))) or 0)
            except Exception:continue
            if rid<=0:continue
            groups.setdefault(rid,[]).append((int(row['id']),x))
        removed=0
        for rid,copies in groups.items():
            if len(copies)<=1:continue
            keep=next((iid for iid,_ in copies if iid in referenced),copies[0][0])
            for iid,_ in copies:
                if iid==keep:continue
                con.execute('DELETE FROM items WHERE id=?',(iid,));removed+=1
        con.execute('INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)',('exactPlayerResourceDedupe','0.8.9.33'))
    log.warning('EXACT PLAYER RESOURCE dedupe removed=%d distinctResources=%d',removed,len(groups))
    return removed

def _all_player_defs():
    return list(_load_player_defs(False))+list(_special_player_defs())

def _is_special_def(d):
    if not isinstance(d,dict):return False
    try:rf=int(d.get('rareflag',d.get('rareFlag',0)) or 0)
    except Exception:rf=0
    return bool(d.get('isSpecial')) or rf>1 or int(d.get('resourceId',d.get('definitionId',d.get('assetId',0))) or 0)!=int(d.get('assetId',0) or 0)

def _ensure_all_players_in_club(defs=None):
    """One-time bulk seed of every FIFA 18 base player into My Club.

    This intentionally uses one SQLite transaction rather than _save_item() so
    a 15k-player collection is fast enough to create during startup.
    """
    defs=list(defs or _load_player_defs(False))
    if len(defs)<1000:
        log.warning('ALL PLAYERS seed deferred: player database only has %d definitions',len(defs))
        return 0
    with _DB_LOCK, _db_connect() as con:
        existing=set()
        for row in con.execute('SELECT data FROM items').fetchall():
            try:
                x=json.loads(row['data'])
                if str(x.get('itemType','')).lower()=='player' and int(x.get('pile',7) or 0)==7:
                    aid=int(x.get('assetId',x.get('definitionId',0)) or 0)
                    rid=int(x.get('resourceId',x.get('definitionId',aid)) or aid)
                    rf=int(x.get('rareflag',x.get('rareFlag',0)) or 0)
                    if aid and rid==aid and rf<=1:existing.add(aid)
            except Exception:pass
        missing=[d for d in defs if int(d.get('assetId',0) or 0) not in existing]
        if not missing:
            con.execute('INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)',('allPlayersInClub',str(len(defs))))
            log.warning('ALL PLAYERS already present ownedBaseAssets=%d',len(existing))
            return 0
        try:next_id=int((con.execute("SELECT value FROM meta WHERE key='nextItemId'").fetchone() or ['790000000001'])[0])
        except Exception:next_id=790000000001
        rows=[]
        for d in missing:
            item=_definition_item(d,pile=7,rare=(int(d.get('rating',0) or 0)>=75),item_id=next_id)
            next_id+=1
            rows.append((int(item['id']),json.dumps(item,separators=(',',':'))))
        con.executemany('INSERT OR REPLACE INTO items(id,data) VALUES(?,?)',rows)
        con.execute('INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)',('nextItemId',str(next_id)))
        con.execute('INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)',('allPlayersInClub',str(len(defs))))
    log.warning('ALL PLAYERS seeded new=%d database=%d MyClub now=%d',len(missing),len(defs),len(_owned_players()))
    return len(missing)

def _refresh_owned_base_player_metadata(defs=None):
    """Refresh searchable metadata without replacing owned item instances.

    Contracts, fitness, pile, item IDs and squad references are preserved.  This
    migration is what makes position/club filters reliable for clubs created by
    older builds as well as newly seeded players.
    Also heals corrupted/50s attributeList and attributeArray on existing owned players.
    """
    defs=list(defs or _load_player_defs(False));by_asset={int(d.get('assetId',0) or 0):d for d in defs if int(d.get('assetId',0) or 0)>0}
    changed=0;healed=0
    with _DB_LOCK,_db_connect() as con:
        for row in con.execute('SELECT id,data FROM items').fetchall():
            try:x=json.loads(row['data'])
            except Exception:continue
            if str(x.get('itemType','')).lower()!='player':continue
            try:
                aid=int(x.get('assetId',0) or 0);rid=int(x.get('resourceId',x.get('definitionId',aid)) or aid);rf=int(x.get('rareflag',x.get('rareFlag',0)) or 0)
            except Exception:continue
            if aid<=0:continue
            dirty=False
            d=by_asset.get(aid)

            # 1. Base player metadata sync (if base card)
            if rid==aid and rf<=1 and d:
                pos=str(d.get('position',x.get('preferredPosition','CM')) or 'CM').upper()
                updates={
                    'preferredPosition':pos,'position':pos,
                    'teamid':int(d.get('teamId',x.get('teamid',0)) or 0),'teamId':int(d.get('teamId',x.get('teamId',0)) or 0),
                    'leagueId':int(d.get('leagueId',x.get('leagueId',0)) or 0),'nation':int(d.get('nation',x.get('nation',0)) or 0),
                }
                if d.get('positions'):updates['alternatePositions']=list(d.get('positions') or [])
                if d.get('name'):updates['name']=str(d['name']);updates['displayName']=str(d['name'])
                if d.get('clubName'):updates['clubName']=str(d['clubName'])
                if d.get('nationality'):updates['nationality']=str(d['nationality'])
                for k,v in updates.items():
                    if x.get(k)!=v:x[k]=v;dirty=True

            # 1b. Special card metadata sync (if special card)
            if _is_special_def(x) or rf>1 or rid!=aid:
                s_def=_definition_by_resource(rid)
                if s_def:
                    s_pos=str(s_def.get('position',s_def.get('preferredPosition','')) or '').upper()
                    s_rating=int(s_def.get('rating',0) or 0)
                    if s_pos and s_pos!=str(x.get('preferredPosition','')).upper():
                        x['preferredPosition']=s_pos;x['position']=s_pos
                        x['cardsubtypeid']=int(PLAYER_CARD_SUBTYPE.get(s_pos,3))
                        dirty=True
                    if s_rating>0 and s_rating!=int(x.get('rating',0) or 0):
                        x['rating']=s_rating;dirty=True
                    s_face=_extract_face_attributes(s_def,s_rating or 50)
                    if any(v!=50 for v in s_face):
                        curr_face=[int(a.get('value',0) if isinstance(a,dict) else a) for a in (x.get('attributeArray') or x.get('attributeList') or [])[:6]]
                        if curr_face!=s_face:
                            x['attributeArray']=list(s_face);x['attributeList']=_attrs(s_face);dirty=True;healed+=1

            # 2. Attribute healing: check if this player has all 50 stats bug
            curr_attrs=[int(a.get('value',0) if isinstance(a,dict) else a) for a in (x.get('attributeArray') or x.get('attributeList') or [])[:6]]
            is_corrupted=(len(curr_attrs)<6 or (all(v==50 for v in curr_attrs[:6]) and int(x.get('rating',0) or 0)!=50))
            if is_corrupted:
                defn=_definition_by_resource(rid) or (d if d else _definition_by_asset(aid))
                if defn:
                    real_face=_extract_face_attributes(defn, int(x.get('rating',50) or 50))
                    if any(v!=50 for v in real_face):
                        x['attributeArray']=list(real_face)
                        x['attributeList']=_attrs(real_face)
                        dirty=True;healed+=1

            if dirty:
                con.execute('UPDATE items SET data=? WHERE id=?',(json.dumps(x,separators=(',',':')),int(row['id'])));changed+=1
        con.execute('INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)',('fullPlayerMetadataRefresh','0.8.9.36.stats_fix_v2'))
    if healed:
        log.warning('FULL PLAYER DB attribute healing restored stats for %d items',healed)
    log.warning('FULL PLAYER DB owned metadata refresh changed=%d healed=%d definitions=%d',changed,healed,len(defs))
    return changed

def _ensure_special_players_in_club(defs=None):
    # Never freeze a tiny bootstrap-derived special catalogue into a fresh save.
    # The normal startup loads the full FIFA 18 snapshot before this function.
    if len(_load_player_defs(False))<1000:
        log.warning('SPECIAL PLAYERS seed deferred: full player database unavailable')
        return 0
    defs=list(defs or _special_player_defs())
    if not defs:return 0
    with _DB_LOCK,_db_connect() as con:
        existing=set()
        for row in con.execute('SELECT data FROM items').fetchall():
            try:
                x=json.loads(row['data'])
                if str(x.get('itemType','')).lower()=='player' and int(x.get('pile',7) or 0)==7:
                    rid=int(x.get('resourceId',x.get('definitionId',x.get('assetId',0))) or 0)
                    if rid:existing.add(rid)
            except Exception:pass
        missing=[d for d in defs if int(d.get('resourceId',d.get('definitionId',0)) or 0) not in existing]
        if not missing:
            con.execute('INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)',('specialPlayersInClub',str(len(defs))))
            return 0
        try:next_id=int((con.execute("SELECT value FROM meta WHERE key='nextItemId'").fetchone() or ['790000000001'])[0])
        except Exception:next_id=790000000001
        rows=[]
        for d in missing:
            item=_definition_item(d,pile=7,item_id=next_id);next_id+=1
            rows.append((int(item['id']),json.dumps(item,separators=(',',':'))))
        con.executemany('INSERT OR REPLACE INTO items(id,data) VALUES(?,?)',rows)
        con.execute('INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)',('nextItemId',str(next_id)))
        con.execute('INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)',('specialPlayersInClub',str(len(defs))))
    log.warning('SPECIAL PLAYERS seeded new=%d specials=%d',len(missing),len(defs))
    return len(missing)



def _catalog_instance(defn,item_id):
    d=dict(defn or {});typ=str(d.get('itemType','')).lower();iid=int(item_id)
    if typ=='manager':
        aid=int(d.get('assetId',d.get('headId',0)) or 0);rid=int(d.get('resourceId',d.get('definitionId',aid)) or aid)
        name=str(d.get('name',d.get('displayName','Manager')) or 'Manager')
        return {'id':iid,'itemId':iid,'assetId':aid,'headId':aid,'managerId':aid,'definitionId':rid,'resourceId':rid,
                'resourceGameYear':2019,'itemType':'manager','cardsubtypeid':int(d.get('cardsubtypeid',4) or 4),
                'itemState':'free','pile':7,'rating':int(d.get('rating',75) or 75),'rareflag':int(d.get('rareflag',0) or 0),
                'rareFlag':int(d.get('rareflag',0) or 0),'nation':int(d.get('nation',0) or 0),'nationId':int(d.get('nationId',d.get('nation',0)) or 0),
                'leagueId':int(d.get('leagueId',0) or 0),'managerLeagueId':int(d.get('managerLeagueId',d.get('leagueId',0)) or 0),
                'owners':1,'untradeable':False,'tradeable':True,'contract':99,'contracts':99,'loans':0,'fitness':99,'discardValue':0,'lastSalePrice':0,'timestamp':int(time.time()),
                'name':name,'displayName':name,'managerName':name,'commonName':name,'label':name,'description':name,
                'nationality':str(d.get('nationality','') or ''),'quality':str(d.get('quality','') or ''),'concept':False,'draftItem':False}
    rid=int(d.get('resourceId',d.get('definitionId',0)) or 0);aid=int(d.get('assetId',d.get('cardassetid',0)) or 0)
    return {'id':iid,'itemId':iid,'assetId':aid,'definitionId':rid,'resourceId':rid,'resourceGameYear':2019,
            'itemType':str(d.get('itemType','training') or 'training'),'cardsubtypeid':int(d.get('cardsubtypeid',0) or 0),
            'itemState':'free','pile':7,'rating':int(d.get('rating',80) or 80),'rareflag':int(d.get('rareflag',0) or 0),
            'rareFlag':int(d.get('rareflag',0) or 0),'owners':1,'untradeable':False,'tradeable':True,'discardValue':0,'lastSalePrice':0,
            'timestamp':int(time.time()),'category':str(d.get('category','') or ''),'level':str(d.get('level','') or ''),
            'consumableType':str(d.get('consumableType','') or ''),'amount':max(99,int(d.get('amount',99) or 99)),
            'name':str(d.get('name','Consumable') or 'Consumable'),'displayName':str(d.get('name','Consumable') or 'Consumable')}

def _manager_defs(refresh=False):
    global _MANAGER_DEFS
    if _MANAGER_DEFS is not None and not refresh:return _MANAGER_DEFS
    _MANAGER_DEFS=load_manager_definitions(MANAGER_CACHE)
    return _MANAGER_DEFS


def _migrate_exact_managers():
    marker=_meta_get('exactManagerCleanup','')
    if marker=='0.8.9.38':return 0
    defs={int(d.get('assetId',0) or 0):dict(d) for d in _manager_defs(False) if int(d.get('assetId',0) or 0)>0}
    allowed=set(defs);removed=updated=auctions=0
    with _DB_LOCK,_db_connect() as con:
        for row in con.execute('SELECT id,data FROM items').fetchall():
            try:x=json.loads(row['data'])
            except Exception:continue
            if str(x.get('itemType','')).lower()!='manager':continue
            try:aid=int(x.get('assetId',x.get('headId',0)) or 0)
            except Exception:aid=0
            if aid not in allowed:
                con.execute('DELETE FROM items WHERE id=?',(int(row['id']),));removed+=1;continue
            d=defs[aid];resource=int(d.get('resourceId',aid) or aid)
            fixes={'headId':aid,'managerId':aid,'definitionId':resource,'resourceId':resource,
                   'nation':int(d.get('nation',0) or 0),'nationId':int(d.get('nationId',d.get('nation',0)) or 0),
                   'leagueId':int(d.get('leagueId',0) or 0),'managerLeagueId':int(d.get('managerLeagueId',d.get('leagueId',0)) or 0),
                   'name':str(d.get('name',x.get('name','Manager')) or 'Manager'),
                   'displayName':str(d.get('displayName',d.get('name',x.get('displayName','Manager'))) or 'Manager'),
                   'managerName':str(d.get('managerName',d.get('name',x.get('managerName','Manager'))) or 'Manager'),
                   'nationality':str(d.get('nationality',x.get('nationality','')) or ''),'verifiedFifa18Manager':True,
                   'contract':max(99,int(x.get('contract',99) or 99)),'contracts':max(99,int(x.get('contracts',99) or 99))}
            dirty=False
            for k,v in fixes.items():
                if x.get(k)!=v:x[k]=v;dirty=True
            if dirty:
                con.execute('UPDATE items SET data=? WHERE id=?',(json.dumps(x,separators=(',',':')),int(row['id'])));updated+=1
        for row in con.execute('SELECT trade_id,data FROM auctions').fetchall():
            try:a=json.loads(row['data']);x=a.get('itemData',{})
            except Exception:continue
            if str(x.get('itemType','')).lower()!='manager':continue
            try:aid=int(x.get('assetId',x.get('headId',0)) or 0)
            except Exception:aid=0
            if aid not in allowed:
                con.execute('DELETE FROM auctions WHERE trade_id=?',(int(row['trade_id']),));auctions+=1
        con.execute('INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)',('exactManagerCleanup','0.8.9.38'))
        con.execute('INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)',('managerCatalogCount',str(len(allowed))))
    log.warning('EXACT MANAGER cleanup removedPlaceholders=%d updatedExact=%d removedAuctions=%d exactManagers=%d',removed,updated,auctions,len(allowed))
    return removed+updated+auctions

def _ensure_catalog_nonplayers_in_club(manager_defs=None):
    managers=list(manager_defs or _manager_defs(False));cons=list(consumable_definitions())
    wanted=managers+cons
    with _DB_LOCK,_db_connect() as con:
        existing=set()
        for row in con.execute('SELECT data FROM items').fetchall():
            try:
                x=json.loads(row['data']);rid=int(x.get('resourceId',x.get('definitionId',0)) or 0);typ=str(x.get('itemType','')).lower()
                if rid and typ in ('manager','development','training'):existing.add((typ,rid))
            except Exception:pass
        try:next_id=int((con.execute("SELECT value FROM meta WHERE key='nextItemId'").fetchone() or ['790000000001'])[0])
        except Exception:next_id=790000000001
        rows=[]
        for d in wanted:
            typ=str(d.get('itemType','')).lower();rid=int(d.get('resourceId',d.get('definitionId',0)) or 0)
            if (typ,rid) in existing:continue
            item=_catalog_instance(d,next_id);next_id+=1
            rows.append((int(item['id']),json.dumps(item,separators=(',',':'))));existing.add((typ,rid))
        if rows:con.executemany('INSERT OR REPLACE INTO items(id,data) VALUES(?,?)',rows)
        con.execute('INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)',('nextItemId',str(next_id)))
        con.execute('INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)',('managerCatalogCount',str(len(managers))))
        con.execute('INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)',('consumableCatalogCount',str(len(cons))))
    log.warning('FUT18 NONPLAYER CATALOG managers=%d consumables=%d seeded=%d',len(managers),len(cons),len(rows))
    return len(rows)

_MAX_CONTRACTS=99

def _ensure_contract_health():
    with _DB_LOCK,_db_connect() as con:
        updated=0
        for row in con.execute('SELECT id,data FROM items').fetchall():
            try:x=json.loads(row['data'])
            except Exception:continue
            typ=str(x.get('itemType','')).lower()
            if typ in ('player','manager'):
                if x.get('skuMode')=='WC':continue
                c=int(x.get('contract',0) or 0)
                cs=int(x.get('contracts',0) or 0)
                loans=int(x.get('loans',0) or 0)
                fitness=int(x.get('fitness',99) or 99)
                dirty=False
                if c!=_MAX_CONTRACTS or cs!=_MAX_CONTRACTS:
                    x['contract']=_MAX_CONTRACTS;x['contracts']=_MAX_CONTRACTS;dirty=True
                if loans!=0:
                    x['loans']=0;dirty=True
                if fitness<99:
                    x['fitness']=99;dirty=True
                if 'success' in x:
                    x.pop('success',None);dirty=True
                if dirty:
                    con.execute('UPDATE items SET data=? WHERE id=?',(json.dumps(x,separators=(',',':')),int(row['id'])))
                    updated+=1
            elif typ in ('development','training','consumable'):
                if x.get('skuMode')=='WC':continue
                amt=int(x.get('amount',0) or 0)
                pile=int(x.get('pile',7) or 7)
                dirty=False
                if amt<99:
                    x['amount']=99;dirty=True
                if pile!=7:
                    x['pile']=7;dirty=True
                if dirty:
                    con.execute('UPDATE items SET data=? WHERE id=?',(json.dumps(x,separators=(',',':')),int(row['id'])))
                    updated+=1
        if updated:
            log.warning('CONTRACT HEALTH renewed contracts and consumables for %d items',updated)
    return updated

def _migrate_world_cup_items():
    """Startup routine: migrate and validate all stored World Cup items in SQLite.
    Ensures every item with skuMode == 'WC' conforms 100% to FIFA 18 World Cup DLC schema.
    """
    with _DB_LOCK, _db_connect() as con:
        updated = 0
        for row in con.execute('SELECT id, data FROM items').fetchall():
            try:
                x = json.loads(row['data'])
            except Exception:
                continue
            if not isinstance(x, dict):
                continue
            if x.get('skuMode') == 'WC' or x.get('pile') in ('wc_club', 'wc_squad', 'wc_trade'):
                if not x.get('assetId'):
                    if 'higuain' in str(x.get('name', '')).lower():
                        x['assetId'] = 167664
                        x['definitionId'] = 167664
                        x['resourceId'] = 167664
                orig_team = int(x.get('teamid', x.get('teamId', 0)) or 0)
                orig_league = int(x.get('leagueId', 0) or 0)
                orig_rare = int(x.get('rareflag', x.get('rareFlag', 0)) or 0)
                orig_rating = int(x.get('rating', 0) or 0)
                orig_attrs = x.get('attributeArray')
                fixed = _apply_world_cup_player_schema(x)
                if (orig_team != fixed.get('teamId') or
                    orig_league != fixed.get('leagueId') or
                    orig_rare != fixed.get('rareflag') or
                    orig_rating != fixed.get('rating') or
                    orig_attrs != fixed.get('attributeArray') or
                    x.get('skuMode') != 'WC' or
                    x.get('contract') != 99 or
                    not x.get('assetId')):
                    con.execute('UPDATE items SET data=? WHERE id=?',
                                (json.dumps(fixed, separators=(',', ':')), int(row['id'])))
                    updated += 1
        if updated:
            log.warning('WORLD CUP MIGRATION updated %d items to authentic WC national schema', updated)
    return updated

def _ensure_world_cup_players_in_club(defs=None):
    """Seed authentic World Cup 2018 squad players and icons into My Club for World Cup mode.

    1. All 17 authentic World Cup Icons (Pelé 98, Maradona 97, Ronaldo 94, Yashin 94, Henry 93, etc.).
    2. All 132 players with authentic World Cup ratings (Ronaldo 95, Messi 94, Neymar 93, Lewandowski 92, etc.).
    3. All players from the 32 qualified nations with rating >= 78.
    Ensures they are present in pile 7 with skuMode='WC', 99 contracts, 99 fitness, and authentic attributes.
    """
    _load_world_cup_data()
    defs = list(defs or _load_player_defs(False))
    if not defs:
        return 0

    with _DB_LOCK, _db_connect() as con:
        existing_wc_assets = set()
        for row in con.execute('SELECT data FROM items').fetchall():
            try:
                x = json.loads(row['data'])
                if str(x.get('itemType', '')).lower() == 'player' and x.get('skuMode') == 'WC':
                    aid = int(x.get('assetId', 0) or 0)
                    if aid > 0:
                        existing_wc_assets.add(aid)
            except Exception:
                pass

        seen = set()
        candidates = []

        # 1. 17 World Cup Icons
        for ic in _world_cup_icons():
            aid = int(ic.get('assetId', 0) or 0)
            if aid > 0 and aid not in seen:
                seen.add(aid)
                candidates.append(ic)

        # 2. Overrides from WC ratings map
        for aid, ov in _WC_RATINGS_MAP.items():
            if aid not in seen:
                seen.add(aid)
                base = _definition_by_asset(aid)
                if base:
                    c = dict(base)
                    c['rating'] = ov['rating']
                    c['position'] = ov['position']
                    c['face'] = ov['face']
                    candidates.append(c)

        # 3. Base cards from the 32 qualified nations with rating >= 78
        for d in defs:
            aid = int(d.get('assetId', 0) or 0)
            nat = int(d.get('nation', 0) or 0)
            rat = int(d.get('rating', 0) or 0)
            if aid > 0 and aid not in seen and nat in WC_32_NATIONS and rat >= 78:
                seen.add(aid)
                candidates.append(d)

        missing = [c for c in candidates if int(c.get('assetId', 0) or 0) not in existing_wc_assets]
        if not missing:
            log.warning('WORLD CUP PLAYERS already complete in club: %d present', len(existing_wc_assets))
            return 0

        try:
            next_id = int((con.execute("SELECT value FROM meta WHERE key='nextItemId'").fetchone() or ['790000000001'])[0])
        except Exception:
            next_id = 790000000001

        rows = []
        for c in missing:
            aid = int(c.get('assetId', 0) or 0)
            base_item = _definition_item(c, pile=7, rare=(int(c.get('rating', 0) or 0) >= 75), item_id=next_id)
            next_id += 1
            wc_item = _apply_world_cup_player_schema(base_item, defn=c)
            wc_item['pile'] = 7
            rows.append((int(wc_item['id']), json.dumps(wc_item, separators=(',', ':'))))

        if rows:
            con.executemany('INSERT OR REPLACE INTO items(id, data) VALUES(?, ?)', rows)
            con.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('nextItemId', ?)", (str(next_id),))
            con.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('worldCupPlayersInClub', ?)", (str(len(candidates)),))

        log.warning('WORLD CUP PLAYERS seeded new=%d totalCandidates=%d clubWcAssets=%d',
                    len(missing), len(candidates), len(existing_wc_assets) + len(missing))
        return len(missing)

def _club_counts(sku_mode=None):
    if sku_mode == 'WC':
        rows=[x for x in _db_items() if int(x.get('pile',7) or 0)==7 and x.get('skuMode')=='WC']
    elif sku_mode:
        rows=[x for x in _db_items() if int(x.get('pile',7) or 0)==7 and x.get('skuMode')!='WC']
    else:
        rows=[x for x in _db_items() if int(x.get('pile',7) or 0)==7]
    players=sum(1 for x in rows if str(x.get('itemType','')).lower()=='player')
    managers=sum(1 for x in rows if str(x.get('itemType','')).lower()=='manager')
    consumables=sum(1 for x in rows if str(x.get('itemType','')).lower() in ('development','training','consumable'))
    return {'players':players,'managers':managers,'consumables':consumables,'total':len(rows)}

def _club_year_stats_payload():
    """Native My Club counters used by FUT Central and StickerBook.

    FIFA 18's tile binder reads the retail FUT_MYCLUB_* stat names rather than
    the aggregate aliases alone. Publish the proven context 0/0 contract first,
    while retaining the later-year context aliases for screens that use them.
    """
    club_rows=[x for x in _db_items() if int(x.get('pile',7) or 0)==7]
    players=[x for x in club_rows if str(x.get('itemType','')).lower()=='player']
    owned=len(players);ratings=[int(x.get('rating',0) or 0) for x in players]
    bronze=sum(1 for r in ratings if 0<r<=64);silver=sum(1 for r in ratings if 65<=r<=74);gold=sum(1 for r in ratings if r>=75)
    rare_players=sum(1 for x in players if int(x.get('rareflag',x.get('rareFlag',0)) or 0)>0)
    def count_type(*names):
        names={str(n).lower() for n in names};return sum(1 for x in club_rows if str(x.get('itemType','')).lower() in names)
    managers=count_type('manager');consumables=count_type('development','training','consumable')
    kits=count_type('kit');badges=count_type('badge','custom');stadia=count_type('stadium');balls=count_type('ball');trophies=count_type('trophy')
    values={
        'players':owned,'playersEmployed':owned,'clubPlayers':owned,'playerCount':owned,'totalPlayers':owned,'ownedPlayers':owned,
        'rarePlayers':rare_players,'rarePlayersEmployed':rare_players,
        'playersBronze':bronze,'bronzePlayersEmployed':bronze,'playersSilver':silver,'silverPlayersEmployed':silver,
        'playersGold':gold,'goldPlayersEmployed':gold,'staff':managers,'staffEmployed':managers,
        'stadia':stadia,'stadiaOwned':stadia,'balls':balls,'ballsEarned':balls,'kits':kits,'kitsAvailable':kits,
        'badges':badges,'badgesAvailable':badges,'trophies':trophies,'trophiesWon':trophies,
    }
    stat_names=[
        ('FUT_MYCLUB_PLAYERS_EMPLOYED',owned),('FUT_MYCLUB_RARE_PLAYERS_EMPLOYED',rare_players),
        ('FUT_MYCLUB_GOLD_PLAYERS_EMPLOYED',gold),('FUT_MYCLUB_SILVER_PLAYERS_EMPLOYED',silver),
        ('FUT_MYCLUB_BRONZE_PLAYERS_EMPLOYED',bronze),('FUT_MYCLUB_STAFF_EMPLOYED',managers),
        ('FUT_MYCLUB_KITS_AVAILABLE',kits),('FUT_MYCLUB_BADGES_AVAILABLE',badges),
        ('FUT_MYCLUB_STADIA_OWNED',stadia),('FUT_MYCLUB_BALLS_EARNED',balls),('FUT_MYCLUB_TROPHIES_WON',trophies),
    ] + list(values.items())
    # Retail PC family first uses contextId=0/contextValue=0. Keep 0/2018,
    # 2/2018 and legacy 6/0 mirrors because FIFA 18 screens are inconsistent.
    contexts=((0,0),(0,2019),(2,2019),(6,0))
    entries=[{'contextId':cid,'contextValue':cv,'type':k,'typeValue':int(v)} for cid,cv in contexts for k,v in stat_names]
    rw,rd,rl=_record_triplet();total=len(club_rows)
    aggregates=[{'contextId':cid,'contextValue':cv,**values,'wins':rw,'draws':rd,'losses':rl,'clubCount':total,'clubItemCount':total} for cid,cv in contexts]
    out={**values,'year':2019,'total':total,'clubCount':total,'clubItemCount':total,'count':owned,
         'staffCount':managers,'managerCount':managers,'consumables':consumables,'consumableCount':consumables,
         'won':rw,'draw':rd,'loss':rl,'wins':rw,'draws':rd,'losses':rl,
         'stat':entries,'entries':entries,'statistics':entries,'stats':aggregates}
    out['clubStats']=aggregates;out['yearStats']=aggregates;out['playerStats']=aggregates
    return out

# ---- FIFA 18 Single Player Seasons ---------------------------------------
FIFA18_SEASON_TROPHY_BASE=9000000

def _season_state():
    def mget(key,default=0):
        try:return int(_meta_get(key,str(default)) or default)
        except Exception:return int(default)
    div=max(1,min(10,mget('offlineDivision',10)));wins=max(0,mget('offlineSeasonWins',0));draws=max(0,mget('offlineSeasonDraws',0));losses=max(0,mget('offlineSeasonLosses',0))
    played=min(10,wins+draws+losses);points=wins*3+draws
    return {'division':div,'seasonId':div,'wins':wins,'draws':draws,'losses':losses,'played':played,'remaining':max(0,10-played),'points':points,
            'titles':max(0,mget('offlineSeasonTitles',0)),'promotions':max(0,mget('offlineSeasonPromotions',0)),'relegations':max(0,mget('offlineSeasonRelegations',0)),
            'completed':bool(mget('offlineSeasonCompleted',0)),'result':mget('offlineSeasonResult',0),'prizeState':mget('offlineSeasonPrizeState',0),'prizeCoins':mget('offlineSeasonPrizeCoins',0)}

def _season_thresholds(div):
    table={1:(23,20,17),2:(21,18,15),3:(19,16,13),4:(18,15,12),5:(16,13,10),6:(16,13,10),7:(14,11,8),8:(13,10,7),9:(11,9,6),10:(12,9,0)}
    return table.get(int(div),(12,9,0))

def _season_progress_fields(st):
    # Safe scalar aliases for history/list responses. Do not put nested objects
    # into seasonData/progressData: native CardsDLL treats those as opaque data.
    return {'offlineDivision':int(st['division']),'offlineSeason':int(st['seasonId']),'seasonId':int(st['seasonId']),
            'gamesWon':int(st['wins']),'gamesDrawn':int(st['draws']),'gamesDraw':int(st['draws']),'gamesLost':int(st['losses']),
            'seasonGamesWon':int(st['wins']),'seasonGamesDraw':int(st['draws']),'seasonGamesLost':int(st['losses']),
            'wins':int(st['wins']),'draws':int(st['draws']),'losses':int(st['losses']),
            'matchesPlayed':int(st['played']),'gamesPlayed':int(st['played']),'matchesRemaining':int(st['remaining']),'gamesRemaining':int(st['remaining']),
            'points':int(st['points']),'seasonPoints':int(st['points']),'titles':int(st['titles']),'seasonTitlesWon':int(st['titles']),
            'promotions':int(st['promotions']),'seasonPromotions':int(st['promotions']),'relegations':int(st['relegations']),'seasonRelegations':int(st['relegations']),
            'seasonCompleted':1 if st['completed'] else 0,'seasonEndResult':int(st['result']),'prizeState':int(st['prizeState']),'prizeCoins':int(st['prizeCoins'])}

def _season_record(div,state=None):
    state=state or _season_state();title,promo,hold=_season_thresholds(div);trophy=FIFA18_SEASON_TROPHY_BASE+int(div)
    return {'id':int(div),'divisionId':int(div),'type':'OFFLINE','numMatches':10,'matchLengthMin':6,
            'pointsForTitle':title,'pointsForPromotion':promo,'pointsForRelegation':hold,
            'matches':[],'prizeSet':[],'elgOperation':'AND','elgReq':[],
            'trophyResourceId':trophy,'trophyUseCount':0,'visStartDays':3650,'visEndDays':3650,
            'startDateTime':0,'endDateTime':2147483647,'untilStartSeconds':0,'untilEndSeconds':315360000}

def _season_user_payload():
    """Fresh/current offline season descriptor in the native scalar contract.

    The 36.17 response crashed immediately after HTTP 200 because seasonData and
    progressData were JSON objects. Retail CardsDLL expects those members to be
    opaque strings and, on a fresh season, expects them to be absent. Division
    10 also uses the inverse current-season wire ordinal (1) on fresh bootstrap.
    """
    st=_season_state();active=bool(st['played'] or st['points'] or st['completed'])
    wire_div=int(st['division']) if active else (11-int(st['division']))
    out={'seasonId':int(st['seasonId']),'divisionId':wire_div,'offlineDivision':int(st['division']),
         'type':'offline','round':max(1,min(10,int(st['played'])+1)),'active':active and not bool(st['completed']),
         'seasonState':'active' if active and not st['completed'] else ('complete' if st['completed'] else 'inactive'),
         'seasonCompleted':bool(st['completed']),'seasonEndResult':int(st['result']),'creationTime':int(_established()),
         'points':int(st['points']),'wins':int(st['wins']),'draws':int(st['draws']),'losses':int(st['losses'])}
    # Only echo opaque client-authored blobs if a later build/save has stored one.
    data=str(_meta_get('offlineSeasonWireData','') or '')
    if data:
        out={'data':data,'dataVersion':max(1,int(_meta_get('offlineSeasonWireDataVersion','1') or 1)),'seasonData':data,**out}
    progress=str(_meta_get('offlineSeasonWireProgressData','') or '')
    if progress:
        out['progressData']=progress;out['progressDataVersion']=max(1,int(_meta_get('offlineSeasonWireProgressVersion','1') or 1))
    return out

def _season_list_payload():
    st=_season_state();rows=[_season_record(i,st) for i in range(1,11)]
    return {'seasons':rows,'seasonList':rows,'offlineSeasons':rows,'count':len(rows)}

def _season_trophy_payload(resource_id):
    rid=int(resource_id);div=max(1,min(10,rid-FIFA18_SEASON_TROPHY_BASE if rid>=FIFA18_SEASON_TROPHY_BASE else 10))
    return {'itemData':[{'id':rid,'assetId':rid,'definitionId':rid,'resourceId':rid,'itemType':'trophy','rareflag':0,'rareFlag':0,
                         'name':f'Division {div} Trophy','displayName':f'Division {div} Trophy','divisionId':div,'year':2019}]}

def _season_trophy_big():
    return b'BIGF'+(16).to_bytes(4,'little')+(0).to_bytes(4,'big')+(16).to_bytes(4,'big')


def _market_price_limits_payload(defid):
    try:defid=int(defid or 0)
    except Exception:defid=0
    card=next((x for x in _all_player_defs() if int(x.get('resourceId',x.get('definitionId',x.get('assetId',0))) or 0)==defid),None)
    min_price=int((card or {}).get('marketDataMinPrice',150) or 150);max_price=int((card or {}).get('marketDataMaxPrice',15000000) or 15000000)
    if min_price<=0:min_price=150
    if max_price<min_price:max_price=max(15000000,min_price)
    return [{'source':'local','defId':defid,'itemId':defid,'minPrice':min_price,'maxPrice':max_price}]

def _club_user_payload():
    c=_club_counts();info={'clubId':CLUB_ID,'clubName':_club_name(),'clubAbbr':_club_abbr(),'established':_established(),
        'credits':_credits(),'playerCount':c['players'],'players':c['players'],'ownedPlayers':c['players'],
        'staffCount':c['managers'],'managerCount':c['managers'],'consumableCount':c['consumables'],
        'clubItemCount':c['total'],'clubCount':c['total'],'count':c['total'],'total':c['total']}
    return {'clubInfo':dict(info),'clubId':CLUB_ID,'clubName':_club_name(),'clubAbbr':_club_abbr(),
            'clubCount':c['total'],'clubItemCount':c['total'],'count':c['total'],'total':c['total'],
            'playerCount':c['players'],'players':c['players'],'ownedPlayers':c['players'],
            'staffCount':c['managers'],'managerCount':c['managers'],'consumableCount':c['consumables'],'credits':_credits(),
            'user':[{'personaId':FAKE_PERSONA,'persona':PERSONA,'public':True}]}

def _refresh_full_catalog_background():
    global _ARCHIVE_PLAYER_DEFS,_MANAGER_DEFS
    # Staff is independent of the large player database, so refresh it first.
    try:
        managers=refresh_manager_cache(MANAGER_CACHE)
        if managers:
            _MANAGER_DEFS=managers;added=_ensure_catalog_nonplayers_in_club(managers)
            log.warning('FUT18 MANAGER CATALOG refreshed managers=%d seeded=%d',len(managers),added)
    except Exception as e:log.warning('FUT18 MANAGER CATALOG refresh failed: %s',e)
    # Give the non-blocking complete-roster refresh first chance to finish so the
    # historical card importer can map names against the largest base identity set.
    for _ in range(30):
        if len(_load_player_defs(False))>=17500:break
        time.sleep(0.5)
    try:
        dl=refresh_all_cards_csv(ALL_CARDS_CSV)
        if dl.get('ok'):
            exact=_exact_special_player_defs();rows=build_archive_variations(ALL_CARDS_CSV,_load_player_defs(False),exact,ARCHIVE_CARD_CACHE)
            _ARCHIVE_PLAYER_DEFS=[]
            log.warning('FUT18 ALL-CARD ARCHIVE reference ready historicalVariations=%d liveSyntheticCards=0 sourceBytes=%s',len(rows),dl.get('bytes'))
        else:log.warning('FUT18 ALL-CARD ARCHIVE refresh unavailable: %s',dl.get('error'))
    except Exception as e:log.exception('FUT18 ALL-CARD ARCHIVE refresh failed: %s',e)

def _club_consumables_payload(family=''):
    family=str(family or '').lower();rows=[]
    for x in _db_items():
        if int(x.get('pile',7) or 0)!=7 or str(x.get('itemType','')).lower() not in ('development','training','consumable'):continue
        cat=str(x.get('category','')).lower();ctype=str(x.get('consumableType','')).lower()
        if family and family not in ('all','any','development','training') and family not in cat and family not in ctype:continue
        rows.append(dict(x))
    return {'itemData':rows,'items':rows,'count':len(rows),'total':len(rows),'endOfList':True}


def _owned_players():
    return [x for x in _db_items() if str(x.get('itemType','')).lower()=='player' and int(x.get('pile',7) or 0)==7]

def _pending_items():
    # The unassigned/purchased pile is not player-only. Keep the helper generic
    # so future consumables/club items leave purchased/items as soon as they are moved.
    return [x for x in _db_items() if int(x.get('pile',0) or 0)==6]

def _db_items():
    with _DB_LOCK, _db_connect() as con:
        rows=con.execute('SELECT data FROM items ORDER BY id').fetchall()
    out=[]
    for row in rows:
        try:
            x=json.loads(row['data'])
            if isinstance(x,dict):out.append(x)
        except Exception:pass
    return out

def _db_item_map():return {int(x.get('id',0)):x for x in _db_items() if int(x.get('id',0))}

def _save_item(item):
    if not isinstance(item,dict):return None
    try:iid=int(item.get('id',item.get('itemId',0)) or 0)
    except Exception:iid=0
    if not iid:return None
    # Query only the row being updated. Building a 16k-entry _db_item_map for
    # every pack card made large promo packs take many seconds after the
    # all-players migration.
    old={}
    with _DB_LOCK,_db_connect() as con:
        row=con.execute('SELECT data FROM items WHERE id=?',(iid,)).fetchone()
        if row:
            try:old=json.loads(row['data'])
            except Exception:old={}
        merged=dict(old if isinstance(old,dict) else {});merged.update(item);merged['id']=iid;merged['itemId']=iid
        con.execute('INSERT OR REPLACE INTO items(id,data) VALUES(?,?)',(iid,json.dumps(merged,separators=(',',':'))))
    return merged

def _move_items(doc, sku_mode=None):
    """Apply a FUT pile move and return the retail-style per-item acknowledgements."""
    candidates=[]
    if isinstance(doc,list):candidates=doc
    elif isinstance(doc,dict):
        if isinstance(doc.get('itemData'),list):candidates=doc['itemData']
        else:candidates=[doc]

    item_map=_db_item_map()
    owned_by_resource={
        int(x.get('resourceId',x.get('definitionId',x.get('assetId',0))) or 0):int(x.get('id',0) or 0)
        for x in _owned_players()
    }
    acks=[];saved=[]
    pile_map={'club':7,'purchased':6,'unassigned':6,'transfer':5,'trade':5,'tradepile':5,'storage':11,'storagepile':11}
    for y in candidates:
        if not isinstance(y,dict):continue
        try:iid=int(y.get('id',y.get('itemId',0)) or 0)
        except Exception:iid=0
        requested_pile=y.get('pile','')
        pile_label=str(requested_pile).lower() if isinstance(requested_pile,str) else requested_pile
        numeric_pile=pile_map.get(pile_label,requested_pile) if isinstance(pile_label,str) else requested_pile
        try:numeric_pile=int(numeric_pile)
        except Exception:numeric_pile=0

        existing=item_map.get(iid)
        if not iid or not existing:
            acks.append({'id':iid,'pile':requested_pile,'success':False,'reason':'Item not found','errorCode':404})
            continue

        asset=int(existing.get('assetId',0) or 0)
        resource=int(existing.get('resourceId',existing.get('definitionId',asset)) or asset)
        is_player=str(existing.get('itemType','')).lower()=='player'
        duplicate_owner=owned_by_resource.get(resource,0) if is_player and numeric_pile==7 else 0
        if duplicate_owner and duplicate_owner!=iid:
            acks.append({'id':iid,'pile':requested_pile,'success':False,'reason':'Duplicate Item Type','errorCode':472})
            continue

        update=dict(y);update['id']=iid;update['itemId']=iid;update['pile']=numeric_pile
        is_wc = (sku_mode == 'WC') or (existing.get('skuMode') == 'WC')
        if is_wc:
            update['skuMode'] = 'WC'
            if is_player:
                update = _apply_world_cup_player_schema(update, defn=existing)
        row=_save_item(update)
        if row:
            saved.append(row)
            if is_player and numeric_pile==7 and resource:owned_by_resource[resource]=iid
            acks.append({'id':iid,'pile':requested_pile,'success':True})
        else:
            acks.append({'id':iid,'pile':requested_pile,'success':False,'reason':'Save failed','errorCode':500})
    return {'itemData':acks},saved,candidates

def _quicksell_items(item_ids):
    """Discard owned/unassigned FUT items atomically and return the retail response."""
    ids=[]
    for raw in item_ids or []:
        try:iid=int(raw)
        except Exception:continue
        if iid and iid not in ids:ids.append(iid)
    sold=[];credit_delta=0
    with _DB_LOCK, _db_connect() as con:
        q='SELECT id,data FROM items WHERE id IN (%s)' % ','.join('?' for _ in ids) if ids else ''
        rows=con.execute(q,ids).fetchall() if q else []
        by_id={int(r['id']):r for r in rows}
        for iid in ids:
            row=by_id.get(iid)
            if not row:continue
            try:item=json.loads(row['data'])
            except Exception:item={'id':iid,'itemId':iid,'discardValue':0}
            if not isinstance(item,dict):item={'id':iid,'itemId':iid,'discardValue':0}
            item['id']=iid;item['itemId']=iid
            try:value=max(0,int(item.get('discardValue',0) or 0))
            except Exception:value=0
            credit_delta+=value;sold.append(item)
        try:
            row=con.execute('SELECT value FROM meta WHERE key=?',('credits',)).fetchone()
            before=max(0,int(row['value'])) if row else 0
        except Exception:before=0
        after=before+credit_delta
        if ids:
            con.executemany('DELETE FROM items WHERE id=?',[(int(x['id']),) for x in sold])
        con.execute('INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)',('credits',str(after)))
    out={
        'items':[{'id':int(x['id']),'itemId':int(x['id'])} for x in sold],
        'totalCredits':int(after),
        # Compatibility aliases for the native PC HUD and our local diagnostics.
        'credits':int(after),'coins':int(after),'creditDelta':int(credit_delta),
        'currencies':[{'name':'COINS','funds':int(after),'finalFunds':int(after)}],
        'success':True,
    }
    log.warning('QUICK SELL requested=%s sold=%s delta=%d credits=%d->%d pendingNow=%d ownedPlayers=%d',
                ids,[int(x['id']) for x in sold],credit_delta,before,after,len(_pending_items()),len(_owned_players()))
    return out

_POS_MOD_MAP = {
    'LWBLB': 'LB', 'LBLWB': 'LWB', 'RWBRB': 'RB', 'RBRWB': 'RWB',
    'LMLW': 'LW', 'RMRW': 'RW', 'LWLM': 'LM', 'RWRM': 'RM',
    'LWLF': 'LF', 'RWRF': 'RF', 'LFLW': 'LW', 'RFRW': 'RW',
    'CMCAM': 'CAM', 'CAMCM': 'CM', 'CDMCM': 'CM', 'CMCDM': 'CDM',
    'CAMCF': 'CF', 'CFCAM': 'CAM', 'CFST': 'ST', 'STCF': 'CF',
}

def _apply_consumable(doc, resource_id=None):
    """Apply consumable cards (contract, fitness, healing, position, chemistry style)
    to a target player or manager in club, squad, or draft."""
    applications = []
    if resource_id is not None and int(resource_id or 0) > 0:
        rid = int(resource_id)
        entries = []
        if isinstance(doc, dict):
            entries = doc.get('apply', doc.get('itemData', []))
            if not isinstance(entries, list): entries = [entries]
            if not entries and doc.get('id'): entries = [doc]
        elif isinstance(doc, list):
            entries = doc
        for ent in entries:
            if isinstance(ent, dict):
                tid = int(ent.get('id', ent.get('itemId', 0)) or 0)
            elif isinstance(ent, (int, str)):
                try: tid = int(ent)
                except Exception: tid = 0
            else:
                tid = 0
            if tid: applications.append((tid, rid, 0))
    else:
        entries = []
        if isinstance(doc, dict):
            entries = doc.get('itemData', doc.get('apply', []))
            if not isinstance(entries, list): entries = [entries]
            if not entries and (doc.get('applyTo') or doc.get('targetId')): entries = [doc]
        elif isinstance(doc, list):
            entries = doc
        for ent in entries:
            if not isinstance(ent, dict): continue
            tid = int(ent.get('applyTo', ent.get('targetId', 0)) or 0)
            cid = int(ent.get('id', ent.get('consumableId', 0)) or 0)
            c_rid = int(ent.get('resourceId', ent.get('definitionId', 0)) or 0)
            if not tid and 'id' in ent and ('resourceId' in ent or 'definitionId' in ent):
                tid = int(ent.get('id', 0) or 0)
            if tid and (cid or c_rid):
                applications.append((tid, c_rid, cid))

    if not applications and isinstance(doc, dict) and doc.get('id'):
        applications.append((int(doc['id']), 0, 0))

    cons_by_rid = {int(c['resourceId']): dict(c) for c in consumable_definitions() if 'resourceId' in c}
    items_map = _db_item_map()
    updated_items = []

    for target_id, c_rid, c_id in applications:
        c_info = {}
        if c_rid and c_rid in cons_by_rid:
            c_info = cons_by_rid[c_rid]
        elif c_id and c_id in items_map:
            c_info = dict(items_map[c_id])
            c_rid = int(c_info.get('resourceId', c_info.get('definitionId', 0)) or 0)
            if c_rid in cons_by_rid:
                merged = dict(cons_by_rid[c_rid])
                merged.update(c_info)
                c_info = merged
        elif c_rid:
            c_info = {'resourceId': c_rid, 'category': 'Contract'}

        category = str(c_info.get('category', '') or '').capitalize()
        ctype = str(c_info.get('consumableType', '') or '')

        target = None
        is_draft = False
        draft_mode = None
        draft_slot = None

        if target_id in items_map:
            target = dict(items_map[target_id])
        else:
            for dm in ('SINGLE_PLAYER', 'ONLINE', 'WORLD_CUP_SINGLE_PLAYER', 'WORLD_CUP_ONLINE'):
                st = _draft_get_state(dm)
                mgr = st.get('selectedManager')
                if isinstance(mgr, dict) and int(mgr.get('id', 0) or 0) == target_id:
                    target = dict(mgr)
                    is_draft = True
                    draft_mode = dm
                    draft_slot = 'manager'
                    break
                picked = st.get('pickedBySlot', {}) if isinstance(st.get('pickedBySlot'), dict) else {}
                for slot, p in picked.items():
                    if isinstance(p, dict) and int(p.get('id', 0) or 0) == target_id:
                        target = dict(p)
                        is_draft = True
                        draft_mode = dm
                        draft_slot = str(slot)
                        break
                if target: break

        if not target:
            # An unknown target id must not be answered with a fabricated item:
            # a row carrying no itemType/resourceId/assetId is not a card the
            # client can bind to.  Acknowledge the miss instead.
            log.warning('CONSUMABLE APPLY TARGET NOT FOUND targetId=%d resourceId=%s', target_id, c_rid)
            updated_items.append({'id': target_id, 'itemId': target_id, 'success': False})
            continue

        if category == 'Fitness':
            target['fitness'] = 99
        elif category == 'Healing':
            target['injuryGames'] = 0
            target['injuryType'] = 'none'
        elif category == 'Positioning':
            new_pos = _POS_MOD_MAP.get(ctype.upper())
            if new_pos:
                target['position'] = new_pos
                target['preferredPosition'] = new_pos
        elif category in ('Chemistrystyle', 'ChemistryStyle', 'Gkchemistrystyle', 'GKChemistryStyle'):
            target['playStyle'] = int(c_info.get('cardsubtypeid', 250) or 250)
        elif category in ('Managerleague', 'ManagerLeague'):
            sub = int(c_info.get('cardsubtypeid', 0) or 0)
            target['leagueId'] = sub
            target['managerLeagueId'] = sub
        elif category in ('Training', 'Gktraining', 'GKTraining'):
            target['training'] = 1
        else:
            # Contract counts are capped at the club-wide full value.  Stacking
            # +28 per application previously persisted contract=127 into the
            # save and grew further on every re-apply.
            target['contract'] = _MAX_CONTRACTS
            target['contracts'] = _MAX_CONTRACTS
            target['loans'] = 0

        # 'success' is an acknowledgement flag for this response only.  Writing
        # it into the item persisted it into the club row and the Draft state.
        target.pop('success', None)
        ack = dict(target)
        ack['success'] = True

        if is_draft and draft_mode:
            st = _draft_get_state(draft_mode)
            if draft_slot == 'manager':
                st['selectedManager'] = target
            elif draft_slot is not None and isinstance(st.get('pickedBySlot'), dict):
                st['pickedBySlot'][draft_slot] = target
            _draft_save(st, draft_mode)
            log.warning('CONSUMABLE APPLIED DRAFT mode=%s slot=%s targetId=%d category=%s',
                        draft_mode, draft_slot, target_id, category)
        else:
            _save_item(target)
            log.warning('CONSUMABLE APPLIED CLUB targetId=%d name=%s category=%s',
                        target_id, target.get('name'), category)

        if c_id and c_id in items_map:
            c_row = dict(items_map[c_id])
            c_row['amount'] = max(99, int(c_row.get('amount', 99) or 99))
            _save_item(c_row)

        updated_items.append(ack)

    return {'itemData': updated_items, 'success': True}

def _client_get(key,default=None):
    if default is None:default={'entries':[]}
    with _DB_LOCK, _db_connect() as con:
        row=con.execute('SELECT data FROM clientdata WHERE key=?',(str(key),)).fetchone()
    if not row:return dict(default)
    try:
        doc=json.loads(row['data'])
        return doc if isinstance(doc,dict) else dict(default)
    except Exception:return dict(default)

def _client_set(key,doc):
    if not isinstance(doc,dict):doc={'entries':[]}
    with _DB_LOCK, _db_connect() as con:
        con.execute('INSERT OR REPLACE INTO clientdata(key,data) VALUES(?,?)',(str(key),json.dumps(doc,separators=(',',':'))))
    CLIENT_DATA[str(key)]=dict(doc)
    return doc

def _squad_id(value,default=1):
    try:sid=int(value or default)
    except Exception:sid=int(default)
    return sid if sid>0 else int(default)

def _save_squad(doc,sid_hint=None,query=None):
    """Persist a normal squad without allowing malformed requests to erase it."""
    active_id=_squad_id(_meta_get('activeSquadId','1'),1)
    if not isinstance(doc,dict) or not doc:
        log.warning('LOCAL SAVE squad rejected: payload is not a non-empty object')
        return _active_squad(sid_hint or active_id,query=query)
    sid=_squad_id(doc.get('id',doc.get('squadId',sid_hint or active_id)),sid_hint or active_id)
    if 'players' in doc and not isinstance(doc.get('players'),list):
        log.warning('LOCAL SAVE squad id=%s rejected: players is not a list',sid)
        return _active_squad(sid,query=query)
    with _DB_LOCK, _db_connect() as con:
        row=con.execute('SELECT data FROM squads WHERE id=?',(sid,)).fetchone()
        try:existing=json.loads(row['data']) if row else {}
        except Exception:existing={}
        if not isinstance(existing,dict):existing={}
        # FIFA sometimes submits only changed fields. Preserve omitted manager,
        # captain, kick-taker, formation, and player-slot state from the last save.
        saved=dict(existing);saved.update(doc)
        saved['id']=sid;saved['squadId']=sid;saved.setdefault('active',True);saved.setdefault('valid',True)
        if not isinstance(saved.get('players',[]),list):
            log.warning('LOCAL SAVE squad id=%s rejected after merge: invalid players',sid)
            return _active_squad(sid,query=query)
        if bool(saved.get('active',True)):
            for other in con.execute('SELECT id,data FROM squads WHERE id<>?',(sid,)).fetchall():
                try:other_doc=json.loads(other['data'])
                except Exception:continue
                if isinstance(other_doc,dict) and other_doc.get('active') is not False:
                    other_doc['active']=False
                    con.execute('UPDATE squads SET data=? WHERE id=?',(json.dumps(other_doc,separators=(',',':')),int(other['id'])))
            con.execute('INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)',('activeSquadId',str(sid)))
            saved['active']=True
        con.execute('INSERT OR REPLACE INTO squads(id,data) VALUES(?,?)',(sid,json.dumps(saved,separators=(',',':'))))
    log.warning('LOCAL SAVE squad id=%s name=%s formation=%s players=%s',sid,saved.get('squadName'),saved.get('formation'),len(saved.get('players',[]) or []))
    return _active_squad(sid,query=query)

def _active_squad(sid=None, query=None):
    if sid is None:sid=_squad_id(_meta_get('activeSquadId','1'),1)
    else:sid=_squad_id(sid,1)
    query = query or {}
    sku_mode = str((query.get('skuMode') or [''])[0] or '').upper()
    with _DB_LOCK, _db_connect() as con:
        row=con.execute('SELECT data FROM squads WHERE id=?',(int(sid),)).fetchone()
    if row:
        try:sq=json.loads(row['data'])
        except Exception:sq=_default_squad()
    else:sq=_default_squad()
    if not isinstance(sq,dict):sq=_default_squad()
    items=_db_item_map()
    players=[]
    for idx,row in enumerate(sq.get('players',[]) or []):
        if not isinstance(row,dict):continue
        r=dict(row);r.setdefault('index',idx)
        raw=r.get('itemData') if isinstance(r.get('itemData'),dict) else {}
        try:iid=int(raw.get('id',0) or 0)
        except Exception:iid=0
        if iid and iid in items:
            full=dict(items[iid]);full.update({k:v for k,v in raw.items() if k not in full or k in ('dream',)})
            if sku_mode == 'WC':
                full = _apply_world_cup_player_schema(full)
            else:
                full['contract']=max(99,int(full.get('contract',99) or 99))
                full['contracts']=max(99,int(full.get('contracts',99) or 99))
                full['loans']=0
            r['itemData']=full
        elif not iid:
            r.pop('itemData',None)
        players.append(r)
    while len(players)<23:players.append({'index':len(players),'loyaltyBonus':0,'kitNumber':0})
    sq['players']=players[:23]
    sq['id']=int(sq.get('id',1) or 1);sq['squadId']=sq['id'];sq.setdefault('personaId',FAKE_PERSONA)
    sq.setdefault('squadName','Local XI');sq.setdefault('name',sq.get('squadName','Local XI'));sq.setdefault('formation','f442')
    sq.setdefault('active',True);sq.setdefault('valid',True);sq.setdefault('squadType','REGULAR_SQUAD');sq['newSquad']=0;sq['newsquad']=0
    mgr_rows=[]
    for idx,row in enumerate(sq.get('manager',[]) or []):
        if not isinstance(row,dict):continue
        r=dict(row)
        raw=r.get('itemData') if isinstance(r.get('itemData'),dict) else {}
        try:iid=int(raw.get('id',r.get('id',0)) or 0)
        except Exception:iid=0
        if iid and iid in items:
            full=dict(items[iid]);full.update({k:v for k,v in raw.items() if k not in full})
            full['contract']=max(99,int(full.get('contract',99) or 99))
            full['contracts']=max(99,int(full.get('contracts',99) or 99))
            full['loans']=0
            if 'itemData' in r or iid in items:
                r['itemData']=full
        elif isinstance(raw,dict) and raw:
            raw['contract']=max(99,int(raw.get('contract',99) or 99))
            raw['contracts']=max(99,int(raw.get('contracts',99) or 99))
            raw['loans']=0
            r['itemData']=raw
        mgr_rows.append(r)
    sq['manager']=mgr_rows
    sq.setdefault('club',[]);sq.setdefault('kicktakers',[])
    # Retail userMassInfo carries the currently-selected club presentation items
    # on the active squad too.  Returning an empty squad.actives makes the native
    # first-club state machine believe kits/badge still need to be chosen even
    # when userInfo.actives and SQLite already contain them.
    sq['actives']=_active_selected_club_items()
    return sq

def _squad_list_payload():
    active_id=_squad_id(_meta_get('activeSquadId','1'),1)
    with _DB_LOCK, _db_connect() as con:
        rows=con.execute('SELECT id,data FROM squads ORDER BY id').fetchall()
    squads=[]
    for row in rows:
        try:sq=json.loads(row['data'])
        except Exception:continue
        if not isinstance(sq,dict):continue
        sid=_squad_id(row['id'],1)
        squads.append({
            'id':sid,'personaId':FAKE_PERSONA,'squadName':sq.get('squadName','Local XI'),
            'formation':sq.get('formation','f442'),'active':sid==active_id,'changed':False,
            'chemistry':int(sq.get('chemistry',0) or 0),'starRating':int(sq.get('starRating',5) or 5),
            'rating':int(sq.get('rating',5) or 5),'valid':bool(sq.get('valid',True)),'newsquad':0,
        })
    if not squads:
        sq=_active_squad(active_id)
        squads=[{'id':active_id,'personaId':FAKE_PERSONA,'squadName':sq.get('squadName','Local XI'),
                  'formation':sq.get('formation','f442'),'active':True,'changed':False,
                  'chemistry':int(sq.get('chemistry',0) or 0),'starRating':int(sq.get('starRating',5) or 5),
                  'rating':int(sq.get('rating',5) or 5),'valid':True,'newsquad':0}]
    return {'activeSquadId':active_id,'squad':squads}

def _normalise_completed_onboarding(doc):
    entries=[]
    if isinstance(doc,dict):
        entries=[dict(x) for x in (doc.get('entries') or []) if isinstance(x,dict)]
    found=False
    for x in entries:
        try:k=int(x.get('key',-1))
        except Exception:k=-1
        if k==0:
            x['key']=0;x['value']=2;found=True
    if not found:entries.insert(0,{'key':0,'value':2})
    return {'entries':entries}

def _load_persistent_globals():
    for key in ('homeKitId','awayKitId','badgeId'):
        try:ONBOARDING_SELECTION[key]=int(_meta_get(key,str(ONBOARDING_SELECTION[key])))
        except Exception:pass
    CLIENT_DATA['userHubData']=_client_get('userHubData',{'entries':[]})
    saved=_client_get('onboarding',{'entries':[{'key':0,'value':0}]})
    if str(_meta_get('onboardingComplete','0'))=='1':
        saved=_normalise_completed_onboarding(saved)
        _client_set('onboarding',saved)
    CLIENT_DATA['onboarding']=saved

from fut19_profile import install as _install_fifa19_profile
_install_fifa19_profile(globals())
_db_init()
_load_persistent_globals()


def _onboarding_kits_payload():
    home=[dict(x) for x in HOME_KIT_ITEMS]
    away=[dict(x) for x in AWAY_KIT_ITEMS]
    # homeItemDataList/awayItemDataList are the native response member names
    # recovered directly from FIFA 18 CardsDLL.  The extra aliases are harmless
    # compatibility fields consumed by frontend/view-model code in this family.
    return {
        'homeItemDataList':home,
        'awayItemDataList':away,
        'kitsHome':home,
        'kitsAway':away,
        'kits':home+away,
        'itemData':home+away,
        'homeKitId':int(ONBOARDING_SELECTION['homeKitId']),
        'awayKitId':int(ONBOARDING_SELECTION['awayKitId']),
        'count':len(home)+len(away),
        'success':True,
    }


def _find_kit(ref, *, home):
    try: ref=int(ref or 0)
    except Exception: ref=0
    pool=HOME_KIT_ITEMS if home else AWAY_KIT_ITEMS
    return next((dict(x) for x in pool if ref in (
        int(x.get('id',0)),int(x.get('resourceId',0)),int(x.get('definitionId',0)),
        int(x.get('teamid',0)),int(x.get('assetId',0))
    )), dict(pool[0]))


def _active_selected_club_items():
    out=[]
    if int(ONBOARDING_SELECTION.get('homeKitId',0)):
        h=_find_kit(ONBOARDING_SELECTION['homeKitId'],home=True)
        h['pile']=7; h['itemState']='activeHomeKit'; out.append(h)
    if int(ONBOARDING_SELECTION.get('awayKitId',0)):
        a=_find_kit(ONBOARDING_SELECTION['awayKitId'],home=False)
        a['pile']=7; a['itemState']='activeAwayKit'; out.append(a)
    if int(ONBOARDING_SELECTION.get('badgeId',0)):
        try: ref=int(ONBOARDING_SELECTION['badgeId'])
        except Exception: ref=0
        b=next((dict(x) for x in BADGE_ITEMS if ref in (int(x.get('id',0)),int(x.get('resourceId',0)),int(x.get('teamid',0)),int(x.get('assetId',0)))),dict(BADGE_ITEMS[0]))
        b['pile']=7; b['itemState']='activeBadge'; out.append(b)
    # Match loading needs a stadium even on old saves created before one was
    # seeded. Keep this deterministic and local; DB migration above makes it
    # visible in the club as well.
    stadium=dict(DEFAULT_STADIUM_ITEM);stadium['pile']=7;stadium['itemState']='activeStadium';out.append(stadium)
    return out


def _onboarding_badges_payload():
    rows=[dict(x) for x in BADGE_ITEMS]
    return {
        'badgeItemDataList':rows,
        'badges':rows,'itemData':rows,'items':rows,
        'badgeAssetId':int(ONBOARDING_SELECTION.get('badgeId',0)),
        'badgeDBid':int(ONBOARDING_SELECTION.get('badgeId',0)),
        'count':len(rows),'success':True,
    }

def _loan_players_payload():
    items=[dict(x) for x in LOAN_ITEMS]
    return {
        'loanPlayers':items,
        'itemData':items,
        'items':items,
        'count':len(items),
        'success':True,
    }

def _account_info():
    club={
        'year':'2019','platform':'pc','clubName':_club_name(),
        'established':_established(),'assetId':CLUB_ID,'clubAbbr':_club_abbr(),
    }
    persona={
        'personaId':FAKE_PERSONA,'personaName':PERSONA,
        'userClubList':[club],'returningUser':1,'hasClub':1,'onboardingComplete':1,'firstTime':False,
    }
    return {'userAccountInfo':{'nucleusId':FAKE_NUCLEUS,'personas':[persona],'returningUser':1,'hasClub':1,'onboardingComplete':1},'personas':[persona],'returningUser':1,'hasClub':1,'onboardingComplete':1}

def _mass_settings():
    # Small high-signal subset of FIFA 18 mass-info settings.  The important
    # onboarding flags are enableGrantLoaner/loanPlayerPurchaseFeatureEnable;
    # the rest keep the normal FUT navigation/store features available.
    vals=[
        ('enableGrantLoaner',1),('loanPlayerPurchaseFeatureEnable',1),
        ('tradingEnabled',1),('transferMarketEnabled',1),('squadBuildingChallengeEnabled',1),
        ('storeEnabled',1),('cardPackStoreEnabled',1),('pointsPackStoreEnabled',1),('coinEnabled',1),('fifaPointsEnabled',1),
        ('packOpeningAnimationEnabled',1),('packsAutoClaimed',0),
        ('transferListSize',100),('transferTargetListSize',50),('squadSlots',30),
        ('squadBattlesEnabled',1),('squadBattleEnabled',1),('enableSquadBattles',1),('squadBattleFeatureEnabled',1),
        ('singlePlayerSquadBattleEnabled',1),('squadBattleRefreshEnabled',1),
        ('enableDynamicObjectives',0),('phishingEnabled',0),
    ]
    return {'configs':[{'value':v,'type':k} for k,v in vals]}

def _user_mass_info(query=None):
    # FIFA 18's native mass-info response is a multi-object bootstrap, not just
    # userInfo.  In particular the *top-level* `squad` carries the 23 slot
    # ItemData list used by the onboarding squad screen.  v0.7.8 omitted it,
    # which is why the pitch rendered 11 placeholders even though /squad itself
    # could return players.
    query = query or {}
    squad=_active_squad(query=query)
    squad['newSquad']=0;squad['newsquad']=0
    owned_count=len(_owned_players())
    pending=[dict(x) for x in _pending_items()]
    summary={k:v for k,v in squad.items() if k not in ('players','manager','actives','club')}
    summary.update({'players':[],'manager':[],'actives':[]})
    record_won,record_draw,record_loss=_record_triplet()
    user_info={
        'personaId':FAKE_PERSONA,
        'clubId':CLUB_ID,'clubName':_club_name(),'clubAbbr':_club_abbr(),
        'won':record_won,'draw':record_draw,'loss':record_loss,
        'gamesWon':record_won,'gamesDraw':record_draw,'gamesLost':record_loss,'gamesPlayed':record_won+record_draw+record_loss,
        'record':{'won':record_won,'draw':record_draw,'loss':record_loss},
        'credits':_credits(),'bidTokens':{},
        'currencies':[
            {'name':'COINS','funds':_credits(),'finalFunds':_credits()},
            {'name':'POINTS','funds':0,'finalFunds':0},
            {'name':'DRAFT_TOKEN','funds':0,'finalFunds':0},
        ],
        'trophies':0,'actives':_active_selected_club_items(),
        'established':str(_established()),'divisionOffline':10,'divisionOnline':10,
        'personaName':PERSONA,
        'squadList':{'squad':[summary],'activeSquadId':int(squad.get('id',1) or 1)},
        'unopenedPacks':_unopened_packs_payload(),
        'purchased':True,'returningUser':1,'hasClub':True,'onboardingComplete':1,'firstTime':False,'firstTimeFlag':0,
        'ownedPlayers':owned_count,'playerCount':owned_count,'players':owned_count,'playersEmployed':owned_count,'clubPlayers':owned_count,'totalPlayers':owned_count,'numPlayers':owned_count,
        'FUT_MYCLUB_PLAYERS_EMPLOYED':owned_count,'clubItemCount':len([x for x in _db_items() if int(x.get('pile',7) or 0)==7]),
        'reliability':{'reliability':100,'startedMatches':0,'finishedMatches':0,'matchUnfinishedTime':0},
        'seasonTicket':False,'accountCreatedPlatformName':'PC',
        'fifaPointsFromLastYear':0,'fifaPointsTransferredStatus':0,
        'unassignedPileSize':len(pending),'feature':{'trade':2,'squadBattle':2,'squadBattles':2},
        'squadBattlesEnabled':True,'squadBattleEnabled':True,'squadBattlePoints':_sb_rank()['battlePoints'],
        'liveTransfers':_live_transfer_count(),'liveTransferCount':_live_transfer_count(),
        'transferMarketCount':_live_transfer_count(),'auctionCount':_live_transfer_count(),
        'sessionCoinsBankBalance':_credits(),
    }
    pile={'entries':[
        {'key':0,'value':100}, {'key':1,'value':50}, {'key':2,'value':100},
    ]}
    loan_client={'entries':[
        {'key':i,'value':int(x['assetId'])} for i,x in enumerate(LOAN_ITEMS)
    ]}
    out={
        'errors':{},
        'settings':_mass_settings(),
        'userInfo':user_info,
        # The unfinished unassigned pile is authoritative across restarts, matching
        # the same purchased-items parser used by the pack-open transaction.
        'purchasedItems':_purchased_payload(query=query),
        'pileSizeClientData':pile,
        'loanPlayerClientData':loan_client,
        # FIFA persists the first-club flow with PUT /clientdata/onboarding and
        # expects that same object back inside userMassInfo on the next login.
        # Without this field the client assumes onboarding state is unknown and
        # asks for kits/badges again even though our SQLite meta says complete.
        'onboardingClientData':_client_get('onboarding',{'entries':[{'key':0,'value':0}]}),
        'squad':squad,
        'clubUser':_club_user_payload(),
        'clubInfo':_club_user_payload().get('clubInfo',{}),
        'activeMessages':{'activeMessage':[]},
        # Compatibility aliases are harmless to the native JSON reader and are
        # useful to later web-style callers.
        'credits':_credits(),
        'returningUser':1,'hasClub':True,'onboardingComplete':1,'firstTime':False,
        'ownedPlayers':owned_count,'playerCount':owned_count,'players':owned_count,'clubCount':_club_counts()['total'],'clubItemCount':_club_counts()['total'],'unassignedPileSize':len(pending),
        'liveTransfers':_live_transfer_count(),'liveTransferCount':_live_transfer_count(),
        'transferMarketCount':_live_transfer_count(),'auctionCount':_live_transfer_count(),
        'isHighTierReturningUser':True,
    }
    log.warning('USERMASSINFO ONBOARDING persisted=%s selection=%s/%s/%s',
                out.get('onboardingClientData'),ONBOARDING_SELECTION.get('homeKitId'),
                ONBOARDING_SELECTION.get('awayKitId'),ONBOARDING_SELECTION.get('badgeId'))
    return out

def _user_payload():
    record_won,record_draw,record_loss=_record_triplet()
    squad=_active_squad()
    return {
        'personaId':FAKE_PERSONA,'nucleusId':FAKE_NUCLEUS,
        'personaName':PERSONA,'displayName':PERSONA,
        'clubId':CLUB_ID,'clubName':_club_name(),'clubAbbr':_club_abbr(),
        'established':_established(),'credits':_credits(),'coins':_credits(),
        'divisionOffline':10,'divisionOnline':10,
        'won':record_won,'draw':record_draw,'loss':record_loss,
        'gamesWon':record_won,'gamesDraw':record_draw,'gamesLost':record_loss,'gamesPlayed':record_won+record_draw+record_loss,
        'record':{'won':record_won,'draw':record_draw,'loss':record_loss},
        'returningUser':1,'hasClub':1,'onboardingComplete':1,'firstTime':False,'purchased':True,'clubNameChangeAllowed':0,
        'ownedPlayers':len(_owned_players()),'playerCount':len(_owned_players()),'players':len(_owned_players()),'clubCount':_club_counts()['total'],'clubItemCount':_club_counts()['total'],
        'bidTokens':{'count':0,'updateTime':0},
        'squadList':[{'id':int(squad.get('id',1) or 1),'squadName':squad.get('squadName','Local XI'),
                      'formation':squad.get('formation','f442'),'active':True}],
    }

def _user_refresh_payload(query=None):
    """Post-match FUT user refresh using the already-proven mass-info shape.

    v0.8.9.17-v0.8.9.20 merged two different native JSON contracts into one
    50+ key object. FIFA accepted enough of it to return from the match, but a
    fresh Draft elimination then stalled before the prize claim request and the
    top-banner W-D-L stayed cached. The same client parses _user_mass_info()
    cleanly every login, so reuse that exact structure here and add only the
    harmless top-level record aliases.
    """
    out=_user_mass_info(query=query)
    rw,rd,rl=_record_triplet();credits=_credits()
    ui=out.get('userInfo') if isinstance(out.get('userInfo'),dict) else {}
    ui['credits']=credits;ui['sessionCoinsBankBalance']=credits
    ui['won']=rw;ui['draw']=rd;ui['loss']=rl
    ui['gamesWon']=rw;ui['gamesDraw']=rd;ui['gamesLost']=rl;ui['gamesPlayed']=rw+rd+rl
    ui['record']={'won':rw,'draw':rd,'loss':rl}
    ui['unopenedPacks']=_unopened_packs_payload()
    out['userInfo']=ui
    out['credits']=credits;out['coins']=credits;out['sessionCoinsBankBalance']=credits
    out['won']=rw;out['draw']=rd;out['loss']=rl
    out['gamesWon']=rw;out['gamesDraw']=rd;out['gamesLost']=rl;out['gamesPlayed']=rw+rd+rl
    out['record']={'won':rw,'draw':rd,'loss':rl}
    out['unopenedPacks']=_unopened_packs_payload()
    st=_draft_get_state();stage=_draft_stage(st)
    claim=bool((bool(st.get('completed')) or stage=='READY_FOR_REWARDS') and not bool(st.get('prizeClaimed')) and not bool(st.get('awardConsumed')))
    # Re-emit UserSessions refresh after the HTTP state has been rebuilt so the
    # FUT chrome sees the new W-D-L/wallet on the next Blaze tick.
    PENDING_USERSESSION_REFRESH.set()
    log.warning('POSTMATCH USER REFRESH MASS-CONTRACT draftStage=%s claim=%s record=%d-%d-%d credits=%d unopened=%d topKeys=%d',
                stage,claim,rw,rd,rl,credits,_unopened_packs_payload().get('count',0),len(out))
    return out


def _store_pack(pid,asset,name,desc,price,points,group,priority,gold,silver,bronze,rare,qty=12):
    typ=group.upper()
    # FIFA's store renderer resolves pack art from the built-in tier asset id, not
    # from our local purchase id.  v0.8.9.9 used 27/30/31/... as assetId for
    # promo packs, which have no installed art and therefore rendered blank.
    art_asset={'bronze':1,'silver':2,'gold':3,'promo':4}.get(str(group).lower(),int(asset))
    return {
        'id':int(pid),'assetId':int(art_asset),'packAssetId':int(art_asset),'packImageId':int(art_asset),'quantity':0,
        'name':name,'packName':name,'displayName':name,'title':name,'description':name,
        'packDescription':desc,'longDescription':desc,'shortDescription':desc,'bio':desc,
        'label':name,'displayLabel':name,'storePackName':name,
        'finalPrice':int(price),'originalPrice':int(price),
        'currencies':[{'name':'coins','funds':int(price),'finalFunds':int(price)},
                      {'name':'points','funds':int(points),'finalFunds':int(points)}],
        'displayGroup':{'value':('special' if group=='promo' else group),'priority':int(priority)},
        'category':group,'categoryId':{'bronze':1,'silver':2,'gold':3,'promo':4}.get(group,3),
        'state':'active',
        # Retail FUT store models distinguish the colour/tier from the purchase
        # type.  The native game already renders these packs, so preserve the
        # legacy tier alias while also supplying the canonical CARDPACK type.
        'type':'CARDPACK','packType':'CARDPACK','tier':typ,'packTier':typ,
        'saleType':'NONE','dealType':('PROMO' if group=='promo' else 'REGULAR'),'sortPriority':int(priority),
        'packContentInfo':{'itemQuantity':int(qty),'goldQuantity':int(gold),'silverQuantity':int(silver),'bronzeQuantity':int(bronze),'rareQuantity':int(rare),'contentType':'all'},
        'visible':True,'isPurchaseable':True,'unopened':False,'untradeable':False,
    }

STORE_PACKS=[
    _store_pack(1,1,'Bronze Pack','A mix of 12 Bronze items with 1 Rare.',400,0,'bronze',10,0,0,10,1),
    _store_pack(101,1,'Premium Bronze Pack','A mix of 12 Bronze items with 3 Rares.',750,0,'bronze',20,0,0,10,3),
    _store_pack(2,2,'Silver Pack','A mix of 12 Silver items with 1 Rare.',2500,50,'silver',10,0,10,0,1),
    _store_pack(102,2,'Premium Silver Pack','A mix of 12 Silver items with 3 Rares.',3750,75,'silver',20,0,10,0,3),
    _store_pack(3,3,'Gold Pack','A mix of 12 Gold items with 1 Rare. Authentic ~2% chance for specials.',5000,100,'gold',10,10,0,0,1),
    _store_pack(103,3,'Premium Gold Pack','A mix of 12 Gold items with 3 Rares. Authentic ~3% chance for specials.',7500,150,'gold',20,10,0,0,3),
    _store_pack(210,210,'Grand Festival All-Stars Pack','40 Gold Rare players. The ultimate multi-event showcase combining FoF, TOTS, TOTY, Prime Icons, and TOTW with our highest authentic odds.',200000,4000,'promo',1,40,0,0,40,40),
    _store_pack(201,201,'Festival of FUTball Players Pack','12 Gold Rare players. Boosted chance for World Cup FoF items (~36%), mixed with TOTS, TOTW, and Icons.',45000,900,'promo',2,12,0,0,12,12),
    _store_pack(202,202,'Jumbo Festival of FUTball Players Pack','24 Gold Rare players. Massive chance for World Cup FoF items (~46%), mixed with TOTS, TOTW, TOTY, and Icons.',75000,1500,'promo',3,24,0,0,24,24),
    _store_pack(203,203,'TOTS Lightning Pack','12 Gold Rare players. Boosted chance for Team of the Season items (~35%), mixed with FoF, TOTW, and Icons.',50000,1000,'promo',4,12,0,0,12,12),
    _store_pack(204,204,'TOTS Jumbo Rare Players Pack','24 Gold Rare players. Massive chance for Team of the Season items (~47%), mixed with FoF, TOTW, TOTY, and Icons.',100000,2000,'promo',5,24,0,0,24,24),
    _store_pack(205,205,'TOTW In-Form 86+ Pack','12 Gold Rare players. Boosted chance for Team of the Week 86+ In-Forms (~30%), mixed with TOTS, FoF, and Icons.',35000,700,'promo',6,12,0,0,12,12),
    _store_pack(206,206,'OTW & Halloween Special Pack','12 Gold Rare players. Boosted chance for Ones to Watch and Ultimate Scream items (~32%), mixed with other specials.',45000,900,'promo',7,12,0,0,12,12),
    _store_pack(207,207,'Prime ICON Edition Pack','24 Gold Rare players. Highest probability for Prime ICONs (88-98 OVR, ~17% icon chance), mixed with TOTS, FoF, and TOTW.',100000,2000,'promo',8,24,0,0,24,24),
    _store_pack(208,208,'TOTY Lightning Round Pack','30 Gold Rare players. Highest probability for Team of the Year items (94-99 OVR, ~48% special chance), mixed with Icons and TOTS.',125000,2500,'promo',9,30,0,0,30,30),
    _store_pack(27,27,'Jumbo Premium Gold Pack','A mix of 24 Gold items with 7 Rares. Popular entry promo pack with ~9% special chance.',15000,300,'promo',27,24,0,0,7,24),
    _store_pack(28,28,'Rare Gold Pack','A mix of 12 Gold items, all Rare. Compact rare gold pack.',25000,500,'promo',28,12,0,0,12,12),
    _store_pack(29,29,'Premium Gold Players Pack','12 Gold player items with 3 Rares. Standard gold player hunting pack.',25000,600,'promo',29,12,0,0,3,12),
    _store_pack(30,30,'Mega Pack','A mix of 30 Gold items with 18 Rares. High probability of boards and walkouts (~72%).',35000,700,'promo',30,30,0,0,18,30),
    _store_pack(31,31,'Rare Players Pack','12 Gold Rare player items. The classic 50k pack for hunting gold walkouts and specials (~16% special chance).',50000,1000,'promo',31,12,0,0,12,12),
    _store_pack(32,32,'Jumbo Rare Players Pack','24 Gold Rare player items, all Rare. Very high chance for walkouts (~68%) and mixed specials (~26%).',100000,2000,'promo',32,24,0,0,24,24),
    _store_pack(34,34,'Ultimate Pack','30 Gold Rare player items. The biggest pack in FUT with highest authentic chance for walkouts (~78%) and specials (~33%).',125000,2500,'promo',34,30,0,0,30,30),
    _store_pack(35,35,'Rare Mega Pack','A mix of 30 Gold items, all Rare. Premium all-rare pack with high board and walkout odds.',55000,1100,'promo',35,30,0,0,30,30),
    _store_pack(36,36,'Prime Gold Players Pack','12 Gold player items with 6 Rares. Excellent value player pack.',45000,600,'promo',36,12,0,0,6,12),
    _store_pack(37,37,'Jumbo Premium Gold Players Pack','24 Gold player items with 7 Rares. High volume player pack.',50000,700,'promo',37,24,0,0,7,24),
    _store_pack(38,38,'Small Rare Gold Players Pack','6 Gold Rare players. Compact all-rare player pack.',25000,500,'promo',38,6,0,0,6,6),
    _store_pack(39,39,'Rare Electrum Players Pack','12 player items (6 Gold, 6 Silver) with 12 Rares.',30000,600,'promo',39,12,0,0,12,12),
    _store_pack(40,40,'Prime Electrum Players Pack','12 player items (6 Gold, 6 Silver) with 6 Rares.',20000,400,'promo',40,12,0,0,6,12),
    _store_pack(41,41,'Premium Mixed Players Pack','12 player items (4 Bronze, 4 Silver, 4 Gold) with 3 Rares.',15000,300,'promo',41,12,0,0,3,12),
    _store_pack(42,42,'Rare Mixed Players Pack','12 player items (4 Bronze, 4 Silver, 4 Gold), all Rare.',25000,500,'promo',42,12,0,0,12,12),
    _store_pack(43,43,'Small Prime Gold Players Pack','6 Gold players with 3 Rares.',10000,200,'promo',43,6,0,0,3,6),
    _store_pack(44,44,'Jumbo Gold Pack','A mix of 24 Gold items with 3 Rares.',10000,200,'promo',44,24,0,0,3,24),
    _store_pack(45,45,'Gold Players Pack','12 Gold player items with 1 Rare.',12500,250,'promo',45,12,0,0,1,12),
]
STORE_CATEGORIES=[
    {'id':1,'categoryId':1,'name':'Bronze Packs','groupName':'Bronze Packs','value':'bronze','displayGroup':'bronze','sortPriority':1,'visible':True},
    {'id':2,'categoryId':2,'name':'Silver Packs','groupName':'Silver Packs','value':'silver','displayGroup':'silver','sortPriority':2,'visible':True},
    {'id':3,'categoryId':3,'name':'Gold Packs','groupName':'Gold Packs','value':'gold','displayGroup':'gold','sortPriority':3,'visible':True},
    {'id':4,'categoryId':4,'name':'Promo Packs','groupName':'Promo Packs','value':'special','displayGroup':'special','sortPriority':4,'visible':True},
    {'id':5,'categoryId':5,'name':'My Packs','groupName':'My Packs','value':'mypacks','displayGroup':'mypacks','sortPriority':5,'visible':True,
     'description':'Packs you own and have not opened yet.'},
]


def _pack_by_id(pack_id):
    try:pid=int(pack_id)
    except Exception:pid=0
    return next((p for p in STORE_PACKS if int(p.get('id',0))==pid),None)

def _owned_pack_counts():
    """Persistent unopened-pack inventory used by Draft rewards.

    FIFA's store has a My Packs path driven by pack quantities.  Earlier builds
    materialised Draft rewards straight into Unassigned, so the prize existed
    but no unopened pack ever appeared in the store.  Keep a tiny persistent
    pack-id -> quantity map instead and let the normal pack-opening endpoint
    consume it without charging coins.
    """
    try:raw=json.loads(_meta_get('unopenedRewardPacks','{}') or '{}')
    except Exception:raw={}
    if not isinstance(raw,dict):raw={}
    out={}
    for k,v in raw.items():
        try:pid=int(k);qty=max(0,int(v or 0))
        except Exception:continue
        if pid>0 and qty>0:out[pid]=qty
    return out

def _save_owned_pack_counts(counts):
    clean={str(int(k)):max(0,int(v)) for k,v in dict(counts or {}).items() if int(k)>0 and int(v)>0}
    _meta_set('unopenedRewardPacks',json.dumps(clean,separators=(',',':')))
    return {int(k):int(v) for k,v in clean.items()}

def _grant_owned_pack(pack_id,qty=1):
    pid=int(pack_id or 0);qty=max(0,int(qty or 0))
    if pid<=0 or qty<=0 or not _pack_by_id(pid):return 0
    counts=_owned_pack_counts();counts[pid]=counts.get(pid,0)+qty;_save_owned_pack_counts(counts)
    log.warning('UNOPENED PACK GRANT packId=%d qty=+%d total=%d',pid,qty,counts[pid])
    return counts[pid]

def _consume_owned_pack(pack_id):
    pid=int(pack_id or 0);counts=_owned_pack_counts();cur=counts.get(pid,0)
    if cur<=0:return False
    if cur<=1:counts.pop(pid,None)
    else:counts[pid]=cur-1
    _save_owned_pack_counts(counts)
    log.warning('UNOPENED PACK CONSUME packId=%d remaining=%d',pid,max(0,cur-1))
    return True

def _unopened_packs_payload():
    counts=_owned_pack_counts();rows=[]
    for pid,qty in sorted(counts.items()):
        p=_pack_by_id(pid)
        if not p:continue
        art=int(p.get('packAssetId',p.get('assetId',pid)) or pid)
        rows.append({'id':pid,'packId':pid,'assetId':art,'packAssetId':art,'packImageId':art,
                     'name':p.get('name','Pack'),'packName':p.get('name','Pack'),
                     'quantity':qty,'count':qty,'ownedQuantity':qty,'unopened':True,'isOwned':True,
                     'recovered':True,'isRecoveredPack':True,'packSource':'DRAFT_REWARD'})
    total=sum(int(x.get('quantity',0) or 0) for x in rows)
    # The historical FUT credits model uses only the two numeric counters, while
    # nearby store readers accept richer aliases. Keep both so My Packs can resolve
    # the entitlement without breaking clients that only deserialize the counters.
    return {'preOrderPacks':0,'recoveredPacks':total,'count':total,'total':total,
            'packs':rows,'pack':rows,'items':rows,'myPacks':rows,'ownedPacks':rows}

def _draft_pack_award_detail(pack_id,hal_id=0):
    pid=int(pack_id or 0);p=_pack_by_id(pid)
    if not p:return {}
    art=int(p.get('packAssetId',p.get('assetId',pid)) or pid)
    name=str(p.get('name','Pack') or 'Pack')
    # Mirrors the rich pack-award vocabulary embedded in Aurora17.Server.dll.
    # Keep the native awardedPrizes row itself compact, but publish this detail
    # beside it so FIFA can resolve pack art and the store entitlement.
    return {
        'type':'pack','awardType':'pack','value':pid,'count':1,
        'isPack':True,'isItem':False,'isUntradeable':False,
        'halId':int(hal_id),'halid':int(hal_id),'id':pid,'packId':pid,
        'packAssetId':art,'packAssetID':str(art),'packImageId':art,
        'packs':str(pid),'packsAmount':1,
        'AWD_PACKS_STRING':str(pid),'AWD_PACKS_AMOUNT':1,'AWD_PACKS_ASSET_IDS':str(art),
        'items':'','itemData':[],
        'IMAGE_UPDATE':True,'IMAGE':str(art),'imageUpdate':True,'image':str(art),
        'name':name,'packName':name,'title':name,
    }

def _draft_award_details(st=None):
    st=st if isinstance(st,dict) else _draft_get_state()
    if bool(st.get('prizeClaimed')) or bool(st.get('awardConsumed')):return []
    bundle=_draft_reward_bundle(int(st.get('wins',0) or 0),st)
    rows=[]
    for i,pid in enumerate(bundle.get('packs',[]) if isinstance(bundle.get('packs'),list) else []):
        d=_draft_pack_award_detail(pid,i)
        if d:rows.append(d)
    return rows

def _choose_pack_defs(pack,qty_override=None,sku_mode=None):
    """Authentic FIFA 18 live-server drop engine — per-card probability, mixed special pools, NO guarantees.

    Every card slot in the pack has an independent small chance of being upgraded
    to a special or icon card.  When a special hits, it is drawn from a weighted
    pool of ALL event types (FoF, TOTS, TOTW, Icons, TOTY, OTW, etc.).  Event
    Packs bias the weights toward their themed type but still allow any other
    special to appear — exactly like the real live server before shutdown.
    """
    info=pack.get('packContentInfo',{}) if isinstance(pack,dict) else {}
    qty=max(1,int(qty_override if qty_override is not None else (info.get('itemQuantity',12) or 12)))
    pid=int((pack or {}).get('id',0) or 0)
    price=int((pack or {}).get('finalPrice',0) or 0)
    group=str((pack or {}).get('displayGroup',{}).get('value',(pack or {}).get('category','gold'))).lower()

    # Base roster — clean non-special non-icon cards
    base=[x for x in _load_player_defs(False) if int(x.get('rating',0) or 0)>0 and not _is_special_def(x) and str(x.get('specialType','')).upper()!='ICON' and int(x.get('clubId',0) or 0)!=112658]
    if sku_mode == 'WC':
        base=[x for x in base if int(x.get('nation',x.get('nationId',0)) or 0) in WC_32_NATIONS]
    if group in ('gold','special','promo') or pid>=200:
        base=[x for x in base if int(x.get('rating',0) or 0)>=75]
    elif group=='silver':
        base=[x for x in base if 65<=int(x.get('rating',0) or 0)<=74]
    elif group=='bronze':
        base=[x for x in base if int(x.get('rating',0) or 0)<=64]
    if not base:
        base=list(_load_player_defs(False))
        if sku_mode == 'WC':
            base=[x for x in base if int(x.get('nation',x.get('nationId',0)) or 0) in WC_32_NATIONS] or list(_load_player_defs(False))

    # Build special card pools
    all_special=list(_special_player_defs())
    icon_pool=[d for d in all_special if str(d.get('specialType','')).upper()=='ICON']
    special_pool=[d for d in all_special if str(d.get('specialType','')).upper()!='ICON']
    fof_pool=[d for d in special_pool if str(d.get('specialType','')).upper()=='FOF']
    tots_pool=[d for d in special_pool if str(d.get('specialType','')).upper()=='TOTS']
    totw_pool=[d for d in special_pool if str(d.get('specialType','')).upper()=='TOTW']
    totw86_pool=[d for d in totw_pool if int(d.get('rating',0) or 0)>=86]
    otw_scream_pool=[d for d in special_pool if str(d.get('specialType','')).upper() in ('OTW','HALLOWEEN')]
    toty_pool=[d for d in special_pool if str(d.get('specialType','')).upper()=='TOTY']
    other_pool=[d for d in special_pool if str(d.get('specialType','')).upper() not in ('FOF','TOTS','TOTW','OTW','HALLOWEEN','TOTY')]
    # Fallbacks
    if not fof_pool:fof_pool=special_pool
    if not tots_pool:tots_pool=special_pool
    if not totw_pool:totw_pool=special_pool
    if not totw86_pool:totw86_pool=totw_pool or special_pool
    if not otw_scream_pool:otw_scream_pool=special_pool
    if not toty_pool:toty_pool=special_pool
    if not other_pool:other_pool=special_pool

    if sku_mode == 'WC':
        # In World Cup mode, only Festival of FUTball (from 32 WC nations) and World Cup Icons are supported.
        # Club TOTS/TOTW/TOTY/Scream and non-WC FoF cards lack 3D walkout shaders and do not belong in World Cup.
        fof_pool = [d for d in fof_pool if int(d.get('nation', d.get('nationId', 0)) or 0) in WC_32_NATIONS] or fof_pool
        icon_pool = _world_cup_icons()
        special_pool = fof_pool + icon_pool
        tots_pool = fof_pool
        totw_pool = fof_pool
        totw86_pool = fof_pool
        otw_scream_pool = fof_pool
        toty_pool = fof_pool
        other_pool = fof_pool

    # ---- Per-card drop rates & weighted special pool ----
    # icon_pct  = per-card chance of pulling an ICON
    # spec_pct  = per-card chance of pulling a non-ICON special
    # max_sp    = hard cap on total specials+icons per pack
    # pool_wt   = list of (pool, weight) for weighted type selection
    icon_pct=0.0; spec_pct=0.0; max_sp=2 if qty>=24 else 1

    # Default mixed pool: proportional to actual pool sizes (natural distribution)
    if sku_mode == 'WC':
        pool_wt=[(fof_pool,100)]
    else:
        pool_wt=[(fof_pool,len(fof_pool)),(tots_pool,len(tots_pool)),(totw_pool,len(totw_pool)),
                 (otw_scream_pool,len(otw_scream_pool)),(toty_pool,len(toty_pool)),(other_pool,len(other_pool))]

    # --- Event Packs (201-210): higher chances, biased toward theme or all-stars mixed ---
    if pid==210:    # Grand Festival All-Stars Pack (200k, 40 cards) - Multi-Event Showcase
        icon_pct=0.0015; spec_pct=0.022; max_sp=4
        pool_wt=[(fof_pool,25),(tots_pool,25),(totw86_pool,18),(toty_pool,12),(otw_scream_pool,10),(other_pool,10)]
    elif pid==201:  # Festival of FUTball Pack (45k, 12 cards)
        icon_pct=0.0010; spec_pct=0.035; max_sp=2
        pool_wt=[(fof_pool,60),(tots_pool,10),(totw_pool,10),(otw_scream_pool,7),(toty_pool,3),(other_pool,10)]
    elif pid==202:  # Jumbo Festival of FUTball Pack (75k, 24 cards)
        icon_pct=0.0008; spec_pct=0.025; max_sp=3
        pool_wt=[(fof_pool,55),(tots_pool,12),(totw_pool,10),(otw_scream_pool,8),(toty_pool,5),(other_pool,10)]
    elif pid==203:  # TOTS Lightning Pack (50k, 12 cards)
        icon_pct=0.0010; spec_pct=0.035; max_sp=2
        pool_wt=[(tots_pool,60),(fof_pool,10),(totw_pool,10),(otw_scream_pool,7),(toty_pool,3),(other_pool,10)]
    elif pid==204:  # TOTS Jumbo Rare Players Pack (100k, 24 cards)
        icon_pct=0.0008; spec_pct=0.025; max_sp=3
        pool_wt=[(tots_pool,50),(fof_pool,13),(totw_pool,12),(otw_scream_pool,8),(toty_pool,7),(other_pool,10)]
    elif pid==205:  # TOTW In-Form 86+ Pack (35k, 12 cards)
        icon_pct=0.0008; spec_pct=0.030; max_sp=2
        pool_wt=[(totw86_pool,60),(fof_pool,10),(tots_pool,10),(otw_scream_pool,7),(toty_pool,3),(other_pool,10)]
    elif pid==206:  # OTW & Halloween Special Pack (45k, 12 cards)
        icon_pct=0.0008; spec_pct=0.030; max_sp=2
        pool_wt=[(otw_scream_pool,55),(fof_pool,12),(tots_pool,12),(totw_pool,8),(toty_pool,3),(other_pool,10)]
    elif pid==207:  # Prime ICON Edition Pack (100k, 24 cards)
        icon_pct=0.0080; spec_pct=0.015; max_sp=3
        pool_wt=[(fof_pool,20),(tots_pool,20),(totw_pool,18),(otw_scream_pool,15),(toty_pool,12),(other_pool,15)]
    elif pid==208:  # TOTY Lightning Round Pack (125k, 30 cards)
        icon_pct=0.0010; spec_pct=0.020; max_sp=3
        pool_wt=[(toty_pool,40),(fof_pool,15),(tots_pool,15),(totw_pool,10),(otw_scream_pool,8),(other_pool,12)]

    # --- Regular Promo & Retail Packs: price-based tiers, fully mixed pool ---
    elif pid==34:   # Ultimate Pack (125k, 30 cards)
        icon_pct=0.0007; spec_pct=0.013; max_sp=3
    elif pid==32:   # Jumbo Rare Players Pack (100k, 24 cards)
        icon_pct=0.00065; spec_pct=0.012; max_sp=2
    elif pid in (35,31):  # Rare Mega (55k) / Rare Players (50k)
        icon_pct=0.0006; spec_pct=0.013; max_sp=2
    elif pid in (36,37,30):  # Prime Gold Players (45k), Jumbo Prem Gold Players (50k), Mega (35k)
        icon_pct=0.0003; spec_pct=0.005; max_sp=2 if qty>=24 else 1
    elif pid in (28,29,38,39,42):  # 25k–30k range
        icon_pct=0.0002; spec_pct=0.0035; max_sp=1
    elif group in ('special','promo'):  # other promos (10k–20k)
        icon_pct=0.00015; spec_pct=0.003; max_sp=1
    elif group=='gold':
        icon_pct=0.00005 if price<=5000 else 0.00008
        spec_pct=0.0015 if price<=5000 else 0.0025
        max_sp=1
    elif group=='silver':
        spec_pct=0.0008; max_sp=1
    elif group=='bronze':
        spec_pct=0.0003; max_sp=1

    if sku_mode == 'WC':
        pool_wt=[(fof_pool,100)]

    # ---- Pick base cards ----
    qty_actual=min(qty,len(base))
    picked=random.sample(base,qty_actual) if len(base)>=qty_actual else [random.choice(base) for _ in range(qty_actual)]

    # ---- Weighted pool picker ----
    used_rids={int(x.get('resourceId',x.get('definitionId',x.get('assetId',0))) or 0) for x in picked}

    def _pick_unique(pool):
        avail=[c for c in pool if int(c.get('resourceId',c.get('definitionId',c.get('assetId',0))) or 0) not in used_rids]
        card=random.choice(avail if avail else pool)
        used_rids.add(int(card.get('resourceId',card.get('definitionId',card.get('assetId',0))) or 0))
        return dict(card)

    def _pick_weighted():
        total=sum(w for _,w in pool_wt if w>0)
        if total<=0: return _pick_unique(special_pool) if special_pool else dict(random.choice(base))
        r=random.random()*total; cum=0
        for pool,weight in pool_wt:
            if weight<=0 or not pool: continue
            cum+=weight
            if r<=cum: return _pick_unique(pool)
        return _pick_unique(special_pool) if special_pool else dict(random.choice(base))

    # ---- Per-card probability rolls — NO guarantees ----
    specials_placed=0
    for i in range(len(picked)):
        if specials_placed>=max_sp: break
        # Icon roll first (rarer)
        if icon_pool and icon_pct>0 and random.random()<icon_pct:
            picked[i]=_pick_unique(icon_pool); specials_placed+=1; continue
        # Special roll (weighted mixed pool)
        if spec_pct>0 and random.random()<spec_pct:
            picked[i]=_pick_weighted(); specials_placed+=1

    random.shuffle(picked)
    return picked


def _pack_is_player_only(pack):
    pid=int((pack or {}).get('id',0) or 0)
    if pid>=200:
        return True
    name=str((pack or {}).get('name','') or '').lower()
    return ('players pack' in name) or ('ultimate pack' in name)


def _pack_tier(pack):
    group=str((pack or {}).get('displayGroup',{}).get('value',(pack or {}).get('category','gold'))).lower()
    return group if group in ('bronze','silver','gold') else 'gold'


def _pack_consumable_defs(tier,qty):
    pool=[dict(x) for x in consumable_definitions() if str(x.get('level','')).lower()==str(tier).lower()]
    if not pool:pool=[dict(x) for x in consumable_definitions()]
    if not pool:return []
    return random.sample(pool,min(int(qty),len(pool))) if len(pool)>=int(qty) else [random.choice(pool) for _ in range(int(qty))]


def _pack_manager_defs(tier,qty):
    if int(qty)<=0:return []
    rows=[dict(x) for x in _manager_defs(False)]
    if not rows:return []
    # The preserved manager catalogue is mainly gold. For lower-tier packs use
    # the same identity but lower the presentation quality on the transient item.
    out=[]
    for d in random.sample(rows,min(int(qty),len(rows))) if len(rows)>=int(qty) else [random.choice(rows) for _ in range(int(qty))]:
        d=dict(d);d['_packTier']=tier
        if tier=='bronze':d['rating']=60;d['rareflag']=0;d['rareFlag']=0;d['quality']='Bronze'
        elif tier=='silver':d['rating']=70;d['rareflag']=0;d['rareFlag']=0;d['quality']='Silver'
        out.append(d)
    return out


def _pack_club_defs(tier,qty):
    templates=[]
    for x in list(HOME_KIT_ITEMS)+list(AWAY_KIT_ITEMS)+list(BADGE_ITEMS)+[DEFAULT_STADIUM_ITEM]:
        d=dict(x);d['_packClubTemplate']=True;d['_packTier']=tier
        d['itemState']='free';d['pile']=6;d['untradeable']=False;d['tradeable']=True
        if tier=='bronze':d['rating']=60;d['rareflag']=0;d['rareFlag']=0
        elif tier=='silver':d['rating']=70;d['rareflag']=0;d['rareFlag']=0
        else:d['rating']=max(75,int(d.get('rating',75) or 75))
        templates.append(d)
    if not templates:return []
    return random.sample(templates,min(int(qty),len(templates))) if len(templates)>=int(qty) else [dict(random.choice(templates)) for _ in range(int(qty))]


def _choose_pack_contents(pack,sku_mode=None):
    """Restore normal FUT mixed packs while retaining player-only promo packs."""
    info=(pack or {}).get('packContentInfo',{}) if isinstance(pack,dict) else {}
    qty=max(1,int(info.get('itemQuantity',12) or 12))
    if sku_mode == 'WC' or _pack_is_player_only(pack):return _choose_pack_defs(pack,qty,sku_mode=sku_mode)
    tier=_pack_tier(pack)
    if qty>=30:players,managers,clubs=9,2,5
    elif qty>=24:players,managers,clubs=7,1,4
    else:players,managers,clubs=3,1,2
    players=min(players,qty);managers=min(managers,max(0,qty-players));clubs=min(clubs,max(0,qty-players-managers))
    consumables=max(0,qty-players-managers-clubs)
    rows=[]
    rows.extend(_choose_pack_defs(pack,players,sku_mode=sku_mode))
    rows.extend(_pack_manager_defs(tier,managers))
    rows.extend(_pack_consumable_defs(tier,consumables))
    rows.extend(_pack_club_defs(tier,clubs))
    random.shuffle(rows)
    return rows[:qty]


def _pack_def_is_player(defn):
    d=defn if isinstance(defn,dict) else {}
    return str(d.get('itemType','')).lower()=='player' or (int(d.get('assetId',0) or 0)>0 and bool(d.get('position') or d.get('preferredPosition')))


def _pack_make_item(defn,item_id,rare_override=None):
    d=dict(defn or {});typ=str(d.get('itemType','')).lower()
    is_player=_pack_def_is_player(d)
    if is_player:
        return _definition_item(d,pile=6,rare=rare_override,item_id=item_id)
    if d.get('_packClubTemplate'):
        item={k:v for k,v in d.items() if not str(k).startswith('_pack')}
        item['id']=int(item_id);item['itemId']=int(item_id);item['pile']=6;item['itemState']='free'
        item['owners']=1;item['untradeable']=False;item['tradeable']=True;item['timestamp']=int(time.time())
    else:
        item=_catalog_instance(d,item_id);item['pile']=6;item['itemState']='free';item['untradeable']=False;item['tradeable']=True
    if rare_override is not None:
        item['rareflag']=1 if bool(rare_override) else 0;item['rareFlag']=item['rareflag']
    if int(item.get('discardValue',0) or 0)<=0:
        tier=str(d.get('_packTier',d.get('level',_pack_tier({'category':'gold'}))) or '').lower()
        item['discardValue']=300 if tier=='gold' else 150 if tier=='silver' else 50
    return item


def _pack_wire_item(item, sku_mode=None):
    if str((item or {}).get('itemType','')).lower()=='player':return _pack_wire_player(item, sku_mode=sku_mode)
    return {k:v for k,v in dict(item or {}).items() if not str(k).startswith('_pack')}

def _publish_walkout_request(headline, expected_walkout, walkout_type, pack_items=None):
    """Publish one short-lived native presentation request for the runtime helper."""
    h=headline if isinstance(headline,dict) else {}
    now_ms=int(time.time()*1000)
    req={
        'version':2,'createdUnixMs':now_ms,'expiresUnixMs':now_ms+30000,
        'expectedWalkout':bool(expected_walkout),'walkoutType':int(walkout_type or 0),
        'itemId':int(h.get('id',h.get('itemId',0)) or 0),
        'assetId':int(h.get('assetId',0) or 0),
        'resourceId':int(h.get('resourceId',h.get('definitionId',0)) or 0),
        'overall':int(h.get('rating',0) or 0),
        'teamId':int(h.get('teamId',h.get('teamid',0)) or 0),
        'nationId':int(h.get('nation',h.get('nationId',0)) or 0),
        'rarity':int(h.get('rareflag',h.get('rareFlag',0)) or 0),
        'discardValue':int(h.get('discardValue',0) or 0),
        'verifiedFifa18':bool(h.get('verifiedFifa18',False)),
    }
    if isinstance(pack_items,list):
        tops=sorted((x for x in pack_items if isinstance(x,dict)),key=lambda x:int(x.get('rating',0) or 0),reverse=True)[:5]
        req['topCards']=[{
            'itemId':int(x.get('id',x.get('itemId',0)) or 0),
            'assetId':int(x.get('assetId',0) or 0),
            'resourceId':int(x.get('resourceId',x.get('definitionId',x.get('assetId',0))) or 0),
            'overall':int(x.get('rating',0) or 0),
            'teamId':int(x.get('teamId',x.get('teamid',0)) or 0),
            'nationId':int(x.get('nation',x.get('nationId',0)) or 0),
            'rarity':int(x.get('rareflag',x.get('rareFlag',0)) or 0),
            'discardValue':int(x.get('discardValue',0) or 0),
        } for x in tops]
    try:
        tmp=WALKOUT_REQUEST_FILE.with_suffix('.json.tmp')
        tmp.write_text(json.dumps(req,separators=(',',':')),encoding='utf-8')
        os.replace(tmp,WALKOUT_REQUEST_FILE)
        log.warning('WALKOUT NATIVE REQUEST expected=%s overall=%d team=%d nation=%d rarity=%d discard=%d asset=%d resource=%d type=%d',
                    req['expectedWalkout'],req['overall'],req['teamId'],req['nationId'],req['rarity'],req['discardValue'],req['assetId'],req['resourceId'],req['walkoutType'])
    except Exception as e:
        log.warning('WALKOUT NATIVE REQUEST write failed: %s',e)
    return req

def _pack_wire_player(item, sku_mode=None):
    """Minimal FIFA 18 dynamic player item for pack reveal / purchased-items.

    The persistent DB keeps the richer club object.  This wire projection intentionally
    follows the working Aurora purchased-items shape while using FIFA 18's own
    ItemSubType.PLAYER (=2).  Static identity (name/nation/etc.) is resolved by the
    client from asset/resource id and does not need to be duplicated here.
    """
    x=dict(item or {})
    if sku_mode == 'WC' or str(x.get('skuMode','')).upper() == 'WC':
        x=_apply_world_cup_player_schema(x)
    attrs=x.get('attributeList') if isinstance(x.get('attributeList'),list) else []
    return {
        'id':int(x.get('id',0) or 0),
        'attributeList':[{'index':int(a.get('index',i) or i),'value':int(a.get('value',0) or 0)} for i,a in enumerate(attrs[:6]) if isinstance(a,dict)],
        'assetId':int(x.get('assetId',0) or 0),
        'definitionId':int(x.get('definitionId',x.get('resourceId',x.get('assetId',0))) or 0),
        'resourceId':int(x.get('resourceId',x.get('definitionId',x.get('assetId',0))) or 0),
        'resourceGameYear':2019,
        'itemType':'player',
        'itemState':'free',
        'rating':int(x.get('rating',0) or 0),
        'preferredPosition':str(x.get('preferredPosition',x.get('position','CM')) or 'CM'),
        'cardsubtypeid':2,
        'teamid':int(x.get('teamid',x.get('teamId',0)) or 0),
        'leagueId':int(x.get('leagueId',0) or 0),
        'rareflag':int(x.get('rareflag',x.get('rareFlag',0)) or 0),
        'nation':int(x.get('nation',x.get('nationId',0)) or 0),
        'nationId':int(x.get('nation',x.get('nationId',0)) or 0),
        'cardassetid':int(x.get('cardassetid',0) or 0),
        'owners':1,
        'untradeable':bool(x.get('untradeable',False)),
        'contract':int(x.get('contract',x.get('contracts',7)) or 7),
        'fitness':int(x.get('fitness',99) or 99),
        'morale':int(x.get('morale',50) or 50),
        'training':int(x.get('training',0) or 0),
        'suspension':int(x.get('suspension',0) or 0),
        'injuryType':0,
        'injuryGames':int(x.get('injuryGames',0) or 0),
        'loans':int(x.get('loans',0) or 0),
        'discardValue':int(x.get('discardValue',0) or 0),
        'lastSalePrice':int(x.get('lastSalePrice',0) or 0),
        'timestamp':0,
    }

def _purchase_pack(doc, query=None):
    """Open one pack using the purchased-items contract used by Aurora17.

    The FIFA 17 preservation build demonstrates that the client selects its native
    pack presentation from the ordinary purchased-items transaction + unassigned
    pile.  Keep that contract compact, but expose the verified FIFA 18 walkout
    members on the headline item at transaction time so the native branch sees
    them before the fallback memory helper is needed.
    """
    if not isinstance(doc,dict):doc={}
    query = query or {}
    sku_mode = str(doc.get('skuMode') or (query.get('skuMode') or [''])[0] or '').upper()
    pack=_pack_by_id(doc.get('packId',doc.get('id',0)))
    if not pack:
        return {
            'duplicateItemIdList':[], 'itemData':[],
            'currencies':[{'name':'coins','funds':_credits(),'finalFunds':_credits()},{'name':'points','funds':0,'finalFunds':0}],
            'unopenedPacks':_unopened_packs_payload(),
        }
    currency=str(doc.get('currency','COINS') or 'COINS').upper()
    if currency not in ('COINS','CREDITS'):
        return {
            'duplicateItemIdList':[], 'itemData':[],
            'currencies':[{'name':'coins','funds':_credits(),'finalFunds':_credits()},{'name':'points','funds':0,'finalFunds':0}],
            'unopenedPacks':_unopened_packs_payload(),
            'reason':'POINTS_NOT_AVAILABLE_LOCALLY',
        }
    owned_before=max(0,int(_owned_pack_counts().get(int(pack.get('id',0) or 0),0)))
    raw_use_credits=doc.get('useCredits',0)
    if isinstance(raw_use_credits,str):
        use_credits=raw_use_credits.strip().lower() not in ('','0','false','no','off')
    else:
        use_credits=bool(raw_use_credits)
    # Paid shelf requests observed from FIFA 18 send useCredits=1. Aurora's
    # native My Packs path is non-purchaseable and opens with useCredits=0.
    opening_owned=owned_before>0 and not use_credits
    if not use_credits and owned_before<=0:
        return {
            'duplicateItemIdList':[], 'itemData':[],
            'currencies':[{'name':'coins','funds':_credits(),'finalFunds':_credits()},{'name':'points','funds':0,'finalFunds':0}],
            'unopenedPacks':_unopened_packs_payload(),
            'reason':'NO_OWNED_PACK',
        }
    price=0 if opening_owned else int(pack.get('finalPrice',0) or 0)
    before=_credits()
    if before<price:
        return {
            'duplicateItemIdList':[], 'itemData':[],
            'currencies':[{'name':'coins','funds':before,'finalFunds':before},{'name':'points','funds':0,'finalFunds':0}],
            'unopenedPacks':_unopened_packs_payload(),
            'reason':'insufficientCoins',
        }

    defs=_choose_pack_contents(pack, sku_mode=sku_mode)
    # Aurora17's working client contract sends pack players lowest -> highest.
    # With our monotonically increasing instance ids this also gives the best card
    # the highest instance id without forcing it into itemData[0].
    defs=sorted(defs,key=lambda d:(int(d.get('rating',0) or 0),int(d.get('assetId',0) or 0)))
    rare_count=max(0,min(len(defs),int(pack.get('packContentInfo',{}).get('rareQuantity',0) or 0)))
    items=[]
    # Aurora's working purchased-items path allocates instance ids in descending
    # order while the definitions themselves are rating-ascending.  Preserve that
    # ordering exactly here instead of relying on _next_item_id() monotonic order.
    with _DB_LOCK:
        try:base_instance=int(_meta_get('nextPackWireInstance','899999900000'))
        except Exception:base_instance=899999900000
        _meta_set('nextPackWireInstance',base_instance+1000000000)
    for i,d in enumerate(defs):
        item_id=base_instance-i*10000000
        # Rare guarantees are applied to non-special cards only.  Special rarity
        # comes from the verified FIFA18 definition itself.
        force_rare=(i>=max(0,len(defs)-rare_count))
        rare_override=None if _pack_def_is_player(d) and _is_special_def(d) else force_rare
        item=_pack_make_item(d,item_id,rare_override=rare_override)
        if sku_mode == 'WC' and str(item.get('itemType','')).lower() == 'player':
            item = _apply_world_cup_player_schema(item, defn=d)
            item['skuMode'] = 'WC'
        _save_item(item);items.append(item)

    after=before-price
    _meta_set('credits',after)
    if opening_owned:
        _consume_owned_pack(int(pack.get('id',0) or 0))

    # Derive duplicates against the persistent club, but keep every newly opened
    # card in pile=6.  GET /purchased/items must return these exact same instances.
    owned_defs={}
    for x in _owned_players():
        rid=int(x.get('resourceId',x.get('definitionId',x.get('assetId',0))) or 0)
        owned_defs.setdefault(rid,int(x.get('id',0) or 0))
    dup=[]
    for x in items:
        rid=int(x.get('resourceId',x.get('definitionId',x.get('assetId',0))) or 0)
        owned=owned_defs.get(rid,0)
        if owned:
            dup.append({'itemId':int(x.get('id',0) or 0),'duplicateItemId':owned})

    # Keep persistence rich, but make the *presentation* response headline-first.
    # The v0.8.9.24 trace proved the native best-card entity is created only after
    # the POST has already returned (~190-326 ms).  Sending the Aurora-style
    # low->high list therefore lets FIFA sample a 75-ish itemData[0] while choosing
    # its presentation branch, before the later 86+ entity can be patched.  Keep the
    # persisted item instances/order unchanged; only order the POST aliases used by
    # the pack-opening parser so the actual best card is the headline immediately.
    wire_items=[_pack_wire_item(x, sku_mode=sku_mode) for x in items]
    presentation_items=sorted(wire_items,key=lambda x:(1 if str(x.get('itemType','')).lower()=='player' else 0,int(x.get('rating',0) or 0),int(x.get('resourceId',0) or 0),int(x.get('id',0) or 0)),reverse=True)
    player_items=[x for x in items if isinstance(x,dict) and str(x.get('itemType','')).lower()=='player']
    best=max(player_items,key=lambda x:int(x.get('rating',0) or 0),default={})
    best_rating=int(best.get('rating',0) or 0) if isinstance(best,dict) else 0
    expected_walkout=best_rating>=86
    best_rarity=int(best.get('rareflag',best.get('rareFlag',0)) or 0) if isinstance(best,dict) else 0
    walkout_type=(2 if best_rarity>1 else 1) if expected_walkout else 0
    best_id=int(best.get('id',0) or 0) if isinstance(best,dict) else 0
    best_asset=int(best.get('assetId',0) or 0) if isinstance(best,dict) else 0
    best_resource=int(best.get('resourceId',best.get('definitionId',best_asset)) or 0) if isinstance(best,dict) else 0
    best_team=int(best.get('teamId',best.get('teamid',0)) or 0) if isinstance(best,dict) else 0
    best_nation=int(best.get('nation',best.get('nationId',0)) or 0) if isinstance(best,dict) else 0
    best_discard=int(best.get('discardValue',0) or 0) if isinstance(best,dict) else 0

    # Restore only the transaction-time native vocabulary recovered in v0.8.9.3-8.
    # The startup walkOut* config remains suppressed because that was the concrete
    # FUT-entry softlock.  Putting these flags directly on the first/highest wire
    # item allows the JSON->Frostbite conversion to construct FUTPackOpeningEntityData
    # already armed, eliminating the runtime bridge race rather than chasing it later.
    headline_wire=presentation_items[0] if presentation_items else None
    if isinstance(headline_wire,dict):
        headline_wire.update({
            'isWalkout':bool(expected_walkout),'walkout':bool(expected_walkout),
            'presentationRating':best_rating,
            'PlayerOverall':best_rating,'PlayerTeamID':best_team,'PlayerNationID':best_nation,
            'PlayerRarity':best_rarity,'PlayerDiscardValue':best_discard,
            'PlayerDoWalkOut':bool(expected_walkout),'playerDoWalkOut':bool(expected_walkout),
            'PlayerWalkOutType':walkout_type,'playerWalkOutType':walkout_type,
            'IS_PLAYER_WALKOUT':bool(expected_walkout),
            'walkOutBucketId':1 if expected_walkout else 0,
            'walkOutPlayerType':walkout_type,'walkOutType':walkout_type,
        })

    entity_data={
        'PlayerOverall':best_rating,'PlayerTeamID':best_team,'PlayerNationID':best_nation,
        'PlayerRarity':best_rarity,'PlayerDiscardValue':best_discard,
        'PlayerWalkOutType':walkout_type,'PlayerDoWalkOut':bool(expected_walkout),
    }
    top3=sorted((x for x in player_items if isinstance(x,dict)),key=lambda x:int(x.get('rating',0) or 0),reverse=True)[:3]
    celebration={'CelebrationTier':3 if expected_walkout else (2 if best_rating>=83 else 1 if best_rating>=80 else 0)}
    for idx in range(3):
        x=top3[idx] if idx<len(top3) else {}
        celebration[f'PlayerOverall{idx}']=int(x.get('rating',0) or 0)

    out={
        'duplicateItemIdList':dup,
        'itemData':presentation_items,
        'currencies':[
            {'name':'coins','funds':after,'finalFunds':after},
            {'name':'points','funds':0,'finalFunds':0},
        ],
        'unopenedPacks':_unopened_packs_payload(),
        'packId':int(pack['id']),
        'purchasedPackId':int(pack['id']),
        'ownedPack':bool(opening_owned),'usedOwnedPack':bool(opening_owned),
        'numberItems':len(presentation_items),
        'newcards':len(presentation_items),
        'itemList':presentation_items,
        'items':presentation_items,
        'walkout':bool(expected_walkout),'isWalkout':bool(expected_walkout),
        'walkoutItemId':best_id if expected_walkout else 0,
        'walkoutAssetId':best_asset if expected_walkout else 0,
        'walkoutRating':best_rating if expected_walkout else 0,
        'highestRatedItemId':best_id,'highestRatedAssetId':best_asset,'highestRatedRating':best_rating,
        'PlayerDoWalkOut':bool(expected_walkout),'PlayerWalkOutType':walkout_type,
        'IS_PLAYER_WALKOUT':bool(expected_walkout),
        'walkOutBucketId':1 if expected_walkout else 0,
        'walkOutPlayerType':walkout_type,'walkOutType':walkout_type,
        'walkOutList':[best_asset] if expected_walkout and best_asset else [],
        'FUTPackOpeningEntityData':entity_data,
        'FUTPackOpeningEntityDataArray':[entity_data],
        'PackOpeningCelebrationInfo':celebration,
    }
    _publish_walkout_request(best,expected_walkout,walkout_type,player_items)
    log.warning('PACK WALKOUT DECISION threshold=86 expected=%s type=%d bestRating=%d rarity=%d headlineFirst=%s nativeHttpGate=%s',expected_walkout,walkout_type,best_rating,best_rarity,int(presentation_items[0].get('rating',0) or 0) if presentation_items else 0,bool(headline_wire and headline_wire.get('PlayerDoWalkOut')))
    log.warning('PACK OPEN AURORA-CONTRACT packId=%s name=%s owned=%s price=%d credits=%d->%d items=%d responseBytes~%d ratings=%s ids=%s best=%s/%s duplicateCount=%d',
                pack.get('id'),pack.get('name'),opening_owned,price,before,after,len(items),len(_json_bytes(out)),
                [int(x.get('rating',0) or 0) for x in presentation_items],[int(x.get('id',0) or 0) for x in presentation_items],
                int(best.get('assetId',0) or 0) if isinstance(best,dict) else 0,
                int(best.get('rating',0) or 0) if isinstance(best,dict) else 0,len(dup))
    return out

def _grant_reward_pack_contents(pack_id, sku_mode=None):
    """Materialise a Draft reward pack into the existing unassigned-item pipeline.

    The local backend has no separate unopened-pack inventory yet. Granting the
    documented reward pack contents straight to pile 6 makes the reward real and
    persistent: the same /purchased/items view used by normal pack openings will
    show the cards immediately after the Draft claim.
    """
    pack=_pack_by_id(pack_id)
    if not pack:return []
    defs=sorted(_choose_pack_contents(pack, sku_mode=sku_mode),key=lambda d:(int(d.get('rating',0) or 0),int(d.get('assetId',0) or 0)))
    rare_count=max(0,min(len(defs),int(pack.get('packContentInfo',{}).get('rareQuantity',0) or 0)))
    with _DB_LOCK:
        try:base_instance=int(_meta_get('nextDraftRewardInstance','979999900000'))
        except Exception:base_instance=979999900000
        _meta_set('nextDraftRewardInstance',base_instance+1000000000)
    items=[]
    for i,d in enumerate(defs):
        force_rare=(i>=max(0,len(defs)-rare_count))
        rare_override=None if _pack_def_is_player(d) and _is_special_def(d) else force_rare
        item=_pack_make_item(d,base_instance-i*10000000,rare_override=rare_override)
        if sku_mode == 'WC' and _pack_def_is_player(d):
            item = _apply_world_cup_player_schema(item, defn=d)
            item['skuMode'] = 'WC'
        _save_item(item);items.append(item)
    log.warning('DRAFT REWARD PACK GRANTED packId=%s name=%s items=%d ratings=%s',pack_id,pack.get('name'),len(items),[int(x.get('rating',0) or 0) for x in items])
    return items

def _purchased_payload(query=None):
    """Return the authoritative unassigned/new-items pile.

    FIFA reads this endpoint with the same parser as the pack-open POST.  The
    item instances here must therefore be the same pile=6 rows created by the
    preceding purchase transaction, not reconstructed cards or an empty alias.
    """
    query = query or {}
    sku_mode = str((query.get('skuMode') or [''])[0] or '').upper()
    persisted=sorted((dict(x) for x in _pending_items()),key=lambda x:int(x.get('id',0) or 0),reverse=True)
    rows=[_pack_wire_item(x, sku_mode=sku_mode) for x in persisted]
    club_defs={}
    for x in _owned_players():
        if sku_mode == 'WC' and x.get('skuMode') != 'WC': continue
        if sku_mode != 'WC' and x.get('skuMode') == 'WC': continue
        rid=int(x.get('resourceId',x.get('definitionId',x.get('assetId',0))) or 0)
        club_defs.setdefault(rid,int(x.get('id',0) or 0))
    dup=[]
    for x in rows:
        rid=int(x.get('resourceId',x.get('definitionId',x.get('assetId',0))) or 0)
        owned=club_defs.get(rid,0)
        if owned:
            dup.append({'itemId':int(x.get('id',0) or 0),'duplicateItemId':owned})
    coins=_credits();count=len(rows)
    return {
        'duplicateItemIdList':dup,
        'itemData':rows,
        'currencies':[{'name':'coins','funds':coins,'finalFunds':coins},{'name':'points','funds':0,'finalFunds':0}],
        'credits':coins,'coins':coins,'sessionCoinsBankBalance':coins,
        'count':count,'itemCount':count,'cardCount':count,'total':count,'totalCount':count,'numberItems':count,
        'itemList':rows,'items':rows,
    }

def _storage_pile_payload(query=None):
    """Return duplicate storage pile items in FIFA 18 World Cup DLC."""
    query = query or {}
    sku_mode = str((query.get('skuMode') or [''])[0] or '').upper()
    all_storage = [x for x in _db_items() if int(x.get('pile', 0) or 0) in (11, 8)]
    if sku_mode == 'WC':
        persisted = [x for x in all_storage if x.get('skuMode') == 'WC']
        rows = [_apply_world_cup_player_schema(x) if str(x.get('itemType','')).lower()=='player' else dict(x) for x in persisted]
    else:
        persisted = [x for x in all_storage if x.get('skuMode') != 'WC']
        rows = [dict(x) for x in persisted]
    count = len(rows)
    return {
        'duplicateItemIdList': [],
        'itemData': rows,
        'itemList': rows,
        'items': rows,
        'count': count,
        'itemCount': count,
        'cardCount': count,
        'total': count,
        'totalCount': count,
        'numberItems': count,
    }

def _store_payload():
    """Return retail-style purchase rows plus a separate My Packs group.

    Aurora17's working owned-pack contract does *not* mutate the paid shelf row.
    Every unopened entitlement is emitted as its own quantity=1 row with
    displayGroup.value="mypacks" and isPurchaseable=false. This matters because
    FIFA's My Packs tile filters the purchase collection by that display group.
    """
    owned=_owned_pack_counts()
    rows=[dict(x) for x in STORE_PACKS]
    my_packs=[]
    for pid,qty in sorted(owned.items()):
        base=_pack_by_id(pid)
        if not base:continue
        for copy_index in range(max(0,int(qty))):
            row=dict(base)
            row['quantity']=1
            row['ownedQuantity']=1
            row['unopened']=True
            row['isOwned']=True
            row['isRecoveredPack']=True
            row['recovered']=True
            row['recoverable']=True
            row['free']=True
            row['purchaseType']='RECOVERED'
            row['packSource']='DRAFT_REWARD'
            row['displayGroup']={'value':'mypacks','priority':int(base.get('sortPriority',10) or 10)+copy_index}
            row['category']='mypacks'
            row['categoryId']=5
            row['isPurchaseable']=False
            row['finalPrice']=0
            row['originalPrice']=0
            row['price']=0
            row['currencies']=[{'name':'coins','funds':0,'finalFunds':0},
                               {'name':'points','funds':0,'finalFunds':0}]
            my_packs.append(row)
    rows.extend(my_packs)
    return {'purchase':rows,'packs':rows,'items':rows,'myPacks':my_packs,'ownedPacks':my_packs,
            'unopenedPackItems':my_packs,'categoryInfo':[dict(x) for x in STORE_CATEGORIES],
            'categories':[dict(x) for x in STORE_CATEGORIES],'credits':_credits(),'points':0,
            'unopenedPacks':_unopened_packs_payload(),'timestamp':int(time.time())}

def _pack_quantities_payload():
    owned=_owned_pack_counts()
    return {'packQuantities':[{'packId':int(p.get('id',0) or 0),
                               'quantity':max(0,int(owned.get(int(p.get('id',0) or 0),0)))}
                              for p in STORE_PACKS]}

def _store_loc_xml():
    lines=['<?xml version="1.0" encoding="UTF-8"?>','<xliff version="1.2">',
           '  <file original="storepackdescriptions" source-language="en-US" datatype="plaintext">','    <body>']
    lines.append('      <trans-unit id="FUT_STORE_CAT_MYPACKS_NAME" resname="FUT_STORE_CAT_MYPACKS_NAME"><source>My Packs</source></trans-unit>')
    lines.append('      <trans-unit id="FUT_STORE_CAT_MYPACKS_DESC" resname="FUT_STORE_CAT_MYPACKS_DESC"><source>Packs you own and have not opened yet.</source></trans-unit>')
    for p in STORE_PACKS:
        pid=int(p['id']);name=str(p['name']);desc=str(p['packDescription'])
        esc=lambda x:(x.replace('&','&amp;').replace('<','&lt;').replace('>','&gt;'))
        lines.append(f'      <trans-unit id="FUT_STORE_PACK_{pid}_NAME" resname="FUT_STORE_PACK_{pid}_NAME"><source>{esc(name)}</source></trans-unit>')
        lines.append(f'      <trans-unit id="FUT_STORE_PACK_{pid}_DESC" resname="FUT_STORE_PACK_{pid}_DESC"><source>{esc(desc)}</source></trans-unit>')
        lines.append(f'      <trans-unit id="FUT_STORE_PACK_{pid}_NAME_MOBILE" resname="FUT_STORE_PACK_{pid}_NAME_MOBILE"><source>{esc(name)}</source></trans-unit>')
    lines += ['    </body>','  </file>','</xliff>']
    return ('\n'.join(lines)+'\n').encode('utf-8')

# ---- Squad Building Challenges ------------------------------------------
# v0.8.9.36 replaces the early hand-written SBC placeholders with the complete
# preserved FIFA 18 archive.  The bundled snapshot contains every long-lived
# category plus the full historical Live/Upgrade indexes and exactly one
# Marquee Matchups set (Week 46), as requested.  When internet is available a
# background refresh upgrades fallback rows with the archived requirements and
# rewards without ever blocking FUT boot.
SBC_BUNDLED_CACHE=ROOT/'data'/'fifa18-sbc-archive.json'
SBC_RUNTIME_CACHE=PLAYER_DATA_DIR/'fifa18-sbc-archive.json'
SBC_CATEGORY_ORDER=('Basic','Advanced','Upgrades','Leagues','Marquee Matchups','POTM','Prime ICONS','Live')
# v0.8.9.36.8: keep Live in the archive/ID namespace, but do not expose the
# historical Live category to the retail FIFA 18 UI.  The original game only
# showed currently-active Live sets; exposing our 270-entry history made the PC
# frontend crash while tabbing.
SBC_VISIBLE_CATEGORY_ORDER=('Basic','Advanced','Upgrades','Leagues','Marquee Matchups','POTM','Prime ICONS')
SBC_CATEGORY_IDS={name:(i+1)*10 for i,name in enumerate(SBC_CATEGORY_ORDER)}
_SBC_DOC_LOCK=threading.RLock()
_SBC_ARCHIVE_DOC=None

# Common FIFA 18 league names used by archived requirement text.  The player DB
# stores EA league IDs; this map lets the local submit validator enforce the
# historically named major-league requirements instead of treating them as
# generic text.
_SBC_LEAGUE_NAME_IDS={
    'premier league':13,'epl':13,'bundesliga':19,'ligue 1':16,'ligue 1 conforama':16,
    'laliga':53,'la liga':53,'laliga santander':53,'calcio a':31,'serie a':31,
    'icons':2118,'icon':2118,
}

# Reward text -> local pack implementation.  The archive still returns the
# original historical reward name verbatim; these IDs only control what appears
# under My Packs when the reward is granted.
_SBC_PACK_NAME_IDS=[
    ('ultimate pack',34),('jumbo rare players pack',32),('rare players pack',31),
    ('rare mega pack',35),('mega pack',30),('jumbo premium gold players pack',37),
    ('prime gold players pack',36),('premium gold players pack',29),
    ('small rare gold players pack',38),('small prime gold players pack',43),
    ('gold players pack',45),('rare electrum players pack',39),('prime electrum players pack',40),
    ('premium mixed players pack',41),('rare mixed players pack',42),('rare gold pack',28),
    ('jumbo premium gold pack',27),('jumbo gold pack',44),('premium gold pack',103),('gold pack',3),
    ('premium silver pack',102),('silver pack',2),('premium bronze pack',101),('bronze pack',1),
    ('two rare gold players pack',46),('two rare gold players',46),
    ('2 silver players pack',49),('2 silver players',49),('two silver players',49),
    ('three common gold players pack',74),('three common gold players',74),('3 common gold players',74),
    ('rare player pack',73),('rare player',73),
    ('small electrum players pack',63),('prime silver players pack',64),('rare bronze players pack',65),
    ('jumbo premium bronze pack',66),('small prime silver players pack',67),('small rare bronze players pack',68),
    ('small bronze players pack',69),('small silver players pack',70),('prime bronze players pack',71),('premium bronze players pack',72),
]

def _sbc_archive_doc():
    global _SBC_ARCHIVE_DOC
    with _SBC_DOC_LOCK:
        if _SBC_ARCHIVE_DOC is None:
            _SBC_ARCHIVE_DOC=load_sbc_archive(SBC_RUNTIME_CACHE,SBC_BUNDLED_CACHE)
        return _SBC_ARCHIVE_DOC

def _refresh_sbc_archive_background():
    global _SBC_ARCHIVE_DOC
    online=str(os.environ.get('FUT18_SBC_ARCHIVE_ONLINE','1')).strip().lower() not in ('0','false','no','off')
    if not online:return
    try:
        doc=refresh_sbc_archive(SBC_RUNTIME_CACHE,timeout=8)
        if isinstance(doc,dict) and isinstance(doc.get('sets'),list) and doc['sets']:
            with _SBC_DOC_LOCK:_SBC_ARCHIVE_DOC=doc
            log.warning('SBC ARCHIVE refresh complete sets=%d counts=%s errors=%s',len(doc['sets']),doc.get('counts',{}),doc.get('errors',{}))
    except Exception as e:
        log.info('SBC ARCHIVE refresh unavailable: %s',e)

def _sbc_progress_map():
    out={}
    with _DB_LOCK,_db_connect() as con:
        for r in con.execute('SELECT challenge_id,data FROM sbc_progress').fetchall():
            try:
                rec=json.loads(r['data'])
                # v0.8.9.26 incorrectly marked an empty challenge-open POST as
                # completion.  Only keep records backed by a real submission.
                if isinstance(rec,dict) and rec.get('completed') and not rec.get('submittedItemIds'):
                    continue
                out[int(r['challenge_id'])]=rec
            except Exception:pass
    return out

def _sbc_favourite_ids():
    try:raw=json.loads(_meta_get('sbcFavouriteSetIds','[]') or '[]')
    except Exception:raw=[]
    if not isinstance(raw,list):raw=[]
    out=set()
    for x in raw:
        try:out.add(int(x))
        except Exception:pass
    # Keep the dedicated table authoritative too.  Older v0.8.9.x experiments
    # created it, and retaining both representations makes favourites resilient
    # across upgrades without relying on one exact client route.
    try:
        with _DB_LOCK,_db_connect() as con:
            out.update(int(r['set_id']) for r in con.execute('SELECT set_id FROM sbc_favourites').fetchall())
    except Exception:pass
    return out

def _sbc_set_favourite(set_id,value=True):
    ids=_sbc_favourite_ids();sid=int(set_id);now=int(time.time())
    if value:ids.add(sid)
    else:ids.discard(sid)
    with _DB_LOCK,_db_connect() as con:
        if value:con.execute('INSERT OR REPLACE INTO sbc_favourites(set_id,updated_at) VALUES(?,?)',(sid,now))
        else:con.execute('DELETE FROM sbc_favourites WHERE set_id=?',(sid,))
    _meta_set('sbcFavouriteSetIds',json.dumps(sorted(ids),separators=(',',':')))
    return sid in ids

def _sbc_num(text,default=0):
    m=re.search(r'(-?\d[\d,]*)',str(text).replace('–','-'))
    if not m:return int(default)
    try:return int(m.group(1).replace(',',''))
    except Exception:return int(default)

def _sbc_rule_op(text):
    low=str(text).lower()
    if 'max' in low or 'at most' in low:return 'MAX'
    if 'exact' in low or 'exactly' in low:return 'EXACT'
    return 'MIN'

def _sbc_challenge_metrics(requirements):
    rating=0;chem=0;players=11
    for line in requirements or []:
        low=str(line).lower().replace('.',' ')
        n=_sbc_num(line,0)
        if ('squad rating' in low or 'team rating' in low or 'team overall rating' in low or 'squad overall rating' in low) and n:rating=n
        elif ('team chemistry' in low or 'squad chemistry' in low) and n:chem=n
        elif ('players in the squad' in low or 'number of players in the squad' in low or low.strip().startswith('number of players:')) and n:players=n
    return max(1,players),max(0,rating),max(0,chem)

def _sbc_native_eligibilities_for(requirements):
    """Compatibility alias used by server-side validation/debug output.

    FIFA 18's SBC contract uses TEAM_RATING=19, TEAM_CHEMISTRY=1 and
    PLAYER_COUNT=2. Key 0 is the separate Team Star Rating rule (0.5-star units),
    which is why sending an 89 OVR target as key 0 rendered as 44.50 stars.
    """
    players,rating,chem=_sbc_challenge_metrics(requirements);out=[];slot=0
    out.append({'eligibilitySlot':slot,'eligibilityKey':2,'eligibilityOperation':'EXACT','eligibilityValue':players});slot+=1
    if chem:
        out.append({'eligibilitySlot':slot,'eligibilityKey':1,'eligibilityOperation':'MIN','eligibilityValue':chem});slot+=1
    if rating:
        out.append({'eligibilitySlot':slot,'eligibilityKey':19,'eligibilityOperation':'MIN','eligibilityValue':rating});slot+=1
    return out

_SBC_LEGACY_SCOPE={'MIN':0,'MAX':1,'EXACT':2}
_SBC_LEGACY_TYPES={
    'TEAM_RATING':19,'TEAM_CHEMISTRY':1,'PLAYER_COUNT':2,
    'SAME_NATION_COUNT':4,'SAME_LEAGUE_COUNT':5,'SAME_CLUB_COUNT':6,
    'NATION_COUNT':7,'LEAGUE_COUNT':8,'CLUB_COUNT':9,
    'NATION_ID':10,'LEAGUE_ID':11,'CLUB_ID':12,'SCOPE':13,
    'LEGEND_COUNT':15,'PLAYER_LEVEL':17,'PLAYER_RARITY':18,
}

def _sbc_norm_name(v):
    # Archive text contains accents and curly apostrophes (notably Côte d’Ivoire).
    # Normalise to ASCII before token matching so the historical prose and FIFA's
    # numeric player metadata describe the same nation/club/league.
    text=str(v or '').replace('’', "'").replace('‘', "'")
    text=unicodedata.normalize('NFKD',text)
    text=''.join(ch for ch in text if not unicodedata.combining(ch)).lower()
    return re.sub(r'[^a-z0-9]+',' ',text).strip()

def _sbc_legacy_scope(slot,op):
    return {'type':'SCOPE','eligibilitySlot':int(slot),'eligibilityKey':13,
            'eligibilityValue':int(_SBC_LEGACY_SCOPE.get(str(op or 'MIN').upper(),0))}

def _sbc_legacy_rule(rows,slot,typ,value,op='MIN',*,count=None,filter_type=None,filter_value=None):
    """Append one FIFA 18 legacy eligibility slot.

    Counted filters (nation/league/rarity/level) share one eligibilitySlot with
    PLAYER_COUNT and the filter key.  SCOPE is then attached to that same slot,
    matching EA's preserved FUT challenge payloads.
    """
    slot=int(slot);op=str(op or 'MIN').upper()
    if count is not None:
        rows.append({'type':'PLAYER_COUNT','eligibilitySlot':slot,'eligibilityKey':2,'eligibilityValue':int(count)})
    rows.append({'type':str(typ),'eligibilitySlot':slot,'eligibilityKey':int(_SBC_LEGACY_TYPES[typ]),'eligibilityValue':int(value)})
    if filter_type is not None:
        rows.append({'type':str(filter_type),'eligibilitySlot':slot,'eligibilityKey':int(_SBC_LEGACY_TYPES[filter_type]),'eligibilityValue':int(filter_value)})
    rows.append(_sbc_legacy_scope(slot,op))
    return slot+1

def _sbc_named_nation_id(name):
    target=_sbc_norm_name(name)
    aliases={
        'czechia':'czech republic','usa':'united states','us':'united states',
        'ivory coast':'cote d ivoire','cote divoire':'cote d ivoire',
        'cote d ivoire':'cote d ivoire',
    }
    target=aliases.get(target,target)
    for nm,nid in NATION_IDS.items():
        n=_sbc_norm_name(nm);n=aliases.get(n,n)
        if n==target:return int(nid)
    return 0

def _sbc_named_league_id(name):
    target=_sbc_norm_name(name)
    if target in _SBC_LEAGUE_NAME_IDS:return int(_SBC_LEAGUE_NAME_IDS[target])
    aliases={'major league soccer':39,'mls':39,'premier league':13,'bundesliga':19,'serie a':31,'calcio a':31,
             'ligue 1':16,'ligue 1 conforama':16,'laliga':53,'la liga':53,'laliga santander':53}
    return int(aliases.get(target,0))

def _sbc_named_club_id(name):
    target=_sbc_norm_name(name)
    if not target:return 0
    # Resolve from the FIFA 18 player catalogue rather than maintaining a
    # second hand-written club table.  This is cached by _all_player_defs().
    try:
        rows=_all_player_defs()
    except Exception:
        rows=[]
    counts={}
    for d in rows:
        nm=_sbc_norm_name(d.get('clubName',''))
        if nm and (nm==target or target in nm or nm in target):
            tid=int(d.get('teamId',d.get('teamid',0)) or 0)
            if tid: counts[tid]=counts.get(tid,0)+1
    return max(counts,key=counts.get) if counts else 0

def _sbc_legacy_elg_req(requirements):
    """Translate archived FIFA 18 requirement text to the legacy elgReq wire format.

    The important distinction is that `type` is the semantic rule name, not the
    comparison operation.  Comparison direction is represented by a SCOPE row.
    Unknown historical prose is left to the server-side validator and never
    encoded with an invented numeric key, avoiding client crashes.
    """
    rows=[];slot=1
    reqs=[str(x).strip() for x in (requirements or []) if str(x).strip()]
    players,rating,chem=_sbc_challenge_metrics(reqs)
    handled=set()
    for idx,line in enumerate(reqs):
        low=line.lower().replace('–','-');op=_sbc_rule_op(line);n=_sbc_num(line,0)
        # Squad-wide count/rating/chemistry.
        if ('players in the squad' in low or 'number of players in the squad' in low or low.strip().startswith('number of players:')) and n:
            # Do not emit a standalone PLAYER_COUNT eligibility for total squad
            # size. FIFA 18 already derives this from playerRequirements/BRICK
            # slots. Sending both produces a duplicate "Players: Exactly N"
            # row which evaluates against the wrong scope and can stay at 0/N,
            # disabling Submit even when all starting slots are populated.
            handled.add(idx);continue
        if ('team rating' in low or 'squad rating' in low or 'team overall rating' in low or 'squad overall rating' in low) and n:
            slot=_sbc_legacy_rule(rows,slot,'TEAM_RATING',n,op);handled.add(idx);continue
        if ('team chemistry' in low or 'squad chemistry' in low) and n:
            slot=_sbc_legacy_rule(rows,slot,'TEAM_CHEMISTRY',n,op);handled.add(idx);continue
        # Unique/same nation/league/club counts.
        if ('different nations' in low or re.match(r'^\s*(?:nations|nationalities)\s*:',low)) and n:
            slot=_sbc_legacy_rule(rows,slot,'NATION_COUNT',n,op);handled.add(idx);continue
        if ('different leagues' in low or re.match(r'^\s*leagues\s*:',low)) and n:
            slot=_sbc_legacy_rule(rows,slot,'LEAGUE_COUNT',n,op);handled.add(idx);continue
        if ('different clubs' in low or re.match(r'^\s*clubs\s*:',low)) and n:
            slot=_sbc_legacy_rule(rows,slot,'CLUB_COUNT',n,op);handled.add(idx);continue
        if ('same nation' in low) and n:
            slot=_sbc_legacy_rule(rows,slot,'SAME_NATION_COUNT',n,op);handled.add(idx);continue
        if ('same league' in low) and n:
            slot=_sbc_legacy_rule(rows,slot,'SAME_LEAGUE_COUNT',n,op);handled.add(idx);continue
        if ('same club' in low) and n:
            slot=_sbc_legacy_rule(rows,slot,'SAME_CLUB_COUNT',n,op);handled.add(idx);continue
        # Card level / rarity.
        level=3 if 'gold player' in low else 2 if 'silver player' in low else 1 if 'bronze player' in low else 0
        if level:
            count=n or players
            level_op=op if n else 'EXACT'
            slot=_sbc_legacy_rule(rows,slot,'PLAYER_LEVEL',level,level_op,count=count);handled.add(idx);continue
        if ('rare player' in low or re.match(r'^\s*rare\s*:',low)) and n:
            # rareflag 1 is the normal FUT rare family; specials are still
            # validated server-side from the full historical text.
            slot=_sbc_legacy_rule(rows,slot,'PLAYER_RARITY',1,op,count=n);handled.add(idx);continue
        if ('icon player' in low or 'number of icon' in low) and (n or 'icon' in low):
            slot=_sbc_legacy_rule(rows,slot,'PLAYER_RARITY',12,op,count=n or 1);handled.add(idx);continue
        if ('totw' in low or 'team of the week' in low) and (n or 'week' in low):
            slot=_sbc_legacy_rule(rows,slot,'PLAYER_RARITY',3,op,count=n or 1);handled.add(idx);continue
        if ('tots' in low or 'team of the season' in low) and (n or 'season' in low):
            slot=_sbc_legacy_rule(rows,slot,'PLAYER_RARITY',11,op,count=n or 1);handled.add(idx);continue
        # Specific named nation/league/club player counts.
        group=''
        m=re.search(r'players?\s+from\s+(.+?)\s*:\s*(?:min\.?|max\.?|exact|exactly)',line,re.I)
        if m:group=m.group(1).strip()
        if not group:
            m=re.search(r'(?:min\.?|max\.?|exactly?)\s*(\d+)\s+players?\s+from\s+(.+)$',line,re.I)
            if m:group=m.group(2).strip()
        if not group:
            m=re.search(r'^(.+?)\s+players?\s*:\s*(?:min\.?|max\.?|exact|exactly)',line,re.I)
            if m:group=m.group(1).strip()
        if group and (n or 'required' in low):
            count=n or 1
            # Intersection requirements such as France + LaLiga carry both
            # filters on one eligibility slot.
            parts=[x.strip() for x in re.split(r'\s*\+\s*',group) if x.strip()]
            nation_ids=[_sbc_named_nation_id(x) for x in parts];nation_ids=[x for x in nation_ids if x]
            league_ids=[_sbc_named_league_id(x) for x in parts];league_ids=[x for x in league_ids if x]
            club_ids=[_sbc_named_club_id(x) for x in parts];club_ids=[x for x in club_ids if x]
            filters=[]
            if nation_ids: filters.append(('NATION_ID',nation_ids[0]))
            if league_ids: filters.append(('LEAGUE_ID',league_ids[0]))
            if club_ids: filters.append(('CLUB_ID',club_ids[0]))
            if filters:
                typ,val=filters[0]
                rows.append({'type':'PLAYER_COUNT','eligibilitySlot':slot,'eligibilityKey':2,'eligibilityValue':int(count)})
                for ftyp,fval in filters:
                    rows.append({'type':ftyp,'eligibilitySlot':slot,'eligibilityKey':_SBC_LEGACY_TYPES[ftyp],'eligibilityValue':int(fval)})
                rows.append(_sbc_legacy_scope(slot,op));slot+=1;handled.add(idx);continue
    # playerRequirements carries the total active-slot count.  elgReq can be
    # empty when an SBC only constrains squad size; this is preferable to a
    # duplicate standalone PLAYER_COUNT row that FIFA 18 evaluates incorrectly.
    return rows

def _sbc_reward_lines(rows):
    return [str(x).strip() for x in (rows or []) if str(x).strip()]

def _sbc_pack_id_for_reward(line):
    raw=str(line or '')
    # Historical pages frequently concatenate linked pack labels (e.g.
    # RareGoldPack). Compare canonical alphanumeric forms.  First prefer the
    # actual local store catalogue so a historical label such as Electrum
    # Players Pack resolves to its real pack definition/content rather than a
    # generic players-pack fallback.
    canon=re.sub(r'[^a-z0-9]+','',raw.lower())
    exact=[]
    for pack in STORE_PACKS:
        key=re.sub(r'[^a-z0-9]+','',str(pack.get('name','')).lower())
        if key: exact.append((key,int(pack.get('id',0) or 0)))
    exact.sort(key=lambda x:len(x[0]),reverse=True)
    for key,pid in exact:
        if pid and key in canon:return pid
    aliases=[]
    for name,pid in _SBC_PACK_NAME_IDS:
        aliases.append((re.sub(r'[^a-z0-9]+','',name.lower()),int(pid)))
    aliases.sort(key=lambda x:len(x[0]),reverse=True)
    for key,pid in aliases:
        if key and key in canon:return pid
    if 'pack' in canon:
        if 'rare' in canon and 'player' in canon:return 31
        if 'player' in canon:return 29
        if 'silver' in canon:return 102
        if 'bronze' in canon:return 101
        return 103
    return 0


# Exact FIFA 18 League SBC group rewards.  These are source-restricted SBC
# player cards (rareflag 24), never TOTW cards.  The archive pages preserve the
# club-by-club challenges but flatten some group reward labels, so keep the
# historical reward player/rating/face stats here and resolve the base identity
# from the local FIFA 18 player database at runtime.
# set name -> (player lookup aliases, display name, rating, position, face stats, coins)
_SBC_LEAGUE_REWARDS={
    'Pro League':(('teodorczyk','lukasz teodorczyk'),'Teodorczyk',86,'ST',[78,87,71,78,29,87],15000),
    'Premier League':(('kevin de bruyne','de bruyne'),'De Bruyne',94,'CAM',[80,91,94,93,52,81],22500),
    'LaLiga Santander':(('antoine griezmann','griezmann'),'Griezmann',92,'ST',[90,91,86,92,36,77],22500),
    'Hyundai A-League':(('bobo','bobô'),'Bobô',85,'ST',[80,87,80,82,37,80],10000),
    'Meiji Yasuda J1 League':(('cristiano',),'Cristiano',83,'ST',[93,85,80,82,45,86],10000),
    'Bundesliga':(('thiago','thiago alcantara','thiago alcántara'),'Thiago',91,'CM',[75,81,93,93,65,69],22500),
    'Calcio A':(('marek hamsik','hamsik','hamšík'),'Hamšík',89,'CM',[76,84,87,87,72,73],25000),
    'Major League Soccer':(('sebastian giovinco','giovinco'),'Giovinco',86,'CF',[85,86,85,89,32,65],25000),
    'Ligue 1 Conforama':(('edinson cavani','cavani'),'Cavani',92,'ST',[83,91,81,87,48,86],22500),
    'Dawry Jameel':(('omar al soma','al soma'),'Al Soma',86,'ST',[82,87,80,83,48,84],20000),
    'EFL Championship':(('liam moore','moore'),'Liam Moore',83,'CB',[80,50,72,75,82,83],25000),
    'Eredivisie':(('amin younes','younes'),'Younes',84,'LW',[88,77,84,92,34,73],22500),
    'Russian League':(('quincy promes','promes'),'Promes',86,'RM',[92,87,84,88,35,73],22500),
    'Liga NOS':(('iker casillas','casillas'),'Casillas',86,'GK',[90,80,62,89,64,85],25000),
    'Liga Bancomer MX':(('andre pierre gignac','andré pierre gignac','gignac'),'Gignac',86,'ST',[76,89,78,80,48,87],20000),
    'Süper Lig':(('ricardo quaresma','quaresma'),'Quaresma',86,'RM',[86,82,85,92,28,66],25000),
}

# Exact EA base asset identity for every FIFA 18 League SBC reward player.
# Keeping the base identity separate from the visual SBC rarity is deliberate:
# the retail card is an untradeable SBC reward (rareflag 24), not a TOTW card.
_SBC_LEAGUE_ASSET_IDS={
    'Pro League':201013,
    'Premier League':192985,
    'LaLiga Santander':194765,
    'Hyundai A-League':152814,
    'Meiji Yasuda J1 League':207722,
    'Bundesliga':189509,
    'Calcio A':171877,
    'Major League Soccer':184431,
    'Ligue 1 Conforama':179813,
    'Dawry Jameel':223627,
    'EFL Championship':200758,
    'Eredivisie':205966,
    'Russian League':208808,
    'Liga NOS':5479,
    'Liga Bancomer MX':153244,
    'Süper Lig':20775,
}

def _sbc_exact_league_reward(set_name):
    key=str(set_name or '').strip();rec=_SBC_LEAGUE_REWARDS.get(key)
    if not rec:return None
    aliases,display,rating,pos,face,_coins=rec
    aid=int(_SBC_LEAGUE_ASSET_IDS.get(key,0) or 0)
    if aid<=0:return None
    # Resolve static/base metadata when available, but never depend on a
    # special-card name search.  The old search could select a same-rating IF
    # or FOF item and was the source of the wrong black reward frames.
    try:base=dict(_definition_by_asset(aid) or {})
    except Exception:base={}
    # If the local definition catalogue is incomplete, still emit a complete,
    # deterministic reward item.  FIFA only needs the exact base asset identity
    # plus the SBC rarity/rating/attributes to render the historical reward.
    base.setdefault('assetId',aid)
    base.setdefault('resourceId',aid)
    base.setdefault('definitionId',aid)
    base.setdefault('name',display)
    rid=aid  # base identity; rareflag 24 selects the SBC reward presentation.
    base.update({'resourceId':rid,'definitionId':rid,'assetId':aid,'rating':int(rating),
        'position':str(pos),'preferredPosition':str(pos),'face':list(face),'attributeArray':list(face),
        'rareflag':24,'rareFlag':24,'specialType':'SBC','cardType':'SBC','isSpecial':True,
        'sourceEligibility':'SBC_REWARD','acquisitionSource':'SBC_REWARD','verifiedFifa18':True,
        'name':str(base.get('name') or display),'displayName':display,
        'source':'FIFA18 exact historical League SBC reward identity'})
    return base

# Exact FIFA 18 Premier League POTM / Award Winner reward identities.
# Values are (resourceId, assetId, rating, position, teamId, leagueId, nation, face).
_SBC_POTM_REWARDS={
    'Wilfried Zaha - April POTM':(151193661,198717,88,'ST',1799,13,108,[96,88,85,93,42,82]),
    'Mohamed Salah - March POTM':(235090355,209331,93,'RW',9,13,111,[99,93,91,96,60,84]),
    'Mohamed Salah - February POTM':(184758707,209331,92,'RW',9,13,111,[98,92,89,95,58,82]),
    'Sergio Agüero - January POTM':(151148023,153079,94,'ST',10,13,52,[92,95,86,95,31,86]),
    'Harry Kane - December POTM':(167974286,202126,91,'ST',18,13,14,[79,95,82,87,48,89]),
    'Mohamed Salah - November POTM':(117649843,209331,88,'ST',9,13,111,[96,87,82,92,53,75]),
    'Leroy Sané - October POTM':(67331356,222492,86,'LW',10,13,21,[95,85,79,89,40,78]),
    'Harry Kane - September POTM':(67310990,202126,89,'ST',18,13,14,[76,92,78,83,47,86]),
    'Sadio Mané - August POTM':(100872018,208722,87,'LW',9,13,136,[95,85,83,88,41,78]),
}

def _sbc_exact_potm_reward(set_name):
    rec=_SBC_POTM_REWARDS.get(str(set_name or '').strip())
    if not rec:return None
    rid,aid,rating,pos,team,league,nation,face=rec
    base=_definition_by_asset(int(aid)) or {}
    d=dict(base);d.update({'resourceId':int(rid),'definitionId':int(rid),'assetId':int(aid),
        'rating':int(rating),'position':str(pos),'preferredPosition':str(pos),
        'teamId':int(team),'teamid':int(team),'leagueId':int(league),'nation':int(nation),
        'face':list(face),'attributeArray':list(face),'rareflag':28,'rareFlag':28,
        'specialType':'AWARD','cardType':'AWARD','isSpecial':True,'verifiedFifa18':True,
        'source':'FIFPlay/FIFAUTeam FIFA18 exact POTM identity'})
    return d

def _sbc_find_reward_player(line,set_name=''):
    text=(str(line)+' '+str(set_name)).lower()
    if 'pack' in text:return None
    league=_sbc_exact_league_reward(set_name)
    if league and any(k in text for k in ('sbc','player','card','untradeable','reward')):
        return league
    potm=_sbc_exact_potm_reward(set_name)
    if potm and any(k in text for k in ('potm','player of the month','sbc card','card','untradeable')):
        return potm
    pool=[]
    try:pool=list(_special_player_defs())
    except Exception:pool=[]
    # Prime Icon SBCs are sourced from the exact FIFA18 icon catalogue too.
    try:pool += list(_icon_player_defs())
    except Exception:pass
    st=''
    if 'festival of futball' in text or 'festival of football' in text:st='FOF'
    elif 'tots' in text or 'team of the season' in text:st='TOTS'
    elif 'totw' in text or 'team of the week' in text or re.search(r'\bif\b',text):st='TOTW'
    elif 'futmas' in text:st='FUTMAS'
    elif 'path to glory' in text or re.search(r'\bptg\b',text):st='PTGS'
    elif 'one to watch' in text or re.search(r'\botw\b',text):st='OTW'
    elif 'halloween' in text or 'ultimate scream' in text:st='HALLOWEEN'
    elif 'premium sbc card' in text:st='SBC_PREMIUM'
    elif 'sbc card' in text:st='SBC'
    elif 'icon' in text:st='ICON'
    named=[]
    text_norm = _promo_name_key(text)
    for d in pool:
        nm=str(d.get('name','') or '').strip()
        nm_norm = _promo_name_key(nm)
        if (nm and len(nm)>=4 and nm.lower() in text) or (nm_norm and len(nm_norm)>=4 and nm_norm in text_norm):
            named.append(d)
    if named:
        if st:
            typed=[d for d in named if st in str(d.get('specialType',d.get('cardType','')) or '').upper() or (st=='ICON' and int(d.get('leagueId',0) or 0)==2118)]
            if typed:return max(typed,key=lambda x:int(x.get('rating',0) or 0))
        return max(named,key=lambda x:int(x.get('rating',0) or 0))
    if st:
        candidates=[d for d in pool if st in str(d.get('specialType',d.get('cardType','')) or '').upper() or (st=='ICON' and int(d.get('leagueId',0) or 0)==2118)]
        if candidates:return max(candidates,key=lambda x:int(x.get('rating',0) or 0))
    return None

def _sbc_awards_from_lines(lines,set_name=''):
    awards=[]
    for raw in _sbc_reward_lines(lines):
        low=raw.lower();count=1
        m=re.search(r'\b(\d+)\s*x\b',low)
        if m:
            try:count=max(1,int(m.group(1)))
            except Exception:count=1
        # Explicit coin reward.
        if 'coin' in low:
            coins=_sbc_num(raw,0)
            if coins>0:
                awards.append({'type':'coin','awardType':'coin','count':1,'value':coins,'coins':coins,'halId':0,'isUntradeable':False,'loan':0,'loanType':'','itemData':{},'name':raw,'displayName':raw})
                continue
        pid=_sbc_pack_id_for_reward(raw)
        if pid:
            awards.append({'type':'pack','awardType':'pack','count':count,'value':pid,'packId':pid,'halId':pid,'isUntradeable':'untradeable' in low,'loan':0,'loanType':'','itemData':{},'name':raw,'displayName':raw,'historicalReward':raw})
            continue
        # Resolve player-card identity only when the SBC is actually completed.
        # Doing it while rendering /sbs/sets would force the full player/special
        # database to load hundreds of times and can stall FIFA's SBC menu.
        if any(k in low for k in (' player','player ','card','icon','potm','tots','totw','futmas','festival of fut','sbc ')):
            awards.append({'type':'item','awardType':'item','count':count,'value':0,'resourceId':0,'assetId':0,'halId':0,
                'isUntradeable':True,'loan':1 if 'loan' in low else 0,'loanType':'MATCHES' if 'loan' in low else '',
                'itemData':{},'name':raw,'displayName':raw,'historicalReward':raw,'deferredIdentity':True})
            continue
        # Keep unsupported historical rewards visible instead of silently
        # replacing their name with a made-up pack.
        awards.append({'type':'text','awardType':'text','count':count,'value':0,'halId':0,'isUntradeable':'untradeable' in low,'loan':0,'loanType':'','itemData':{},'name':raw,'displayName':raw,'historicalReward':raw})
    return awards

_SBC_REWARD_PREVIEW_CACHE={}
_SBC_CLIENT_AWARD_TYPES={'coin','pack','item'}

def _sbc_reward_item_data(d,set_name='',loan_matches=0):
    """Build a complete, display-only FIFA 18 player ItemData reward preview.

    FIFA 18's Reward model constructs an Item immediately for type="item" and
    dereferences itemData.loans before doing anything else.  Never send a partial
    or empty itemData object for an item award.
    """
    d=dict(d or {})
    rid=int(d.get('resourceId',d.get('definitionId',d.get('assetId',0))) or 0)
    aid=int(d.get('assetId',rid) or rid)
    if not rid or not aid:
        return None
    pos=str(d.get('position',d.get('preferredPosition','CM')) or 'CM').upper()
    rating=int(d.get('rating',0) or 0)
    rare=int(d.get('rareflag',d.get('rareFlag',0)) or 0)
    face=_extract_face_attributes(d, rating or 50)
    attrs=[int(x or 0) for x in face]
    loan_matches=max(0,int(loan_matches or 0))
    contract=loan_matches if loan_matches>0 else 7
    name=str(d.get('name',set_name) or set_name or 'SBC Reward')
    # Keep the same rich dynamic-item contract already proven by My Club and
    # pack items.  cardsubtypeid=2 is the FIFA ItemSubType.PLAYER family used
    # by the reward model; preferredPosition carries the football position.
    return {
        'id':rid,'itemId':rid,
        'assetId':aid,'definitionId':rid,'resourceId':rid,'resourceGameYear':2019,
        'itemType':'player','cardsubtypeid':2,'itemState':'free',
        'rating':rating,'rareflag':rare,'rareFlag':rare,
        'preferredPosition':pos,'position':pos,
        'leagueId':int(d.get('leagueId',0) or 0),
        'teamid':int(d.get('teamId',d.get('teamid',0)) or 0),
        'teamId':int(d.get('teamId',d.get('teamid',0)) or 0),
        'nation':int(d.get('nation',d.get('nationId',0)) or 0),
        'attributeList':_attrs(attrs),'attributeArray':attrs,
        'statsList':[],'statsArray':[0,0,0,0,0],
        'lifetimeStats':[],'lifetimeStatsArray':[0,0,0,0,0],
        'contract':contract,'contracts':contract,'fitness':99,'morale':50,
        'formation':'f442','injuryGames':0,'injuryType':'none','suspension':0,
        'training':0,'trainingId':0,'trainingResourceId':0,
        'owners':1,'loyaltyBonus':1,'loans':loan_matches,
        'loansInfo':{'loanType':'MATCHES' if loan_matches else '', 'loanValue':loan_matches},
        'playStyle':250,'pile':0,'untradeable':True,'tradeable':False,
        'discardValue':0,'lastSalePrice':0,'timestamp':0,
        'marketDataMinPrice':150,'marketDataMaxPrice':15000000,
        'posMods':[],'possiblePositions':[],
        'assists':0,'lifetimeAssists':0,'skillmoves':3,'weakfootabilitytypecode':3,
        'attackingworkrate':0,'defensiveworkrate':0,'trait1':0,'trait2':0,
        'preferredfoot':1,'baseTraits':[],'iconTraits':[],'groups':[],
        'name':name,'displayName':name,'knownAs':name,'firstName':'','lastName':'',
        'description':'','detaildescription':'',
    }

def _sbc_item_award_preview(award,set_name=''):
    """Resolve an item award to a complete static player identity for display.

    Returns None when the historical reward cannot be resolved safely.  Sending
    type="item" with itemData={} crashes FIFA 18 because its Reward constructor
    dereferences itemData.loans synchronously.
    """
    a=dict(award or {})
    if str(a.get('type','')).lower()!='item':
        return a
    existing=a.get('itemData')
    if isinstance(existing,dict) and existing.get('resourceId') and 'loans' in existing:
        return a
    key=(str(a.get('historicalReward',a.get('name',''))),str(set_name))
    cached=_SBC_REWARD_PREVIEW_CACHE.get(key,'__missing__')
    if cached!='__missing__':
        return dict(cached) if isinstance(cached,dict) else None
    d=_sbc_find_reward_player(key[0],set_name)
    if not d:
        _SBC_REWARD_PREVIEW_CACHE[key]=None
        return None
    loan_matches=0
    m=re.search(r'(\d+)\s*[- ]?match',key[0],re.I)
    if m:
        try:loan_matches=int(m.group(1))
        except Exception:loan_matches=0
    item=_sbc_reward_item_data(d,set_name,loan_matches)
    if not item:
        _SBC_REWARD_PREVIEW_CACHE[key]=None
        return None
    rid=int(item['resourceId']);aid=int(item['assetId'])
    safe={
        'type':'item','awardType':'item','value':rid,'resourceId':rid,'assetId':aid,
        'halId':100,'count':max(1,int(a.get('count',1) or 1)),
        'isUntradeable':True,'loan':loan_matches,'loanType':'MATCHES' if loan_matches else '',
        'itemData':item,'name':str(a.get('name',key[0])),'displayName':str(a.get('displayName',a.get('name',key[0]))),
        'historicalReward':str(a.get('historicalReward',key[0])),'deferredIdentity':False,
    }
    _SBC_REWARD_PREVIEW_CACHE[key]=dict(safe)
    return safe

def _sbc_reward_display_label(raw,fallback='Pack'):
    text=str(raw or '').strip()
    text=re.sub(r'^\s*[?✔️•-]*\s*','',text)
    text=re.sub(r'^\s*\d+\s*[x×]\s*','',text,flags=re.I)
    # Expand labels flattened by HTML anchors (RareGoldPack -> Rare Gold Pack).
    text=re.sub(r'(?<=[a-z])(?=[A-Z])',' ',text)
    text=re.sub(r'\s+',' ',text).strip()
    return text or str(fallback or 'Pack')

def _sbc_awards_for_display(awards,set_name=''):
    """Project historical rewards onto FIFA 18's *actual* Award contract.

    Retail FIFA 18 accepts coin/pack/item only.  Unknown text rewards are kept
    in historicalRewards/groupRewardsText, never in the native awards array.
    Unresolved item rewards are also withheld until an exact card identity is
    available rather than exposing a crash-prone empty itemData object.
    """
    out=[]
    for raw in awards or []:
        if not isinstance(raw,dict):continue
        typ=str(raw.get('type',raw.get('awardType',''))).lower()
        if typ not in _SBC_CLIENT_AWARD_TYPES:
            continue
        if typ=='item':
            row=_sbc_item_award_preview(raw,set_name)
            if not row or not isinstance(row.get('itemData'),dict):continue
            item=row['itemData']
            if not int(item.get('resourceId',0) or 0) or 'loans' not in item or 'contract' not in item:continue
            out.append(row);continue
        if typ=='coin':
            value=int(raw.get('coins',raw.get('value',0)) or 0)
            if value<=0:continue
            out.append({'type':'coin','awardType':'coin','value':value,'count':max(1,int(raw.get('count',1) or 1)),
                        'halId':0,'isUntradeable':False,'loan':0,'loanType':''})
            continue
        if typ=='pack':
            value=int(raw.get('packId',raw.get('value',0)) or 0)
            if value<=0:continue
            pack=_pack_by_id(value) or {}
            hist=str(raw.get('historicalReward',raw.get('displayName',raw.get('name',''))) or '');pname=_sbc_reward_display_label(hist,pack.get('name','Pack'))
            art=int(pack.get('packAssetId',pack.get('assetId',4)) or 4)
            out.append({'type':'pack','awardType':'pack','value':value,'id':value,'packId':value,
                        'packAssetId':art,'packImageId':art,'assetId':art,
                        'count':max(1,int(raw.get('count',1) or 1)),
                        'halId':int(raw.get('halId',value) or value),'isUntradeable':bool(raw.get('isUntradeable',False)),
                        'loan':0,'loanType':'','name':pname,'displayName':pname,'packName':pname,'title':pname,
                        'description':str(pack.get('packDescription',pname) or pname),
                        'historicalReward':str(raw.get('historicalReward',raw.get('name','')) or '')})
    return out

def _sbc_set_id(category,set_index):
    # Signed-32-bit-safe and deterministic.  Category occupies the high digits,
    # while the archive order provides stable IDs inside the category.
    return int(SBC_CATEGORY_IDS.get(category,90))*1_000_000 + (int(set_index)+1)*100

def _sbc_compile_sets():
    doc=_sbc_archive_doc();rawsets=list(doc.get('sets',[]) if isinstance(doc,dict) else [])
    # Enforce the user's one-Marquee-only request even if an old runtime cache
    # from a previous build contains every historical week.
    seen_marquee=False;filtered=[]
    for r in rawsets:
        if not isinstance(r,dict):continue
        cat=str(r.get('category','Live') or 'Live')
        if cat=='Marquee Matchups':
            if seen_marquee:continue
            seen_marquee=True
        if cat not in SBC_CATEGORY_IDS:continue
        filtered.append(r)
    bycat={c:[] for c in SBC_CATEGORY_ORDER}
    for r in filtered:bycat[str(r.get('category'))].append(r)
    progress=_sbc_progress_map();fav=_sbc_favourite_ids();compiled=[]
    for cat in SBC_CATEGORY_ORDER:
        for si,raw in enumerate(bycat.get(cat,[])):
            sid=_sbc_set_id(cat,si);chrows=[]
            raw_ch=list(raw.get('challenges',[]) if isinstance(raw.get('challenges'),list) else [])
            if not raw_ch:raw_ch=[{'name':raw.get('name','SBC'),'requirements':['Number of Players in the Squad: 11'],'rewards':[]}]
            for ci,ch in enumerate(raw_ch):
                cid=sid+ci+1;req=_sbc_reward_lines(ch.get('requirements',[]));rew=_sbc_reward_lines(ch.get('rewards',[]));players,rating,chem=_sbc_challenge_metrics(req)
                prec=progress.get(cid,{}) or {};times_done=int(prec.get('timesCompleted',1 if prec.get('completed') else 0) or 0);done=times_done>0;elig=_sbc_native_eligibilities_for(req);grant_awards=_sbc_awards_from_lines(rew,str(raw.get('name','')));awards=_sbc_awards_for_display(grant_awards,str(raw.get('name','')))
                desc=str(ch.get('description','') or '').strip() or 'Complete this FIFA 18 SBC challenge.'
                challenge={
                    'id':cid,'challengeId':cid,'setId':sid,'name':str(ch.get('name',raw.get('name','Squad Building Challenge'))),
                    'priority':ci+1,'description':desc,'requirements':req,'requirementsText':req,'historicalRequirements':req,
                    'historicalRewards':rew,'status':'COMPLETED' if done else 'NOT_STARTED','challengeType':'OPEN_CHALLENGE','type':'OPEN_CHALLENGE',
                    'startTime':0,'endTime':0,'repeatable':bool(raw.get('repeatable',False)),'notExpirable':True,
                    'timesCompleted':times_done,'lastCompleteTime':int(prec.get('completedAt',0) or 0),
                    'awards':awards,'rewards':awards,'grantAwards':grant_awards,'formation':'f442','playerCount':players,'chemistry':chem,'rating':rating,
                    'elgReq':[{'type':x['eligibilityOperation'],'eligibilitySlot':x['eligibilitySlot'],'eligibilityKey':x['eligibilityKey'],'eligibilityValue':x['eligibilityValue']} for x in elig],
                    'elgDesc':req,'elgOperation':'AND','challengeImageId':str(sid),'assetId':str(sid),'tutorial':0,
                    'eligibilities':elig,'eligibilityRules':[dict(x) for x in elig],'eligibilityOperation':'AND',
                }
                chrows.append(challenge)
            completed=sum(1 for x in chrows if x['timesCompleted']);set_done=bool(chrows) and completed==len(chrows)
            group_lines=_sbc_reward_lines(raw.get('groupRewards',[]))
            if cat=='Leagues' and str(raw.get('name','')) in _SBC_LEAGUE_REWARDS:
                _lr=_SBC_LEAGUE_REWARDS[str(raw.get('name',''))]
                group_lines=[f'1 x SBC {_lr[1]}',f'{int(_lr[5])} coins']
            group_grant_awards=_sbc_awards_from_lines(group_lines,str(raw.get('name','')));group_awards=_sbc_awards_for_display(group_grant_awards,str(raw.get('name','')))
            st={
                'id':sid,'setId':sid,'categoryId':SBC_CATEGORY_IDS[cat],'category':cat,'name':str(raw.get('name','Squad Building Challenge')),
                'description':str(raw.get('description','') or f"FIFA 18 {cat} SBC: {raw.get('name','Squad Building Challenge')}"),'priority':si+1,
                'challengesCount':len(chrows),'challengesCompletedCount':completed,'hidden':False,'tagged':1 if sid in fav else 0,
                'endTime':0,'repeatable':bool(raw.get('repeatable',False)),'repeatabilityMode':'UNLIMITED' if raw.get('repeatable') else 'NONE',
                'timesCompleted':1 if set_done else 0,'tutorial':str(raw.get('name',''))=='Let’s Get Started','taggedByProduction':False,
                'taggedByUser':sid in fav,'isFavourite':sid in fav,'setImageId':str(sid),'assetId':str(sid),'previewImageId':str(sid),
                'rewardPreviewImageId':str(sid),'releaseTime':0,'startTime':0,'repeats':0,'repeatRefreshInterval':0,
                'timesCompletedInInterval':0,'lastCompletedTime':max([int(progress.get(x['id'],{}).get('completedAt',0) or 0) for x in chrows] or [0]),
                'notExpirable':True,'isFeatured':cat in ('Prime ICONS','Leagues','Live'),'isSingleChallenge':len(chrows)==1,
                'awards':group_awards,'rewards':group_awards,'grantAwards':group_grant_awards,'groupRewardsText':group_lines,'challenges':chrows,'refreshInterval':0,
                'sourceCompleteness':raw.get('sourceCompleteness','archive'),
            }
            compiled.append(st)
    return compiled

# FIFA 18's native /sbs/sets parser is a set-index parser, not the challenge
# detail parser.  v0.8.9.36 accidentally embedded every challenge, requirement
# array and reward object in every set *and* duplicated those objects through the
# All/category envelopes.  With the complete historical archive that produced a
# 3.5-4+ MB JSON document and the retail client died immediately after parsing it.
# Keep this response lightweight; only safe group reward previews are included.
# Full challenge requirements/rewards are served by /sbs/setId/<id>/challenges.
SBC_ALL_PREVIEW_LIMIT=64
_SBC_SET_INDEX_KEYS=(
    'id','setId','categoryId','name','description','priority','challengesCount',
    'challengesCompletedCount','hidden','tagged','endTime','repeatable',
    'repeatabilityMode','timesCompleted','tutorial','taggedByProduction',
    'taggedByUser','isFavourite','setImageId','assetId','previewImageId',
    'rewardPreviewImageId','releaseTime','startTime','repeats',
    'repeatRefreshInterval','timesCompletedInInterval','lastCompletedTime',
    'notExpirable','isFeatured','isSingleChallenge','refreshInterval','awards',
)

def _sbc_set_index_summary(st):
    if not isinstance(st,dict):return {}
    out={k:st[k] for k in _SBC_SET_INDEX_KEYS if k in st}
    # Group rewards must be present in the set index for the retail left-hand
    # reward panel, but keep only the actual FIFA Award contract here. Full
    # display metadata remains on the per-set/per-challenge response.
    if isinstance(out.get('awards'),list):
        compact=[]
        for a in out['awards']:
            if not isinstance(a,dict):continue
            typ=str(a.get('type','')).lower()
            if typ not in _SBC_CLIENT_AWARD_TYPES:continue
            row={k:a[k] for k in ('value','type','halId','count','isUntradeable','loan','loanType') if k in a}
            if typ=='item':
                d=a.get('itemData') if isinstance(a.get('itemData'),dict) else {}
                # The set list only needs the dynamic identity required by
                # Reward -> Item.createItem.  Keep the richer player object on
                # the per-set/challenge response so the initial SBC tile stays
                # within the retail-sized response envelope.
                if not int(d.get('resourceId',0) or 0) or 'loans' not in d:continue
                row['itemData']={k:d[k] for k in (
                    'id','itemId','assetId','definitionId','resourceId','resourceGameYear',
                    'itemType','cardsubtypeid','rating','rareflag','preferredPosition',
                    'teamid','leagueId','nation','formation','untradeable','owners',
                    'loans','contract','injuryGames','injuryType','fitness','morale',
                    'attributeArray','attributeList','skillmoves','weakfootabilitytypecode',
                    'attackingworkrate','defensiveworkrate','preferredfoot'
                ) if k in d}
            compact.append(row)
        out['awards']=compact
    return out

def _sbc_all_preview(sets,limit=SBC_ALL_PREVIEW_LIMIT):
    # Put permanent/core content first. The complete archive remains available
    # in the individual category tabs, while All stays close to the number of
    # simultaneously-active sets the retail UI was designed to render.
    wanted=('Basic','Advanced','Marquee Matchups','POTM','Prime ICONS','Leagues','Upgrades')
    out=[]
    for cat in wanted:
        for st in sets:
            if st.get('category')!=cat:continue
            out.append(st)
            if len(out)>=int(limit):return out
    return out

def _sbc_sets_payload(favourites_only=False):
    # Keep the complete historical archive compiled and addressable internally,
    # but do not expose historical Live sets to the retail FIFA 18 UI.  Even a
    # 54-set Live snapshot still crashes the PC client while tabbing.  Live can
    # be reintroduced later as a true date-filtered/current-event feed.
    archived=_sbc_compile_sets();archive_count=len(archived);fav=_sbc_favourite_ids()
    full=[x for x in archived if str(x.get('category','')) in SBC_VISIBLE_CATEGORY_ORDER]
    if favourites_only:full=[x for x in full if int(x['id']) in fav]
    summaries=[_sbc_set_index_summary(x) for x in full]
    by_id={int(x.get('id',0)):x for x in summaries}
    by_cat={cat:[] for cat in SBC_VISIBLE_CATEGORY_ORDER}
    for st in full:
        row=by_id.get(int(st.get('id',0)))
        if row is not None:by_cat.setdefault(str(st.get('category','')),[]).append(row)
    if favourites_only:
        preview=list(summaries)
    else:
        preview_ids={int(x.get('id',0)) for x in _sbc_all_preview(full)}
        preview=[x for x in summaries if int(x.get('id',0)) in preview_ids]
    categories=[]
    for cat in SBC_VISIBLE_CATEGORY_ORDER:
        catsets=by_cat.get(cat,[])
        categories.append({'id':SBC_CATEGORY_IDS[cat],'categoryId':SBC_CATEGORY_IDS[cat],'name':cat,
                           'priority':SBC_CATEGORY_IDS[cat],'sets':catsets,'setIds':[x['setId'] for x in catsets],
                           'displayable':True,'isAll':False,'isFavourite':False,'type':0})
    categories.insert(0,{'id':1,'categoryId':1,'name':'All','priority':1,'sets':preview,'setIds':[x['setId'] for x in preview],
                         'displayable':True,'isAll':True,'isFavourite':False,'type':1})
    # Crucially, allSetIds/catalogCount describe only the client-visible set
    # catalogue.  Supplying hidden Live IDs here still caused the frontend to
    # retain/instantiate historical content even without a Live category.
    visible_ids=[x['setId'] for x in summaries]
    return {'categories':categories,'sets':preview,'count':len(preview),'catalogCount':len(summaries),
            'archiveCatalogCount':archive_count,'visibleCatalogCount':len(summaries),
            'allSetIds':visible_ids,'endOfList':True,'timestamp':int(time.time()),
            'archiveSource':str((_sbc_archive_doc() or {}).get('source','bundled'))}

def _sbc_find_set_or_challenge(identifier):
    try:identifier=int(identifier)
    except Exception:return None,None
    for st in _sbc_compile_sets():
        if int(st['id'])==identifier:return st,None
        for ch in st.get('challenges',[]):
            if int(ch.get('id',0))==identifier:return st,ch
    return None,None

def _sbc_category_for_set(st):
    if not isinstance(st,dict):return 'Live'
    return str(st.get('category','Live') or 'Live')

def _sbc_native_eligibilities(st,ch=None):
    if isinstance(ch,dict) and isinstance(ch.get('eligibilities'),list):return [dict(x) for x in ch['eligibilities']]
    return _sbc_native_eligibilities_for((ch or {}).get('requirementsText',[]) if isinstance(ch,dict) else [])

def _sbc_native_challenge(st,ch):
    """FIFA 18 legacy /sbs/setId/<id>/challenges projection.

    Keep this deliberately close to preserved EA responses.  In particular,
    elgReq.type is a semantic requirement name and reduced-player challenges
    are BRICK_CHALLENGE, not OPEN_CHALLENGE.
    """
    if not st or not ch:return None
    cid=int(ch.get('id',ch.get('challengeId',0)) or 0);sid=int(st.get('id',st.get('setId',0)) or 0)
    progress=_sbc_progress_map().get(cid,{}) or {};req=list(ch.get('requirementsText',[]) or [])
    players,_,_=_sbc_challenge_metrics(req);times=int(progress.get('timesCompleted',1 if progress.get('completed') else 0) or 0)
    awards=_sbc_awards_for_display(ch.get('awards',[]),str(st.get('name','')))
    ctype='BRICK_CHALLENGE' if int(players)<11 else 'OPEN_CHALLENGE'
    return {
        'name':str(ch.get('name',st.get('name','Squad Building Challenge'))),
        'priority':int(ch.get('priority',1) or 1),
        'status':'COMPLETED' if times else 'NOT_STARTED',
        'setId':sid,
        'description':str(ch.get('description','Complete the squad challenge.')),
        'challengeId':cid,
        'endTime':0,
        'repeatable':bool(ch.get('repeatable',st.get('repeatable',False))),
        'formation':str(ch.get('formation','f442') or 'f442'),
        'timesCompleted':times,
        'elgReq':_sbc_legacy_elg_req(req),
        'elgOperation':'AND',
        'awards':[dict(x) for x in awards],
        'tutorial':int(ch.get('tutorial',0) or 0),
        'type':ctype,
        'challengeImageId':str(ch.get('challengeImageId',sid)),
        # Safe text aliases retained for this native PC client; unlike v36.2/3
        # they are not substituted for elgReq and do not carry numeric keys.
        'elgDesc':list(req),
    }

def _sbc_squad_load(cid):
    try:cid=int(cid)
    except Exception:return None
    with _DB_LOCK,_db_connect() as con:
        row=con.execute('SELECT data FROM sbc_squads WHERE challenge_id=?',(cid,)).fetchone()
    if row:
        try:
            doc=json.loads(row['data'])
            if isinstance(doc,dict):return doc
        except Exception:pass
    return None

def _sbc_extract_slots(doc):
    src=doc if isinstance(doc,dict) else {}
    if isinstance(src.get('squad'),dict):src=src['squad']
    rows=src.get('players',[]) if isinstance(src,dict) else []
    if not isinstance(rows,list):rows=[]
    owned=_db_item_map();slots=[0]*11
    for n,row in enumerate(rows[:64]):
        if not isinstance(row,dict):continue
        try:idx=int(row.get('index',n))
        except Exception:idx=n
        if not 0<=idx<11:continue
        item=row.get('itemData',row.get('item',row));item=item if isinstance(item,dict) else {}
        try:iid=int(item.get('id',item.get('itemId',0)) or 0)
        except Exception:iid=0
        if iid and iid in owned and str(owned[iid].get('itemType','')).lower()=='player':slots[idx]=iid
        elif iid==0:slots[idx]=0
    return slots

def _sbc_doc_rating_chemistry(doc,slots=None):
    src=doc if isinstance(doc,dict) else {}
    if isinstance(src.get('squad'),dict):src=src['squad']
    rating=_ival(src.get('rating',src.get('squadRating',0)),0) if isinstance(src,dict) else 0
    chem=_ival(src.get('chemistry',src.get('teamChemistry',src.get('squadChemistry',0))),0) if isinstance(src,dict) else 0
    if not rating and slots:
        owned=_db_item_map();vals=[int(owned.get(int(i),{}).get('rating',0) or 0) for i in slots if int(i or 0)>0]
        if vals:rating=int(round(sum(vals)/len(vals)))
    return rating,chem

def _sbc_squad_save(cid,slots,rating=0,chemistry=0):
    cid=int(cid);slots=list(slots or [])[:11]+[0]*max(0,11-len(list(slots or [])[:11]))
    doc={'challengeId':cid,'formation':'f442','slots':[int(x or 0) for x in slots[:11]],'rating':int(rating or 0),'chemistry':int(chemistry or 0),'updatedAt':int(time.time())}
    with _DB_LOCK,_db_connect() as con:
        con.execute('INSERT OR REPLACE INTO sbc_squads(challenge_id,data) VALUES(?,?)',(cid,json.dumps(doc,separators=(',',':'))))
    return doc

def _sbc_empty_item(item_type='player'):
    return {'id':0,'timestamp':0,'formation':'any','untradeable':False,'assetId':0,'rating':0,'itemType':str(item_type),'resourceId':0,
        'owners':0,'discardValue':0,'itemState':'invalid','cardsubtypeid':0,'lastSalePrice':0,'morale':0,'fitness':0,'injuryType':'none',
        'injuryGames':0,'preferredPosition':'any','statsList':[],'lifetimeStats':[],'training':0,'contract':0,'suspension':0,
        'attributeList':[],'teamid':0,'rareflag':0,'loyaltyBonus':1,'pile':0,'nation':0,'resourceGameYear':2019}

def _sbc_squad_payload(cid):
    st,ch=_sbc_find_set_or_challenge(cid)
    if ch is None and st and st.get('challenges'):ch=st['challenges'][0]
    if not st or not ch:return {'challengeId':int(cid),'playerRequirements':[],'squad':{'id':1,'formation':'f442','rating':0,'chemistry':0,'manager':[_sbc_empty_item('manager')],'players':[]}}
    cid=int(ch['id']);saved=_sbc_squad_load(cid) or {};slots=list(saved.get('slots',[]))[:11];slots += [0]*(11-len(slots));owned=_db_item_map();players=[]
    for idx in range(23):
        iid=int(slots[idx] or 0) if idx<11 else 0;item=dict(owned.get(iid,{})) if iid else _sbc_empty_item('player')
        if iid:item['id']=iid;item.setdefault('itemType','player');item.setdefault('itemState','active');item.setdefault('resourceGameYear',2018)
        players.append({'index':idx,'itemData':item})
    squad={'id':1,'formation':str(ch.get('formation','f442') or 'f442'),'rating':int(saved.get('rating',0) or 0),'chemistry':int(saved.get('chemistry',0) or 0),
           'manager':[_sbc_empty_item('manager')],'players':players}
    req_count=max(1,min(11,int(ch.get('playerCount',_sbc_challenge_metrics(ch.get('requirementsText',[]))[0]) or 11)))
    player_req=[{'index':i,'playerType':'DEFAULT' if i<req_count else 'BRICK'} for i in range(11)]
    return {'challengeId':cid,'playerRequirements':player_req,'squad':squad}

def _sbc_squad_update(cid,doc):
    st,ch=_sbc_find_set_or_challenge(cid)
    if ch is None and st and st.get('challenges'):ch=st['challenges'][0]
    if not st or not ch:return {'challengeId':int(cid),'playerRequirements':[],'squad':{'players':[]}}
    cid=int(ch['id']);slots=_sbc_extract_slots(doc);rating,chem=_sbc_doc_rating_chemistry(doc,slots);_sbc_squad_save(cid,slots,rating,chem)
    out=_sbc_squad_payload(cid);log.warning('SBC SQUAD SAVE challenge=%s populated=%d rating=%d chemistry=%d slots=%s',cid,sum(1 for x in slots if x),rating,chem,slots);return out

def _extract_item_ids(obj):
    found=[]
    def walk(x):
        if isinstance(x,dict):
            for k,v in x.items():
                if str(k).lower() in ('id','itemid'):
                    try:
                        n=int(v)
                        if n>=700000000000:found.append(n)
                    except Exception:pass
                else:walk(v)
        elif isinstance(x,list):
            for v in x:walk(v)
    walk(obj);return list(dict.fromkeys(found))

def _sbc_starting_xi_ids(obj):
    """Return owned item ids from squad indices 0..10 only.

    FIFA 18 may include bench/reserve slots (11..22) in an SBC squad PUT. Those
    items are not part of the submitted XI and must never be consumed.
    """
    src=obj if isinstance(obj,dict) else {}
    if isinstance(src.get('squad'),dict):src=src['squad']
    rows=src.get('players',[]) if isinstance(src,dict) else []
    if not isinstance(rows,list):rows=[]
    by_index={}
    for n,row in enumerate(rows[:64]):
        if not isinstance(row,dict):continue
        try:idx=int(row.get('index',n))
        except Exception:idx=n
        if not 0<=idx<11:continue
        item=row.get('itemData',row.get('item',row));item=item if isinstance(item,dict) else {}
        try:iid=int(item.get('id',item.get('itemId',0)) or 0)
        except Exception:iid=0
        if iid>=700000000000:by_index[idx]=iid
    out=[]
    seen=set()
    for idx in range(11):
        iid=int(by_index.get(idx,0) or 0)
        if iid and iid not in seen:
            out.append(iid);seen.add(iid)
    return out

def _sbc_level(item):
    r=int(item.get('rating',0) or 0)
    return 'gold' if r>=75 else 'silver' if r>=65 else 'bronze'

def _sbc_item_league_token(item):
    """Return the best league identity available for an owned SBC item.

    Old club rows can have leagueId=0 even though FIFA's installed static DB knows
    the league. Prefer the repaired club mapping; only use a stable synthetic club
    token as a last resort so missing metadata cannot collapse several visibly
    different teams into one anonymous league bucket during server-side checking.
    """
    try: league=int(item.get('leagueId',0) or 0)
    except Exception: league=0
    if league>0:return league
    try: team=int(item.get('teamid',item.get('teamId',0)) or 0)
    except Exception: team=0
    mapped=int(LEAGUE_BY_CLUB.get(team,0) or 0)
    if mapped>0:return mapped
    return -team if team>0 else 0


def _sbc_named_group_count(items,name):
    raw=str(name or '').strip()
    parts=[_sbc_norm_name(z) for z in re.split(r'\s*\+\s*',raw) if str(z).strip()]
    if not parts:return 0
    matches=[];kinds=[]
    for target in parts:
        nation_id=_sbc_named_nation_id(target)
        league_id=_sbc_named_league_id(target)
        club_id=_sbc_named_club_id(target)
        idx=set();kind='unknown'
        for i,x in enumerate(items):
            try:item_nation=int(x.get('nation',x.get('nationId',0)) or 0)
            except Exception:item_nation=0
            try:item_team=int(x.get('teamid',x.get('teamId',0)) or 0)
            except Exception:item_team=0
            item_league=_sbc_item_league_token(x)
            club=_sbc_norm_name(x.get('clubName',''))
            nat=_sbc_norm_name(x.get('nationality',''))
            league=_sbc_norm_name(x.get('leagueName',''))
            if nation_id and item_nation==nation_id:idx.add(i);kind='nation';continue
            if league_id and item_league==league_id:idx.add(i);kind='league';continue
            if club_id and item_team==club_id:idx.add(i);kind='club';continue
            if target and (target==league or (league and target in league)):idx.add(i);kind='league';continue
            if target and (target==nat or (nat and target in nat)):idx.add(i);kind='nation';continue
            if target and (target==club or (club and target in club)):idx.add(i);kind='club';continue
        matches.append(idx);kinds.append(kind)
    if len(matches)==1:return len(matches[0])
    known={k for k in kinds if k!='unknown'}
    # Two clubs joined by '+' means either club. Nation+league means a player
    # matching both attributes (e.g. France + LaLiga Santander).
    if len(known)==1:return len(set().union(*matches))
    return len(set.intersection(*matches)) if all(matches) else 0

def _sbc_check_number(value,op,wanted):
    return value<=wanted if op=='MAX' else value==wanted if op=='EXACT' else value>=wanted

def _sbc_validate_requirements(ch,doc,submitted):
    owned=_db_item_map();items=[owned.get(int(i),{}) for i in submitted if int(i) in owned]
    saved=_sbc_squad_load(int(ch['id'])) or {};rating,chem=_sbc_doc_rating_chemistry(doc,submitted)
    if not rating:rating=int(saved.get('rating',0) or 0)
    if not chem:chem=int(saved.get('chemistry',0) or 0)
    failures=[]
    for raw in ch.get('requirementsText',[]) or []:
        line=str(raw);low=line.lower().replace('–','-');op=_sbc_rule_op(line);effective_op=op;n=_sbc_num(line,0)
        if not n and not any(z in low for z in ('gold','silver','bronze')):continue
        ok=True;actual=None
        if 'players in the squad' in low or 'number of players in the squad' in low or low.strip().startswith('number of players:'):
            # FIFA SBC text without an explicit min/max qualifier means an exact
            # squad size (e.g. "Number of Players in the Squad: 2").
            count_op=op if any(k in low for k in ('min','max','at least','at most','exact')) else 'EXACT'
            effective_op=count_op;actual=len(items);ok=_sbc_check_number(actual,count_op,n)
        elif 'squad rating' in low or 'team rating' in low or 'team overall rating' in low or 'squad overall rating' in low:
            actual=rating;ok=(actual==0) or _sbc_check_number(actual,op,n)
        elif 'team chemistry' in low or 'squad chemistry' in low:
            actual=chem;ok=(actual==0) or _sbc_check_number(actual,op,n)
        elif 'same nation count' in low:
            from collections import Counter
            actual=max(Counter(int(x.get('nation',0) or 0) for x in items if int(x.get('nation',0) or 0)>0).values() or [0]);ok=_sbc_check_number(actual,op,n)
        elif 'same league count' in low:
            from collections import Counter
            actual=max(Counter(_sbc_item_league_token(x) for x in items if _sbc_item_league_token(x)!=0).values() or [0]);ok=_sbc_check_number(actual,op,n)
        elif 'same club count' in low:
            from collections import Counter
            actual=max(Counter(int(x.get('teamid',x.get('teamId',0)) or 0) for x in items if int(x.get('teamid',x.get('teamId',0)) or 0)>0).values() or [0]);ok=_sbc_check_number(actual,op,n)
        elif 'players from the same nation' in low or 'players from same nation' in low or re.search(r'\b(?:max|min|exact(?:ly)?)\s*\d+\s+players?\s+from\s+(?:the\s+)?same\s+nation\b',low):
            from collections import Counter
            actual=max(Counter(int(x.get('nation',0) or 0) for x in items if int(x.get('nation',0) or 0)>0).values() or [0]);ok=_sbc_check_number(actual,op,n)
        elif 'players from the same league' in low or 'players from same league' in low or re.search(r'\b(?:max|min|exact(?:ly)?)\s*\d+\s+players?\s+from\s+(?:the\s+)?same\s+league\b',low):
            from collections import Counter
            actual=max(Counter(_sbc_item_league_token(x) for x in items if _sbc_item_league_token(x)!=0).values() or [0]);ok=_sbc_check_number(actual,op,n)
        elif 'players from the same club' in low or 'players from same club' in low or re.search(r'\b(?:max|min|exact(?:ly)?)\s*\d+\s+players?\s+from\s+(?:the\s+)?same\s+club\b',low):
            from collections import Counter
            actual=max(Counter(int(x.get('teamid',x.get('teamId',0)) or 0) for x in items if int(x.get('teamid',x.get('teamId',0)) or 0)>0).values() or [0]);ok=_sbc_check_number(actual,op,n)
        elif ('different leagues' in low or re.match(r'^\s*leagues\s*:',low)):
            actual=len({_sbc_item_league_token(x) for x in items if _sbc_item_league_token(x)!=0});ok=_sbc_check_number(actual,op,n)
        elif ('different nations' in low or re.match(r'^\s*nations\s*:',low) or 'nationalities:' in low):
            actual=len({int(x.get('nation',0) or 0) for x in items if int(x.get('nation',0) or 0)>0});ok=_sbc_check_number(actual,op,n)
        elif ('different clubs' in low or re.match(r'^\s*clubs\s*:',low)):
            actual=len({int(x.get('teamid',x.get('teamId',0)) or 0) for x in items if int(x.get('teamid',x.get('teamId',0)) or 0)>0});ok=_sbc_check_number(actual,op,n)
        elif 'rare players' in low or re.match(r'^\s*rare\s*:',low):
            actual=sum(1 for x in items if int(x.get('rareflag',x.get('rareFlag',0)) or 0)>0);ok=_sbc_check_number(actual,op,n)
        elif 'totw' in low or 'team of the week' in low:
            actual=sum(1 for x in items if str(x.get('specialType','')).upper()=='TOTW');ok=_sbc_check_number(actual,op,n or 1)
        elif 'tots' in low or 'team of the season' in low:
            actual=sum(1 for x in items if str(x.get('specialType','')).upper()=='TOTS');ok=_sbc_check_number(actual,op,n or 1)
        elif 'icon player' in low or 'icon players' in low:
            actual=sum(1 for x in items if int(x.get('leagueId',0) or 0)==2118);ok=_sbc_check_number(actual,op,n or 1)
        elif 'player level' in low and 'gold' in low:
            actual=sum(1 for x in items if _sbc_level(x)=='gold');ok=actual==len(items) if 'exact' in low else actual>=n
        elif 'player level' in low and 'silver' in low:
            actual=sum(1 for x in items if _sbc_level(x)=='silver');ok=actual==len(items) if 'exact' in low else actual>=n
        elif 'player level' in low and 'bronze' in low:
            actual=sum(1 for x in items if _sbc_level(x)=='bronze');ok=actual==len(items) if 'exact' in low else actual>=n
        elif 'gold players' in low:
            actual=sum(1 for x in items if _sbc_level(x)=='gold');ok=_sbc_check_number(actual,op,n or len(items))
        elif 'silver players' in low:
            actual=sum(1 for x in items if _sbc_level(x)=='silver');ok=_sbc_check_number(actual,op,n or len(items))
        elif 'bronze players' in low:
            actual=sum(1 for x in items if _sbc_level(x)=='bronze');ok=_sbc_check_number(actual,op,n or len(items))
        else:
            group=''
            m=re.search(r'players?\s+from\s+(.+?)\s*:\s*(?:min\.?|max\.?|exact|exactly)',line,re.I)
            if m:group=m.group(1).strip()
            if not group:
                m=re.search(r'(?:min\.?|max\.?|exactly?)\s*\d+\s+players?\s+from\s+(.+)$',line,re.I)
                if m:group=m.group(1).strip()
            if not group:
                m=re.search(r'^(.+?)\s+players?\s*:\s*(?:min\.?|max\.?|exact|exactly)',line,re.I)
                if m:group=m.group(1).strip()
            if group:
                actual=_sbc_named_group_count(items,group);ok=_sbc_check_number(actual,op,n or 1)
        if not ok:failures.append({'requirement':line,'actual':actual,'required':n,'operation':effective_op})
    return failures

def _sbc_grant_item_reward(award,set_name=''):
    raw=str(award.get('historicalReward',award.get('name','')) or '');d=_sbc_find_reward_player(raw,set_name)
    if not d:return []
    count=max(1,int(award.get('count',1) or 1));ids=[]
    for _ in range(count):
        # Set-level player rewards must enter FIFA's normal unassigned/new-item
        # flow. The client can then present the reward and let the user Store in
        # Club, instead of the server silently depositing it into My Club.
        item=_definition_item(d,pile=6,item_id=_next_item_id());item['untradeable']=True;item['tradeable']=False;item['acquisitionSource']='SBC_REWARD';item['sourceEligibility']='SBC_REWARD';item['itemState']='free';item['newItem']=True
        _save_item(item);ids.append(int(item['id']))
    return ids

def _sbc_grant_awards(awards,set_name=''):
    granted_packs=[];granted_items=[];coins=0
    for a in awards or []:
        typ=str(a.get('type','')).lower();qty=max(1,int(a.get('count',1) or 1))
        if typ=='coin':coins += int(a.get('coins',a.get('value',0)) or 0)*qty
        elif typ=='pack':
            pid=int(a.get('packId',a.get('value',0)) or 0)
            if pid and _grant_owned_pack(pid,qty):granted_packs.extend([pid]*qty)
        elif typ=='item':granted_items.extend(_sbc_grant_item_reward(a,set_name))
    if coins:_meta_set('credits',_credits()+coins)
    return coins,granted_packs,granted_items

def _sbc_group_reward_claimed_ids():
    try:v=json.loads(_meta_get('sbcGroupRewardSetIds','[]') or '[]')
    except Exception:v=[]
    return {int(x) for x in v if str(x).isdigit()}

def _sbc_mark_group_reward_claimed(sid):
    ids=_sbc_group_reward_claimed_ids();ids.add(int(sid));_meta_set('sbcGroupRewardSetIds',json.dumps(sorted(ids),separators=(',',':')))

def _sbc_consume_submitted_items(item_ids):
    ids=[]
    for x in item_ids or []:
        try:
            n=int(x)
            if n>0:ids.append(n)
        except Exception:pass
    ids=list(dict.fromkeys(ids))
    if not ids:return 0
    # Validate the complete ownership set before deleting anything.  This keeps
    # a malformed/replayed submission atomic: FIFA must never lose the valid
    # subset of a squad when another submitted id is stale or unowned.
    with _DB_LOCK,_db_connect() as con:
        marks=','.join('?' for _ in ids)
        rows=con.execute(f'SELECT id FROM items WHERE id IN ({marks})',tuple(ids)).fetchall()
        owned={int(r['id']) for r in rows}
        if owned != set(ids):return 0
        cur=con.execute(f'DELETE FROM items WHERE id IN ({marks})',tuple(ids))
        return int(cur.rowcount or 0)



def _sbc_reward_instances(item_ids):
    item_map=_db_item_map();out=[]
    for iid in item_ids or []:
        try:x=dict(item_map.get(int(iid),{}) or {})
        except Exception:x={}
        if not x:continue
        # Keep the reward item on FIFA's real unassigned/new-items pile.  The
        # pack wire projection is already known-good for the retail parser, but
        # SBC's completed-set screen also consumes pile/new-item ownership state
        # before it hands off to futNewItemsFlow.nav.
        if str(x.get('itemType','')).lower()=='player':
            row=_pack_wire_player(x)
            row.update({'pile':int(x.get('pile',6) or 6),'itemState':str(x.get('itemState','free') or 'free'),
                        'untradeable':bool(x.get('untradeable',True)),'newItem':bool(x.get('newItem',True)),
                        'acquisitionSource':str(x.get('acquisitionSource','SBC_REWARD') or 'SBC_REWARD'),
                        'sourceEligibility':str(x.get('sourceEligibility','SBC_REWARD') or 'SBC_REWARD')})
            out.append(row)
        else:
            row=dict(x);row.setdefault('pile',6);row.setdefault('itemState','free');row.setdefault('newItem',True)
            out.append(row)
    return out


def _sbc_awards_with_instances(display_awards,item_ids):
    ids=list(item_ids or []);instances=_sbc_reward_instances(ids);cursor=0;out=[]
    for award in display_awards or []:
        a=dict(award) if isinstance(award,dict) else award
        if isinstance(a,dict) and str(a.get('type','')).lower()=='item' and cursor<len(instances):
            item=dict(instances[cursor]);cursor+=1
            a['itemData']=item
            a['value']=int(item.get('resourceId',a.get('value',0)) or 0)
            a['resourceId']=int(item.get('resourceId',0) or 0)
            a['assetId']=int(item.get('assetId',0) or 0)
        out.append(a)
    return out

def _sbc_set_reward_record(sid):
    try:raw=json.loads(_meta_get(f'sbcSetReward:{int(sid)}','{}') or '{}')
    except Exception:raw={}
    if not isinstance(raw,dict):raw={}
    def ints(name):
        out=[]
        for x in raw.get(name,[]) or []:
            try:
                n=int(x)
                if n>0:out.append(n)
            except Exception:pass
        return out
    return {'itemIds':ints('itemIds'),'packIds':ints('packIds'),'completedAt':int(raw.get('completedAt',0) or 0)}


def _sbc_pending_reward_set_id():
    # Recover rewards created by v36.10 as well as rewards created by this build.
    # A completed set remains pending while at least one of its granted item
    # instances is still on pile 6 (unassigned/new items).
    item_map=_db_item_map();candidates=[]
    with _DB_LOCK,_db_connect() as con:
        rows=con.execute("SELECT key,value FROM meta WHERE key LIKE 'sbcSetReward:%'").fetchall()
    for row in rows:
        try:sid=int(str(row['key']).split(':',1)[1]);rec=json.loads(row['value'] or '{}')
        except Exception:continue
        ids=[]
        for x in (rec.get('itemIds',[]) if isinstance(rec,dict) else []) or []:
            try:ids.append(int(x))
            except Exception:pass
        pending=any(int(item_map.get(i,{}).get('pile',0) or 0)==6 for i in ids)
        # Pack-only set rewards are also valid completed-set rewards; retain the
        # newest record as a fallback if there is no item instance to test.
        packs=list((rec.get('packIds',[]) if isinstance(rec,dict) else []) or [])
        if pending or (not ids and packs):candidates.append((int((rec or {}).get('completedAt',0) or 0),sid))
    return max(candidates)[1] if candidates else 0


def _sbc_set_rewards_payload(sid=0):
    try:sid=int(sid or 0)
    except Exception:sid=0
    if sid<=0:sid=_sbc_pending_reward_set_id()
    st,_=_sbc_find_set_or_challenge(sid) if sid else (None,None)
    if not st:
        return {'setId':sid,'sets':[],'awards':[],'rewards':[],'itemData':[],'itemList':[],'items':[],
                'itemIdList':[],'numberItems':0,'credits':_credits(),'unopenedPacks':_unopened_packs_payload()}
    rec=_sbc_set_reward_record(sid);ids=rec['itemIds'];packs=rec['packIds']
    actual=_sbc_awards_with_instances(list(st.get('awards',[]) or []),ids)
    rows=_sbc_reward_instances(ids)
    snap=_sbc_set_index_summary(st)
    snap.update({'status':'COMPLETED' if int(st.get('timesCompleted',0) or 0)>0 else 'IN_PROGRESS',
                 'awards':actual,'rewards':actual})
    return {'setId':sid,'completedSetId':sid,'status':snap['status'],'setCompleted':snap['status']=='COMPLETED',
            'set':snap,'completedSet':snap,'sbcSet':snap,'setData':snap,'sets':[snap],
            'awards':actual,'rewards':actual,'setAwards':actual,'groupAwards':actual,'groupRewards':actual,
        'grantedChallengeAwards':[],'grantedSetAwards':actual,'grantedAwards':actual,
            'itemData':rows,'itemList':rows,'items':rows,'newItems':rows,'itemIdList':[int(x.get('id',0) or 0) for x in rows],
            'numberItems':len(rows),'count':len(rows),'newcards':len(rows),'rewardItems':ids,'groupRewardItems':ids,
            'rewardPacks':packs,'groupRewardPacks':packs,'credits':_credits(),'unopenedPacks':_unopened_packs_payload(),
            'completedAt':int(rec.get('completedAt',0) or 0)}


def _sbc_submit(cid,doc):
    st,ch=_sbc_find_set_or_challenge(cid)
    if ch is None and st and st.get('challenges'):ch=st['challenges'][0]
    if not st or not ch:return {'success':False,'error':'SBC_NOT_FOUND','status':404}
    cid=int(ch['id']);sid=int(st['id']);repeatable=bool(ch.get('repeatable') or st.get('repeatable'));progress=_sbc_progress_map();old=progress.get(cid,{}) or {}
    previous_times=int(old.get('timesCompleted',1 if old.get('completed') else 0) or 0);already=previous_times>0 and not repeatable
    submitted=_sbc_starting_xi_ids(doc)
    if not submitted:
        saved=_sbc_squad_load(cid) or {};submitted=[int(x) for x in saved.get('slots',[]) if int(x or 0)>0]
        doc={'players':[{'index':i,'itemData':{'id':iid}} for i,iid in enumerate(submitted)],'rating':saved.get('rating',0),'chemistry':saved.get('chemistry',0)}
    required=int(ch.get('playerCount',11) or 11)
    unique_submitted=list(dict.fromkeys(int(x) for x in submitted if int(x or 0)>0))
    if not already and len(unique_submitted) < required:
        return {'success':False,'challengeId':cid,'setId':sid,'reason':'INCOMPLETE_SQUAD','code':461,
                'requiredPlayers':required,'submittedPlayers':len(unique_submitted),'credits':_credits()}
    failures=[] if already else _sbc_validate_requirements(ch,doc,submitted)
    if failures:
        log.warning('SBC SUBMIT REJECT challenge=%s failures=%s',cid,failures[:8])
        return {'success':False,'challengeId':cid,'setId':sid,'reason':'ELIGIBILITY_REQUIREMENTS_NOT_MET','code':461,
                'requirementsFailed':failures,'credits':_credits()}

    display_awards=list(ch.get('awards',[]) or []);grant_awards=list(ch.get('grantAwards',display_awards) or [])
    coins=0;granted_packs=[];granted_items=[];group_awards=[];group_item_ids=[];group_pack_ids=[];consumed=0;times=previous_times
    if not already:
        consumed=_sbc_consume_submitted_items(submitted)
        if consumed < required:
            log.warning('SBC SUBMIT ABORT challenge=%s consumed=%d required=%d',cid,consumed,required)
            return {'success':False,'challengeId':cid,'setId':sid,'reason':'SUBMITTED_ITEMS_NOT_OWNED','code':461,'credits':_credits()}
        coins,granted_packs,granted_items=_sbc_grant_awards(grant_awards,str(st.get('name','')));times=previous_times+1
        rec={'completed':True,'timesCompleted':times,'completedAt':int(time.time()),'submittedItemIds':submitted,'coinsAwarded':coins,
             'grantedPackIds':granted_packs,'grantedItemIds':granted_items,'rewardGranted':True}
        with _DB_LOCK,_db_connect() as con:
            con.execute('INSERT OR REPLACE INTO sbc_progress(challenge_id,data) VALUES(?,?)',(cid,json.dumps(rec,separators=(',',':'))))
            con.execute('DELETE FROM sbc_squads WHERE challenge_id=?',(cid,))
        done=_sbc_progress_map();all_done=all(int(x['id']) in done for x in st.get('challenges',[]))
        if all_done:
            if repeatable and len(st.get('challenges',[]))==1:
                group_awards=list(st.get('awards',[]) or []);group_grant_awards=list(st.get('grantAwards',group_awards) or [])
                gc,gp,gi=_sbc_grant_awards(group_grant_awards,str(st.get('name','')));coins+=gc;granted_packs+=gp;granted_items+=gi;group_pack_ids+=gp;group_item_ids+=gi
            elif sid not in _sbc_group_reward_claimed_ids():
                group_awards=list(st.get('awards',[]) or []);group_grant_awards=list(st.get('grantAwards',group_awards) or [])
                gc,gp,gi=_sbc_grant_awards(group_grant_awards,str(st.get('name','')));coins+=gc;granted_packs+=gp;granted_items+=gi;group_pack_ids+=gp;group_item_ids+=gi;_sbc_mark_group_reward_claimed(sid)
                _meta_set(f'sbcSetReward:{sid}',json.dumps({'itemIds':group_item_ids,'packIds':group_pack_ids,'completedAt':int(time.time())},separators=(',',':')))
    else:
        granted_packs=list(old.get('grantedPackIds',[]) or []);granted_items=list(old.get('grantedItemIds',[]) or [])
        done=_sbc_progress_map();all_done=all(int(x['id']) in done for x in st.get('challenges',[]))

    # A replay of the final non-repeatable challenge must still describe the
    # already-completed set so the UI cannot fall back into a resubmission loop.
    if all_done and not group_awards:
        group_awards=list(st.get('awards',[]) or [])
        try:set_reward=json.loads(_meta_get(f'sbcSetReward:{sid}','{}') or '{}')
        except Exception:set_reward={}
        group_item_ids=[int(x) for x in (set_reward.get('itemIds',[]) or []) if str(x).isdigit()]
        group_pack_ids=[int(x) for x in (set_reward.get('packIds',[]) or []) if str(x).isdigit()]

    # Recompile after writing progress.  The pre-submit `st` object still says
    # the final challenge/set is incomplete; returning that stale object is what
    # made futSbcSquadsFlow.nav emit `back` instead of `sbcSetRewards`.
    latest_st,latest_ch=_sbc_find_set_or_challenge(cid)
    if not latest_st:latest_st=st
    if not latest_ch:latest_ch=ch
    completed_count=int(latest_st.get('challengesCompletedCount',sum(1 for x in latest_st.get('challenges',[]) if int(x['id']) in _sbc_progress_map())) or 0)
    challenge_count=int(latest_st.get('challengesCount',len(latest_st.get('challenges',[]) or [])) or 0)
    actual_group_awards=_sbc_awards_with_instances(group_awards,group_item_ids)
    reward_instances=_sbc_reward_instances(group_item_ids)
    headline=reward_instances[0] if reward_instances else None
    unopened=_unopened_packs_payload();unopened_count=int(unopened.get('count',0) or 0);pending_count=len(_pending_items())
    walkout=bool(all_done and headline and str(headline.get('itemType','')).lower()=='player')
    walkout_rating=int((headline or {}).get('rating',0) or 0);walkout_asset=int((headline or {}).get('assetId',0) or 0);walkout_id=int((headline or {}).get('id',0) or 0)

    completed_set=_sbc_set_index_summary(latest_st)
    completed_set['status']='COMPLETED' if all_done else 'IN_PROGRESS'
    completed_set['challengesCompletedCount']=completed_count
    completed_set['challengesCount']=challenge_count
    completed_set['timesCompleted']=max(int(completed_set.get('timesCompleted',0) or 0),1 if all_done else 0)
    if all_done:
        # Mode 2 of futsbchubviewmodel renders FUT_SBS_SETS_REWARDS from the
        # completed set's native awards array.  Replace the static preview with
        # the real instance-backed award so selecting Continue can hand the same
        # item id to futNewItemsFlow.nav.
        completed_set['awards']=actual_group_awards
        completed_set['rewards']=actual_group_awards
    native_challenge=_sbc_native_challenge(latest_st,latest_ch) or {}
    # Retail SBC completion has two *grant* channels.  The challenge award is
    # what drives the normal "Challenge Complete" reward notification (usually
    # a pack), while grantedSetAwards is what makes the group flow transition to
    # futSetsCompletedRewards when the final challenge finishes.  v36.11 put the
    # group reward in generic `awards`; FIFA accepted the grant but treated it as
    # static preview data and simply navigated back to the SBC hub.
    response_awards=display_awards
    # Only a fresh submit grants the challenge reward.  Replays remain a set-reward
    # recovery description and must not make FIFA announce the same pack twice.
    granted_challenge_awards=[dict(x) for x in display_awards] if not already else []
    granted_set_awards=[dict(x) for x in actual_group_awards] if all_done else []
    granted_awards=granted_challenge_awards + granted_set_awards

    log.warning('SBC SUBMIT challenge=%s set=%s submitted=%d consumed=%d repeatable=%s already=%s times=%d coins=%d packs=%s items=%s groupReward=%s setCompleted=%s unassigned=%d',cid,latest_st.get('name'),len(submitted),consumed,repeatable,already,times,coins,granted_packs,granted_items,bool(group_awards),all_done,pending_count)
    if all_done:
        log.warning('SBC SET REWARD READY set=%s timesCompleted=%s completed=%s/%s itemIds=%s packIds=%s rewardResourceIds=%s',sid,completed_set.get('timesCompleted'),completed_count,challenge_count,group_item_ids,group_pack_ids,[int(x.get('resourceId',x.get('value',0)) or 0) for x in actual_group_awards if isinstance(x,dict)])
    out={
        'success':True,'completed':True,'challengeCompleted':True,'status':'COMPLETED',
        'challengeId':cid,'setId':sid,'completedSetId':sid,'setName':str(latest_st.get('name','')),
        'timesCompleted':times,'repeatable':repeatable,
        'setCompleted':bool(all_done),'setComplete':bool(all_done),'allChallengesCompleted':bool(all_done),
        'setStatus':'COMPLETED' if all_done else 'IN_PROGRESS','setTimesCompleted':int(completed_set.get('timesCompleted',0) or 0),
        'challengesCompletedCount':completed_count,'challengesCount':challenge_count,
        'awards':response_awards,'rewards':response_awards,
        'challengeAwards':display_awards,'challengeRewards':display_awards,
        'grantedChallengeAwards':granted_challenge_awards,
        'grantedSetAwards':granted_set_awards,
        'grantedAwards':granted_awards,
        'groupAwards':actual_group_awards,'setAwards':actual_group_awards,'groupRewards':actual_group_awards,
        'challenge':native_challenge,'challengeData':native_challenge,
        'set':completed_set,'completedSet':completed_set,'sbcSet':completed_set,'setData':completed_set,
        'sets':[completed_set] if all_done else [],'completedSets':[completed_set] if all_done else [],
        'setsCompleted':[sid] if all_done else [],
        'credits':_credits(),'unopenedPacks':unopened,'unopenedPackCount':unopened_count,'myPacksCount':unopened_count,'recoveredPackCount':unopened_count,
        'rewardPacks':granted_packs,'groupRewardPacks':group_pack_ids,'rewardItems':granted_items,'groupRewardItems':group_item_ids,
        'itemData':reward_instances,'items':reward_instances,'itemList':reward_instances,'newItems':reward_instances,
        'itemIdList':[int(x.get('id',0) or 0) for x in reward_instances],'numberItems':len(reward_instances),'newcards':len(reward_instances),
        'unassignedPileSize':pending_count,'consumedItemCount':consumed,
    }
    if walkout:
        out.update({'walkout':True,'isWalkout':True,'IS_PLAYER_WALKOUT':True,'PlayerDoWalkOut':True,'PlayerWalkOutType':2,
                    'walkOutType':2,'walkOutPlayerType':2,'walkoutItemId':walkout_id,'walkoutAssetId':walkout_asset,'walkoutRating':walkout_rating,
                    'highestRatedItemId':walkout_id,'highestRatedAssetId':walkout_asset,'highestRatedRating':walkout_rating,
                    'walkOutList':[walkout_asset] if walkout_asset else []})
    return out

# ---- Local Transfer Market AI --------------------------------------------
_MARKET_LOCK=threading.RLock()
_AI_AUCTIONS={}
_MARKET_TARGETS={}

def _qint(query,names,default=0):
    for name in names:
        vals=query.get(name) or query.get(name.lower())
        if vals:
            try:return int(float(vals[0]))
            except Exception:pass
    return default

def _round_coins(v):
    v=max(150,int(v))
    step=50 if v<10000 else 100 if v<50000 else 250 if v<100000 else 500
    return max(step,(v//step)*step)

def _market_value(d):
    r=int(d.get('rating',50) or 50)
    if r<=64:base=400+(r-45)*20
    elif r<=74:base=800+(r-65)*180
    elif r<=79:base=1200+(r-75)*450
    elif r<=82:base=3500+(r-80)*1600
    elif r<=84:base=8000+(r-83)*4500
    elif r==85:base=18000
    elif r==86:base=32000
    elif r==87:base=52000
    elif r==88:base=85000
    elif r==89:base=130000
    elif r==90:base=210000
    else:base=300000+(r-91)*170000
    if _is_special_def(d):
        try:rf=int(d.get('rareflag',d.get('rareFlag',3)) or 3)
        except Exception:rf=3
        mult={3:1.15,5:1.75,11:1.6,21:1.2,22:1.2,24:1.3,28:1.4,30:1.35,32:1.35,34:1.35,35:1.5,39:1.3}.get(rf,1.2)
        base=int(base*mult)
    return _round_coins(base)

def _auction_payload(trade_id,item,start,buy,expires=1800,current=0,owner=False,state='active',bid_state='none',extra=None):
    now=int(time.time());tid=int(trade_id)
    out={'tradeId':tid,'tradeIdStr':str(tid),'itemData':dict(item),'tradeState':state,'bidState':bid_state,'buyNowPrice':int(buy),'currentBid':int(current),
         'offers':0,'watched':False,'expires':max(0,int(expires)),'startingBid':int(start),'confidenceValue':100,'timestamp':now,
         'tradeOwnerId':FAKE_PERSONA if owner else 9900000001,'tradeOwnerName':PERSONA if owner else 'Market AI','tradeOwnerEstablished':'2017','tradeOwner':bool(owner),
         'sellerId':FAKE_PERSONA if owner else 9900000001,'sellerName':PERSONA if owner else 'Market AI','sellerEstablished':'2017','coinsProcessed':0,'stale':False}
    if extra:out.update(extra)
    return out

def _auction_response(rows,success=True,**extra):
    credits=_credits()
    out={'auctionInfo':[dict(x) for x in (rows or [])],
         'bidTokens':{'count':0,'updateTime':0},
         'credits':credits,
         'currencies':[{'name':'coins','funds':credits,'finalFunds':credits},{'name':'points','funds':0,'finalFunds':0}],
         'duplicateItemIdList':[],
         'errorState':None,
         'success':bool(success)}
    out.update(extra)
    return out

def _ai_trade_id(asset,salt):
    return 880000000000 + ((int(asset)*1315423911 + int(salt)*2654435761) % 19000000000)

def _transfer_market(query):
    defs=list(_all_player_defs()); typ=str((query.get('type') or ['player'])[0]).lower()
    if typ not in ('','player','players','any'):return {'auctionInfo':[],'count':0,'total':0,'endOfList':True,'credits':_credits()}
    wanted=_qint(query,('defId','defid','maskedDefId','assetId','assetid'),0)
    nation=_qint(query,('nation','nat','nationId','nationid'),0); league=_qint(query,('league','leag','leagueId','leagueid'),0); club=_qint(query,('club','team','clubId','clubid','teamId','teamid'),0)
    minb=_qint(query,('minBid','micr'),0); maxb=_qint(query,('maxBid','macr'),0)
    minbuy=_qint(query,('minBuy','minBuyNowPrice','minb'),0); maxbuy=_qint(query,('maxBuy','maxBuyNowPrice','maxb'),0)
    level=str((query.get('lev') or query.get('level') or [''])[0]).lower(); pos=_normalise_position(str((query.get('pos') or query.get('position') or query.get('preferredPosition') or [''])[0]).upper())
    rare=str((query.get('rare') or [''])[0]).strip().lower()
    start=max(0,_qint(query,('start','offset'),0)); count=max(1,min(50,_qint(query,('count','num'),21)))
    pool=[]
    for d in defs:
        aid=int(d.get('assetId',0) or 0); rid=int(d.get('resourceId',d.get('definitionId',aid)) or aid); rating=int(d.get('rating',0) or 0)
        special=_is_special_def(d)
        if wanted and wanted not in (aid,rid):continue
        if nation and int(d.get('nation',0) or 0)!=nation:continue
        if league and int(d.get('leagueId',0) or 0)!=league:continue
        if club and int(d.get('teamId',0) or 0)!=club:continue
        if pos and str(d.get('position','')).upper()!=pos:continue
        if level=='bronze' and rating>64:continue
        if level=='silver' and not (65<=rating<=74):continue
        if level=='gold' and (rating<75 or special):continue
        if level in ('special','sp') and not special:continue
        if rare in ('sp','special') and not special:continue
        if rare in ('1','true','rare') and int(d.get('rareflag',d.get('rareFlag',0)) or 0)!=1:continue
        base=_market_value(d)
        if minb and base<minb:continue
        if maxb and _round_coins(base*.85)>maxb:continue
        if minbuy and base<minbuy:continue
        if maxbuy and base>maxbuy:continue
        pool.append(d)
    stable_query={k:v for k,v in query.items() if str(k).lower() not in ('start','offset','count','num')}
    seed=int(hashlib.sha1(urllib.parse.urlencode(sorted((k,tuple(v)) for k,v in stable_query.items())).encode()).hexdigest()[:12],16)
    rng=random.Random(seed); rng.shuffle(pool)
    selected=pool[start:start+count]
    rows=[]
    with _MARKET_LOCK:
        for idx,d in enumerate(selected):
            base=_market_value(d); buy=_round_coins(base*rng.uniform(.82,1.22)); starting=_round_coins(max(150,buy*rng.uniform(.72,.9)))
            if minbuy and buy<minbuy:continue
            if maxbuy and buy>maxbuy:continue
            if minb and starting<minb:continue
            if maxb and starting>maxb:continue
            rid=int(d.get('resourceId',d.get('definitionId',d.get('assetId',0))) or 0)
            tid=_ai_trade_id(rid,start+idx+1)
            item=_definition_item(d,pile=5,item_id=770000000000+(rid%10000000000))
            auc=_auction_payload(tid,item,starting,buy,expires=rng.choice((300,600,900,1800,3600)))
            _AI_AUCTIONS[tid]=auc; rows.append(dict(auc))
    log.warning('MARKET SEARCH filters=%s pool=%d returned=%d specials=%d',query,len(pool),len(rows),sum(1 for x in rows if int(x.get('itemData',{}).get('rareflag',0) or 0)>1))
    return _auction_response(rows,count=len(rows),total=len(pool),totalResults=len(pool),numItems=len(pool),endOfList=start+len(rows)>=len(pool))


def _live_transfer_count():
    # Synthetic local market population. This is deliberately stable enough to
    # look like a living PC market while reflecting the size of the AI catalogue.
    base=max(50000,len(_all_player_defs())*16)
    active=sum(1 for x in _db_auctions() if x.get('tradeState')=='active')
    wobble=(int(time.time())//60 % 120)*37
    return int(base+active+wobble)

def _db_auctions():
    out=[]
    with _DB_LOCK,_db_connect() as con:
        rows=con.execute('SELECT data FROM auctions ORDER BY trade_id DESC').fetchall()
    for r in rows:
        try:
            x=json.loads(r['data'])
            if isinstance(x,dict):out.append(x)
        except Exception:pass
    return out

def _save_auction(auc):
    with _DB_LOCK,_db_connect() as con:
        con.execute('INSERT OR REPLACE INTO auctions(trade_id,data) VALUES(?,?)',(int(auc['tradeId']),json.dumps(auc,separators=(',',':'))))

def _delete_auction(tid):
    with _DB_LOCK,_db_connect() as con:con.execute('DELETE FROM auctions WHERE trade_id=?',(int(tid),))

def _next_trade_id():
    with _DB_LOCK:
        try:n=int(_meta_get('nextTradeId','900000000001'))
        except Exception:n=900000000001
        _meta_set('nextTradeId',n+1);return n

def _market_ai_tick_user_auctions():
    now=int(time.time())
    for auc in _db_auctions():
        if auc.get('tradeState')!='active':continue
        listed=int(auc.get('listedAt',now) or now); end=int(auc.get('endTime',listed+3600) or listed+3600)
        item=auc.get('itemData',{}) if isinstance(auc.get('itemData'),dict) else {}
        rid=item.get('resourceId',item.get('definitionId',item.get('assetId',0)))
        d=_definition_by_resource(rid) or _definition_by_asset(item.get('assetId',0)) or {'rating':item.get('rating',50)}
        fair=_market_value(d); buy=int(auc.get('buyNowPrice',0) or 0); elapsed=max(0,now-listed)
        attractiveness=(fair/max(150,buy)) if buy else 0
        threshold=8 if attractiveness>=1.10 else 18 if attractiveness>=.95 else 35 if attractiveness>=.80 else 10**9
        if elapsed>=threshold:
            auc['tradeState']='closed';auc['bidState']='highest';auc['currentBid']=buy;auc['expires']=0;auc['sold']=True;auc['closedAt']=now
            if not auc.get('credited'):
                _meta_set('credits',_credits()+buy);auc['credited']=True
                try:
                    iid=int(item.get('id',0) or 0)
                    with _DB_LOCK,_db_connect() as con:con.execute('DELETE FROM items WHERE id=?',(iid,))
                except Exception:pass
            _save_auction(auc);log.warning('MARKET AI BOUGHT trade=%s asset=%s price=%s fair=%s',auc.get('tradeId'),item.get('assetId'),buy,fair)
        elif now>=end:
            auc['tradeState']='expired';auc['bidState']='none';auc['expires']=0
            item['pile']=7;item['itemState']='free';_save_item(item);auc['itemData']=item;_save_auction(auc)

def _tradepile_payload():
    _market_ai_tick_user_auctions();now=int(time.time());rows=[]
    for auc in _db_auctions():
        x=dict(auc)
        if x.get('tradeState')=='active':x['expires']=max(0,int(x.get('endTime',now)-now))
        rows.append(x)
    return _auction_response(rows,count=len(rows),endOfList=True)

def _list_market_item(doc):
    if not isinstance(doc,dict):doc={}
    raw=doc.get('itemData',{}) if isinstance(doc.get('itemData'),dict) else {}
    iid=int(raw.get('id',doc.get('itemId',doc.get('id',0))) or 0)
    item=_db_item_map().get(iid)
    if not item:return {'success':False,'error':'ITEM_NOT_FOUND'}
    start=max(150,int(doc.get('startingBid',doc.get('startPrice',150)) or 150));buy=max(start,int(doc.get('buyNowPrice',doc.get('buyNow',start)) or start))
    duration=max(60,min(259200,int(doc.get('duration',doc.get('expires',3600)) or 3600)))
    item=dict(item);item['pile']=5;item['itemState']='forSale';_save_item(item)
    tid=_next_trade_id();now=int(time.time());auc=_auction_payload(tid,item,start,buy,duration,owner=True,extra={'listedAt':now,'endTime':now+duration})
    _save_auction(auc);log.warning('MARKET LIST trade=%s item=%s asset=%s start=%s buy=%s',tid,iid,item.get('assetId'),start,buy)
    response=dict(auc);response['id']=tid;response['idStr']=str(tid);response['tradeId']=tid;response['success']=True;response['credits']=_credits()
    return response

def _bid_market_trade(tid,doc):
    try:tid=int(tid)
    except Exception:return {'success':False,'error':'TRADE_NOT_FOUND'}
    if not isinstance(doc,dict):doc={}
    bid=int(doc.get('bid',doc.get('bidAmount',doc.get('buyNowPrice',0))) or 0)
    with _MARKET_LOCK:auc=_AI_AUCTIONS.get(tid)
    if not auc:return {'success':False,'error':'TRADE_NOT_FOUND','tradeId':tid,'credits':_credits()}
    buy=int(auc.get('buyNowPrice',0) or 0); minimum=max(int(auc.get('startingBid',0) or 0),int(auc.get('currentBid',0) or 0)+50)
    if bid<minimum:return {'success':False,'error':'BID_TOO_LOW','tradeId':tid,'credits':_credits()}
    if bid>=buy and buy>0:
        if _credits()<buy:return {'success':False,'error':'INSUFFICIENT_FUNDS','tradeId':tid,'credits':_credits()}
        _meta_set('credits',_credits()-buy)
        item=dict(auc.get('itemData',{}));item['id']=_next_item_id();item['itemId']=item['id'];item['pile']=6;item['itemState']='free';_save_item(item)
        auc=dict(auc);auc['itemData']=item;auc['currentBid']=buy;auc['tradeState']='closed';auc['bidState']='buyNow';auc['expires']=0
        _MARKET_TARGETS[tid]=auc
        with _MARKET_LOCK:_AI_AUCTIONS.pop(tid,None)
        log.warning('MARKET BUY trade=%s asset=%s price=%s item=%s',tid,item.get('assetId'),buy,item.get('id'))
        return _auction_response([auc],success=True)
    auc=dict(auc);auc['currentBid']=bid;auc['bidState']='highest';_AI_AUCTIONS[tid]=auc;_MARKET_TARGETS[tid]=auc
    return _auction_response([auc],success=True)

def _clear_trade(tid):
    try:tid=int(tid)
    except Exception:return {'success':False}
    auc=next((x for x in _db_auctions() if int(x.get('tradeId',0))==tid),None)
    if auc:
        if auc.get('tradeState') in ('expired','closed'):
            if auc.get('tradeState')=='expired':
                item=dict(auc.get('itemData',{}));item['pile']=7;item['itemState']='free';_save_item(item)
            _delete_auction(tid)
        return {'success':True,'tradeId':tid,'credits':_credits()}
    with _MARKET_LOCK:_MARKET_TARGETS.pop(tid,None);_AI_AUCTIONS.pop(tid,None)
    return {'success':True,'tradeId':tid,'credits':_credits()}


def _clear_finished_trades():
    removed=0
    for auc in list(_db_auctions()):
        if auc.get('tradeState') not in ('expired','closed'):continue
        tid=int(auc.get('tradeId',0) or 0)
        if auc.get('tradeState')=='expired':
            item=dict(auc.get('itemData',{}));item['pile']=7;item['itemState']='free';_save_item(item)
        if tid:_delete_auction(tid);removed+=1
    return {'success':True,'removed':removed,'credits':_credits()}

def _relist_market():
    now=int(time.time());rows=[]
    for auc in _db_auctions():
        if auc.get('tradeState')=='expired':
            auc['tradeState']='active';auc['bidState']='none';auc['currentBid']=0;auc['listedAt']=now;auc['endTime']=now+3600;auc['expires']=3600;_save_auction(auc)
        rows.append(auc)
    return _auction_response(rows,success=True)

def _club_page(query):
    sku_mode = str((query.get('skuMode') or [''])[0] or '').upper()
    if sku_mode == 'WC':
        items = [_apply_world_cup_player_schema(x) if str(x.get('itemType','')).lower()=='player' else dict(x)
                 for x in _db_items() if int(x.get('pile',7) or 0)==7 and x.get('skuMode') == 'WC']
    else:
        items = [dict(x) for x in _db_items() if int(x.get('pile',7) or 0)==7 and x.get('skuMode') != 'WC']
    typ=str((query.get('type') or query.get('itemType') or [''])[0]).lower()
    if typ in ('player','players'):
        items=[x for x in items if str(x.get('itemType','')).lower()=='player']
    elif typ in ('kit','kits'):
        items=[x for x in items if str(x.get('itemType','')).lower()=='kit']
    elif typ in ('badge','badges'):
        items=[x for x in items if str(x.get('itemType','')).lower()=='custom' and int(x.get('cardsubtypeid',0) or 0)==11]
    elif typ in ('manager','managers','staff'):
        items=[x for x in items if str(x.get('itemType','')).lower()=='manager']
    elif typ in ('consumable','consumables'):
        items=[x for x in items if str(x.get('itemType','')).lower() in ('development','training','consumable')]
    elif typ in ('development','training'):
        items=[x for x in items if str(x.get('itemType','')).lower()==typ]
    elif typ in ('equippables','clubitems'):
        items=[x for x in items if str(x.get('itemType','')).lower()!='player']

    # FIFA 18 reuses the /club endpoint for squad-player searches. Older local
    # builds ignored these selectors, which made GK/club/etc. return arbitrary
    # players. Accept the native and common alias spellings used across FUT.
    wanted=_qint(query,('defId','defid','maskedDefId','definitionId','resourceId','assetId','assetid'),0)
    nation=_qint(query,('nation','nat','nationId','nationid'),0)
    league=_qint(query,('league','leag','leagueId','leagueid'),0)
    club=_qint(query,('club','team','clubId','clubid','teamId','teamid'),0)
    # FIFA 18 sends -1 for "Any" in squad search selectors. v0.8.9.29
    # accidentally treated -1 as a real ID (and as truthy), filtering every
    # player out even for an otherwise unfiltered My Club search.
    wanted = wanted if wanted > 0 else 0
    nation = nation if nation > 0 else 0
    league = league if league > 0 else 0
    club = club if club > 0 else 0
    pos=str((query.get('pos') or query.get('position') or query.get('preferredPosition') or query.get('preferredposition') or [''])[0]).strip().upper()
    pos=_normalise_position(pos)
    name=str((query.get('name') or query.get('playerName') or query.get('search') or [''])[0]).strip().lower()
    if typ in ('player','players') or any((wanted,nation,league,club,pos,name)):
        filtered=[]
        for x in items:
            if str(x.get('itemType','')).lower()!='player':continue
            aid=int(x.get('assetId',0) or 0);rid=int(x.get('resourceId',x.get('definitionId',aid)) or aid)
            if wanted and wanted not in (aid,rid):continue
            if nation and int(x.get('nation',0) or 0)!=nation:continue
            if league and int(x.get('leagueId',0) or 0)!=league:continue
            if club and int(x.get('teamid',x.get('teamId',0)) or 0)!=club:continue
            xpos=_normalise_position(x.get('preferredPosition',x.get('position','')))
            if pos and xpos!=pos:continue
            if name:
                hay=' '.join(str(x.get(k,'') or '') for k in ('name','displayName','firstName','lastName')).lower()
                if name not in hay:continue
            filtered.append(x)
        items=filtered

    level=str((query.get('lev') or query.get('level') or [''])[0]).lower()
    rare=str((query.get('rare') or [''])[0]).strip().lower()
    if typ in ('player','players') or any((wanted,nation,league,club,pos,name)):
        def special_item(x):
            try:rf=int(x.get('rareflag',x.get('rareFlag',0)) or 0)
            except Exception:rf=0
            aid=int(x.get('assetId',0) or 0);rid=int(x.get('resourceId',x.get('definitionId',aid)) or aid)
            return rf>1 or rid!=aid or bool(x.get('isSpecial'))
        if level=='bronze':items=[x for x in items if int(x.get('rating',0) or 0)<=64 and not special_item(x)]
        elif level=='silver':items=[x for x in items if 65<=int(x.get('rating',0) or 0)<=74 and not special_item(x)]
        elif level=='gold':items=[x for x in items if int(x.get('rating',0) or 0)>=75 and not special_item(x)]
        elif level in ('special','sp'):items=[x for x in items if special_item(x)]
        if rare in ('sp','special'):items=[x for x in items if special_item(x)]
        elif rare in ('1','true','rare'):items=[x for x in items if int(x.get('rareflag',x.get('rareFlag',0)) or 0)==1]
    reverse=((query.get('sort') or ['desc'])[0].lower()!='asc')
    items.sort(key=lambda x:(int(x.get('rating',0) or 0),int(x.get('rareflag',0) or 0),int(x.get('id',0) or 0)),reverse=reverse)
    start=max(0,_qint(query,('start','offset'),0))
    count=max(1,min(500,_qint(query,('count','num'),11)))
    rows=[dict(x) for x in items[start:start+count]]
    end=(start+len(rows)>=len(items))
    log.warning('CLUB PAGE start=%d count=%d total=%d returned=%d filters={pos:%s club:%s league:%s nation:%s def:%s name:%r level:%s rare:%s} specials=%d end=%s query=%s',
                start,count,len(items),len(rows),pos,club,league,nation,wanted,name,level,rare,
                sum(1 for x in rows if int(x.get('rareflag',0) or 0)>1),end,query)
    cc=_club_counts(sku_mode=sku_mode)
    return {'clubInfo':{'clubId':CLUB_ID,'clubName':_club_name(),'clubAbbr':_club_abbr(),'established':_established(),'credits':_credits(),'playerCount':cc['players'],'players':cc['players'],'ownedPlayers':cc['players'],'clubCount':cc['total'],'clubItemCount':cc['total'],'staffCount':cc['managers'],'consumableCount':cc['consumables']},
            'clubId':CLUB_ID,'clubName':_club_name(),'clubAbbr':_club_abbr(),'clubCount':len(items),'playerCount':cc['players'],'ownedPlayers':cc['players'],
            'itemData':rows,'items':rows,'count':len(rows),'total':len(items),'start':start,'endOfList':end,'credits':_credits()}

def _json_bytes(obj):
    return json.dumps(obj,separators=(',',':')).encode('utf-8')

def _route_count(method, low):
    key=(method,low)
    n=HTTP_ROUTE_COUNTS.get(key,0)+1
    HTTP_ROUTE_COUNTS[key]=n
    return n

def _log_http_request(handler, body, low):
    n=_route_count(handler.command,low)
    # The v0.7.7 malformed catalog response generated >600 requests in ~30 s.
    # Keep enough diagnostics to prove whether that loop is gone without
    # creating another half-megabyte wall of identical lines.
    noisy='/pow/store/game/fifa18/catalog/0/item/list' in low
    if not noisy or n<=5 or n in (10,25,50,100,250,500) or n%1000==0:
        log.warning('FUTHTTP %s %s count=%d headers=%s body=%r',handler.command,handler.path,n,dict(handler.headers),body[:8192])

def _onboarding_payload():
    # Multi-alias diagnostic envelope.  No observed v0.7.7 request reached this
    # route yet, but CardsDLL contains Get/SetOnboardingData plus a starterPack
    # member.  Supplying the same five real ItemData candidates under common
    # envelope aliases makes the first live request informative without hiding
    # it behind another empty {} response.
    candidates=[dict(x) for x in STARTER_ITEMS[:5]]
    pack={'itemData':candidates,'items':candidates,'count':len(candidates)}
    return {
        'onboardingClientData':{'starterPack':pack,'completed':True},
        'starterPack':pack,
        'itemData':candidates,
        'items':candidates,
        'count':len(candidates),
        'completed':True,
        'success':True,
    }


def _grant_100m_once():
    if _meta_get('grant100m0893','')=='1':return _credits()
    before=_credits()
    if before<100_000_000:_meta_set('credits',100_000_000)
    _meta_set('grant100m0893','1')
    log.warning('ECONOMY one-time 100M grant credits=%d->%d',before,_credits())
    return _credits()

_DRAFT_FORMATIONS=('f442','f433','f4231','f41212','f352')
_DRAFT_DIFFICULTY_NAMES=('BEGINNER','AMATEUR','SEMIPRO','PRO','WORLDCLASS','LEGENDARY','ULTIMATE')
_DRAFT_RUNTIME_ACTIVE_KEYS=set()
_DRAFT_ITEM_ID_BASE=20_000_000
_DRAFT_ITEM_ID_LAST=29_999_999
_DRAFT_STATE_KEYS=(
    'draftSinglePlayer','draftOnline',
    'draftWorldCupSinglePlayer','draftWorldCupOnline',
)

def _draft_item_id_is_safe(value):
    try:value=int(value or 0)
    except Exception:return False
    return _DRAFT_ITEM_ID_BASE < value <= _DRAFT_ITEM_ID_LAST

def _draft_state_has_unsafe_item_ids(st):
    """Reject persisted Draft instances that FIFA 18 cannot parse as Int32 IDs."""
    if not isinstance(st,dict):return False
    rows=[]
    picked=st.get('pickedBySlot',{})
    if isinstance(picked,dict):rows.extend(x for x in picked.values() if isinstance(x,dict))
    cached=st.get('lastDraftChoiceItems',[])
    if isinstance(cached,list):rows.extend(x for x in cached if isinstance(x,dict))
    manager=st.get('selectedManager')
    if isinstance(manager,dict):rows.append(manager)
    for item in rows:
        for name in ('id','itemId'):
            if name in item and not _draft_item_id_is_safe(item.get(name)):
                return True
    return False

def _draft_allocate_item_ids(count=1):
    """Allocate durable Aurora-style Draft-only instance IDs in a small Int32 range."""
    count=max(1,int(count or 1))
    with _DB_LOCK,_db_connect() as con:
        row=con.execute("SELECT value FROM meta WHERE key='draftNextItemId'").fetchone()
        try:current=int(row['value'] if row else _DRAFT_ITEM_ID_BASE)
        except Exception:current=_DRAFT_ITEM_ID_BASE
        current=max(_DRAFT_ITEM_ID_BASE,min(_DRAFT_ITEM_ID_LAST,current))
        # On first use after upgrading, continue above any already-safe persisted
        # Draft ID instead of colliding with a resumable squad.
        placeholders=','.join('?' for _ in _DRAFT_STATE_KEYS)
        for saved in con.execute(f'SELECT value FROM meta WHERE key IN ({placeholders})',_DRAFT_STATE_KEYS).fetchall():
            try:doc=json.loads(saved['value'] or '{}')
            except Exception:continue
            candidates=[]
            if isinstance(doc,dict):
                picked=doc.get('pickedBySlot',{})
                if isinstance(picked,dict):candidates.extend(x for x in picked.values() if isinstance(x,dict))
                cached=doc.get('lastDraftChoiceItems',[])
                if isinstance(cached,list):candidates.extend(x for x in cached if isinstance(x,dict))
                if isinstance(doc.get('selectedManager'),dict):candidates.append(doc['selectedManager'])
            for item in candidates:
                try:iid=int(item.get('id',item.get('itemId',0)) or 0)
                except Exception:iid=0
                if _draft_item_id_is_safe(iid):
                    if not row or (current <= iid < current + count + 100):
                        current=max(current,iid)
        if current+count>_DRAFT_ITEM_ID_LAST:
            current=_DRAFT_ITEM_ID_BASE
        ids=list(range(current+1,current+count+1))
        con.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('draftNextItemId',?)",(str(ids[-1]),))
    return ids

def _draft_mode_name(mode='SINGLE_PLAYER'):
    raw=str(mode or 'SINGLE_PLAYER').upper().replace('-','_').replace(' ','_')
    is_world_cup=('WORLD_CUP' in raw or 'WORLDCUP' in raw or raw.startswith('WC_') or raw=='WC')
    if is_world_cup:
        return 'WORLD_CUP_ONLINE' if 'ONLINE' in raw else 'WORLD_CUP_SINGLE_PLAYER'
    return 'ONLINE' if raw in ('ONLINE','0') else 'SINGLE_PLAYER'

def _draft_mode_key(mode='SINGLE_PLAYER'):
    name=_draft_mode_name(mode)
    return {
        'ONLINE':'draftOnline',
        'WORLD_CUP_ONLINE':'draftWorldCupOnline',
        'WORLD_CUP_SINGLE_PLAYER':'draftWorldCupSinglePlayer',
    }.get(name,'draftSinglePlayer')

def _resolve_draft_mode(mode_id=None, query=None, doc=None):
    q = query or {}
    d = doc if isinstance(doc, dict) else {}
    sku = str((q.get('skuMode') or [d.get('skuMode', '')])[0] or '').upper()
    req = str((q.get('mode') or [d.get('mode', '')])[0] or '').upper()
    if 'ONLINE' in req:
        is_online = True
    elif 'SINGLE' in req or 'OFFLINE' in req:
        is_online = False
    elif mode_id is not None and str(mode_id).isdigit():
        is_online = (int(mode_id) == 0)
    else:
        is_online = False
    if sku == 'WC' or 'WC' in req or 'WORLDCUP' in req:
        return 'WORLD_CUP_ONLINE' if is_online else 'WORLD_CUP_SINGLE_PLAYER'
    return 'ONLINE' if is_online else 'SINGLE_PLAYER'

def _draft_recover_stale_partial(key,st):
    """Quarantine only legacy persisted Draft state containing unsafe item IDs."""
    if key in _DRAFT_RUNTIME_ACTIVE_KEYS or not isinstance(st,dict):return st
    state=str(st.get('draftState',st.get('state','NOT_STARTED')) or 'NOT_STARTED').upper()
    stage=str(st.get('draftStage','') or '').upper()
    picked=st.get('pickedBySlot',{}) if isinstance(st.get('pickedBySlot'),dict) else {}
    if state in ('NOT_STARTED','COMPLETE') or not _draft_state_has_unsafe_item_ids(st):return st

    backup_key=f'{key}CrashRecoveryBackup08921'
    marker_key=f'{key}CrashRecoveryHash08921'
    replacement={
        'draftsCompleted':max(0,int(st.get('draftsCompleted',0) or 0)),
        'draftToken':max(0,int(st.get('draftToken',0) or 0)),
        'draftTokens':max(0,int(st.get('draftTokens',st.get('draftToken',0)) or 0)),
    }
    refund=0;digest=''
    with _DB_LOCK, _db_connect() as con:
        row=con.execute('SELECT value FROM meta WHERE key=?',(key,)).fetchone()
        raw=row['value'] if row else json.dumps(st,separators=(',',':'))
        try:current=json.loads(raw or '{}')
        except Exception:current=st
        current_picked=current.get('pickedBySlot',{}) if isinstance(current,dict) and isinstance(current.get('pickedBySlot'),dict) else {}
        current_state=str(current.get('draftState',current.get('state','NOT_STARTED')) or 'NOT_STARTED').upper() if isinstance(current,dict) else 'NOT_STARTED'
        current_stage=str(current.get('draftStage','') or '').upper() if isinstance(current,dict) else ''
        if current_state in ('NOT_STARTED','COMPLETE') or not _draft_state_has_unsafe_item_ids(current):return current
        digest=hashlib.sha256(raw.encode('utf-8','replace')).hexdigest()
        marker=con.execute('SELECT value FROM meta WHERE key=?',(marker_key,)).fetchone()
        already_refunded=bool(marker and marker['value']==digest)
        currency=str(current.get('entryCurrency','COINS') or 'COINS').upper()
        paid=max(0,int(current.get('entryPaid',current.get('entryFee',15000)) or 0))
        if not already_refunded and currency=='COINS':refund=min(15000,paid)
        credits_row=con.execute("SELECT value FROM meta WHERE key='credits'").fetchone()
        try:credits=max(0,int(credits_row['value'] if credits_row else 0))
        except Exception:credits=0
        backup=json.dumps({'recoveredAt':int(time.time()),'reason':'unsafe_draft_item_id','refund':refund,'state':current},separators=(',',':'))
        con.execute('INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)',(backup_key,backup))
        con.execute('INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)',(marker_key,digest))
        if refund:con.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('credits',?)",(str(credits+refund),))
        con.execute('INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)',(key,json.dumps(replacement,separators=(',',':'))))
    log.warning('DRAFT CRASH RECOVERY key=%s staleStage=%s picks=%d reason=unsafe_item_id refund=%d backup=%s hash=%s',key,stage,len(picked),refund,backup_key,digest[:12])
    return replacement

# FIFA 18 Single Player FUT Draft documented reward pool.
# Pack ids map to this build's retail-named pack catalogue:
# 2 Silver, 102 Premium Silver, 3 Gold, 103 Premium Gold,
# 27 Jumbo Premium Gold, 28 Rare Gold, 29 Premium Gold Players,
# 30 Mega, 45 Gold Players.
_DRAFT_REWARD_SETS={
    # FIFA 18 Single Player FUT Draft reward combinations. 0-2 win sets were
    # reported historically but not all were marked confirmed; 3 and 4 win
    # entries below use the confirmed pack-only combinations from the FIFA18
    # reward table so the local backend does not invent higher-value outcomes.
    0: [
        {'packs':[3], 'coins':0, 'label':'1 x Gold Pack'},
    ],
    1: [
        {'packs':[2,3,3], 'coins':0, 'label':'1 x Silver Pack + 2 x Gold Packs'},
    ],
    2: [
        {'packs':[27], 'coins':0, 'label':'1 x Jumbo Premium Gold Pack'},
        {'packs':[3,3,3], 'coins':0, 'label':'3 x Gold Packs'},
    ],
    3: [
        {'packs':[103,103,103], 'coins':0, 'label':'3 x Premium Gold Packs'},
    ],
    4: [
        {'packs':[103,45], 'coins':0, 'label':'1 x Premium Gold Pack + 1 x Gold Players Pack'},
        {'packs':[103,27], 'coins':0, 'label':'1 x Premium Gold Pack + 1 x Jumbo Premium Gold Pack'},
        {'packs':[28], 'coins':0, 'label':'1 x Rare Gold Pack'},
        {'packs':[45,27], 'coins':0, 'label':'1 x Gold Players Pack + 1 x Jumbo Premium Gold Pack'},
        {'packs':[103,103,27], 'coins':0, 'label':'2 x Premium Gold Packs + 1 x Jumbo Premium Gold Pack'},
        {'packs':[103,29], 'coins':0, 'label':'1 x Premium Gold Pack + 1 x Premium Gold Players Pack'},
        {'packs':[30], 'coins':0, 'label':'1 x Mega Pack'},
    ],
}

def _record_triplet():
    def _n(k):
        try:return max(0,int(_meta_get(k,'0') or 0))
        except Exception:return 0
    return _n('recordWon'),_n('recordDraw'),_n('recordLoss')

def _record_settle(reason):
    r=str(reason or '').upper()
    won,draw,loss=_record_triplet()
    if r=='WIN':won+=1
    elif r=='DRAW':draw+=1
    elif r in ('LOSS','DNF','QUIT','FORFEIT'):loss+=1
    _meta_set('recordWon',won);_meta_set('recordDraw',draw);_meta_set('recordLoss',loss)
    return won,draw,loss

def _draft_reward_bundle(wins, st=None):
    w=max(0,min(4,int(wins or 0)))
    st=st if isinstance(st,dict) else {}
    existing=st.get('pendingRewardBundle')
    if isinstance(existing,dict) and isinstance(existing.get('packs'),list):
        return dict(existing)
    choices=_DRAFT_REWARD_SETS.get(w) or _DRAFT_REWARD_SETS[0]
    return dict(random.choice(choices))

def _draft_reward(wins):
    b=_draft_reward_bundle(wins,{})
    packs=b.get('packs',[]) if isinstance(b.get('packs'),list) else []
    return int(b.get('coins',0) or 0), int(packs[0] if packs else 0)

def _draft_stats_payload(st=None):
    st=st or _draft_get_state()
    wins=max(0,min(4,int(st.get('wins',0) or 0)))
    eliminated=bool(st.get('completed')) or _draft_stage(st)=='READY_FOR_REWARDS'
    return {
        'bestBuilderScore':0,'concededGoals':0,
        'draftChampion':1 if eliminated and wins>=4 else 0,
        'draftsCompleted':int(st.get('draftsCompleted',0) or 0)+(1 if eliminated else 0),
        'gamesLost':1 if eliminated and wins<4 else 0,
        'gamesWon':wins,
        'passAccuracyTotal':0,'possessionPercentage':0,'possesionTotal':0,
        'possessionTotal':0,'scoredGoals':0,
    }

def _draft_get_state(mode=None):
    m_name=_draft_mode_name(mode)
    key=_draft_mode_key(m_name)
    try:st=json.loads(_meta_get(key,'{}') or '{}')
    except Exception:st={}
    if not isinstance(st,dict):st={}
    st=_draft_recover_stale_partial(key,st)
    # v0.8.9.7 stored active Draft picks as one flat resource-id list and had no
    # transient squad/slot ownership.  Resume would therefore preserve the exact
    # broken state the user just tested.  Reset that legacy in-progress session
    # once, refunding its 15k entry, so v0.8.9.8 starts with the corrected model.
    if _meta_get('draftIsolationMigration0898','')!='1':
        legacy_active=str(st.get('draftState',st.get('state','NOT_STARTED')) or 'NOT_STARTED').upper() not in ('NOT_STARTED','COMPLETE')
        if legacy_active and not isinstance(st.get('pickedBySlot'),dict):
            _meta_set('credits',_credits()+15000)
            st={}
            _meta_set(key,'{}')
            log.warning('DRAFT MIGRATION 0.8.9.8 reset legacy flat-pick session and refunded 15000 coins')
        _meta_set('draftIsolationMigration0898','1')
    if _meta_get('draftIsolationMigration0899','')!='1':
        pre_state=str(st.get('draftState',st.get('state','NOT_STARTED')) or 'NOT_STARTED').upper()
        if pre_state not in ('NOT_STARTED','COMPLETE'):
            _meta_set('credits',_credits()+15000)
            st={}
            _meta_set(key,'{}')
            log.warning('DRAFT MIGRATION 0.8.9.9 reset pre-native-isolation session and refunded 15000 coins')
        _meta_set('draftIsolationMigration0899','1')
    if _meta_get('draftManagerMigration08910','')!='1':
        picked=st.get('pickedBySlot',{}) if isinstance(st.get('pickedBySlot'),dict) else {}
        stage=str(st.get('draftStage','') or '').upper()
        if len(picked)>=23 and not isinstance(st.get('selectedManager'),dict) and stage in ('READY_FOR_MATCH','PLAYER_DRAFT','MANAGER_DRAFT'):
            st['draftStage']='MANAGER_DRAFT'
            _meta_set(key,json.dumps(st,separators=(',',':')))
            log.warning('DRAFT MIGRATION 0.8.9.10 resumed completed 23-player draft at manager selection')
        _meta_set('draftManagerMigration08910','1')
    if _meta_get('draftManagerWireMigration08911','')!='1':
        if isinstance(st.get('selectedManager'),dict):
            upgraded=_upgrade_draft_manager_item(st.get('selectedManager'))
            if isinstance(upgraded,dict) and upgraded!=st.get('selectedManager'):
                st['selectedManager']=upgraded
                _meta_set(key,json.dumps(st,separators=(',',':')))
                log.warning('DRAFT MIGRATION 0.8.9.11 upgraded selected manager asset=%s resource=%s',upgraded.get('assetId'),upgraded.get('resourceId'))
        _meta_set('draftManagerWireMigration08911','1')
    # v0.8.9.22 followed Aurora17 and bypassed the manager step for normal
    # Single Player Draft. FIFA 18 PC does require a selected manager: without
    # one it renders *LAbbr_0 with zero contracts and refuses to start a match.
    # The old migration must now be marker-only. Replaying its former body would
    # delete a valid selectedManager from saves that predate the marker.
    manager_bypass_key=f'{key}ManagerBypass08922'
    if m_name=='SINGLE_PLAYER' and _meta_get(manager_bypass_key,'')!='1':
        _meta_set(manager_bypass_key,'1')
        log.warning('DRAFT MIGRATION 0.8.9.22 retired; marker recorded without changing Draft state')
    manager_required_key=f'{key}ManagerRequired08925'
    if m_name=='SINGLE_PLAYER' and _meta_get(manager_required_key,'')!='1':
        picked=st.get('pickedBySlot',{}) if isinstance(st.get('pickedBySlot'),dict) else {}
        stage=str(st.get('draftStage','') or '').upper()
        needs_manager=(
            len(picked)>=23 and stage=='READY_FOR_MATCH' and
            not isinstance(st.get('selectedManager'),dict) and
            not bool(st.get('matchInProgress')) and
            str(st.get('draftState','IN_PROGRESS') or 'IN_PROGRESS').upper()=='IN_PROGRESS'
        )
        if needs_manager:
            _meta_set(f'{key}BeforeManagerRequired08925',json.dumps(st,separators=(',',':')))
            st['draftStage']='MANAGER_DRAFT'
            st['lastDraftChoices']=[];st['lastDraftChoiceItems']=[];st['lastPositionId']=0
            st['active']=True;st['completed']=False
            _meta_set(key,json.dumps(st,separators=(',',':')))
            log.warning('DRAFT MIGRATION 0.8.9.25 restored required manager selection; preserved %d players and wallet/items',len(picked))
        _meta_set(manager_required_key,'1')
    # v0.8.9.25 emitted a synthetic manager item with an incomplete FIFA 18
    # native DTO and retained player-slot 23 after the manager choice.  The
    # retail client actually posts positionId=0 for this choice; on the next
    # state response the old selected item/position pair terminates FIFA18.
    # Reopen only an affected normal Draft once, after retaining the exact old
    # metadata.  This never touches the wallet, club inventory, or My Squads.
    manager_native_key=f'{key}ManagerNativeWire08926'
    if m_name=='SINGLE_PLAYER' and _meta_get(manager_native_key,'')!='1':
        selected=st.get('selectedManager') if isinstance(st.get('selectedManager'),dict) else None
        affected_assets={int(x[0]) for x in _DRAFT_MANAGERS}
        try:selected_asset=int((selected or {}).get('assetId',(selected or {}).get('headId',0)) or 0)
        except Exception:selected_asset=0
        unsafe_selected=(
            selected is not None and selected_asset in affected_assets and
            int(selected.get('nativeDraftManagerWire',0) or 0)<_DRAFT_MANAGER_WIRE_VERSION and
            not bool(st.get('matchInProgress')) and
            str(st.get('draftState','IN_PROGRESS') or 'IN_PROGRESS').upper()=='IN_PROGRESS'
        )
        if unsafe_selected:
            _meta_set(f'{key}BeforeManagerNativeWire08926',json.dumps(st,separators=(',',':')))
            st.pop('selectedManager',None)
            st['draftStage']='MANAGER_DRAFT'
            st['lastDraftChoices']=[];st['lastDraftChoiceItems']=[];st['lastPositionId']=0
            st['active']=True;st['completed']=False
            _meta_set(key,json.dumps(st,separators=(',',':')))
            log.warning('DRAFT MIGRATION 0.8.9.26 quarantined incomplete selected-manager wire asset=%s; preserved players and wallet/items',selected_asset)
        _meta_set(manager_native_key,'1')
    # v0.8.9.26 still marked Draft managers as concept/dream player items.
    # FIFA 18 has no manager variant for that player-card presentation path.
    # Quarantine an already-selected v2 manager once so the client never has
    # to animate that stale object. The exact state remains recoverable.
    manager_standard_key=f'{key}ManagerStandardCard08927'
    if m_name=='SINGLE_PLAYER' and _meta_get(manager_standard_key,'')!='1':
        selected=st.get('selectedManager') if isinstance(st.get('selectedManager'),dict) else None
        affected_assets={int(x[0]) for x in _DRAFT_MANAGERS}
        try:selected_asset=int((selected or {}).get('assetId',(selected or {}).get('headId',0)) or 0)
        except Exception:selected_asset=0
        unsafe_selected=(
            selected is not None and selected_asset in affected_assets and
            (int(selected.get('nativeDraftManagerWire',0) or 0)<3 or
             bool(selected.get('concept')) or bool(selected.get('dream'))) and
            not bool(st.get('matchInProgress')) and
            str(st.get('draftState','IN_PROGRESS') or 'IN_PROGRESS').upper()=='IN_PROGRESS'
        )
        if unsafe_selected:
            _meta_set(f'{key}BeforeManagerStandardCard08927',json.dumps(st,separators=(',',':')))
            st.pop('selectedManager',None)
            st['draftStage']='MANAGER_DRAFT'
            st['lastDraftChoices']=[];st['lastDraftChoiceItems']=[];st['lastPositionId']=0
            st['active']=True;st['completed']=False
            _meta_set(key,json.dumps(st,separators=(',',':')))
            log.warning('DRAFT MIGRATION 0.8.9.27 quarantined concept/dream manager asset=%s; preserved players and wallet/items',selected_asset)
        _meta_set(manager_standard_key,'1')
    # v0.8.9.27 disabled draftItem together with the unsupported manager
    # concept/dream presentation flags.  A manager still has to remain a
    # temporary Draft entity while FIFA 18 transitions to Draft Summary.
    # Reopen only a selected v3/non-Draft manager so the user can select a
    # corrected v4 object.  Preserve the exact old Draft JSON first and leave
    # the wallet, club inventory, regular squads and all 23 picks untouched.
    manager_draft_entity_key=f'{key}ManagerDraftEntity08928'
    if m_name=='SINGLE_PLAYER' and _meta_get(manager_draft_entity_key,'')!='1':
        selected=st.get('selectedManager') if isinstance(st.get('selectedManager'),dict) else None
        affected_assets={int(x[0]) for x in _DRAFT_MANAGERS}
        try:selected_asset=int((selected or {}).get('assetId',(selected or {}).get('headId',0)) or 0)
        except Exception:selected_asset=0
        unsafe_selected=(
            selected is not None and selected_asset in affected_assets and
            (int(selected.get('nativeDraftManagerWire',0) or 0)<_DRAFT_MANAGER_WIRE_VERSION or
             not bool(selected.get('draftItem'))) and
            not bool(st.get('matchInProgress')) and
            str(st.get('draftState','IN_PROGRESS') or 'IN_PROGRESS').upper()=='IN_PROGRESS'
        )
        if unsafe_selected:
            _meta_set(f'{key}BeforeManagerDraftEntity08928',json.dumps(st,separators=(',',':')))
            st.pop('selectedManager',None)
            st['draftStage']='MANAGER_DRAFT'
            st['lastDraftChoices']=[];st['lastDraftChoiceItems']=[];st['lastPositionId']=0
            st['active']=True;st['completed']=False
            _meta_set(key,json.dumps(st,separators=(',',':')))
            log.warning('DRAFT MIGRATION 0.8.9.28 quarantined non-Draft manager asset=%s; preserved players and wallet/items',selected_asset)
        _meta_set(manager_draft_entity_key,'1')
    # The consumable apply path wrote its response-only 'success' flag and an
    # uncapped stacked contract count (observed: 127) into the saved Draft
    # objects.  Sanitize those two fields in place.  This keeps the selected
    # manager, every pick, the stage, the wallet and club inventory untouched;
    # nothing is quarantined or reopened.
    consumable_write_key=f'{key}ConsumableWriteCleanup08937'
    if _meta_get(consumable_write_key,'')!='1':
        def _sanitize_draft_item(obj):
            if not isinstance(obj,dict):return obj,False
            fixed=dict(obj);changed=False
            if 'success' in fixed:
                fixed.pop('success',None);changed=True
            for field in ('contract','contracts'):
                if field in fixed:
                    try:value=int(fixed.get(field) or 0)
                    except Exception:continue
                    if value>_MAX_CONTRACTS:
                        fixed[field]=_MAX_CONTRACTS;changed=True
            return fixed,changed
        cleaned=0
        manager_obj,manager_changed=_sanitize_draft_item(st.get('selectedManager'))
        if manager_changed:
            st['selectedManager']=manager_obj;cleaned+=1
        picked=st.get('pickedBySlot',{}) if isinstance(st.get('pickedBySlot'),dict) else {}
        for slot,item in list(picked.items()):
            fixed_item,item_changed=_sanitize_draft_item(item)
            if item_changed:
                picked[slot]=fixed_item;cleaned+=1
        if cleaned:
            st['pickedBySlot']=picked
            _meta_set(key,json.dumps(st,separators=(',',':')))
            log.warning('DRAFT MIGRATION 0.8.9.37 cleaned consumable writes on %d Draft objects for %s; picks and wallet preserved',cleaned,key)
        _meta_set(consumable_write_key,'1')
    # v0.8.9.23 incorrectly advanced a paid PICK_DIFFICULTY run without a
    # client selection.  Undo only the exact empty state produced by that
    # migration.  Coins, club items and the paid-entry record are preserved,
    # and a copy of the pre-repair Draft metadata is retained for audit/recovery.
    draft_heal_key=f'{key}DraftDiffHeal08923'
    draft_heal_rollback_key=f'{key}DraftDiffHealRollback08924'
    if _meta_get(draft_heal_key,'')=='1' and _meta_get(draft_heal_rollback_key,'')!='1':
        stage=str(st.get('draftStage','') or '').upper()
        picked=st.get('pickedBySlot',{}) if isinstance(st.get('pickedBySlot'),dict) else {}
        was_empty_heal=(
            str(st.get('draftState','')).upper()=='IN_PROGRESS' and
            stage=='FORMATION_DRAFT' and not str(st.get('formation','') or '') and
            not picked and not st.get('lastDraftChoiceItems') and
            str(st.get('difficultyName','') or '').upper()=='AMATEUR'
        )
        if was_empty_heal:
            _meta_set(f'{key}BeforeDraftDiffHealRollback08924',json.dumps(st,separators=(',',':')))
            st['draftStage']='PICK_DIFFICULTY'
            st['difficulty']=0
            st['difficultyName']='BEGINNER'
            _meta_set(key,json.dumps(st,separators=(',',':')))
            log.warning('DRAFT MIGRATION 0.8.9.24 restored empty auto-healed state to PICK_DIFFICULTY key=%s coinsAndItemsUntouched=True',key)
        _meta_set(draft_heal_rollback_key,'1')
    # v0.8.9.15 was the first build to launch a real Draft match, but its
    # post-match response advertised zero completion/reward coins. FIFA then
    # parked on Prize Progress without ever requesting /grant/award. Roll back
    # only that one stale, unclaimed elimination so the existing 23-player
    # Draft can be replayed once under the corrected v0.8.9.16 settlement wire.
    if _meta_get('draftPostMatchMigration08916','')!='1':
        stale_reason=str(st.get('lastMatchEndReason','') or '').upper()
        stale_finished=bool(st.get('completed')) and not bool(st.get('prizeClaimed'))
        if stale_finished and stale_reason in ('LOSS','DNF','QUIT','FORFEIT') and int(st.get('wins',0) or 0)==0:
            st['draftState']='IN_PROGRESS';st['state']='IN_PROGRESS';st['status']='IN_PROGRESS'
            st['draftStage']='READY_FOR_MATCH';st['active']=True;st['completed']=False
            st['matchesPlayed']=max(0,int(st.get('matchesPlayed',0) or 0)-1)
            st['losses']=max(0,int(st.get('losses',0) or 0)-1)
            st['round']=1;st['matchInProgress']=False;st['prizeClaimed']=False
            st['pendingCoinReward']=0;st['pendingRewardPackId']=0;st['awardConsumed']=False
            st.pop('lastMatchEndReason',None);st.pop('lastSettledMatchStart',None)
            _meta_set(key,json.dumps(st,separators=(',',':')))
            log.warning('DRAFT MIGRATION 0.8.9.16 rolled stale v0.8.9.15 elimination back to READY_FOR_MATCH; existing picks preserved')
        _meta_set('draftPostMatchMigration08916','1')
    # v0.8.9.16 settled the forfeit correctly but FIFA then froze while
    # rebuilding the frontend at GET /user. Roll back only that one unclaimed
    # zero-win terminal result so the same 23-player Draft can be replayed once
    # against v0.8.9.17's full post-match /user refresh contract.
    if _meta_get('draftPostMatchUserMigration08917','')!='1':
        stale_reason=str(st.get('lastMatchEndReason','') or '').upper()
        stale_finished=bool(st.get('completed')) and not bool(st.get('prizeClaimed')) and not bool(st.get('awardConsumed'))
        stale_reward=max(0,int(st.get('pendingCoinReward',0) or 0))
        if stale_finished and stale_reward>0 and stale_reason in ('LOSS','DNF','QUIT','FORFEIT') and int(st.get('wins',0) or 0)==0:
            st['draftState']='IN_PROGRESS';st['state']='IN_PROGRESS';st['status']='IN_PROGRESS'
            st['draftStage']='READY_FOR_MATCH';st['active']=True;st['completed']=False
            st['matchesPlayed']=max(0,int(st.get('matchesPlayed',0) or 0)-1)
            st['losses']=max(0,int(st.get('losses',0) or 0)-1)
            st['round']=1;st['matchInProgress']=False;st['prizeClaimed']=False
            st['pendingCoinReward']=0;st['pendingRewardPackId']=0;st['awardConsumed']=False
            st.pop('lastMatchEndReason',None);st.pop('lastSettledMatchStart',None)
            won,draw,loss=_record_triplet()
            if loss>0:_meta_set('recordLoss',loss-1)
            _meta_set(key,json.dumps(st,separators=(',',':')))
            log.warning('DRAFT MIGRATION 0.8.9.17 rolled v0.8.9.16 frontend-freeze elimination back to READY_FOR_MATCH; picks preserved and stale global loss removed')
        _meta_set('draftPostMatchUserMigration08917','1')
    # v0.8.9.17 reached the native Prize screen but advertised a synthetic
    # reward-pack id that Aurora17 never publishes in its Draft AwardJson path.
    # FIFA's prize viewmodel then froze before issuing /grant/award. Preserve
    # the completed run and pending coin prize, but remove every pack-prize hint
    # so the client follows the proven coin-only native claim contract.
    if _meta_get('draftPrizeContractMigration08918','')!='1':
        if (bool(st.get('completed')) or str(st.get('draftStage','')).upper()=='READY_FOR_REWARDS') and not bool(st.get('prizeClaimed')):
            if int(st.get('pendingCoinReward',0) or 0)<=0:
                st['pendingCoinReward']=0
            st['pendingRewardPackId']=0
            st['lastRewardPackId']=0
            st['HAS_PACK_PRIZE']=False
            st['HAS_ITEM_PRIZE']=False
            st['awardConsumed']=False
            st['prizeClaimed']=False
            _meta_set(key,json.dumps(st,separators=(',',':')))
            log.warning('DRAFT MIGRATION 0.8.9.18 preserved READY_FOR_REWARDS coin prize=%s and removed synthetic pack prize',st.get('pendingCoinReward',0))
        _meta_set('draftPrizeContractMigration08918','1')
    # v0.8.9.19 replaces the temporary flat-coin test reward with FIFA18's
    # documented Single Player Draft pack sets. Preserve an already-finished run
    # so the user can claim the corrected prize immediately after upgrading.
    if _meta_get('draftAuthenticRewardsMigration08919','')!='1':
        if (bool(st.get('completed')) or str(st.get('draftStage','')).upper()=='READY_FOR_REWARDS') and not bool(st.get('prizeClaimed')):
            bundle=_draft_reward_bundle(st.get('wins',0),{})
            st['pendingRewardBundle']=bundle
            st['pendingCoinReward']=int(bundle.get('coins',0) or 0)
            packs=bundle.get('packs',[]) if isinstance(bundle.get('packs'),list) else []
            st['pendingRewardPackId']=int(packs[0] if packs else 0)
            st['awardConsumed']=False;st['prizeClaimed']=False
            _meta_set(key,json.dumps(st,separators=(',',':')))
            log.warning('DRAFT MIGRATION 0.8.9.19 converted pending test reward to FIFA18 documented set wins=%s label=%s packs=%s coins=%s',st.get('wins',0),bundle.get('label'),packs,bundle.get('coins',0))
        _meta_set('draftAuthenticRewardsMigration08919','1')
    # v0.8.9.20 keeps the current run intact.  It only repairs the data contract:
    # zero-based difficulty, authoritative Draft award rows, and fresh UI aliases.
    if _meta_get('draftAuthoritativeStateMigration08920','')!='1':
        raw_name=str(st.get('difficultyName','') or '').upper().replace('_','').replace('-','').replace(' ','')
        names=('AMATEUR','SEMIPRO','PRO','WORLDCLASS','LEGENDARY','ULTIMATE')
        raw_name={'PROFESSIONAL':'PRO'}.get(raw_name,raw_name)
        if raw_name in names:
            st['difficulty']=names.index(raw_name)
            st['difficultyName']=raw_name
        elif int(st.get('difficulty',0) or 0)>0:
            # 0.8.9.19 stored the same six entries one-based.
            old=max(1,min(6,int(st.get('difficulty',1) or 1)))
            st['difficulty']=old-1;st['difficultyName']=names[old-1]
        if (bool(st.get('completed')) or str(st.get('draftStage','')).upper()=='READY_FOR_REWARDS') and not bool(st.get('prizeClaimed')):
            bundle=_draft_reward_bundle(st.get('wins',0),st)
            st['pendingRewardBundle']=bundle
            packs=bundle.get('packs',[]) if isinstance(bundle.get('packs'),list) else []
            st['pendingRewardPackId']=int(packs[0] if packs else 0)
            st['pendingCoinReward']=int(bundle.get('coins',0) or 0)
            st['awardConsumed']=False;st['prizeClaimed']=False
        _meta_set(key,json.dumps(st,separators=(',',':')))
        _meta_set('draftAuthoritativeStateMigration08920','1')
        log.warning('DRAFT MIGRATION 0.8.9.20 preserved current run; difficulty=%s/%s reward=%s',st.get('difficulty'),st.get('difficultyName'),st.get('pendingRewardBundle'))
    state=str(st.get('draftState',st.get('state','NOT_STARTED')) or 'NOT_STARTED').upper()
    st['mode']=m_name;st['draftMode']=m_name;st['gameMode']=m_name
    st['draftState']=state;st['state']=state;st['status']=state
    st.setdefault('draftId',18002 if m_name == 'ONLINE' else 18001);st.setdefault('id',st['draftId'])
    st['online']=(m_name=='ONLINE');st['offline']=(m_name!='ONLINE');st['isOnline']=(m_name=='ONLINE');st['isModeOnline']=(m_name=='ONLINE')
    st['active']=state not in ('NOT_STARTED','COMPLETE');st['completed']=state=='COMPLETE'
    st.setdefault('draftToken',0);st.setdefault('draftTokens',0);st.setdefault('entryFee',15000);st.setdefault('entryCost',15000);st.setdefault('draftEntry',15000)
    st.setdefault('wins',0);st.setdefault('losses',0);st.setdefault('matchesPlayed',0);st.setdefault('difficulty',0);st.setdefault('difficultyName','BEGINNER');st.setdefault('formation','')
    st.setdefault('round',min(4,int(st.get('wins',0) or 0)+1));st['currentRound']=st['round'];st['maxRounds']=4;st['maxWins']=4
    st.setdefault('picks',[]);st.setdefault('prizeClaimed',False)
    st.setdefault('pendingCoinReward',0);st.setdefault('pendingRewardPackId',0);st.setdefault('awardConsumed',False)
    st['shouldClaimPrize']=bool(st['completed'] and not st['prizeClaimed']);st['isPrizeAvailable']=st['shouldClaimPrize']
    st['SHOULD_CLAIM_PRIZE']=st['shouldClaimPrize'];st['SHOW_PRIZE_SHIELD']=st['shouldClaimPrize']
    native_awards=_draft_native_awards(st) if st['shouldClaimPrize'] else []
    st['awardedPrizes']=native_awards
    award_details=_draft_award_details(st) if st['shouldClaimPrize'] else []
    st['awardItemData']=award_details;st['awardMappings']=award_details
    st['awards']=award_details;st['rewards']=award_details;st['groupAwards']=award_details;st['groupRewards']=award_details
    if award_details:
        first=award_details[0]
        st['AWD_PACKS_STRING']=','.join(str(x.get('packId',0)) for x in award_details)
        st['AWD_PACKS_AMOUNT']=len(award_details)
        st['AWD_PACKS_ASSET_IDS']=','.join(str(x.get('packAssetId',0)) for x in award_details)
        st['IMAGE_UPDATE']=True;st['IMAGE']=str(first.get('packAssetId',0));st['imageUpdate']=True;st['image']=str(first.get('packAssetId',0))
    st['awardSetId']=min(4,int(st.get('wins',0) or 0))
    st['AWARDS_COUNT']=len(native_awards)
    st['HAS_PACK_PRIZE']=any(int(x.get('type',-1))==1 for x in native_awards if isinstance(x,dict))
    st['HAS_ITEM_PRIZE']=False;st['CLAIM_PRIZE_STATUS']=1 if st['prizeClaimed'] else 0
    st['modeName']='FUT_DRAFT_ONLINE_HUB' if m_name=='ONLINE' else 'FUT_DRAFT_OFFLINE_HUB';st['enableDraftMode']=True;st['grantsGameModePrizes']=True
    st['numRounds']=4;st['roundId']=st['round'];st['rounds']=[1,2,3,4]
    st['roundsInfo']=[{'roundId':i,'completed':i<int(st.get('round',1) or 1),'won':i<=int(st.get('wins',0) or 0)} for i in (1,2,3,4)]
    st['draftsCompleted']=int(st.get('draftsCompleted',0) or 0);st['draftChampion']=bool(st['completed'] and int(st.get('wins',0) or 0)>=4);st['COMPLETED_DRAFT']=st['completed']
    st['prizeLevel']=min(4,int(st.get('wins',0) or 0));st['prizeSet']=st['prizeLevel'];st['prizeTiers']=[0,1,2,3,4];st['prizesInError']=False
    rw,rd,rl=_record_triplet();credits=_credits()
    st['credits']=credits;st['coins']=credits;st['sessionCoinsBankBalance']=credits
    st['currencies']=[{'name':'COINS','funds':credits,'finalFunds':credits},{'name':'POINTS','funds':0,'finalFunds':0}]
    st['record']={'won':rw,'draw':rd,'loss':rl};st['gamesWon']=rw;st['gamesDraw']=rd;st['gamesLost']=rl;st['gamesPlayed']=rw+rd+rl
    st['draftSummary']={'wins':st.get('wins',0),'losses':st.get('losses',0),'matchesPlayed':st.get('matchesPlayed',0),'round':st.get('round',1),'maxRounds':4,'numRounds':4,'draftChampion':st['draftChampion'],'teamRating':int(st.get('savedRating',0) or 0),'teamChemistry':int(st.get('savedChemistry',0) or 0)}
    return st



def _draft_stage(st=None, mode=None):
    st = st or _draft_get_state(mode)
    state = str(st.get('draftState', 'NOT_STARTED') or 'NOT_STARTED').upper()
    if state in ('NOT_STARTED', 'COMPLETE'):
        return 'INVALID' if state == 'NOT_STARTED' else 'READY_FOR_REWARDS'
    m_name = str(st.get('mode') or mode or 'SINGLE_PLAYER').upper()
    stage = str(st.get('draftStage', '') or '').upper()
    if m_name.endswith('ONLINE') and stage == 'PICK_DIFFICULTY':
        stage = 'FORMATION_DRAFT'
        st['draftStage'] = 'FORMATION_DRAFT'
    if stage in ('PICK_DIFFICULTY', 'FORMATION_DRAFT', 'CAPTAIN_DRAFT', 'PLAYER_DRAFT', 'MANAGER_DRAFT', 'COMPLETED_DRAFT', 'READY_FOR_MATCH', 'READY_FOR_REWARDS'):
        return stage
    if not st.get('formation'):
        return 'FORMATION_DRAFT' if m_name == 'ONLINE' else 'PICK_DIFFICULTY'
    return 'PLAYER_DRAFT'

# The A/B selector is retired.  One canonical Single Player wire remains.
#
# Why (2026-09-16 rebuild): the selector itself caused the two longest outages
# of 2026-09-15.  A backend started before a variant name existed in source did
# not recognise the configured name and fell back to 'current', which answers
# INVALID/-1 and therefore never opens a slot picker - not even after leaving
# and re-entering the Draft.  Sessions 20260915-174333 (18:18, 18:51) show the
# user unable to draft at all for ~40 minutes for exactly that reason, with the
# code on disk already fixed.  A configuration knob that can silently select a
# non-working mode is worse than no knob.
#
# Every alternative the selector offered is disproven natively:
#   rows-loyalty / rows-no-formation / rows-empty-item  -> empty {index,kitNumber:0}
#       rows exit normal Single Player, 14 of 14 sessions.
#   choose-blaze-refresh   -> UserSessions push ignored (20260915-082930).
#   choose-error-refresh   -> popup, no refetch (20260915-075923).
#   manager-refresh        -> in-screen refetch fires but no slot opens
#                             (20260915-174333, 17:48:13).
#   manager-picker         -> tried by the user, did not work.
#   autocomplete-one-slot  -> fills without offering a choice.
# What survives is the behaviour formerly called 'chain-next-slot': compact
# rows plus PLAYER/<first empty slot>, which completed 23 of 23 and started a
# match in session 20260915-174333.
_DRAFT_SLOT_CANONICAL = 'chain-next-slot'
_DRAFT_SLOT_VARIANTS = (_DRAFT_SLOT_CANONICAL,)
_DRAFT_SLOT_VARIANT_FILE = ROOT / 'config' / 'draft-slot-variant.json'

def _draft_slot_variant():
    """The single supported Single Player wire. No configuration input.

    Deliberately ignores FIFA18_DRAFT_SLOT_VARIANT and the config file so a
    stale backend, a leftover config or another agent cannot put the Draft into
    a mode that silently refuses to open a picker.
    """
    return _DRAFT_SLOT_CANONICAL


def _draft_exposes_empty_slots(st, stage, mode=None):
    """Expose 23-slot skeleton only where accepted (World Cup early stages).

    In Normal Single Player FUT, emitting dummy rows {"index": N, "kitNumber": 0}
    during PLAYER_DRAFT causes FIFA 18 to instantiate definitionId=0 entities
    (rendering as grey 'defin ndefine' cards with 85 rating / 32 chem) and locks
    the pitch into Squad Management (Swap) mode. Normal SP PLAYER_DRAFT must
    use compact rows containing only genuinely drafted players.
    """
    mode_name = _draft_mode_name(mode or (st.get('mode') if isinstance(st, dict) else None))
    if mode_name.startswith('WORLD_CUP'):
        return stage in ('PICK_DIFFICULTY', 'FORMATION_DRAFT', 'CAPTAIN_DRAFT')
    return False

def _draft_wire_manager_entry(mgr):
    if not isinstance(mgr, dict):
        return []
    m_item = dict(_upgrade_draft_manager_item(mgr))
    m_id = int(m_item.get('id', m_item.get('itemId', 0)) or 0)
    m_item.pop('success', None)
    m_item['concept'] = False
    m_item['dream'] = False
    m_item['draftItem'] = True
    m_item['itemState'] = 'free'
    m_item['untradeable'] = True
    m_item['tradeable'] = False
    m_item['contract'] = min(99, max(1, int(m_item.get('contract', 99) or 99)))
    m_item['contracts'] = min(99, max(1, int(m_item.get('contracts', 99) or 99)))
    m_item['loans'] = 0
    return [{'index': 0, 'id': m_id, 'itemData': m_item}]

def _draft_wire_state(mode=None):
    """Exact Aurora17-compliant FIFA 18 Draft state wire contract for both Single Player and Online Draft."""
    st = _draft_get_state(mode)
    stage = _draft_stage(st, mode)
    # This is the entry cost (one token), not the user's token-wallet balance.
    # Aurora17 always emits 1 here. Sending 0 after a coin purchase makes FIFA
    # 18 exit while it transitions from the entry screen to PICK_DIFFICULTY.
    token_entry_cost = 1
    wire_mode = _draft_mode_name(st.get('mode') or mode)
    picked = st.get('pickedBySlot', {}) if isinstance(st.get('pickedBySlot'), dict) else {}

    if stage in ('INVALID', 'NOT_STARTED') or (stage in ('READY_FOR_REWARDS', 'COMPLETED_DRAFT') and not picked):
        if stage in ('READY_FOR_REWARDS', 'COMPLETED_DRAFT') and not picked:
            _draft_claim_award(wire_mode)
        return [{
            'entranceCriteria': {'COINS': 15000, 'DRAFT_TOKEN': token_entry_cost, 'POINTS': 300},
            'gamesWonCurrentMatch': 0,
            'roundsInfo': [],
            'squadState': 'INVALID',
            'stateParam1': 'INVALID',
            'stateParam2': '0'
        }]

    def _slot_item(idx):
        item = picked.get(str(idx), picked.get(idx))
        return item if isinstance(item, dict) and int(item.get('assetId', 0) or 0) > 0 else None

    if stage == 'PLAYER_DRAFT':
        next_empty = next((idx for idx in range(23) if _slot_item(idx) is None), None)
        if wire_mode in ('SINGLE_PLAYER', 'WORLD_CUP_SINGLE_PLAYER') and next_empty is not None:
            # Session 20260914-165156: on PLAYER/<slot> FIFA 18 immediately
            # requests /choices/player for that slot.  Advertise an empty slot
            # so the client never reopens an occupied one.  This is the only
            # mechanism proven to open the five-card picker, and it is only
            # acted on when the state is fetched on Draft-screen entry.
            state_param1 = 'PLAYER'
            state_param2 = str(next_empty)
        else:
            # Never auto-open the last slot: it is occupied after a pick and
            # PLAYER/<last slot> makes FIFA request that same slot again.
            state_param1 = 'INVALID'
            state_param2 = '-1'
    elif stage in ('READY_FOR_MATCH', 'READY_FOR_REWARDS', 'COMPLETED_DRAFT', 'MANAGER_DRAFT'):
        state_param1 = 'INVALID'
        # FIFA 18 posts manager choices with positionId=0.  Position 23 is the
        # sentinel used while finishing player slots, not a manager slot.
        state_param2 = '0'
    else:
        # Aurora 17 verified: PICK_DIFFICULTY, FORMATION_DRAFT, CAPTAIN_DRAFT all use stateParam2='0'
        state_param1 = 'INVALID'
        state_param2 = '0'

    stored_formation = str(st.get('formation') or '').strip()
    formation = stored_formation or 'f442'
    captain = 0
    try: cap_slot = int(st.get('captainSlot', -1))
    except Exception: cap_slot = -1
    cap = picked.get(str(cap_slot), picked.get(cap_slot)) if cap_slot >= 0 else None
    if isinstance(cap, dict): captain = int(cap.get('id', 0) or 0)

    players = []
    # Aurora17 (the open-source FIFA 17 server this backend is derived from, and
    # where Draft demonstrably works) builds this array in DraftCatalog
    # .PlayersArray as an unconditional loop over all 23 slots:
    #     filled: {"index":N,"kitNumber":N+1,"itemData":{...}}
    #     empty : {"index":N,"kitNumber":0}
    # Those are byte-identical to the rows this backend used to send, so the
    # rows themselves cannot be what FIFA 18 rejects, and the earlier
    # "empty rows crash Single Player" conclusion was correlation.
    #
    # One real difference remains: Aurora17 has no PICK_DIFFICULTY state at all.
    # Its first state is FORMATION_DRAFT (DraftCatalog.ChooseJson). Every
    # confirmed Single Player exit-on-rows at PICK_DIFFICULTY (6 sessions) is
    # therefore a document Aurora17 never produces, so rows start at the first
    # stage Aurora17 actually has.
    expose_empty_slots = _draft_exposes_empty_slots(st, stage, wire_mode)
    is_wc = wire_mode.startswith('WORLD_CUP')
    for idx in range(23):
        row = {'index': idx, 'kitNumber': 0}
        item = _slot_item(idx)
        if item is not None:
            row['kitNumber'] = idx + 1
            it = dict(item)
            if is_wc:
                it = _apply_world_cup_player_schema(it)
            row['itemData'] = it
            players.append(row)
        elif expose_empty_slots:
            players.append(row)

    manager = []
    if stage not in ('PICK_DIFFICULTY', 'FORMATION_DRAFT', 'CAPTAIN_DRAFT', 'PLAYER_DRAFT'):
        manager = _draft_wire_manager_entry(st.get('selectedManager'))

    # Aurora17 exposes all 23 indexed rows while a squad is being built.  The
    # empty rows are the native selectable Draft cards; omitting them leaves the
    # FIFA UI with undefined, non-interactive placeholders and only Complete
    # Squad can progress.  Completed/manager/reward states remain compact unless
    # a real item occupies the row.
    squad_id = 900002 if _draft_mode_name(st.get('mode') or mode).endswith('ONLINE') else _DRAFT_SQUAD_ID
    # Aurora17 emits chemistry and starRating as literal zeros in this document
    # (DraftCatalog.StateDocument concatenates the constant
    # `,"chemistry":0,"starRating":0,"players":`), even once a squad is built.
    # This backend sent computed values instead, which is one of the few real
    # differences from the working FIFA 17 contract. The authoritative numbers
    # are still stored in the Draft state and still travel on the squad and
    # /match contracts, so only this wire projection changes.
    squad = {
        'id': squad_id,
        'personaId': FAKE_PERSONA,
        'squadName': 'Draft Squad',
        'squadType': 'DRAFT_SQUAD',
        'active': False,
        'changed': 0,
        'formation': formation,
        'captain': captain,
        'chemistry': 0,
        'starRating': 0,
        'players': players,
        'manager': manager,
    }

    return [{
        'entranceCriteria': {'COINS': 15000, 'DRAFT_TOKEN': token_entry_cost, 'POINTS': 300},
        'gamesWonCurrentMatch': int(st.get('wins', 0) or 0),
        'roundsInfo': [],
        'squadState': stage,
        'stateParam1': state_param1,
        'stateParam2': state_param2,
        'squad': squad
    }]

def _draft_purchase(mode_id=0, doc=None, sku_mode=None):
    doc = doc if isinstance(doc, dict) else {}
    sku = str(sku_mode or doc.get('skuMode', '')).upper()
    is_wc = (sku == 'WC')
    if is_wc:
        mode = 'WORLD_CUP_ONLINE' if int(mode_id) == 0 else 'WORLD_CUP_SINGLE_PLAYER'
    else:
        mode = 'ONLINE' if int(mode_id) == 0 else 'SINGLE_PLAYER'
    st = _draft_get_state(mode)
    active = str(st.get('draftState', 'NOT_STARTED')).upper() not in ('NOT_STARTED', 'COMPLETE')
    if active:
        return [{'COINS': _credits(), 'POINTS': 0, 'DRAFT_TOKEN': int(st.get('draftToken', 0) or 0)}]
    token_count = int(st.get('draftToken', 0) or 0)
    payment = str(doc.get('currency', doc.get('currencyName', doc.get('paymentMethod', 'COINS'))) or 'COINS').upper()
    requested_token = payment in ('DRAFT_TOKEN', 'TOKEN') or bool(doc.get('useDraftToken', doc.get('useToken', False)))
    requested_points = payment == 'POINTS' or bool(doc.get('usePoints', False))
    if requested_token and token_count > 0:
        st['draftToken'] = token_count - 1
        _draft_save(st, mode)
        _meta_set('draftFreeEntryOnce', '1')
    elif requested_points:
        _meta_set('draftFreeEntryOnce', '1')
    elif _credits() < 15000:
        return [{'COINS': _credits(), 'POINTS': 0, 'DRAFT_TOKEN': token_count}]
    st = _draft_start(mode)
    log.warning('DRAFT PURCHASE COMPLETE mode=%s credits=%d', mode, _credits())
    return [{'COINS': _credits(), 'POINTS': 0, 'DRAFT_TOKEN': int(st.get('draftToken', 0) or 0)}]

def _draft_sync_occupancy_file(st, m_name):
    try:
        if _draft_mode_name(m_name) == 'SINGLE_PLAYER':
            stage = str(st.get('draftStage', '')).upper()
            is_player_draft = (stage == 'PLAYER_DRAFT')
            picked = st.get('pickedBySlot', {}) if isinstance(st.get('pickedBySlot'), dict) else {}
            mask = 0
            for i in range(23):
                if str(i) in picked or i in picked:
                    mask |= (1 << i)
            data = struct.pack('<II', 1 if is_player_draft else 0, mask)
            out_path = RUNTIME / 'draft_occupancy.bin'
            out_path.write_bytes(data)
    except Exception as e:
        log.warning('DRAFT OCCUPANCY SYNC failed: %s', e)

def _draft_save(st, mode=None):
    st = dict(st or {})
    m_name = st.get('mode') or mode or 'SINGLE_PLAYER'
    key = _draft_mode_key(m_name)
    _DRAFT_RUNTIME_ACTIVE_KEYS.add(key)
    _meta_set(key, json.dumps(st, separators=(',', ':')))
    _draft_sync_occupancy_file(st, m_name)
    return _draft_get_state(m_name)

def _draft_start(mode='SINGLE_PLAYER'):
    m_name=_draft_mode_name(mode)
    mode_key=_draft_mode_key(m_name)
    st = _draft_get_state(m_name)
    if st.get('draftState') in ('NOT_STARTED', 'COMPLETE') or not st.get('draftState'):
        fee = 15000
        free_entry = _meta_get('draftFreeEntryOnce', '') == '1'
        if (not free_entry) and _credits() < fee:
            st['canEnter'] = False
            st['error'] = 'NOT_ENOUGH_CREDITS'
            return st
        if free_entry:
            _meta_set('draftFreeEntryOnce', '0')
        else:
            _meta_set('credits', _credits() - fee)
        initial_stage = 'FORMATION_DRAFT' if m_name.endswith('ONLINE') else 'PICK_DIFFICULTY'
        st = {
            'mode': m_name,
            'draftMode': m_name,
            'draftId': 18002 if m_name == 'ONLINE' else 18001,
            'id': 18002 if m_name == 'ONLINE' else 18001,
            'draftState': 'IN_PROGRESS',
            'state': 'IN_PROGRESS',
            'status': 'IN_PROGRESS',
            'active': True,
            'draftStage': initial_stage,
            'draftToken': 0,
            'draftTokens': 0,
            'entryFee': fee,
            'entryCost': fee,
            'draftEntry': fee,
            'entryCurrency': 'FREE' if free_entry else 'COINS',
            'entryPaid': 0 if free_entry else fee,
            'wins': 0,
            'losses': 0,
            'matchesPlayed': 0,
            'draftsCompleted': int(st.get('draftsCompleted', 0) or 0),
            'round': 1,
            'difficulty': 0,
            'difficultyName': 'BEGINNER',
            'formation': '',
            'picks': [],
            'pickedBySlot': {},
            'captainSlot': -1,
            'lastDraftChoices': [],
            'lastDraftChoiceItems': [],
            'lastPositionId': 0,
            'completed': False,
            'prizeClaimed': False,
            'selectedManager': None
        }
    _DRAFT_RUNTIME_ACTIVE_KEYS.add(mode_key)
    st = _draft_save(st, m_name)
    st['canEnter'] = True
    return st

_DRAFT_SQUAD_ID=900001
_DRAFT_FORMATION_SLOTS={
    'f41212':('GK','RB','CB','CB','LB','CDM','CM','CM','CAM','ST','ST'),
    'f4141': ('GK','RB','CB','CB','LB','CDM','RM','CM','CM','LM','ST'),
    'f4222': ('GK','RB','CB','CB','LB','CDM','CDM','CAM','CAM','ST','ST'),
    'f4231': ('GK','RB','CB','CB','LB','CDM','CDM','CAM','CAM','CAM','ST'),
    'f4312': ('GK','RB','CB','CB','LB','CM','CM','CM','CAM','ST','ST'),
    'f4321': ('GK','RB','CB','CB','LB','CM','CM','CM','RF','LF','ST'),
    'f433':  ('GK','RB','CB','CB','LB','CM','CM','CM','RW','ST','LW'),
    'f4411': ('GK','RB','CB','CB','LB','RM','CM','CM','LM','CF','ST'),
    'f442':  ('GK','RB','CB','CB','LB','RM','CM','CM','LM','ST','ST'),
    'f451':  ('GK','RB','CB','CB','LB','RM','CM','LM','CM','CAM','ST'),
    'f3412': ('GK','CB','CB','CB','RM','CM','CM','LM','CAM','ST','ST'),
    'f3421': ('GK','CB','CB','CB','RM','CM','CM','LM','RF','ST','LF'),
    'f343':  ('GK','CB','CB','CB','RM','CM','CM','LM','RW','ST','LW'),
    'f352':  ('GK','CB','CB','CB','RM','CDM','CDM','LM','CAM','ST','ST'),
    'f5212': ('GK','RWB','CB','CB','CB','LWB','CM','CM','CAM','ST','ST'),
    'f5221': ('GK','RWB','CB','CB','CB','LWB','CM','CM','RW','ST','LW'),
    'f532':  ('GK','RWB','CB','CB','CB','LWB','CM','CM','CM','ST','ST'),
    'f541':  ('GK','RWB','CB','CB','CB','LWB','RM','CM','CM','LM','ST'),
}

def _draft_slot_position(formation,position_id):
    try:pid=int(position_id)
    except Exception:return 'ANY'
    if not 0<=pid<=10:return 'ANY'
    slots=_DRAFT_FORMATION_SLOTS.get(str(formation or '').lower(),())
    return slots[pid] if pid<len(slots) else 'ANY'

def _draft_allowed_positions(formation,position_id,mode=None):
    try:pid=int(position_id)
    except Exception:return None
    if pid==11:return {'GK'}
    if pid in (12,13):return {'RWB','RB','CB','LB','LWB'}
    if pid in (14,15):return {'CDM','RM','CM','LM','CAM'}
    if pid==16:return {'CDM','RM','CM','LM','CAM','RF','CF','LF','RW','ST','LW'}
    if pid==17:return {'RF','CF','LF','RW','ST','LW'}
    if 18<=pid<=22:return None
    target=_draft_slot_position(formation,pid)
    if target=='ANY':return None
    if mode is not None and _draft_mode_name(mode) in ('SINGLE_PLAYER', 'WORLD_CUP_SINGLE_PLAYER'):
        if target=='RF':return {'RF','RW'}
        if target=='LF':return {'LF','LW'}
    if target=='RF':target='RW'
    elif target=='LF':target='LW'
    if target=='LWB':return {'LWB','LB'}
    if target=='RWB':return {'RWB','RB'}
    return {target}

def _draft_console_slot_labels(formation):
    """23 display labels for the draft-console grid, reusing the real logic."""
    labels=[]
    for idx in range(23):
        if idx<=10:
            labels.append(_draft_slot_position(formation,idx))
        elif idx==11:
            labels.append('SUB GK')
        elif idx in (12,13):
            labels.append('SUB DEF')
        elif idx in (14,15):
            labels.append('SUB MID')
        elif idx==16:
            labels.append('SUB MID/ATT')
        elif idx==17:
            labels.append('SUB ATT')
        else:
            labels.append('RES')
    return labels

def _draft_state_squad(st=None, mode=None):
    if isinstance(st, str) and mode is None:
        mode = st
        st = None
    st=st or _draft_get_state(mode)
    stage=_draft_stage(st,mode)
    formation=str(st.get('formation') or 'f442')
    picked=st.get('pickedBySlot',{}) if isinstance(st.get('pickedBySlot'),dict) else {}
    players=[]
    expose_empty_slots=_draft_exposes_empty_slots(st,stage,mode)
    for idx in range(23):
        row={'index':idx,'kitNumber':0}
        item=picked.get(str(idx),picked.get(idx))
        if isinstance(item,dict) and int(item.get('assetId',0) or 0)>0:
            row['kitNumber']=idx+1;row['itemData']=dict(item)
        if expose_empty_slots or 'itemData' in row:players.append(row)
    captain=0
    try:cap_slot=int(st.get('captainSlot',-1))
    except Exception:cap_slot=-1
    cap=picked.get(str(cap_slot),picked.get(cap_slot)) if cap_slot>=0 else None
    if isinstance(cap,dict):captain=int(cap.get('id',0) or 0)
    sq_id = 900002 if _draft_mode_name(st.get('mode') or mode).endswith('ONLINE') else _DRAFT_SQUAD_ID
    return {
        'id':sq_id,'squadId':sq_id,'personaId':FAKE_PERSONA,
        'squadName':'Draft Squad','name':'Draft Squad','squadType':'DRAFT_SQUAD',
        'active':False,'valid':True,'changed':0,'formation':formation,'captain':captain,
        'chemistry':int(st.get('savedChemistry',0) or 0),'starRating':int(st.get('savedRating',0) or 0),'rating':int(st.get('savedRating',0) or 0),'players':players,
        'manager':_draft_wire_manager_entry(st.get('selectedManager')),
        'kicktakers':st.get('kicktakers') if isinstance(st.get('kicktakers'),list) and st.get('kicktakers') else (_resolve_squad_kicktakers([p['itemData'] for p in players if isinstance(p.get('itemData'),dict) and 0<=int(p.get('index',-1))<11], captain_id=captain) if len([p for p in players if isinstance(p.get('itemData'),dict) and 0<=int(p.get('index',-1))<11])==11 else []),
        'club':[],'actives':[],'newSquad':0,'newsquad':0,
    }

def _draft_apply_squad_layout(doc, mode=None):
    doc=doc if isinstance(doc,dict) else {}
    if mode is None:
        mode = 'ONLINE' if int(doc.get('id',0) or doc.get('squadId',0) or 0)==900002 else 'SINGLE_PLAYER'
    st=_draft_get_state(mode)
    rows=doc.get('players',[]) if isinstance(doc.get('players'),list) else []
    if rows:
        picked=st.get('pickedBySlot',{}) if isinstance(st.get('pickedBySlot'),dict) else {}
        owned=[dict(x) for x in picked.values() if isinstance(x,dict)]
        normal_sp=_draft_mode_name(mode) in ('SINGLE_PLAYER', 'WORLD_CUP_SINGLE_PLAYER')
        by_key={}
        for x in owned:
            for key in ('id','itemId','resourceId','definitionId','assetId'):
                try:v=int(x.get(key,0) or 0)
                except Exception:v=0
                if v:by_key.setdefault((key,v) if normal_sp else v,x)
        next_picked={}
        used_instances=set()
        seen_indices=set()
        layout_invalid=normal_sp and len(rows)>23
        for fallback,row in enumerate(rows[:23]):
            if not isinstance(row,dict):continue
            try:idx=_draft_parse_slot(row.get('index',fallback)) if normal_sp else int(row.get('index',fallback) or fallback)
            except DraftSlotError:
                layout_invalid=True
                break
            except Exception:idx=fallback
            if not 0<=idx<23:continue
            if normal_sp and idx in seen_indices:
                layout_invalid=True
                break
            seen_indices.add(idx)
            ref=row.get('itemData') if isinstance(row.get('itemData'),dict) else {}
            keys=[]
            for key in ('id','itemId','resourceId','definitionId','assetId'):
                try:v=int(ref.get(key,0) or 0)
                except Exception:v=0
                if v:keys.append((key,v) if normal_sp else v)
            found=next((by_key.get(v) for v in keys if v in by_key),None)
            if normal_sp and len({int(by_key[v].get('id',0) or 0) for v in keys if v in by_key})>1:
                layout_invalid=True
                break
            if found is not None:
                instance=int(found.get('id',found.get('itemId',0)) or 0)
                if normal_sp and (str(idx) in next_picked or instance in used_instances):
                    layout_invalid=True
                    break
                used_instances.add(instance)
                next_picked[str(idx)]=dict(found)
        if not layout_invalid and len(next_picked)==len(owned) and next_picked:
            st['pickedBySlot']=next_picked
            st['picks']=[int(next_picked[str(i)].get('resourceId',next_picked[str(i)].get('definitionId',next_picked[str(i)].get('assetId',0))) or 0) for i in sorted(int(k) for k in next_picked)]
            try:st['savedChemistry']=max(0,min(100,int(doc.get('chemistry',0) or 0)))
            except Exception:pass
            try:st['savedRating']=max(0,min(99,int(doc.get('rating',doc.get('starRating',0)) or 0)))
            except Exception:pass
        else:
            log.warning('DRAFT SQUAD SAVE rejected incomplete identity map matched=%d owned=%d; prior layout preserved',len(next_picked),len(owned))
    if doc.get('formation'):
        f=str(doc.get('formation') or '').lower()
        if f in _DRAFT_FORMATION_SLOTS:st['formation']=f
    try:
        cap=int(doc.get('captain',0) or 0)
        if cap:
            for k,item in (st.get('pickedBySlot',{}) or {}).items():
                if not isinstance(item,dict):continue
                ids={int(item.get(x,0) or 0) for x in ('id','itemId','resourceId','definitionId','assetId')}
                if cap in ids:st['captainSlot']=int(k);break
    except Exception:pass
    mgr_rows = doc.get('manager')
    if isinstance(mgr_rows, list) and mgr_rows:
        mrow = mgr_rows[0] if isinstance(mgr_rows[0], dict) else {}
        mdata = mrow.get('itemData') if isinstance(mrow.get('itemData'), dict) else {}
        try: mid = int(mdata.get('id', mrow.get('id', 0)) or 0)
        except Exception: mid = 0
        if mid > 0:
            items = _db_item_map()
            if mid in items:
                st['selectedManager'] = _upgrade_draft_manager_item(items[mid])
            elif mdata and int(mdata.get('assetId', mdata.get('headId', 0)) or 0) > 0:
                st['selectedManager'] = _upgrade_draft_manager_item(mdata)
            chem, rating = _draft_compute_metrics(st)
            st['savedChemistry'] = chem
            st['savedRating'] = rating
    kt = doc.get('kicktakers')
    if isinstance(kt, list) and kt:
        st['kicktakers'] = kt
    _draft_save(st, mode)
    return {'id': 900002 if _draft_mode_name(mode).endswith('ONLINE') else _DRAFT_SQUAD_ID}

def _draft_item_identity_values(item):
    vals=[]
    if not isinstance(item,dict):return vals
    for key in ('id','itemId','resourceId','definitionId','assetId'):
        try:v=int(item.get(key,0) or 0)
        except Exception:v=0
        if v and v not in vals:vals.append(v)
    return vals

_DRAFT_POSITION_FAMILIES=(
    {'CDM','CM','CAM'},{'LM','LW','LF'},{'RM','RW','RF'},
    {'ST','CF'},{'LB','LWB'},{'RB','RWB'},
)

def _draft_position_fit(preferred,target):
    p=str(preferred or '').upper();t=str(target or '').upper()
    if p and p==t:return 3
    for family in _DRAFT_POSITION_FAMILIES:
        if p in family and t in family:return 2
    return 0

def _draft_compute_metrics(st):
    picked=st.get('pickedBySlot',{}) if isinstance(st.get('pickedBySlot'),dict) else {}
    starters=[picked.get(str(i),picked.get(i)) for i in range(11)]
    starters=[x if isinstance(x,dict) else {} for x in starters]
    ratings=[max(0,int(x.get('rating',0) or 0)) for x in starters]
    rating=int(sum(ratings)/len(ratings)) if len(ratings)==11 and all(ratings) else 0
    slots=_DRAFT_FORMATION_SLOTS.get(str(st.get('formation') or '').lower(),())
    manager=_upgrade_draft_manager_item(st.get('selectedManager')) if isinstance(st.get('selectedManager'),dict) else {}
    try:mleague=int(manager.get('leagueId',manager.get('managerLeagueId',0)) or 0)
    except Exception:mleague=0
    try:mnation=int(manager.get('nation',manager.get('nationId',0)) or 0)
    except Exception:mnation=0
    chemistry=0
    for i,x in enumerate(starters):
        if not x:continue
        preferred=x.get('preferredPosition',x.get('position',''))
        target=slots[i] if i<len(slots) else ''
        fit=_draft_position_fit(preferred,target)
        try:club=int(x.get('teamid',x.get('teamId',x.get('clubId',0))) or 0)
        except Exception:club=0
        try:league=int(x.get('leagueId',0) or 0)
        except Exception:league=0
        try:nation=int(x.get('nation',x.get('nationId',0)) or 0)
        except Exception:nation=0
        relationship=0
        for j,y in enumerate(starters):
            if i==j or not y:continue
            try:yclub=int(y.get('teamid',y.get('teamId',y.get('clubId',0))) or 0)
            except Exception:yclub=0
            try:yleague=int(y.get('leagueId',0) or 0)
            except Exception:yleague=0
            try:ynation=int(y.get('nation',y.get('nationId',0)) or 0)
            except Exception:ynation=0
            if club and yclub==club:relationship=max(relationship,3)
            elif league and yleague==league:relationship=max(relationship,2)
            elif nation and ynation==nation:relationship=max(relationship,1)
        manager_bonus=1 if ((league and league==mleague) or (nation and nation==mnation)) else 0
        chemistry+=min(10,fit+relationship+manager_bonus+1)
    return max(0,min(100,chemistry)),max(0,min(99,rating))

def _draft_advance_to_manager(st,mode='SINGLE_PLAYER',source='player selection'):
    """Move a complete 23-player Draft to FIFA 18's required manager step."""
    if _draft_mode_name(mode) not in ('SINGLE_PLAYER', 'WORLD_CUP_SINGLE_PLAYER'):return False
    picked=st.get('pickedBySlot',{}) if isinstance(st.get('pickedBySlot'),dict) else {}
    if len(picked)<23:return False
    st.pop('selectedManager',None)
    st['lastDraftChoices']=[];st['lastDraftChoiceItems']=[];st['lastPositionId']=0
    chem,rating=_draft_compute_metrics(st)
    st['savedChemistry']=chem;st['savedRating']=rating
    st['draftStage']='MANAGER_DRAFT';st['active']=True;st['completed']=False
    log.warning('DRAFT PLAYERS COMPLETE mode=%s source=%s players=%d chemistry=%d rating=%d managerRequired=1',mode,source,len(picked),chem,rating)
    return True

def _draft_apply_swap_snapshot(st,swap_ids):
    if not isinstance(st,dict) or not isinstance(swap_ids,list):return False
    refs=[]
    for x in swap_ids[:23]:
        try:v=int(x or 0)
        except Exception:v=0
        refs.append(v)
    picked=st.get('pickedBySlot',{}) if isinstance(st.get('pickedBySlot'),dict) else {}
    owned=[dict(x) for x in picked.values() if isinstance(x,dict)]
    if len(refs)!=23 or any(v<=0 for v in refs) or len(owned)!=23:return False
    old_cap=None
    try:old_cap=picked.get(str(int(st.get('captainSlot',-1))),picked.get(int(st.get('captainSlot',-1))))
    except Exception:old_cap=None
    by_key={}
    for item in owned:
        for v in _draft_item_identity_values(item):by_key.setdefault(v,[]).append(item)
    used=set();ordered=[]
    for ref in refs:
        candidates=by_key.get(ref,[])
        found=next((x for x in candidates if int(x.get('id',x.get('itemId',0)) or 0) not in used),None)
        if found is None:return False
        iid=int(found.get('id',found.get('itemId',0)) or 0)
        used.add(iid);ordered.append(dict(found))
    if len(ordered)!=23:return False
    st['pickedBySlot']={str(i):ordered[i] for i in range(23)}
    st['picks']=[int(x.get('resourceId',x.get('definitionId',x.get('assetId',0))) or 0) for x in ordered]
    if isinstance(old_cap,dict):
        old_ids=set(_draft_item_identity_values(old_cap))
        for i,item in enumerate(ordered):
            if old_ids.intersection(_draft_item_identity_values(item)):
                st['captainSlot']=i;break
    chem,rating=_draft_compute_metrics(st);st['savedChemistry']=chem;st['savedRating']=rating
    st['authoritativeSwapPlayerDefIds']=refs
    return True

_DRAFT_MANAGERS=(
    (1067,'Antonio','Conte',27,13,1),
    (2629,'Alex','Neil',42,14,0),
    (5571,'Diego','Simeone',52,53,1),
    (8213,'Aitor','Karanka',45,14,0),
    (8885,'Mauricio','Pochettino',52,13,1),
    (51404,'Sean','Dyche',14,13,1),
    (53951,'Roberto','Martínez',45,13,0),
    (167942,'Claude','Puel',18,13,1),
    (169894,'Pep','Guardiola',45,13,1),
    (174609,'Tony','Pulis',50,13,1),
    (183617,'Slaven','Bilić',10,13,1),
    (220467,'Roy','Hodgson',14,13,1),
    (220470,'Louis','van Gaal',34,13,1),
    (232297,'Eddie','Howe',14,13,1),
    (232298,'Arsène','Wenger',18,13,1),
    (232300,'Alan','Pardew',14,13,1),
    (232301,'Claudio','Ranieri',27,16,1),
    (232302,'Jürgen','Klopp',21,13,1),
    (232303,'Ronald','Koeman',34,13,1),
    (232304,'Mark','Hughes',50,13,1),
    (232305,'Sam','Allardyce',14,13,1),
    (232306,'Francesco','Guidolin',27,31,1),
    (232307,'Quique','Sánchez Flores',45,53,1),
    (232425,'José','Mourinho',38,13,1),
    (233859,'Rafa','Benítez',45,13,0),
    (234529,'Walter','Mazzarri',27,31,1),
    (234530,'Steve','Bruce',14,14,1),
    (235732,'David','Moyes',42,13,1),
    (236408,'Mike','Phelan',14,14,0),
    (237388,'Carlo','Ancelotti',27,19,1),
    (237389,'Unai','Emery',45,16,1),
    (237870,'Curt','Onalfo',95,39,0),
    (238399,'Zinedine','Zidane',18,53,1),
    (239228,'Paul','Clement',14,13,1),
    (239229,'Craig','Shakespeare',14,13,1),
    (240199,'David','Wagner',95,13,1),
    (240488,'Chris','Hughton',14,13,1),
    (241,'Ryan','Giggs',50,13,1),
    (1041,'Gennaro','Gattuso',27,31,1),
    (1088,'Patrick','Vieira',18,39,1),
    (1134,'Clarence','Seedorf',34,53,1),
    (1397,'Jaap','Stam',34,14,0),
    (1409,'Paul','Lambert',42,13,0),
)

_DRAFT_MANAGER_WIRE_VERSION=4

def _draft_manager_item(asset,first,last,nation,league,rare,item_id=0):
    asset=int(asset);resource=asset
    name=(str(first)+' '+str(last)).strip()
    iid=int(item_id or _draft_allocate_item_ids(1)[0])
    return {
        'id':iid,'itemId':iid,'assetId':asset,'headId':asset,'managerId':asset,
        'definitionId':resource,'resourceId':resource,'resourceGameYear':2019,
        'itemType':'manager','cardsubtypeid':4,'itemState':'free','rating':75+(5 if rare else 0),
        'rareflag':int(rare),'rareFlag':int(rare),'nation':int(nation),'nationId':int(nation),
        'leagueId':int(league),'managerLeagueId':int(league),'teamid':0,'teamId':0,
        'owners':1,'untradeable':True,'tradeable':False,'contract':99,'contracts':99,
        'pile':0,'loans':0,'formation':'f442','discardValue':0,'lastSalePrice':0,'timestamp':BOOT_TIME,
        'name':name,'displayName':name,'managerName':name,'commonName':name,'commonname':name,
        'firstName':str(first),'lastName':str(last),'negotiation':3 if rare else 1,
        # Managers use the standard staff-card presentation, so concept/dream
        # stay disabled.  draftItem must remain enabled because the manager is
        # still a temporary Draft entity during the Summary transition.
        'label':name,'description':name,'concept':False,'dream':False,'draftItem':True,
        'nativeDraftManagerWire':_DRAFT_MANAGER_WIRE_VERSION,
    }

def _upgrade_draft_manager_item(item):
    if not isinstance(item,dict):return item
    try:asset=int(item.get('assetId',item.get('headId',0)) or 0)
    except Exception:asset=0
    row=next((x for x in _DRAFT_MANAGERS if int(x[0])==asset),None)
    if not row:return dict(item)
    fixed=_draft_manager_item(*row,item_id=int(item.get('id',item.get('itemId',0)) or 0))
    skip_keys=('resourceId','definitionId','name','displayName','managerName','commonName','commonname',
               'firstName','lastName','label','description','contract','contracts','pile','loans',
               'dream','concept','draftItem','negotiation','nativeDraftManagerWire',
               'nation','nationId','leagueId','managerLeagueId','rating','rareflag','rareFlag','cardsubtypeid','itemType','assetId','headId','managerId')
    for k,v in item.items():
        if k not in skip_keys:
            fixed[k]=v
    try:contracts=int(item.get('contract',item.get('contracts',99)) or 99)
    except Exception:contracts=99
    fixed['contract']=max(99,contracts);fixed['contracts']=max(99,contracts);fixed['loans']=0
    return fixed

def _draft_manager_choices(count=5):
    rows=[]
    chosen=random.sample(_DRAFT_MANAGERS, min(count,len(_DRAFT_MANAGERS))) if len(_DRAFT_MANAGERS)>=count else list(_DRAFT_MANAGERS)
    ids=_draft_allocate_item_ids(len(chosen))
    for iid,row in zip(ids,chosen):
        rows.append(_draft_manager_item(*row,item_id=iid))
    return rows

def _draft_player_choices(count=5,allowed_positions=None,captain=False,mode='SINGLE_PLAYER'):
    is_wc = _draft_mode_name(mode).startswith('WORLD_CUP')
    pool=[d for d in _all_player_defs() if int(d.get('rating',0) or 0)>0]
    if is_wc:
        wc_icon_list = _world_cup_icons()
        wc_non_icons = [d for d in pool if int(d.get('nation', d.get('nationId', 0)) or 0) in WC_32_NATIONS and str(d.get('specialType', '')).upper() not in ('ICON', 'TOTS', 'TOTW', 'TOTY', 'OTW', 'HALLOWEEN')]
        pool = wc_non_icons + wc_icon_list
    if allowed_positions:
        allowed={str(x).upper() for x in allowed_positions}
        exact=[d for d in pool if str(d.get('position','')).upper() in allowed]
        if exact:pool=exact
    st=_draft_get_state(mode);picked=st.get('pickedBySlot',{}) if isinstance(st.get('pickedBySlot'),dict) else {}
    used_assets={int(x.get('assetId',0) or 0) for x in picked.values() if isinstance(x,dict)}
    used_icons={str(x.get('name','')).strip().lower() for x in picked.values() if isinstance(x,dict) and str(x.get('specialType','')).upper()=='ICON'}
    fresh=[]
    for d in pool:
        if int(d.get('assetId',0) or 0) in used_assets:continue
        if str(d.get('specialType','')).upper()=='ICON' and str(d.get('name','')).strip().lower() in used_icons:continue
        fresh.append(d)
    if len(fresh)>=count:pool=fresh
    ranked=sorted(pool,key=lambda d:int(d.get('rating',0) or 0),reverse=True)
    icons=[d for d in ranked if str(d.get('specialType','')).upper()=='ICON']
    regular_limit = max(35, count) if allowed_positions else max(240, count)
    top_regular=[d for d in ranked if str(d.get('specialType','')).upper()!='ICON'][:regular_limit]
    top=icons+top_regular or pool
    by_asset={}
    for d in top:
        aid=int(d.get('assetId',0) or 0)
        key=('icon',str(d.get('name','')).strip().lower()) if str(d.get('specialType','')).upper()=='ICON' else ('asset',aid)
        prev=by_asset.get(key)
        if prev is None or (int(d.get('rating',0) or 0),1 if _is_special_def(d) else 0)>(int(prev.get('rating',0) or 0),1 if _is_special_def(prev) else 0):
            by_asset[key]=d
    unique=list(by_asset.values());random.shuffle(unique)
    chosen=unique[:min(count,len(unique))]
    rows=[]
    instance_ids=_draft_allocate_item_ids(len(chosen)) if chosen else []
    for iid,d in zip(instance_ids,chosen):
        item=_definition_item(d,pile=0,rare=None if _is_special_def(d) else int(d.get('rating',0) or 0)>=75,
                              item_id=iid)
        if is_wc:
            item = _apply_world_cup_player_schema(item, defn=d)
            item['skuMode'] = 'WC'
        item['contract']=99;item['contracts']=99;item['loans']=0;item['fitness']=99
        item['concept']=True;item['untradeable']=True;item['tradeable']=False;item['draftItem']=True
        rows.append(item)
    return rows

@_draft_sp_serialized(3)
def _draft_payload_choices(kind,query=None,doc=None,mode='SINGLE_PLAYER'):
    _DRAFT_RUNTIME_ACTIVE_KEYS.add(_draft_mode_key(mode))
    st=_draft_get_state(mode);kind=str(kind).lower();doc=doc if isinstance(doc,dict) else {};query=query or {}
    if str(st.get('draftState','NOT_STARTED')).upper()=='NOT_STARTED':
        return {'choices':[],'positionid':0,'tier':0}
    if kind=='difficulty':
        return {'choices':[{'choiceIndex':i,'difficulty':i,'difficultyName':name} for i,name in enumerate(_DRAFT_DIFFICULTY_NAMES)]}
    if kind=='formation':
        return {'choices':[{'formation':x,'index':i} for i,x in enumerate(_DRAFT_FORMATIONS)],'positionid':0,'tier':0}
    if _draft_mode_name(mode) == 'SINGLE_PLAYER' and kind in ('player','captain'):
        raw_position=doc.get('positionId',doc.get('positionid'))
        if raw_position is None:
            value=query.get('positionId',query.get('positionid'))
            raw_position=value[0] if isinstance(value,list) and value else value
        if kind=='captain' and raw_position is None:
            raw_position=0
        picked = st.get('pickedBySlot', {}) if isinstance(st.get('pickedBySlot'), dict) else {}
        next_empty = next((i for i in range(23) if str(i) not in picked and i not in picked), None)
        try:
            position_id=_draft_parse_slot(raw_position)
            # Auto-advance: FIFA 18's native client re-requests choices for the
            # slot it just chose (e.g. pick slot 4, then immediately PUT
            # /choices/player with positionId=4). That slot is now occupied, so
            # the server used to return choices=[] which broke the auto-chain.
            # Only advance when the requested slot matches lastPositionId (the
            # "just-chose-this" re-request pattern).
            if kind == 'player' and (str(position_id) in picked or position_id in picked):
                last_pos = st.get('lastPositionId')
                if last_pos is not None and (position_id == last_pos or str(position_id) == str(last_pos)):
                    if next_empty is not None:
                        log.warning('DRAFT SLOT OFFER auto-advancing occupied slot %s to next empty slot %s', position_id, next_empty)
                        position_id = next_empty
                    elif len(picked) >= 23:
                        log.warning('DRAFT SLOT OFFER all 23 slots filled; redirecting to manager choices')
                        return _draft_payload_choices('manager', query=query, doc=doc, mode=mode)
            allowed=None if kind=='captain' else _draft_allowed_positions(st.get('formation'),position_id,mode=mode)
            vals=_draft_slot_offer(st,position_id,
                lambda: _draft_player_choices(5,allowed_positions=allowed,captain=kind=='captain',mode=mode),
                captain=kind=='captain')
        except DraftSlotError as exc:
            log.warning('DRAFT SLOT OFFER rejected mode=%s kind=%s position=%r reason=%s',mode,kind,raw_position,exc)
            return {'choices':[],'positionid':-1,'tier':0}
        # Compatibility fields for retail DTO/manager routing. Authoritative
        # player offers now live per-slot and survive requests for other slots.
        st['lastDraftChoices']=[int(x.get('resourceId',x.get('definitionId',x.get('assetId',0))) or 0) for x in vals]
        st['lastDraftChoiceItems']=[dict(x) for x in vals]
        st['lastDraftChoiceKind']=kind;st['lastPositionId']=position_id
        _draft_save(st,mode)
        log.warning('DRAFT SLOT OFFER mode=%s kind=%s positionId=%s count=%d',mode,kind,position_id,len(vals))
        return {'choices':[{'index':i,'itemData':item} for i,item in enumerate(vals)],'positionid':position_id,'tier':0}
    if kind=='manager':
        # The manager-picker experiment (serving player cards through the
        # Manager card so a five-card pick needed no hub round trip) was tried
        # natively by the user and did not work; it is removed rather than left
        # as a disabled branch.
        picked_now=st.get('pickedBySlot',{}) if isinstance(st.get('pickedBySlot'),dict) else {}
        stage_now=_draft_stage(st, mode)
        vals=_draft_manager_choices(5)
        st['lastDraftChoices']=[int(x.get('resourceId',0) or 0) for x in vals]
        st['lastDraftChoiceItems']=[dict(x) for x in vals]
        st['lastPositionId']=0
        st['lastDraftChoiceKind']='manager'
        picked=st.get('pickedBySlot',{}) if isinstance(st.get('pickedBySlot'),dict) else {}
        stage=_draft_stage(st, mode)
        if stage in ('CAPTAIN_DRAFT','PLAYER_DRAFT') and len(picked)<23:
            # The manager card is selectable at any time.  Session
            # 20260915-082930 flipped a 6-player squad to MANAGER_DRAFT here and
            # the choose then started a match with 6 players.  Stay in the build
            # stage; _draft_choose treats the pick as an in-screen refresh.
            log.warning('DRAFT EARLY MANAGER CHOICES mode=%s stage=%s players=%d',mode,stage,len(picked))
        else:
            st['draftStage']='MANAGER_DRAFT'
        _draft_save(st, mode)
        return {'choices':[{'index':i,'itemData':item} for i,item in enumerate(vals)],'positionid':0,'tier':0}
    position_id=0
    for v in (doc.get('positionId'),doc.get('positionid'),(query.get('positionId') or query.get('positionid') or [None])[0] if query else None):
        try:
            if v is not None:position_id=int(v);break
        except Exception:pass
    if kind=='captain':
        allowed=None
        vals=_draft_player_choices(5,allowed_positions=allowed,captain=True,mode=mode)
        target='CAPTAIN'
    else:
        allowed=_draft_allowed_positions(st.get('formation'),position_id)
        vals=_draft_player_choices(5,allowed_positions=allowed,captain=False,mode=mode)
        target=_draft_slot_position(st.get('formation'),position_id)
    st['lastDraftChoices']=[int(x.get('resourceId',x.get('definitionId',x.get('assetId',0))) or 0) for x in vals]
    st['lastDraftChoiceItems']=[dict(x) for x in vals]
    st['lastDraftChoiceKind']=kind
    st['lastPositionId']=position_id;_draft_save(st, mode)
    rows=[{'index':i,'itemData':item} for i,item in enumerate(vals)]
    log.warning('DRAFT POSITION LOCK mode=%s formation=%s positionId=%s target=%s allowed=%s',
                mode,st.get('formation'),position_id,target,sorted(allowed) if allowed else ['ANY'])
    return {'choices':rows,'positionid':position_id,'tier':0}

def _draft_offer_token(st, position_id, items):
    payload=[st.get('formation'),position_id,items]
    return hashlib.sha256(_json_bytes(payload)).hexdigest()


@_draft_sp_serialized(1)
def _draft_select_action(doc=None, mode='SINGLE_PLAYER'):
    """Explicit bridge API. The caller is responsible for displaying a picker."""
    if _draft_mode_name(mode) not in ('SINGLE_PLAYER', 'WORLD_CUP_SINGLE_PLAYER'):
        return {'success':False,'error':'unsupported_mode'}
    if not isinstance(doc,dict):
        return {'success':False,'error':'invalid_document'}
    st=_draft_get_state(mode)
    if st.get('draftStage')!='PLAYER_DRAFT' or _draft_stage(st,mode)!='PLAYER_DRAFT':
        return {'success':False,'error':'not_player_draft'}
    try:
        position_id=_draft_parse_slot(doc.get('positionId'))
    except DraftSlotError:
        return {'success':False,'error':'invalid_slot'}
    picked=st.get('pickedBySlot',{})
    if not isinstance(picked,dict) or any(not isinstance(item,dict) for item in picked.values()):
        return {'success':False,'error':'invalid_state'}
    if str(position_id) in picked or position_id in picked:
        return {'success':False,'error':'occupied_slot'}
    action=doc.get('action','select')
    if action=='select':
        try:
            allowed=_draft_allowed_positions(st.get('formation'),position_id,mode=mode)
            items=_draft_slot_offer(st,position_id,
                lambda: _draft_player_choices(5,allowed_positions=allowed,mode=mode))
        except DraftSlotError:
            return {'success':False,'error':'invalid_offer'}
        if not items:
            return {'success':False,'error':'no_eligible_cards'}
        _DRAFT_RUNTIME_ACTIVE_KEYS.add(_draft_mode_key(mode))
        _draft_save(st,mode)
        return {'success':True,'positionId':position_id,
                'choices':[{'index':i,'itemData':item} for i,item in enumerate(items)],
                'offerToken':_draft_offer_token(st,position_id,items)}
    if action!='confirm':
        return {'success':False,'error':'invalid_action'}
    offers=st.get('slotOffers')
    items=offers.get(str(position_id)) if isinstance(offers,dict) else None
    if not isinstance(items,list) or not items:
        return {'success':False,'error':'missing_offer'}
    token=doc.get('offerToken')
    if (st.get('slotOffersFormation')!=st.get('formation') or not isinstance(token,str)
            or token!=_draft_offer_token(st,position_id,items)):
        return {'success':False,'error':'stale_offer'}
    raw_choice=doc.get('choiceIndex')
    try:
        choice=int(raw_choice)
        if (isinstance(raw_choice,bool) or str(choice)!=str(raw_choice).strip()
                or not 0<=choice<len(items)):
            raise ValueError()
    except (ValueError,TypeError,OverflowError):
        return {'success':False,'error':'invalid_choice'}
    if not isinstance(items[choice],dict):
        return {'success':False,'error':'invalid_offer'}
    expected=items[choice].get('id')
    try:
        _draft_slot_choose(st,position_id,choice)
    except DraftSlotError:
        return {'success':False,'error':'choice_rejected'}
    if len(st.get('pickedBySlot',{}))>=23:
        if not _draft_advance_to_manager(st,mode,'23rd explicit bridge player choice'):
            st['draftStage']='MANAGER_DRAFT'
    _DRAFT_RUNTIME_ACTIVE_KEYS.add(_draft_mode_key(mode))
    _draft_save(st,mode)
    chosen=st['pickedBySlot'].get(str(position_id))
    if not isinstance(chosen,dict) or chosen.get('id')!=expected:
        return {'success':False,'error':'choice_rejected'}
    return {'success':True,'positionId':position_id,'itemData':chosen,
            'stage':_draft_stage(st,mode)}


def _draft_choose_difficulty(doc=None,mode='SINGLE_PLAYER'):
    _DRAFT_RUNTIME_ACTIVE_KEYS.add(_draft_mode_key(mode))
    st=_draft_get_state(mode);doc=doc if isinstance(doc,dict) else {}
    raw=str(doc.get('difficultyName','') or '').upper().replace('_','').replace('-','').replace(' ','')
    names=list(_DRAFT_DIFFICULTY_NAMES)
    aliases={'PROFESSIONAL':'PRO','SEMI-PRO':'SEMIPRO'}
    raw=aliases.get(raw,raw)
    try:
        if raw in names:wire=names.index(raw)
        elif 'difficulty' in doc:wire=int(doc.get('difficulty',0) or 0)
        else:wire=int(doc.get('choiceIndex',0) or 0)
    except Exception:wire=1
    st['difficulty']=max(0,min(len(names)-1,wire));st['difficultyName']=names[st['difficulty']]
    st['draftStage']='FORMATION_DRAFT';_draft_save(st, mode)
    log.warning('DRAFT DIFFICULTY STORED mode=%s requested=%r name=%s gameplay=%d',mode,doc,st['difficultyName'],st['difficulty'])
    return {}

@_draft_sp_serialized(1)
def _draft_choose(doc=None,mode='SINGLE_PLAYER'):
    _DRAFT_RUNTIME_ACTIVE_KEYS.add(_draft_mode_key(mode))
    st=_draft_get_state(mode);doc=doc if isinstance(doc,dict) else {};stage=_draft_stage(st, mode)
    if stage=='PICK_DIFFICULTY' or 'difficulty' in doc or 'difficultyName' in doc:
        return _draft_choose_difficulty(doc,mode)
    swap_ids=doc.get('swapPlayerDefIds') if isinstance(doc.get('swapPlayerDefIds'),list) else None
    if swap_ids:_draft_apply_swap_snapshot(st,swap_ids)
    try:choice=int(doc.get('choiceIndex',doc.get('index',0)) or 0)
    except Exception:choice=0
    try:position_id=int(doc.get('positionId',doc.get('positionid',st.get('lastPositionId',0))) or 0)
    except Exception:position_id=0
    normal_sp=_draft_mode_name(mode) == 'SINGLE_PLAYER'
    explicit_position='positionId' in doc or 'positionid' in doc
    if (normal_sp and stage in ('CAPTAIN_DRAFT','PLAYER_DRAFT') and not explicit_position
            and st.get('lastDraftChoiceKind')!='manager'):
        log.warning('DRAFT SLOT CHOOSE rejected missing explicit positionId')
        return {}
    pending=st.get('slotOffers')
    pending=pending if isinstance(pending,dict) else {}
    offer_key='captain' if stage=='CAPTAIN_DRAFT' else str(position_id)
    # Native manager chooses use slot0. Without a kind/token, slot0 is
    # ambiguous after a manager offer: retain the existing no-placement guard.
    explicit_player_offer=(normal_sp and explicit_position and position_id!=0
                           and not swap_ids and isinstance(pending.get(offer_key),list))
    if stage=='FORMATION_DRAFT':
        st['formation']=_DRAFT_FORMATIONS[choice] if 0<=choice<len(_DRAFT_FORMATIONS) else _DRAFT_FORMATIONS[0]
        st['draftStage']='CAPTAIN_DRAFT';st['lastPositionId']=0
        st['pickedBySlot']={};st['picks']=[];st['captainSlot']=-1
        st.pop('slotOffers',None);st.pop('slotOffersFormation',None)
        st['lastDraftChoices']=[];st['lastDraftChoiceItems']=[];st['lastDraftChoiceKind']=''
    elif (stage in ('CAPTAIN_DRAFT','PLAYER_DRAFT') and st.get('lastDraftChoiceKind')=='manager'
            and not explicit_player_offer
            and isinstance(st.get('lastDraftChoiceItems'),list) and st['lastDraftChoiceItems']
            and all(isinstance(x,dict) and str(x.get('itemType','')).lower()=='manager' for x in st['lastDraftChoiceItems'])):
        # Early manager pick with an incomplete squad: FIFA 18 re-fetches the
        # Draft state right after it (session 20260915-082930), which is the
        # only proven in-screen refresh.  Keep the squad and stage unchanged so
        # the state can offer the next empty slot; the real manager is chosen
        # after the 23rd player (_draft_advance_to_manager).
        st['lastDraftChoices']=[];st['lastDraftChoiceItems']=[];st['lastDraftChoiceKind']=''
        log.warning('DRAFT EARLY MANAGER CHOOSE ignored mode=%s stage=%s players=%d choice=%s',mode,stage,len(st.get('pickedBySlot') or {}),choice)
    elif stage in ('CAPTAIN_DRAFT','PLAYER_DRAFT'):
        if _draft_mode_name(mode) == 'SINGLE_PLAYER':
            # Parse explicitly: malformed input must not silently select GK0 or
            # choice0. Legacy modes keep their existing wire behavior below.
            raw_position=doc.get('positionId',doc.get('positionid',st.get('lastPositionId')))
            raw_choice=doc.get('choiceIndex',doc.get('index'))
            try:
                position_id=_draft_parse_slot(raw_position)
                if isinstance(raw_choice,bool) or raw_choice is None:
                    raise DraftSlotError('invalid choice index')
                choice=int(raw_choice)
                if str(choice)!=str(raw_choice).strip():
                    raise DraftSlotError('invalid choice index')
                picked = st.get('pickedBySlot', {}) if isinstance(st.get('pickedBySlot'), dict) else {}
                if stage == 'PLAYER_DRAFT' and position_id == 0 and (str(0) in picked or 0 in picked):
                    last_pos = st.get('lastPositionId')
                    offers = st.get('slotOffers', {}) if isinstance(st.get('slotOffers'), dict) else {}
                    if last_pos is not None and str(last_pos) not in picked and last_pos not in picked and (str(last_pos) in offers or not offers):
                        position_id = int(last_pos)
                    elif offers:
                        open_offers = [int(k) for k in offers if k.isdigit() and k not in picked and int(k) not in picked]
                        if open_offers:
                            position_id = open_offers[0]
                    if str(position_id) in picked or position_id in picked:
                        first_empty = next((i for i in range(23) if str(i) not in picked and i not in picked), None)
                        if first_empty is not None:
                            position_id = first_empty
                    log.warning('DRAFT POSITION DISAMBIGUATED from 0 to %s (lastPositionId=%s)', position_id, last_pos)
                    if isinstance(st.get('slotOffers'), dict) and str(position_id) not in st['slotOffers']:
                        if isinstance(st.get('lastDraftChoiceItems'), list) and st['lastDraftChoiceItems']:
                            st['slotOffers'][str(position_id)] = list(st['lastDraftChoiceItems'])
                            st['slotOffersFormation'] = str(st.get('formation', ''))
                _draft_slot_choose(st,position_id,choice,captain=stage=='CAPTAIN_DRAFT')
            except (DraftSlotError,ValueError,TypeError,OverflowError) as exc:
                log.warning('DRAFT SLOT CHOOSE rejected mode=%s position=%r choice=%r reason=%s',mode,raw_position,raw_choice,exc)
                return {}
            st['lastDraftChoices']=[];st['lastDraftChoiceItems']=[];st['lastDraftChoiceKind']=''
            if len(st.get('pickedBySlot',{}))>=23:
                if not _draft_advance_to_manager(st,mode,'23rd per-slot player choice'):
                    st['draftStage']='MANAGER_DRAFT'
            _draft_save(st,mode)
            log.warning('DRAFT SLOT CHOOSE mode=%s positionId=%s players=%d',mode,position_id,len(st.get('pickedBySlot',{})))
            return {}
        cached=st.get('lastDraftChoiceItems',[]) if isinstance(st.get('lastDraftChoiceItems'),list) else []
        chosen=dict(cached[choice]) if 0<=choice<len(cached) and isinstance(cached[choice],dict) else None
        picked=st.get('pickedBySlot',{}) if isinstance(st.get('pickedBySlot'),dict) else {}
        try:offered_position=int(st.get('lastPositionId',-1))
        except Exception:offered_position=-1
        reject=None
        if not 0<=position_id<23:
            reject='position out of range'
        elif isinstance(picked.get(str(position_id)),dict):
            reject='slot already occupied'
        elif chosen is None:
            reject='no offered choice'
        elif stage=='PLAYER_DRAFT' and position_id!=offered_position:
            reject=f'choices were offered for position {offered_position}'
        if reject:
            # Keep the saved Draft untouched: a retried or stale choose must not
            # overwrite a card or reuse one card in a second slot.
            log.warning('DRAFT CHOOSE rejected mode=%s stage=%s positionId=%s choice=%s reason=%s',mode,stage,position_id,choice,reject)
            return {}
        picked[str(position_id)]=chosen;st['pickedBySlot']=picked
        st['picks']=[int(x.get('resourceId',x.get('definitionId',x.get('assetId',0))) or 0) for x in picked.values() if isinstance(x,dict)]
        if stage=='CAPTAIN_DRAFT':st['captainSlot']=position_id
        st['lastDraftChoices']=[];st['lastDraftChoiceItems']=[]
        st['lastPositionId']=position_id;st['draftStage']='PLAYER_DRAFT'
        if len(st.get('pickedBySlot',{}))>=23:
            if not _draft_advance_to_manager(st,mode,'23rd player choice'):
                st['draftStage']='MANAGER_DRAFT'
    elif stage=='MANAGER_DRAFT':
        cached=st.get('lastDraftChoiceItems',[]) if isinstance(st.get('lastDraftChoiceItems'),list) else []
        chosen=dict(cached[choice]) if 0<=choice<len(cached) and isinstance(cached[choice],dict) else None
        if chosen:
            st['selectedManager']=_upgrade_draft_manager_item(chosen)
            chem,rating=_draft_compute_metrics(st);st['savedChemistry']=chem;st['savedRating']=rating
            st['draftStage']='READY_FOR_MATCH'
            st['lastDraftChoices']=[];st['lastDraftChoiceItems']=[];st['lastPositionId']=0;st['lastDraftChoiceKind']=''
            log.warning('DRAFT FINAL SQUAD mode=%s manager=%s contract=%s authoritative metrics chemistry=%d rating=%d',mode,st['selectedManager'].get('name'),st['selectedManager'].get('contract'),chem,rating)
        else:
            st['draftStage']='MANAGER_DRAFT'
            log.warning('DRAFT MANAGER CHOOSE rejected invalid choice=%s cached=%d',choice,len(cached))
    _draft_save(st, mode);return {}

@_draft_sp_serialized(0)
def _draft_complete(mode='SINGLE_PLAYER'):
    """Fill every remaining empty Draft slot (Complete my Squad).

    Session 20260915-104056 proved FIFA 18 reads this response body and redraws
    the squad in place: after one PUT /draft/autocomplete the client requested
    22 new player-head textures with no state GET and no hub visit. The
    /draft/choose body is ignored, so this is the only route whose reply the
    client applies on screen.  The autocomplete-one-slot experiment built on
    that was removed on 2026-09-16: filling one slot per press never offers the
    five-card choice, so it was not worth a second Draft behaviour.
    """
    _DRAFT_RUNTIME_ACTIVE_KEYS.add(_draft_mode_key(mode))
    is_wc = _draft_mode_name(mode).startswith('WORLD_CUP')
    st=_draft_get_state(mode);picked=st.get('pickedBySlot',{}) if isinstance(st.get('pickedBySlot'),dict) else {}
    used={int(x.get('assetId',0) or 0) for x in picked.values() if isinstance(x,dict)}
    pool=list(_all_player_defs());rng=random.SystemRandom();auto_assets=[]
    if is_wc:
        wc_icon_list = _world_cup_icons()
        wc_non_icons = [d for d in pool if int(d.get('nation', d.get('nationId', 0)) or 0) in WC_32_NATIONS and str(d.get('specialType', '')).upper() not in ('ICON', 'TOTS', 'TOTW', 'TOTY', 'OTW', 'HALLOWEEN')]
        pool = wc_non_icons + wc_icon_list

    def choose_candidate(candidates):
        if not candidates:return None
        candidates=sorted(candidates,key=lambda d:int(d.get('rating',0) or 0),reverse=True)[:240]
        weights=[max(1,(max(1,int(d.get('rating',0) or 0))-45)**3) for d in candidates]
        try:return rng.choices(candidates,weights=weights,k=1)[0]
        except Exception:return rng.choice(candidates)

    missing_slots=[idx for idx in range(23) if str(idx) not in picked]
    instance_ids=iter(_draft_allocate_item_ids(len(missing_slots))) if missing_slots else iter(())
    for idx in range(23):
        if str(idx) in picked:continue
        allowed=_draft_allowed_positions(st.get('formation'),idx)
        candidates=[d for d in pool if int(d.get('assetId',0) or 0) not in used and (not allowed or str(d.get('position','')).upper() in allowed)]
        if not candidates:candidates=[d for d in pool if int(d.get('assetId',0) or 0) not in used]
        d=choose_candidate(candidates)
        if not d:continue
        asset=int(d.get('assetId',0) or 0);used.add(asset);auto_assets.append(asset)
        item=_definition_item(d,pile=0,rare=None if _is_special_def(d) else int(d.get('rating',0) or 0)>=75,item_id=next(instance_ids))
        if is_wc:
            item = _apply_world_cup_player_schema(item, defn=d)
            item['skuMode'] = 'WC'
        item['contract']=99;item['contracts']=99;item['loans']=0;item['fitness']=99
        item['concept']=True;item['untradeable']=True;item['tradeable']=False;item['draftItem']=True
        picked[str(idx)]=item
    st['pickedBySlot']=picked;st['picks']=[int(x.get('resourceId',x.get('definitionId',x.get('assetId',0))) or 0) for x in picked.values() if isinstance(x,dict)]
    # Autocomplete replaces any outstanding five-card offer.  Keeping the old
    # offer here lets a delayed /draft/choose overwrite the newly filled slot
    # or place a card that is no longer visible in the client.
    st['lastDraftChoices']=[];st['lastDraftChoiceItems']=[];st['lastDraftChoiceKind']=''
    st.pop('slotOffers',None);st.pop('slotOffersFormation',None)
    if len(picked)>=23:
        if not _draft_advance_to_manager(st,mode,'autocomplete'):
            st['draftStage']='MANAGER_DRAFT' if not isinstance(st.get('selectedManager'),dict) else 'READY_FOR_MATCH';st['active']=True;st['completed']=False
    else:
        # Slots remain (the catalogue could not fill one): stay in the build
        # stage.  Handing back MANAGER_DRAFT with an incomplete squad is what
        # let a 6-player squad reach a match in session 20260915-082930.
        st['draftStage']='PLAYER_DRAFT';st['active']=True;st['completed']=False
        st['lastPositionId']=next((idx for idx in range(23) if not isinstance(picked.get(str(idx),picked.get(idx)),dict)),0)
        # The autocomplete response is rendered directly without another state
        # fetch. Keep its score panel consistent with the cards it carries.
        chem,rating=_draft_compute_metrics(st);st['savedChemistry']=chem;st['savedRating']=rating
    _draft_save(st, mode);sq=_draft_state_squad(st)
    log.warning('DRAFT COMPLETE SQUAD mode=%s filled=%d nextSlot=%s assets=%s',mode,len(auto_assets),st.get('lastPositionId'),auto_assets)
    return {'success':True,'mode':mode,'draftState':st,'squad':sq,'players':sq.get('players',[])}

def _draft_squad_payload(mode='SINGLE_PLAYER'):
    st=_draft_get_state(mode);sq=_draft_state_squad(st)
    return {'success':True,'mode':mode,'draftState':st,'squad':sq,'players':sq.get('players',[])}


def _match_squad_with_club_items(squad):
    sq=dict(squad or {})
    act=[dict(x) for x in _active_selected_club_items()]
    sq['actives']=[dict(x) for x in act]
    sq['club']=[dict(x) for x in act]
    return sq


def _draft_match_player_item(item, formation='f442'):
    """Project a Draft UI card into a playable native match item.

    Draft choice/squad screens deliberately use concept=True so the temporary
    cards are never mistaken for persistent club inventory.  That flag must not
    cross the final match boundary: the native FUT match loader treats concept
    players as non-playable squad members even when the surrounding squad says
    valid=True.  Keep the saved/UI Draft object untouched and sanitize only the
    transient /match response.
    """
    x=dict(item or {})
    if str(x.get('skuMode','')).upper() == 'WC':
        x=_apply_world_cup_player_schema(x)
    x['concept']=False
    x.pop('draftItem',None)
    x['itemState']='free'
    x['pile']=0
    x['untradeable']=True
    x['tradeable']=False
    x['owners']=max(1,int(x.get('owners',1) or 1))
    x['contract']=max(99,int(x.get('contract',x.get('contracts',99)) or 99))
    x['contracts']=max(99,int(x.get('contracts',x.get('contract',99)) or 99))
    x['loans']=0
    x['fitness']=max(1,int(x.get('fitness',99) or 99))
    x['morale']=int(x.get('morale',50) or 50)
    x['formation']=str(formation or x.get('formation') or 'f442')
    return x


def _extract_card_stat(item, stat_index, default=50):
    if not isinstance(item, dict):
        return int(default)
    arr = item.get('attributeArray')
    if isinstance(arr, list) and len(arr) > stat_index:
        try:
            return int(arr[stat_index])
        except Exception:
            pass
    face = item.get('face')
    if isinstance(face, list) and len(face) > stat_index:
        try:
            return int(face[stat_index])
        except Exception:
            pass
    attr_list = item.get('attributeList')
    if isinstance(attr_list, list):
        for entry in attr_list:
            if isinstance(entry, dict) and entry.get('index') == stat_index:
                try:
                    return int(entry.get('value', default))
                except Exception:
                    pass
    return int(item.get('rating', default) or default)


def _resolve_squad_kicktakers(starters, existing_kicktakers=None, captain_id=0):
    """Resolve 5 authentic kicktaker slots (0: Long FK, 1: Short FK, 2: Left Corner,
    3: Right Corner, 4: Penalty) for a starting XI.
    Preserves user assignments without deduplicating players across distinct roles,
    and falls back to intelligent outfield candidates (highest shooting/passing)
    rather than assigning goalkeepers or defenders to set pieces."""
    valid_items = [x for x in starters if isinstance(x, dict) and int(x.get('id', 0) or 0) > 0]
    if not valid_items:
        return []
    valid_ids = [int(x['id']) for x in valid_items]
    outfield = [x for x in valid_items if str(x.get('position', x.get('preferredPosition', ''))).upper() != 'GK']
    if not outfield:
        outfield = valid_items

    # Smart defaults for roles:
    # Index 4 (Penalty): best shooting
    penalty_default = max(outfield, key=lambda x: (_extract_card_stat(x, 1), int(x.get('rating', 0) or 0)))['id']
    # Index 1 (Short FK): best shooting + passing combined
    short_fk_default = max(outfield, key=lambda x: (_extract_card_stat(x, 1) + _extract_card_stat(x, 2), int(x.get('rating', 0) or 0)))['id']
    # Index 0 (Long FK): best passing
    long_fk_default = max(outfield, key=lambda x: (_extract_card_stat(x, 2), int(x.get('rating', 0) or 0)))['id']
    # Index 2 (Left Corner): best passing
    left_corner_default = max(outfield, key=lambda x: (_extract_card_stat(x, 2), int(x.get('rating', 0) or 0)))['id']
    # Index 3 (Right Corner): best passing
    right_corner_default = max(outfield, key=lambda x: (_extract_card_stat(x, 2), int(x.get('rating', 0) or 0)))['id']

    role_defaults = [long_fk_default, short_fk_default, left_corner_default, right_corner_default, penalty_default]

    # Map existing user assignments by slot index (0..4)
    assigned = {}
    if isinstance(existing_kicktakers, list):
        for row in existing_kicktakers:
            if not isinstance(row, dict):
                continue
            try:
                slot_idx = int(row.get('index', -1))
                slot_id = int(row.get('id', 0) or 0)
            except Exception:
                continue
            if 0 <= slot_idx < 5 and slot_id in valid_ids:
                assigned[slot_idx] = slot_id

    out = []
    for i in range(5):
        chosen_id = assigned.get(i, role_defaults[i] if i < len(role_defaults) else valid_ids[0])
        out.append({'index': i, 'id': chosen_id, 'dream': False})
    return out


def _draft_native_match_squad(st=None):
    """Build the compact playable squad contract consumed by match launch.

    The squad editor and the native match loader do not consume the same wire
    object.  The working neighbouring PC backend's match path supplies tactics,
    kick takers, non-zero chemistry/star rating and playable item instances.
    Keep Draft squad id 900001 transient and do not activate/overwrite My Squads.
    """
    st=st or _draft_get_state()
    ui=_draft_state_squad(st)
    formation=str(ui.get('formation') or 'f442')
    players=[]
    playable=[]
    for idx,row in enumerate((ui.get('players') or [])[:23]):
        src=dict(row) if isinstance(row,dict) else {'index':idx}
        out={'index':int(src.get('index',idx) or idx),'kitNumber':int(src.get('kitNumber',0) or 0)}
        item=src.get('itemData') if isinstance(src.get('itemData'),dict) else None
        if item:
            fixed=_draft_match_player_item(item,formation)
            out['itemData']=fixed
            playable.append((out['index'],fixed))
        players.append(out)
    while len(players)<23:
        players.append({'index':len(players),'kitNumber':0})

    starters=[item for idx,item in playable if 0<=idx<11]
    if not starters:
        starters=[item for _,item in playable[:11]]
    captain=int(ui.get('captain',0) or 0)
    if not captain and starters:
        captain=int(starters[0].get('id',0) or 0)
    kicktakers = _resolve_squad_kicktakers(starters, st.get('kicktakers') if isinstance(st, dict) else None, captain)

    manager=[]
    for i,row in enumerate(ui.get('manager',[]) if isinstance(ui.get('manager'),list) else []):
        if not isinstance(row,dict):continue
        r=dict(row)
        item=r.get('itemData') if isinstance(r.get('itemData'),dict) else None
        if item:
            fixed=dict(item);fixed['concept']=False;fixed['dream']=False;fixed.pop('draftItem',None)
            fixed['itemState']='free';fixed['untradeable']=True;fixed['tradeable']=False
            fixed['contract']=max(99,int(fixed.get('contract',fixed.get('contracts',99)) or 99))
            fixed['contracts']=max(99,int(fixed.get('contracts',fixed.get('contract',99)) or 99))
            fixed['loans']=0
            r['itemData']=fixed
        r.setdefault('index',i);manager.append(r)
    if not manager and isinstance(st.get('selectedManager'),dict):
        m_item=dict(st['selectedManager'])
        m_item['concept']=False;m_item['dream']=False;m_item.pop('draftItem',None)
        m_item['itemState']='free';m_item['untradeable']=True;m_item['tradeable']=False
        m_item['contract']=99;m_item['contracts']=99;m_item['loans']=0
        manager.append({'index':0,'id':int(m_item.get('id',0) or 0),'itemData':m_item})
    if not manager and _DRAFT_MANAGERS:
        default_mgr=_draft_manager_item(*_DRAFT_MANAGERS[0])
        default_mgr['concept']=False;default_mgr['dream']=False;default_mgr.pop('draftItem',None)
        default_mgr['itemState']='free';default_mgr['contract']=99;default_mgr['contracts']=99;default_mgr['loans']=0
        manager.append({'index':0,'id':int(default_mgr['id']),'itemData':default_mgr})

    # starRating is the five-star squad-strength scalar in this wire contract,
    # not the OVR average.  rating is kept as the useful XI OVR alias for later
    # FIFA 18 code paths while starRating remains in the retail 1..5 range.
    xi_ratings=[int(x.get('rating',0) or 0) for x in starters if int(x.get('rating',0) or 0)>0]
    rating=int(st.get('savedRating',0) or 0) or (round(sum(xi_ratings)/len(xi_ratings)) if xi_ratings else 75)
    act=[dict(x) for x in _active_selected_club_items()]
    return {
        'id':_DRAFT_SQUAD_ID,'squadId':_DRAFT_SQUAD_ID,'personaId':FAKE_PERSONA,
        'squadName':'Draft Squad','name':'Draft Squad','formation':formation,
        'captain':captain,'chemistry':int(st.get('savedChemistry',100) or 100),'changed':0,'starRating':5,'rating':rating,
        'custom':'[0,0,0,0,0,0,0,0,0,0,0]',
        'players':players,'actives':[dict(x) for x in act],'club':[dict(x) for x in act],
        'manager':manager,'kicktakers':kicktakers,
        'active':True,'valid':True,'squadType':'DRAFT_SQUAD','newSquad':0,'newsquad':0,
    }


def _draft_match_start(doc=None):
    st=_draft_get_state();doc=doc if isinstance(doc,dict) else {}
    if _draft_stage(st)!='READY_FOR_MATCH':
        return None
    sq=_draft_native_match_squad(st)
    now=int(time.time())
    st['matchInProgress']=True;st['lastMatchStart']=now
    try:st['lastMatchCustomData1']=int(doc.get('customData1',0) or 0)
    except Exception:st['lastMatchCustomData1']=0
    _draft_save(st)
    log.warning('DRAFT MATCH NATIVE WIRE playable=%d starters=%d chemistry=%s stars=%s rating=%s kicktakers=%d conceptPlayers=%d custom=%s',
                sum(1 for x in sq.get('players',[]) if isinstance(x.get('itemData'),dict)),
                sum(1 for x in sq.get('players',[])[:11] if isinstance(x.get('itemData'),dict)),
                sq.get('chemistry'),sq.get('starRating'),sq.get('rating'),len(sq.get('kicktakers',[]) or []),
                sum(1 for x in sq.get('players',[]) if isinstance(x.get('itemData'),dict) and bool(x['itemData'].get('concept'))),
                sq.get('custom'))
    return {'squad':sq,'opponentTeamId':DRAFT_OPPONENT_TEAM_ID,'startDateTime':now}


def _draft_match_ready(doc=None):
    """Acknowledge the arena -> kickoff readiness PUT without restarting Draft state.

    FIFA 15 PC sends PUT /match after the skill-game arena with its active XI.
    FIFA 18 has not reached this request yet in our trace, but keeping POST and
    PUT semantically separate prevents a future successful handoff from resetting
    lastMatchStart or re-entering the Draft-start path.
    """
    st=_draft_get_state();doc=doc if isinstance(doc,dict) else {}
    sq=_draft_native_match_squad(st)
    started=int(st.get('lastMatchStart',0) or 0) or int(time.time())
    st['matchInProgress']=True;st['lastMatchReady']=int(time.time());_draft_save(st)
    ids=[]
    for row in doc.get('items',[]) if isinstance(doc.get('items'),list) else []:
        if isinstance(row,dict):
            try:ids.append(int(row.get('id',0) or 0))
            except Exception:pass
    log.warning('DRAFT MATCH READY PUT starters=%d itemIds=%s start=%s body=%r',
                sum(1 for x in sq.get('players',[])[:11] if isinstance(x.get('itemData'),dict)),ids[:23],started,str(doc)[:2048])
    return {'squad':sq,'opponentTeamId':DRAFT_OPPONENT_TEAM_ID,'startDateTime':started}


def _native_regular_match_squad(squad=None):
    """Normalize a persistent FUT squad into the native CreateMatch contract.

    The 2026-08-29 BETA5 trace exposed a concrete mismatch that the earlier
    Draft-parity work did not catch: the saved regular squad carried
    ``starRating=95`` (the OVR value), while the proven Draft match contract and
    generated Squad Battles opponents use the native 1..5 star scalar.  FIFA
    accepted the HTTP response but never emitted MatchReady.  Normalize only the
    transient match copy so the user's persistent squad/UI data is not rewritten.
    """
    raw=_match_squad_with_club_items(squad if isinstance(squad,dict) else _active_squad())
    sq=dict(raw or {})
    formation=str(sq.get('formation') or 'f442')
    players=[];playable=[];formation_fixes=0;concept_fixes=0
    for idx,row in enumerate((sq.get('players') or [])[:23]):
        src=dict(row) if isinstance(row,dict) else {'index':idx}
        out=dict(src);out['index']=int(src.get('index',idx) or idx);out.setdefault('kitNumber',0)
        item=src.get('itemData') if isinstance(src.get('itemData'),dict) else None
        if item:
            fixed=dict(item)
            if bool(fixed.get('concept')): concept_fixes+=1
            fixed['concept']=False
            fixed['itemState']='free'
            fixed['contract']=max(99,int(fixed.get('contract',fixed.get('contracts',99)) or 99))
            fixed['contracts']=max(99,int(fixed.get('contracts',fixed.get('contract',99)) or 99))
            fixed['loans']=0
            fixed['fitness']=max(1,int(fixed.get('fitness',99) or 99))
            if str(fixed.get('formation') or '')!=formation: formation_fixes+=1
            fixed['formation']=formation
            fixed['owners']=max(1,int(fixed.get('owners',1) or 1))
            out['itemData']=fixed;playable.append((out['index'],fixed))
        players.append(out)
    while len(players)<23:players.append({'index':len(players),'kitNumber':0,'loyaltyBonus':0})
    sq['players']=players[:23]

    starters=[item for idx,item in playable if 0<=idx<11]
    if not starters:starters=[item for _,item in playable[:11]]
    xi_ratings=[max(0,int(x.get('rating',0) or 0)) for x in starters if int(x.get('rating',0) or 0)>0]
    rating=max(1,min(99,int(sq.get('rating',0) or 0))) if int(sq.get('rating',0) or 0)>0 else (round(sum(xi_ratings)/len(xi_ratings)) if xi_ratings else 75)
    raw_star=int(sq.get('starRating',0) or 0)
    native_star=raw_star if 1<=raw_star<=5 else max(1,min(5,round((rating-55)/7)))
    sq['rating']=rating;sq['starRating']=native_star;sq['chemistry']=max(0,min(100,int(sq.get('chemistry',0) or 0)))
    sq['changed']=0;sq['active']=True;sq['valid']=True;sq['newSquad']=0;sq['newsquad']=0
    sq.setdefault('squadType','REGULAR_SQUAD')
    if sq.get('custom') is None:sq['custom']='[0,0,0,0,0,0,0,0,0,0,0]'

    valid_ids=[int(x.get('id',0) or 0) for x in starters if int(x.get('id',0) or 0)>0]
    captain=int(sq.get('captain',0) or 0)
    if captain not in valid_ids and valid_ids:captain=valid_ids[0]
    sq['captain']=captain
    sq['kicktakers']=_resolve_squad_kicktakers(starters, sq.get('kicktakers'), captain)

    manager=[]
    for i,row in enumerate(sq.get('manager',[]) if isinstance(sq.get('manager'),list) else []):
        if not isinstance(row,dict):continue
        r=dict(row);item=r.get('itemData') if isinstance(r.get('itemData'),dict) else None
        if item:
            fixed=dict(item);fixed['concept']=False;fixed['itemState']='free'
            fixed['contract']=max(99,int(fixed.get('contract',fixed.get('contracts',99)) or 99))
            fixed['contracts']=max(99,int(fixed.get('contracts',fixed.get('contract',99)) or 99))
            fixed['loans']=0
            r['itemData']=fixed;r.setdefault('index',i);manager.append(r)
        elif int(r.get('id',0) or 0)>0:
            r.setdefault('index',i);manager.append(r)
    if not manager:
        active_mgrs=[x for x in _db_items() if str(x.get('itemType','')).lower()=='manager']
        if active_mgrs:
            mgr_item=dict(active_mgrs[0])
            mgr_item['concept']=False;mgr_item['itemState']='free'
            mgr_item['contract']=99;mgr_item['contracts']=99;mgr_item['loans']=0
            manager.append({'index':0,'id':int(mgr_item.get('id',0) or 0),'itemData':mgr_item})
    sq['manager']=manager
    log.warning('NATIVE REGULAR MATCH SQUAD rawStar=%s nativeStar=%s rating=%s chemistry=%s formation=%s playable=%d starters=%d formationFixes=%d conceptFixes=%d captain=%s kicktakers=%d manager=%d',
                raw_star,native_star,rating,sq['chemistry'],formation,len(playable),len(starters),formation_fixes,concept_fixes,captain,len(sq['kicktakers']),len(manager))
    return sq


def _normal_match_start(doc=None):
    return {'squad':_native_regular_match_squad(),'opponentTeamId':DRAFT_OPPONENT_TEAM_ID,'startDateTime':int(time.time())}


def _normalise_match_end_reason(doc):
    doc=doc if isinstance(doc,dict) else {}
    reason=str(doc.get('endReason',doc.get('result','LOSS')) or 'LOSS').strip().upper()
    if reason=='FORFEIT':reason='QUIT'
    if reason not in ('WIN','DRAW','LOSS','DNF','QUIT','NO_CONTEST'):reason='NO_CONTEST'
    return reason


def _draft_match_end(doc=None):
    doc=doc if isinstance(doc,dict) else {}
    raw_reason=_normalise_match_end_reason(doc)
    st=_draft_get_state()
    # Aurora17 treats every played non-win (including QUIT/FORFEIT/DNF) as a
    # Draft LOSS on the retail wire. NO_CONTEST remains a teardown and does not
    # consume a round.
    response_reason=raw_reason
    if raw_reason not in ('WIN','NO_CONTEST'):
        response_reason='LOSS'

    started=int(st.get('lastMatchStart',0) or 0)
    already_settled=bool(started and int(st.get('lastSettledMatchStart',0) or 0)==started)
    if raw_reason!='NO_CONTEST' and not already_settled:
        _draft_match_result({'won':raw_reason=='WIN','result':raw_reason})
        st=_draft_get_state()
        st['lastSettledMatchStart']=started
        _record_settle('WIN' if raw_reason=='WIN' else 'LOSS')
    else:
        st=_draft_get_state()

    st['matchInProgress']=False
    st['lastMatchEndReason']=raw_reason
    eliminated=bool(st.get('completed')) or _draft_stage(st)=='READY_FOR_REWARDS'
    pending=0
    pack_id=0
    if eliminated:
        bundle=_draft_reward_bundle(st.get('wins',0),st)
        st['pendingRewardBundle']=bundle
        pending=int(bundle.get('coins',0) or 0)
        packs=bundle.get('packs',[]) if isinstance(bundle.get('packs'),list) else []
        pack_id=int(packs[0] if packs else 0)
        st['pendingCoinReward']=pending
        st['pendingRewardPackId']=pack_id
        st['awardConsumed']=False
    st=_draft_save(st)

    credits=_credits()
    try:seconds=max(0,int(doc.get('secondsPlayed',0) or 0))
    except Exception:seconds=0
    try:difficulty=max(0,int(doc.get('matchDifficulty',st.get('difficulty',0)) or 0))
    except Exception:difficulty=0
    out={
        'endReason':response_reason,'secondsPlayed':seconds,'matchDifficulty':difficulty,'opponentTeamId':DRAFT_OPPONENT_TEAM_ID,
        'items':doc.get('items',[]) if isinstance(doc.get('items'),list) else [],
        'matchData':str(doc.get('matchData','') or ''),'completionAward':pending,'skillAward':0,
        'rewardCoins':pending,'totalCoins':pending,'credits':credits,'coins':credits,
        'sessionCoinsBankBalance':credits,
        'currencies':[{'name':'coins','funds':credits,'finalFunds':credits},{'name':'points','funds':0,'finalFunds':0}],
        'record':{'won':_record_triplet()[0],'draw':_record_triplet()[1],'loss':_record_triplet()[2]},
        'gamesWon':_record_triplet()[0],'gamesDraw':_record_triplet()[1],'gamesLost':_record_triplet()[2],'gamesPlayed':sum(_record_triplet()),
        'unopenedPacks':_unopened_packs_payload(),'dnfModifier':1.0,
        'won':1 if raw_reason=='WIN' else 0,'draw':0,
        'loss':1 if raw_reason not in ('WIN','NO_CONTEST') else 0,
        'matchCoinPartials':None,'matchCoinMultipliers':[{'type':'DIFFICULTY','value':1.0}],
        'boostConis':0,'draftWins':int(st.get('wins',0) or 0),
        'draftEliminated':eliminated,
    }
    if isinstance(doc.get('myMatchStats'),dict):out['myMatchStats']=doc['myMatchStats']
    if isinstance(doc.get('opponentMatchStats'),dict):out['opponentMatchStats']=doc['opponentMatchStats']
    if 'myRating' in doc:out['myRating']=doc.get('myRating')
    if 'opponentRating' in doc:out['opponentRating']=doc.get('opponentRating')
    PENDING_USERSESSION_REFRESH.set()
    log.warning('DRAFT SETTLEMENT rawReason=%s wireReason=%s wins=%s eliminated=%s completionAward=%s rewardPackId=%s alreadySettled=%s UserSessionsRefresh=queued',
                raw_reason,response_reason,st.get('wins',0),eliminated,pending,pack_id,already_settled)
    return out


def _draft_match_result(doc=None,mode='SINGLE_PLAYER'):
    st=_draft_get_state(mode);doc=doc if isinstance(doc,dict) else {}
    if st.get('draftState')=='NOT_STARTED':st=_draft_start(mode)
    won=bool(doc.get('won',doc.get('win',doc.get('result','WIN') in ('WIN','WON','win','won',1,True))))
    st['matchesPlayed']=int(st.get('matchesPlayed',0) or 0)+1
    if won:
        st['wins']=min(4,int(st.get('wins',0) or 0)+1)
        if int(st.get('wins',0) or 0)>=4:
            st['draftState']='COMPLETE';st['state']='COMPLETE';st['status']='COMPLETE'
            st['draftStage']='READY_FOR_REWARDS';st['active']=False;st['completed']=True
        else:
            st['draftState']='IN_PROGRESS';st['state']='IN_PROGRESS';st['status']='IN_PROGRESS'
            st['draftStage']='READY_FOR_MATCH';st['active']=True;st['completed']=False
    else:
        st['losses']=int(st.get('losses',0) or 0)+1
        st['draftState']='COMPLETE';st['state']='COMPLETE';st['status']='COMPLETE'
        st['draftStage']='READY_FOR_REWARDS';st['active']=False;st['completed']=True
    st['round']=min(4,int(st.get('wins',0) or 0)+1)
    st=_draft_save(st, mode)
    return {'success':True,'mode':mode,'won':won,'wins':st['wins'],'losses':st['losses'],'matchesPlayed':st['matchesPlayed'],'round':st['round'],'draftState':st}



def _draft_native_awards(st=None):
    st=st if isinstance(st,dict) else _draft_get_state()
    if bool(st.get('prizeClaimed')) or bool(st.get('awardConsumed')):return []
    bundle=_draft_reward_bundle(int(st.get('wins',0) or 0),st)
    rows=[]
    coins=max(0,int(bundle.get('coins',0) or 0))
    if coins:rows.append({'halId':0,'type':0,'value':coins})
    packs=bundle.get('packs',[]) if isinstance(bundle.get('packs'),list) else []
    for pid in packs:
        try:pid=int(pid or 0)
        except Exception:pid=0
        if pid>0:rows.append({'halId':0,'type':1,'value':pid})
    return rows


def _draft_claim_award(mode='SINGLE_PLAYER'):
    m_name = _draft_mode_name(mode)
    key = _draft_mode_key(m_name)
    st=_draft_get_state(m_name);wins=int(st.get('wins',0) or 0)
    if not (bool(st.get('completed')) or _draft_stage(st, m_name)=='READY_FOR_REWARDS'):return []
    if bool(st.get('awardConsumed')) or bool(st.get('prizeClaimed')):return []
    bundle=_draft_reward_bundle(wins,st)
    coins=int(bundle.get('coins',0) or 0)
    pack_ids=[int(x) for x in (bundle.get('packs',[]) if isinstance(bundle.get('packs'),list) else []) if int(x)>0]
    if coins>0:_meta_set('credits',_credits()+coins)
    granted_packs=[]
    for pid in pack_ids:
        _grant_owned_pack(pid,1);granted_packs.append(pid)
    # Capture the FIFA Draft award descriptors before consuming/resetting state.
    # Aurora proves type=0 for coins; FIFA18's DraftAwardType vocabulary supplies
    # the pack branch used here as type=1. Pack contents are also persisted locally.
    awards=_draft_native_awards(st)
    completed_count=int(st.get('draftsCompleted',0) or 0)+1
    fresh={
        'mode':m_name,'draftMode':m_name,'gameMode':m_name,
        'draftId':18002 if m_name.endswith('ONLINE') else 18001,'id':18002 if m_name.endswith('ONLINE') else 18001,'draftState':'NOT_STARTED','state':'NOT_STARTED','status':'NOT_STARTED',
        'draftStage':'INVALID','active':False,'completed':False,'draftToken':0,'draftTokens':0,
        'entryFee':15000,'entryCost':15000,'draftEntry':15000,'wins':0,'losses':0,'matchesPlayed':0,
        'difficulty':0,'formation':'','round':1,'picks':[],'pickedBySlot':{},'selectedManager':None,
        'prizeClaimed':True,'pendingCoinReward':0,'pendingRewardPackId':0,'awardConsumed':True,
        'draftsCompleted':completed_count,'lastRewardCoins':coins,'lastRewardPackId':(pack_ids[0] if pack_ids else 0),
        'lastRewardBundle':bundle,'lastRewardItemCount':0,'lastRewardOwnedPacks':granted_packs,
    }
    _meta_set(key,json.dumps(fresh,separators=(',',':')))
    PENDING_USERSESSION_REFRESH.set()
    log.warning('DRAFT AWARD CLAIM FIFA18 mode=%s wins=%s label=%s packs=%s coins=+%s rewardPacks=%d draftsCompleted=%s credits=%s -> INVALID',m_name,wins,bundle.get('label'),pack_ids,coins,len(granted_packs),completed_count,_credits())
    return awards

def _draft_prize_payload(claim=False):
    st=_draft_get_state();wins=int(st.get('wins',0) or 0)
    bundle=_draft_reward_bundle(wins,st)
    coins=int(bundle.get('coins',0) or 0)
    pack_ids=[int(x) for x in (bundle.get('packs',[]) if isinstance(bundle.get('packs'),list) else []) if int(x)>0]
    claimed=bool(st.get('prizeClaimed') or st.get('awardConsumed'))
    pack_rows=[]
    for i,pid in enumerate(pack_ids):
        p=_pack_by_id(pid)
        if p:
            d=_draft_pack_award_detail(pid,i)
            d.update({'quantity':1,'rewardType':'PACK','rewardValue':pid})
            pack_rows.append(d)
    if claim:
        native=_draft_claim_award()
        return {'success':True,'mode':'SINGLE_PLAYER','awards':pack_rows,'items':pack_rows,'awardedPrizes':native,
                'awardItemData':pack_rows,'awardMappings':pack_rows,'rewards':pack_rows,'groupAwards':pack_rows,'groupRewards':pack_rows,
                'awardSetId':min(4,wins),'unopenedPacks':_unopened_packs_payload(),
                'rewardPacks':pack_rows,'rewardSummary':bundle.get('label',''),'coins':coins,'credits':_credits(),'claimed':True,'shouldClaimPrize':False,'draftState':_draft_get_state()}
    native=_draft_native_awards(st) if not claimed else []
    return {'success':True,'mode':'SINGLE_PLAYER','awards':pack_rows,'items':pack_rows,'awardedPrizes':native,
            'awardItemData':pack_rows,'awardMappings':pack_rows,'rewards':pack_rows,'groupAwards':pack_rows,'groupRewards':pack_rows,
            'awardSetId':min(4,wins),'unopenedPacks':_unopened_packs_payload(),
            'rewardPacks':pack_rows,'rewardSummary':bundle.get('label',''),'coins':coins,'credits':_credits(),'claimed':claimed,
            'shouldClaimPrize':bool(st.get('completed') and not claimed),'draftState':st}



# ---- FIFA 18 Squad Battles -------------------------------------------------
# FIFA 18 shipped the native Squad Battles screen/assets.  This local backend
# reconstructs a persistent seven-day competition, four generated opponents per
# refresh, 30 point-earning matches, difficulty-scaled Battle Points and a
# deterministic local AI Top 100.
_SB_DIFFICULTIES=(
    ('BEGINNER',0.5,0.20,10),('AMATEUR',0.6,0.25,20),('SEMI_PRO',0.7,0.40,40),
    ('PROFESSIONAL',1.0,0.50,60),('WORLD_CLASS',1.6,0.60,100),
    ('LEGENDARY',2.1,0.70,140),('ULTIMATE',2.6,0.70,160),
)
_SB_STATE_KEY='squadBattleState'
_SB_SCHEMA=9
_SB_WEEK_SECONDS=7*24*60*60
_SB_POINT_MATCH_LIMIT=30
_SB_AI_COUNT=250
_SB_AI_NAMES=(
    'Aether FC','Northbridge','Kings Eleven','Redwood United','Metro Stars','Vortex FC','Blue Lions','Royal City','Atlas XI','Phoenix Club',
    'Iron Wolves','Golden Boys','Night Owls','Rising XI','Union 18','Velocity','Titan FC','Orbit United','Cobalt City','Crimson XI',
    'Highland FC','Riverside','Eastgate','West End XI','Capital Club','Galaxy FC','Rapid City','Victory XI','Foundry FC','Crown United',
    'Borough Boys','Parkside','Lakeside','Harbour FC','Summit XI','Dynamo Local','Oakwood','Kingsport','Valley FC','Neon United',
    'Falcon XI','Storm City','Comet FC','Pioneer United','Mercury XI','Nova FC','Aurora City','Eclipse XI','Horizon FC','Zenith United',
)

def _sb_competition_bootstrap(st, now=None):
    now=int(now or time.time());changed=False
    try:start=int(st.get('competitionStart',0) or 0);end=int(st.get('competitionEnd',0) or 0)
    except Exception:start=end=0
    if start<=0 or end<=start:
        start=now;end=start+_SB_WEEK_SECONDS
        st['competitionStart']=start;st['competitionEnd']=end
        st['competitionId']=max(1,int(st.get('competitionId',0) or 0))
        changed=True
    if now>=end:
        previous={
            'competitionId':int(st.get('competitionId',1) or 1),
            'startDateTime':start,'endDateTime':end,
            'battlePoints':int(st.get('battlePoints',0) or 0),
            'matchesPlayed':int(st.get('matchesPlayed',0) or 0),
            'wins':int(st.get('wins',0) or 0),'draws':int(st.get('draws',0) or 0),'losses':int(st.get('losses',0) or 0),
        }
        st['previousCompetition']=previous
        st['competitionId']=int(st.get('competitionId',1) or 1)+1
        st['competitionStart']=now;st['competitionEnd']=now+_SB_WEEK_SECONDS
        st['refreshId']=0;st['refreshesUsed']=0;st['generatedAt']=0;st['opponents']=[]
        st['selectedOpponentId']=0;st['selectedDifficulty']=3;st['matchInProgress']=False
        st['lastMatchStart']=0;st['lastSettledMatchStart']=0
        st['battlePoints']=0;st['matchesPlayed']=0;st['pointMatchesPlayed']=0
        st['wins']=0;st['draws']=0;st['losses']=0
        changed=True
    return st,changed

def _sb_load():
    raw=_meta_get(_SB_STATE_KEY,'')
    try:st=json.loads(raw) if raw else {}
    except Exception:st={}
    if not isinstance(st,dict):st={}
    changed=False
    old_schema=int(st.get('schema',0) or 0)
    schema_changed=old_schema!=_SB_SCHEMA
    if schema_changed:changed=True
    st['schema']=_SB_SCHEMA
    defaults={
        'competitionId':1,'competitionStart':0,'competitionEnd':0,'refreshId':0,'refreshesUsed':0,'generatedAt':0,
        'opponents':[],'selectedOpponentId':0,'selectedDifficulty':3,'matchInProgress':False,
        'lastMatchStart':0,'lastSettledMatchStart':0,'lastMatchReadyItemIds':[],
        'lastMatchOpponentTeamId':0,'lastMatchEndReason':'','lastMatchWireReason':'',
        'lastMatchBattlePoints':0,'lastMatchCoins':0,'lastOpponentId':0,'lastDifficulty':3,
        'battlePoints':0,'matchesPlayed':0,'pointMatchesPlayed':0,
        'wins':0,'draws':0,'losses':0,
    }
    for k,v in defaults.items():
        if k not in st:st[k]=v;changed=True
    # BETA3 schema 5 keeps the BETA2 native opponent/view-squad repair and also
    # regenerates untouched test sets with a deterministic, playable club identity
    # (real kit/badge team ids) for the final match handoff. Never erase genuine
    # played history or Battle Points.
    if schema_changed:
        prior_opps=st.get('opponents',[]) if isinstance(st.get('opponents'),list) else []
        prior_played=max(0,int(st.get('matchesPlayed',0) or 0))
        any_prior_played=any(bool(x.get('played')) for x in prior_opps if isinstance(x,dict))
        if prior_played<=0 and not any_prior_played:
            st['opponents']=[];st['refreshId']=0;st['refreshesUsed']=0;st['generatedAt']=0
            st['selectedOpponentId']=0;st['matchInProgress']=False;st['lastMatchStart']=0
            st['lastSettledMatchStart']=0;st['pointMatchesPlayed']=0;changed=True
        # BETA8 could reach gameplay but froze after a QUIT because it returned
        # QUIT plus SqBt-specific nested objects through FutDestroyMatch. Keep the
        # already-consumed fixture/history, but normalize that persisted result to
        # the same LOSS/0-3 representation BETA9 now uses on the retail wire.
        for migrated in prior_opps:
            if not isinstance(migrated,dict) or not bool(migrated.get('played')):continue
            old_result=str(migrated.get('result','') or '').upper()
            if old_result in ('QUIT','DNF','FORFEIT'):
                migrated['rawEndReason']=old_result
                migrated['result']='LOSS';migrated['battlePoints']=0;migrated['coins']=0
                migrated['userScore']=0;migrated['oppScore']=max(3,int(migrated.get('oppScore',0) or 0))
                changed=True
        # BETA10 replaces the old random-slot opponents with chemistry-accurate
        # 4-4-2 XIs. Preserve every played/result/economy field while rebuilding
        # the visual squad so upgrades do not erase Squad Battles progress.
        if old_schema<8 and prior_opps:
            try:
                refresh=max(1,int(st.get('refreshId',1) or 1));seed=int(st.get('seed',refresh) or refresh)
                rebuilt=[_sb_make_opponent(i,refresh,random.Random(seed ^ (0x5B710000 + i*7919))) for i in range(4)]
                prior_by_id={int(x.get('id',0) or 0):x for x in prior_opps if isinstance(x,dict)}
                preserve=('played','available','result','rawEndReason','battlePoints','coins','userScore','oppScore','playedAt','difficulty','difficultyName')
                for fresh in rebuilt:
                    old=prior_by_id.get(int(fresh.get('id',0) or 0))
                    if not isinstance(old,dict):continue
                    for key in preserve:
                        if key in old:fresh[key]=old[key]
                    if bool(fresh.get('played')):fresh['available']=False
                st['opponents']=rebuilt;changed=True
                log.warning('SQUAD BATTLES v1.0 migration rebuilt opponent lineups while preserving played history=%s',
                            [(x.get('id'),x.get('played'),x.get('result'),x.get('rating'),x.get('chemistry')) for x in rebuilt])
            except Exception as e:
                log.exception('SQUAD BATTLES v1.0 opponent migration failed: %s',e)
    # Public beta migration: keep genuine played history, but never let a stale
    # internal-test counter exhaust a fresh, completely unplayed opponent set.
    played_total=max(0,int(st.get('matchesPlayed',0) or 0))
    point_total=max(0,min(_SB_POINT_MATCH_LIMIT,int(st.get('pointMatchesPlayed',0) or 0)))
    opps=st.get('opponents',[]) if isinstance(st.get('opponents'),list) else []
    any_played=any(bool(x.get('played')) for x in opps if isinstance(x,dict))
    if played_total<=0 and not any_played and point_total!=0:
        point_total=0;changed=True
    if int(st.get('pointMatchesPlayed',0) or 0)!=point_total:
        st['pointMatchesPlayed']=point_total;changed=True
    st,rolled=_sb_competition_bootstrap(st);changed=changed or rolled
    if changed:_meta_set(_SB_STATE_KEY,json.dumps(st,separators=(',',':')))
    return st

def _sb_save(st):
    st=dict(st or {});st['schema']=_SB_SCHEMA
    st,_=_sb_competition_bootstrap(st)
    _meta_set(_SB_STATE_KEY,json.dumps(st,separators=(',',':')))
    return st

def _sb_difficulty(value):
    if isinstance(value,str):
        t=value.strip().upper().replace('-','_').replace(' ','_')
        aliases={'SEMIPRO':'SEMI_PRO','WORLDCLASS':'WORLD_CLASS'};t=aliases.get(t,t)
        for i,(name,_,__,___) in enumerate(_SB_DIFFICULTIES):
            if t==name:return i
        try:value=int(value)
        except Exception:value=3
    try:i=int(value)
    except Exception:i=3
    return max(0,min(len(_SB_DIFFICULTIES)-1,i))

def _sb_player_pool(lo,hi):
    rows=[]
    for d in _load_player_defs(False):
        try:r=int(d.get('rating',0) or 0);aid=int(d.get('assetId',0) or 0)
        except Exception:continue
        if aid>0 and int(lo)<=r<=int(hi):rows.append(d)
    return rows


# FUT 18 4-4-2 slot order used by the standard squad wire.  BETA9 generated
# players first and then assigned them to these slots, so the UI could show a
# goalkeeper at ST or a striker at CB while still claiming 90+ chemistry.
# BETA10 builds the XI *for the slot* and derives team chemistry from the same
# league/nation/club links the client renders.
_SB_F442_STARTER_POSITIONS=('GK','RB','CB','CB','LB','RM','CM','CM','LM','ST','ST')
_SB_F442_LINKS=(
    (10,9),(10,8),(10,7),(9,6),(9,5),
    (8,7),(8,4),(7,6),(7,4),(7,3),
    (6,5),(6,2),(6,1),(5,1),(4,3),
    (3,2),(2,1),(3,0),(2,0),
)
# The themed slots are deliberately connected in FIFA's actual f442 wire order.
_SB_THEME_SLOT_ORDER=(2,3,0,6,7,1,4,5,8,9,10)
_SB_THEME_COUNTS=(0,6,9,11)
_SB_THEME_LEAGUES=(0,13,19,53)  # mixed, Premier League, Bundesliga, LaLiga


def _sb_link_strength(a,b):
    a=a if isinstance(a,dict) else {};b=b if isinstance(b,dict) else {}
    try:aclub=int(a.get('teamid',a.get('teamId',0)) or 0);bclub=int(b.get('teamid',b.get('teamId',0)) or 0)
    except Exception:aclub=bclub=0
    try:aleague=int(a.get('leagueId',0) or 0);bleague=int(b.get('leagueId',0) or 0)
    except Exception:aleague=bleague=0
    try:anation=int(a.get('nation',0) or 0);bnation=int(b.get('nation',0) or 0)
    except Exception:anation=bnation=0
    shared=(1 if aclub>0 and aclub==bclub else 0)+(1 if aleague>0 and aleague==bleague else 0)+(1 if anation>0 and anation==bnation else 0)
    return 2 if shared>=2 else 1 if shared==1 else 0


def _sb_estimated_team_chemistry(starters):
    """Derive the advertised 0..100 chemistry from the XI actually rendered.

    FUT link colours are based on club/league/nation and positional fit.  One
    shared identity is enough for a normal link; two or more is a strong link.
    Normalize each player's available links and exact 4-4-2 position into the
    squad-wide scalar.  The important invariant is that chemistry can no longer
    disagree with an obviously red/out-of-position lineup.
    """
    xi=[x if isinstance(x,dict) else {} for x in list(starters or [])[:11]]
    while len(xi)<11:xi.append({})
    degrees=[0]*11;points=[0]*11
    for left,right in _SB_F442_LINKS:
        degrees[left]+=1;degrees[right]+=1
        strength=_sb_link_strength(xi[left],xi[right])
        points[left]+=strength;points[right]+=strength
    total=0.0
    for slot,item in enumerate(xi):
        exact=1.0 if str(item.get('position',item.get('preferredPosition','')) or '').upper()==_SB_F442_STARTER_POSITIONS[slot] else 0.0
        link_ratio=min(1.0,float(points[slot])/float(max(1,degrees[slot])))
        # Exact position matters, but links dominate team chemistry just as the
        # on-screen red/orange/green network suggests.
        total+=10.0*((0.30*exact)+(0.70*link_ratio))
    return max(0,min(100,int(round((total/110.0)*100.0))))


def _sb_position_candidates(position,lo,hi,used,league_id=0):
    pos=str(position or '').upper();league_id=int(league_id or 0)
    out=[]
    # A small rating window below the nominal opponent band is intentional: a
    # league's best natural RB/LM may be a few OVR below its stars.  Exact slot
    # position is more important than putting an 88 ST at RB to fake the OVR.
    low=max(1,int(lo)-8);high=min(99,int(hi)+3)
    for d in _load_player_defs(False):
        if not isinstance(d,dict):continue
        try:aid=int(d.get('assetId',0) or 0);rating=int(d.get('rating',0) or 0);league=int(d.get('leagueId',0) or 0)
        except Exception:continue
        if aid<=0 or aid in used or rating<low or rating>high:continue
        if str(d.get('position',d.get('preferredPosition','')) or '').upper()!=pos:continue
        if league_id>0 and league!=league_id:continue
        out.append(d)
    return out


def _sb_pick_slot_player(rng,index,position,lo,hi,used,theme_league=0,themed=False):
    preferred=int(theme_league or 0) if themed else 0
    candidates=_sb_position_candidates(position,lo,hi,used,preferred)
    if not themed and theme_league:
        # Non-themed slots should genuinely break some links rather than
        # accidentally selecting the same league and inflating chemistry.
        away=[d for d in candidates if int(d.get('leagueId',0) or 0)!=int(theme_league)]
        if away:candidates=away
    if not candidates:candidates=_sb_position_candidates(position,lo,hi,used,0)
    if not candidates:
        candidates=[d for d in _load_player_defs(False)
                    if isinstance(d,dict) and int(d.get('assetId',0) or 0)>0
                    and int(d.get('assetId',0) or 0) not in used
                    and str(d.get('position',d.get('preferredPosition','')) or '').upper()==str(position).upper()]
    if not candidates:return None
    midpoint=(int(lo)+int(hi))/2.0
    if int(index)==3:
        # The best squad should look like one: take natural-position elite cards
        # from one league, allowing only slight rotation among the strongest.
        candidates.sort(key=lambda d:int(d.get('rating',0) or 0),reverse=True);window=1
    elif int(index)==2:
        candidates.sort(key=lambda d:(abs(int(d.get('rating',0) or 0)-83),-int(d.get('rating',0) or 0)));window=min(8,len(candidates))
    else:
        candidates.sort(key=lambda d:(abs(int(d.get('rating',0) or 0)-midpoint),-int(d.get('rating',0) or 0)));window=min(12,len(candidates))
    return rng.choice(candidates[:max(1,window)])


def _sb_pick_bench(rng,index,lo,hi,used,theme_league=0,count=7):
    pool=[];low=max(1,int(lo)-8);high=min(99,int(hi)+3)
    for d in _load_player_defs(False):
        if not isinstance(d,dict):continue
        try:aid=int(d.get('assetId',0) or 0);rating=int(d.get('rating',0) or 0);league=int(d.get('leagueId',0) or 0)
        except Exception:continue
        if aid<=0 or aid in used or rating<low or rating>high:continue
        if int(index)>=2 and theme_league and league!=int(theme_league):continue
        pool.append(d)
    rng.shuffle(pool);out=[]
    for d in pool:
        aid=int(d.get('assetId',0) or 0)
        if aid in used:continue
        used.add(aid);out.append(d)
        if len(out)>=int(count):break
    if len(out)<int(count):
        fallback=[d for d in _load_player_defs(False) if isinstance(d,dict) and int(d.get('assetId',0) or 0)>0 and int(d.get('assetId',0) or 0) not in used]
        rng.shuffle(fallback)
        for d in fallback:
            aid=int(d.get('assetId',0) or 0);used.add(aid);out.append(d)
            if len(out)>=int(count):break
    return out[:int(count)]


def _sb_make_opponent(index,refresh_id,rng):
    profiles=(
        ('Bronze Wanderers',45,64),
        ('Gold Challengers',75,79),
        ('Gold Contenders',80,84),
        ('Gold Elite',85,91),
    )
    index=max(0,min(3,int(index)));name,lo,hi=profiles[index]
    theme_league=int(_SB_THEME_LEAGUES[index]);theme_count=int(_SB_THEME_COUNTS[index])
    themed_slots=set(_SB_THEME_SLOT_ORDER[:theme_count])
    used=set();starter_defs=[]
    for slot,position in enumerate(_SB_F442_STARTER_POSITIONS):
        d=_sb_pick_slot_player(rng,index,position,lo,hi,used,theme_league,slot in themed_slots)
        if d is None:continue
        starter_defs.append(d);used.add(int(d.get('assetId',0) or 0))
    # A standard FUT squad needs eleven populated starters.  The position picker
    # has a full-roster fallback, but keep a final defensive fallback for a
    # corrupt/short cache rather than emitting a malformed opponent.
    if len(starter_defs)<11:
        for slot in range(len(starter_defs),11):
            pos=_SB_F442_STARTER_POSITIONS[slot]
            fallback=_sb_pick_slot_player(rng,index,pos,1,99,used,0,False)
            if fallback is not None:starter_defs.append(fallback);used.add(int(fallback.get('assetId',0) or 0))
    bench_defs=_sb_pick_bench(rng,index,lo,hi,used,theme_league,7)
    defs=(starter_defs+bench_defs)[:18];players=[];ratings=[]
    base_id=888000000000+int(refresh_id%1000000)*10000+index*100
    for slot,d in enumerate(defs):
        item=_definition_item(d,pile=0,rare=None,item_id=base_id+slot+1)
        item['concept']=False;item['itemState']='free';item['untradeable']=True;item['tradeable']=False
        item['owners']=1;item['contract']=99;item['contracts']=99;item['fitness']=99;item['morale']=50;item['formation']='f442';item['loyaltyBonus']=1
        players.append({'index':slot,'kitNumber':slot+1,'loyaltyBonus':1,'itemData':item})
        if slot<11:ratings.append(int(item.get('rating',0) or 0))
    while len(players)<23:players.append({'index':len(players),'kitNumber':0,'loyaltyBonus':0})
    starters=[x['itemData'] for x in players[:11] if isinstance(x.get('itemData'),dict)]
    rating=round(sum(ratings)/len(ratings)) if ratings else lo
    chemistry=_sb_estimated_team_chemistry(starters)
    opp_id=920001+index;ids=[int(x.get('id',0) or 0) for x in starters if int(x.get('id',0) or 0)>0]
    kick=_resolve_squad_kicktakers(starters, captain_id=ids[0] if ids else 0)
    identity_team_ids=(1,1,21,243)
    team_id=identity_team_ids[index]
    squad={'id':opp_id,'squadId':opp_id,'valid':True,'personaId':FAKE_PERSONA,
           'squadName':name,'name':name,'formation':'f442','active':True,
           'captain':ids[0] if ids else 0,'chemistry':chemistry,'changed':0,
           'starRating':max(1,min(5,round((rating-55)/7))),'rating':rating,
           'dreamSquad':None,'newSquad':0,'newsquad':0,'squadType':'REGULAR_SQUAD',
           'custom':None,'players':players,'actives':[],'manager':[],'club':[],
           'kicktakers':kick}
    squad['personaId']=1100000000+(opp_id%100000000)
    return {'id':opp_id,'opponentId':opp_id,'squadId':opp_id,'name':name,'teamName':name,'clubName':name,
            'rating':rating,'teamRating':rating,'chemistry':chemistry,'teamChemistry':chemistry,'difficultyBand':index,
            'teamId':team_id,'played':False,'available':True,'result':'','battlePoints':0,'squad':squad,
            'opponentSquad':squad,'itemData':[x.get('itemData') for x in players if isinstance(x.get('itemData'),dict)]}

def _sb_generate(force=False):
    st=_sb_load()
    if st.get('opponents') and not force:return st
    refresh=max(1,int(st.get('refreshId',0) or 0)+1);seed=secrets.randbits(63) if 'secrets' in globals() else random.SystemRandom().randrange(1,2**63)
    rng=random.Random(seed);opps=[_sb_make_opponent(i,refresh,rng) for i in range(4)]
    for i in range(4):
        # Do not mutate rating/chemistry after generation: both values must stay
        # derived from the cards FIFA actually renders.
        sq=opps[i].get('squad') if isinstance(opps[i].get('squad'),dict) else {}
        log.warning('SQUAD BATTLES OPPONENT ACCURATE index=%d name=%s rating=%s chemistry=%s positions=%s leagues=%s',
                    i,opps[i].get('name'),opps[i].get('rating'),opps[i].get('chemistry'),
                    [str(((r.get('itemData') or {}).get('position',''))) for r in (sq.get('players') or [])[:11] if isinstance(r,dict)],
                    [int(((r.get('itemData') or {}).get('leagueId',0) or 0)) for r in (sq.get('players') or [])[:11] if isinstance(r,dict)])
    st.update({'refreshId':refresh,'refreshesUsed':int(st.get('refreshesUsed',0) or 0)+1,'seed':seed,'generatedAt':int(time.time()),
               'opponents':opps,'selectedOpponentId':0,'matchInProgress':False,'lastMatchStart':0})
    _sb_save(st);log.warning('SQUAD BATTLES REFRESH id=%d opponents=%s',refresh,[(x['name'],x['rating'],x['chemistry']) for x in opps]);return st

def _sb_opponent_persona_id(x):
    """Stable synthetic owner distinct from the local FUT persona."""
    try:oid=max(1,int((x or {}).get('id',0) or 0))
    except Exception:oid=1
    return 1100000000 + (oid % 100000000)


def _sb_view_squad(x):
    """Return a parser-safe standard FUT squad for Squad Battles viewing/play."""
    x=x if isinstance(x,dict) else {}
    raw=x.get('squad') if isinstance(x.get('squad'),dict) else {}
    base=_default_squad()
    oid=int(x.get('id',raw.get('id',0)) or 0)
    name=str(x.get('name',raw.get('name','Local XI')) or 'Local XI')
    players=raw.get('players') if isinstance(raw.get('players'),list) else []
    # Always expose exactly 23 slot records, matching the regular squad wire.
    clean=[]
    for slot in range(23):
        src=players[slot] if slot<len(players) and isinstance(players[slot],dict) else {}
        row={'index':slot,'loyaltyBonus':int(src.get('loyaltyBonus',0) or 0),
             'kitNumber':int(src.get('kitNumber',slot+1 if slot<11 else 0) or 0)}
        item=src.get('itemData')
        if isinstance(item,dict) and int(item.get('id',0) or 0)>0:
            row['itemData']=dict(item)
        clean.append(row)
    captain=int(raw.get('captain',0) or 0)
    if captain<=0:
        for row in clean[:11]:
            item=row.get('itemData')
            if isinstance(item,dict) and int(item.get('id',0) or 0)>0:
                captain=int(item['id']);break
    rating=int(x.get('rating',raw.get('rating',0)) or 0)
    chemistry=int(x.get('chemistry',raw.get('chemistry',0)) or 0)
    base.update({
        'id':oid,'squadId':oid,'valid':True,'personaId':_sb_opponent_persona_id(x),
        'squadName':name,'name':name,'formation':str(raw.get('formation','f442') or 'f442'),
        'active':True,'captain':captain,'chemistry':chemistry,'changed':0,
        'starRating':int(raw.get('starRating',max(1,min(5,round((rating-55)/7)))) or 1),
        'rating':rating,'dreamSquad':None,'newSquad':0,'newsquad':0,
        'squadType':'REGULAR_SQUAD','custom':None,'players':clean,
        'actives':[],'manager':[],'club':[],
        'kicktakers':raw.get('kicktakers',[]) if isinstance(raw.get('kicktakers'),list) else [],
    })
    return base

def _sb_match_team_id(x):
    x=x if isinstance(x,dict) else {}
    try:team_id=int(x.get('teamId',243) or 243)
    except Exception:team_id=243
    known={int(row[0]) for row in KIT_TEAM_DEFS}
    return team_id if team_id in known else 243


def _sb_opponent_club_items(x):
    """Build match-only active kit/badge/stadium items for a generated opponent.

    View Squad intentionally remains the standard parser-safe squad body.  The
    final /match handoff gets the additional active club items that the native
    offline match loader needs to resolve both uniforms and instantiate the venue.
    IDs are opponent-local so they cannot collide with the user's active items.
    """
    x=x if isinstance(x,dict) else {}
    team_id=_sb_match_team_id(x)
    # Every generated opponent is assigned one of the known teams above. Keep a
    # deterministic Real Madrid fallback for migrated/corrupt state.
    idx=next((i for i,row in enumerate(KIT_TEAM_DEFS) if int(row[0])==team_id),len(KIT_TEAM_DEFS)-1)
    h=dict(HOME_KIT_ITEMS[idx]);a=dict(AWAY_KIT_ITEMS[idx])
    b=next((dict(row) for row in BADGE_ITEMS if int(row.get('teamId',0) or 0)==team_id),dict(BADGE_ITEMS[-1]))
    # Match transient ids live in a separate range and are stable per opponent.
    oid=max(0,int(x.get('id',0) or 0));base=886000000000+(oid%100000)*10
    h['id']=h['itemId']=base+1;h['pile']=7;h['itemState']='activeHomeKit'
    a['id']=a['itemId']=base+2;a['pile']=7;a['itemState']='activeAwayKit'
    b['id']=b['itemId']=base+3;b['pile']=7;b['itemState']='activeBadge'
    stadium=dict(DEFAULT_STADIUM_ITEM);stadium['id']=stadium['itemId']=base+4;stadium['pile']=7;stadium['itemState']='activeStadium'
    return [h,a,b,stadium]

def _sb_match_opponent_squad(x):
    sq=_sb_view_squad(x)
    act=_sb_opponent_club_items(x)
    sq['actives']=[dict(row) for row in act]
    sq['club']=[dict(row) for row in act]
    return sq


def _sb_public_opponent(x,st=None):
    st=st or _sb_load();o=dict(x or {});sq=_sb_view_squad(o)
    o.update({'squad':sq,'opponentSquad':sq,'squadData':sq,'team':sq,'isPlayed':bool(o.get('played')),
              'isAvailable':not bool(o.get('played')),'status':'PLAYED' if o.get('played') else 'AVAILABLE',
              'competitionId':int(st.get('competitionId',1) or 1),'endDateTime':int(st.get('competitionEnd',0) or 0)})
    return o

def _sb_tier(points):
    pts=max(0,int(points or 0))
    tiers=((40000,'ELITE_1'),(25000,'ELITE_2'),(15000,'ELITE_3'),(9000,'GOLD_1'),(6000,'GOLD_2'),(3500,'GOLD_3'),
           (2500,'SILVER_1'),(1500,'SILVER_2'),(750,'SILVER_3'),(350,'BRONZE_1'),(100,'BRONZE_2'),(0,'BRONZE_3'))
    return next(name for threshold,name in tiers if pts>=threshold)

def _sb_rankings_payload(st=None):
    st=st or _sb_load();thresholds=((0,'BRONZE_3'),(100,'BRONZE_2'),(350,'BRONZE_1'),(750,'SILVER_3'),(1500,'SILVER_2'),(2500,'SILVER_1'),(3500,'GOLD_3'),(6000,'GOLD_2'),(9000,'GOLD_1'),(15000,'ELITE_3'),(25000,'ELITE_2'),(40000,'ELITE_1'))
    rows=[]
    for i,(pts,name) in enumerate(thresholds):
        rows.append({'id':i,'rank':name,'rankName':name,'ranking':name,'minPoints':pts,'minimumPoints':pts,'pointsRequired':pts,'minMatchesToRank':1})
    return rows

def _sb_ai_entries(st=None):
    """Deterministic local rivals for Bronze->Elite->Top 100 progression."""
    st=st or _sb_load();cid=int(st.get('competitionId',1) or 1);rows=[]
    for i in range(_SB_AI_COUNT):
        rng=random.Random((cid*1000003)+(i*7919)+3619)
        # The 100th rival is deliberately around 45k: Top 100 only becomes
        # reachable after Elite 1 (40k), while 30 strong matches can still win it.
        target=max(5000,int(72000-(i*270)+rng.randint(-700,700)))
        games=max(16,min(_SB_POINT_MATCH_LIMIT,18+int(rng.random()*13)))
        score=max(0,target)
        base=_SB_AI_NAMES[i%len(_SB_AI_NAMES)];suffix=(i//len(_SB_AI_NAMES))+1
        name=base if suffix==1 else f'{base} {suffix}'
        win_rate=0.48+(rng.random()*0.42);wins=min(games,int(round(games*win_rate)));draws=min(games-wins,int(round(games*(0.02+rng.random()*0.08))));losses=max(0,games-wins-draws)
        rows.append({'personaId':8800000000+i,'displayName':name,'clubName':name,'score':score,'points':score,'battlePoints':score,
                     'matchesPlayed':games,'gamesPlayed':games,'wins':wins,'draws':draws,'losses':losses,'rankName':_sb_tier(score),'isUser':False})
    rows.sort(key=lambda x:(-int(x['score']),str(x['displayName'])))
    return rows

def _sb_user_position(st=None):
    st=st or _sb_load();pts=max(0,int(st.get('battlePoints',0) or 0));return 1+sum(1 for x in _sb_ai_entries(st) if int(x.get('score',0) or 0)>pts)

def _sb_rank(st=None):
    st=st or _sb_load();pts=max(0,int(st.get('battlePoints',0) or 0));played=int(st.get('matchesPlayed',0) or 0);point_played=min(_SB_POINT_MATCH_LIMIT,int(st.get('pointMatchesPlayed',played) or 0));position=_sb_user_position(st);tier=_sb_tier(pts)
    return {'rank':tier,'rankName':tier,'position':position,'rankNumber':position,'leaderboardRank':position,'points':pts,'battlePoints':pts,'score':pts,
            'matchesPlayed':played,'gamesPlayed':played,'pointMatchesPlayed':point_played,'matchesRemaining':max(0,_SB_POINT_MATCH_LIMIT-point_played),
            'maxMatches':_SB_POINT_MATCH_LIMIT,'canEarnPoints':point_played<_SB_POINT_MATCH_LIMIT,
            'wins':int(st.get('wins',0) or 0),'draws':int(st.get('draws',0) or 0),'losses':int(st.get('losses',0) or 0),'totalCompetitors':_SB_AI_COUNT+1}

def _sb_leaderboard(st=None,limit=100):
    st=st or _sb_load();rank=_sb_rank(st)
    user={'personaId':FAKE_PERSONA,'displayName':PERSONA,'clubName':_club_name(),'score':rank['battlePoints'],'points':rank['battlePoints'],'battlePoints':rank['battlePoints'],
          'matchesPlayed':rank['pointMatchesPlayed'],'gamesPlayed':rank['pointMatchesPlayed'],'wins':rank['wins'],'draws':rank['draws'],'losses':rank['losses'],
          'rankName':rank['rankName'],'isUser':True}
    rows=_sb_ai_entries(st)+[user];rows.sort(key=lambda x:(-int(x.get('score',0) or 0),0 if x.get('isUser') else 1,str(x.get('displayName',''))))
    for pos,row in enumerate(rows,1):row['position']=pos;row['rank']=pos;row['leaderboardRank']=pos
    return rows[:max(1,min(100,int(limit or 100)))]

def _sb_competition_payload(st=None):
    st=st or _sb_load();now=int(time.time());start=int(st.get('competitionStart',now) or now);end=int(st.get('competitionEnd',now+_SB_WEEK_SECONDS) or now+_SB_WEEK_SECONDS);remaining=max(0,end-now);rank=_sb_rank(st)
    return {'id':int(st.get('competitionId',1) or 1),'competitionId':int(st.get('competitionId',1) or 1),'name':'Local Squad Battles','type':'SQUAD_BATTLE',
            'status':'ACTIVE','state':'ACTIVE','competitionStatus':'ACTIVE','active':True,'isActive':True,'ended':False,'isEnded':False,
            'startDateTime':start,'endDateTime':end,'startTime':start,'endTime':end,'starttime':start,'endtime':end,'start':start,'end':end,'competitionStart':start,'competitionEnd':end,
            'secondsUntilStart':0,'secondsRemaining':remaining,'timeRemaining':remaining,'untilEndSeconds':remaining,'durationSeconds':_SB_WEEK_SECONDS,
            'maxMatches':_SB_POINT_MATCH_LIMIT,'pointMatchLimit':_SB_POINT_MATCH_LIMIT,'matchesPlayed':rank['pointMatchesPlayed'],
            'matchesRemaining':rank['matchesRemaining'],'canEarnPoints':rank['canEarnPoints']}

def _sb_payload(st=None):
    st=st or _sb_generate(False);opps=[_sb_public_opponent(x,st) for x in st.get('opponents',[]) if isinstance(x,dict)];rank=_sb_rank(st);selected=int(st.get('selectedOpponentId',0) or 0);diff=_sb_difficulty(st.get('selectedDifficulty',3));comp=_sb_competition_payload(st)
    difficulties=[{'id':i,'difficultyId':i,'name':x[0],'value':x[0],'winMultiplier':x[1],'lossMultiplier':x[2]} for i,x in enumerate(_SB_DIFFICULTIES)]
    all_played=bool(opps) and all(bool(x.get('played')) for x in opps)
    return {'success':True,'enabled':True,'active':True,'mode':'SQUAD_BATTLE','competitionId':comp['competitionId'],'competition':comp,'currentCompetition':comp,
            'startDateTime':comp['startDateTime'],'endDateTime':comp['endDateTime'],'endTime':comp['endTime'],'competitionStatus':'ACTIVE','secondsRemaining':comp['secondsRemaining'],
            'refreshId':int(st.get('refreshId',0) or 0),'refreshesUsed':int(st.get('refreshesUsed',0) or 0),'generatedAt':int(st.get('generatedAt',0) or 0),
            'opponents':opps,'squads':opps,'items':opps,'opponentSquads':opps,'opponentSelection':opps,'count':len(opps),'availableSquads':sum(1 for x in opps if not x.get('played')),
            'selectedOpponentId':selected,'selectedDifficulty':diff,'difficulty':diff,'difficultyName':_SB_DIFFICULTIES[diff][0],'difficulties':difficulties,
            'battlePoints':rank['battlePoints'],'points':rank['points'],'squadBattlesScore':rank['battlePoints'],'rank':rank,'userRank':rank,'leaderboardRank':rank['position'],'position':rank['position'],'sqbtEvent':comp,'sqbtEventId':comp['competitionId'],'sqbtOppSquadList':opps,'sqbtOppSquads':[x.get('squad') for x in opps],
            'matchesPlayed':rank['matchesPlayed'],'gamesPlayed':rank['matchesPlayed'],'pointMatchesPlayed':rank['pointMatchesPlayed'],'maxMatches':_SB_POINT_MATCH_LIMIT,
            'matchesRemaining':rank['matchesRemaining'],'remainingMatches':rank['matchesRemaining'],'squadsRemaining':sum(1 for x in opps if not x.get('played')),'canEarnPoints':rank['canEarnPoints'],'wins':rank['wins'],'draws':rank['draws'],'losses':rank['losses'],
            'refreshAvailable':all_played or not opps,'canRefresh':all_played or not opps,'featuredSquad':opps[-1] if opps else None}

def _sb_tier_level(points):
    # FIFA 18 SqBt numeric tier order, Bronze 3 -> Elite 1.
    name=_sb_tier(points)
    order=('BRONZE_3','BRONZE_2','BRONZE_1','SILVER_3','SILVER_2','SILVER_1','GOLD_3','GOLD_2','GOLD_1','ELITE_3','ELITE_2','ELITE_1')
    try:return order.index(name)
    except ValueError:return 0

def _sb_native_opponent_descriptor(x,st=None):
    """Exact FIFA 18 FutSquadBattleOpponentInfo wire.

    CardsDLL has no explicit played/available member here.  Retail uses the
    score/points sentinels: -1 means the opponent has not yet been played.
    A non-negative pointsWon marks a completed opponent.
    """
    st=st or _sb_load();x=x if isinstance(x,dict) else {}
    oid=int(x.get('id',0) or 0);rating=int(x.get('rating',0) or 0);chem=int(x.get('chemistry',0) or 0)
    name=str(x.get('name',x.get('clubName','Local XI')) or 'Local XI')
    words=[w for w in re.split(r'[^A-Za-z0-9]+',name.upper()) if w]
    abbr=(''.join(w[:1] for w in words)[:3] or name[:3].upper() or 'LFC')
    base_win=max(100,int(round((rating*4.0)+(chem*2.0)+250)))
    base_loss=max(50,int(round((rating*1.5)+(chem*0.75))))
    played=bool(x.get('played'))
    if played:
        points=max(0,int(x.get('battlePoints',0) or 0))
        score=max(0,int(x.get('userScore',0) or 0))
        opp_score=max(0,int(x.get('oppScore',x.get('opponentScore',0)) or 0))
        pen=int(x.get('penaltyScore',-1) if x.get('penaltyScore') is not None else -1)
        opp_pen=int(x.get('oppPenaltyScore',-1) if x.get('oppPenaltyScore') is not None else -1)
    else:
        points=score=opp_score=pen=opp_pen=-1
    out={
        'id':oid,
        'squadId':int(x.get('squadId',oid) or oid),
        'clubName':name,
        'clubAbbr':abbr,
        'badgeAssetId':int(x.get('teamId',0) or 0),
        'chemistry':chem,
        'rating':rating,
        'nation':0,
        'basePointsWin':base_win,
        'basePointsLoss':base_loss,
        'pointsWon':points,
        'score':score,
        'oppScore':opp_score,
        'penaltyScore':pen,
        'oppPenaltyScore':opp_pen,
    }
    return out

def _sb_native_opponent_list(st=None):
    st=st or _sb_generate(False);now=int(time.time())
    opps=[x for x in st.get('opponents',[]) if isinstance(x,dict)]
    all_played=bool(opps) and all(bool(x.get('played')) for x in opps)
    generated=int(st.get('generatedAt',now) or now)
    next_refresh=now if all_played else max(now+1,generated+(24*60*60))
    return {
        'nextSquadRefreshTimeStamp':int(next_refresh),
        'sqbtOppSquads':[_sb_native_opponent_descriptor(x,st) for x in opps],
    }

def _sb_hub_payload(st=None):
    """Exact FIFA 18 SqBt hub contract recovered from CardsDLL.

    The client parser expects currentTime/startTime/endTime as root int64 values,
    score/rank/tier scalars, three seven-element difficulty arrays, and a nested
    sqbtOppSquadList object.  Previous speculative aliases are deliberately not
    emitted here because several of them map to different native types.
    """
    st=st or _sb_generate(False);rank=_sb_rank(st);comp=_sb_competition_payload(st)
    now=int(time.time());start=int(comp.get('startDateTime',now) or now);end=int(comp.get('endDateTime',now+_SB_WEEK_SECONDS) or now+_SB_WEEK_SECONDS)
    # Ensure the wire can never describe an expired event during the active local week.
    if end<=now:
        st,_=_sb_competition_bootstrap(st,now);_sb_save(st)
        start=int(st.get('competitionStart',now) or now);end=int(st.get('competitionEnd',now+_SB_WEEK_SECONDS) or now+_SB_WEEK_SECONDS)
    win_mod=[float(x[1]) for x in _SB_DIFFICULTIES]
    loss_mod=[float(x[2]) for x in _SB_DIFFICULTIES]
    out={
        'currentTime':int(now),
        'startTime':int(start),
        'endTime':int(end),
        'sqbtEventId':int(st.get('competitionId',1) or 1),
        'score':int(rank.get('battlePoints',0) or 0),
        # Outside Top 100 the tier crest drives Bronze->Elite progression.
        'rank':int(rank.get('position',0) or 0) if int(rank.get('position',0) or 0)<=100 else 0,
        'userTierLevel':int(_sb_tier_level(rank.get('battlePoints',0))),
        'isPrizeAvailable':False,
        'maxMatches':int(_SB_POINT_MATCH_LIMIT),
        'numberOfMatchesPlayed':int(rank.get('pointMatchesPlayed',0) or 0),
        'finishedMatches':int(rank.get('pointMatchesPlayed',0) or 0),
        'gamesPlayed':int(rank.get('pointMatchesPlayed',0) or 0),
        'remainingMatches':int(rank.get('matchesRemaining',_SB_POINT_MATCH_LIMIT) or 0),
        'squadsRemaining':sum(1 for x in st.get('opponents',[]) if isinstance(x,dict) and not bool(x.get('played'))),
        'lastMatchUnfinished':False,
        'difficultyModifiers':win_mod,
        'winDifficultyModifiers':win_mod,
        'lossDifficultyModifiers':loss_mod,
        'nextFeatureSquadTimeStamp':int(end),
        'nextLBRefreshTimeStamp':int(min(end,now+300)),
        'sqbtOppSquadList':_sb_native_opponent_list(st),
    }
    log.warning('SQUAD BATTLES HUB native-wire bytes=%d event=%s now=%s start=%s end=%s score=%s rank=%s opps=%d',
                len(json.dumps(out,separators=(',',':')).encode('utf-8')),out['sqbtEventId'],now,start,end,out['score'],out['rank'],
                len(out['sqbtOppSquadList'].get('sqbtOppSquads',[])))
    return out

def _sb_feature_stats_payload(st=None):
    """Small native-facing Featured Squad/Squad Battles state record.

    v36.21 proved FIFA 18 accepts the scalar /sqbt/user/hub then immediately
    consumes this route.  An empty object renders the exact fallback seen in the
    client: competition ended + blank 0/0 Featured Squad.  Keep this response
    flat/scalar and put the actual 23-player squad behind /featuredsquad/<id>.
    """
    st=st or _sb_generate(False)
    rank=_sb_rank(st)
    comp=_sb_competition_payload(st)
    now=int(time.time())
    start=int(comp.get('startDateTime',now) or now)
    end=int(comp.get('endDateTime',now+_SB_WEEK_SECONDS) or now+_SB_WEEK_SECONDS)
    remaining=max(1,end-now)
    opps=[x for x in st.get('opponents',[]) if isinstance(x,dict)]
    # The retail screen has a separate Featured Squad tab.  Reuse the strongest
    # generated local opponent as that feature until FIFA asks for its body.
    featured=opps[-1] if opps else None
    fid=int((featured or {}).get('id',0) or 0)
    rating=int((featured or {}).get('rating',0) or 0)
    chemistry=int((featured or {}).get('chemistry',0) or 0)
    name=str((featured or {}).get('name','Local Featured XI') or 'Local Featured XI')
    out={
        'featureConsumerId':'sqbt',
        'featureId':fid,
        'featuredSquadId':fid,
        'squadId':fid,
        'opponentId':fid,
        'name':name,
        'squadName':name,
        'teamName':name,
        'rating':rating,
        'teamRating':rating,
        'chemistry':chemistry,
        'teamChemistry':chemistry,
        'pointsAvailable':2000,
        'maxPoints':2000,
        'played':False,
        'isPlayed':False,
        'active':True,
        'isActive':True,
        'ended':False,
        'isEnded':False,
        'status':'ACTIVE',
        'state':'ACTIVE',
        'eventId':int(st.get('competitionId',1) or 1),
        'sqbtEventId':int(st.get('competitionId',1) or 1),
        'startDateTime':start,
        'endDateTime':end,
        'startTime':start,
        'endTime':end,
        'secondsUntilStart':0,
        'secondsUntilEnd':remaining,
        'timeUntilStart':0,
        'timeUntilEnd':remaining,
        'nextFeatureSquadTimeStamp':int(end),
        'nextSquadRefreshTimeStamp':int(now+max(1,min(24*60*60,remaining))),
        'nextLBRefreshTimeStamp':int(min(end,now+300)),
        'battlePoints':int(rank.get('battlePoints',0) or 0),
        'squadBattlesScore':int(rank.get('battlePoints',0) or 0),
        'matchesPlayed':int(rank.get('pointMatchesPlayed',0) or 0),
        'numberOfMatchesPlayed':int(rank.get('pointMatchesPlayed',0) or 0),
        'remainingMatches':int(rank.get('matchesRemaining',_SB_POINT_MATCH_LIMIT) or 0),
        'finishedMatches':int(rank.get('pointMatchesPlayed',0) or 0),
        'gamesPlayed':int(rank.get('pointMatchesPlayed',0) or 0),
        'squadsRemaining':sum(1 for x in st.get('opponents',[]) if isinstance(x,dict) and not bool(x.get('played'))),
        'lastMatchUnfinished':False,
        'maxMatches':int(_SB_POINT_MATCH_LIMIT),
    }
    log.warning('SQUAD BATTLES FEATURE STATS active-flat bytes=%d id=%s name=%r rating=%s chem=%s end=%s points=%s matches=%s/%s',
                len(json.dumps(out,separators=(',',':')).encode('utf-8')),fid,name,rating,chemistry,end,
                out['squadBattlesScore'],out['numberOfMatchesPlayed'],_SB_POINT_MATCH_LIMIT)
    return out

def _sb_select(doc=None,path=''):
    st=_sb_generate(False);doc=doc if isinstance(doc,dict) else {};oid=0
    for key in ('sqbtOpponentSquadId','sqbtOppid','opponentId','selectedOpponentId','opponentSquadId','id','squadId'):
        try:
            if key in doc and int(doc.get(key,0) or 0)>0:oid=int(doc[key]);break
        except Exception:pass
    if not oid:
        m=re.search(r'/(92000[1-4])(?:/|$)',str(path or ''))
        if m:oid=int(m.group(1))
    if not oid:
        available=[x for x in st.get('opponents',[]) if isinstance(x,dict) and not x.get('played')]
        if available:oid=int(available[0].get('id',0) or 0)
    diff=st.get('selectedDifficulty',3)
    for key in ('difficulty','difficultyId','gameDifficulty','skillLevel','selectedDifficulty','sqbtMatchDifficulty'):
        if key in doc:diff=doc.get(key);break
    diff=_sb_difficulty(diff)
    if not any(int(x.get('id',0) or 0)==oid for x in st.get('opponents',[]) if isinstance(x,dict)):return None
    st['selectedOpponentId']=oid;st['selectedDifficulty']=diff;_sb_save(st);return st

def _sb_selected(st=None):
    st=st or _sb_load();oid=int(st.get('selectedOpponentId',0) or 0);return next((x for x in st.get('opponents',[]) if isinstance(x,dict) and int(x.get('id',0) or 0)==oid),None)

def _sb_match_manager_item(item, squad_id):
    """Normalize one manager only for the gameplay handoff."""
    fixed=dict(item or {})
    try:asset=int(fixed.get('assetId',fixed.get('headId',fixed.get('managerId',0))) or 0)
    except Exception:asset=0
    if asset>0:
        fixed['assetId']=asset;fixed['headId']=asset;fixed['managerId']=asset
        fixed['definitionId']=asset+1000000;fixed['resourceId']=asset+1000000
    try:sid=max(1,int(squad_id or 1))
    except Exception:sid=1
    iid=884000000000+(sid%100000000)
    fixed['id']=iid;fixed['itemId']=iid
    fixed['concept']=False;fixed['itemState']='free';fixed['untradeable']=True;fixed['tradeable']=False
    fixed['contract']=99;fixed['contracts']=99;fixed['loans']=0
    fixed.pop('draftItem',None)
    return fixed


def _sb_transient_match_squad(squad, *, opponent=False):
    """Project a FUT squad to the transient shape measured entering gameplay.

    The successful FIFA 18 Draft capture is our measured CreateMatch ->
    MatchReady -> gameplay contract. BETA6 normalized rating/formation but still
    sent persistent inventory semantics and omitted the custom FUT opponent.
    Leave persistent club data untouched and normalize only this match copy.
    """
    if opponent:
        raw=dict(squad or {})
    else:
        raw=_native_regular_match_squad(squad if isinstance(squad,dict) else _active_squad())
    sq=dict(raw or {})
    formation=str(sq.get('formation') or 'f442')
    players=[];playable=[]
    for idx,row in enumerate((sq.get('players') or [])[:23]):
        src=dict(row) if isinstance(row,dict) else {'index':idx}
        out={'index':int(src.get('index',idx) or idx),'kitNumber':int(src.get('kitNumber',0) or 0)}
        if 'loyaltyBonus' in src:
            try:out['loyaltyBonus']=int(src.get('loyaltyBonus',0) or 0)
            except Exception:out['loyaltyBonus']=0
        item=src.get('itemData') if isinstance(src.get('itemData'),dict) else None
        if item:
            fixed=dict(item)
            fixed['concept']=False
            fixed.pop('draftItem',None)
            fixed.pop('dream',None)
            fixed['itemState']='free'
            fixed['pile']=0
            fixed['untradeable']=True
            fixed['tradeable']=False
            fixed['owners']=max(1,int(fixed.get('owners',1) or 1))
            fixed['contract']=max(99,int(fixed.get('contract',fixed.get('contracts',99)) or 99))
            fixed['contracts']=max(99,int(fixed.get('contracts',fixed.get('contract',99)) or 99))
            fixed['loans']=0
            fixed['fitness']=max(1,int(fixed.get('fitness',99) or 99))
            fixed['morale']=int(fixed.get('morale',50) or 50)
            fixed['formation']=formation
            out['itemData']=fixed
            playable.append((out['index'],fixed))
        players.append(out)
    while len(players)<23:players.append({'index':len(players),'kitNumber':0})
    sq['players']=players[:23]

    starters=[item for idx,item in playable if 0<=idx<11]
    if not starters:starters=[item for _,item in playable[:11]]
    valid_ids=[int(x.get('id',0) or 0) for x in starters if int(x.get('id',0) or 0)>0]
    captain=int(sq.get('captain',0) or 0)
    if captain not in valid_ids and valid_ids:captain=valid_ids[0]
    sq['captain']=captain
    sq['kicktakers']=_resolve_squad_kicktakers(starters, sq.get('kicktakers'), captain)

    sq['custom']='[0,0,0,0,0,0,0,0,0,0,0]'
    sq['changed']=0;sq['active']=True;sq['valid']=True;sq['newSquad']=0;sq['newsquad']=0
    rating=max(1,min(99,int(sq.get('rating',0) or 0)))
    sq['rating']=rating
    raw_star=int(sq.get('starRating',0) or 0)
    sq['starRating']=raw_star if 1<=raw_star<=5 else max(1,min(5,round((rating-55)/7)))
    sq['chemistry']=max(0,min(100,int(sq.get('chemistry',0) or 0)))

    manager=[]
    squad_identity=int(sq.get('id',sq.get('squadId',1)) or 1)
    for i,row in enumerate(sq.get('manager',[]) if isinstance(sq.get('manager'),list) else []):
        if not isinstance(row,dict):continue
        r=dict(row);item=r.get('itemData') if isinstance(r.get('itemData'),dict) else None
        if item:
            r['itemData']=_sb_match_manager_item(item,squad_identity)
            r['index']=int(r.get('index',i) or i);manager.append(r)
    if not manager:
        try:
            fallback=_draft_manager_choices(1)
            if fallback:
                manager=[{'index':0,'itemData':_sb_match_manager_item(fallback[0],squad_identity)}]
        except Exception:
            manager=[]
    sq['manager']=manager
    return sq

def _sb_native_match_squad():
    return _sb_transient_match_squad(_active_squad(),opponent=False)

def _sb_native_opponent_match_squad(opp):
    return _sb_transient_match_squad(_sb_match_opponent_squad(opp),opponent=True)

def _sb_match_start(doc=None,ready=False):
    """Create/acknowledge Squad Battles through the proven Draft wire.

    BETA10 retains the proven BETA8/BETA9 arena and settlement flow and fixes generated XI positional/link chemistry accuracy: the cached opponent
    has its own persona, each side has a unique valid FUT manager instance, and
    opponentTeamId matches the selected opponent's real kit/badge club.
    """
    st=_sb_generate(False);doc=doc if isinstance(doc,dict) else {}
    if not _sb_selected(st):st=_sb_select(doc) or st
    opp=_sb_selected(st)
    if not opp:return None
    now=int(time.time())
    if not ready or not int(st.get('lastMatchStart',0) or 0):st['lastMatchStart']=now
    st['matchInProgress']=True
    if ready:st['lastMatchReady']=now
    else:st['lastMatchReady']=0
    try:st['lastMatchCustomData1']=int(doc.get('customData1',0) or 0)
    except Exception:st['lastMatchCustomData1']=0
    opponent_team_id=_sb_match_team_id(opp)
    st['lastMatchOpponentTeamId']=opponent_team_id
    if ready:
        ready_ids=[]
        for row in doc.get('items',[]) if isinstance(doc.get('items'),list) else []:
            if not isinstance(row,dict):continue
            try:iid=int(row.get('id',row.get('itemId',0)) or 0)
            except Exception:iid=0
            if iid>0 and iid not in ready_ids:ready_ids.append(iid)
        st['lastMatchReadyItemIds']=ready_ids[:11]
    elif not isinstance(st.get('lastMatchReadyItemIds'),list):
        st['lastMatchReadyItemIds']=[]
    _sb_save(st)
    squad=_sb_native_match_squad()
    cached_opponent=_sb_native_opponent_match_squad(opp)
    out={'squad':squad,'opponentTeamId':opponent_team_id,
         'startDateTime':int(st.get('lastMatchStart',0) or now)}
    try:
        snapshot=dict(out);snapshot['_request']=doc;snapshot['_selectedOpponentId']=int(opp.get('id',0) or 0);snapshot['_difficulty']=int(st.get('selectedDifficulty',3) or 3)
        snapshot['_cachedOpponentSquad']=cached_opponent
        (LOGDIR/'last-match-create.json').write_text(json.dumps(snapshot,indent=2,ensure_ascii=False),encoding='utf-8')
    except Exception as e:log.warning('MATCH CREATE snapshot write failed: %s',e)
    own_mgr=((squad.get('manager') or [{}])[0].get('itemData') or {}) if squad.get('manager') else {}
    opp_mgr=((cached_opponent.get('manager') or [{}])[0].get('itemData') or {}) if cached_opponent.get('manager') else {}
    log.warning('SQUAD BATTLES MATCH HANDOFF v1.0 ready=%s opponent=%s opponentPersona=%s opponentTeamId=%s difficulty=%s ownPlayable=%d ownManagerId=%s ownManagerDef=%s oppPlayable=%d oppManagerId=%s oppManagerDef=%s ownActives=%s oppActives=%s start=%s',
                bool(ready),int(opp.get('id',0) or 0),int(cached_opponent.get('personaId',0) or 0),opponent_team_id,int(st.get('selectedDifficulty',3) or 3),
                sum(1 for x in squad.get('players',[]) if isinstance(x.get('itemData'),dict)),own_mgr.get('id'),own_mgr.get('resourceId'),
                sum(1 for x in cached_opponent.get('players',[]) if isinstance(x.get('itemData'),dict)),opp_mgr.get('id'),opp_mgr.get('resourceId'),
                [str(x.get('itemState','')) for x in squad.get('actives',[]) if isinstance(x,dict)],
                [str(x.get('itemState','')) for x in cached_opponent.get('actives',[]) if isinstance(x,dict)],int(st.get('lastMatchStart',0) or now))
    return out

def _sb_stat(doc,names,default=0,opponent=False):
    stats=doc.get('opponentMatchStats' if opponent else 'myMatchStats') if isinstance(doc,dict) else {}
    if not isinstance(stats,dict):stats={}
    for name in names:
        try:
            if name in stats:return int(float(stats.get(name,default) or default))
            if isinstance(doc,dict) and name in doc:return int(float(doc.get(name,default) or default))
        except Exception:pass
    return int(default)

def _sb_match_coin_breakdown(doc,reason):
    completed=reason in ('WIN','DRAW','LOSS');goals=max(0,_sb_stat(doc,('goals','goalsScored')));against=max(0,_sb_stat(doc,('goals','goalsScored'),opponent=True))
    shots=max(0,_sb_stat(doc,('shotsOnTarget','shotsontarget')));tackles=max(0,_sb_stat(doc,('successfulTackles','tacklesWon','tackles')));corners=max(0,_sb_stat(doc,('corners','cornerKicks')))
    passing=max(0,min(100,_sb_stat(doc,('passingPercentage','passAccuracy','passing'))));possession=max(0,min(100,_sb_stat(doc,('possessionPercentage','possession'))))
    fouls=max(0,_sb_stat(doc,('fouls',)));yellows=max(0,_sb_stat(doc,('yellowCards','yellow')));reds=max(0,_sb_stat(doc,('redCards','red')));offsides=max(0,_sb_stat(doc,('offsides',)));motm=1 if _sb_stat(doc,('manOfTheMatch','motm')) else 0
    parts={'goals':min(goals,5)*40,'shotsOnTarget':min(shots,10)*5,'successfulTackles':min(tackles,20),'corners':min(corners,10)*5,
           'cleanSheet':75 if completed and against==0 else 0,'passAccuracy':min(passing,80),'possession':min(possession,80),'manOfTheMatch':15 if motm else 0,
           'goalsConceded':-min(against,4)*20,'fouls':-min(fouls,4)*5,'cards':-min(yellows+reds,8)*10,'offsides':-min(offsides,15)}
    skill=sum(parts.values())
    try:seconds=max(0,int(doc.get('secondsPlayed',0) or 0))
    except Exception:seconds=0
    minutes=90.0 if completed else min(90.0,(seconds/60.0) if seconds>90 else float(seconds));completion=int(round(325.0*minutes/90.0));dnf=1.0;multiplier_supplied=False
    for key in ('dnfModifier','matchMultiplier','coinMultiplier','skillMultiplier','multiplier'):
        if key not in doc:continue
        try:dnf=float(doc.get(key,1.0) or 1.0);multiplier_supplied=True;break
        except Exception:pass
    if completed and multiplier_supplied:dnf=max(0.0,min(2.0,dnf))
    competition=0;boost=0
    if reason in ('DNF','QUIT','NO_CONTEST'):
        # Retail Squad Battles treats an unfinished match as a DNF/loss: it
        # consumes the fixture but grants no match coins or Battle Points.
        parts={k:0 for k in parts};skill=0;completion=0;dnf=1.0;coins=0
    else:
        coins=max(0,int(round(skill*dnf))+completion+competition+boost)
    return {'parts':parts,'skillReward':skill,'completionAward':completion,'competitionReward':competition,'coinBoost':boost,'dnfModifier':dnf,'matchCoins':coins,'goals':goals,'goalsAgainst':against,'minutesPlayed':minutes}

def _sb_battle_points(opp,difficulty,reason,coin_breakdown):
    if reason in ('DNF','QUIT','NO_CONTEST'):return 0
    d=_sb_difficulty(difficulty);name,win_mult,loss_mult,goal_value=_SB_DIFFICULTIES[d];multiplier=win_mult if reason=='WIN' else loss_mult
    rating=max(0,int((opp or {}).get('rating',0) or 0));chem=max(0,int((opp or {}).get('chemistry',0) or 0));match_complete=(rating*3)+(chem/2.0);result=200 if reason=='WIN' else 50
    skill=max(0,int(coin_breakdown.get('skillReward',0) or 0));goal_bonus=min(5,int(coin_breakdown.get('goals',0) or 0))*goal_value
    return max(0,int(round(multiplier*(match_complete+result+skill)+goal_bonus)))

def _sb_apply_match_item_updates(doc,st,consume_contracts=True):
    """Persist post-match fitness and one contract for the XI that entered play.

    MatchReady is authoritative for the starting eleven. /match/end can include
    the bench too, so only the MatchReady XI consumes a contract while every
    reported owned player can receive its returned fitness value.
    """
    doc=doc if isinstance(doc,dict) else {};st=st if isinstance(st,dict) else {}
    starter_ids=[]
    for raw in st.get('lastMatchReadyItemIds',[]) if isinstance(st.get('lastMatchReadyItemIds'),list) else []:
        try:iid=int(raw)
        except Exception:iid=0
        if iid>0 and iid not in starter_ids:starter_ids.append(iid)
    updates=[]
    for raw in doc.get('items',[]) if isinstance(doc.get('items'),list) else []:
        if not isinstance(raw,dict):continue
        try:iid=int(raw.get('id',raw.get('itemId',0)) or 0)
        except Exception:iid=0
        if iid<=0:continue
        update={'id':iid,'itemId':iid}
        if 'fitness' in raw:
            try:update['fitness']=max(0,min(99,int(raw.get('fitness',99) or 0)))
            except Exception:pass
        existing=None
        with _DB_LOCK,_db_connect() as con:
            row=con.execute('SELECT data FROM items WHERE id=?',(iid,)).fetchone()
        if row:
            try:existing=json.loads(row['data'])
            except Exception:existing=None
        if isinstance(existing,dict) and consume_contracts and iid in starter_ids:
            try:before=max(0,int(existing.get('contract',existing.get('contracts',0)) or 0))
            except Exception:before=0
            after=max(99,before-1) if before>99 else 99
            update['contract']=after;update['contracts']=after
            update['loans']=0
        saved=_save_item(update)
        if isinstance(saved,dict):updates.append(saved)
    return updates


def _sb_match_end(doc=None):
    """Settle a complete Squad Battles match through FIFA 18's proven destroy wire.

    BETA8 reached gameplay but returned a Squad-Battles-specific object from
    FutDestroyMatch. The retail client actually parses the same native
    FutDestroyMatchServerResponse used by the now-working Draft flow. In
    particular QUIT/DNF must be represented as LOSS on that wire, while Battle
    Points/rank stay in the subsequent SqBt hub/stat refresh endpoints.
    """
    doc=doc if isinstance(doc,dict) else {}
    raw_reason=_normalise_match_end_reason(doc)
    wire_reason='LOSS' if raw_reason in ('QUIT','DNF') else raw_reason
    st=_sb_load();opp=_sb_selected(st);diff=_sb_difficulty(st.get('selectedDifficulty',3))
    started=int(st.get('lastMatchStart',0) or 0)
    already=bool(started and int(st.get('lastSettledMatchStart',0) or 0)==started)
    point_eligible=int(st.get('pointMatchesPlayed',0) or 0)<_SB_POINT_MATCH_LIMIT
    breakdown=_sb_match_coin_breakdown(doc,raw_reason)
    award=0;points=0
    opponent_team_id=int(st.get('lastMatchOpponentTeamId',_sb_match_team_id(opp) if opp else 243) or 243)

    if raw_reason!='NO_CONTEST' and not already:
        award=int(breakdown.get('matchCoins',0) or 0)
        if award>0:_meta_set('credits',_credits()+award)
        if point_eligible:
            points=_sb_battle_points(opp,diff,raw_reason,breakdown)
            st['battlePoints']=max(0,int(st.get('battlePoints',0) or 0)+points)
            st['pointMatchesPlayed']=int(st.get('pointMatchesPlayed',0) or 0)+1
        st['matchesPlayed']=int(st.get('matchesPlayed',0) or 0)+1
        if raw_reason=='WIN':st['wins']=int(st.get('wins',0) or 0)+1
        elif raw_reason=='DRAW':st['draws']=int(st.get('draws',0) or 0)+1
        else:st['losses']=int(st.get('losses',0) or 0)+1

        if opp:
            opp['played']=True;opp['available']=False
            opp['result']=wire_reason
            opp['rawEndReason']=raw_reason
            opp['battlePoints']=points;opp['coins']=award;opp['pointEligible']=point_eligible
            user_score=max(0,int(breakdown.get('goals',0) or 0))
            opp_score=max(0,int(breakdown.get('goalsAgainst',0) or 0))
            if raw_reason in ('QUIT','DNF'):
                # DNF fixtures are consumed and shown as a conventional 0-3 loss.
                user_score=0;opp_score=max(3,opp_score)
            opp['userScore']=user_score;opp['oppScore']=opp_score
            opp['penaltyScore']=_sb_stat(doc,('penaltyScore','penaltiesScored'),-1)
            opp['oppPenaltyScore']=_sb_stat(doc,('oppPenaltyScore','opponentPenaltyScore','penaltiesScored'),-1,opponent=True)
            opp['difficulty']=diff

        _sb_apply_match_item_updates(doc,st,consume_contracts=True)
        st['lastSettledMatchStart']=started
        st['lastMatchEndReason']=raw_reason;st['lastMatchWireReason']=wire_reason
        st['lastMatchBattlePoints']=points;st['lastMatchCoins']=award
        st['lastOpponentId']=int((opp or {}).get('id',st.get('selectedOpponentId',0)) or 0)
        st['lastDifficulty']=diff
        st['matchInProgress']=False;st['selectedOpponentId']=0;st['lastMatchReadyItemIds']=[]
        _sb_save(st)
        _record_settle('WIN' if raw_reason=='WIN' else 'DRAW' if raw_reason=='DRAW' else 'LOSS')
    elif already:
        # Idempotent ProtoHttp retry: do not double-credit, double-count or consume
        # another contract. Reuse the persisted last-settlement values.
        points=max(0,int(st.get('lastMatchBattlePoints',0) or 0))
        award=max(0,int(st.get('lastMatchCoins',0) or 0))
        st['matchInProgress']=False;st['selectedOpponentId']=0;st['lastMatchReadyItemIds']=[]
        _sb_save(st)

    credits=_credits();won,draws,losses=_record_triplet()
    try:seconds=max(0,int(doc.get('secondsPlayed',0) or 0))
    except Exception:seconds=0
    difficulty_multiplier=float(_SB_DIFFICULTIES[diff][1] if wire_reason=='WIN' else _SB_DIFFICULTIES[diff][2])

    # Keep this intentionally identical in shape to the proven Draft
    # FutDestroyMatchServerResponse. Squad Battles-specific rank/points objects
    # are served by /sqbt/user/hub and /featuredsquad/user/stats immediately after
    # the native destroy callback instead of being injected into this parser.
    out={
        'endReason':wire_reason,'secondsPlayed':seconds,'matchDifficulty':diff,
        'opponentTeamId':opponent_team_id,
        'items':doc.get('items',[]) if isinstance(doc.get('items'),list) else [],
        'matchData':str(doc.get('matchData','') or ''),
        'completionAward':int(breakdown.get('completionAward',0) or 0),
        'skillAward':int(breakdown.get('skillReward',0) or 0),
        'rewardCoins':award,'totalCoins':award,
        'credits':credits,'coins':credits,'sessionCoinsBankBalance':credits,
        'currencies':[{'name':'coins','funds':credits,'finalFunds':credits},{'name':'points','funds':0,'finalFunds':0}],
        'record':{'won':won,'draw':draws,'loss':losses},
        'gamesWon':won,'gamesDraw':draws,'gamesLost':losses,'gamesPlayed':won+draws+losses,
        'unopenedPacks':_unopened_packs_payload(),'dnfModifier':1.0,
        'won':1 if wire_reason=='WIN' else 0,'draw':1 if wire_reason=='DRAW' else 0,
        'loss':1 if wire_reason=='LOSS' else 0,
        'matchCoinPartials':None,
        'matchCoinMultipliers':[{'type':'DIFFICULTY','value':difficulty_multiplier}],
        'boostConis':0,
    }
    if isinstance(doc.get('myMatchStats'),dict):out['myMatchStats']=doc['myMatchStats']
    if isinstance(doc.get('opponentMatchStats'),dict):out['opponentMatchStats']=doc['opponentMatchStats']
    if 'myRating' in doc:out['myRating']=doc.get('myRating')
    if 'opponentRating' in doc:out['opponentRating']=doc.get('opponentRating')

    # The working Draft flow queues this after /match/end so FIFA immediately
    # refreshes /user and then the mode state instead of retaining the pre-match
    # wallet/W-D-L cache.
    PENDING_USERSESSION_REFRESH.set()
    try:
        rank=_sb_rank(_sb_load())
        snapshot={'request':doc,'response':out,'rawEndReason':raw_reason,'wireEndReason':wire_reason,
                  'battlePointsAwarded':points,'totalBattlePoints':int(rank.get('battlePoints',0) or 0),
                  'matchesPlayed':int(rank.get('pointMatchesPlayed',0) or 0),
                  'matchesRemaining':int(rank.get('matchesRemaining',0) or 0),
                  'lastOpponentId':int(st.get('lastOpponentId',0) or 0)}
        (LOGDIR/'last-sqbt-match-end.json').write_text(json.dumps(snapshot,indent=2,ensure_ascii=False),encoding='utf-8')
    except Exception as e:log.warning('SQUAD BATTLES match-end snapshot failed: %s',e)
    log.warning('SQUAD BATTLES SETTLEMENT v1.0 rawReason=%s wireReason=%s opponent=%s difficulty=%s coins=%s battlePoints=%s credits=%s record=%s-%s-%s alreadySettled=%s UserSessionsRefresh=queued',
                raw_reason,wire_reason,st.get('lastOpponentId',0),_SB_DIFFICULTIES[diff][0],award,points,credits,won,draws,losses,already)
    return out

def _sb_opponent_list_payload(st=None):
    # Exact FutRefreshSquadBattleOpponentsServerResponse body.
    return _sb_native_opponent_list(st or _sb_generate(False))

def _sb_selected_response(st=None):
    st=st or _sb_generate(False);opp=_sb_selected(st);diff=_sb_difficulty(st.get('selectedDifficulty',3));comp=_sb_competition_payload(st)
    out={'sqbtEventId':int(comp['competitionId']),'selectedOpponentId':int(st.get('selectedOpponentId',0) or 0),
         'sqbtOppid':int(st.get('selectedOpponentId',0) or 0),'sqbtMatchDifficulty':diff,
         'difficulty':diff,'difficultyName':_SB_DIFFICULTIES[diff][0]}
    if opp:
        pub=_sb_public_opponent(opp,st)
        out['opponent']={'id':int(pub.get('id',0) or 0),'name':pub.get('name',''),'rating':int(pub.get('rating',0) or 0),
                         'chemistry':int(pub.get('chemistry',0) or 0),'teamId':int(pub.get('teamId',0) or 0),'played':bool(pub.get('played'))}
        out['squad']=_sb_view_squad(opp)
    return out

def _sb_leaderboard_wire(st=None,limit=100):
    """Exact FIFA 18 SqBt leaderboard contract recovered from CardsDLL.

    Root object: {"entries": [...]}.  Each entry parser accepts exactly badge,
    clubName, est, insetUrl, persona, rank, score and tiebreak.  score/tiebreak
    are nested {icon,value} objects; sending a scalar score crashes the client.
    """
    st=st or _sb_load();rows=_sb_leaderboard(st,limit);entries=[]
    for row in rows:
        rank=int(row.get('position',row.get('rank',0)) or 0)
        score=int(row.get('score',row.get('battlePoints',0)) or 0)
        persona=str(row.get('displayName','') or '')
        club=str(row.get('clubName',persona) or persona)
        # badge is an int32 in the native parser.  Use the club badge team id
        # where available; 0 is a valid blank shield fallback for local AI.
        badge=0
        if bool(row.get('isUser')):
            try:badge=int(ONBOARDING_SELECTION.get('badgeId',0) or 0)
            except Exception:badge=0
        entries.append({
            'badge':badge,
            'clubName':club,
            'est':2026,
            'insetUrl':'',
            'persona':persona,
            'rank':rank,
            'score':{'icon':'','value':score},
            'tiebreak':{'icon':'','value':int(row.get('wins',0) or 0)},
        })
    out={'entries':entries}
    log.warning('SQUAD BATTLES LEADERBOARD native-wire rows=%d bytes=%d userRank=%s',len(entries),len(json.dumps(out,separators=(',',':')).encode('utf-8')),_sb_user_position(st))
    return out

def _sb_route(low,method,query,body):
    if not any(token in low for token in ('/sqbt','featuredsquad','squadbattle','squad-battle','squad/battle')):return None
    try:doc=json.loads(body.decode('utf-8','replace') or '{}') if body else {}
    except Exception:doc={}
    if not isinstance(doc,dict):doc={}
    st=_sb_generate(False)
    if 'featuredsquad/user/stats' in low:return _sb_feature_stats_payload(st)
    if low.endswith('/sqbt/user/hub') or low.endswith('/sqbt/hub') or '/sqbt/user/hub/' in low:return _sb_hub_payload(st)
    # Retail refresh contract is only the opponent-list object, never the hub.
    if '/sqbt/user/opponents/refresh' in low and method in ('POST','PUT','GET'):
        return _sb_native_opponent_list(_sb_generate(True))
    if any(x in low for x in ('leaderboard','top100','top/100','standing')):
        return _sb_leaderboard_wire(st,100)
    if '/rank' in low or low.endswith('/rank'):
        return _sb_leaderboard_wire(st,100)
    # Exact retail opponent body route discovered in FIFA 18 CardsDLL.
    opponent_match=re.search(r'/sqbt/user/opponentsquad/(\d+)(?:/|$)',low)
    if opponent_match and method=='GET':
        oid=int(opponent_match.group(1));opp=next((x for x in st.get('opponents',[]) if isinstance(x,dict) and int(x.get('id',0) or 0)==oid),None)
        if not opp:return {}
        out=_sb_native_opponent_match_squad(opp)
        try:(LOGDIR/'last-sqbt-opponentsquad.json').write_text(json.dumps(out,indent=2,ensure_ascii=False),encoding='utf-8')
        except Exception:pass
        mgr=((out.get('manager') or [{}])[0].get('itemData') or {}) if out.get('manager') else {}
        log.warning('SQUAD BATTLES OPPONENT CACHE v1.0 id=%s persona=%s teamId=%s players=%s managerId=%s managerDef=%s actives=%s',
                    oid,int(out.get('personaId',0) or 0),_sb_match_team_id(opp),sum(1 for r in out.get('players',[]) if isinstance(r.get('itemData'),dict)),mgr.get('id'),mgr.get('resourceId'),[(x.get('itemState'),x.get('resourceId')) for x in out.get('actives',[]) if isinstance(x,dict)])
        return out
    featured_id=re.search(r'/featuredsquad/(\d+)(?:/|$)',low)
    if featured_id and method=='GET':
        oid=int(featured_id.group(1));opp=next((x for x in st.get('opponents',[]) if isinstance(x,dict) and int(x.get('id',0) or 0)==oid),None)
        if opp:
            return _sb_view_squad(opp)
        return {}
    if 'featuredsquad' in low:
        opps=st.get('opponents',[]);featured=_sb_public_opponent(opps[-1],st) if opps else None
        if not featured:return {'featureConsumerId':'sqbt','featuredSquad':{}}
        return {'featureConsumerId':'sqbt','featuredSquad':{'id':int(featured.get('id',0) or 0),'name':featured.get('name',''),
                'rating':int(featured.get('rating',0) or 0),'chemistry':int(featured.get('chemistry',0) or 0),'squad':featured.get('squad') or {}}}
    if any(x in low for x in ('opponent','squad/list','squads','selection')) and method=='GET':return _sb_native_opponent_list(st)
    path_select=bool(re.search(r'/(92000[1-4])(?:/|$)',low)) or any(x in low for x in ('/select','/start','/play'))
    if method in ('POST','PUT') and (path_select or any(k in doc for k in ('sqbtOpponentSquadId','sqbtOppid','sqbtMatchDifficulty','opponentId','selectedOpponentId','opponentSquadId','difficulty','difficultyId','gameDifficulty','skillLevel'))):
        st=_sb_select(doc,low)
        if st is None:return {'success':False,'reason':'INVALID_OPPONENT'}
        return _sb_selected_response(st)
    return _sb_hub_payload(st) if '/sqbt' in low else _sb_native_opponent_list(st)

class FutHandler(QuietHTTP):
    def go(self):
        body=self.body()
        parsed=urllib.parse.urlsplit(self.path)
        path='/' + parsed.path.lstrip('/')
        low=path.lower().rstrip('/') or '/'
        query=urllib.parse.parse_qs(parsed.query)
        _log_http_request(self,body,low)

        # Both SID headers are harmless and avoid depending on which SportsWorld
        # layer issued the request.
        headers={'X-UT-SID':'LOCALFUT18-SID','X-POW-SID':'LOCALFUT18-SID','Cache-Control':'no-store'}
        out=None
        ctype='application/json; charset=utf-8'

        # --- POW / EASFC --------------------------------------------------
        if low == '/pow/auth':
            out={'sid':'LOCALFUT18-SID','serverTime':int(time.time()),'lastOnlineTime':int(time.time())}
        elif low == '/pow/bank/user/account':
            out={'currencies':[{'currency':'pow_funds','funds':_credits(),'fundsCapInfo':[{'period':'daily','fundsEarned':0},{'period':'weekly','fundsEarned':0}]}]}
        elif low == '/pow/store/game/fifa18/catalog/list':
            out={'catalogs':[{'catalogId':0,'name':'FUT Store'},{'catalogId':1,'name':'FIFAPoints'}]}
        elif '/pow/store/game/fifa18/catalog/0/item/list' in low:
            out={'items':[dict(x) for x in STORE_PACKS],'endOfList':True,'count':len(STORE_PACKS)}
        elif low == '/pow/inventory/item/list':
            out={'items':[],'endOfList':True,'count':0}
        elif low == '/pow/bank/currency/pow_funds/cap/info':
            out={'currency':'pow_funds','funds':0,'fundsCapInfo':[{'period':'daily','fundsEarned':0},{'period':'weekly','fundsEarned':0}]}
        elif low.startswith('/pow/lvl/weight/'):
            out={'weight':0,'tier':0,'level':0}
        elif low.startswith('/pow/lvl/user/'):
            out={'tier':0,'level':0,'xp':0,'nextLevelXp':0}
        elif low == '/pow/pfyc/user':
            rw,rd,rl=_record_triplet()
            out={'personaId':FAKE_PERSONA,'nucleusId':FAKE_NUCLEUS,'displayName':PERSONA,'clubId':CLUB_ID,'clubName':_club_name(),
                 'won':rw,'draw':rd,'loss':rl,'gamesWon':rw,'gamesDraw':rd,'gamesLost':rl,'gamesPlayed':rw+rd+rl}
        elif low == '/pow/pfyc/user/club':
            out={'clubId':CLUB_ID,'clubName':_club_name(),'clubAbbr':_club_abbr(),'personaId':FAKE_PERSONA,'success':True}
        elif low.startswith('/pow/v2/activity'):
            out={'activities':[],'items':[],'endOfList':True}
        elif low.startswith('/pow/'):
            # Keep unknown POW routes successful but make list-looking routes
            # terminate rather than returning an ambiguous empty object.
            if '/list' in low:
                out={'items':[],'endOfList':True}
            else:
                out={}

        # --- FUT auth / account bootstrap --------------------------------
        elif low == '/ut/auth':
            if self.command == 'DELETE': out={}
            else: out={'sid':'LOCALFUT18-SID','protocol':'http','ipPort':'127.0.0.1:8099'}
        elif low == '/ut/game/fifa18/user/accountinfo':
            out=_account_info()
        elif low == '/ut/game/fifa18/settings':
            out={'configs':[
                {'type':'IsStoreEnabled','value':'1'},
                {'type':'cardPackStoreEnabled','value':'1'},
                {'type':'pointsPackStoreEnabled','value':'1'},
                {'type':'packOpeningAnimationEnabled','value':'1'},
                {'type':'packsAutoClaimed','value':'0'},
                {'type':'squadBuildingChallengeEnabled','value':'1'},
                {'type':'transferMarketEnabled','value':'1'},
                {'type':'enableOfflineDraftMode','value':'1'},
                {'type':'enableSinglePlayerDraftMode','value':'1'},
                {'type':'draftEnabled','value':'1'},
                {'type':'FUTDraftEnabled','value':'1'},
                {'type':'offlineDraftEnabled','value':'1'},
                {'type':'enableDraftMode','value':'1'},
                {'type':'squadBattlesEnabled','value':'1'},
                {'type':'squadBattleEnabled','value':'1'},
                {'type':'enableSquadBattles','value':'1'},
                {'type':'squadBattleFeatureEnabled','value':'1'},
                {'type':'singlePlayerSquadBattleEnabled','value':'1'},
                {'type':'squadBattleCouchPlayEnabled','value':'1'},
            ]}
        elif low.startswith('/ut/game/fifa18/phishing/trusteddevice'):
            out={'trusted':True,'exists':True,'locked':False,'changed':False,'token':'fut18-local-phishing-token','isTrusted':True,'completed':True,'answerRequired':False,'attempts':0}
        elif low.startswith('/ut/game/fifa18/phishing/question'):
            out={'trusted':True,'completed':True,'answerRequired':False,'attempts':0,'question':''}
        elif low.startswith('/ut/game/fifa18/phishing/validate'):
            out={'trusted':True,'completed':True,'answerRequired':False,'attempts':0,'success':True,'token':'fut18-local-phishing-token'}
        elif low.startswith('/ut/game/fifa18/sqbt') or low.startswith('/ut/game/fifa18/featuredsquad') or any(token in low for token in ('squadbattle','squad-battle','squad/battle')):
            out=_sb_route(low,self.command,query,body)
            log.warning('SQUAD BATTLES ROUTE method=%s path=%s refresh=%s selected=%s difficulty=%s',self.command,low,(out or {}).get('refreshId') if isinstance(out,dict) else None,(out or {}).get('selectedOpponentId') if isinstance(out,dict) else None,(out or {}).get('difficultyName') if isinstance(out,dict) else None)
        elif low == '/ut/game/fifa18/match/end' and self.command in ('POST','PUT'):
            try:doc=json.loads(body.decode('utf-8','replace') or '{}')
            except Exception:doc={}
            sb=_sb_load();sb_active=bool(sb.get('matchInProgress'))
            st=_draft_get_state()
            is_draft=bool(st.get('matchInProgress')) or int(doc.get('squadId',0) or 0)==_DRAFT_SQUAD_ID or _draft_stage(st)=='READY_FOR_MATCH'
            if sb_active:
                out=_sb_match_end(doc)
                log.warning('SQUAD BATTLES MATCH END reason=%s opponent=%s difficulty=%s coins=%s battlePoints=%s totalPoints=%s body=%r',out.get('endReason'),sb.get('selectedOpponentId'),_SB_DIFFICULTIES[_sb_difficulty(sb.get('selectedDifficulty',3))][0],out.get('matchCoins'),out.get('matchBattlePoints'),out.get('totalBattlePoints'),body[:2048])
            elif is_draft:
                out=_draft_match_end(doc)
                log.warning('DRAFT MATCH END reason=%s wins=%s completed=%s completionAward=%s body=%r',out.get('endReason'),out.get('draftWins'),out.get('draftEliminated'),out.get('completionAward'),body[:2048])
            else:
                reason=_normalise_match_end_reason(doc);credits=_credits()
                if reason!='NO_CONTEST':_record_settle(reason)
                out={'endReason':reason,'secondsPlayed':int(doc.get('secondsPlayed',0) or 0),'items':doc.get('items',[]) if isinstance(doc.get('items'),list) else [],'matchData':str(doc.get('matchData','') or ''),'credits':credits,'coins':credits,'sessionCoinsBankBalance':credits,'currencies':[{'name':'coins','funds':credits,'finalFunds':credits},{'name':'points','funds':0,'finalFunds':0}],'unopenedPacks':_unopened_packs_payload(),'dnfModifier':1.0,'won':1 if reason=='WIN' else 0,'draw':1 if reason=='DRAW' else 0,'loss':1 if reason in ('LOSS','DNF','QUIT') else 0,'matchCoinPartials':None,'matchCoinMultipliers':[{'type':'DIFFICULTY','value':1.0}],'boostConis':0}
        elif low == '/ut/game/fifa18/match' and self.command in ('POST','PUT'):
            try:doc=json.loads(body.decode('utf-8','replace') or '{}')
            except Exception:doc={}
            sb=_sb_load();st=_draft_get_state()
            try:squad_id=int(doc.get('squadId',0) or 0)
            except Exception:squad_id=0
            # FIFA 18 does not send a separate Squad Battles selection POST.
            # The authoritative opponent + difficulty arrive on POST /match as
            # sqbtOpponentSquadId / sqbtMatchDifficulty. BETA2 ignored those two
            # exact retail names and therefore fell through to the generic OFFLINE
            # response, leaving the client stuck on the opponent-squad screen.
            sb_markers=('sqbtEventId','sqbtOpponentSquadId','sqbtOppid','sqbtMatchDifficulty','opponentId','selectedOpponentId','opponentSquadId','difficultyId','gameDifficulty')
            sb_request=any(k in doc for k in sb_markers)
            if sb_request:
                maybe=_sb_select(doc,low)
                if maybe is not None:sb=maybe
            sb_selected=bool(_sb_selected(sb)) and (sb_request or bool(sb.get('matchInProgress')))
            if sb_selected:
                out=_sb_match_start(doc,ready=(self.command=='PUT' and bool(sb.get('matchInProgress'))))
                if out is None:out={'success':False,'reason':'SQUAD_BATTLE_NOT_READY'}
                sb_now=_sb_load();opp_now=_sb_selected(sb_now);diff_now=_sb_difficulty(sb_now.get('selectedDifficulty',3))
                opp_actives=_sb_opponent_club_items(opp_now) if isinstance(opp_now,dict) else []
                wire_bytes=len(json.dumps(out,separators=(',',':')).encode('utf-8')) if isinstance(out,dict) else 0
                log.warning('SQUAD BATTLES MATCH %s native-create-v1 keys=%s bytes=%s opponent=%s opponentTeamId=%s difficulty=%s start=%s ownPlayers=%s ownActives=%s cachedOppActives=%s body=%r',self.command,sorted((out or {}).keys()) if isinstance(out,dict) else [],wire_bytes,int((opp_now or {}).get('id',0) or 0),int((out or {}).get('opponentTeamId',0) or 0),_SB_DIFFICULTIES[diff_now][0],(out or {}).get('startDateTime'),len(((out or {}).get('squad') or {}).get('players',[])),[(x.get('itemState'),x.get('resourceId')) for x in (((out or {}).get('squad') or {}).get('actives',[])) if isinstance(x,dict)],[(x.get('itemState'),x.get('resourceId')) for x in opp_actives if isinstance(x,dict)],body[:2048])
            elif squad_id==_DRAFT_SQUAD_ID or _draft_stage(st)=='READY_FOR_MATCH':
                if self.command=='PUT' and bool(st.get('matchInProgress')):
                    out=_draft_match_ready(doc)
                else:
                    out=_draft_match_start(doc)
                    if out is None:
                        out={'reason':'DRAFT_NOT_READY_FOR_MATCH','success':False}
                    log.warning('DRAFT MATCH START squadId=%s stage=%s start=%s players=%d actives=%d activeStates=%s opponentTeamId=%s body=%r',squad_id,_draft_stage(),out.get('startDateTime') if isinstance(out,dict) else None,len((out.get('squad',{}) or {}).get('players',[]) if isinstance(out,dict) else []),len((out.get('squad',{}) or {}).get('actives',[]) if isinstance(out,dict) else []),[x.get('itemState') for x in ((out.get('squad',{}) or {}).get('actives',[]) if isinstance(out,dict) else []) if isinstance(x,dict)],out.get('opponentTeamId') if isinstance(out,dict) else None,body[:2048])
            else:
                out=_normal_match_start(doc)
                log.warning('OFFLINE MATCH %s squadId=%s start=%s body=%r',self.command,squad_id,out.get('startDateTime'),body[:2048])
        elif low == '/ut/game/fifa18/match/reset':
            st=_draft_get_state()
            if st.get('matchInProgress'):
                st['matchInProgress']=False;_draft_save(st)
            sb=_sb_load()
            if sb.get('matchInProgress') or int(sb.get('selectedOpponentId',0) or 0)>0:
                sb['matchInProgress']=False;sb['selectedOpponentId']=0;_sb_save(sb)
            out={'reset':True}
        elif low in ('/ut/game/fifa18/usermassinfo','/ut/game/fifa18/user/massinfo'):
            out=_user_mass_info(query=query)
        elif low == '/ut/game/fifa18/user':
            out=_user_refresh_payload(query=query)
        elif low == '/ut/game/fifa18/user/club':
            if self.command in ('PUT','POST'):
                try:doc=json.loads(body.decode('utf-8','replace') or '{}')
                except Exception:doc={}
                if isinstance(doc,dict):
                    name=str(doc.get('clubName',doc.get('name',_club_name())) or _club_name()).strip()[:32]
                    abbr=str(doc.get('clubAbbr',doc.get('abbr',_club_abbr())) or _club_abbr()).strip()[:3]
                    if name:_meta_set('clubName',name)
                    if abbr:_meta_set('clubAbbr',abbr)
                log.warning('LOCAL SAVE profile clubName=%s clubAbbr=%s',_club_name(),_club_abbr())
            out={'clubId':CLUB_ID,'clubName':_club_name(),'clubAbbr':_club_abbr(),'credits':_credits(),'success':True}
        elif low == '/ut/game/fifa18/userdata':
            out={'onlineELORating':0,'onlineRatedUser':False,'accountResetCount':0}

        # --- First-club / onboarding probes -------------------------------
        elif low.startswith('/ut/game/fifa18/clientdata/'):
            key=path.rsplit('/',1)[-1]
            if self.command in ('PUT','POST'):
                try:doc=json.loads(body.decode('utf-8','replace') or '{}')
                except Exception:doc={}
                if not isinstance(doc,dict):doc={'entries':[]}
                stored=doc
                if str(key).lower()=='onboarding':
                    entries=doc.get('entries',[]) if isinstance(doc,dict) else []
                    completed=any(int(x.get('key',-1))==0 and int(x.get('value',0))>=2 for x in entries if isinstance(x,dict))
                    if completed:
                        _meta_set('onboardingComplete',1)
                        stored=_normalise_completed_onboarding(doc)
                _client_set(key,stored)
                out=dict(doc);out['success']=True
                log.warning('CLIENTDATA WRITE key=%s received=%s stored=%s',key,doc.get('entries'),stored.get('entries') if isinstance(stored,dict) else None)
            else:
                out=_client_get(key)
        elif low == '/ut/game/fifa18/onboarding/kits':
            if self.command == 'GET':
                out=_onboarding_kits_payload()
                log.warning('ONBOARDING KITS GET home=%d away=%d homeIds=%s awayIds=%s',
                            len(HOME_KIT_ITEMS),len(AWAY_KIT_ITEMS),
                            [x['resourceId'] for x in HOME_KIT_ITEMS],
                            [x['resourceId'] for x in AWAY_KIT_ITEMS])
            else:
                try: doc=json.loads(body.decode('utf-8','replace') or '{}')
                except Exception: doc={}
                home_ref=doc.get('homeKitId',doc.get('homekitid',0)) if isinstance(doc,dict) else 0
                away_ref=doc.get('awayKitId',doc.get('awaykitid',0)) if isinstance(doc,dict) else 0
                h=_find_kit(home_ref,home=True); a=_find_kit(away_ref,home=False)
                # Persist the compact 32-bit identifiers the retail POST itself uses.
                ONBOARDING_SELECTION['homeKitId']=int(h.get('resourceId',0))
                ONBOARDING_SELECTION['awayKitId']=int(a.get('resourceId',0))
                _meta_set('homeKitId',ONBOARDING_SELECTION['homeKitId']);_meta_set('awayKitId',ONBOARDING_SELECTION['awayKitId'])
                h['pile']=7; h['itemState']='activeHomeKit'
                a['pile']=7; a['itemState']='activeAwayKit'
                _save_item(h);_save_item(a)
                out={
                    'homeKitId':int(h['resourceId']),'awayKitId':int(a['resourceId']),
                    'homeItemData':h,'awayItemData':a,'itemData':[h,a],
                    'actives':[h,a],'success':True,
                }
                log.warning('ONBOARDING KITS POST requestedHome=%s requestedAway=%s selectedHome=%s/%s selectedAway=%s/%s body=%r',
                            home_ref,away_ref,h.get('teamid'),h.get('resourceId'),a.get('teamid'),a.get('resourceId'),body[:2048])
        elif (low in ('/ut/game/fifa18/onboarding/badge','/ut/game/fifa18/onboarding/badges')
              or low.startswith('/ut/game/fifa18/onboarding/badge/')
              or low.startswith('/ut/game/fifa18/onboarding/badges/')):
            # Retail FIFA 18 uses the plural /onboarding/badges endpoint.  v0.8.0
            # accidentally pre-wired only the singular alias, so the real GET returned
            # {}, the UI rendered nine `undefined` shields, and selecting one crashed.
            # Keep both spellings for compatibility, but treat /badges as canonical.
            if self.command == 'GET':
                out=_onboarding_badges_payload()
                log.warning('ONBOARDING BADGES GET path=%s count=%d resourceIds=%s',path,len(BADGE_ITEMS),[x['resourceId'] for x in BADGE_ITEMS])
            else:
                try: doc=json.loads(body.decode('utf-8','replace') or '{}')
                except Exception: doc={}
                ref=0
                if isinstance(doc,dict):
                    ref=doc.get('badgeId',doc.get('badgeDBid',doc.get('badgeAssetId',0)))
                # Some client variants encode the selected ID as a trailing path
                # segment rather than in the JSON body; accept that shape too.
                if not ref:
                    m=re.search(r'/(\d+)$',low)
                    if m: ref=m.group(1)
                try: ref=int(ref or 0)
                except Exception: ref=0
                b=next((dict(x) for x in BADGE_ITEMS if ref in (int(x.get('id',0)),int(x.get('resourceId',0)),int(x.get('definitionId',0)),int(x.get('teamid',0)),int(x.get('assetId',0)))),dict(BADGE_ITEMS[0]))
                ONBOARDING_SELECTION['badgeId']=int(b.get('resourceId',0))
                _meta_set('badgeId',ONBOARDING_SELECTION['badgeId'])
                b['pile']=7; b['itemState']='activeBadge'
                _save_item(b)
                out={'badgeId':int(b['resourceId']),'badgeAssetId':int(b.get('teamid',0)),'badgeDBid':int(b['resourceId']),'itemData':[b],'items':[b],'activeBadge':b,'actives':[b],'success':True}
                log.warning('ONBOARDING BADGES SUBMIT method=%s path=%s requested=%s selected=%s/%s body=%r',self.command,path,ref,b.get('teamid'),b.get('resourceId'),body[:2048])
        elif low == '/ut/game/fifa18/onboarding':
            out=_onboarding_payload()
            log.warning('ONBOARDING %s responseCandidates=%s query=%s body=%r',self.command,[x['assetId'] for x in STARTER_ITEMS[:5]],query,body[:2048])
        elif low == '/ut/game/fifa18/loan/players':
            out=_loan_players_payload()
            log.warning('LOAN PLAYERS %s count=%s assets=%s query=%s',self.command,len(LOAN_ITEMS),[x['assetId'] for x in LOAN_ITEMS],query)
        elif re.match(r'^/ut/game/fifa18/loan/player/\d+$', low):
            try:
                requested=int(low.rsplit('/',1)[-1])
            except Exception:
                requested=0
            chosen=next((dict(x) for x in LOAN_ITEMS if int(x.get('assetId',0))==requested or int(x.get('resourceId',0))==requested or int(x.get('id',0))==requested), dict(LOAN_ITEMS[0]))
            out={'itemData':[chosen],'items':[chosen],'loanPlayer':chosen,'success':True}
            log.warning('LOAN SIGN %s requested=%s chosenAsset=%s chosenItem=%s body=%r',self.command,requested,chosen.get('assetId'),chosen.get('id'),body[:2048])
        elif low == '/ut/game/fifa18/clubuser':
            out=_club_user_payload()
        elif low == '/ut/game/fifa18/club':
            out=_club_page(query)
        elif low in ('/ut/game/fifa18/purchased/items','/ut/game/fifa18/purchased'):
            if self.command == 'POST':
                try:doc=json.loads(body.decode('utf-8','replace') or '{}')
                except Exception:doc={}
                log.warning('PACK PURCHASE REQUEST body=%r',body[:4096])
                out=_purchase_pack(doc, query=query)
            else:
                out=_purchased_payload(query=query)
        elif low in ('/ut/game/fifa18/storagepile', '/ut/game/fifa18/storage'):
            out=_storage_pile_payload(query=query)
        elif low in ('/ut/game/fifa18/squad','/ut/game/fifa18/squad/active'):
            if self.command in ('PUT','POST'):
                try:
                    doc=json.loads(body.decode('utf-8','replace') or '{}')
                    if not isinstance(doc,dict):raise ValueError('squad payload must be an object')
                except Exception as exc:
                    log.warning('LOCAL SAVE squad rejected path=%s error=%s',low,exc)
                    out=_active_squad(query=query);out['saveError']='invalid squad payload'
                else:out=_save_squad(doc, query=query)
            else:out=_active_squad(query=query)
        elif re.match(r'^/ut/game/fifa18/squad/\d+$',low):
            sid=int(low.rsplit('/',1)[-1])
            if sid in (_DRAFT_SQUAD_ID, 900002):
                draft_m = 'ONLINE' if sid == 900002 else 'SINGLE_PLAYER'
                if self.command in ('PUT','POST'):
                    try:doc=json.loads(body.decode('utf-8','replace') or '{}')
                    except Exception:doc={}
                    out=_draft_apply_squad_layout(doc, mode=draft_m)
                else:
                    out=_draft_state_squad(mode=draft_m)
                    log.warning('DRAFT SQUAD GET isolated id=%d mode=%s formation=%s populated=%d normalSquadUntouched=True',
                                sid,draft_m,out.get('formation'),sum(1 for x in out.get('players',[]) if isinstance(x,dict) and isinstance(x.get('itemData'),dict)))
            elif self.command in ('PUT','POST'):
                try:
                    doc=json.loads(body.decode('utf-8','replace') or '{}')
                    if not isinstance(doc,dict):raise ValueError('squad payload must be an object')
                except Exception as exc:
                    log.warning('LOCAL SAVE squad id=%s rejected error=%s',sid,exc)
                    out=_active_squad(sid, query=query);out['saveError']='invalid squad payload'
                else:
                    doc.setdefault('id',sid)
                    out=_save_squad(doc,sid_hint=sid, query=query)
            else:out=_active_squad(sid, query=query)
        elif low == '/ut/game/fifa18/squad/list':
            out=_squad_list_payload()
        elif low == '/ut/game/fifa18/defid':
            # Definition search must expose every card variation for a footballer,
            # not only the NIF/base card. This is what lets the native player
            # picker show IF/TOTY/TOTS/etc. alongside the base definition.
            wanted=0
            for key in ('defId','defid','assetId','assetid','maskedDefId','maskeddefid'):
                vals=query.get(key)
                if vals:
                    try:wanted=int(vals[0])
                    except Exception:wanted=0
                    break
            defs=_all_player_defs()
            found=[]
            for d in defs:
                aid=int(d.get('assetId',0) or 0)
                rid=int(d.get('resourceId',d.get('definitionId',aid)) or aid)
                if not wanted or wanted in (aid,rid):found.append(d)
            try:limit=max(1,min(100,int((query.get('count') or [len(found) or 1])[0])))
            except Exception:limit=min(100,len(found))
            rows=[]
            for d in found[:limit]:
                rid=int(d.get('resourceId',d.get('definitionId',d.get('assetId',0))) or 0)
                iid=760000000000+(rid%10000000000)
                rows.append(_definition_item(d,pile=0,rare=None if _is_special_def(d) else (int(d.get('rating',0))>=75),item_id=iid))
            out={'itemData':rows,'items':rows,'count':len(rows),'total':len(found),'endOfList':len(rows)>=len(found)}
            log.warning('DEFID RESPONSE requested=%s count=%d total=%d dbCount=%d specials=%d resources=%s',wanted,len(rows),len(found),len(defs),sum(1 for x in rows if int(x.get('rareflag',0) or 0)>1),[x.get('resourceId') for x in rows[:12]])
        elif low == '/ut/delete/game/fifa18/item' and self.command == 'POST':
            try:doc=json.loads(body.decode('utf-8','replace') or '{}')
            except Exception:doc={}
            ids=doc.get('itemId',[]) if isinstance(doc,dict) else []
            if not isinstance(ids,list):ids=[ids]
            out=_quicksell_items(ids)
        elif re.match(r'^/ut/(?:delete/)?game/fifa18/item/\d+$',low) and self.command == 'DELETE':
            try:iid=int(low.rsplit('/',1)[-1])
            except Exception:iid=0
            out=_quicksell_items([iid])
        elif low.startswith('/ut/game/fifa18/item/resource'):
            if self.command in ('POST','PUT'):
                try:doc=json.loads(body.decode('utf-8','replace') or '{}')
                except Exception:doc={}
                m=re.search(r'/ut/game/fifa18/item/resource(?:/(\d+))?$',low)
                rid=int(m.group(1)) if (m and m.group(1)) else int((query.get('resourceId') or [0])[0] or 0)
                out=_apply_consumable(doc,resource_id=rid)
                log.warning('CONSUMABLE APPLY BY RES rid=%s method=%s acks=%s',rid,self.command,out.get('itemData',[]))
            else:
                out={'itemData':[dict(x) for x in _db_items()],'success':True}
        elif low.startswith('/ut/game/fifa18/item'):
            if self.command in ('PUT','POST'):
                try:doc=json.loads(body.decode('utf-8','replace') or '{}')
                except Exception:doc={}

                is_apply=False
                if isinstance(doc,dict):
                    if 'apply' in doc or any('applyTo' in x for x in doc.get('itemData',[]) if isinstance(x,dict)):
                        is_apply=True
                elif isinstance(doc,list):
                    if any('applyTo' in x for x in doc if isinstance(x,dict)):
                        is_apply=True

                if is_apply:
                    out=_apply_consumable(doc)
                    log.warning('CONSUMABLE APPLY ITEM acks=%s',out.get('itemData',[]))
                else:
                    sku_mode = str((query.get('skuMode') or [''])[0] or '').upper()
                    out,saved,candidates=_move_items(doc, sku_mode=sku_mode)
                    log.warning('ITEM MOVE ACK requested=%d saved=%d acks=%s pendingNow=%d ownedPlayers=%d',
                                len(candidates),len(saved),out.get('itemData',[]),len(_pending_items()),len(_owned_players()))
            else:out={'itemData':[],'success':True}


        # --- FUT Draft (Online & Single Player) --------------------------------------
        elif low in ('/ut/game/fifa18/squad/mode/draft/state','/ut/game/fifa18/draft/state'):
            mode = _resolve_draft_mode(query=query)
            out = _draft_wire_state(mode)
            row = out[0] if isinstance(out,list) and out else {}
            wire_squad = row.get('squad') if isinstance(row.get('squad'),dict) else {}
            wire_managers = wire_squad.get('manager') if isinstance(wire_squad.get('manager'),list) else []
            log.warning('DRAFT STATE AURORA-CONTRACT method=%s mode=%s squadState=%s managerRows=%d slotVariant=%s bytes=%d query=%s payload=%s',self.command,mode,row.get('squadState'),len(wire_managers),_draft_slot_variant() if _draft_mode_name(mode)=='SINGLE_PLAYER' else 'n/a',len(_json_bytes(out)),query,_json_bytes(out)[:2048])
        elif re.match(r'^/ut/game/fifa18/purchase/mode/(\d+)/draft$',low):
            mode_id=int(re.search(r'/purchase/mode/(\d+)/draft$',low).group(1))
            try:doc=json.loads(body.decode('utf-8','replace') or '{}')
            except Exception:doc={}
            sku_mode = (query.get('skuMode') or [doc.get('skuMode','')])[0].upper()
            before=_credits();out=_draft_purchase(mode_id,doc,sku_mode=sku_mode)
            log.warning('DRAFT PURCHASE NATIVE modeId=%d method=%s credits=%d->%d body=%r payload=%s',mode_id,self.command,before,_credits(),body[:1024],_json_bytes(out)[:1024])
        elif low in ('/ut/game/fifa18/squad/mode/draft/choose/difficulty','/ut/game/fifa18/draft/choose/difficulty') or re.match(r'^/ut/game/fifa18/squad/mode/(\d+)/draft/choose/difficulty$',low):
            try:doc=json.loads(body.decode('utf-8','replace') or '{}')
            except Exception:doc={}
            m_match=re.search(r'/squad/mode/(\d+)/draft/choose/difficulty$',low)
            mode_id=int(m_match.group(1)) if m_match else 1
            mode = _resolve_draft_mode(mode_id, query=query, doc=doc)
            out=_draft_choose_difficulty(doc,mode);log.warning('DRAFT CHOOSE DIFFICULTY method=%s body=%r stage=%s',self.command,body[:1024],_draft_stage(mode=mode))
        elif re.match(r'^/ut/game/fifa18/squad/mode/(\d+)/draft/select$',low):
            mode_id=int(re.search(r'/squad/mode/(\d+)/draft/select$',low).group(1))
            if self.command!='POST':
                self.send(405,_json_bytes({'success':False,'error':'post_required'}));return
            try:doc=json.loads(body.decode('utf-8','replace'))
            except (ValueError,UnicodeError):doc=None
            mode=_resolve_draft_mode(mode_id,query=query,doc=doc if isinstance(doc,dict) else {})
            out=_draft_select_action(doc,mode)
            log.warning('DRAFT SELECT BRIDGE mode=%s success=%s error=%s',mode,out.get('success'),out.get('error'))
        elif re.match(r'^/ut/game/fifa18/squad/mode/(\d+)/draft/choices/(difficulty|formation|captain|player|manager)$',low):
            m=re.search(r'/squad/mode/(\d+)/draft/choices/(difficulty|formation|captain|player|manager)$',low);mode_id=int(m.group(1));kind=m.group(2)
            try:doc=json.loads(body.decode('utf-8','replace') or '{}')
            except Exception:doc={}
            mode = _resolve_draft_mode(mode_id, query=query, doc=doc)
            out=_draft_payload_choices(kind,query,doc,mode);log.warning('DRAFT NATIVE CHOICES mode=%d (%s) kind=%s method=%s count=%d body=%r',mode_id,mode,kind,self.command,len(out.get('choices',[])) if isinstance(out,dict) else 0,body[:1024])
        elif re.match(r'^/ut/game/fifa18/squad/mode/draft/choices/(difficulty|formation|captain|player|manager)$',low):
            kind=re.search(r'/draft/choices/(difficulty|formation|captain|player|manager)$',low).group(1)
            try:doc=json.loads(body.decode('utf-8','replace') or '{}')
            except Exception:doc={}
            mode = _resolve_draft_mode(query=query, doc=doc)
            out=_draft_payload_choices(kind,query,doc,mode);log.warning('DRAFT CHOICES alias kind=%s method=%s count=%d body=%r',kind,self.command,len(out.get('choices',[])) if isinstance(out,dict) else 0,body[:1024])
        elif re.match(r'^/ut/game/fifa18/squad/mode/(\d+)/draft/choose$',low):
            mode_id=int(re.search(r'/squad/mode/(\d+)/draft/choose$',low).group(1))
            try:doc=json.loads(body.decode('utf-8','replace') or '{}')
            except Exception:doc={}
            mode = _resolve_draft_mode(mode_id, query=query, doc=doc)
            picks_before=len(_draft_get_state(mode).get('pickedBySlot') or {})
            out=_draft_choose(doc,mode);log.warning('DRAFT NATIVE CHOOSE mode=%d (%s) method=%s body=%r stage=%s',mode_id,mode,self.command,body[:1024],_draft_stage(mode=mode))
            # The choose answer stays the retail `{}`.  FIFA 18 discards this
            # body (proven repeatedly on 2026-09-15), so the next empty slot is
            # advertised only where the client actually reads it: stateParam1 =
            # PLAYER/<slot> on the state GET issued when the Draft screen opens.
            if _draft_mode_name(mode) in ('SINGLE_PLAYER', 'WORLD_CUP_SINGLE_PLAYER') and _draft_stage(mode=mode)=='PLAYER_DRAFT':
                log.warning('DRAFT CHOOSE next empty slot=%s',_draft_wire_state(mode)[0].get('stateParam2'))
        elif re.match(r'^/ut/game/fifa18/squad/mode/(\d+)/draft/autocomplete$',low):
            mode_id=int(re.search(r'/squad/mode/(\d+)/draft/autocomplete$',low).group(1))
            mode = _resolve_draft_mode(mode_id, query=query)
            before=len(_draft_get_state(mode).get('pickedBySlot') or {})
            out=_draft_complete(mode)
            after=len(_draft_get_state(mode).get('pickedBySlot') or {})
            log.warning('DRAFT NATIVE AUTOCOMPLETE mode=%d (%s) method=%s players=%d->%d stage=%s',mode_id,mode,self.command,before,after,_draft_stage(mode=mode))
        elif re.match(r'^/ut/game/fifa18/squad/mode/(\d+)/draft$',low):
            mode_id=int(re.search(r'/squad/mode/(\d+)/draft$',low).group(1))
            mode = _resolve_draft_mode(mode_id, query=query)
            out=_draft_wire_state(mode);log.warning('DRAFT NATIVE BASE mode=%d (%s) method=%s',mode_id,mode,self.command)
        elif re.match(r'^/ut/game/fifa18/(?:squad/mode/)?(?:draft/)?(?:choices|choose)/(formation|captain|player|manager|difficulty)$',low):
            kind=re.search(r'/(?:choices|choose)/(formation|captain|player|manager|difficulty)$',low).group(1)
            try:doc=json.loads(body.decode('utf-8','replace') or '{}')
            except Exception:doc={}
            mode = _resolve_draft_mode(query=query, doc=doc)
            out=_draft_payload_choices(kind,query,doc,mode);log.warning('DRAFT COMPAT CHOICES kind=%s method=%s count=%d body=%r',kind,self.command,len(out.get('choices',[])) if isinstance(out,dict) else 0,body[:1024])
        elif low in ('/ut/game/fifa18/squad/mode/draft/squad','/ut/game/fifa18/draft/squad'):
            mode = _resolve_draft_mode(query=query)
            out=_draft_squad_payload(mode)
        elif low in ('/ut/game/fifa18/squad/mode/draft/match','/ut/game/fifa18/draft/match','/ut/game/fifa18/squad/mode/draft/result'):
            try:doc=json.loads(body.decode('utf-8','replace') or '{}')
            except Exception:doc={}
            mode = _resolve_draft_mode(query=query, doc=doc)
            out=_draft_match_result(doc,mode=mode);log.warning('DRAFT MATCH mode=%s result=%s wins=%s losses=%s',mode,out.get('won'),out.get('wins'),out.get('losses'))
        elif (low.endswith('/stats') and '/draft' in low) or low=='/ut/game/fifa18/draft/mode/1/stats':
            mode = _resolve_draft_mode(query=query)
            out=_draft_stats_payload(_draft_get_state(mode));log.warning('DRAFT STATS RETAIL mode=%s payload=%s',mode,_json_bytes(out)[:1024])
        elif '/grant/award' in low and '/ut/game/fifa18/' in low:
            mode = _resolve_draft_mode(query=query)
            out=_draft_claim_award(mode=mode);log.warning('DRAFT GRANT AWARD AURORA-CONTRACT method=%s mode=%s path=%s body=%r payload=%s',self.command,mode,path,body[:1024],_json_bytes(out)[:1024])
        # --- Squad Building Challenges ----------------------------------
        elif low == '/ut/game/fifa18/sbs/sets':
            favq=str((query.get('taggedByUser') or query.get('favourite') or query.get('favorite') or [''])[0]).lower();out=_sbc_sets_payload(favq in ('1','true','yes'));log.warning('SBC SETS visible=%d catalog=%d categories=%d bytes=%d',len(out.get('sets',[])),int(out.get('catalogCount',len(out.get('sets',[]))) or 0),len(out.get('categories',[])),len(_json_bytes(out)))
        elif re.match(r'^/ut/game/fifa18/sbs/setid/(\d+)/challenges$',low):
            ident=int(re.search(r'/setid/(\d+)/challenges$',low).group(1));st,ch=_sbc_find_set_or_challenge(ident)
            native=[]
            if st:
                for raw_ch in st.get('challenges',[]):
                    row=_sbc_native_challenge(st,raw_ch)
                    if row:native.append(row)
            out={'challenges':native}
            log.warning('SBC CHALLENGES NATIVE setId=%s returned=%d payload=%s',ident,len(native),_json_bytes(out)[:4096])
        elif re.match(r'^/ut/game/fifa18/sbs/(?:set|sets|setid)/(\d+)/(?:rewards|awards)$',low):
            ident=int(re.search(r'/(\d+)/(?:rewards|awards)$',low).group(1));out=_sbc_set_rewards_payload(ident)
            log.warning('SBC SET REWARDS NATIVE setId=%s items=%d payload=%s',ident,len(out.get('itemData',[])),_json_bytes(out)[:4096])
        elif low in ('/ut/game/fifa18/sbs/rewards','/ut/game/fifa18/sbs/sets/rewards','/ut/game/fifa18/sbs/sets/awards'):
            try:ident=int((query.get('setId') or query.get('setid') or query.get('id') or [0])[0] or 0)
            except Exception:ident=0
            out=_sbc_set_rewards_payload(ident)
            log.warning('SBC SET REWARDS RECOVERY setId=%s resolved=%s items=%d payload=%s',ident,out.get('setId'),len(out.get('itemData',[])),_json_bytes(out)[:4096])
        elif re.match(r'^/ut/game/fifa18/sbs/(?:set|sets|setid)/(\d+)/(?:tag|tagged|favorite|favourite)$',low):
            ident=int(re.search(r'/(\d+)/(?:tag|tagged|favorite|favourite)$',low).group(1))
            if self.command=='DELETE':flag=False
            elif self.command in ('POST','PUT'):
                try:doc=json.loads(body.decode('utf-8','replace') or '{}')
                except Exception:doc={}
                val=doc.get('taggedByUser',doc.get('isFavourite',doc.get('favorite',doc.get('favourite',doc.get('tagged',True))))) if isinstance(doc,dict) else True
                flag=bool(val) if not isinstance(val,str) else val.lower() not in ('0','false','off','no')
            else:flag=ident in _sbc_favourite_ids()
            _sbc_set_favourite(ident,flag);st,ch=_sbc_find_set_or_challenge(ident);out=_sbc_set_index_summary(st) if st else {'id':ident,'setId':ident,'taggedByUser':flag,'isFavourite':flag,'tagged':1 if flag else 0}
        elif low in ('/ut/game/fifa18/sbs/favourites','/ut/game/fifa18/sbs/favorites','/ut/game/fifa18/sbs/sets/tagged','/ut/game/fifa18/sbs/sets/favourites','/ut/game/fifa18/sbs/sets/favorites'):
            out=_sbc_sets_payload(True);log.warning('SBC FAVOURITES returned=%d',len(out.get('sets',[])))
        elif re.match(r'^/ut/game/fifa18/sbs/(?:set|sets)/(\d+)$',low):
            ident=int(low.rsplit('/',1)[-1]);st,ch=_sbc_find_set_or_challenge(ident)
            if self.command in ('POST','PUT') and st:
                try:doc=json.loads(body.decode('utf-8','replace') or '{}')
                except Exception:doc={}
                if isinstance(doc,dict) and any(k in doc for k in ('taggedByUser','isFavourite','favorite','favourite','tagged')):
                    val=doc.get('taggedByUser',doc.get('isFavourite',doc.get('favorite',doc.get('favourite',doc.get('tagged',True)))))
                    flag=bool(val) if not isinstance(val,str) else val.lower() not in ('0','false','off','no');_sbc_set_favourite(ident,flag);st,ch=_sbc_find_set_or_challenge(ident)
            out=_sbc_set_index_summary(st) if st else {'sets':[]}
        elif re.match(r'^/ut/game/fifa18/sbs/(?:challenge|challenges)/(\d+)/squad$',low):
            ident=int(re.search(r'/(\d+)/squad$',low).group(1))
            if self.command in ('POST','PUT'):
                try:doc=json.loads(body.decode('utf-8','replace') or '{}')
                except Exception:doc={}
                out=_sbc_squad_update(ident,doc)
            else:out=_sbc_squad_payload(ident)
            log.warning('SBC SQUAD NATIVE challenge=%s method=%s payload=%s',ident,self.command,_json_bytes(out)[:4096])
        elif re.match(r'^/ut/game/fifa18/sbs/(?:challenge|challenges)/(\d+)/(?:submit|complete)$',low):
            ident=int(re.search(r'/(\d+)/(?:submit|complete)$',low).group(1))
            try:doc=json.loads(body.decode('utf-8','replace') or '{}')
            except Exception:doc={}
            # If FIFA submits with no body after saving the squad, reconstruct
            # the item IDs from the persisted challenge squad.
            if not _extract_item_ids(doc):
                saved=_sbc_squad_load(ident) or {}
                doc={'players':[{'index':i,'itemData':{'id':iid}} for i,iid in enumerate(saved.get('slots',[])) if iid],
                     'rating':int(saved.get('rating',0) or 0),'chemistry':int(saved.get('chemistry',0) or 0)}
            out=_sbc_submit(ident,doc);log.warning('SBC COMPLETE NATIVE challenge=%s method=%s payload=%s',ident,self.command,_json_bytes(out)[:4096])
        elif re.match(r'^/ut/game/fifa18/sbs/(?:challenge|challenges)/(\d+)$',low):
            ident=int(re.search(r'/(\d+)$',low).group(1));st,ch=_sbc_find_set_or_challenge(ident)
            if ch is None and st and st.get('challenges'):ch=st['challenges'][0]
            if self.command in ('POST','PUT'):
                try:doc=json.loads(body.decode('utf-8','replace') or '{}')
                except Exception:doc={}
                ids=_extract_item_ids(doc)
                has_players=isinstance(doc,dict) and (isinstance(doc.get('players'),list) or (isinstance(doc.get('squad'),dict) and isinstance(doc['squad'].get('players'),list)))
                empty_action=(not body.strip()) or (not ids and not has_players)
                if self.command=='POST' and empty_action:
                    # Trace-proven FIFA18 OPEN contract: empty POST.
                    out=_sbc_squad_payload(ident)
                    log.warning('SBC OPEN RETAIL challenge=%s isolatedSquad=True players=%d activeSquadReused=False',ident,len(out.get('squad',{}).get('players',[])))
                elif self.command=='PUT' and empty_action:
                    # Trace-proven FIFA18 SUBMIT contract: after PUT /squad, the
                    # client sends PUT /sbs/challenge/<id> with body {}. Rebuild
                    # the submitted XI from the persisted SBC-only squad.
                    saved=_sbc_squad_load(ident) or {}
                    submit_doc={'players':[{'index':i,'itemData':{'id':iid}} for i,iid in enumerate(saved.get('slots',[])) if int(iid or 0)>0],
                                'rating':int(saved.get('rating',0) or 0),'chemistry':int(saved.get('chemistry',0) or 0)}
                    out=_sbc_submit(ident,submit_doc)
                    log.warning('SBC SUBMIT RETAIL-ENDPOINT challenge=%s method=PUT savedPlayers=%d payload=%s',ident,len(submit_doc['players']),_json_bytes(out)[:4096])
                else:
                    out=_sbc_squad_update(ident,doc)
            else:
                out=_sbc_squad_payload(ident)
            log.warning('SBC CHALLENGE RETAIL challenge=%s method=%s payload=%s',ident,self.command,_json_bytes(out)[:4096])

        # --- Transfer Market AI -----------------------------------------
        elif low == '/ut/game/fifa18/marketdata/pricelimits':
            try:defid=int((query.get('defId') or query.get('defid') or [0])[0] or 0)
            except Exception:defid=0
            out=_market_price_limits_payload(defid)
            log.warning('MARKET PRICE LIMITS defId=%d min=%d max=%d',defid,out[0]['minPrice'],out[0]['maxPrice'])
        elif low in ('/ut/game/fifa18/transfermarket/count','/ut/game/fifa18/transfermarket/livetransfers','/ut/game/fifa18/auctionhouse/count'):
            live=_live_transfer_count();out={'count':live,'total':live,'liveTransfers':live,'liveTransferCount':live,'transferMarketCount':live,'auctionCount':live,'numLiveAuctions':live}
        elif low == '/ut/game/fifa18/transfermarket':
            out=_transfer_market(query)
        elif low == '/ut/game/fifa18/tradepile' and self.command=='GET':
            out=_tradepile_payload()
        elif low == '/ut/game/fifa18/watchlist':
            rows=[dict(x) for x in _MARKET_TARGETS.values()];out=_auction_response(rows,count=len(rows),endOfList=True)
        elif low == '/ut/game/fifa18/auctionhouse':
            if self.command in ('POST','PUT'):
                try:doc=json.loads(body.decode('utf-8','replace') or '{}')
                except Exception:doc={}
                out=_list_market_item(doc)
            else:out={'auctionInfo':[],'success':True}
        elif low == '/ut/game/fifa18/auctionhouse/relist':
            out=_relist_market()
        elif re.match(r'^/ut/game/fifa18/trade/(\d+)/bid$',low):
            tid=int(re.search(r'/trade/(\d+)/bid$',low).group(1))
            try:doc=json.loads(body.decode('utf-8','replace') or '{}')
            except Exception:doc={}
            out=_bid_market_trade(tid,doc)
        elif re.match(r'^/ut/game/fifa18/tradepile/(\d+)$',low):
            tid=int(low.rsplit('/',1)[-1]);out=_clear_trade(tid) if self.command=='DELETE' else {'auctionInfo':[],'tradeId':tid,'success':True}
        elif re.match(r'^/ut/game/fifa18/trade/(\d+)$',low):
            tid=int(low.rsplit('/',1)[-1])
            if self.command=='DELETE':out=_clear_trade(tid)
            elif self.command=='PUT':
                try:doc=json.loads(body.decode('utf-8','replace') or '{}')
                except Exception:doc={}
                out=_bid_market_trade(tid,doc) if any(k in doc for k in ('bid','bidAmount','buyNowPrice')) else {'auctionInfo':[],'tradeId':tid,'success':True}
            else:out={'auctionInfo':[],'tradeId':tid,'success':True}
        elif low == '/ut/game/fifa18/tradepile' and self.command=='DELETE':
            out=_clear_finished_trades()
        elif low == '/ut/game/fifa18/trade/status':
            _market_ai_tick_user_auctions();rows=[dict(x) for x in _db_auctions()]+[dict(x) for x in _MARKET_TARGETS.values()];out=_auction_response(rows,count=len(rows))

        elif low in ('/ut/game/fifa18/squad/mode/draft/stats','/ut/game/fifa18/squad/mode/draft/summary','/ut/game/fifa18/squad/mode/draft/history'):
            out=_draft_stats_payload()
        elif low in ('/ut/game/fifa18/squad/mode/draft/award','/ut/game/fifa18/squad/mode/draft/awards','/ut/game/fifa18/squad/mode/draft/prize',
                       '/ut/game/fifa18/draft/award','/ut/game/fifa18/draft/awards','/ut/game/fifa18/draft/prize'):
            out=_draft_prize_payload(claim=self.command in ('POST','PUT'))
        elif low in ('/ut/game/fifa18/squad/mode/draft/token','/ut/game/fifa18/squad/mode/draft/entry'):
            out={'mode':'SINGLE_PLAYER','draftToken':0,'draftTokens':0,'entryFee':15000,'entryCost':15000,'credits':_credits(),'coins':_credits(),'canEnter':_credits()>=15000}

        # --- Store / hub / lightweight club stats -------------------------
        elif low == '/ut/game/fifa18/user/credits':
            # The My Packs state decides whether recovered packs exist from this
            # response before it asks for purchasegroup/all. v0.8.9.21 omitted
            # unopenedPacks here, so the client cached a zero-pack category even
            # though the subsequent catalogue correctly carried quantity=1.
            unopened=_unopened_packs_payload()
            out={'credits':_credits(),'coins':_credits(),'points':0,
                 'currencies':[{'name':'COINS','funds':_credits(),'finalFunds':_credits()},{'name':'POINTS','funds':0,'finalFunds':0}],
                 'unopenedPacks':{'preOrderPacks':0,'recoveredPacks':int(unopened.get('recoveredPacks',0) or 0)},
                 'unopenedPackCount':int(unopened.get('count',0) or 0)}
        elif low in ('/ut/game/fifa18/store/packquantities','/ut/game/fifa18/store/packquantity'):
            out=_pack_quantities_payload()
            log.warning('STORE PACK QUANTITIES unopened=%s payload=%s',_owned_pack_counts(),_json_bytes(out)[:2048])
        elif low in ('/ut/game/fifa18/store','/ut/game/fifa18/store/purchasegroup',
                     '/ut/game/fifa18/store/purchasegroup/all','/ut/game/fifa18/store/purchasegroup/cardpack',
                     '/ut/game/fifa18/store/purchasegroup/mypacks'):
            out=_store_payload()
            log.warning('STORE CATALOG nativeMyPacks=True packs=%s coins=%s unopened=%s playerDbCache=%s',[x['name'] for x in STORE_PACKS],_credits(),_owned_pack_counts(),PLAYER_CACHE.exists())
        elif low.startswith('/ut/v2/game/fifa18/store/transaction'):
            out={'state':'NOTRANSACTION','transactionId':0,'coins':_credits(),'points':0}
        elif low == '/ut/game/fifa18/season/list':
            out=_season_list_payload()
            log.warning('SEASONS LIST nativeMinimal=True count=%d division10WireId=%s',len(out['seasons']),next((x.get('divisionId') for x in out['seasons'] if x.get('id')==10),None))
        elif low in ('/ut/game/fifa18/season/user','/ut/game/fifa18/season'):
            out=_season_user_payload()
            log.warning('SEASONS USER nativeMinimal=True offlineDivision=%s seasonId=%s wireDivisionId=%s round=%s active=%s state=%s points=%s WDL=%s-%s-%s opaqueData=%s',
                        out.get('offlineDivision'),out.get('seasonId'),out.get('divisionId'),out.get('round'),out.get('active'),out.get('seasonState'),out.get('points'),out.get('wins'),out.get('draws'),out.get('losses'),bool(out.get('data')))
        elif low == '/ut/game/fifa18/season/user/history':
            st=_season_state();out={**_season_progress_fields(st),'seasonWins':st['wins'],'seasonCompleted':int(st['completed']),'seasonCoins':int(st['prizeCoins'])}
        elif re.match(r'^/fut/items/pc/(90000\d+)\.json$',low):
            rid=int(re.search(r'/pc/(\d+)\.json$',low).group(1));out=_season_trophy_payload(rid)
            log.warning('SEASONS TROPHY ITEM resource=%d',rid)
        elif low == '/ut/game/fifa18/club/stats/staff':
            staff=[dict(x) for x in _db_items() if int(x.get('pile',7) or 0)==7 and str(x.get('itemType','')).lower()=='manager']
            n=len(staff);entries=[{'contextId':0,'contextValue':0,'type':'staff','typeValue':n},{'contextId':0,'contextValue':0,'type':'staffEmployed','typeValue':n},{'contextId':0,'contextValue':0,'type':'FUT_MYCLUB_STAFF_EMPLOYED','typeValue':n}]
            out={'staff':staff,'itemData':staff,'managerCount':n,'count':n,'total':n,'stat':entries,'entries':entries}
        elif low in ('/ut/game/fifa18/club/stats','/ut/game/fifa18/clubstats','/ut/game/fifa18/club/statistics',
                     '/ut/game/fifa18/club/stats/year','/ut/game/fifa18/club/stats/players','/ut/game/fifa18/club/stats/year/2018'):
            out=_club_year_stats_payload()
            log.warning('CLUB STATS NATIVE players=%d total=%d contexts=%s nativePlayersStat=%s route=%s',out['players'],out['total'],[(0,0),(0,2018),(2,2018),(6,0)],next((x.get('typeValue') for x in out['stat'] if x.get('type')=='FUT_MYCLUB_PLAYERS_EMPLOYED' and x.get('contextId')==0 and x.get('contextValue')==0),None),low)
        elif low == '/ut/game/fifa18/club/stats/newcards':
            out={'count':len(_pending_items()),'newCards':len(_pending_items()),'itemData':[]}
        elif low == '/ut/game/fifa18/club/stats/consumables':
            cr=[x for x in _db_items() if int(x.get('pile',7) or 0)==7 and str(x.get('itemType','')).lower() in ('development','training','consumable')]
            def ncat(*names):return sum(1 for x in cr if str(x.get('category','')).lower() in names)
            n=len(cr);entries=[{'contextId':6,'contextValue':0,'type':'consumables','typeValue':n}]
            out={'consumables':n,'playerContracts':ncat('contract'),'fitness':ncat('fitness'),'healing':ncat('healing'),'training':ncat('training','gktraining'),'position':ncat('positioning'),'playStyle':ncat('chemistrystyle','gkchemistrystyle'),'managerLeague':ncat('managerleague'),'count':n,'total':n,'stat':entries,'entries':entries}
        elif low.startswith('/ut/game/fifa18/club/consumables/'):
            out=_club_consumables_payload(low.rsplit('/',1)[-1])
        elif low == '/ut/game/fifa18/hub':
            live=_live_transfer_count();rw,rd,rl=_record_triplet()
            _hub_players=len(_owned_players())
            out={'credits':_credits(),'unopenedPacks':_unopened_packs_payload(),'unassignedPileSize':len(_pending_items()),'ownedPlayers':_hub_players,'playerCount':_hub_players,'players':_hub_players,'playersEmployed':_hub_players,'clubPlayers':_hub_players,'totalPlayers':_hub_players,'numPlayers':_hub_players,'FUT_MYCLUB_PLAYERS_EMPLOYED':_hub_players,'clubCount':_club_counts()['total'],
                 'won':rw,'draw':rd,'loss':rl,'gamesWon':rw,'gamesDraw':rd,'gamesLost':rl,'gamesPlayed':rw+rd+rl,
                 'record':{'won':rw,'draw':rd,'loss':rl},
                 'liveTransfers':live,'liveTransferCount':live,'transferMarketCount':live,'auctionCount':live,'numLiveAuctions':live,
                 'messages':[],'items':[],'news':[],
                 'squadBattlesEnabled':True,'squadBattleEnabled':True,'squadBattle':_sb_payload(_sb_generate(False)),'squadBattlePoints':_sb_rank()['battlePoints']}
        elif low == '/ut/game/fifa18/leaderboards/options':
            _sbopt={'id':'sqbt','type':'SQUAD_BATTLE','name':'Squad Battles','enabled':True,'scope':'WEEKLY'}
            out={'options':[_sbopt],'leaderboardOptions':[_sbopt],'items':[_sbopt]}
        elif low == '/ut/game/fifa18/user/dynamicobjectives':
            # Return fully-completed starter objectives so the Objectives
            # catalogue never shows a broken "undefined" screen with locked
            # items.  The four retail FIFA 18 starter groups are Welcome to
            # FUT, Create Your Squad, Play a Match, Buy a Player.  Each
            # objective within a group has progress >= target (i.e. complete).
            _obj_id = 8001
            def _mk_obj(label, desc, target=1):
                nonlocal _obj_id
                o = {
                    'id': _obj_id,
                    'name': label,
                    'description': desc,
                    'progress': target,
                    'target': target,
                    'completed': True,
                    'claimed': True,
                    'rewardsClaimed': True,
                    'rewards': [],
                }
                _obj_id += 1
                return o
            def _mk_group(gid, name, desc, objectives):
                return {
                    'id': gid,
                    'name': name,
                    'description': desc,
                    'objectives': objectives,
                    'completed': True,
                    'claimed': True,
                    'rewardsClaimed': True,
                    'totalObjectives': len(objectives),
                    'completedObjectives': len(objectives),
                    'rewards': [],
                    'endOfList': True,
                }
            g1 = _mk_group(1001, 'Welcome to FUT',
                'Complete the Welcome to FUT objectives', [
                    _mk_obj('Welcome to FUT', 'Open the FUT menu'),
                    _mk_obj('View Squad', 'View your squad'),
                    _mk_obj('Open Starter Pack', 'Open your starter squad pack'),
                    _mk_obj('View Objectives', 'View the objectives screen'),
                    _mk_obj('Check Store', 'Visit the FUT Store'),
                    _mk_obj('View Transfer Market', 'View the Transfer Market'),
                ])
            g2 = _mk_group(1002, 'Create Your Squad',
                'Open your starter squad pack', [
                    _mk_obj('Open Starter Pack', 'Open your starter squad pack'),
                    _mk_obj('Apply Contract', 'Apply a contract to a player'),
                    _mk_obj('Apply Chemistry Style', 'Apply a chemistry style'),
                    _mk_obj('Apply Fitness', 'Apply a fitness card'),
                    _mk_obj('Apply Position Change', 'Apply a position change'),
                    _mk_obj('Squad Chemistry', 'Reach 50 squad chemistry', 50),
                    _mk_obj('Add Manager', 'Add a manager to your club'),
                ])
            g3 = _mk_group(1003, 'Play a Match',
                'Play your first FUT match', [
                    _mk_obj('Play Squad Battles', 'Play a Squad Battles match'),
                    _mk_obj('Play Single Player', 'Play a single player season match'),
                    _mk_obj('Play Draft', 'Enter a FUT Draft'),
                    _mk_obj('Score a Goal', 'Score a goal in FUT'),
                    _mk_obj('Win a Match', 'Win a match in FUT'),
                    _mk_obj('Complete a Match', 'Complete any FUT match'),
                ])
            g4 = _mk_group(1004, 'Buy a Player',
                'Purchase a player from the Transfer Market', [
                    _mk_obj('Search Transfer Market', 'Search for a player'),
                    _mk_obj('Place a Bid', 'Place a bid on a player'),
                    _mk_obj('Buy Now', 'Buy a player with Buy Now'),
                    _mk_obj('List a Player', 'List a player for transfer'),
                    _mk_obj('Quick Sell', 'Quick sell an item'),
                    _mk_obj('Redeem Coins', 'Collect coins from a sale'),
                    _mk_obj('Compare Price', 'Compare price of a player'),
                ])
            all_objectives = g1['objectives'] + g2['objectives'] + g3['objectives'] + g4['objectives']
            out = {
                'objectives': all_objectives,
                'groups': [g1, g2, g3, g4],
                'items': all_objectives,
                'totalObjectives': len(all_objectives),
                'completedObjectives': len(all_objectives),
                'endOfList': True,
            }

        # --- Static bootstrap content ------------------------------------
        elif self.command=='GET' and low.startswith('/fut/items/images/trophies/') and low.endswith('.big'):
            payload=_season_trophy_big()
            log.warning('SEASONS TROPHY BIG path=%s bytes=%d',path,len(payload))
            self.send(200,payload,'application/octet-stream',headers=headers);return
        elif ('storepackdescriptions' in low and low.endswith('.xml')) or low == '/fut/packs/loc/storepackdescriptions.en_us.xml':
            payload=_store_loc_xml()
            log.warning('STORE LOC XML bytes=%d packs=%d path=%s',len(payload),len(STORE_PACKS),path)
            self.send(200,payload,'application/xml; charset=utf-8',headers=headers);return
        elif low == '/fut/loc/pc/leaderboards.eng_us.xml':
            ctype='application/xml; charset=utf-8'
            payload=b'<?xml version="1.0" encoding="UTF-8"?><leaderboards></leaderboards>'
            self.send(200,payload,ctype,headers=headers);return
        elif re.match(r'^/fut/playerheads/g4/single/p(\d+)\.dds$',low):
            # Special cards request versioned action-shot DDS files. v0.8.9.1
            # accidentally returned JSON {} with HTTP 200, so the decoder never
            # received either an image or a real miss. v0.8.9.2 caches authentic
            # archive art when reachable, converts the preserved PNG to DDS when
            # necessary, then falls back to the player's normal portrait. If all
            # archive sources are offline, 404 lets FIFA use its installed head.
            m=re.search(r'/p(\d+)\.dds$',low);rid=int(m.group(1)) if m else 0
            packaged=ROOT/'data'/'playerheads'/f'p{rid}.dds'
            payload=None
            if packaged.exists() and packaged.is_file() and packaged.stat().st_size>=128:
                try:
                    candidate=packaged.read_bytes();payload=candidate if candidate[:4]==b'DDS ' else None
                except Exception:payload=None
            d=_definition_by_resource(rid);asset=int((d or {}).get('assetId',0) or 0);st=str((d or {}).get('specialType','') or '')
            if payload is None:payload=_fetch_player_head_dds(rid,asset)
            if payload:
                log.warning('PLAYER HEAD DDS served resource=%d asset=%d special=%s bytes=%d',rid,asset,st,len(payload))
                self.send(200,payload,'image/vnd-ms.dds',headers=headers);return
            log.warning('PLAYER HEAD DDS archive miss resource=%d asset=%d special=%s -> HTTP 404 native base-head fallback',rid,asset,st)
            self.send(404,b'','application/octet-stream',headers=headers);return
        elif low == '/fut/playerheads/g4/fut2dheads.big':
            # Valid empty BIGF archive header, rather than JSON masquerading as
            # an EA archive. Player head art can be added once item rendering is
            # confirmed; card identity does not depend on this file.
            ctype='application/octet-stream'
            payload=b'BIGF'+(16).to_bytes(4,'little')+(0).to_bytes(4,'big')+(16).to_bytes(4,'big')
            self.send(200,payload,ctype,headers=headers);return
        elif low in ('/fut/packs/packopening/packopeningsetting.json','/data/store/packopeningsetting.json'):
            # The retail DLL names data/store/packopeningsetting.json.  Some PC
            # paths use the compiled local asset, but serve a conservative HTTP
            # equivalent too if this build requests it dynamically.
            out={
                'enabled':True,
                'RevealItemTiming':{
                    'RevealStaggerId':1,'RevealSpeedId':1,
                    'RevealOverallTiming':0.35,'RevealPositionTiming':0.60,
                    'RevealCrestTiming':0.90,'RevealNationTiming':1.15,
                    'RevealPhotoTiming':1.45,'RevealNameTiming':1.75,'RevealAttributesTiming':2.10,
                },
            }
            log.warning('PACK OPENING RETAIL SETTING compatibility route served')
        elif low == '/fut/packs/packopening/packopeningconfig.json':
            # v0.8.9.27.9 controlled A/B: serve the preserved exact FIFA 18
            # packopeningconfig.json while keeping the entire proven v0.8.9.27.7
            # /purchased/items wire untouched.  FIFA18 itself only requests this
            # CDN file; its packopeningsetting lives in the shipped VFS at
            # data/store/packopeningsetting.json.  Do not fabricate a second CDN
            # file and do not perform any memory writes in this build.
            exact=_packopening_exact_bytes('packopeningconfig.json')
            if exact is not None:
                log.warning('PACK OPENING EXACT FIFA18 CONFIG served stableWire=True bytes=%d sha256=%s',len(exact),hashlib.sha256(exact).hexdigest())
                self.send(200,exact,'application/json',headers=headers);return
            anim={
                'enabled':True,'packOpeningEnabled':True,'animationEnabled':True,
                'usePackOpeningAnimation':True,'skipAllowed':True,'skipEnabled':True,
                'minimumAnimationTime':2500,'minimumRevealTime':2500,
                'revealDelay':250,'walkoutEnabled':True,
                'walkoutMinRating':86,'walkoutMinimumRating':86,'walkoutRatingThreshold':86,
                'walkout':{'enabled':True,'minRating':86,'minimumRating':86,'ratingThreshold':86},
            }
            out=dict(anim);out['config']=dict(anim)
            log.warning('PACK OPENING exact FIFA18 config missing; boot-safe fallback served')
        elif low.endswith('.png') and low.startswith('/fut/'):
            # Never return JSON to an image decoder.  A tiny valid transparent PNG
            # is preferable to the former {} body while authentic hub art is still
            # being reconstructed.
            import base64
            payload=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=')
            self.send(200,payload,'image/png',headers=headers);return

        else:
            out={}
            log.warning('FUT18 UNKNOWN %s %s query=%s body=%r',self.command,path,query,body[:2048])

        payload=_json_bytes(out)
        # Response summaries are especially useful now that FUT itself is
        # reachable. Avoid printing the full starter item list repeatedly.
        if low == '/pow/store/game/fifa18/catalog/0/item/list':
            n=HTTP_ROUTE_COUNTS.get((self.command,low),0)
            if n<=5 or n in (10,25,50,100):
                log.warning('FUTHTTP RESPONSE %s %s count=%d body=%s',self.command,path,n,payload[:512])
        elif low in ('/ut/game/fifa18/usermassinfo','/ut/game/fifa18/user/massinfo'):
            sq=out.get('squad',{}) if isinstance(out,dict) else {}
            rows=sq.get('players',[]) if isinstance(sq,dict) else []
            xi=sum(1 for x in rows[:11] if isinstance(x,dict) and isinstance(x.get('itemData'),dict))
            log.warning('USERMASSINFO RESPONSE bytes=%d squadId=%s slots=%d populatedXI=%d firstAssets=%s topKeys=%s',len(payload),sq.get('id') if isinstance(sq,dict) else None,len(rows),xi,[x.get('itemData',{}).get('assetId') for x in rows[:11] if isinstance(x,dict)],list(out.keys()) if isinstance(out,dict) else [])
        elif low in ('/ut/game/fifa18/onboarding','/ut/game/fifa18/onboarding/kits','/ut/game/fifa18/onboarding/badge','/ut/game/fifa18/onboarding/badges','/ut/game/fifa18/loan/players','/ut/game/fifa18/clubuser','/ut/game/fifa18/club','/ut/game/fifa18/squad','/ut/game/fifa18/squad/active','/ut/game/fifa18/purchased/items') or low.startswith('/ut/game/fifa18/loan/player/'):
            log.warning('FUTHTTP RESPONSE %s %s bytes=%d keys=%s',self.command,path,len(payload),list(out.keys()) if isinstance(out,dict) else type(out).__name__)
        self.send(getattr(self,'_draft_status_override',200),payload,ctype,headers=headers)
    do_GET=go;do_POST=go;do_PUT=go;do_DELETE=go;do_OPTIONS=go

def _sbc_reward_protected_item_ids():
    """Item ids referenced by a completed SBC reward record.

    These ids are ownership/history, not catalogue seed rows.  They must never
    be removed by source-aware catalogue cleanup even when they were created by
    an older build that did not stamp acquisitionSource yet.
    """
    protected=set()
    with _DB_LOCK,_db_connect() as con:
        rows=con.execute("SELECT value FROM meta WHERE key LIKE 'sbcSetReward:%'").fetchall()
    for row in rows:
        try:rec=json.loads(row['value'] or '{}')
        except Exception:continue
        if not isinstance(rec,dict):continue
        for x in rec.get('itemIds',[]) or []:
            try:
                iid=int(x)
                if iid>0:protected.add(iid)
            except Exception:pass
    return protected


def _sbc_repair_missing_set_reward_items_once():
    """One-time v36.12 repair for reward instances deleted by v36.11 cleanup.

    v36.11 could delete a legitimately-earned restricted special if the item was
    created by an older reward path without sourceEligibility/acquisitionSource,
    even though sbcSetReward:<setId> still proved ownership.  Recreate only those
    missing, recorded ids, exactly once.  This avoids turning the repair into a
    general 'resurrect discarded rewards' feature on later launches.
    """
    repair_key='sbcRewardIntegrityRepair'
    if _meta_get(repair_key,'')=='0.8.9.36.12':return 0
    item_map=_db_item_map();repaired=0;details=[];unresolved=[]
    with _DB_LOCK,_db_connect() as con:
        meta_rows=con.execute("SELECT key,value FROM meta WHERE key LIKE 'sbcSetReward:%'").fetchall()
    for row in meta_rows:
        try:
            sid=int(str(row['key']).split(':',1)[1]);rec=json.loads(row['value'] or '{}')
        except Exception:continue
        if not isinstance(rec,dict):continue
        ids=[]
        for x in rec.get('itemIds',[]) or []:
            try:
                iid=int(x)
                if iid>0:ids.append(iid)
            except Exception:pass
        missing=[iid for iid in ids if iid not in item_map]
        if not missing:continue
        st,_=_sbc_find_set_or_challenge(sid)
        if not st:
            unresolved.extend((sid,iid) for iid in missing);continue
        set_name=str(st.get('name','') or '')
        # Flatten the set-level item awards in historical order so each recorded
        # item id is rebuilt from the same reward definition it originally used.
        award_defs=[]
        for award in list(st.get('grantAwards',[]) or st.get('awards',[]) or []):
            if not isinstance(award,dict) or str(award.get('type','')).lower()!='item':continue
            for _ in range(max(1,int(award.get('count',1) or 1))):award_defs.append(award)
        if not award_defs:
            unresolved.extend((sid,iid) for iid in missing);continue
        for pos,iid in enumerate(ids):
            if iid not in missing:continue
            award=award_defs[min(pos,len(award_defs)-1)]
            raw=str(award.get('historicalReward',award.get('name','')) or '')
            d=_sbc_find_reward_player(raw,set_name)
            if not d:
                unresolved.append((sid,iid));continue
            item=_definition_item(d,pile=6,item_id=iid)
            item.update({'untradeable':True,'tradeable':False,'acquisitionSource':'SBC_REWARD',
                         'sourceEligibility':'SBC_REWARD','itemState':'free','newItem':True,'pile':6})
            _save_item(item);item_map[iid]=item;repaired+=1
            details.append((sid,iid,int(item.get('resourceId',0) or 0)))
    if not unresolved:_meta_set(repair_key,'0.8.9.36.12')
    log.warning('SBC REWARD INTEGRITY repair restored=%d details=%s unresolved=%s markerWritten=%s',repaired,details,unresolved,not unresolved)
    return repaired


def tls_context(cert,key):
    c=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    try:
        c.minimum_version=ssl.TLSVersion.TLSv1
        c.maximum_version=ssl.TLSVersion.TLSv1_2
    except Exception:pass
    try:c.set_ciphers('ALL:@SECLEVEL=0')
    except Exception:
        try:c.set_ciphers('ALL')
        except Exception:pass
    c.load_cert_chain(cert,key);return c

def run():
    ap=argparse.ArgumentParser();ap.add_argument('--cert',default=str(ROOT/'tls/winter15-chain.pem'));ap.add_argument('--key',default=str(ROOT/'tls/winter15.key'));a=ap.parse_args()
    # v1.0.0: publishable offline release. Fresh clubs start with 23 bronze players and 0 coins; full catalog stays available through packs/market/concept search.
    # v0.8.9.32 added native club-stat entries, exact paged special-card discovery and manager renderer identity repair.
    # v0.8.9.27.9: stable v0.8.9.27.7 pack wire retained; exact FIFA18 packopeningconfig is served with no memory writer.
    defs=_load_player_defs(False)
    _migrate_verified_specials()
    _repair_stored_player_identities()
    _festival_identity_migration(phase='startup')
    _refresh_owned_base_player_metadata(defs)
    _sbc_repair_missing_set_reward_items_once()
    _cleanup_exact_player_resource_duplicates()
    _migrate_exact_managers()
    _ensure_catalog_nonplayers_in_club()
    _ensure_contract_health()
    _migrate_world_cup_items()
    _ensure_world_cup_players_in_club(defs)
    _draft_sync_occupancy_file(_draft_get_state('SINGLE_PLAYER'), 'SINGLE_PLAYER')
    servers=[]
    r=LoggingTLSServer(('127.0.0.1',42230),RedirectHandler,tls_context(a.cert,a.key));r.label='REDIR42230';servers.append(r)
    b=ReuseTCP(('127.0.0.1',10051),BlazeHandler);servers.append(b)
    e=ThreadingHTTPServer(('127.0.0.1',42232),EASWHandler);e.label='EASW42232';servers.append(e)
    f=ThreadingHTTPServer(('127.0.0.1',8099),FutHandler);f.label='FUT8099';servers.append(f)
    f2=ThreadingHTTPServer(('127.0.0.1',8199),FutHandler);f2.label='FUT8199';servers.append(f2)
    cc=_club_counts()
    log.warning('FIFA 18 LOCAL FUT %s ready',VERSION);log.warning('Persistent club: %s / %d coins / ownedPlayers=%d / managers=%d / consumables=%d / clubTotal=%d / onboardingComplete=%s',_club_name(),_credits(),cc['players'],cc['managers'],cc['consumables'],cc['total'],_meta_get('onboardingComplete','0'));log.warning('FIFA18 Icons: %d cards / %d footballers (club 112658, league 2118, rareflag 12)',len(_icon_player_defs()),len({str(x.get('name','')).lower() for x in _icon_player_defs()}));log.warning('Full FIFA18 player DB: %d base definitions / cache=%s',len(defs),PLAYER_CACHE);log.warning('Redirector TLS 127.0.0.1:42230 (GOS 2015 identity) | Blaze 10051 | EASW 42232 | FUT 8099/8199');log.warning('Network policy: offlineOnly=%s backgroundRefresh=%s',OFFLINE_ONLY,ENABLE_BACKGROUND_REFRESH);log.warning('Log: %s',LOGFILE)
    for s in servers:threading.Thread(target=s.serve_forever,daemon=True).start()
    if ENABLE_BACKGROUND_REFRESH:
        log.warning('BACKGROUND REFRESH enabled by FIFA18_LOCAL_BACKGROUND_REFRESH=1')
        if len(defs)<17500:
            threading.Thread(target=_background_refresh_full_player_db,daemon=True,name='FIFA18PlayerDbRefresh').start()
        threading.Thread(target=_refresh_full_catalog_background,daemon=True,name='FIFA18FullCatalogRefresh').start()
        threading.Thread(target=_refresh_verified_specials_live_background,daemon=True,name='FIFA18VerifiedSpecialRefresh').start()
    else:
        log.warning('FAST BOOT active: player/archive/special live refresh workers disabled for this run')
    try:
        while True:time.sleep(1)
    except KeyboardInterrupt:pass
    finally:
        for s in servers:s.shutdown();s.server_close()
if __name__=='__main__':run()
