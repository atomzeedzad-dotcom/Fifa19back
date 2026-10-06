# FIFA19 Local Server — Backend Preview

เตรียม local backend สำหรับ FIFA19 โดยอ้างอิงระบบจาก `D:\Fifaback18`
**ยังไม่ใช่รุ่นที่ยืนยันว่าเปิด FUT19 ในเกมได้** เพราะตอนทำงานไม่มีไฟล์เกม FIFA19
ส่วน routing, TLS trust/pinning, Origin/EA session และรูปแบบคำตอบที่ตัวเกมต้องการ
ต้องตรวจจากเกมจริงก่อน ไม่สามารถใช้แพตช์ EXE/DLL ของ FIFA18 กับ FIFA19 ได้

## วิธีเปิด

ต้องมี Python 3.10 ขึ้นไป พร้อม tkinter (Python สำหรับ Windows ปกติมีให้)
ไม่ต้องติดตั้ง pip packages และไม่ต้องใช้สิทธิ์ Administrator เพื่อเปิด backend

1. ดับเบิลคลิก `Fifaback19.bat`
2. กด **เปิด Server**
3. กด **ตรวจการเชื่อมต่อ** เพื่อดูสถานะ FUT API
4. กด **หยุด Server** ก่อนปิดโปรแกรม

เปิดแบบ console ได้ด้วย `RUN_LOCAL_FUT19.cmd` และหยุดด้วย Ctrl+C
หรือใช้ `python server/localfut19.py`

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

TLS certificate เป็น certificate เพื่อพัฒนาที่คัดจากต้นแบบ
การทดสอบ handshake ใช้ client ที่ไม่ตรวจ trust; ยังไม่ยืนยันว่า FIFA19 จะยอมรับ certificate นี้
server ฟังเฉพาะ loopback และไม่มี background download/crawl

## ทดสอบและทำต่อเมื่อมีตัวเกม

รัน `TEST_LOCAL_FUT19.cmd` หรือ `python -m unittest discover -s tests -v`
ใช้ฐาน SQLite ชั่วคราวและพอร์ตชั่วคราว แยกจากเซฟใช้งาน

เมื่อมี FIFA19.exe ให้สร้างรายงานแบบอ่านอย่างเดียว:

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

ไม่ควรรัน `engine/localfut18_server.py` โดยตรง; ใช้ launcher หรือ `server/localfut19.py`
เพราะ adapter เป็นส่วนที่กำหนด routing/config และปิดฟังก์ชันที่ยังไม่รองรับ
