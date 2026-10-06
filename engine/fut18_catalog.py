from __future__ import annotations

import csv
import hashlib
import html
import io
import json
import re
import unicodedata
import urllib.request
from pathlib import Path

ALL_CARDS_URLS=(
    'https://raw.githubusercontent.com/kafagy/fifa-FUT-Data/master/FIFA18.csv',
    'https://cdn.jsdelivr.net/gh/kafagy/fifa-FUT-Data@master/FIFA18.csv',
)
MANAGER_URL='https://fifauteam.com/fifa-18-managers-cards-guide/'

PROMO_RARE_TYPES={
    0:'NONE',1:'RARE',2:'LOCK',3:'TOTW',4:'PURPLE',5:'TOTY',6:'RECORD_BREAKER',7:'GREEN',
    8:'MOTM',9:'FUTTIES_PINK',10:'TEAL',11:'TOTS',12:'ICON',13:'WC',14:'UNICEF',15:'OLD_MOTM',
    16:'FUTTIES',17:'STORYMODE',18:'CHAMPION',19:'CMOTM',20:'IMOTM',21:'OTW',22:'HALLOWEEN',
    23:'MOVEMBER',24:'SBC',25:'SBC_PREMIUM',26:'PROMO_A',27:'PROMO_B',28:'AWARD',30:'FUT_BIRTHDAY',
    31:'UNITED',32:'FUTMAS',33:'RTRC',34:'PTGS',35:'FOF',36:'MARQUEE',37:'CHAMPIONSHIP',
    38:'EUMOTM',39:'TOTT',40:'RRC',41:'RRR',
}

EXACT_MANAGER_HEADS={
    'antonio conte':1067,'alex neil':2629,'diego simeone':5571,'aitor karanka':8213,
    'mauricio pochettino':8885,'sean dyche':51404,'roberto martinez':53951,'roberto martínez':53951,
    'claude puel':167942,'pep guardiola':169894,'josep guardiola':169894,'tony pulis':174609,
    'slaven bilic':183617,'slaven bilić':183617,'roy hodgson':220467,'louis van gaal':220470,
    'eddie howe':232297,'arsene wenger':232298,'arsène wenger':232298,'alan pardew':232300,
    'claudio ranieri':232301,'jurgen klopp':232302,'jürgen klopp':232302,'ronald koeman':232303,
    'mark hughes':232304,'sam allardyce':232305,'francesco guidolin':232306,'quique sanchez flores':232307,
    'quique sánchez flores':232307,'jose mourinho':232425,'josé mourinho':232425,'rafa benitez':233859,
    'rafa benítez':233859,'walter mazzarri':234529,'steve bruce':234530,'david moyes':235732,
    'mike phelan':236408,'carlo ancelotti':237388,'unai emery':237389,'curt onalfo':237870,
    'zinedine zidane':238399,'paul clement':239228,'craig shakespeare':239229,'david wagner':240199,
    'chris hughton':240488,'gennaro gattuso':1041,'patrick vieira':1088,
    'clarence seedorf':1134,'jaap stam':1397,'paul lambert':1409,'ryan giggs':241,
}

# FIFA 18 FUT nation IDs used by the manager-card renderer. Unknown values stay
# at 0 instead of inventing an identity. Manager league is mutable in FUT, but
# the launch-era league is supplied where it is known from the exact manager.
COUNTRY_NATION_IDS={
    'Albania':1,'Argentina':52,'Belgium':7,'Brazil':54,'Croatia':10,'England':14,
    'France':18,'Germany':21,'Italy':27,'Netherlands':34,'Portugal':38,'Spain':45,
    'Turkey':48,'United States':95,'Wales':50,'Scotland':42,
}
EXACT_MANAGER_LEAGUES={
    'diego simeone':53,'mauricio pochettino':13,'antonio conte':13,
    'arsene wenger':13,'zinedine zidane':53,'jurgen klopp':13,
    'pep guardiola':13,'josep guardiola':13,'unai emery':16,'rafa benitez':13,
    'jose mourinho':13,'ronald koeman':13,'sean dyche':13,'eddie howe':13,
    'chris hughton':13,'mark hughes':13,'tony pulis':13,'slaven bilic':13,
    'david wagner':13,'craig shakespeare':13,'paul clement':13,
    'claude puel':13,'alan pardew':13,'claudio ranieri':16,
    'quique sanchez flores':53,'sam allardyce':13,'steve bruce':14,
    'david moyes':13,'carlo ancelotti':19,'curt onalfo':39,
    'roberto martinez':13,'roberto martínez':13,'louis van gaal':13,
    'francesco guidolin':31,'walter mazzarri':31,'gennaro gattuso':31,
    'patrick vieira':39,'clarence seedorf':53,'jaap stam':14,
    'paul lambert':13,'ryan giggs':13,'alex neil':14,'aitor karanka':14,
    'roy hodgson':13,'mike phelan':14,
}

# Offline-safe subset.  A full FUT18 manager table is refreshed from the archived
# FIFAUTeam guide when available; this list means staff support never starts empty.
FALLBACK_MANAGERS=(
    ('Antonio Conte','Italy','Gold Rare'),('Alex Neil','Scotland','Gold Common'),
    ('Diego Simeone','Argentina','Gold Rare'),('Aitor Karanka','Spain','Gold Common'),
    ('Mauricio Pochettino','Argentina','Gold Rare'),('Sean Dyche','England','Gold Rare'),
    ('Roberto Martínez','Spain','Gold Common'),('Claude Puel','France','Gold Rare'),
    ('Pep Guardiola','Spain','Gold Rare'),('Tony Pulis','Wales','Gold Rare'),
    ('Slaven Bilić','Croatia','Gold Rare'),('Roy Hodgson','England','Gold Rare'),
    ('Louis van Gaal','Netherlands','Gold Rare'),('Eddie Howe','England','Gold Rare'),
    ('Arsène Wenger','France','Gold Rare'),('Alan Pardew','England','Gold Rare'),
    ('Claudio Ranieri','Italy','Gold Rare'),('Jürgen Klopp','Germany','Gold Rare'),
    ('Ronald Koeman','Netherlands','Gold Rare'),('Mark Hughes','Wales','Gold Rare'),
    ('Sam Allardyce','England','Gold Rare'),('Francesco Guidolin','Italy','Gold Rare'),
    ('Quique Sánchez Flores','Spain','Gold Rare'),('José Mourinho','Portugal','Gold Rare'),
    ('Rafa Benítez','Spain','Gold Common'),('Walter Mazzarri','Italy','Gold Rare'),
    ('Steve Bruce','England','Gold Rare'),('David Moyes','Scotland','Gold Rare'),
    ('Mike Phelan','England','Gold Common'),('Carlo Ancelotti','Italy','Gold Rare'),
    ('Unai Emery','Spain','Gold Rare'),('Curt Onalfo','United States','Gold Common'),
    ('Zinedine Zidane','France','Gold Rare'),('Paul Clement','England','Gold Rare'),
    ('Craig Shakespeare','England','Gold Rare'),('David Wagner','United States','Gold Rare'),
    ('Chris Hughton','England','Gold Rare'),('Ryan Giggs','Wales','Gold Rare'),
    ('Gennaro Gattuso','Italy','Gold Rare'),('Patrick Vieira','France','Gold Rare'),
    ('Clarence Seedorf','Netherlands','Gold Rare'),('Jaap Stam','Netherlands','Gold Common'),
    ('Paul Lambert','Scotland','Gold Common'),
)

def _norm(s):
    s=unicodedata.normalize('NFKD',str(s or '')).encode('ascii','ignore').decode('ascii').lower()
    return re.sub(r'[^a-z0-9]+',' ',s).strip()

def _ua_request(url,timeout=5.0,max_bytes=12_000_000):
    req=urllib.request.Request(url,headers={'User-Agent':'Mozilla/5.0 FIFA18LocalFUT/1.0.0','Accept':'text/html,text/plain,*/*'})
    with urllib.request.urlopen(req,timeout=timeout) as r:
        return r.read(max_bytes)

def refresh_all_cards_csv(csv_path:Path):
    csv_path=Path(csv_path);csv_path.parent.mkdir(parents=True,exist_ok=True)
    last=None
    for url in ALL_CARDS_URLS:
        try:
            data=_ua_request(url,timeout=6.0,max_bytes=8_000_000)
            if len(data)<200_000 or b'NAME,CLUB,LEAGUE,POSITION,TIER,RATING' not in data[:256]:
                raise ValueError('unexpected FIFA18.csv payload')
            csv_path.write_bytes(data)
            return {'ok':True,'url':url,'bytes':len(data)}
        except Exception as e:last=e
    return {'ok':False,'error':str(last or 'download failed')}

def _base_name_index(base_defs):
    idx={}
    for d in base_defs:
        for k in ('name','displayName','longName','commonName'):
            n=_norm(d.get(k,''))
            if n:idx.setdefault(n,[]).append(d)
    return idx

def _face_from_csv(row):
    vals=[]
    for k in ('PACE','SHOOTING','PASSING','DRIBBLING','DEFENDING','PHYSICAL'):
        try:vals.append(max(1,min(99,int(float(row.get(k,50) or 50)))))
        except Exception:vals.append(50)
    return vals

def build_archive_variations(csv_path:Path,base_defs,exact_special_defs,cache_path:Path|None=None):
    """Build a reference-only historical variation index.

    FIFA18.csv proves that a rating/position variation existed, but it does not
    contain EA's definition/resource ID or promo rarity. v0.8.9.31 therefore
    never manufactures either value. Exact FUT cards enter the live catalogue
    only through the Resource-ID verified special snapshot.
    """
    csv_path=Path(csv_path)
    if not csv_path.exists():return []
    exact_keys=set()
    for d in exact_special_defs:
        try:
            exact_keys.add((_norm(d.get('name','')),int(d.get('rating',0) or 0),str(d.get('position','')).upper()))
        except Exception:pass
    idx=_base_name_index(base_defs);out=[];seen=set()
    try:text=csv_path.read_text(encoding='utf-8-sig',errors='replace')
    except Exception:return []
    for row in csv.DictReader(io.StringIO(text)):
        name=_norm(row.get('NAME',''));candidates=idx.get(name) or []
        if not candidates:continue
        try:rating=int(float(row.get('RATING',0) or 0))
        except Exception:continue
        pos=str(row.get('POSITION','') or '').upper().strip();club=_norm(row.get('CLUB',''))
        base=next((d for d in candidates if club and club==_norm(d.get('clubName',''))),None)
        if base is None:base=max(candidates,key=lambda d:int(d.get('rating',0) or 0))
        aid=int(base.get('assetId',0) or 0)
        if aid<=0:continue
        base_rating=int(base.get('rating',0) or 0);base_pos=str(base.get('position','') or '').upper()
        if (name,rating,pos) in exact_keys:continue
        if rating==base_rating and (not pos or pos==base_pos):continue
        sig=(aid,rating,pos,tuple(_face_from_csv(row)))
        if sig in seen:continue
        seen.add(sig)
        out.append({
            'assetId':aid,'rating':rating,'position':pos or base_pos,
            'preferredPosition':pos or base_pos,'face':_face_from_csv(row),
            'historicalVariation':True,'verifiedFifa18':False,'catalogReferenceOnly':True,
            'source':'kafagy/fifa-FUT-Data FIFA18.csv (historical variation; no exact resourceId)',
            'sourceTier':str(row.get('TIER','') or ''),'sourceClub':str(row.get('CLUB','') or ''),
            'sourceLeague':str(row.get('LEAGUE','') or '')})
    if cache_path:
        p=Path(cache_path);p.parent.mkdir(parents=True,exist_ok=True)
        p.write_text(json.dumps({'schema':2,'source':'kafagy/fifa-FUT-Data FIFA18.csv',
                                 'identityPolicy':'reference-only; no synthetic FUT resource IDs',
                                 'cards':out},ensure_ascii=False,separators=(',',':')),encoding='utf-8')
    return out

def load_archive_variations(cache_path:Path):
    try:
        doc=json.loads(Path(cache_path).read_text(encoding='utf-8'))
        rows=doc.get('cards',[]) if isinstance(doc,dict) else doc
        return [dict(x) for x in rows if isinstance(x,dict)]
    except Exception:return []

def _quality_values(quality):
    q=str(quality or '').lower();rare=1 if 'rare' in q and 'common' not in q else 0
    if 'gold' in q:return (80 if rare else 75,rare)
    if 'silver' in q:return (70 if rare else 65,rare)
    return (60 if rare else 55,rare)

def _manager_head(name,used):
    head=int(EXACT_MANAGER_HEADS.get(_norm(name),0) or 0)
    if head<=0 or head in used:return 0
    used.add(head);return head

def manager_definitions(rows):
    out=[];used=set();seen=set()
    for name,country,quality in rows:
        name=str(name).strip();key=_norm(name)
        if not name or not key or key in seen:continue
        seen.add(key);head=_manager_head(name,used)
        # Never manufacture a manager head/resource ID. A missing exact identity
        # is safer omitted than rendered as a broken placeholder card.
        if head<=0:continue
        rating,rare=_quality_values(quality)
        # v0.8.9.31 used a synthetic 1,000,000+head namespace for FUT manager
        # resource IDs. The retail client then requested a non-existent manager
        # image and rendered NOT FOUND. Keep the verified FIFA 18 manager/head ID
        # as the card identity instead; unlike the old offset this value actually
        # exists in the shipped FIFA 18 manager database.
        resource=head
        country_text=str(country or '').strip()
        country_key=_norm(country_text)
        nation=0
        for country_name,country_id in COUNTRY_NATION_IDS.items():
            nk=_norm(country_name)
            if country_key==nk or country_key.endswith(' '+nk) or country_key.endswith(nk):
                nation=int(country_id);break
        league=int(EXACT_MANAGER_LEAGUES.get(key,0) or 0)
        out.append({'assetId':head,'headId':head,'managerId':head,'definitionId':resource,'resourceId':resource,
                    'resourceGameYear':2018,'itemType':'manager','cardsubtypeid':4,'rating':rating,
                    'rareflag':rare,'rareFlag':rare,'nation':nation,'nationId':nation,'leagueId':league,'managerLeagueId':league,
                    'name':name,'displayName':name,'managerName':name,'nationality':str(country),
                    'quality':str(quality),'verifiedFifa18Manager':True,
                    'source':'FIFAUTeam FIFA 18 Managers Cards Guide + verified FIFA18 manager/head ID identity'})
    return out

def _manager_rows_from_html(raw):
    text=html.unescape(raw.decode('utf-8','replace'))
    # Strip scripts/styles, then reduce table rows to cell text.  The archived guide has
    # a regular NAME / COUNTRY / QUALITY table; only rows whose quality is a FUT tier pass.
    text=re.sub(r'(?is)<(script|style).*?>.*?</\1>',' ',text)
    rows=[]
    for tr in re.findall(r'(?is)<tr[^>]*>(.*?)</tr>',text):
        cells=[]
        for td in re.findall(r'(?is)<t[dh][^>]*>(.*?)</t[dh]>',tr):
            val=re.sub(r'(?is)<[^>]+>',' ',td);val=re.sub(r'\s+',' ',html.unescape(val)).strip()
            cells.append(val)
        if len(cells)>=3 and re.search(r'(?i)\b(gold|silver|bronze)\b',cells[-1]):
            # table may carry an icon/status column; last three semantic cells are name/country/quality.
            quality=cells[-1];country=cells[-2];name=cells[-3]
            if name and not re.search(r'(?i)^name$',name):rows.append((name,country,quality))
    return rows

def refresh_manager_cache(cache_path:Path):
    try:
        raw=_ua_request(MANAGER_URL,timeout=6.0,max_bytes=4_000_000)
        rows=_manager_rows_from_html(raw)
        if len(rows)<100:raise ValueError(f'manager table too small ({len(rows)})')
        defs=manager_definitions(rows)
        Path(cache_path).parent.mkdir(parents=True,exist_ok=True)
        Path(cache_path).write_text(json.dumps({'schema':1,'source':MANAGER_URL,'managers':defs},ensure_ascii=False,separators=(',',':')),encoding='utf-8')
        return defs
    except Exception:return []

def load_manager_definitions(cache_path:Path):
    try:
        p=Path(cache_path)
        if not p.exists() or p.stat().st_size < 100:
            fb=Path(__file__).resolve().parent.parent/'data'/'fifa18-managers-fallback.json'
            if fb.exists():p=fb
        doc=json.loads(p.read_text(encoding='utf-8'))
        rows=doc.get('managers',[]) if isinstance(doc,dict) else []
        if rows:
            structured=[]
            for x in rows:
                if not isinstance(x,dict):continue
                aid=int(x.get('assetId',x.get('headId',0)) or 0)
                if aid>0:
                    nation=int(x.get('nation',x.get('nationId',0)) or 0)
                    league=int(x.get('leagueId',x.get('managerLeagueId',0)) or 0)
                    rating=int(x.get('rating',75) or 75)
                    rare=int(x.get('rareflag',x.get('rareFlag',0)) or 0)
                    name=str(x.get('name',x.get('displayName',x.get('managerName','Manager'))) or 'Manager')
                    structured.append({'assetId':aid,'headId':aid,'managerId':aid,'definitionId':aid,'resourceId':aid,
                                       'resourceGameYear':2018,'itemType':'manager','cardsubtypeid':4,'rating':rating,
                                       'rareflag':rare,'rareFlag':rare,'nation':nation,'nationId':nation,
                                       'leagueId':league,'managerLeagueId':league,'name':name,'displayName':name,
                                       'managerName':name,'nationality':str(x.get('nationality','') or ''),
                                       'quality':str(x.get('quality','Gold Rare' if rare else 'Gold Common') or 'Gold Common'),
                                       'verifiedFifa18Manager':True,'source':str(x.get('source','FIFAUTeam FIFA 18 Managers Cards Guide') or '')})
            if structured:return structured
            raw=[]
            for x in rows:
                if not isinstance(x,dict):continue
                raw.append((str(x.get('name',x.get('managerName','')) or ''),
                            str(x.get('nationality','') or ''),str(x.get('quality','Gold Common') or 'Gold Common')))
            exact=manager_definitions(raw)
            if exact:return exact
    except Exception:pass
    return manager_definitions(FALLBACK_MANAGERS)

def consumable_definitions():
    out=[]
    def add(rid,asset,subtype,rating,rare,category,level,ctype,value=0,amount=99):
        out.append({'assetId':asset,'cardassetid':asset,'definitionId':rid,'resourceId':rid,'resourceGameYear':2018,
                    'itemType':'development' if category in ('Contract','Fitness','Healing') else 'training',
                    'cardsubtypeid':subtype,'rating':rating,'rareflag':rare,'rareFlag':rare,'category':category,
                    'level':level,'consumableType':ctype,'value':value,'amount':amount,'name':f'{level} {category} {ctype}'.strip(),
                    'source':'FUT-Consumables-Resource-IDs catalog'})
    levels=(('Bronze',55),('Silver',70),('Gold',80))
    # Player contracts 5001001-6 and manager contracts 5001007-12, plus legacy all-99 player contract 5001013.
    for base,asset,sub,cat in ((5001001,7,201,'Contract'),(5001007,8,202,'Contract')):
        owner='Player' if asset==7 else 'Manager'
        for i,(lev,rat) in enumerate(levels):add(base+i,asset,sub,rat,0,cat,lev,owner)
        for i,(lev,rat) in enumerate((('Bronze',60),('Silver',70),('Gold',90))):add(base+3+i,asset,sub,rat,1,cat,lev,owner)
    add(5001013,7,201,90,1,'Contract','Gold','Player',99)
    for i,(lev,rat,amt) in enumerate((('Bronze',55,20),('Silver',70,40),('Gold',80,60))):add(5002001+i,10,219,rat,0,'Fitness',lev,'Player',amt)
    for i,(lev,rat,amt) in enumerate((('Bronze',55,10),('Silver',70,20),('Gold',80,30))):add(5002004+i,10,220,rat,1,'Fitness',lev,'Squad',amt)
    heal_groups=((5002007,211,'Head'),(5002010,212,'UpperBody'),(5002013,213,'Arm'),(5002019,215,'Knee'),(5002022,216,'Leg'),(5002025,217,'Foot'))
    for base,sub,part in heal_groups:
        for i,(lev,rat,amt) in enumerate((('Bronze',55,1),('Silver',70,2),('Gold',80,5))):add(base+i,9,sub,rat,0,'Healing',lev,part,amt)
    for i,(lev,rat,amt) in enumerate((('Bronze',60,1),('Silver',74,2),('Gold',85,4))):add(5002028+i,9,218,rat,1,'Healing',lev,'All',amt)
    # GK and player training: 7 attribute groups x 3 qualities.
    for group,ctype in enumerate(('DIV','HAN','KIC','SPD','POS','REF','ALL')):
        sub=51+group;base=5003001+group*3
        for i,(lev,rat,amt) in enumerate((('Bronze',55,5),('Silver',65,10),('Gold',85,15)) if group<6 else (('Bronze',64,3),('Silver',74,6),('Gold',95,10))):add(base+i,3,sub,rat,1 if group==6 else 0,'GKTraining',lev,ctype,amt)
    for group,ctype in enumerate(('PAC','SHO','PAS','DRI','PHY','DEF','ALL')):
        sub=61+group;base=5003022+group*3
        for i,(lev,rat,amt) in enumerate((('Bronze',55,5),('Silver',65,10),('Gold',85,15)) if group<6 else (('Bronze',64,3),('Silver',74,6),('Gold',95,10))):add(base+i,1,sub,rat,1 if group==6 else 0,'Training',lev,ctype,amt)
    pos_types=('LWBLB','LBLWB','RWBRB','RBRWB','LMLW','RMRW','LWLM','RWRM','LWLF','RWRF','LFLW','RFRW','CMCAM','CAMCM','CDMCM','CMCDM','CAMCF','CFCAM','CFST','STCF')
    for i,ctype in enumerate(pos_types):add(5003059+i,34,91+i,95 if i%3 else 90,1,'Positioning','Gold',ctype)
    chem=('Sniper','Finisher','Deadeye','Marksman','Hawk','Artist','Architect','Powerhouse','Maestro','Engine','Sentinel','Guardian','Gladiator','Backbone','Anchor','Hunter','Catalyst','Shadow','Basic')
    for i,ctype in enumerate(chem):add(5003095+i,50,250+i,80,1 if ctype!='Basic' else 0,'ChemistryStyle','Gold',ctype)
    for i,ctype in enumerate(('Wall','Shield','Cat','Glove','GK Basic')):add(5003114+i,51,269+i,80,1 if ctype!='GK Basic' else 0,'GKChemistryStyle','Gold',ctype)
    # Manager league cards. IDs/subtypes are the documented FUT sequence; 5003121/subtype302 is absent.
    for rid in range(5003119,5003159):
        if rid==5003121:continue
        sub=300+(rid-5003119)
        add(rid,32,sub,80,1 if rid in (5003123,5003130,5003141,5003147,5003150) else 0,'ManagerLeague','Gold',f'League {sub}')
    assert len(out)==165,len(out)
    return out
