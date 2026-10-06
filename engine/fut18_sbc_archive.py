#!/usr/bin/env python3
from __future__ import annotations
import argparse, concurrent.futures, hashlib, html as _html, json, os, re, time, unicodedata, urllib.request
from pathlib import Path

VERSION = 8
SOURCE_URLS = {
    'Basic': 'https://fifauteam.com/fifa-18-basic-sbcs/',
    'Advanced': 'https://fifauteam.com/fifa-18-advanced-sbcs/',
    'Upgrades': 'https://fifauteam.com/fifa-18-upgrades-sbcs/',
    'Leagues': 'https://fifauteam.com/fifa-18-league-sbc-rewards/',
    'Marquee Matchups': 'https://fifauteam.com/fifa-18-marquee-matchups/',
    'POTM': 'https://fifauteam.com/fifa-18-premier-league-potm-sbc/',
    'Prime ICONS': 'https://fifauteam.com/fifa-18-throwback-thursdays-prime-icons/',
    'Live': 'https://fifauteam.com/fifa-18-live-squad-building-challenges-rewards/',
}

UPGRADE_NAMES = [
'PREMIUM LIGUE 1 UPGRADE V','LIGUE 1 UPGRADE V','PREMIUM CALCIO A UPGRADE V','CALCIO A UPGRADE V',
'PREMIUM BUNDESLIGA UPGRADE V','BUNDESLIGA UPGRADE V','PREMIUM LALIGA UPGRADE V','LALIGA UPGRADE V',
'PREMIUM PL UPGRADE V','PL UPGRADE V','BASE ICON UPGRADE','86-89 UPGRADE','84-87 UPGRADE','82-85 UPGRADE',
'PREMIUM LALIGA UPGRADE IV','LALIGA UPGRADE IV','PREMIUM BUNDESLIGA UPGRADE IV','BUNDESLIGA UPGRADE IV',
'PREMIUM LIGUE 1 UPGRADE IV','LIGUE 1 UPGRADE IV','PREMIUM CALCIO A UPGRADE IV','CALCIO A UPGRADE IV',
'PREMIUM PL UPGRADE IV','PL UPGRADE IV','CALCIO A UPGRADE III','PREMIUM CALCIO A UPGRADE III',
'BUNDESLIGA UPGRADE III','PREMIUM BUNDESLIGA UPGRADE III','LIGUE 1 UPGRADE III','PREMIUM LIGUE 1 UPGRADE III',
'LALIGA UPGRADE III','PREMIUM LALIGA UPGRADE III','PL UPGRADE III','PREMIUM PL UPGRADE III',
'CALCIO A UPGRADE II','PREMIUM CALCIO A UPGRADE II','BUNDESLIGA UPGRADE II','PREMIUM BUNDESLIGA UPGRADE II',
'LIGUE 1 UPGRADE II','PREMIUM LIGUE 1 UPGRADE II','LALIGA UPGRADE II','PREMIUM LALIGA UPGRADE II',
'PL UPGRADE II','PREMIUM PL UPGRADE II','CALCIO A UPGRADE','PREMIUM CALCIO A UPGRADE',
'BUNDESLIGA UPGRADE','PREMIUM BUNDESLIGA UPGRADE','LIGUE 1 UPGRADE','PREMIUM LIGUE 1 UPGRADE',
'LALIGA UPGRADE','PREMIUM LALIGA UPGRADE','PL UPGRADE','PREMIUM PL UPGRADE',
'GOLD UPGRADE PLUS','GOLD UPGRADE II','SILVER UPGRADE II','BRONZE UPGRADE II','GOLD UPGRADE','SILVER UPGRADE','BRONZE UPGRADE'
]

ICON_NAMES = [
'Pelé','Javier Zanetti','Paolo Maldini','Ronaldo','Ruud Gullit','Alessandro Nesta','Hernán Crespo','Jari Litmanen',
'Diego Maradona','Michael Owen','Laurent Blanc','Michael Laudrup','Patrick Kluivert','Marcel Desailly','Frank Rijkaard',
'Henrik Larsson','Andriy Shevchenko','Edwin van der Sar','Jay-Jay Okocha','Filippo Inzaghi','Lev Yashin','Ronaldinho',
'Lothar Matthäus','Marco van Basten','Alan Shearer','Carles Puyol','Dennis Bergkamp','Patrick Vieira','Deco','Robert Pirès',
'Gheorghe Hagi','Rio Ferdinand','Roberto Carlos','Thierry Henry','Emmanuel Petit','Peter Schmeichel','Luis Hernández',
'Alessandro Del Piero','Marc Overmars','Rui Costa'
]

LEAGUE_NAMES = [
'Pro League','Premier League','LaLiga Santander','Hyundai A-League','Meiji Yasuda J1 League','Bundesliga','Calcio A',
'Major League Soccer','Ligue 1 Conforama','Dawry Jameel','EFL Championship','Eredivisie','Russian League','Liga NOS',
'Liga Bancomer MX','Süper Lig'
]

POTM_NAMES = [
'Wilfried Zaha - April POTM','Mohamed Salah - March POTM','Mohamed Salah - February POTM','Sergio Agüero - January POTM',
'Harry Kane - December POTM','Mohamed Salah - November POTM','Leroy Sané - October POTM','Harry Kane - September POTM',
'Sadio Mané - August POTM'
]

# FIFAUTeam's Live page release index (2017-18). The parser replaces these fallback
# records with the archived requirement/reward blocks when an internet refresh succeeds.
LIVE_NAMES = [
'FESTIVAL OF FUTBALL REPEATABLE III','FESTIVAL OF FUTBALL III','FESTIVAL OF FUTBALL REPEATABLE II','FESTIVAL OF FUTBALL II',
'FESTIVAL OF FUTBALL REPEATABLE I','FESTIVAL OF FUTBALL I','FESTIVAL OF FUTBALL FLASH SBC XIX','FESTIVAL OF FUTBALL FLASH SBC XVIII',
'FESTIVAL OF FUTBALL FLASH SBC XVII','FESTIVAL OF FUTBALL FLASH SBC XVI','FESTIVAL OF FUTBALL FLASH SBC XV B','FESTIVAL OF FUTBALL FLASH SBC XV A',
'FESTIVAL OF FUTBALL FLASH SBC XIII','FESTIVAL OF FUTBALL FLASH SBC XII','FESTIVAL OF FUTBALL FLASH SBC XI','FESTIVAL OF FUTBALL FLASH SBC X',
'FESTIVAL OF FUTBALL FLASH SBC IX','FESTIVAL OF FUTBALL FLASH SBC VIII','FESTIVAL OF FUTBALL FLASH SBC VII','FESTIVAL OF FUTBALL FLASH SBC VI',
'FESTIVAL OF FUTBALL FLASH SBC V','FESTIVAL OF FUTBALL FLASH SBC IV','FESTIVAL OF FUTBALL FLASH SBC III','FESTIVAL OF FUTBALL FLASH SBC II',
'FESTIVAL OF FUTBALL FLASH SBC I','FGS – AMSTERDAM VII','FGS – AMSTERDAM VI','FGS – AMSTERDAM V','FGS – AMSTERDAM IV',
'FGS – AMSTERDAM III','FGS – AMSTERDAM II','FGS – AMSTERDAM I','UNFP POTS','UNFP POTS (LOAN)','UNFP YOUNG POTS',
'UNFP YOUNG POTS (LOAN)','ULTIMATE TOTS','CALCIO A TOTS','LIGUE 1 TOTS','BUNDESLIGA TOTS','LALIGA TOTS','PREMIER LEAGUE TOTS',
'EFL TOTS','COMMUNITY TOTS','ROW 9-1 TOTS PLAYERS','ROW 19-11 TOTS PLAYERS','ROW 29-21 TOTS PLAYERS','ROW 39-31 TOTS PLAYERS',
'ROW 49-41 TOTS PLAYERS','FUT SWAP PLAYER IV','PFA POTY','PFA POTY (LOAN)','PFA YOUNG POTY','PFA YOUNG POTY (LOAN)',
'FUT SWAP PLAYER III','EFL CHAMPIONSHIP POTY','FUT SWAP PLAYER II','FUT SWAP PLAYER I','CARLOS TÉVEZ','ALEXIS SÁNCHEZ - LIVE',
'ULTIMATE PACK','LUKA MODRIC','TIMO WERNER - LIVE','LORENZO INSIGNE','PRIME GOLD PLAYERS PACK','ZLATAN IBRAHIMOVIC - LIVE',
'DIMITRI PAYET','SANTI CAZORLA','KOKE','WENDELL','RARE GOLD PACK','JORDAN HENDERSON','PREMIUM GOLD PACK','KEITA BALDÉ DIAO',
'FUT 17','FUT 16','FUT 15','FUT 14','FUT 13','FUT 12','FUT 11','FUT 10','FUT 09','KAKÁ','KAKÁ (LOAN)','WAYNE ROONEY',
'WAYNE ROONEY (LOAN)','ROBIN VAN PERSIE','ROBIN VAN PERSIE (LOAN)','BASTIAN SCHWEINSTEIGER','BASTIAN SCHWEINSTEIGER (LOAN)',
'YAYA TOURÉ','YAYA TOURÉ (LOAN)','TRADEABLE TOTW UPGRADE - A','INTERNATIONAL COMPETITION - A','WORLD FOOTBALL','GROUP STAGE',
'TRIO OF NATIONAL TRIOS - A','NATIONAL ACADEMY','NATIONAL HEROES','ENGLAND VS SCOTLAND','PATH TO GLORY: SPRING (TRADEABLE)',
'PATH TO GLORY: SPRING (UNTRADEABLE)','PATH TO GLORY: AUTUMN (TRADEABLE)','PATH TO GLORY: AUTUMN (UNTRADEABLE)',
'IMPROVED GOLD UPGRADE','TRIO OF NATIONAL TRIOS - B','HAPPY NEW YEAR','RAINING CATS AND DOGS','GOLD RETRIEVERS','THE TERRIERS',
'THE CANINE FAMILY','LUNAR NEW YEAR','LES DOGUES','HEART OF GOLD','LA JAURÍA','LIONS, TIGERS AND DOGS','BEST IN SHOW','CRAZY EIGHTS',
'IMPROVE GOLD UPGRADE','TOTW 22 UPGRADE','TRADEABLE TOTW UPGRADE - B','WHO LET THE DOGS OUT','LUCAS MOURA','PIERRE-EMERIK AUBAMEYANG',
'ALEXIS SÁNCHEZ - OTW','82+ RATED GUARANTEE','OTW: WINTER PLAYER','OTW: WINTER PLAYER (UNTRADEABLE)','OTW: SUMMER PLAYER',
'OTW: SUMMER PLAYER (UNTRADEABLE)','FGS – BARCELONA III','FGS – BARCELONA II','FGS – BARCELONA I','TOTY NOMINEES XIV','TOTY NOMINEES XIII',
'TOTY NOMINEES XII','TOTY NOMINEES XI','TOTY NOMINEES X','TOTY NOMINEES IX','TOTY NOMINEES VIII','TOTY NOMINEES VII','TOTY NOMINEES VI',
'TOTY NOMINEES V','TOTW 18 UPGRADE','TOTY NOMINEES IV','TOTW 17 UPGRADE','TRADEABLE TOTW UPGRADE - C','TOTY NOMINEES III',
'TOTY NOMINEES II','81+ DOUBLE GUARANTEE - A','TOTY NOMINEES I','LAST DAY OF FUTMAS','DAILY FUTMAS SBC XVI','DAILY FUTMAS SBC XV',
'DAILY FUTMAS SBC XIV','DAILY FUTMAS SBC XIII','81+ DOUBLE GUARANTEE - B','TRADEABLE TOTW UPGRADE - D','TOTW 15 UPGRADE',
'DAILY FUTMAS SBC XII','DAILY FUTMAS SBC XI','TOTW 14 UPGRADE','DAILY FUTMAS SBC X','FUTMAS SBC BUNDLE','DANNY ROSE FUTMAS','FABINHO FUTMAS',
'GIANLUIGI BUFFON FUTMAS','DAILY FUTMAS SBC IX','LAURENT KOSCIELNY FUTMAS','GEORGINIO WIJNALDUM FUTMAS','RADJA NAINGGOLAN FUTMAS',
'DAILY FUTMAS SBC VIII','ANDREA PIRLO','ANDREA PIRLO (LOAN)','JAMIE VARDY FUTMAS','NABY KEITA FUTMAS','ROMAIN ALESSANDRINI FUTMAS',
'DAILY FUTMAS SBC VII','THOMAS MEUNIER FUTMAS','TIMO WERNER FUTMAS','ZLATAN IBRAHIMOVIC FUTMAS','DAILY FUTMAS SBC VI','PAULINHO FUTMAS',
'JESSE LINGARD FUTMAS','AHMED MUSA FUTMAS','DAILY FUTMAS SBC V','JORDI ALBA FUTMAS','VIRGIL VAN DIJK FUTMAS','ZLATKO JUNUZOVIC FUTMAS',
'DAILY FUTMAS SBC IV','ANDER HERRERA FUTMAS','LUIS MURIEL FUTMAS','SOFIANE BOUFAL FUTMAS','DAILY FUTMAS SBC III','DANIELE RUGANI FUTMAS',
'JULIAN DRAXLER FUTMAS','HENRIKH MKHITARYAN FUTMAS','DAILY FUTMAS SBC II','DOMENICO BERARDI FUTMAS','KALIDOU KOULIBALY FUTMAS',
'THOMAS LEMAR FUTMAS','DAILY FUTMAS SBC I','CÉSAR AZPILICUETA FUTMAS','SERGE GNABRY FUTMAS','MICHAIL ANTONIO FUTMAS','MLS MVP',
'CYBER MONDAY FLASH SBC X','CYBER MONDAY FLASH SBC IX','CYBER MONDAY FLASH SBC VIII','CYBER MONDAY FLASH SBC VII','ELITESERIEN POTY',
'CYBER MONDAY FLASH SBC VI','CYBER MONDAY FLASH SBC V','CYBER MONDAY FLASH SBC IV','CYBER MONDAY FLASH SBC III','CYBER MONDAY FLASH PLAYER SBC I',
'CYBER MONDAY FLASH SBC II','CYBER MONDAY FLASH SBC I','TOTW 10 UPGRADE','TRADEABLE TOTW UPGRADE - E','BLACK FRIDAY FLASH SBC XII',
'BLACK FRIDAY FLASH SBC XI','BLACK FRIDAY FLASH SBC X','BLACK FRIDAY FLASH SBC IX','BLACK FRIDAY FLASH SBC VIII','BLACK FRIDAY FLASH PLAYER SBC II',
'BLACK FRIDAY FLASH SBC VII','BLACK FRIDAY FLASH SBC VI','BLACK FRIDAY FLASH SBC V','BLACK FRIDAY FLASH SBC IV','BLACK FRIDAY FLASH PLAYER SBC I',
'BLACK FRIDAY FLASH SBC III','BLACK FRIDAY FLASH SBC II','BLACK FRIDAY FLASH SBC I','PFAI PLAYER OF THE YEAR','NATION PAIRS','A GAME OF NATIONS',
'ARGENTINA V BRAZIL','INTERNATIONAL COMPETITION - B','GERMANY V FRANCE','NATIONAL SQUAD','JONATHAN VIERA RAMOS','MARIO GÓMEZ','JERMAIN DEFOE',
'EMILIANO RIGONI','ALLSVENSKANS MVP','THE RETURN OF ULTIMATE SCREAM','SILENT AS THE GRAVE','COVEN OF WITCHES','ULTIMATE SCREAM',
'LOST IN THE LABYRINTH','TERROR FROM THE DEEP','THE WEREWOLF’S CURSE','SPIDER’S WEB','DRACULA’S 11','FC KOREA','FC JAPAN','FC AUSTRALIA',
'HOME LEAGUE HEROES','NEYMAR','KEITA BALDÉ DIAO - OTW','ROMELU LUKAKU','DOUGLAS COSTA','ALEXANDRE LACAZETTE','KICK OFF'
]

BASIC = [
{'name':'Let’s Keep Going','groupRewards':['Premium Loan Player Pack'],'challenges':[
 {'name':'Brick Slots','requirements':['Players from different Leagues: Exactly 2','Players from different Nations: Exactly 2','Min. Team Chemistry: 43','Number of Players in the Squad: 2'],'rewards':['Bronze Pack']},
 {'name':'The Correct Position','requirements':['Players from different Leagues: Exactly 3','Players from different Nations: Exactly 3','Min. Team Chemistry: 12','Number of Players in the Squad: 3'],'rewards':['Premium Bronze Pack']},
 {'name':'Perfect Link','requirements':['Min. 2 Silver Players','Max 2 Players from the same Club','Max 2 Players from the same Nation','Min. Team Chemistry: 40','Number of Players in the Squad: 5'],'rewards':['500 coins','2 Silver Players']},
 {'name':'Loyal Lads','requirements':['Players from different Leagues: Exactly 3','Players from different Nations: Exactly 1','Min. Team Chemistry: 30','Number of Players in the Squad: 3'],'rewards':['2 Silver Players']} ]},
{'name':'League and Nation Basics','groupRewards':['All Players Pack'],'challenges':[
 {'name':'One League Attack','requirements':['Players from different Leagues: Exactly 1','Players from different Nations: Exactly 5','Min. Team Chemistry: 45','Number of Players in the Squad: 5'],'rewards':['Premium Silver Players Pack']},
 {'name':'One Nation Midfield','requirements':['Min. 3 Silver Players','Players from different Leagues: Exactly 5','Players from different Nations: Exactly 1','Min. Team Chemistry: 45','Number of Players in the Squad: 5'],'rewards':['Premium Gold Pack']},
 {'name':'Multi League & Nation','requirements':['Clubs: Exactly 6','Max 2 Players from the same League','Max 2 Players from the same Nation','Min. Team Rating: 55','Min. Team Chemistry: 48','Number of Players in the Squad: 6'],'rewards':['Premium Gold Pack']} ]},
{'name':'Let’s Get Started','groupRewards':['Two Rare Gold Players Pack'],'challenges':[
 {'name':'First Exchange','requirements':['Exactly Bronze Players','Number of Players in the Squad: 1'],'rewards':['Bronze Reward Pack']},
 {'name':'The Second Step','requirements':['Min. Team Chemistry: 12','Number of Players in the Squad: 3'],'rewards':['Bronze Pack']},
 {'name':'The Third Step','requirements':['Players from different Nations: Exactly 3','Min. Team Chemistry: 17','Number of Players in the Squad: 4'],'rewards':['Bronze Players Pack']} ]},
]

ADVANCED = [
{'name':'Cultural Exchange','groupRewards':['Rare Players Pack','5,000 coins'],'challenges':[
 {'name':'France to Spain','requirements':['Min. 2 Players from France + LaLiga Santander','Players from different Leagues: Exactly 2','Max 6 Players from the same League','Max 4 Players from the same Nation','Min. Team Rating: 79','Min. Team Chemistry: 100','Number of Players in the Squad: 11'],'rewards':['Jumbo Premium Gold Pack','1,000 coins']},
 {'name':'Germany to England','requirements':['Min. 1 Players from Germany + Premier League','Max 3 Players from the same League','Players from different Nations: Min 3','Min. Team Rating: 79','Min. Team Chemistry: 100','Number of Players in the Squad: 11'],'rewards':['Premium Gold Players Pack']},
 {'name':'Spain to Germany','requirements':['Min. 2 Players from Spain + Bundesliga','Players from different Leagues: Exactly 4','Max 3 Players from the same League','Players from different Nations: Exactly 4','Min. Team Rating: 80','Min. Team Chemistry: 95','Number of Players in the Squad: 11'],'rewards':['Mega Pack']},
 {'name':'French Connection','requirements':['Players from Ligue 1 Conforama: Min 4','France + Ligue 1 Conforama Players: Exactly 0','Max 4 Players from the same League','Max 2 Players from the same Nation','Min. Team Rating: 80','Min. Team Chemistry: 95','Number of Players in the Squad: 11'],'rewards':['Prime Gold Players Pack']} ]},
{'name':'League and Nation Hybrid','groupRewards':['12,000 coins'],'challenges':[
 {'name':'The Puzzler','requirements':['Players from different Leagues: Exactly 2','Max 6 Players from the same League','Players from different Nations: Exactly 3','Max 4 Players from the same Nation','Min. Team Rating: 77','Min. Team Chemistry: 100','Number of Players in the Squad: 11'],'rewards':['Premium Gold Players Pack']},
 {'name':'Intermediate','requirements':['Players from different Leagues: Exactly 3','Max 4 Players from the same League','Players from different Nations: Exactly 5','Max 3 Players from the same Nation','Min. Team Rating: 78','Min. Team Chemistry: 100','Number of Players in the Squad: 11'],'rewards':['1,500 coins','Rare Gold Pack']},
 {'name':'Tough','requirements':['Players from different Leagues: Exactly 6','Max 2 Players from the same League','Players from different Nations: Exactly 6','Max 2 Players from the same Nation','Min. Team Rating: 80','Min. Team Chemistry: 89','Number of Players in the Squad: 11'],'rewards':['Mega Pack']},
 {'name':'Hybrid Master','requirements':['Players from different Leagues: Exactly 7','Players from different Nations: Exactly 9','Min. Team Rating: 82','Min. Team Chemistry: 86','Number of Players in the Squad: 11'],'rewards':['Rare Player']} ]},
{'name':'Hybrid Nations','groupRewards':['Rare Mega Pack'],'challenges':[
 {'name':'Quads','requirements':['Min. Rare Players: 4','Players from different Nations: Exactly 4','Max 3 Players from the same Nation','Min. Team Rating: 75','Min. Team Chemistry: 80','Number of Players in the Squad: 11'],'rewards':['Premium Gold Players Pack']},
 {'name':'The Six','requirements':['Players from different Nations: Exactly 6','Max 2 Players from the same Nation','Min. Team Rating: 70','Min. Team Chemistry: 85','Number of Players in the Squad: 11'],'rewards':['Premium Gold Pack']},
 {'name':'It Takes Eight','requirements':['Exactly Gold Players','Min. Rare Players: 5','Players from different Nations: Exactly 8','Max 2 Players from the same Nation','Min. Team Chemistry: 88','Number of Players in the Squad: 11'],'rewards':['Prime Gold Players Pack']},
 {'name':'National Pride','requirements':['Min. Rare Players: 7','Players from different Nations: Exactly 10','Min. Team Rating: 80','Team Chemistry: Exactly 100','Number of Players in the Squad: 11'],'rewards':['Mega Pack']} ]},
{'name':'Hybrid Leagues','groupRewards':['Rare Mega Pack'],'challenges':[
 {'name':'Rare Fives','requirements':['Min. Rare Players: 5','Players from different Leagues: Exactly 5','Max 3 Players from the same League','Min. Team Rating: 68','Min. Team Chemistry: 80','Number of Players in the Squad: 11'],'rewards':['Jumbo Gold Pack']},
 {'name':'Seven Suspects','requirements':['Players from different Leagues: Exactly 7','Max 3 Players from the same League','Min. Team Rating: 78','Min. Team Chemistry: 85','Number of Players in the Squad: 11'],'rewards':['Jumbo Premium Gold Pack']},
 {'name':'Prime Nine','requirements':['Min. Rare Players: 5','Players from different Leagues: Exactly 9','Max 2 Players from the same League','Min. Team Rating: 79','Min. Team Chemistry: 99','Number of Players in the Squad: 11'],'rewards':['Prime Gold Players Pack']},
 {'name':'First XI','requirements':['Players from different Leagues: Exactly 11','Exactly Gold Players','Min. Rare Players: 7','Min. Team Chemistry: 100','Number of Players in the Squad: 11'],'rewards':['Rare Players Pack']} ]},
]

# Verified permanent Prime ICON SBCs used as an offline integrity anchor.
# These two are deliberately bundled in full because they caught the archive
# row-order bug: the source page interleaves permanent and [LOAN] sections.
PELE_SBC = {
    'name':'Pelé','category':'Prime ICONS','repeatable':False,'sourceCompleteness':'verified-static-wefut-fifauteam',
    'groupRewards':['1x Icon Pelé'],
    'challenges':[
        {'name':'Pelé - An Icon','description':'Exchange an Icon (CF)','requirements':['Number of Icon players: exactly 1','Team chemistry: min. 3','Number of players: exactly 1'],'rewards':['1x Jumbo Rare Players Pack']},
        {'name':'Brazil','description':"Exchange a squad featuring Pelé's days with his national team",'requirements':['Players from Brazil: exactly 10','Team overall rating: min. 81','Team chemistry: min. 95','Number of players: exactly 10'],'rewards':['1x Jumbo Premium Gold Pack']},
        {'name':'International Victories','description':"Exchange a squad featuring some of Pelé's biggest international victories",'requirements':['Players from Sweden: min. 1','Players from Czech Republic: min. 1','Players from Italy: min. 1','Number of Team of the Week players: min. 3','Team overall rating: min. 81','Team chemistry: min. 90','Number of players: exactly 10'],'rewards':['1x Jumbo Premium Gold Pack']},
        {'name':'Domestic Victories','description':"Exchange a squad using TOTW players representing each of Pelé's league wins",'requirements':['Number of Team of the Week players: exactly 10','Team overall rating: min. 80','Team chemistry: min. 95','Number of players: exactly 10'],'rewards':['1x Prime Gold Players Pack']},
        {'name':'82-Rated Squad','description':'Exchange an 82-rated squad','requirements':['Team overall rating: min. 82','Team chemistry: min. 80','Number of players: exactly 11'],'rewards':['1x Jumbo Premium Gold Pack']},
        {'name':'83-Rated Squad','description':'Exchange an 83-rated squad','requirements':['Number of Team of the Week players: min. 2','Team overall rating: min. 83','Team chemistry: min. 75','Number of players: exactly 11'],'rewards':['1x Rare Gold Pack']},
        {'name':'84-Rated Squad','description':'Exchange an 84-rated squad','requirements':['Number of Team of the Week players: min. 2','Team overall rating: min. 84','Team chemistry: min. 70','Number of players: exactly 11'],'rewards':['1x Mega Pack']},
        {'name':'85-Rated Squad','description':'Exchange an 85-rated squad','requirements':['Number of Team of the Week players: min. 2','Team overall rating: min. 85','Team chemistry: min. 65','Number of players: exactly 11'],'rewards':['1x Prime Gold Players Pack']},
        {'name':'86-Rated Squad','description':'Exchange an 86-rated squad with an Icon','requirements':['Number of Icon players: exactly 1','Team overall rating: min. 86','Team chemistry: min. 60','Number of players: exactly 11'],'rewards':['1x Rare Players Pack']},
        {'name':'87-Rated Squad','description':'Exchange an 87-rated squad with an Icon','requirements':['Number of Icon players: exactly 1','Team overall rating: min. 87','Team chemistry: min. 55','Number of players: exactly 11'],'rewards':['1x Rare Mega Pack']},
        {'name':'88-Rated Squad','description':'Exchange an 88-rated squad with an Icon','requirements':['Number of Icon players: exactly 1','Team overall rating: min. 88','Team chemistry: min. 50','Number of players: exactly 11'],'rewards':['1x Jumbo Rare Players Pack']},
        {'name':'89-Rated Squad','description':'Exchange an 89-rated squad with an Icon','requirements':['Number of Icon players: exactly 1','Team overall rating: min. 89','Team chemistry: min. 50','Number of players: exactly 11'],'rewards':['1x Jumbo Rare Players Pack']},
    ]
}

ZANETTI_SBC = {
    'name':'Javier Zanetti','category':'Prime ICONS','repeatable':False,'sourceCompleteness':'verified-static-wefut-fifauteam',
    'groupRewards':['1x Icon Zanetti'],
    'challenges':[
        {'name':'Primera División','description':"Exchange a squad featuring Zanetti's days in the Primera División",'requirements':['Players from T. Córdoba: min. 1','Players from Club Atlético Banfield: min. 1','Players from same nation: max. 4','Team overall rating: min. 76','Team chemistry: min. 90','Number of players: exactly 10'],'rewards':['1x Premium Gold Pack']},
        {'name':'Inter','description':"Exchange a squad featuring Zanetti's days at Inter",'requirements':['Players from Inter: min. 2','Team overall rating: min. 78','Team chemistry: min. 90','Number of players: exactly 10'],'rewards':['1x Premium Gold Pack']},
        {'name':'Argentina','description':"Exchange a squad featuring Zanetti's time with his national team",'requirements':['Players from Argentina: min. 4','Number of leagues: min. 4','Team overall rating: min. 77','Team chemistry: min. 90','Number of players: exactly 10'],'rewards':['1x Premium Gold Pack']},
        {'name':'84-Rated Squad','description':'Exchange an 84-rated squad','requirements':['Team overall rating: min. 84','Team chemistry: min. 65','Number of players: exactly 11'],'rewards':['1x Jumbo Premium Gold Pack']},
        {'name':'85-Rated Squad','description':'Exchange an 85-rated squad','requirements':['Team overall rating: min. 85','Team chemistry: min. 60','Number of players: exactly 11'],'rewards':['1x Rare Gold Pack']},
        {'name':'86-Rated Squad','description':'Exchange an 86-rated squad','requirements':['Team overall rating: min. 86','Team chemistry: min. 55','Number of players: exactly 11'],'rewards':['1x Mega Pack']},
    ]
}

MARQUEE_WEEK_46 = {'name':'Week 46','groupRewards':['Prime Gold Players Pack'],'challenges':[
 {'name':'Toronto FC v NYCFC','requirements':['Players from Toronto FC: Min 1','Players from New York City FC: Min 1','Players from Major League Soccer: Min 4','Gold Players: Min 4','Team Chemistry: Min 80','Players in the Squad: 11'],'rewards':['Electrum Players Pack']},
 {'name':'Arsenal v Man City','requirements':['Players from Arsenal: Min 1','Players from Manchester City: Min 1','Players from Premier League: Min 5','Squad Rating: Min 78','Team Chemistry: Min 85','Players in the Squad: 11'],'rewards':['Premium Gold Players Pack']},
 {'name':'Frankfurt v Bayern','requirements':['Players from Eintracht Frankfurt + FC Bayern München: Min 3','Players from Germany: Min 5','Squad Rating: Min 75','Team Chemistry: Min 85','Players in the Squad: 11'],'rewards':['Jumbo Premium Gold Pack']},
 {'name':'Chelsea v OL Lyon','requirements':['Players from Chelsea: Min 1','Players from Olympique Lyonnais: Min 1','Same Nation Count: Max 4','Gold Players: Min 5','Team Chemistry: Min 80','Players in the Squad: 11'],'rewards':['Rare Mixed Players Pack']} ]}

def _fallback_single(name, category, req=None, rewards=None, repeatable=False):
    return {'name':name,'groupRewards':list(rewards or ['Premium Gold Pack']),'repeatable':bool(repeatable),'sourceCompleteness':'fallback-index',
            'challenges':[{'name':name,'requirements':list(req or ['Number of Players in the Squad: 11']),'rewards':[]}]}

def fallback_archive():
    sets=[]
    for s in BASIC: sets.append(dict(s,category='Basic',sourceCompleteness='verified-static'))
    for s in ADVANCED: sets.append(dict(s,category='Advanced',sourceCompleteness='verified-static'))
    for n in UPGRADE_NAMES:
        u=n.upper(); repeat=True
        if 'PREMIUM ' in u and any(x in u for x in ('PL UPGRADE','LALIGA UPGRADE','BUNDESLIGA UPGRADE','CALCIO A UPGRADE','LIGUE 1 UPGRADE')):
            req=['Exactly Gold Players','Rare Players: Exactly 11','Min. Team Chemistry: 30','Number of Players in the Squad: 11']
            reward=[n.split(' UPGRADE')[0].replace('PREMIUM ','').title()+' Premium Players Pack']
        elif any(x in u for x in ('PL UPGRADE','LALIGA UPGRADE','BUNDESLIGA UPGRADE','CALCIO A UPGRADE','LIGUE 1 UPGRADE')):
            req=['Exactly Gold Players','Min. Team Chemistry: 30','Number of Players in the Squad: 11']; reward=['Premium Gold Pack']
        elif u=='BRONZE UPGRADE': req=['Exactly Bronze Players','Min. Team Chemistry: 40','Number of Players in the Squad: 11']; reward=['2 Silver Players']
        elif u=='SILVER UPGRADE': req=['Exactly Silver Players','Min. Team Chemistry: 40','Number of Players in the Squad: 11']; reward=['3 Common Gold Players']
        elif u=='GOLD UPGRADE': req=['Exactly Gold Players','Min. Team Chemistry: 40','Number of Players in the Squad: 11']; reward=['Two Rare Gold Players Pack']
        else: req=['Min. Team Rating: 82','Min. Team Chemistry: 40','Number of Players in the Squad: 11']; reward=['Rare Gold Players Pack']
        x=_fallback_single(n,'Upgrades',req,reward,repeat);x['category']='Upgrades';sets.append(x)
    # League/Icon/Live fallback records keep the full historical index visible. An online source refresh fills their exact sub-challenges.
    for n in LEAGUE_NAMES:
        x=_fallback_single(n,'Leagues',['Players from the named club/league: Required','Min. Team Chemistry: 95','Number of Players in the Squad: 11'],['League SBC Player Reward']);x['category']='Leagues';sets.append(x)
    for n in ICON_NAMES:
        if n=='Pelé':
            sets.append(dict(PELE_SBC))
            continue
        if n=='Javier Zanetti':
            sets.append(dict(ZANETTI_SBC))
            continue
        x=_fallback_single(n,'Prime ICONS',['Icon Players: Exactly 1','Min. Team Chemistry: 3','Number of Players in the Squad: 1'],[f'{n} Prime ICON card (untradeable)']);x['category']='Prime ICONS';sets.append(x)
    x=dict(MARQUEE_WEEK_46,category='Marquee Matchups',sourceCompleteness='verified-static');sets.append(x)
    for n in POTM_NAMES:
        x=_fallback_single(n,'POTM',['Number of Players in the Squad: 11'],[n.split(' - ')[0]+' POTM card (untradeable)']);x['category']='POTM';sets.append(x)
    # The four premium league/nation SBCs were part of the FIFA 18 live archive.
    for n,r in [('Americas','Arturo Vidal Premium SBC card'),('Asia','Shinji Kagawa Premium SBC card'),('Africa','Wilfred Ndidi Premium SBC card'),('Europe','Marcus Rashford Premium SBC card')]:
        x=_fallback_single(n,'Live',['Complete the nation challenges in this group'],[r]);x['category']='Live';sets.append(x)
    for n in LIVE_NAMES:
        x=_fallback_single(n,'Live',['Number of Players in the Squad: 11'],['Archived FIFA 18 SBC reward']);x['category']='Live';sets.append(x)
    return {'schemaVersion':VERSION,'generatedAt':0,'source':'bundled-fallback','sets':sets,'sources':SOURCE_URLS}

def _plain_fragment(s):
    s=re.sub(r'<[^>]+>',' ',s,flags=re.S);s=_html.unescape(s);s=re.sub(r'\s+',' ',s).strip();return s

def _html_lines(raw):
    # Mark real h3 headings before flattening so the archive grouping survives.
    raw=re.sub(r'<h3\b[^>]*>(.*?)</h3>',lambda m:'\n@@SET@@ '+_plain_fragment(m.group(1))+'\n',raw,flags=re.I|re.S)
    raw=re.sub(r'<(?:br|hr)\b[^>]*>','\n',raw,flags=re.I)
    raw=re.sub(r'</(?:p|li|div|section|h1|h2|h4|h5|h6|tr|td|th)>','\n',raw,flags=re.I)
    raw=re.sub(r'<[^>]+>',' ',raw,flags=re.S);raw=_html.unescape(raw).replace('\xa0',' ')
    out=[]
    for line in raw.splitlines():
        line=re.sub(r'\s+',' ',line).strip()
        if not line:continue
        line=re.sub(r'^[✔️?ℹ️\u2022\-]+\s*','',line).strip()
        if line:out.append(line)
    return out

def _parse_sections(raw,category):
    lines=_html_lines(raw);sections=[];cur=None
    for line in lines:
        if line.startswith('@@SET@@ '):
            if cur:sections.append(cur)
            cur={'name':line[8:].strip(),'meta':[],'body':[]}
        elif cur:
            cur['body'].append(line)
    if cur:sections.append(cur)
    # Weed out modern-page/sidebar h3s by requiring SBC-ish structure.
    useful=[]
    for s in sections:
        txt='\n'.join(s['body'][:160])
        if 'REQUIREMENTS' in txt or 'GROUP REWARDS' in txt or re.search(r'\bChallenge',txt,re.I):useful.append(s)
    return [_section_to_set(s,category) for s in useful]

def _section_to_set(sec,category):
    body=list(sec.get('body',[]));name=str(sec.get('name','')).strip();repeatable=any('Repeatable' in x and 'Non-Repeatable' not in x for x in body[:20])

    # Find only real sequential challenge headings. Reward quantities such as
    # "1 x Rare Gold Pack" and metadata such as "4 Challenges" must never be
    # interpreted as challenge starts.
    starts=[];expected=1
    for j,x in enumerate(body):
        m=re.match(r'^(\d+)\s+(.+)$',x)
        if not m:continue
        n=int(m.group(1));tail=m.group(2).strip();low=tail.lower()
        if n!=expected or n>40:continue
        if re.match(r'^[x×]\b',tail,re.I) or re.match(r'^challenges?\b',low):continue
        if re.search(r'\b(2017|2018)\b',x):continue
        starts.append((j,tail));expected+=1
    if not starts:starts=[(0,name)]
    first_start=starts[0][0]

    # Group rewards live between GROUP REWARDS and the first numbered challenge.
    # This is intentionally bounded by first_start so rows such as "1 Rare Player"
    # are preserved without accidentally swallowing challenge headings.
    group=[]
    try:g=next(j for j,x in enumerate(body[:max(80,first_start+1)]) if x.upper()=='GROUP REWARDS')
    except StopIteration:g=-1
    if g>=0:
        for x in body[g+1:first_start]:
            if _is_reward_line(x):group.append(x)

    set_desc=''
    for x in body[:first_start if first_start>0 else 40]:
        xu=x.upper()
        if xu in ('GROUP REWARDS','REQUIREMENTS','REWARDS'):continue
        if re.search(r'^(?:\d+\s+Challenges?|Non-Repeatable|Repeatable|From |Available since)',x,re.I):continue
        if len(x)>12:set_desc=x;break

    challenges=[]
    for k,(st,cname) in enumerate(starts):
        en=starts[k+1][0] if k+1<len(starts) else len(body);chunk=body[st:en]
        req=[];rew=[];cdesc=''
        try:ridx=next(j for j,x in enumerate(chunk) if x.upper()=='REQUIREMENTS')
        except StopIteration:ridx=-1
        try:widx=next(j for j,x in enumerate(chunk) if x.upper()=='REWARDS')
        except StopIteration:widx=-1
        if ridx>=0:
            stop=widx if widx>ridx else len(chunk)
            req=[x for x in chunk[ridx+1:stop] if _is_requirement_line(x)]
            for x in chunk[1:ridx]:
                if not re.search(r'^(?:\d+\s+Challenges?|Non-Repeatable|Repeatable|From |Available since)',x,re.I) and x.upper() not in ('GROUP REWARDS',):
                    if len(x)>8:cdesc=x;break
        if widx>=0:
            rew=[x for x in chunk[widx+1:] if _is_reward_line(x)]
        if req or rew or len(starts)==1:
            desc=cdesc or set_desc
            if not cdesc:
                if category=='POTM':
                    player=name.split(' - ',1)[0].strip()
                    if _archive_name_key(cname)==_archive_name_key(player):
                        desc=f"Exchange one of {player}'s special items as the first step towards his POTM."
                    else:
                        desc=f"Exchange a squad meeting the {cname} requirements towards {player}'s POTM."
                elif category=='Prime ICONS':
                    desc=f"Exchange a squad towards earning the Prime version of {name}."
                elif not desc:
                    desc=f"Complete the {cname} FIFA 18 Squad Building Challenge."
            challenges.append({'name':cname,'description':desc,'requirements':req,'rewards':rew})
    if not challenges:challenges=[{'name':name,'description':set_desc,'requirements':['Number of Players in the Squad: 11'],'rewards':[]}]
    return {'name':name,'description':set_desc,'category':category,'repeatable':repeatable,'groupRewards':_clean_reward_lines(group),'challenges':challenges,'sourceCompleteness':'archive-page'}

def _is_requirement_line(x):
    y=x.lower()
    if not x or x.startswith('Image:') or x.startswith('©'):return False
    keys=('player','players','team rating','squad rating','chemistry','clubs','club','leagues','league','nations','nation','nationalit','rare','gold','silver','bronze','icon','totw','team of the week','tots','season')
    return any(k in y for k in keys) and len(x)<220

def _is_reward_line(x):
    y=x.lower()
    if not x or x.upper() in ('REQUIREMENTS','GROUP REWARDS'):return False
    if any(k in y for k in ('pack','player','coin','card','item')) and len(x)<180:return True
    return False

def _clean_reward_lines(rows):
    out=[]
    for x in rows:
        if _is_reward_line(x):out.append(x)
    return out

def _download(url,timeout=12):
    req=urllib.request.Request(url,headers={'User-Agent':'Mozilla/5.0 FIFA18LocalFUT/0.8.9.36','Accept':'text/html,*/*','Accept-Language':'en-GB,en;q=0.9'})
    with urllib.request.urlopen(req,timeout=timeout) as r:return r.read().decode('utf-8','replace')

def _archive_name_key(value,strip_loan=True):
    value=unicodedata.normalize('NFKD',str(value or ''))
    value=''.join(ch for ch in value if not unicodedata.combining(ch)).lower()
    if strip_loan:
        value=re.sub(r'\[(?:20-match )?loan\]|\((?:20-match )?loan\)|\bloan\b',' ',value)
    value=re.sub(r'\bprime\b|\bicons?\b|\bsbc\b',' ',value)
    value=re.sub(r'[^a-z0-9]+',' ',value)
    return re.sub(r'\s+',' ',value).strip()

def _fallback_set_for(fallback,category,name):
    key=_archive_name_key(name)
    for row in fallback.get('sets',[]):
        if row.get('category')==category and _archive_name_key(row.get('name'))==key:
            return dict(row)
    return None

def _stabilise_archive_rows(category, rows, fallback):
    """Keep source data attached to the set that actually owns it.

    The Prime ICON page interleaves permanent and [LOAN] sections.  v4 renamed
    parsed rows by ordinal, which made Pelé [LOAN] become Javier Zanetti.  This
    function never renames unrelated source blocks: Icons are exact-name matched,
    and an unmatched release-index entry falls back to its own bundled record.
    """
    labels={
        'Upgrades':UPGRADE_NAMES,
        'Leagues':LEAGUE_NAMES,
        'Prime ICONS':ICON_NAMES,
        'POTM':POTM_NAMES,
        'Live':LIVE_NAMES,
    }.get(category)
    rows=[dict(x) for x in (rows or []) if isinstance(x,dict)]
    if labels:
        if category=='Prime ICONS':
            source=[x for x in rows if 'loan' not in str(x.get('name','')).lower()]
            matched=[];used=set()
            for label in labels:
                lk=_archive_name_key(label);best=None;best_i=None
                for i,row in enumerate(source):
                    if i in used:continue
                    if _archive_name_key(row.get('name',''))==lk:
                        best=row;best_i=i;break
                if best is None:
                    best=_fallback_set_for(fallback,category,label) or _fallback_single(label,category)
                    best['sourceCompleteness']='fallback-integrity-anchor'
                else:
                    used.add(best_i);best=dict(best);best['sourceHeading']=best.get('name','');best['name']=label
                matched.append(best)
            rows=matched
        elif category=='POTM':
            # The POTM page repeats the same footballer across different months
            # (Salah x3, Kane x2) and interleaves loan versions. Match permanent
            # rows by player identity *and occurrence order* instead of collapsing
            # them into one dictionary key.
            source=[x for x in rows if 'loan' not in str(x.get('name','')).lower()]
            matched=[];used=set()
            for label in labels:
                player=label.split(' - ',1)[0].strip();pk=_archive_name_key(player)
                best=None;best_i=None
                for i,row in enumerate(source):
                    if i in used:continue
                    rk=_archive_name_key(row.get('name',''))
                    if rk==pk or pk in rk or rk in pk:
                        best=row;best_i=i;break
                if best is None:
                    best=_fallback_set_for(fallback,category,label) or _fallback_single(label,category)
                    best['sourceCompleteness']='fallback-integrity-anchor'
                else:
                    used.add(best_i);best=dict(best);best['sourceHeading']=best.get('name','');best['name']=label
                matched.append(best)
            rows=matched
        else:
            # Live has permanent and loan sets with otherwise identical names, so
            # preserve the LOAN token when matching that category.
            strip_loan=(category!='Live')
            by_key={}
            for row in rows:
                k=_archive_name_key(row.get('name',''),strip_loan=strip_loan)
                if k and k not in by_key:by_key[k]=row
            matched=[]
            for label in labels:
                lk=_archive_name_key(label,strip_loan=strip_loan)
                row=by_key.get(lk) or _fallback_set_for(fallback,category,label)
                if row is None:continue
                row=dict(row);row['name']=label;matched.append(row)
            if len(matched)>=max(1,int(len(labels)*0.70)):
                rows=matched
            else:
                rows=rows[:len(labels)]
                existing={_archive_name_key(x.get('name'),strip_loan=strip_loan) for x in rows}
                for label in labels:
                    fb=_fallback_set_for(fallback,category,label)
                    k=_archive_name_key(label,strip_loan=strip_loan)
                    if fb and k not in existing:
                        rows.append(fb);existing.add(k)
                rows=rows[:len(labels)]
    if category=='Live':
        regional=[x for x in fallback['sets'] if x.get('category')=='Live' and x.get('name') in ('Americas','Asia','Africa','Europe')]
        rows=regional+rows
    return rows


def refresh_archive(cache_path,timeout=12):
    fallback=fallback_archive();errors={};counts={};by_category={}
    minimum={'Basic':3,'Advanced':4,'Upgrades':55,'Leagues':16,'POTM':9,'Prime ICONS':35,'Live':240}
    def fetch_one(item):
        category,url=item
        try:
            raw=_download(url,timeout=timeout);rows=_parse_sections(raw,category)
            if category=='Marquee Matchups':
                exact=[x for x in rows if re.search(r'WEEK\s*46',x.get('name',''),re.I)]
                rows=exact[:1] if exact else [dict(MARQUEE_WEEK_46,category=category,sourceCompleteness='verified-static')]
            else:
                need=minimum.get(category,1)
                if len(rows)<need:raise ValueError(f'incomplete archive parse: {len(rows)} < {need}')
                rows=_stabilise_archive_rows(category,rows,fallback)
            if not rows:raise ValueError('no SBC sections parsed')
            return category,rows,None
        except Exception as exc:
            rows=[x for x in fallback['sets'] if x.get('category')==category]
            return category,rows,f'{type(exc).__name__}: {exc}'
    # The eight historical archive pages are independent. Fetch them together so
    # first-run startup is bounded by one request timeout rather than eight.
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(SOURCE_URLS)) as ex:
        for category,rows,err in ex.map(fetch_one,SOURCE_URLS.items()):
            by_category[category]=rows;counts[category]=len(rows)
            if err:errors[category]=err
    all_sets=[]
    for category in SOURCE_URLS:
        all_sets.extend(by_category.get(category,[]))
    doc={'schemaVersion':VERSION,'generatedAt':int(time.time()),'source':'fifauteam-archive-pages+identity-matched-bundled-fallback',
         'sets':all_sets,'sources':SOURCE_URLS,'counts':counts,'errors':errors}
    p=Path(cache_path);p.parent.mkdir(parents=True,exist_ok=True);tmp=p.with_suffix(p.suffix+'.tmp')
    tmp.write_text(json.dumps(doc,ensure_ascii=False,indent=2),encoding='utf-8');os.replace(tmp,p)
    return doc

def _cache_is_complete(path):
    try:
        d=json.loads(Path(path).read_text(encoding='utf-8'))
        if int(d.get('schemaVersion',0))<VERSION or d.get('errors'):return False
        counts=d.get('counts') or {}
        expected={'Basic':3,'Advanced':4,'Upgrades':61,'Leagues':16,'Marquee Matchups':1,'POTM':9,'Prime ICONS':40,'Live':270}
        if not all(int(counts.get(k,0) or 0)==v for k,v in expected.items()):return False
        icons=[x for x in d.get('sets',[]) if x.get('category')=='Prime ICONS']
        names=[str(x.get('name','')) for x in icons]
        if names!=ICON_NAMES:return False
        by={x.get('name'):x for x in icons}
        if len((by.get('Pelé') or {}).get('challenges',[]))!=12:return False
        if len((by.get('Javier Zanetti') or {}).get('challenges',[]))!=6:return False
        if any('pelé' in str(c.get('name','')).lower() for c in (by.get('Javier Zanetti') or {}).get('challenges',[])):return False
        potm=[x for x in d.get('sets',[]) if x.get('category')=='POTM']
        if [x.get('name') for x in potm]!=POTM_NAMES:return False
        # A successful v6 refresh must contain real multi-challenge POTM data,
        # not the old one-line fallback placeholders.
        expected_min=[2,4,4,4,2,4,4,1,1]
        if any(len((x or {}).get('challenges',[]))<expected_min[i] for i,x in enumerate(potm)):return False
        if any(str(x.get('sourceCompleteness','')).startswith('fallback') for x in potm):return False
        # v7: every permanent POTM progression squad historically grants a
        # reward.  Reject old caches produced by the numeric-reward parser bug.
        if any(any(not list(c.get('rewards',[]) or []) for c in x.get('challenges',[])) for x in potm):return False
        return True
    except Exception:return False

def load_archive(cache_path=None,bundled_path=None):
    candidates=[]
    if cache_path:candidates.append(Path(cache_path))
    if bundled_path:candidates.append(Path(bundled_path))
    for p in candidates:
        try:
            doc=json.loads(p.read_text(encoding='utf-8'))
            if isinstance(doc,dict) and int(doc.get('schemaVersion',0))>=VERSION and isinstance(doc.get('sets'),list) and doc['sets']:return doc
        except Exception:pass
    return fallback_archive()

def write_fallback(path):
    p=Path(path);p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(fallback_archive(),ensure_ascii=False,indent=2),encoding='utf-8')

def stable_int(prefix,key,low=100000,span=1900000000):
    h=int(hashlib.sha1((str(prefix)+'|'+str(key)).encode('utf-8')).hexdigest()[:12],16)
    return low+(h%span)

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--refresh',action='store_true');ap.add_argument('--ensure',action='store_true');ap.add_argument('--cache');ap.add_argument('--write-fallback');ap.add_argument('--timeout',type=float,default=12);a=ap.parse_args()
    if a.write_fallback:write_fallback(a.write_fallback)
    if a.refresh or a.ensure:
        if not a.cache:raise SystemExit('--cache is required with --refresh/--ensure')
        if a.ensure and _cache_is_complete(a.cache):
            d=json.loads(Path(a.cache).read_text(encoding='utf-8'));d['cacheStatus']='current'
        else:
            d=refresh_archive(a.cache,a.timeout);d['cacheStatus']='refreshed'
        print(json.dumps({'cacheStatus':d.get('cacheStatus'),'sets':len(d['sets']),'counts':d.get('counts',{}),'errors':d.get('errors',{})},ensure_ascii=False))
if __name__=='__main__':main()
