# FIFA19 Local Server v0.2.1 — Windows Portable Preview

**[ดาวน์โหลด Windows Portable ZIP](https://github.com/atomzeedzad-dotcom/Fifa19back/releases/download/v0.2.1/FIFA19LocalServer-v0.2.1-Windows-x64.zip)**

ดาวน์โหลดแล้วใช้ **Extract All / แตกไฟล์ทั้งหมด** จากนั้นดับเบิลคลิก
`FIFA19LocalServer.exe` แล้วกด **เปิด Local Server**
รวม Python และส่วนประกอบไว้แล้ว ไม่ต้องพิมพ์คำสั่งหรือติดตั้ง Python
เก็บโฟลเดอร์ `_internal`, `data` และ `config` ไว้ข้าง `.exe`

**รุ่นนี้เปิด launcher และ server ได้ แต่ยังเข้าเล่น FUT19 ในเกมจริงไม่ได้**
การเชื่อมตัวเกมต้องพัฒนาต่อเมื่อมี FIFA19.exe ให้ทดสอบ
ไฟล์ที่ใช้สำหรับคนทั่วไปอยู่ใน **Releases**; ZIP ของ **Code → Download ZIP** เป็น source สำหรับพัฒนา

เตรียม local backend สำหรับ FIFA19 โดยอ้างอิงระบบจาก `D:\Fifaback18`
**ยังไม่ใช่รุ่นที่ยืนยันว่าเปิด FUT19 ในเกมได้** เพราะตอนทำงานไม่มีไฟล์เกม FIFA19
ส่วน routing, TLS trust/pinning, Origin/EA session และรูปแบบคำตอบที่ตัวเกมต้องการ
ต้องตรวจจากเกมจริงก่อน ไม่สามารถใช้แพตช์ EXE/DLL ของ FIFA18 กับ FIFA19 ได้

## วิธีเปิด

สำหรับคนทั่วไป ใช้ Windows Portable ZIP ด้านบน ไม่ต้องใช้คำสั่ง
ไม่ต้องใช้สิทธิ์ Administrator เพื่อเปิด backend

1. แตก ZIP แล้วดับเบิลคลิก `FIFA19LocalServer.exe`
2. กด **เปิด Local Server**
3. กด **ตรวจทุกบริการ** เพื่อตรวจ Redirector TLS, Blaze, EASW และ FUT ทั้งสองพอร์ต
4. กด **หยุด Server** ก่อนปิดโปรแกรม

ผู้พัฒนาที่รัน source ต้องมี Python 3.10 ขึ้นไปพร้อม tkinter และติดตั้ง
`python -m pip install -r requirements.txt`
จากนั้นใช้ `Fifaback19.bat` หรือ `python server/localfut19.py`
เปิดแบบ console ได้ด้วย `RUN_LOCAL_FUT19.cmd` และหยุดด้วย Ctrl+C

## บริการและข้อมูล

| บริการ | ที่อยู่เริ่มต้น |
|---|---|
| Redirector HTTPS | `127.0.0.1:42330` |
| Blaze FIRE2/TDF TCP | `127.0.0.1:10151` |
| EASW HTTP | `127.0.0.1:42332` |
| FUT HTTP | `127.0.0.1:9099` และ `127.0.0.1:9199` |

ตั้งค่าใน `config/server.json`; พอร์ตแยกจาก FIFA18 เพื่อให้เปิดคู่กันได้
พอร์ตเหล่านี้เป็นค่าที่ใช้พัฒนา backend ยังไม่ใช่ค่าที่ตรวจยืนยันจากตัวเกม FIFA19
ตรวจสถานะที่ `http://127.0.0.1:9099/health`

API ใช้ `/ut/game/fifa19/...` และ `/pow/store/game/fifa19/...`
มี account bootstrap, userMassInfo, สโมสร/ทีมและการบันทึก SQLite,
สัญญารายการแพ็กแบบว่าง และโค้ดตลาดซื้อขายที่สืบทอดจากต้นแบบ
**การที่ API ตอบได้ไม่ยืนยันว่าเมนู การเปิดแพ็ก หรือการแข่งขันในเกมจะทำงาน**
EASW ยังเป็น stub; ต้องตรวจ schema และ asset ของ FIFA19 ต่อ

เซฟอยู่ที่ `%LOCALAPPDATA%\FIFA19LocalFUT\fut19-local.sqlite3`
logs และ raw Blaze frames อยู่ในโฟลเดอร์เดียวกัน
ทดสอบแบบแยกเซฟได้ด้วย `python server/localfut19.py --runtime .\runtime\sandbox`
ชุดนี้ไม่มีตัวเกมและไม่มีตัวติดตั้ง patch/client routing

## ฐานนักเตะและข้อจำกัด

นำเข้า base players 15,462 คนจากประวัติ
[kafagy/fifa-FUT-Data](https://github.com/kafagy/fifa-FUT-Data/blob/master/FutBinCards19.csv)
พร้อมเก็บไฟล์ต้นฉบับและ MIT license ใน `data`
EA asset ID มาจากชื่อไฟล์ภาพนักเตะ ไม่ใช่คอลัมน์ ID ที่เป็นเลขแถวของผู้เก็บข้อมูล
ข้อมูลตัวอย่าง Ronaldo ตรงกับ Juventus, rating 94, ST ใน FIFA19

เป็น snapshot บางช่วง ไม่ใช่ฐาน FUT19 ทั้งฤดูกาล
ไม่เดา ID ของลีกที่ไม่มีหลักฐาน (ใช้ 0), base rare/common ยังไม่ยืนยัน (ใช้ common ชั่วคราว)
ไม่โหลด special cards/Icons ของ FIFA18 มาเป็น FIFA19
ชุดเริ่มต้นใช้ 23 นักเตะ bronze จากฐาน FIFA19 แต่ kit/badge/stadium,
consumables, pack metadata และบางส่วนของ wire schema ยังสืบทอดต้นแบบและไม่ได้ยืนยันกับ FIFA19

Draft, World Cup, Division Rivals, Squad Battles, SBC archive, match dispatch และการซื้อแพ็กยังปิดไว้และตอบ HTTP 501
ร้านค้ายังไม่ประกาศขายแพ็ก เพราะข้อมูลที่มีไม่ยืนยันการ์ด rare และรายการสินค้า FIFA19
Draft ในต้นแบบพบการเลือก slot เพิ่มจากคำสั่งซ้ำ จึงไม่เปิดใช้ fallback นั้นใน derivative นี้
World Cup DLC ของ FIFA18 ไม่ควรถูกนำไปอ้างเป็นโหมด FIFA19

TLS certificate/key สำหรับพัฒนาจะสร้างอัตโนมัติแยกในเครื่องแต่ละคน
แพ็กเกจไม่รวม private key ของผู้พัฒนา และไม่ต้องตั้งค่า TLS ผ่านคำสั่ง
การทดสอบ handshake ใช้ client ที่ไม่ตรวจ trust; ยังไม่ยืนยันว่า FIFA19 จะยอมรับ certificate นี้
server ฟังเฉพาะ loopback และไม่มี background download/crawl

## ทดสอบและทำต่อเมื่อมีตัวเกม

รัน `TEST_LOCAL_FUT19.cmd` หรือ `python -m unittest discover -s tests -v`
ใช้ฐาน SQLite ชั่วคราวและพอร์ตชั่วคราว แยกจากเซฟใช้งาน

เมื่อมี FIFA19.exe เลือกไฟล์ใน launcher แล้วกด **รายงานตัวเกม** ได้เลย
รุ่น v0.2.1 ตรวจ EXE และ DLL ที่อยู่ข้างกัน รวม CardsDLL และ OriginSDK
บันทึก architecture, SHA-256, endpoint และ session API ที่พบ โดยไม่แก้ไฟล์เกม
รายงานอยู่ใน `fifa19-client-report.json` ส่วนผลตรวจ server อยู่ใน `connection-report.json`
อ่านเส้นทางการทำงานของ FIFA18 และส่วนที่นำมาใช้ได้ใน [FIFA18_PORT_REVIEW.md](docs/FIFA18_PORT_REVIEW.md)
ผู้พัฒนาสามารถสร้างรายงานเดียวกันแบบอ่านอย่างเดียวผ่านคำสั่ง:

```powershell
python tools/inspect_game.py "D:\FIFA 19"
```

ขั้นถัดไปคือยืนยันเวอร์ชันเกม, redirector hostname/port, TLS pin,
EA session, FIRE2/TDF command contracts, และ FUT19 onboarding/schema
แล้วทำวิธี client routing แบบสำรองและย้อนกลับได้ก่อนทดสอบเข้า FUT19 จริง
การสแกน executable อย่างเดียวไม่ได้ยืนยันว่าตัวเกมเชื่อมได้

## โครงสร้าง

- `server/localfut19.py`: ตัวรันและ adapter ของ FIFA19
- `server/fut19_profile.py`: ติดตั้งฐานนักเตะและชุดเริ่มต้นก่อน engine สร้างเซฟ
- `engine/`: snapshot โค้ดจาก FIFA18 พร้อม bootstrap changes ที่ตรวจได้
- `docs/reference-manifest.json`: SHA-256 และรายการการปรับ snapshot
- `tools/import_fut19.py`: สร้างฐาน JSON จาก CSV ที่เก็บไว้ โดยไม่ต้องออนไลน์
- `tools/create_reference_snapshot.py`: สร้าง snapshot ใหม่จากต้นแบบ (ไม่จำเป็นสำหรับใช้งาน)
- `tests/`: ทดสอบ TLS, Blaze, HTTP, isolation และ persistence
- `portable_entry.py`: ทางเข้าของโปรแกรม Windows แบบ GUI และ server worker
- `tools/build_portable.py`: สร้าง EXE และ ZIP สำหรับแจก
- `tools/verify_portable.py`: ทดสอบ EXE ที่แตกจาก ZIP โดยไม่มี Python บน PATH
- `tools/publish_release.py`: อัปโหลดแพ็กเกจที่ผ่านการตรวจไป GitHub Releases

ผู้ดูแลสร้างแพ็กเกจใหม่ได้โดยติดตั้ง `requirements-build.txt` แล้วใช้
`python tools/build_portable.py` และ `python tools/verify_portable.py`

ไม่ควรรัน `engine/localfut18_server.py` โดยตรง; ใช้ launcher หรือ `server/localfut19.py`
เพราะ adapter เป็นส่วนที่กำหนด routing/config และปิดฟังก์ชันที่ยังไม่รองรับ
