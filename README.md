# 🤖 Discord AI + Music Bot (ฟรีทั้งหมด)

บอท Discord ที่
- **คุยกับ AI ได้ทุกเรื่อง**: `@บอท คำถาม` ในห้องไหนก็ได้ (หรือ DM) บอทจะอ่านแล้วตอบ จำบทสนทนาล่าสุดในห้อง และถ้าคุณ reply ข้อความของคนอื่นแล้ว @บอท บอทจะอ่านข้อความนั้นด้วย
- **ค้นเว็บได้เอง**: ถ้าถามเรื่องล่าสุด เช่น ข่าว ราคาทอง ผลบอล AI จะค้นเว็บก่อนตอบและแนบลิงก์แหล่งที่มา (ใช้ได้กับโมเดล `openai/gpt-oss-*` บน Groq)
- **เปิดเพลง**: `/play` รับลิงก์ **YouTube, YouTube Music, Spotify** (เพลงเดี่ยว/อัลบั้ม/เพลย์ลิสต์) หรือพิมพ์ชื่อเพลงก็ได้

ใช้ LLM ฟรีผ่าน API แบบ OpenAI-compatible: **Groq** (ค่าเริ่มต้น), **Google Gemini** หรือ **OpenRouter** และใส่หลายโมเดลไว้ได้ ถ้าตัวแรกโดน rate limit บอทจะสลับไปตัวถัดไปเอง

> **เรื่อง Spotify:** Spotify ไม่อนุญาตให้บอทสตรีมเสียง บอทจึงดึงแค่ "ชื่อเพลง + ศิลปิน" จากลิงก์ (ไม่ต้องใช้ API key) แล้วไปค้นหาเพลงเดียวกันบน YouTube มาเล่นแทน แบบเดียวกับบอทเพลงทั่วไป

---

## คำสั่ง

| คำสั่ง | ทำอะไร |
|---|---|
| `@บอท <คำถาม>` | ถาม AI (ใช้ได้ทุกห้อง + DM) |
| `/ask` `/reset` | ถาม AI ผ่าน slash command / ล้างความจำบทสนทนาในห้อง |
| `/play <ลิงก์หรือชื่อเพลง>` | เปิดเพลง หรือเพิ่มเข้าคิว — ลิงก์เพลย์ลิสต์ / Mix ของ YouTube (`&list=...`) จะเข้าคิวทั้งลิสต์ โดยเริ่มจากเพลงในลิงก์ (Mix ดึง 50 เพลงแรก) |
| `/pause` `/resume` | หยุดชั่วคราว / เล่นต่อ |
| `/skip` | ข้ามเพลง |
| `/stop` `/leave` | หยุด ล้างคิว และออกจากห้องเสียง |
| `/queue [หน้า]` | ดูคิว |
| `/nowplaying` | ดูเพลงที่เล่นอยู่ (มีปุ่ม ⏯️ ⏭️ 🔁 ⏹️) |
| `/volume <0-150>` | ปรับเสียง |
| `/loop <ปิด/วนเพลงนี้/วนทั้งคิว>` | ตั้งโหมดวนซ้ำ |
| `/shuffle` `/remove <ลำดับ>` `/clear` | สลับคิว / ลบเพลงจากคิว / ล้างคิว |
| `/help` | ดูวิธีใช้ |

บอทจะออกจากห้องเสียงเองเมื่อคิวว่างเกิน 3 นาที หรือเมื่อไม่มีใครอยู่ในห้องเสียงเกิน 1 นาที

---

## ขั้นที่ 1: สร้างบอทใน Discord

1. ไปที่ <https://discord.com/developers/applications> → **New Application**
2. เมนู **Bot** → กด **Reset Token** แล้วคัดลอก token เก็บไว้ (ห้ามให้ใครเห็น)
3. ในหน้า **Bot** เดิม เลื่อนลงไปเปิด **MESSAGE CONTENT INTENT** แล้วกด Save *(ถ้าไม่เปิด บอทจะอ่านข้อความไม่ได้)*
4. เมนู **General Information** → คัดลอก **Application ID**
5. เชิญบอทเข้าเซิร์ฟเวอร์ โดยเปิดลิงก์นี้ (เปลี่ยน `APP_ID` เป็น Application ID ของคุณ)

   ```
   https://discord.com/oauth2/authorize?client_id=APP_ID&scope=bot+applications.commands&permissions=274881137664
   ```
   (สิทธิ์ที่ขอ: ดูห้อง, ส่งข้อความ, ส่งข้อความในเธรด, ฝัง embed, อ่านประวัติข้อความ, เข้าห้องเสียง, พูดในห้องเสียง)

## ขั้นที่ 2: ขอ API key ของ LLM ฟรี

- **Groq** (แนะนำ เร็วมาก): <https://console.groq.com/keys> สมัครด้วย Google ได้ ไม่ต้องใช้บัตร
- หรือ **Gemini**: <https://aistudio.google.com/apikey>
- หรือ **OpenRouter**: <https://openrouter.ai/keys> (ใช้โมเดล `openrouter/free`)

แต่ละเจ้ามีโควตาฟรีต่อนาทีและต่อวัน เช่น Groq ให้ประมาณ 30 ครั้ง/นาที, 1,000 ครั้ง/วัน และ 8,000 token/นาทีต่อโมเดล ถ้าใส่หลายโมเดลไว้ใน `LLM_MODEL` โควตาก็จะรวมกันได้มากขึ้น และถ้าโควตานาทีนั้นเต็ม บอทจะรอไม่เกิน 20 วินาทีแล้วลองใหม่ให้เอง

> การค้นเว็บแต่ละครั้งใช้ราว 7,000–8,000 token (เกือบเต็มโควตา 1 นาทีของโมเดลนั้น) ถ้าในเซิร์ฟเวอร์ถามเรื่องที่ต้องค้นพร้อมกันหลายคน บอทจะสลับไปโมเดลสำรอง หรือตอบช้าลงเล็กน้อย ปิดการค้นเว็บได้ด้วย `LLM_WEB_SEARCH=false`

## ขั้นที่ 3: ตั้งค่า

คัดลอก `.env.example` ไปเป็น `.env` แล้วกรอก `DISCORD_TOKEN` และ `LLM_API_KEY` (ช่องอื่นใช้ค่าเริ่มต้นได้เลย)

```bash
cp .env.example .env
```

> ระหว่างทดสอบ ให้ใส่ `DEV_GUILD_ID` เป็น ID เซิร์ฟเวอร์ของคุณ (คลิกขวาที่ชื่อเซิร์ฟเวอร์ → Copy Server ID) เพื่อให้ slash command ขึ้นทันที

---

## รันบนเครื่องตัวเอง (Windows)

ต้องมี **Python 3.11 ขึ้นไป** (แนะนำ 3.12), **FFmpeg** และ **Deno** (yt-dlp ต้องใช้ Deno ถอดรหัสลิงก์ YouTube)

```bash
winget install Python.Python.3.12
```
```bash
winget install Gyan.FFmpeg
```
```bash
winget install DenoLand.Deno
```

ปิดแล้วเปิด terminal ใหม่ จากนั้น

```bash
py -3.12 -m venv .venv
```
```bash
.venv\Scripts\pip install -r requirements.txt
```
```bash
.venv\Scripts\python bot.py
```

เห็น `Logged in as ...` แปลว่าบอทออนไลน์แล้ว ลองพิมพ์ `@บอท สวัสดี` หรือเข้าห้องเสียงแล้วพิมพ์ `/play lofi`

### เปิด/ปิดบอทเบื้องหลัง (Windows) — ดับเบิลคลิกไฟล์ได้เลย

| ไฟล์ | ทำอะไร |
|---|---|
| `start-bot.bat` | เปิดบอทเบื้องหลัง (ไม่มีหน้าต่าง รีสตาร์ตเองใน 10 วินาทีถ้าล่ม) |
| `stop-bot.bat` | ปิดบอท |
| `autostart-on.bat` | ให้บอทเปิดเองทุกครั้งที่ล็อกอิน Windows |
| `autostart-off.bat` | เลิกเปิดเองตอนเปิดเครื่อง (ยังเปิดเองด้วย `start-bot.bat` ได้) |

ดู log ได้ที่ `logs\bot.log` • ถ้าแก้โค้ดหรือ `.env` ให้กด `stop-bot.bat` แล้ว `start-bot.bat` เพื่อรีสตาร์ต

---

## รัน 24 ชั่วโมงฟรี

บอทต้องรันตลอดเวลา และบอทเพลงต้องใช้ FFmpeg กับการเชื่อมต่อเสียง (UDP) จึงต้องการ **VPS** จริง ๆ ไม่ใช่โฮสต์ที่หลับเองเมื่อไม่มีคนใช้ ตัวเลือกที่ฟรีถาวร ได้แก่

| ตัวเลือก | ข้อดี | ข้อเสีย |
|---|---|---|
| **Oracle Cloud Always Free** ⭐ แนะนำ | VM ฟรีตลอดไป สเปกแรง (ARM สูงสุด 4 core / RAM 24GB หรือ AMD 1GB) | ต้องใช้บัตรเครดิต/เดบิตยืนยันตัวตน (ไม่ตัดเงิน) และบางช่วง ARM อาจเต็ม |
| **Google Cloud e2-micro** | ฟรีถาวร (เฉพาะโซน US) | RAM 1GB, ต้องใช้บัตรยืนยัน |
| **คอมเก่า / Raspberry Pi ที่บ้าน** | IP บ้าน YouTube ไม่ค่อยบล็อก (เพลงเสถียรที่สุด) | เสียค่าไฟ และต้องเปิดเครื่องทิ้งไว้ |

> โฮสต์แบบ PaaS ฟรี (Render, Koyeb ฯลฯ) มักหลับเมื่อไม่มี traffic หรือจำกัดชั่วโมงใช้งาน ถ้าจะลองใช้ ให้ตั้ง `PORT` ใน `.env` แล้วบอทจะเปิด health check ให้ แต่ไม่แนะนำสำหรับบอทเพลง

### ติดตั้งบน Oracle Cloud / VPS (Ubuntu) ด้วย Docker

1. สมัคร <https://www.oracle.com/cloud/free/> → **Create a VM instance** → เลือก image **Ubuntu** และ shape แบบ **Always Free** (Ampere A1 หรือ VM.Standard.E2.1.Micro) → ดาวน์โหลด SSH key
2. SSH เข้าเครื่อง: `ssh -i key.pem ubuntu@<IP ของ VM>`
3. ติดตั้ง Docker:
   ```bash
   curl -fsSL https://get.docker.com | sudo sh
   ```
4. อัปโหลดโฟลเดอร์โปรเจกต์นี้ขึ้นไป (จะใช้ `git clone` จาก GitHub ของคุณ หรือ `scp -r` ก็ได้) แล้วสร้างไฟล์ `.env` บนเซิร์ฟเวอร์
5. รัน:
   ```bash
   sudo docker compose up -d --build
   ```
   ดู log ได้ด้วย `sudo docker compose logs -f` บอทจะรีสตาร์ตเองถ้าล่ม หรือถ้า VM รีบูต

อัปเดต yt-dlp เป็นระยะ (YouTube เปลี่ยนระบบบ่อย) ด้วยการ build ใหม่:
```bash
sudo docker compose build --no-cache && sudo docker compose up -d
```

---

## ⚠️ ปัญหา "Sign in to confirm you're not a bot" (YouTube)

YouTube มักบล็อกการดึงเพลงจาก IP ของ cloud/datacenter (Oracle, Google Cloud ฯลฯ) ถ้าเจอข้อความนี้ ให้ใช้ cookies ของ YouTube

1. ใช้ **บัญชี Google สำรอง** (ไม่ใช่บัญชีหลัก เพราะบัญชีนี้อาจโดน YouTube จับตา) ล็อกอิน YouTube ในหน้าต่าง Incognito
2. export cookies เป็นไฟล์ `cookies.txt` ด้วยส่วนขยายอย่าง "Get cookies.txt LOCALLY" แล้วปิดหน้าต่าง Incognito ทันที (ดูวิธีละเอียดใน [yt-dlp wiki](https://github.com/yt-dlp/yt-dlp/wiki/Extractors#exporting-youtube-cookies))
3. วาง `cookies.txt` ไว้ในโฟลเดอร์โปรเจกต์ ตั้งค่า `YTDL_COOKIES=/app/cookies.txt` ใน `.env` และเปิดบรรทัด `volumes` ใน `docker-compose.yml`

ถ้ารันบนเครื่องที่บ้าน ส่วนใหญ่ไม่ต้องทำขั้นตอนนี้

---

## แก้ปัญหาอื่น ๆ

- **@บอทแล้วไม่ตอบ**: ยังไม่ได้เปิด *Message Content Intent* (ขั้นที่ 1 ข้อ 3) หรือบอทไม่มีสิทธิ์ส่งข้อความในห้องนั้น
- **บอทตอบว่า "AI ตอบไม่ได้"**: API key ผิด หรือใช้โควตาฟรีหมด ให้ดู log หรือเพิ่มโมเดลสำรองใน `LLM_MODEL`
- **ไม่เห็น slash command**: รอสักครู่ หรือตั้ง `DEV_GUILD_ID` แล้วรีสตาร์ตบอท และลองรีโหลด Discord (Ctrl+R)
- **เข้าห้องเสียงแล้วไม่มีเสียง**: ตรวจว่าติดตั้ง FFmpeg แล้ว (`ffmpeg -version`) หรือตั้ง `FFMPEG_PATH` ให้ชี้ไปที่ไฟล์ ffmpeg
- **เปิดเพลงไม่ได้ทั้งที่ลิงก์ถูก**: อัปเดต yt-dlp (`pip install -U "yt-dlp[default]"`) และตรวจว่าติดตั้ง Deno แล้ว

## โครงสร้างโปรเจกต์

```
bot.py            จุดเริ่มต้น: โหลด cogs, sync slash command, health check server
cogs/chat.py      ตอบคำถามเมื่อถูก @mention, /ask, /reset
cogs/music.py     ระบบเพลง: คิว, /play, /skip, /queue, ปุ่มควบคุม ฯลฯ
core/llm.py       client สำหรับ API แบบ OpenAI-compatible (มีระบบสลับโมเดลเมื่อโดน rate limit)
core/spotify.py   อ่านรายชื่อเพลงจากลิงก์ Spotify (ไม่ต้องใช้ API key)
```
